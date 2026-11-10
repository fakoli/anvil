"""Bounded, local-Docker OpenShell qualification; never changes a gateway.

uv run --project bin python benchmarks/openshell_execution_scale.py --image IMAGE --image-id SHA256 --output /tmp/openshell-scale.json

Start with two sandboxes. Larger 8/32/128 waves require the preceding successful
receipt, native stage p95 <=15ms, and explicit aggregate resource budgets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks import execution_scale  # noqa: E402

RUN_LABEL = "anvil.execution-scale-run"
SANDBOX_LABEL = "openshell.ai/sandbox-id"
ROLE_LABEL = "openshell.ai/isolation-role"
WAVES = (2, 8, 32, 128)
# The ledger target covers the guarded append, not Git/context preparation in
# the outer scoped claim. Both durations remain separately visible in receipts.
NATIVE_STAGES = ("claim_append", "progress_append", "proof_validation", "evidence_append")
POLICY = """version: 1
filesystem_policy:
  include_workdir: false
  read_only: [/app, /bin, /usr, /lib, /lib64, /etc, /proc, /dev/urandom]
  read_write: [/sandbox, /tmp, /dev/null]
landlock:
  compatibility: hard_requirement
"""
DOCKER_FIELDS = {
    "id": ".Id", "image": ".Image", "labels": ".Config.Labels",
    "nano_cpus": ".HostConfig.NanoCpus", "memory": ".HostConfig.Memory",
    "memory_swap": ".HostConfig.MemorySwap", "pids_limit": ".HostConfig.PidsLimit",
    "device_requests": ".HostConfig.DeviceRequests", "devices": ".HostConfig.Devices",
    "mounts": ".Mounts", "privileged": ".HostConfig.Privileged",
    "network": ".HostConfig.NetworkMode", "running": ".State.Running",
}


def _write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".new")
    with temporary.open("w") as output:
        json.dump(value, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _run(arguments, *, timeout=30, cwd=None):
    result = subprocess.run(arguments, cwd=cwd, stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=timeout)
    if result.returncode:
        # These control calls contain only this run's non-secret names, limits,
        # paths and IDs; preserve bounded diagnostics for refused admission.
        message = result.stderr.decode(errors="replace")[:1024]
        raise RuntimeError(f"Control command failed ({result.returncode}): {arguments[:4]}: {message}")
    if len(result.stdout) > 2 * 1024 * 1024:
        raise RuntimeError("Control response exceeded 2 MiB")
    return result.stdout


def _docker_resources(sandbox_id, *, label=SANDBOX_LABEL):
    identifiers = _run(["docker", "ps", "-a", "--no-trunc", "--filter",
                        f"label={label}={sandbox_id}", "--format", "{{.ID}}"])
    template = "{" + ",".join(json.dumps(key) + ":{{json " + value + "}}"
                              for key, value in DOCKER_FIELDS.items()) + "}"
    return [json.loads(_run(["docker", "inspect", "--format", template, identifier]))
            for identifier in identifiers.decode().split()]


def _verify_resources(resources, sandbox_id, image_id, cpu_milli, memory_mib):
    roles = {item["labels"].get(ROLE_LABEL): item for item in resources}
    if len(resources) != 2 or set(roles) != {"sandbox", "supervisor"}:
        raise ValueError("Expected exactly one workload and one supervisor")
    for role, item in roles.items():
        expected_memory = memory_mib if role == "sandbox" else 128
        if (item["labels"].get(SANDBOX_LABEL) != sandbox_id or not item["running"]
                or item["privileged"] or item["device_requests"] or item["devices"]
                or item["nano_cpus"] != cpu_milli * 1_000_000
                or item["memory"] != expected_memory * 1024 * 1024
                or not 0 < (item["pids_limit"] or 0) <= (2048 if role == "sandbox" else 128)):
            raise ValueError(f"Resource/isolation readback failed for {role}")
        if any(mount["Type"] != "volume" or not mount["Destination"].startswith("/.openshell/")
               for mount in item["mounts"]):
            raise ValueError("Unexpected mount on qualification container")
    if roles["sandbox"]["image"] != image_id or roles["sandbox"]["network"] != "none":
        raise ValueError("Workload image or network isolation differs")
    return resources


def _resource_budget(options):
    workers = options["workers"]
    cpu = workers * options["cpu_milli"] * 2 / 1000
    memory_mib = workers * (options["memory_mib"] + 128)
    available_memory = next(int(line.split()[1]) * 1024 for line in
                            Path("/proc/meminfo").read_text().splitlines()
                            if line.startswith("MemAvailable:"))
    available_cpus = len(os.sched_getaffinity(0))
    if (cpu > options["max_total_cpu"] or memory_mib > options["max_total_memory_mib"]
            or cpu > available_cpus * .8 or memory_mib * 1024 * 1024 > available_memory * .5):
        raise ValueError("Wave exceeds explicit budget or available host capacity reserve")
    return {"requested_cpu": cpu, "requested_memory_mib": memory_mib,
            "logical_cpus_available": available_cpus, "mem_available_bytes": available_memory,
            "cpu_reserve_fraction": .2, "available_memory_reserve_fraction": .5}


def _check_options(options):
    if (options["workers"] not in WAVES or not 1 <= options["cpu_milli"] <= 250
            or options["memory_mib"] not in (128, 256)
            or not options["workers"] <= options["tasks"] <= 2000
            or options["workers"] > 2 and options["tasks"] < 1000):
        raise ValueError("Use 2/8/32/128 workers, cpu1..250m, memory128/256Mi, tasks<=2000; waves>2 need1000tasks")
    if options["workers"] > 2:
        previous = json.loads(Path(options["previous_receipt"]).read_text())
        native = previous.get("stages", {})
        expected = WAVES[WAVES.index(options["workers"]) - 1]
        if (not previous.get("validated") or not previous.get("cleanup", {}).get("complete")
                or previous.get("sandbox_workers") != expected
                or previous.get("openshell", {}).get("image_id") != options["image_id"]
                or previous.get("adapter_sha256") != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                or previous.get("source_sha256") != execution_scale._source_identity()
                or previous.get("durability") != options["durability"]
                or previous.get("events_storage") != options["events_storage"]
                or previous.get("minimum_lease_margin_seconds", 0) < 60
                or any(not native.get(stage, {}).get("samples")
                       or native[stage]["p95_ms"] > 15 for stage in NATIVE_STAGES)):
            raise ValueError("Previous wave lacks matching source, cleanup, latency or lease gates")


class OwnedSandboxes:
    """One explicit local qualification run, backed by an fsynced ownership journal."""

    def __init__(self, journal_path, options=None):
        self.path = journal_path
        if options is None:
            self.journal = json.loads(journal_path.read_text())
        else:
            self.journal = {"run_id": uuid.uuid4().hex, "options": options, "sandboxes": [],
                            "control_seconds": {}, "cleanup": {"complete": False}}
            _write_json(self.path, self.journal)
        self.options = self.journal["options"]
        self.prefix = [self.options["openshell"], "--gateway", self.options["gateway"],
                       "--workspace", self.options["workspace"], "--color", "never", "sandbox"]

    def control(self, operation, *arguments, timeout=30, cwd=None):
        started = time.perf_counter()
        try:
            return _run([*self.prefix, *arguments], timeout=timeout, cwd=cwd)
        finally:
            self.journal["control_seconds"].setdefault(operation, []).append(time.perf_counter() - started)
            _write_json(self.path, self.journal)

    def catalog(self):
        entries, token = {}, ""
        for _ in range(32):
            page = json.loads(self.control("list", "list", "-o", "json", "--page-size", "100",
                                           "--page-token", token))
            entries.update({item["name"]: item for item in page["sandboxes"]})
            token = page["next_page_token"]
            if not token:
                return entries
        raise ValueError("Sandbox catalog exceeded bounded discovery")

    def verify_identity(self, record):
        observed = json.loads(self.control("get", "get", record["name"], "-o", "json"))
        if (observed["name"] != record["name"]
                or observed["workspace"] != self.options["workspace"]
                or observed.get("labels", {}).get(RUN_LABEL) != self.journal["run_id"]
                or record.get("id") is not None and observed["id"] != record["id"]):
            raise ValueError("Sandbox identity differs from owned creation")
        return observed

    def prepare(self, project):
        options = self.options
        image_id = _run(["docker", "image", "inspect", "--format", "{{.Id}}", options["image"]]).decode().strip()
        if image_id != options["image_id"]:
            raise ValueError("Local image does not match expected immutable image ID")
        self.journal["resource_budget"] = _resource_budget(options)
        self.journal["fixture_commit"] = execution_scale._git(project, "rev-parse", "HEAD")
        fixture_bytes = (project / "verify.py").read_bytes()
        self.journal["fixture_sha256"] = hashlib.sha256(fixture_bytes).hexdigest()
        policy = self.path.parent / "worker-policy.yaml"
        policy.write_text(POLICY)
        self.journal["policy_sha256"] = hashlib.sha256(POLICY.encode()).hexdigest()
        existing = self.catalog()
        for index in range(options["workers"]):
            name = f"avs-{self.journal['run_id'][:10]}-{index:03d}"
            if name in existing:
                raise ValueError("Fresh sandbox name already exists; never adopting it")
            record = {"name": name, "id": None, "creation_attempted": True}
            self.journal["sandboxes"].append(record)
            _write_json(self.path, self.journal)
            self.control("create", "create", "--name", name, "--from", options["image"],
                         "--cpu", f"{options['cpu_milli']}m", "--memory", f"{options['memory_mib']}Mi",
                         "--policy", str(policy), "--label", f"{RUN_LABEL}={self.journal['run_id']}",
                         "--no-auto-providers", "--no-tty", "--detach", "--restart-policy", "never",
                         "--", "python3", "-c", "import time; time.sleep(3600)", timeout=90)
            observed = self.verify_identity(record)
            record.update(id=observed["id"], created_at=observed["created_at"])
            _write_json(self.path, self.journal)
            resources = _docker_resources(record["id"])
            record["containers"] = resources
            _write_json(self.path, self.journal)
            companions = [item for item in resources if item["labels"].get(ROLE_LABEL) == "supervisor"]
            if len(companions) != 1 or any(item["labels"].get(SANDBOX_LABEL) != record["id"] for item in resources):
                raise ValueError("Cannot bind one owned companion for restrictive resource caps")
            self.verify_identity(record)
            _run(["docker", "update", "--cpus", str(options["cpu_milli"] / 1000),
                  "--memory", "128m", "--memory-swap", "128m", "--pids-limit", "128", companions[0]["id"]])
            resources = _verify_resources(_docker_resources(record["id"]), record["id"], image_id,
                                          options["cpu_milli"], options["memory_mib"])
            record["containers"] = resources
            providers = json.loads(self.control("providers", "provider", "list", name, "-o", "json"))
            if providers["providers"] or providers["next_page_token"]:
                raise ValueError("Qualification workers must have no providers")
            self.control("upload", "upload", "--no-git-ignore", name, str(project / "verify.py"), "/sandbox/project/verify.py",
                         cwd=project)
            readiness = json.loads(self.control("fixture_hash", "exec", name, "--no-login-shell", "--no-tty",
                                  "--timeout", "15", "--", "python3", "-c",
                                  "import hashlib,json,shutil; print(json.dumps({'sha256':hashlib.sha256(open('/sandbox/project/verify.py','rb').read()).hexdigest(),'flock':shutil.which('flock')}))"))
            if (readiness["sha256"] != self.journal["fixture_sha256"]
                    or readiness["flock"] not in ("/usr/bin/flock", "/bin/flock")):
                raise ValueError("Uploaded fixture differs or native flock is unavailable")
            record["fixture_sha256"] = readiness["sha256"]
            record["flock"] = readiness["flock"]
            _write_json(self.path, self.journal)

        def command_argv(task_id):
            # Stable assignment is declared in the task before any claim exists.
            suffix = task_id.split(":T", 1)[1]
            index = 0 if suffix == "001" else int(suffix.split(".", 1)[1])
            sandbox = self.journal["sandboxes"][index % options["workers"]]
            return [*self.prefix, "exec", sandbox["name"], "--no-login-shell", "--no-tty", "--timeout",
                    str(max(1, options["command_timeout"] - 2)), "--workdir", "/sandbox/project",
                    "--", sandbox["flock"], "/sandbox/execution.lock", "python3", "verify.py", task_id]

        return command_argv

    def cleanup(self):
        failures = []
        for record in self.journal["sandboxes"]:
            try:
                current = self.catalog().get(record["name"])
                if current is None:
                    if _docker_resources(record["name"], label="openshell.ai/sandbox-name"):
                        raise ValueError("Owned Docker resources remain without their OpenShell record")
                    if record["creation_attempted"] and record.get("id") is None:
                        raise ValueError("Creation outcome is unresolved; absence cannot prove cancellation")
                    record["deleted"] = True
                    continue
                observed = self.verify_identity(record)
                record["id"] = observed["id"]
                resources = _docker_resources(record["id"])
                expected_ids = {item["id"] for item in record.get("containers", [])}
                expected_images = {item["id"]: item["image"] for item in record.get("containers", [])}
                if expected_ids and {item["id"] for item in resources} - expected_ids:
                    raise ValueError("Container identity changed; refusing cleanup")
                if any(item["labels"].get(SANDBOX_LABEL) != record["id"]
                       or item["id"] in expected_images and item["image"] != expected_images[item["id"]]
                       or item["labels"].get(ROLE_LABEL) == "sandbox"
                       and item["image"] != self.options["image_id"] for item in resources):
                    raise ValueError("Container ownership/image differs; refusing cleanup")
                _write_json(self.path, self.journal)
                self.control("delete", "delete", record["name"], timeout=60)
                deadline = time.monotonic() + 15
                while _docker_resources(record["id"]):
                    if time.monotonic() >= deadline:
                        raise ValueError("Docker resources remain after managed deletion")
                    time.sleep(.2)
                if record["name"] in self.catalog():
                    raise ValueError("Sandbox remains after managed deletion")
                record["deleted"] = True
            except Exception as error:
                failures.append({"name": record["name"], "error": repr(error)})
        self.journal["cleanup"] = {"complete": not failures, "failures": failures}
        _write_json(self.path, self.journal)
        return self.journal["cleanup"]


def run(work, options):
    _check_options(options)
    adapter_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    work.mkdir(parents=True, exist_ok=False)
    owner = OwnedSandboxes(work / "owned-sandboxes.json", options)
    result = {"validated": False}
    try:
        result = execution_scale.run(work / "fixture", tasks=options["tasks"], workers=options["workers"],
                                     command_timeout=options["command_timeout"], durability=options["durability"],
                                     events_storage=options["events_storage"], prepare_executor=owner.prepare)
    except Exception as error:
        result["error"] = repr(error)
    finally:
        cleanup = owner.cleanup()
    result.update(schema="anvil.openshell-execution-scale.v1", sandbox_workers=options["workers"],
                  containers=sum(len(record.get("containers", [])) for record in owner.journal["sandboxes"]),
                  cleanup=cleanup, ownership_journal=str(owner.path),
                  adapter_sha256=adapter_sha256,
                  openshell={"image_id": options["image_id"], "gateway": options["gateway"],
                             "workspace": options["workspace"], "version": _run([options["openshell"], "--version"]).decode().strip()},
                  resource_budget=owner.journal.get("resource_budget"),
                  control_stages={key: execution_scale._summary(samples) for key, samples in owner.journal["control_seconds"].items()})
    result["adapter_unchanged"] = adapter_sha256 == hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["validated"] = result["validated"] and cleanup["complete"] and result["adapter_unchanged"]
    result["limits"] = [
        "Local Docker OpenShell only; each sandbox includes workload and supervisor containers.",
        "One coordinator; bounded outer CLI callers and native flock serialize fixture execution within each sandbox.",
        "Command duration includes host CLI, gateway/network, remote startup/lock wait and fixture execution; components are not individually instrumented.",
        "Admission and proof queues, proof validation and durable native append remain separate stages.",
        "Companion caps are restrictive Docker updates on exact newly owned IDs; gateway configuration is unchanged.",
        "Claim-owner self-attested command proofs; no independent issuer or actual task acceptance.",
    ]
    return result


def _child(output, options):
    os.setsid()
    try:
        result = run(output.with_suffix(".work"), options)
    except BaseException as error:
        result = {"validated": False, "error": repr(error)}
    _write_json(output, result)


def _interrupt(_signal_number, _frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--gateway", default="openshell")
    parser.add_argument("--workspace", default="default")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--tasks", type=int, default=1000)
    parser.add_argument("--cpu-milli", type=int, default=250)
    parser.add_argument("--memory-mib", type=int, default=128)
    parser.add_argument("--max-total-cpu", type=float, default=4)
    parser.add_argument("--max-total-memory-mib", type=int, default=4096)
    parser.add_argument("--previous-receipt")
    parser.add_argument("--command-timeout", type=int, default=30)
    parser.add_argument("--deadline", type=int, default=900)
    parser.add_argument("--durability", choices=("relaxed", "strict"), default="relaxed")
    parser.add_argument("--events-storage", choices=("local", "git"), default="local")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux" or not 10 <= args.deadline <= 3600:
        parser.error("Linux and deadline10..3600 required")
    output = args.output.resolve()
    if output.exists() or output.with_suffix(".work").exists():
        parser.error("output/work paths must be fresh")
    output.parent.mkdir(parents=True, exist_ok=True)
    options = vars(args).copy()
    options.pop("output")
    options.pop("deadline")
    options["openshell"] = shutil.which("openshell") or "openshell"
    _check_options(options)
    _resource_budget(options)
    previous_term_handler = signal.signal(signal.SIGTERM, _interrupt)
    try:
        receipt = execution_scale._supervise(output, options, args.deadline, child_target=_child)
    finally:
        signal.signal(signal.SIGTERM, previous_term_handler)
    journal = output.with_suffix(".work") / "owned-sandboxes.json"
    if journal.exists() and receipt.get("child_stopped"):
        receipt["cleanup"] = OwnedSandboxes(journal).cleanup()
        receipt["validated"] = receipt["validated"] and receipt["cleanup"]["complete"]
    _write_json(output, receipt)
    print(json.dumps({"output": str(output), "validated": receipt["validated"],
                      "cleanup": receipt.get("cleanup"), "error": receipt.get("error")}))
    return 0 if receipt["validated"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
