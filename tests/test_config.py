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

    def test_review_tier_thresholds_default(self, tmp_path: Path) -> None:
        """retro-opps T001 — absent keys → max_min=4, light_risk_max=2."""
        config_path = _write_config(tmp_path / "config.yaml", _minimal_yaml())
        cfg = load_config(config_path)
        assert cfg.review_tier_max_min == 4
        assert cfg.review_tier_light_risk_max == 2

    def test_review_tier_thresholds_override(self, tmp_path: Path) -> None:
        """retro-opps T001 — explicit thresholds are parsed onto Config."""
        yaml_content = (
            _minimal_yaml()
            + "review_tier_max_min: 3\n"
            + "review_tier_light_risk_max: 1\n"
        )
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        cfg = load_config(config_path)
        assert cfg.review_tier_max_min == 3
        assert cfg.review_tier_light_risk_max == 1

    def test_review_tier_max_min_out_of_range_raises(
        self, tmp_path: Path
    ) -> None:
        """retro-opps T001 — out-of-range review_tier_max_min raises at load."""
        yaml_content = _minimal_yaml() + "review_tier_max_min: 0\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="review_tier_max_min"):
            load_config(config_path)

    def test_merge_check_default_and_values(self, tmp_path: Path) -> None:
        """retro-opps T007 — defaults to advisory; literal-validated."""
        config_path = _write_config(tmp_path / "config.yaml", _minimal_yaml())
        assert load_config(config_path).merge_check == "advisory"
        config_path2 = _write_config(
            tmp_path / "config2.yaml", _minimal_yaml() + "merge_check: strict\n"
        )
        assert load_config(config_path2).merge_check == "strict"
        config_path3 = _write_config(
            tmp_path / "config3.yaml", _minimal_yaml() + "merge_check: sometimes\n"
        )
        with pytest.raises(ValueError, match="merge_check"):
            load_config(config_path3)

    def test_lease_warning_minutes_default_and_override(
        self, tmp_path: Path
    ) -> None:
        """retro-opps T008 — defaults to 10.0; float override loads."""
        config_path = _write_config(tmp_path / "config.yaml", _minimal_yaml())
        assert load_config(config_path).lease_warning_minutes == 10.0
        config_path2 = _write_config(
            tmp_path / "config2.yaml",
            _minimal_yaml() + "lease_warning_minutes: 0\n",
        )
        assert load_config(config_path2).lease_warning_minutes == 0.0

    def test_review_tier_light_risk_max_out_of_range_raises(
        self, tmp_path: Path
    ) -> None:
        """retro-opps T001 — out-of-range review_tier_light_risk_max raises."""
        yaml_content = _minimal_yaml() + "review_tier_light_risk_max: 6\n"
        config_path = _write_config(tmp_path / "config.yaml", yaml_content)
        with pytest.raises(ValueError, match="review_tier_light_risk_max"):
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


def test_execution_registration_ignores_project_config(tmp_path, monkeypatch):
    """Project settings cannot turn source-controlled data into trusted argv."""
    import yaml

    from anvil.config import load_execution_adapter

    global_path = tmp_path / "global.yaml"
    global_path.write_text("project_name: owner\nproject_id: owner\n")
    global_path.chmod(0o600)
    monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(global_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "execution_adapter": {"script": "/attacker/chosen.py"},
    }))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="global execution_adapter"):
        load_execution_adapter()


def test_execution_registration_hash_drift_refuses(tmp_path, monkeypatch):
    import dataclasses

    import yaml

    from anvil.config import load_execution_adapter, register_execution_adapter

    script = tmp_path / "scripts" / "coordinate.py"
    extension = tmp_path / "extensions" / "coordination.js"
    profile = tmp_path / "profile.json"
    package = tmp_path / "package.json"
    for path in (script, extension, profile, package):
        path.parent.mkdir(exist_ok=True)
        path.write_text("{}")
        path.chmod(0o600)
    package.write_text('{"name":"codex-pi-coordination","version":"0.2.0"}')
    registration = register_execution_adapter(script, script, profile)
    config = tmp_path / "global.yaml"
    config.write_text(yaml.safe_dump({"execution_adapter": dataclasses.asdict(registration)}))
    config.chmod(0o600)
    monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(config))
    assert load_execution_adapter().registration["contract_version"] == 2
    script.write_bytes(b"changed")
    with pytest.raises(ValueError, match="input changed"):
        load_execution_adapter()


@pytest.fixture
def execution_upgrade_owner(tmp_path, monkeypatch):
    """Disposable owner/config; no OpenShell, model, canonical State or subprocess."""
    import contextlib
    import dataclasses
    import hashlib
    import json
    import os
    from types import SimpleNamespace

    import anvil.config as config_module
    import anvil.execution as execution
    from anvil.config import register_execution_adapter, validate_execution_adapter_inputs

    if os.name != "posix":
        pytest.skip("The concrete Pi owner uses POSIX flock")
    import fcntl

    owner_home = tmp_path / "owner-home"
    owner_home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(owner_home))
    script = owner_home / "plugin/scripts/coordinate.py"
    extension = owner_home / "plugin/extensions/coordination.js"
    package = owner_home / "plugin/package.json"
    interpreter = owner_home / "python"
    profile = owner_home / "profile.json"
    for path in (script, extension, package, interpreter, profile):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic trusted bytes")
        path.chmod(0o600)
    package.write_text('{"name":"codex-pi-coordination","version":"0.3.0"}')
    profile.write_text('{"sandbox_id":"synthetic-sandbox","sandbox_name":"synthetic"}')
    registration = register_execution_adapter(script, interpreter, profile)
    config = owner_home / "config.yaml"
    config.write_text("# Original owner comment\n" + yaml.safe_dump({
        "owner_setting": {"keep": ["one", "two"]},
        "execution_adapter": dataclasses.asdict(registration),
    }))
    config.chmod(0o600)
    monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(config))
    control = SimpleNamespace(
        config=config, script=script, extension=extension, package=package,
        interpreter=interpreter, profile=profile, registration=registration,
        original=config.read_bytes(), probe_calls=[], owner_reads=0, probe_error=False,
        real_owner_reader=config_module._read_execution_adapter_owner,
        owner_error=False, state={"id": "synthetic-sandbox", "name": "synthetic",
                                  "phase": "Stopped", "main_process_instance_id": "synthetic-main",
                                  "restart_count": 1}, change_after_read=None, on_owner_read=None,
    )
    lock_root = owner_home / ".local/state/codex-pi-coordination"
    key = hashlib.sha256(b"synthetic-sandbox").hexdigest()
    control.lock = lock_root / (key + ".lock")
    control.journal = lock_root / (key + ".json")

    def validate(registration, cwd, assignment=None, *, candidate_path=None):
        assert cwd == config.parent and assignment is None
        assert json.loads(candidate_path.read_bytes()) == dataclasses.asdict(registration)
        validate_execution_adapter_inputs(registration)
        control.probe_calls.append(candidate_path)
        if control.probe_error:
            raise ValueError("Synthetic adapter lacks the saved contract")

    def read_owner(candidate, candidate_path, cwd):
        assert cwd == config.parent
        assert json.loads(candidate_path.read_bytes()) == dataclasses.asdict(candidate)
        # Even a synthetic read must run while the parent's concrete owner lock is held.
        with control.lock.open("a") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        control.owner_reads += 1
        if control.on_owner_read is not None:
            control.on_owner_read(control.owner_reads)
        if control.owner_error:
            raise OSError("synthetic unavailable owner")
        if control.change_after_read == control.owner_reads:
            control.state["main_process_instance_id"] = "changed-main"
        return dict(control.state)

    @contextlib.contextmanager
    def exclusive_owner(selected_profile, selected_lock_root):
        assert selected_profile["sandbox_id"] == "synthetic-sandbox"
        assert selected_lock_root == lock_root
        lock_root.mkdir(parents=True, exist_ok=True)
        with control.lock.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield control.journal

    module = SimpleNamespace(
        digest=lambda value: hashlib.sha256(value).hexdigest(),
        profile_settings=lambda path: json.loads(path.read_bytes()),
        exclusive_owner=exclusive_owner,
        OpenShellOwner=lambda *_: pytest.fail("Owner reads require bounded capture"),
    )

    def adapter_module(candidate, *, require_registered=True):
        assert require_registered is False
        validate_execution_adapter_inputs(candidate)
        return module

    monkeypatch.setattr(execution, "validate_adapter_contract", validate)
    monkeypatch.setattr(execution, "adapter_module", adapter_module)
    monkeypatch.setattr(config_module, "_read_execution_adapter_owner", read_owner)
    monkeypatch.setattr(execution, "journal_root", lambda *_: pytest.fail("No project State in upgrade"))
    return control


def test_saved_registration_is_explicit_and_legacy_loader_preserves_capability(execution_upgrade_owner):
    import dataclasses

    from anvil.config import ExecutionAdapter, load_execution_adapter, register_execution_adapter

    owner = execution_upgrade_owner
    assert load_execution_adapter().registration["contract_version"] == 2
    assert load_execution_adapter().registration["capabilities"]["saved_session_continue"] is False
    saved = register_execution_adapter(owner.script, owner.interpreter, owner.profile,
                                       saved_session_continue=True)
    assert saved.registration["contract_version"] == 3
    assert saved.registration["capabilities"]["saved_session_continue"] is True
    for version, enabled in ((2, True), (3, False), (3, 1), (2, 0)):
        data = dataclasses.asdict(saved)
        data["registration"]["contract_version"] = version
        data["registration"]["capabilities"]["saved_session_continue"] = enabled
        with pytest.raises(ValueError, match="capabilities"):
            ExecutionAdapter(**data)


def test_registration_upgrade_preserves_backup_profile_journal_and_other_settings(execution_upgrade_owner):
    import json

    from anvil.config import (
        confirm_execution_adapter_upgrade,
        load_execution_adapter,
        preview_execution_adapter_upgrade,
    )

    owner = execution_upgrade_owner
    owner.journal.parent.mkdir(parents=True)
    journal = json.dumps({"cleanup_complete": True, "sandbox_after": owner.state}).encode()
    owner.journal.write_bytes(journal)
    original_profile = owner.profile.read_bytes()
    preview = preview_execution_adapter_upgrade()
    assert owner.config.read_bytes() == owner.original and owner.owner_reads == 0
    assert preview["previous"]["registration"]["contract_version"] == 2
    assert preview["candidate"]["registration"]["contract_version"] == 3
    result = confirm_execution_adapter_upgrade(preview["preview_id"])
    assert result["changed"] and result["written"] and not result["runtime_qualified"]
    assert load_execution_adapter().registration["capabilities"]["saved_session_continue"] is True
    assert yaml.safe_load(owner.config.read_bytes())["owner_setting"] == {"keep": ["one", "two"]}
    assert [p.read_bytes() for p in owner.config.parent.glob("*.before-execution-*")] == [owner.original]
    assert owner.journal.read_bytes() == journal and owner.profile.read_bytes() == original_profile
    assert owner.owner_reads == 3 and len(owner.probe_calls) == 2
    with pytest.raises(ValueError, match="already confirmed"):
        confirm_execution_adapter_upgrade(preview["preview_id"])
    current = owner.config.read_bytes()
    noop = preview_execution_adapter_upgrade()
    assert not noop["changed"]
    assert not confirm_execution_adapter_upgrade(noop["preview_id"])["written"]
    assert owner.config.read_bytes() == current
    assert len(list(owner.config.parent.glob("*.before-execution-*"))) == 1


def test_pending_registration_result_refuses_before_owner_read(execution_upgrade_owner):
    from anvil.config import confirm_execution_adapter_upgrade, preview_execution_adapter_upgrade
    from anvil.execution import pending_receipt_path

    owner = execution_upgrade_owner
    confirmed = preview_execution_adapter_upgrade()
    confirm_execution_adapter_upgrade(confirmed["preview_id"])
    preview = preview_execution_adapter_upgrade()
    assert not preview["changed"]
    directory = (owner.config.parent / (owner.config.name + ".execution-upgrades")
                 / preview["preview_id"])
    pending_receipt_path(directory / "result.json").touch(mode=0o600)
    reads = owner.owner_reads
    probes = len(owner.probe_calls)
    with pytest.raises(ValueError, match="already confirmed"):
        confirm_execution_adapter_upgrade(preview["preview_id"])
    assert owner.owner_reads == reads and len(owner.probe_calls) == probes


@pytest.mark.parametrize("changed", ["config", "script", "extension", "interpreter", "profile", "candidate"])
def test_registration_upgrade_preimage_drift_refuses(execution_upgrade_owner, changed):
    from anvil.config import confirm_execution_adapter_upgrade, preview_execution_adapter_upgrade

    owner = execution_upgrade_owner
    preview = preview_execution_adapter_upgrade()
    if changed == "candidate":
        owner.probe_calls[0].write_text("{}")
    else:
        path = getattr(owner, changed)
        path.write_bytes(path.read_bytes() + (b"\n# drift\n" if changed == "config" else b" changed"))
    current = owner.config.read_bytes()
    with pytest.raises((ValueError, OSError)):
        confirm_execution_adapter_upgrade(preview["preview_id"])
    assert owner.config.read_bytes() == current and owner.owner_reads == 0
    assert not list(owner.config.parent.glob("*.before-execution-*"))


@pytest.mark.parametrize("seconds", [-1, 300, 301])
def test_registration_upgrade_expiry_and_clock_refuse(execution_upgrade_owner, monkeypatch, seconds):
    from datetime import datetime, timedelta

    import anvil.execution as execution
    from anvil.config import confirm_execution_adapter_upgrade, preview_execution_adapter_upgrade

    owner = execution_upgrade_owner
    preview = preview_execution_adapter_upgrade()
    monkeypatch.setattr(execution, "now", lambda: datetime.fromisoformat(preview["created_at"]) + timedelta(seconds=seconds))
    with pytest.raises(ValueError, match="expired|clock"):
        confirm_execution_adapter_upgrade(preview["preview_id"])
    assert owner.config.read_bytes() == owner.original and owner.owner_reads == 0


@pytest.mark.parametrize("failure", ["busy", "config_busy", "unresolved", "foreign_main", "foreign_id", "ready", "unavailable", "changed_main"])
def test_registration_upgrade_owner_refusals_preserve_config(execution_upgrade_owner, failure):
    import contextlib
    import fcntl
    import json

    from anvil.config import confirm_execution_adapter_upgrade, preview_execution_adapter_upgrade

    owner = execution_upgrade_owner
    preview = preview_execution_adapter_upgrade()
    owner.journal.parent.mkdir(parents=True, exist_ok=True)
    journal = {"cleanup_complete": True, "sandbox_after": dict(owner.state)}
    if failure == "unresolved":
        journal["cleanup_complete"] = False
    if failure == "foreign_main":
        journal["sandbox_after"]["main_process_instance_id"] = "foreign"
    if failure == "foreign_id":
        journal["sandbox_after"]["id"] = "foreign"
    if failure == "ready":
        owner.state["phase"] = "Ready"
    owner.owner_error = failure == "unavailable"
    owner.change_after_read = 2 if failure == "changed_main" else None
    owner.journal.write_text(json.dumps(journal))
    journal_bytes = owner.journal.read_bytes()
    if failure == "config_busy":
        owner.config.with_name(owner.config.name + ".execution-registration.lock").write_bytes(b"other")
    with contextlib.ExitStack() as stack:
        if failure == "busy":
            lock = stack.enter_context(owner.lock.open("a"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises((ValueError, OSError)):
            confirm_execution_adapter_upgrade(preview["preview_id"])
    assert owner.config.read_bytes() == owner.original
    assert owner.journal.read_bytes() == journal_bytes


@pytest.mark.parametrize("failure", [
    None, "command_timeout", "output_overflow", "containment_unknown",
    "malformed", "duplicate_key", "empty_main", "extra_field", "bool_restart", "oversized",
])
def test_registration_upgrade_uses_bounded_owner_capture(execution_upgrade_owner, monkeypatch, failure):
    import fcntl
    import json
    import sys
    import time

    import anvil.config as config_module
    import anvil.execution_runner as runner
    from anvil.execution import ExecutionError, json_bytes

    owner = execution_upgrade_owner
    preview = config_module.preview_execution_adapter_upgrade()
    monkeypatch.setattr(config_module, "_read_execution_adapter_owner", owner.real_owner_reader)
    calls = []

    def capture(argv, cwd, directory, name, deadline, maximum, *, separate_stdout):
        assert argv == [
            sys.executable, "-I", "-m", "anvil.execution_runner", "--verify-owner",
            "--candidate", str(owner.probe_calls[0]),
            "--output", str(directory / "owner-cli"),
        ]
        assert cwd == owner.config.parent and name == "owner"
        assert maximum == 4096 and separate_stdout is True
        assert 0 < deadline - time.monotonic() <= 40
        assert not (directory / "owner-cli").exists()
        with owner.lock.open("a") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls.append(directory)
        if failure in {"command_timeout", "output_overflow", "containment_unknown"}:
            raise ExecutionError(failure, "Synthetic owned-capture refusal")
        state = dict(owner.state)
        if failure == "empty_main":
            state["main_process_instance_id"] = ""
        if failure == "extra_field":
            state["unexpected"] = "not allowed"
        if failure == "bool_restart":
            state["restart_count"] = True
        raw = json_bytes(state)
        if failure == "malformed":
            raw = b"{"
        if failure == "duplicate_key":
            raw = b'{"id":"wrong",' + json.dumps(state).encode()[1:]
        if failure == "oversized":
            raw = b" " * 4097
        (directory / "owner.stdout").write_bytes(raw)
        return {"exit_code": 0}

    monkeypatch.setattr(runner, "capture", capture)
    if failure is None:
        assert config_module.confirm_execution_adapter_upgrade(preview["preview_id"])["written"]
        assert len(calls) == len(set(calls)) == 3
    else:
        with pytest.raises(ValueError, match="verify"):
            config_module.confirm_execution_adapter_upgrade(preview["preview_id"])
        assert owner.config.read_bytes() == owner.original
        assert not list(owner.config.parent.glob("*.before-execution-*"))
        assert len(calls) == 1


def test_registration_upgrade_never_executes_drifted_old_code(execution_upgrade_owner):
    from anvil.config import confirm_execution_adapter_upgrade, preview_execution_adapter_upgrade

    owner = execution_upgrade_owner
    owner.script.write_text("# new candidate, old pinned bytes no longer installed")
    preview = preview_execution_adapter_upgrade()
    assert preview["previous"]["registration"]["code_sha256"] != preview["candidate"]["registration"]["code_sha256"]
    assert confirm_execution_adapter_upgrade(preview["preview_id"])["written"]


def test_registration_upgrade_requires_contract_probe_and_unchanged_profile(execution_upgrade_owner):
    from anvil.config import preview_execution_adapter_upgrade

    owner = execution_upgrade_owner
    owner.probe_error = True
    with pytest.raises(ValueError, match="lacks the saved contract"):
        preview_execution_adapter_upgrade()
    assert owner.config.read_bytes() == owner.original and owner.owner_reads == 0
    owner.profile.write_text('{"different_owner":true}')
    with pytest.raises(ValueError, match="preserve.*profile"):
        preview_execution_adapter_upgrade()
    assert len(owner.probe_calls) == 1


@pytest.mark.parametrize("changed", ["config", "profile", "candidate", "preview", "journal"])
def test_registration_upgrade_mid_confirmation_drift_preserves_config(execution_upgrade_owner, changed):
    from anvil.config import confirm_execution_adapter_upgrade, preview_execution_adapter_upgrade

    owner = execution_upgrade_owner
    preview = preview_execution_adapter_upgrade()
    directory = owner.probe_calls[0].parent

    def drift(read_number):
        if read_number != 3:
            return
        if changed in {"config", "profile"}:
            path = getattr(owner, changed)
            path.write_bytes(path.read_bytes() + b"\n# concurrent change\n")
        elif changed == "journal":
            owner.journal.write_text('{"cleanup_complete":false}')
        else:
            path = directory / (changed + ".json")
            path.write_bytes(path.read_bytes() + b"\n")

    owner.on_owner_read = drift
    with pytest.raises(ValueError, match="changed"):
        confirm_execution_adapter_upgrade(preview["preview_id"])
    expected = owner.original + b"\n# concurrent change\n" if changed == "config" else owner.original
    assert owner.config.read_bytes() == expected
    assert not (directory / "result.json").exists()


@pytest.fixture
def execution_permission_root():
    """Use reachable disposable ancestry; pytest's private temp root masks exposure."""
    import os
    import tempfile

    if os.name != "posix":
        pytest.skip("POSIX owner and directory permission policy")
    with tempfile.TemporaryDirectory(prefix="anvil-registration-", dir=Path("/tmp").resolve()) as raw:
        root = Path(raw)
        root.chmod(0o755)
        yield root


@pytest.mark.parametrize(
    "ancestor_mode, parent_mode, file_mode, accepted",
    [
        (0o700, 0o775, 0o664, True),
        (0o700, 0o775, 0o775, True),
        (0o755, 0o755, 0o644, True),
        (0o755, 0o755, 0o755, True),
        (0o755, 0o777, 0o644, False),
        (0o777, 0o700, 0o644, False),
        (0o775, 0o700, 0o644, False),
        (0o1777, 0o700, 0o664, True),
        (0o1777, 0o755, 0o664, False),
        (0o1777, 0o777, 0o644, False),
        (0o750, 0o755, 0o664, False),
        (0o701, 0o755, 0o664, False),
    ],
)
def test_execution_file_ancestry_permissions(
    execution_permission_root, ancestor_mode, parent_mode, file_mode, accepted
):
    from anvil.config import execution_file_bytes

    ancestor = execution_permission_root / "ancestor"
    parent = ancestor / "parent"
    parent.mkdir(parents=True)
    ancestor.chmod(ancestor_mode)
    parent.chmod(parent_mode)
    path = parent / "input"
    path.write_bytes(b"trusted bytes")
    path.chmod(file_mode)
    if accepted:
        assert execution_file_bytes(path) == b"trusted bytes"
    else:
        with pytest.raises(ValueError):
            execution_file_bytes(path)
    assert ancestor.stat().st_mode & 0o7777 == ancestor_mode
    assert parent.stat().st_mode & 0o7777 == parent_mode
    assert path.stat().st_mode & 0o7777 == file_mode


def test_execution_file_private_writable_hardlink_refuses(execution_permission_root):
    import os

    from anvil.config import execution_file_bytes

    private = execution_permission_root / "private"
    private.mkdir(mode=0o700)
    path = private / "input"
    path.write_bytes(b"externally writable alias")
    path.chmod(0o664)
    os.link(path, execution_permission_root / "outside")
    with pytest.raises(ValueError):
        execution_file_bytes(path)


@pytest.mark.parametrize("kind", ["leaf_link", "ancestor_link", "parent_reference", "directory", "fifo"])
def test_execution_file_alias_and_type_refusal(execution_permission_root, kind):
    import os

    from anvil.config import execution_file_bytes

    private = execution_permission_root / "private"
    private.mkdir(mode=0o700)
    target = private / "input"
    target.write_bytes(b"bounded")
    target.chmod(0o600)
    candidate = execution_permission_root / "candidate"
    if kind == "leaf_link":
        candidate.symlink_to(target)
    elif kind == "ancestor_link":
        candidate.symlink_to(private, target_is_directory=True)
        candidate /= "input"
    elif kind == "parent_reference":
        candidate = private / ".." / "private" / "input"
    elif kind == "directory":
        candidate.mkdir()
    else:
        os.mkfifo(candidate)
    with pytest.raises((ValueError, OSError)):
        execution_file_bytes(candidate)


@pytest.mark.parametrize("parent_mode, accepted", [(0o700, True), (0o755, True), (0o777, False), (0o1777, False)])
def test_execution_file_missing_prefix_custody(execution_permission_root, parent_mode, accepted):
    from anvil.config import execution_file_bytes

    parent = execution_permission_root / "parent"
    parent.mkdir()
    parent.chmod(parent_mode)
    target = parent / "missing" / "config.yaml"
    if accepted:
        assert execution_file_bytes(target, missing_ok=True) is None
    else:
        with pytest.raises(ValueError):
            execution_file_bytes(target, missing_ok=True)
    assert not (parent / "missing").exists()


@pytest.mark.parametrize("foreign_component", ["parent", "leaf"])
def test_execution_file_foreign_owner_refuses(execution_permission_root, monkeypatch, foreign_component):
    import os

    from anvil.config import execution_file_bytes

    parent = execution_permission_root / "private"
    parent.mkdir(mode=0o700)
    path = parent / "input"
    path.write_bytes(b"trusted bytes")
    path.chmod(0o600)
    target = (parent if foreign_component == "parent" else path).stat()
    original = os.fstat

    def foreign_metadata(descriptor):
        metadata = original(descriptor)
        if os.path.samestat(metadata, target):
            fields = list(metadata)
            fields[4] = max(0, os.geteuid()) + 10000
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(os, "fstat", foreign_metadata)
    with pytest.raises(ValueError, match="untrusted owner"):
        execution_file_bytes(path)


def test_execution_file_bounded_bytes(execution_permission_root):
    from anvil.config import execution_file_bytes

    path = execution_permission_root / "input"
    path.write_bytes(b"12345678")
    path.chmod(0o600)
    assert execution_file_bytes(path, 8) == b"12345678"
    with pytest.raises(ValueError, match="byte limit"):
        execution_file_bytes(path, 7)


@pytest.mark.parametrize("change", ["grow", "replace", "permissions"])
def test_execution_file_changes_during_read_refuse(execution_permission_root, monkeypatch, change):
    import os

    from anvil.config import execution_file_bytes

    path = execution_permission_root / "input"
    path.write_bytes(b"1234")
    path.chmod(0o600)
    original = os.read
    changed = False

    def read_then_change(descriptor, size):
        nonlocal changed
        raw = original(descriptor, size)
        if raw and not changed:
            changed = True
            if change == "grow":
                with path.open("ab") as stream:
                    stream.write(b"extra bytes beyond limit")
            elif change == "replace":
                replacement = path.with_name("replacement")
                replacement.write_bytes(b"1234")
                replacement.chmod(0o600)
                replacement.replace(path)
            else:
                path.chmod(0o666)
        return raw

    monkeypatch.setattr(os, "read", read_then_change)
    with pytest.raises(ValueError, match="byte limit|changed while reading"):
        execution_file_bytes(path, 8)


def test_execution_registration_private_install_and_custody_drift(execution_permission_root, monkeypatch):
    import dataclasses

    from anvil.config import load_execution_adapter, register_execution_adapter

    private = execution_permission_root / "private"
    private.mkdir(mode=0o700)
    script = private / "cache" / "scripts" / "coordinate.py"
    extension = private / "cache" / "extensions" / "coordination.js"
    package = private / "cache" / "package.json"
    profile = private / "profile.json"
    interpreter = private / "python"
    for path in (script, extension, package, profile, interpreter):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}")
        path.chmod(0o664)
    for path in (script.parent, extension.parent, package.parent):
        path.chmod(0o775)
    interpreter.chmod(0o775)
    package.write_text('{"name":"codex-pi-coordination","version":"0.3.0"}')
    interpreter_selector = private / "python-selector"
    interpreter_selector.symlink_to(interpreter)
    registration = register_execution_adapter(script, interpreter_selector, profile)
    assert registration.interpreter == str(interpreter)
    config = private / "config.yaml"
    config.write_text(yaml.safe_dump({"execution_adapter": dataclasses.asdict(registration)}))
    config.chmod(0o600)
    config_selector = private / "config-selector.yaml"
    config_selector.symlink_to(config)
    monkeypatch.setenv("ANVIL_GLOBAL_CONFIG", str(config_selector))
    assert global_config_path() == config
    assert load_execution_adapter() == registration
    private.chmod(0o755)
    with pytest.raises(ValueError):
        load_execution_adapter()
