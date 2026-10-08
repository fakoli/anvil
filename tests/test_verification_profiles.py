"""Portable disposable profile fixtures; no runner is executed by resolution."""
import hashlib
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from anvil.state.models import (
    ProofKind,
    ProofRequirement,
    Verification,
    VerificationProfileBinding,
    VerificationProfileReference,
)
from anvil.verification_profiles import (
    MANIFEST,
    MAX_FILE_BYTES,
    ProfileError,
    materialize_verification,
    require_profile_current,
    resolve_profile,
)


MANIFEST_TEXT = '''schema_version = 1
[profiles.full]
source_files = ["tools/verify.py"]
[profiles.full.platforms.linux]
commands = ["python tools/verify.py full"]
preflight_commands = ["python --version"]
[profiles.full.platforms.darwin]
commands = ["python tools/verify.py full"]
[profiles.full.platforms.windows]
commands = ["py tools/verify.py full"]
'''


def reference(root, text=MANIFEST_TEXT, platform="linux"):
    (root / MANIFEST).write_bytes(text.encode())
    return VerificationProfileReference(
        name="full", platform=platform, source_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


@pytest.fixture
def repository(tmp_path):
    # macOS temporary directories may have a system-level /var alias.
    root = tmp_path.resolve()
    (root / "tools").mkdir()
    (root / "tools/verify.py").write_text("import sys\nassert sys.argv[1] == 'full'\n")
    return root


def test_legacy_exact_serialization(repository):
    legacy = Verification(commands=["pytest"])
    assert legacy.model_dump_json() == (
        '{"commands":["pytest"],"manual_steps":[],"required_evidence":[],"required_proofs":[]}'
    )
    assert materialize_verification(legacy, repository) is legacy
    require_profile_current(legacy, repository)


@pytest.mark.parametrize("platform", ["linux", "darwin", "windows"])
def test_deterministic_immutable_binding(repository, platform):
    ref = reference(repository, platform=platform)
    binding = resolve_profile(repository, ref)
    assert binding == resolve_profile(repository, ref)
    assert VerificationProfileBinding.model_validate_json(binding.model_dump_json()) == binding
    assert binding.source_file_digests == ((
        "tools/verify.py", hashlib.sha256((repository / "tools/verify.py").read_bytes()).hexdigest(),
    ),)
    with pytest.raises(ValidationError):
        binding.contract_sha256 = "0" * 64
    with pytest.raises(ValidationError):
        VerificationProfileBinding.model_validate({**binding.model_dump(), "contract_sha256": "0" * 64})


def test_materialize_preserves_explicit_proofs_and_is_idempotent(repository):
    ref = reference(repository)
    explicit = ProofRequirement(kind=ProofKind.command, command="pytest", label="full suite")
    legacy = Verification(
        profile=ref, commands=["pytest"], required_proofs=[explicit],
        manual_steps=["check UI"], required_evidence=["logs"],
    )
    result = materialize_verification(legacy, repository)
    assert result.commands == ["pytest", "python tools/verify.py full"]
    assert result.required_proofs[0] == explicit
    assert result.manual_steps == legacy.manual_steps
    assert result.required_evidence == legacy.required_evidence
    assert result.profile_binding.preflight_commands == ("python --version",)
    assert "python --version" not in result.commands
    assert materialize_verification(result, repository) == result
    assert legacy.profile_binding is None
    require_profile_current(result, repository)
    with pytest.raises(ProfileError, match="binding_mismatch"):
        require_profile_current(legacy, repository)


@pytest.mark.parametrize("change", ["missing", "near", "exit", "command", "reference"])
def test_binding_requires_exact_commands_proofs_and_reference(repository, change):
    result = materialize_verification(Verification(profile=reference(repository)), repository)
    values = result.model_dump(mode="json")
    if change == "missing":
        values["required_proofs"] = []
    elif change == "near":
        values["required_proofs"][0]["command"] += " "
    elif change == "exit":
        values["required_proofs"][0]["passing_exit_codes"] = [0, 1]
    elif change == "command":
        values["commands"] = []
    else:
        values["profile"]["name"] = "other"
    with pytest.raises(ValidationError):
        Verification.model_validate(values)


def test_mutated_verification_is_revalidated(repository):
    result = materialize_verification(Verification(profile=reference(repository)), repository)
    result.required_proofs.clear()
    with pytest.raises(ProfileError, match="invalid_verification"):
        require_profile_current(result, repository)


@pytest.mark.parametrize("text", [
    "", "not toml", MANIFEST_TEXT + "commands=[]\n",
    MANIFEST_TEXT.replace("schema_version = 1", "schema_version = true"),
    MANIFEST_TEXT.replace("schema_version = 1", "schema_version = 2"),
    MANIFEST_TEXT.replace("[profiles.full]", "extra = 1\n[profiles.full]"),
    MANIFEST_TEXT.replace('source_files = ["tools/verify.py"]', 'source_files = []'),
    MANIFEST_TEXT.replace('source_files = ["tools/verify.py"]', 'source_files = [1]'),
    MANIFEST_TEXT.replace('source_files = ["tools/verify.py"]', 'source_files = ["tools/verify.py", "tools/verify.py"]'),
    MANIFEST_TEXT.replace('commands = ["python tools/verify.py full"]', 'commands = []', 1),
    MANIFEST_TEXT.replace('commands = ["python tools/verify.py full"]', 'commands = [1]', 1),
    MANIFEST_TEXT.replace('commands = ["python tools/verify.py full"]', 'commands = [" "]', 1),
    MANIFEST_TEXT.replace('commands = ["python tools/verify.py full"]', 'commands = ["x\\ny"]', 1),
    MANIFEST_TEXT.replace("platforms.linux", "platforms.other"),
    MANIFEST_TEXT.replace("profiles.full", "profiles.'bad name'"),
    MANIFEST_TEXT.replace("preflight_commands", "preflight_command"),
])
def test_invalid_manifest_closed_error(repository, text):
    with pytest.raises(ProfileError) as error:
        resolve_profile(repository, reference(repository, text))
    assert error.value.code == "invalid_manifest"
    assert str(error.value) == "verification profile refused: invalid_manifest"


@pytest.mark.parametrize("path", ["../outside", "/outside", "C:/outside", "tools/../verify.py", "tools//verify.py", "./verify.py", "NUL", "tools/file."])
def test_invalid_source_paths(repository, path):
    text = MANIFEST_TEXT.replace("tools/verify.py\"]", path + '\"]')
    with pytest.raises(ProfileError, match="invalid_manifest"):
        resolve_profile(repository, reference(repository, text))


@pytest.mark.parametrize("updates", [{"platform": "freebsd"}, {"source_sha256": "A" * 64}, {"source_sha256": "0" * 63}, {"name": "../bad"}, {"name": 1}, {"extra": 1}])
def test_invalid_reference(updates):
    with pytest.raises(ValidationError):
        VerificationProfileReference.model_validate({"name": "full", "platform": "linux", "source_sha256": "0" * 64, **updates})


def test_missing_profile_platform_and_digest(repository):
    ref = reference(repository)
    with pytest.raises(ProfileError, match="profile_missing"):
        resolve_profile(repository, ref.model_copy(update={"name": "other"}))
    with pytest.raises(ProfileError, match="source_mismatch"):
        resolve_profile(repository, ref.model_copy(update={"source_sha256": "0" * 64}))
    linux_only = MANIFEST_TEXT.split("[profiles.full.platforms.darwin]")[0]
    with pytest.raises(ProfileError, match="platform_unsupported"):
        resolve_profile(repository, reference(repository, linux_only, "windows"))


@pytest.mark.parametrize("kind", ["manifest", "runner"])
def test_drift_refuses_existing_binding(repository, kind):
    original = materialize_verification(Verification(profile=reference(repository)), repository)
    path = repository / (MANIFEST if kind == "manifest" else "tools/verify.py")
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ProfileError, match="source_mismatch|binding_mismatch"):
        materialize_verification(original, repository)
    with pytest.raises(ProfileError):
        require_profile_current(original, repository)


@pytest.mark.parametrize("target", ["manifest", "file", "parent", "root"])
def test_symlinks_never_resolve(repository, target):
    ref = reference(repository)
    path = repository / {"manifest": MANIFEST, "file": "tools/verify.py", "parent": "tools", "root": ""}[target]
    moved = path.with_name(path.name + "-real")
    path.rename(moved)
    try:
        path.symlink_to(moved, target_is_directory=moved.is_dir())
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ProfileError):
        resolve_profile(repository, ref)


@pytest.mark.parametrize("kind", ["directory", "missing", "fifo"])
def test_nonregular_missing_sources(repository, kind):
    ref = reference(repository)
    source = repository / "tools/verify.py"
    source.unlink()
    if kind == "directory":
        source.mkdir()
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO unavailable")
        os.mkfifo(source)
    with pytest.raises(ProfileError):
        resolve_profile(repository, ref)


@pytest.mark.parametrize("count", [0, 16, 17])
@pytest.mark.parametrize("key", ["commands", "preflight_commands"])
def test_command_entry_limits(repository, key, count):
    import json
    old = 'commands = ["python tools/verify.py full"]' if key == "commands" else 'preflight_commands = ["python --version"]'
    text = MANIFEST_TEXT.replace(old, key + " = " + json.dumps([f"echo {i}" for i in range(count)]), 1)
    ref = reference(repository, text)
    if count > 16 or (count == 0 and key == "commands"):
        with pytest.raises(ProfileError, match="invalid_manifest"):
            resolve_profile(repository, ref)
    else:
        resolve_profile(repository, ref)


@pytest.mark.parametrize("count", [0, 32, 33])
def test_source_entry_limits(repository, count):
    import json
    paths = [f"source{i}.py" for i in range(count)]
    for path in paths:
        (repository / path).write_bytes(b"")
    text = MANIFEST_TEXT.replace('["tools/verify.py"]', json.dumps(paths), 1)
    ref = reference(repository, text)
    if count in {0, 33}:
        with pytest.raises(ProfileError, match="invalid_manifest"):
            resolve_profile(repository, ref)
    else:
        assert len(resolve_profile(repository, ref).source_file_digests) == count


@pytest.mark.parametrize("size", [0, MAX_FILE_BYTES, MAX_FILE_BYTES + 1])
def test_source_byte_limits(repository, size):
    ref = reference(repository)
    (repository / "tools/verify.py").write_bytes(b"x" * size)
    if size > MAX_FILE_BYTES:
        with pytest.raises(ProfileError, match="size_limit"):
            resolve_profile(repository, ref)
    else:
        resolve_profile(repository, ref)


@pytest.mark.parametrize("size", [MAX_FILE_BYTES, MAX_FILE_BYTES + 1])
def test_manifest_byte_limits(repository, size):
    text = MANIFEST_TEXT + "#" + "x" * (size - len(MANIFEST_TEXT) - 1)
    ref = reference(repository, text)
    if size > MAX_FILE_BYTES:
        with pytest.raises(ProfileError, match="size_limit"):
            resolve_profile(repository, ref)
    else:
        resolve_profile(repository, ref)


@pytest.mark.parametrize("over", [False, True])
def test_aggregate_source_limit(repository, over):
    import json
    paths = [f"source{i}.py" for i in range(17)]
    for index, path in enumerate(paths):
        (repository / path).write_bytes(b"x" * (MAX_FILE_BYTES if index < 16 else int(over)))
    ref = reference(repository, MANIFEST_TEXT.replace('["tools/verify.py"]', json.dumps(paths), 1))
    if over:
        with pytest.raises(ProfileError, match="size_limit"):
            resolve_profile(repository, ref)
    else:
        resolve_profile(repository, ref)


@pytest.mark.parametrize("replace_identity", [False, True])
def test_change_during_read_is_refused(repository, monkeypatch, replace_identity):
    ref = reference(repository)
    original = os.read
    changed = False
    source = repository / "tools/verify.py"

    def read(fd, size):
        nonlocal changed
        data = original(fd, size)
        if data.startswith(b"import sys") and not changed:
            changed = True
            if replace_identity:
                source.rename(source.with_suffix(".old"))
                source.write_bytes(data)
            else:
                source.write_bytes(b"x" * len(data))
        return data

    monkeypatch.setattr(os, "read", read)
    with pytest.raises(ProfileError):
        resolve_profile(repository, ref)


def test_absolute_root_required(repository, monkeypatch):
    ref = reference(repository)
    monkeypatch.chdir(repository)
    with pytest.raises(ProfileError, match="invalid_path"):
        resolve_profile(Path("."), ref)


@pytest.mark.parametrize("size", [1024, 1025])
def test_command_utf8_byte_limit(repository, size):
    import json
    command = "é" * (size // 2) + "x" * (size % 2)
    text = MANIFEST_TEXT.replace('["python tools/verify.py full"]', json.dumps([command]), 1)
    ref = reference(repository, text)
    if size > 1024:
        with pytest.raises(ProfileError, match="invalid_manifest"):
            resolve_profile(repository, ref)
    else:
        assert resolve_profile(repository, ref).commands == (command,)


def test_unreadable_file_is_safe_error(repository, monkeypatch):
    ref = reference(repository)

    def denied(*args, **kwargs):
        raise PermissionError("secret source path")

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(ProfileError) as error:
        resolve_profile(repository, ref)
    assert str(error.value) == "verification profile refused: file_unavailable"


def test_invalid_utf8_manifest(repository):
    (repository / MANIFEST).write_bytes(b"\xff")
    ref = VerificationProfileReference(name="full", platform="linux", source_sha256=hashlib.sha256(b"\xff").hexdigest())
    with pytest.raises(ProfileError, match="invalid_manifest"):
        resolve_profile(repository, ref)


def test_source_identity_changes_between_snapshot_reads(repository, monkeypatch):
    import anvil.verification_profiles as profiles
    ref = reference(repository)
    original = profiles._read_file
    calls = 0

    def read(root, path):
        nonlocal calls
        result = original(root, path)
        calls += 1
        if calls == 2:
            source = root / path
            source.rename(source.with_suffix(".old"))
            source.write_bytes(result[0])
        return result

    monkeypatch.setattr(profiles, "_read_file", read)
    with pytest.raises(ProfileError, match="file_changed"):
        resolve_profile(repository, ref)


def test_binding_rejects_nonarray_sequences(repository):
    binding = resolve_profile(repository, reference(repository))
    with pytest.raises(ValidationError):
        VerificationProfileBinding.model_validate({**binding.model_dump(), "commands": set(binding.commands)})


def test_constructed_invalid_reference_revalidated(repository):
    reference(repository)
    invalid = VerificationProfileReference.model_construct(name="full", platform="other", source_sha256="0" * 64)
    with pytest.raises(ProfileError, match="invalid_reference"):
        resolve_profile(repository, invalid)
