#!/usr/bin/env python3
"""Validate agent-facing ``anvil`` citations against an artifact manifest.

Unlike the source-tree unit test this checker never imports ``anvil``.  Its
manifest can therefore come from a console script installed from the exact
wheel being considered for publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

ALLOWLIST_PHANTOM = {"decision", "start-prd"}
ALLOWLIST_OUTPUT = {"for"}
ALLOWLIST = ALLOWLIST_PHANTOM | ALLOWLIST_OUTPUT

_NAME = re.compile(r"[a-z][a-z-]+")
_FLAG = re.compile(r"--[a-z0-9][a-z0-9-]*")
_SHELL_OP = re.compile(r"\s(?:\|\||\||&&|&|;)\s")
_VERSION_OUTPUT = re.compile(r"v?\d+(?:\.\d+)+(?:[+.-][^\s]+)?")


def _placeholder(token: str) -> bool:
    return (
        (token.startswith("<") and token.endswith(">"))
        or (token.startswith("[") and token.endswith("]"))
        or token.startswith(("$", "${", "$("))
        or _VERSION_OUTPUT.fullmatch(token) is not None
    )


class ContractError(ValueError):
    """The artifact manifest is absent, malformed, or internally inconsistent."""


def load_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize one ``anvil describe --json`` payload."""

    if payload.get("ok") is not True or payload.get("command") != "describe":
        raise ContractError("manifest must be a successful describe envelope")
    data: Any = payload.get("data")
    if not isinstance(data, Mapping):
        raise ContractError("manifest data must be an object")
    api_version = data.get("api_version")
    if not isinstance(api_version, str) or not api_version:
        raise ContractError("manifest api_version must be a non-empty string")
    version = data.get("engine_version")
    if not isinstance(version, str) or not version:
        raise ContractError("manifest engine_version must be a non-empty string")
    cli = data.get("cli")
    if not isinstance(cli, Mapping):
        raise ContractError("manifest cli must be an object")
    commands = cli.get("commands")
    if (
        not isinstance(commands, Sequence)
        or isinstance(commands, (str, bytes))
        or not commands
        or any(not isinstance(item, str) or not item for item in commands)
    ):
        raise ContractError("manifest cli.commands must be a non-empty string list")
    command_list = list(commands)
    if command_list != sorted(set(command_list)):
        raise ContractError("manifest cli.commands must be sorted and unique")
    for command in command_list:
        parts = command.split(" ")
        if not parts or any(not _NAME.fullmatch(part) for part in parts):
            raise ContractError(f"manifest command path {command!r} is malformed")
    count = cli.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        raise ContractError("manifest cli.count must be an integer")
    if count != len(command_list):
        raise ContractError("manifest cli.count does not match cli.commands")
    options = cli.get("options")
    if not isinstance(options, Mapping) or set(options) != set(command_list):
        raise ContractError("manifest cli.options must map every command exactly once")
    normalized_options: dict[str, list[str]] = {}
    for command in command_list:
        values = options[command]
        if (
            not isinstance(values, Sequence)
            or isinstance(values, (str, bytes))
            or any(
                not isinstance(item, str) or not _FLAG.fullmatch(item)
                for item in values
            )
        ):
            raise ContractError(f"manifest options for {command!r} are malformed")
        option_list = list(values)
        if option_list != sorted(set(option_list)):
            raise ContractError(
                f"manifest options for {command!r} must be sorted and unique"
            )
        normalized_options[command] = option_list

    contracts = cli.get("contracts")
    if (
        not isinstance(contracts, Sequence)
        or isinstance(contracts, (str, bytes))
        or not contracts
    ):
        raise ContractError("manifest cli.contracts must be a non-empty list")
    contract_count = cli.get("contract_count")
    if isinstance(contract_count, bool) or not isinstance(contract_count, int):
        raise ContractError("manifest cli.contract_count must be an integer")
    if contract_count != len(contracts):
        raise ContractError("manifest cli.contract_count does not match contracts")
    nodes: dict[str, dict[str, Any]] = {}
    for item in contracts:
        if not isinstance(item, Mapping):
            raise ContractError("manifest CLI contract entries must be objects")
        path = item.get("path")
        kind = item.get("kind")
        flags = item.get("flags")
        if (
            not isinstance(path, Sequence)
            or isinstance(path, (str, bytes))
            or any(not isinstance(part, str) or not _NAME.fullmatch(part) for part in path)
        ):
            raise ContractError("manifest CLI contract path is malformed")
        if kind not in {"group", "command"}:
            raise ContractError("manifest CLI contract kind is malformed")
        if (
            not isinstance(flags, Sequence)
            or isinstance(flags, (str, bytes))
            or any(not isinstance(flag, str) or not _FLAG.fullmatch(flag) for flag in flags)
        ):
            raise ContractError("manifest CLI contract flags are malformed")
        flag_list = list(flags)
        if flag_list != sorted(set(flag_list)):
            raise ContractError("manifest CLI contract flags must be sorted and unique")
        joined = " ".join(path)
        if joined in nodes:
            raise ContractError(f"duplicate manifest CLI contract path: {joined!r}")
        nodes[joined] = {"kind": kind, "flags": flag_list}
    if "" not in nodes or nodes[""]["kind"] != "group":
        raise ContractError("manifest CLI contracts must contain the root group")
    for path, _node in nodes.items():
        if not path:
            continue
        parts = path.split(" ")
        parent = " ".join(parts[:-1])
        if parent not in nodes:
            raise ContractError(
                f"manifest CLI contract path {path!r} has no parent group"
            )
        if nodes[parent]["kind"] != "group":
            raise ContractError(
                f"manifest CLI contract path {path!r} descends from a command"
            )
    contract_paths = [" ".join(item["path"]) for item in contracts]
    if contract_paths != sorted(contract_paths):
        raise ContractError("manifest CLI contracts must be sorted by path")
    leaf_nodes = sorted(
        path for path, item in nodes.items() if item["kind"] == "command"
    )
    if leaf_nodes != command_list:
        raise ContractError("manifest commands do not match command contract leaves")
    if any(nodes[name]["flags"] != normalized_options[name] for name in command_list):
        raise ContractError("manifest cli.options disagrees with command contracts")
    return {
        "api_version": api_version,
        "engine_version": version,
        "commands": command_list,
        "options": normalized_options,
        "nodes": nodes,
    }


def docs_for(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "skills").glob("*/SKILL.md")) + [
        repo_root / "AGENTS.md"
    ]


def code_segments(text: str) -> Iterable[tuple[int, str]]:
    """Yield fenced and inline code, joining shell continuation lines."""

    in_fence = False
    continued: list[str] = []
    start_line = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        if re.match(r"^\s*```", line):
            if continued:
                yield start_line, " ".join(continued)
                continued = []
            in_fence = not in_fence
            continue
        if in_fence:
            stripped = line.rstrip()
            is_continuation = stripped.endswith(("\\", "`"))
            part = stripped[:-1].rstrip() if is_continuation else stripped
            if continued:
                continued.append(part.lstrip())
            elif is_continuation:
                start_line = lineno
                continued = [part]
            else:
                yield lineno, line
            if continued and not is_continuation:
                yield start_line, " ".join(continued)
                continued = []
        else:
            for span in re.findall(r"`([^`\n]+)`", line):
                yield lineno, span
    if continued:
        yield start_line, " ".join(continued)


def invocations(segment: str) -> Iterable[list[str]]:
    """Yield tokenized ``anvil ...`` invocations from one code segment."""

    for match in re.finditer(r"\banvil[ \t]+", segment):
        invocation = _SHELL_OP.split(segment[match.start() :])[0]
        invocation = invocation.rstrip().rstrip("\\").strip()
        if not invocation:
            continue
        try:
            tokens = shlex.split(invocation, posix=True)
        except ValueError:
            tokens = [
                token
                for token in invocation.split()
                if "'" not in token and '"' not in token
            ]
        if "#" in tokens:
            tokens = tokens[: tokens.index("#")]
        tokens = [token.rstrip("`") for token in tokens]
        if tokens:
            tokens[-1] = tokens[-1].rstrip("`) ,.")
        if tokens and tokens[0] == "anvil":
            yield tokens


def check_invocation(tokens: list[str], contract: Mapping[str, Any]) -> list[str]:
    """Validate one tokenized invocation against the normalized contract."""

    rest = list(tokens[1:])
    if not rest:
        return []
    nodes = contract["nodes"]
    if rest[0].strip("[]").startswith("--"):
        valid = set(nodes[""]["flags"])
        valid.add("--help")
        return [
            f"unknown root flag {flag} for: anvil"
            for token in rest
            for candidate in [token.strip("[]")]
            if candidate.startswith("--")
            for flag in candidate.split("=", 1)[0].split("/")
            if flag.startswith("--") and flag not in valid
        ]
    command = rest.pop(0)
    if command in ALLOWLIST or _placeholder(command):
        return []
    if not _NAME.fullmatch(command):
        return [f"malformed command: anvil {command}"]

    if command not in nodes:
        return [f"unknown command: anvil {command}"]

    path = [command]
    while nodes[" ".join(path)]["kind"] == "group":
        joined = " ".join(path)
        prefix = joined + " "
        children = {
            name[len(prefix) :].split(" ", 1)[0]
            for name in nodes
            if name.startswith(prefix)
        }
        if not children:
            break
        if rest and rest[0] in children:
            path.append(rest.pop(0))
            continue
        if rest and not rest[0].startswith("--") and not _placeholder(rest[0]):
            return [
                "unknown subcommand: anvil "
                + " ".join(path)
                + " "
                + rest[0]
                + " (valid: "
                + ",".join(sorted(children))
                + ")"
            ]
        break

    joined = " ".join(path)
    valid = set(nodes[joined]["flags"])
    valid.add("--help")

    findings: list[str] = []
    for token in rest:
        candidate = token.strip("[]")
        if not candidate.startswith("--") or len(candidate) <= 2:
            continue
        base = candidate.split("=", 1)[0]
        for flag in base.split("/"):
            if flag.startswith("--") and flag not in valid:
                findings.append(f"unknown flag {flag} for: anvil {joined}")
    return findings


def validate_docs(repo_root: Path, payload: Mapping[str, Any]) -> list[str]:
    """Return source-qualified findings for all shipped skills and AGENTS.md."""

    contract = load_manifest(payload)
    findings: list[str] = []
    for document in docs_for(repo_root):
        if not document.is_file():
            raise ContractError(f"required agent document is missing: {document}")
        text = document.read_text(encoding="utf-8")
        lines = text.splitlines()
        for lineno, segment in code_segments(text):
            for tokens in invocations(segment):
                for finding in check_invocation(tokens, contract):
                    source = lines[lineno - 1].strip()
                    relative = document.relative_to(repo_root)
                    findings.append(
                        f"{relative}:{lineno}: {finding}\n    -> {source}"
                    )
    return findings


def contract_digest(payload: Mapping[str, Any]) -> str:
    """Return the stable digest frozen for one published CLI contract."""

    contract = load_manifest(payload)
    normalized = {
        "api_version": contract["api_version"],
        "commands": contract["commands"],
        "nodes": [
            {
                "path": path.split(" ") if path else [],
                "kind": item["kind"],
                "flags": item["flags"],
            }
            for path, item in contract["nodes"].items()
        ],
    }
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--expected-version")
    parser.add_argument("--expected-api-version")
    return parser.parse_args(argv)


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        raw = json.loads(
            args.manifest.read_text(encoding="utf-8"),
            object_pairs_hook=_no_duplicate_object,
        )
        if not isinstance(raw, Mapping):
            raise ContractError("manifest root must be an object")
        contract = load_manifest(raw)
        if (
            args.expected_version is not None
            and contract["engine_version"] != args.expected_version
        ):
            raise ContractError(
                "manifest engine_version "
                f"{contract['engine_version']!r} does not match expected "
                f"{args.expected_version!r}"
            )
        if (
            args.expected_api_version is not None
            and contract["api_version"] != args.expected_api_version
        ):
            raise ContractError(
                "manifest api_version "
                f"{contract['api_version']!r} does not match expected "
                f"{args.expected_api_version!r}"
            )
        findings = validate_docs(args.repo_root.resolve(), raw)
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        print(f"skill/CLI contract: ERROR: {exc}", file=sys.stderr)
        return 2
    if findings:
        print("skill/CLI contract: FAILED", file=sys.stderr)
        print("\n".join(findings), file=sys.stderr)
        return 1
    print(
        "skill/CLI contract: PASS "
        f"({len(contract['commands'])} commands, engine "
        f"{contract['engine_version']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
