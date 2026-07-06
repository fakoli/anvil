"""Tests for anvil.config — Config loading, validation, and template generation.

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

from anvil.config import (
    Config,
    config_template,
    global_config_path,
    load_config,
    load_merged_config,
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
        assert cfg.default_lease_minutes == 240
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
        assert cfg.llm_fallback is False

    def test_llm_fallback_parses_bool(self, tmp_path: Path) -> None:
        cfg = load_config(
            _write_config(
                tmp_path / "config.yaml", _minimal_yaml() + "llm_fallback: true\n"
            )
        )
        assert cfg.llm_fallback is True

    def test_llm_fallback_non_bool_raises(self, tmp_path: Path) -> None:
        """A quoted/typo'd non-bool llm_fallback fails loudly at load (strict
        validation mirroring auto_expand), not a silent truthy coercion."""
        bad = _write_config(
            tmp_path / "config.yaml", _minimal_yaml() + 'llm_fallback: "false"\n'
        )
        with pytest.raises(ValueError, match="llm_fallback must be a boolean"):
            load_config(bad)

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
crossPrdGuard: refuse
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
        assert cfg.cross_prd_guard == "refuse"
        assert cfg.sync_github_enabled is True
        assert cfg.sync_github_conflict_strategy == "local_wins"

    def test_cross_prd_guard_defaults_to_warn(self, tmp_path: Path) -> None:
        """T007 — absent key keeps warn-by-default behaviour."""
        config_path = _write_config(tmp_path / "config.yaml", _minimal_yaml())
        cfg = load_config(config_path)
        assert cfg.cross_prd_guard == "warn"

    def test_cross_prd_guard_refuse_loads(self, tmp_path: Path) -> None:
        """T007 — crossPrdGuard: refuse is parsed for claim hard-stop mode."""
        yaml_content = _minimal_yaml() + "crossPrdGuard: refuse\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        assert cfg.cross_prd_guard == "refuse"

    def test_cross_prd_guard_snake_case_alias_loads(
        self, tmp_path: Path
    ) -> None:
        """Programmatic configs may use the dataclass-shaped snake-case key."""
        yaml_content = _minimal_yaml() + "cross_prd_guard: refuse\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        assert cfg.cross_prd_guard == "refuse"

    def test_cross_prd_guard_invalid_value_raises(self, tmp_path: Path) -> None:
        """T007 — typo'd guard modes fail at config-load time."""
        yaml_content = _minimal_yaml() + "crossPrdGuard: block\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="crossPrdGuard"):
            load_config(config_path)

    def test_cross_prd_guard_in_default_template(self, tmp_path: Path) -> None:
        """The scaffolded default config declares crossPrdGuard: warn."""
        config_path = tmp_path / "config.yaml"
        write_default_config(config_path, project_name="Tmpl Project")
        cfg = load_config(config_path)
        assert cfg.cross_prd_guard == "warn"

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

    def test_fast_lane_thresholds_default(self, tmp_path: Path) -> None:
        """T020 — absent keys → the renderer's built-in 2/2 ceilings."""
        config_path = _write_config(tmp_path / "config.yaml", _minimal_yaml())
        cfg = load_config(config_path)
        assert cfg.fast_lane_complexity_max == 2
        assert cfg.fast_lane_blast_radius_max == 2

    def test_fast_lane_thresholds_override(self, tmp_path: Path) -> None:
        """T020 — explicit ceilings are parsed and surfaced on Config."""
        yaml_content = (
            _minimal_yaml()
            + "fast_lane_complexity_max: 3\n"
            + "fast_lane_blast_radius_max: 1\n"
        )
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        assert cfg.fast_lane_complexity_max == 3
        assert cfg.fast_lane_blast_radius_max == 1

    def test_fast_lane_thresholds_in_default_template(self, tmp_path: Path) -> None:
        """The scaffolded default config declares the 2/2 fast-lane ceilings."""
        config_path = tmp_path / "config.yaml"
        write_default_config(config_path, project_name="Tmpl Project")
        cfg = load_config(config_path)
        assert cfg.fast_lane_complexity_max == 2
        assert cfg.fast_lane_blast_radius_max == 2

    def test_fast_lane_complexity_max_out_of_range_raises(
        self, tmp_path: Path
    ) -> None:
        """T020 — an out-of-range fast_lane_complexity_max raises at load time."""
        yaml_content = _minimal_yaml() + "fast_lane_complexity_max: 9\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="fast_lane_complexity_max"):
            load_config(config_path)

    def test_fast_lane_blast_radius_max_boolean_rejected(
        self, tmp_path: Path
    ) -> None:
        """T020 — a boolean fast_lane_blast_radius_max is rejected, not coerced."""
        yaml_content = _minimal_yaml() + "fast_lane_blast_radius_max: true\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="fast_lane_blast_radius_max"):
            load_config(config_path)


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
    `agent/` branches from anvil claim."""

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

    def test_config_template_includes_cross_prd_guard(self) -> None:
        """T007 — template exposes the warn-by-default cross-PRD claim guard."""
        parsed = yaml.safe_load(config_template(project_name="Template Project"))
        assert parsed["crossPrdGuard"] == "warn"


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


# ---------------------------------------------------------------------------
# Global-config layer (T016/B17 — ~/.config/anvil with project override)
# ---------------------------------------------------------------------------


class TestGlobalConfigPath:
    """`global_config_path()` resolution precedence:

        ANVIL_GLOBAL_CONFIG (explicit file)
            > $XDG_CONFIG_HOME/anvil/config.yaml
            > ~/.config/anvil/config.yaml
    """

    def test_global_config_default_under_dot_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With neither env var set, falls back to ~/.config/anvil."""
        monkeypatch.delenv("ANVIL_GLOBAL_CONFIG", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        p = global_config_path()
        assert p == (Path.home() / ".config" / "anvil" / "config.yaml").resolve()

    def test_global_config_honours_home_when_path_home_is_userprofile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On Windows-like resolution, HOME isolates global config with state."""
        userprofile = tmp_path / "userprofile"
        home = tmp_path / "home"
        userprofile.mkdir()
        home.mkdir()
        monkeypatch.delenv("ANVIL_GLOBAL_CONFIG", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: userprofile)
        monkeypatch.setenv("USERPROFILE", str(userprofile))
        monkeypatch.setenv("HOME", str(home))

        p = global_config_path()
        assert p == (home / ".config" / "anvil" / "config.yaml").resolve()

    def test_global_config_path_home_monkeypatch_wins_without_userprofile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POSIX tests that monkeypatch Path.home must not write real HOME."""
        patched_home = tmp_path / "patched-home"
        env_home = tmp_path / "env-home"
        patched_home.mkdir()
        env_home.mkdir()
        monkeypatch.delenv("ANVIL_GLOBAL_CONFIG", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.delenv("USERPROFILE", raising=False)
        monkeypatch.setattr(Path, "home", lambda: patched_home)
        monkeypatch.setenv("HOME", str(env_home))

        p = global_config_path()
        assert p == (patched_home / ".config" / "anvil" / "config.yaml").resolve()

    def test_global_config_honours_xdg_config_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """$XDG_CONFIG_HOME relocates the default global-config location."""
        monkeypatch.delenv("ANVIL_GLOBAL_CONFIG", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        p = global_config_path()
        assert p == (tmp_path / "anvil" / "config.yaml").resolve()

    def test_global_config_explicit_override_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ANVIL_GLOBAL_CONFIG points at an explicit file and wins
        over XDG_CONFIG_HOME."""
        explicit = tmp_path / "custom-global.yaml"
        monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(explicit))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ignored"))
        assert global_config_path() == explicit.resolve()

    def test_global_config_tilde_override_uses_home_semantics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ANVIL_GLOBAL_CONFIG=~/... follows the same HOME isolation."""
        userprofile = tmp_path / "userprofile"
        home = tmp_path / "home"
        userprofile.mkdir()
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: userprofile)
        monkeypatch.setenv("USERPROFILE", str(userprofile))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", "~/anvil-global.yaml")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ignored"))

        assert global_config_path() == (home / "anvil-global.yaml").resolve()


class TestLoadMergedGlobalConfig:
    """`load_merged_config()` merges the global layer UNDER the project config.

    Precedence (lowest → highest):
        built-in dataclass default
          < global config (~/.config/anvil/config.yaml)
          < project config (.anvil/config.yaml)
          < explicit CLI arg  (exercised in TestGlobalConfigLeasePrecedence)
    """

    def _project(self, tmp_path: Path, body: str = "") -> Path:
        return _write_config(tmp_path / "config.yaml", _minimal_yaml() + body)

    def test_global_config_supplies_defaults_when_project_omits_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A key set only in the global config supplies the value for a
        project that omits it (global default, no project override)."""
        global_path = tmp_path / "global.yaml"
        _write_config(global_path, "default_lease_minutes: 45\n")
        monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(global_path))

        project = self._project(tmp_path)  # no lease key
        cfg = load_merged_config(project)
        assert cfg.default_lease_minutes == 45

    def test_global_config_project_overrides_global_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The project config overrides the same key in the global config."""
        global_path = tmp_path / "global.yaml"
        _write_config(global_path, "default_lease_minutes: 45\n")
        monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(global_path))

        project = self._project(tmp_path, "default_lease_minutes: 30\n")
        cfg = load_merged_config(project)
        assert cfg.default_lease_minutes == 30

    def test_global_config_project_cross_prd_alias_overrides_global_primary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Alias normalization must happen before global<project merge."""
        global_path = tmp_path / "global.yaml"
        _write_config(global_path, "crossPrdGuard: refuse\n")
        monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(global_path))

        project = self._project(tmp_path, "cross_prd_guard: warn\n")
        cfg = load_merged_config(project)
        assert cfg.cross_prd_guard == "warn"

    def test_global_config_falls_through_to_builtin_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A key set in neither layer falls through to the dataclass default."""
        global_path = tmp_path / "global.yaml"
        _write_config(global_path, "branch_prefix: feature\n")
        monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(global_path))

        cfg = load_merged_config(self._project(tmp_path))
        # branch_prefix supplied by global; lease falls through to the built-in
        # default (240 — raised from 60 after real sessions hit silent expiry).
        assert cfg.branch_prefix == "feature"
        assert cfg.default_lease_minutes == 240

    def test_global_config_missing_file_uses_project_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-existent global config is fine — project config loads alone,
        identical to the pre-T016 single-file behaviour (back-compat)."""
        monkeypatch.setenv(
            "ANVIL_GLOBAL_CONFIG", str(tmp_path / "does-not-exist.yaml")
        )
        project = self._project(tmp_path, "default_lease_minutes: 30\n")
        merged = load_merged_config(project)
        single = load_config(project)
        assert merged.default_lease_minutes == single.default_lease_minutes == 30

    def test_global_config_supplies_required_project_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Required identity fields resolve against the MERGED mapping: a
        global config MAY supply project_name for a project that omits it."""
        global_path = tmp_path / "global.yaml"
        _write_config(global_path, "project_name: 'Org Default'\n")
        monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(global_path))

        # Project supplies only project_id; project_name comes from global.
        project = _write_config(tmp_path / "config.yaml", "project_id: 'p-1'\n")
        cfg = load_merged_config(project)
        assert cfg.project_name == "Org Default"
        assert cfg.project_id == "p-1"

    def test_global_config_missing_required_in_both_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When neither layer supplies a required field, the same ValueError
        as the single-file path is raised against the merged mapping."""
        global_path = tmp_path / "global.yaml"
        _write_config(global_path, "default_lease_minutes: 45\n")
        monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(global_path))

        project = _write_config(tmp_path / "config.yaml", "project_id: 'p-1'\n")
        with pytest.raises(ValueError, match="project_name"):
            load_merged_config(project)

    def test_global_config_paths_resolve_against_project_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """db_path/events_path always resolve next to the PROJECT config, even
        when supplied by the global layer — per-project state never lands in
        ~/.config."""
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        global_path = global_dir / "config.yaml"
        _write_config(global_path, "db_path: shared.db\n")
        monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(global_path))

        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        project = _write_config(project_dir / "config.yaml", _minimal_yaml())
        cfg = load_merged_config(project)
        # Resolves under the project dir, NOT the global dir.
        assert cfg.db_path == str((project_dir / "shared.db").resolve())

    def test_global_config_missing_project_file_raises(
        self, tmp_path: Path
    ) -> None:
        """The PROJECT config is still required to exist."""
        with pytest.raises(FileNotFoundError):
            load_merged_config(tmp_path / "nope.yaml")

    def test_global_config_default_path_used_when_arg_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a global_path argument, load_merged_config consults
        global_config_path() (here pinned via ANVIL_GLOBAL_CONFIG)."""
        global_path = tmp_path / "global.yaml"
        _write_config(global_path, "branch_prefix: fix\n")
        monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(global_path))
        cfg = load_merged_config(self._project(tmp_path))
        assert cfg.branch_prefix == "fix"

    def test_global_config_explicit_arg_overrides_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit global_path argument beats the env-resolved default."""
        env_global = tmp_path / "env-global.yaml"
        _write_config(env_global, "branch_prefix: fromenv\n")
        monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(env_global))

        arg_global = tmp_path / "arg-global.yaml"
        _write_config(arg_global, "branch_prefix: fromarg\n")

        cfg = load_merged_config(self._project(tmp_path), global_path=arg_global)
        assert cfg.branch_prefix == "fromarg"

    def test_global_config_explicit_tilde_arg_uses_home_semantics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """load_merged_config(global_path='~/...') uses Anvil HOME semantics."""
        userprofile = tmp_path / "userprofile"
        home = tmp_path / "home"
        userprofile.mkdir()
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: userprofile)
        monkeypatch.setenv("USERPROFILE", str(userprofile))
        monkeypatch.setenv("HOME", str(home))
        _write_config(home / "global.yaml", "branch_prefix: fromhome\n")

        cfg = load_merged_config(
            self._project(tmp_path),
            global_path="~/global.yaml",
        )
        assert cfg.branch_prefix == "fromhome"

    def test_global_config_empty_file_is_no_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty global config file means 'no global defaults', not an error."""
        global_path = tmp_path / "global.yaml"
        global_path.write_text("", encoding="utf-8")
        monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(global_path))
        cfg = load_merged_config(self._project(tmp_path))
        assert cfg.default_lease_minutes == 240  # dataclass default


class TestGlobalConfigLeasePrecedence:
    """The headline T016/B17 acceptance scenario, end to end through the CLI
    lease helper: a global default lease of 45 is overridden to 30 by a
    project config and to 15 by a CLI flag.

    Precedence: explicit CLI arg > project config > global config > built-in.
    """

    def _setup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        global_lease: str | None,
        project_lease: str | None,
    ) -> Path:
        if global_lease is not None:
            global_path = tmp_path / "global.yaml"
            _write_config(global_path, f"default_lease_minutes: {global_lease}\n")
            monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(global_path))
        else:
            monkeypatch.delenv("ANVIL_GLOBAL_CONFIG", raising=False)
            monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-such-xdg"))

        body = (
            f"default_lease_minutes: {project_lease}\n"
            if project_lease is not None
            else ""
        )
        return _write_config(tmp_path / "config.yaml", _minimal_yaml() + body)

    def test_global_config_lease_used_when_no_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Global lease 45, no project lease, no CLI flag → 45."""
        from anvil.cli._helpers import _lease_manager_kwargs

        project = self._setup(
            tmp_path, monkeypatch, global_lease="45", project_lease=None
        )
        cfg = load_merged_config(project)
        kwargs = _lease_manager_kwargs(cfg, lease_override=None)
        assert kwargs["default_lease_minutes"] == 45

    def test_global_config_project_overrides_to_30(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Global lease 45 overridden to 30 by the project config."""
        from anvil.cli._helpers import _lease_manager_kwargs

        project = self._setup(
            tmp_path, monkeypatch, global_lease="45", project_lease="30"
        )
        cfg = load_merged_config(project)
        kwargs = _lease_manager_kwargs(cfg, lease_override=None)
        assert kwargs["default_lease_minutes"] == 30

    def test_global_config_cli_flag_overrides_to_15(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Global 45 → project 30 → CLI --lease 15 wins (full precedence)."""
        from anvil.cli._helpers import _lease_manager_kwargs

        project = self._setup(
            tmp_path, monkeypatch, global_lease="45", project_lease="30"
        )
        cfg = load_merged_config(project)
        kwargs = _lease_manager_kwargs(cfg, lease_override=15)
        assert kwargs["default_lease_minutes"] == 15

    def test_global_config_cli_flag_overrides_with_no_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--lease still wins when there is no config at all (override beats
        the built-in ClaimManager default even with config=None)."""
        from anvil.cli._helpers import _lease_manager_kwargs

        monkeypatch.delenv("ANVIL_GLOBAL_CONFIG", raising=False)
        kwargs = _lease_manager_kwargs(None, lease_override=15)
        assert kwargs["default_lease_minutes"] == 15

    def test_global_config_no_override_no_config_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No config and no flag → empty kwargs (ClaimManager keeps its own
        240-min default)."""
        from anvil.cli._helpers import _lease_manager_kwargs

        assert _lease_manager_kwargs(None, lease_override=None) == {}
