"""Tests for fakoli_state.config — Config loading, validation, and template generation.

Coverage targets:
- load_config() happy path with defaults
- load_config() missing required fields raises ValueError
- load_config() invalid literal field raises ValueError
- config_template() returns parseable YAML
- write_default_config() creates a valid config file
- write_default_config() raises FileExistsError on existing file
- Path resolution for db_path and events_path
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import yaml

from fakoli_state.config import (
    Config,
    config_template,
    load_config,
    read_events_storage,
    write_default_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(path: Path, content: str) -> Path:
    """Write a YAML string to path and return the path."""
    path.write_text(content, encoding="utf-8")
    return path


def _minimal_yaml(
    project_name: str = "Test Project",
    project_id: str = "test-id",
) -> str:
    return f"""\
project_name: {project_name!r}
project_id: {project_id!r}
"""


# ---------------------------------------------------------------------------
# load_config — happy path
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_load_default_config(self, tmp_path: Path) -> None:
        """write_default_config then load_config returns Config with expected fields."""
        config_path = tmp_path / "config.yaml"
        write_default_config(config_path, project_name="My Project")
        cfg = load_config(config_path)

        assert isinstance(cfg, Config)
        assert cfg.project_name == "My Project"
        assert isinstance(cfg.project_id, str)
        # The project_id should be a UUID4
        uuid.UUID(cfg.project_id)  # raises ValueError if not valid UUID
        assert cfg.default_lease_minutes == 60
        assert cfg.default_heartbeat_minutes == 5
        assert cfg.git_ops_mode == "auto"
        assert cfg.durability == "relaxed"
        assert cfg.sync_github_enabled is False
        assert cfg.sync_github_conflict_strategy == "prompt"

    def test_load_config_with_minimal_yaml(self, tmp_path: Path) -> None:
        """Minimal config (only required fields) loads with defaults applied."""
        config_path = _write_config(tmp_path / "config.yaml", _minimal_yaml())
        cfg = load_config(config_path)
        assert cfg.project_name == "Test Project"
        assert cfg.project_id == "test-id"
        assert cfg.llm_provider is None
        assert cfg.llm_model is None

    def test_load_config_returns_frozen_config(self, tmp_path: Path) -> None:
        """Config is frozen (dataclass frozen=True) — assignment raises FrozenInstanceError."""
        import dataclasses

        config_path = _write_config(tmp_path / "config.yaml", _minimal_yaml())
        cfg = load_config(config_path)
        assert dataclasses.is_dataclass(cfg)
        # frozen=True means __setattr__ raises FrozenInstanceError (subclass of AttributeError)
        with pytest.raises((AttributeError, TypeError)):
            # setattr bypasses mypy's assignment check and triggers the frozen guard
            cfg.project_name = "mutate me"

    def test_load_config_accepts_path_object(self, tmp_path: Path) -> None:
        """load_config accepts a pathlib.Path argument."""
        config_path = _write_config(tmp_path / "config.yaml", _minimal_yaml())
        cfg = load_config(config_path)  # Path object, not str
        assert cfg.project_name == "Test Project"

    def test_load_config_accepts_string_path(self, tmp_path: Path) -> None:
        """load_config accepts a string path argument."""
        config_path = _write_config(tmp_path / "config.yaml", _minimal_yaml())
        cfg = load_config(str(config_path))  # string, not Path
        assert cfg.project_name == "Test Project"

    def test_load_config_resolves_db_path_relative(self, tmp_path: Path) -> None:
        """db_path is resolved relative to the config file's directory."""
        yaml_content = _minimal_yaml() + "db_path: my_state.db\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        expected = str(tmp_path / "my_state.db")
        assert cfg.db_path == expected

    def test_load_config_resolves_events_path_relative(self, tmp_path: Path) -> None:
        """events_path is resolved relative to the config file's directory."""
        yaml_content = _minimal_yaml() + "events_path: my_events.jsonl\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        expected = str(tmp_path / "my_events.jsonl")
        assert cfg.events_path == expected

    def test_load_config_all_fields(self, tmp_path: Path) -> None:
        """Config with all optional fields set loads correctly."""
        yaml_content = """\
project_name: 'Full Config Project'
project_id: 'full-config-id'
llm_provider: 'anthropic'
llm_model: 'claude-sonnet-4-6'
default_lease_minutes: 120
default_heartbeat_minutes: 10
git_ops_mode: record_only
durability: strict
sync_github_enabled: true
sync_github_conflict_strategy: local_wins
"""
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        assert cfg.llm_provider == "anthropic"
        assert cfg.llm_model == "claude-sonnet-4-6"
        assert cfg.default_lease_minutes == 120
        assert cfg.default_heartbeat_minutes == 10
        assert cfg.git_ops_mode == "record_only"
        assert cfg.durability == "strict"
        assert cfg.sync_github_enabled is True
        assert cfg.sync_github_conflict_strategy == "local_wins"

    def test_strict_evidence_defaults_false(self, tmp_path: Path) -> None:
        """T025/B25 — absent key → advisory default (False), back-compat."""
        config_path = _write_config(tmp_path / "config.yaml", _minimal_yaml())
        cfg = load_config(config_path)
        assert cfg.strict_evidence is False

    def test_strict_evidence_true_loads(self, tmp_path: Path) -> None:
        """T025/B25 — strict_evidence: true is parsed as True."""
        yaml_content = _minimal_yaml() + "strict_evidence: true\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        assert cfg.strict_evidence is True

    def test_strict_evidence_in_default_template(self, tmp_path: Path) -> None:
        """The scaffolded default config declares strict_evidence: false."""
        config_path = tmp_path / "config.yaml"
        write_default_config(config_path, project_name="Tmpl Project")
        cfg = load_config(config_path)
        assert cfg.strict_evidence is False


# ---------------------------------------------------------------------------
# load_config — validation failures
# ---------------------------------------------------------------------------


class TestLoadConfigErrors:
    def test_load_config_missing_project_name(self, tmp_path: Path) -> None:
        """YAML with missing project_name raises ValueError."""
        yaml_content = "project_id: 'some-id'\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="project_name"):
            load_config(config_path)

    def test_load_config_missing_project_id(self, tmp_path: Path) -> None:
        """YAML with missing project_id raises ValueError."""
        yaml_content = "project_name: 'Some Project'\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="project_id"):
            load_config(config_path)

    def test_load_config_blank_project_name_raises(self, tmp_path: Path) -> None:
        """Blank project_name ('') raises ValueError."""
        yaml_content = "project_name: ''\nproject_id: 'some-id'\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="project_name"):
            load_config(config_path)

    def test_load_config_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        """Non-existent config file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yaml")

    def test_load_config_invalid_git_ops_mode(self, tmp_path: Path) -> None:
        """Invalid git_ops_mode value raises ValueError."""
        yaml_content = _minimal_yaml() + "git_ops_mode: invalid_mode\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="git_ops_mode"):
            load_config(config_path)

    def test_load_config_invalid_durability(self, tmp_path: Path) -> None:
        """Invalid durability value raises the same ValueError that every
        other literal-typed field raises (via _validate_literal)."""
        yaml_content = _minimal_yaml() + "durability: paranoid\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="durability"):
            load_config(config_path)

    def test_load_config_invalid_conflict_strategy(self, tmp_path: Path) -> None:
        """Invalid sync_github_conflict_strategy raises ValueError."""
        yaml_content = _minimal_yaml() + "sync_github_conflict_strategy: bad_strategy\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="sync_github_conflict_strategy"):
            load_config(config_path)

    def test_load_config_non_dict_yaml_raises(self, tmp_path: Path) -> None:
        """YAML that is not a mapping (e.g. a list) raises ValueError."""
        config_path = _write_config(tmp_path / "config.yaml", "- item1\n- item2\n")
        with pytest.raises(ValueError, match="mapping"):
            load_config(config_path)


# ---------------------------------------------------------------------------
# branch_prefix (v1.15.0)
# ---------------------------------------------------------------------------


class TestBranchPrefix:
    """v1.15.0: config-driven branch naming so host projects with
    `feature/` or `fix/` conventions don't get silently-incompatible
    `agent/` branches from fakoli-state claim."""

    def test_default_branch_prefix_is_agent(self, tmp_path: Path) -> None:
        """No branch_prefix key in YAML → defaults to 'agent' (preserves
        pre-v1.15.0 behaviour)."""
        config_path = _write_config(tmp_path / "config.yaml", _minimal_yaml())
        cfg = load_config(config_path)
        assert cfg.branch_prefix == "agent"

    def test_custom_branch_prefix_feature(self, tmp_path: Path) -> None:
        yaml_content = _minimal_yaml() + "branch_prefix: feature\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        assert cfg.branch_prefix == "feature"

    def test_nested_branch_prefix_allowed(self, tmp_path: Path) -> None:
        yaml_content = _minimal_yaml() + "branch_prefix: feature/agent\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        assert cfg.branch_prefix == "feature/agent"

    def test_empty_branch_prefix_allowed(self, tmp_path: Path) -> None:
        """Empty string is the explicit no-prefix mode."""
        yaml_content = _minimal_yaml() + 'branch_prefix: ""\n'
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        assert cfg.branch_prefix == ""

    def test_leading_slash_raises(self, tmp_path: Path) -> None:
        yaml_content = _minimal_yaml() + 'branch_prefix: "/feature"\n'
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="branch_prefix"):
            load_config(config_path)

    def test_trailing_slash_raises(self, tmp_path: Path) -> None:
        yaml_content = _minimal_yaml() + 'branch_prefix: "feature/"\n'
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="branch_prefix"):
            load_config(config_path)

    def test_whitespace_in_prefix_raises(self, tmp_path: Path) -> None:
        yaml_content = _minimal_yaml() + 'branch_prefix: "agent prefix"\n'
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="branch_prefix"):
            load_config(config_path)

    def test_non_string_branch_prefix_raises(self, tmp_path: Path) -> None:
        """Numeric YAML value (e.g. `branch_prefix: 42`) gets a clear type
        error rather than crashing later in create_branch_for_task."""
        yaml_content = _minimal_yaml() + "branch_prefix: 42\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="branch_prefix"):
            load_config(config_path)


# ---------------------------------------------------------------------------
# durability (SL1-RR-1)
# ---------------------------------------------------------------------------


class TestDurability:
    """SL1-RR-1: write-path durability knob. `relaxed` (default) skips the
    per-event fsync; `strict` opts into synchronous=FULL + fsync(log) before
    COMMIT. Consumed by the write path; the config layer only validates and
    defaults the value."""

    def test_default_durability_is_relaxed(self, tmp_path: Path) -> None:
        """No durability key in YAML → defaults to 'relaxed' (back-compat:
        configs written before this knob existed keep prior behaviour)."""
        config_path = _write_config(tmp_path / "config.yaml", _minimal_yaml())
        cfg = load_config(config_path)
        assert cfg.durability == "relaxed"

    def test_explicit_relaxed(self, tmp_path: Path) -> None:
        yaml_content = _minimal_yaml() + "durability: relaxed\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        assert cfg.durability == "relaxed"

    def test_explicit_strict(self, tmp_path: Path) -> None:
        yaml_content = _minimal_yaml() + "durability: strict\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        assert cfg.durability == "strict"

    def test_invalid_durability_raises(self, tmp_path: Path) -> None:
        """Any value outside {relaxed, strict} raises ValueError naming the
        field — same validation path as git_ops_mode / llm_tier."""
        yaml_content = _minimal_yaml() + "durability: fsync_everything\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="durability"):
            load_config(config_path)

    def test_template_documents_durability(self) -> None:
        """write_default_config / config_template output includes the
        durability key documented with both legal values and the relaxed
        default."""
        template = config_template()
        assert "durability: relaxed" in template
        assert "relaxed" in template
        assert "strict" in template
        # The rendered template round-trips through load_config to "relaxed".
        parsed = yaml.safe_load(template)
        assert parsed.get("durability") == "relaxed"


# ---------------------------------------------------------------------------
# write_default_config
# ---------------------------------------------------------------------------


class TestWriteDefaultConfig:
    def test_write_creates_valid_config(self, tmp_path: Path) -> None:
        """write_default_config creates a YAML file that load_config can read."""
        config_path = tmp_path / "config.yaml"
        write_default_config(config_path, project_name="Written Project")
        assert config_path.exists()
        cfg = load_config(config_path)
        assert cfg.project_name == "Written Project"

    def test_write_generates_unique_project_id(self, tmp_path: Path) -> None:
        """write_default_config generates a UUID4 project_id."""
        config_path = tmp_path / "config.yaml"
        write_default_config(config_path, project_name="UUID Test")
        cfg = load_config(config_path)
        # Should be a valid UUID
        parsed = uuid.UUID(cfg.project_id)
        assert parsed.version == 4

    def test_write_raises_if_file_exists(self, tmp_path: Path) -> None:
        """write_default_config raises FileExistsError if file already exists."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("existing content", encoding="utf-8")
        with pytest.raises(FileExistsError):
            write_default_config(config_path, project_name="Test")

    def test_write_creates_parent_directories(self, tmp_path: Path) -> None:
        """write_default_config creates parent directories if they don't exist."""
        config_path = tmp_path / "nested" / "dir" / "config.yaml"
        write_default_config(config_path, project_name="Deep Project")
        assert config_path.exists()


# ---------------------------------------------------------------------------
# config_template
# ---------------------------------------------------------------------------


class TestConfigTemplate:
    def test_config_template_yaml_valid(self) -> None:
        """config_template() returns parseable YAML."""
        template = config_template()
        parsed = yaml.safe_load(template)
        assert isinstance(parsed, dict)

    def test_config_template_default_project_name(self) -> None:
        """config_template() uses default project_name='my-project'."""
        template = config_template()
        parsed = yaml.safe_load(template)
        assert parsed.get("project_name") == "my-project"

    def test_config_template_custom_project_name(self) -> None:
        """config_template(project_name=...) uses the given name."""
        template = config_template(project_name="Custom Name")
        parsed = yaml.safe_load(template)
        assert parsed.get("project_name") == "Custom Name"

    def test_config_template_has_required_fields(self) -> None:
        """Template YAML includes project_name and project_id."""
        template = config_template()
        parsed = yaml.safe_load(template)
        assert "project_name" in parsed
        assert "project_id" in parsed

    def test_config_template_generates_fresh_uuid_each_call(self) -> None:
        """config_template() generates a different project_id each call."""
        t1 = yaml.safe_load(config_template())
        t2 = yaml.safe_load(config_template())
        # UUIDs should differ
        assert t1.get("project_id") != t2.get("project_id")

    def test_config_template_can_be_loaded_by_load_config(self, tmp_path: Path) -> None:
        """A template written to disk can be read by load_config without error."""
        template = config_template(project_name="Template Project")
        config_path = tmp_path / "config.yaml"
        config_path.write_text(template, encoding="utf-8")
        cfg = load_config(config_path)
        assert cfg.project_name == "Template Project"


# ---------------------------------------------------------------------------
# Phase 9 T5 — multi-provider sync.providers config schema
# ---------------------------------------------------------------------------


class TestSyncProvidersConfig:
    """T5 — optional top-level ``sync.providers`` key in config.yaml.

    Contract:
    * Key absent → ``Config.sync_providers is None`` (callers fall back to
      ``sorted(PROVIDER_REGISTRY)`` — matches v1.8.0 behaviour).
    * Key present with a list → ``Config.sync_providers`` is a tuple of
      provider ids in declaration order (NOT sorted — preserves operator
      intent for any UI that might render them).
    * Key present with an empty list → ``Config.sync_providers == ()`` —
      operator explicitly opts out of every provider; callers MUST NOT
      silently fall back to the registry.
    * Non-mapping ``sync`` block, non-list ``sync.providers``, or
      non-string list entries → ``ValueError`` at load time.
    """

    def test_sync_providers_absent_defaults_to_none(
        self, tmp_path: Path,
    ) -> None:
        """No ``sync`` block → ``sync_providers is None`` (registry fallback)."""
        config_path = _write_config(tmp_path / "config.yaml", _minimal_yaml())
        cfg = load_config(config_path)
        assert cfg.sync_providers is None, (
            "T5 contract: absent sync.providers must yield None so callers "
            "can fall back to sorted(PROVIDER_REGISTRY)."
        )

    def test_sync_providers_explicit_list_is_preserved(
        self, tmp_path: Path,
    ) -> None:
        """``sync.providers: [a, b]`` → ``sync_providers == ("a", "b")``."""
        yaml_content = _minimal_yaml() + (
            "sync:\n"
            "  providers:\n"
            "    - github_issues\n"
            "    - linear\n"
        )
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        assert cfg.sync_providers == ("github_issues", "linear"), (
            f"T5 regression: declaration order must be preserved; "
            f"got {cfg.sync_providers!r}"
        )

    def test_sync_providers_empty_list_is_distinct_from_none(
        self, tmp_path: Path,
    ) -> None:
        """``sync.providers: []`` → ``sync_providers == ()`` (NOT None).

        The distinction matters: ``None`` means "use the registry" while
        ``()`` means "opt out of every provider" (e.g. for a frozen
        project that should no longer surface sync drift).
        """
        yaml_content = _minimal_yaml() + (
            "sync:\n"
            "  providers: []\n"
        )
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        assert cfg.sync_providers == (), (
            f"T5 contract: explicit empty list must be preserved as () "
            f"(opt-out) not None (registry fallback); "
            f"got {cfg.sync_providers!r}"
        )
        assert cfg.sync_providers is not None

    def test_sync_block_without_providers_key_is_none(
        self, tmp_path: Path,
    ) -> None:
        """``sync:`` present but no ``providers`` key → ``None`` (registry fallback)."""
        yaml_content = _minimal_yaml() + (
            "sync:\n"
            "  some_future_key: ignored\n"
        )
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        assert cfg.sync_providers is None

    def test_sync_providers_non_list_raises(
        self, tmp_path: Path,
    ) -> None:
        """``sync.providers: github_issues`` (a string, not a list) raises."""
        yaml_content = _minimal_yaml() + (
            "sync:\n"
            "  providers: github_issues\n"
        )
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="sync.providers"):
            load_config(config_path)

    def test_sync_providers_blank_entry_raises(
        self, tmp_path: Path,
    ) -> None:
        """List entries must be non-empty strings."""
        yaml_content = _minimal_yaml() + (
            "sync:\n"
            "  providers:\n"
            "    - github_issues\n"
            "    - ''\n"
        )
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="sync.providers"):
            load_config(config_path)

    def test_sync_block_not_mapping_raises(
        self, tmp_path: Path,
    ) -> None:
        """``sync: somestring`` (not a mapping) raises."""
        yaml_content = _minimal_yaml() + "sync: just-a-string\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="'sync'"):
            load_config(config_path)


# ---------------------------------------------------------------------------
# Auto-expansion knobs (v1.21.0)
# ---------------------------------------------------------------------------


class TestAutoExpandConfig:
    def test_defaults_when_keys_absent(self, tmp_path: Path) -> None:
        """Minimal config: auto-expansion on, threshold 4."""
        config_path = _write_config(tmp_path / "config.yaml", _minimal_yaml())
        cfg = load_config(config_path)
        assert cfg.auto_expand is True
        assert cfg.auto_expand_threshold == 4

    def test_explicit_values_parse(self, tmp_path: Path) -> None:
        yaml_content = _minimal_yaml() + "auto_expand: false\nauto_expand_threshold: 5\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        assert cfg.auto_expand is False
        assert cfg.auto_expand_threshold == 5

    def test_quoted_threshold_coerces_like_lease_minutes(self, tmp_path: Path) -> None:
        yaml_content = _minimal_yaml() + "auto_expand_threshold: '3'\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        assert load_config(config_path).auto_expand_threshold == 3

    def test_threshold_below_range_raises(self, tmp_path: Path) -> None:
        yaml_content = _minimal_yaml() + "auto_expand_threshold: 0\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="auto_expand_threshold"):
            load_config(config_path)

    def test_threshold_above_range_raises(self, tmp_path: Path) -> None:
        yaml_content = _minimal_yaml() + "auto_expand_threshold: 9\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="auto_expand_threshold"):
            load_config(config_path)

    def test_boolean_threshold_rejected_not_coerced(self, tmp_path: Path) -> None:
        """``true`` must not silently become threshold 1 (queue everything)."""
        yaml_content = _minimal_yaml() + "auto_expand_threshold: true\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="boolean"):
            load_config(config_path)

    def test_non_boolean_auto_expand_raises(self, tmp_path: Path) -> None:
        yaml_content = _minimal_yaml() + "auto_expand: 'yes'\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="auto_expand"):
            load_config(config_path)

    def test_template_documents_the_knobs(self, tmp_path: Path) -> None:
        """config_template ships the knobs with library defaults."""
        parsed = yaml.safe_load(config_template(project_name="X"))
        assert parsed["auto_expand"] is True
        assert parsed["auto_expand_threshold"] == 4


# ---------------------------------------------------------------------------
# Events storage knob (v1.22.0 — git-backed events Phase A)
# ---------------------------------------------------------------------------


class TestEventsStorageConfig:
    def test_default_when_key_absent(self, tmp_path: Path) -> None:
        """Minimal config: pre-1.22.0 projects stay in local mode."""
        config_path = _write_config(tmp_path / "config.yaml", _minimal_yaml())
        assert load_config(config_path).events_storage == "local"

    def test_explicit_values_parse(self, tmp_path: Path) -> None:
        for mode in ("local", "git"):
            yaml_content = _minimal_yaml() + f"events_storage: {mode}\n"
            config_path = _write_config(tmp_path / f"{mode}.yaml", yaml_content)
            assert load_config(config_path).events_storage == mode

    def test_invalid_value_raises(self, tmp_path: Path) -> None:
        """A typo'd mode must fail at load time, not silently run local."""
        yaml_content = _minimal_yaml() + "events_storage: gti\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="events_storage"):
            load_config(config_path)

    def test_template_documents_the_knob(self) -> None:
        """config_template ships events_storage with the local default."""
        parsed = yaml.safe_load(config_template(project_name="X"))
        assert parsed["events_storage"] == "local"


class TestReadEventsStorage:
    """The narrow reader the backend factories use to pick the storage mode.

    Tolerance contract: missing file and unparseable YAML fall back to
    "local" (the repo-wide "broken config never blocks a command" rule —
    see TestMalformedConfigFallsBackToRegistry in test_cli_sync), but an
    explicitly set INVALID value raises: the user typed the knob, so a
    silent local fallback would mix sequence ids into a hash-chained log.
    """

    def test_missing_file_is_local(self, tmp_path: Path) -> None:
        assert read_events_storage(tmp_path / "config.yaml") == "local"

    def test_git_value_is_read(self, tmp_path: Path) -> None:
        config_path = _write_config(
            tmp_path / "config.yaml", _minimal_yaml() + "events_storage: git\n"
        )
        assert read_events_storage(config_path) == "git"

    def test_unparseable_yaml_falls_back_to_local(self, tmp_path: Path) -> None:
        config_path = _write_config(
            tmp_path / "config.yaml", "sync:\n  providers: [unclosed\n"
        )
        assert read_events_storage(config_path) == "local"

    def test_non_mapping_yaml_falls_back_to_local(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path / "config.yaml", "- just\n- a list\n")
        assert read_events_storage(config_path) == "local"

    def test_invalid_value_raises(self, tmp_path: Path) -> None:
        config_path = _write_config(
            tmp_path / "config.yaml", _minimal_yaml() + "events_storage: dropbox\n"
        )
        with pytest.raises(ValueError, match="events_storage"):
            read_events_storage(config_path)
