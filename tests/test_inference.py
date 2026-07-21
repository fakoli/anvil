"""Tests for anvil.planning.inference — dependency and conflict-group inference.

All tests follow the pure-function contract:
- Input tasks are never mutated.
- Output tasks are new instances via model_copy.
"""

from __future__ import annotations

import datetime
import sys
import time
from collections.abc import Callable
from itertools import permutations, product
from types import SimpleNamespace

import pytest

from anvil.planning import inference as inference_module
from anvil.planning.inference import (
    BundlePlanningError,
    InferenceResult,
    PathIdentityError,
    build_bundle_plan,
    infer_all,
    infer_conflict_groups,
    infer_dependencies,
)
from anvil.state.models import Score, Task, TaskPriority, TaskStatus, Verification

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = datetime.UTC
_NOW = datetime.datetime(2026, 5, 24, 18, 0, 0, tzinfo=_UTC)
_WINDOWS_DEVICE_NAMES = [
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
    *(f"COM{index}" for index in "¹²³"),
    *(f"LPT{index}" for index in "¹²³"),
]
_WINDOWS_CONSOLE_DEVICE_NAMES = ["CONIN$", "CONOUT$"]
_PORTABLE_PATH_ENTRYPOINTS = [
    infer_dependencies,
    infer_conflict_groups,
    infer_all,
    build_bundle_plan,
]


def _make_task(
    task_id: str,
    likely_files: list[str],
    *,
    dependencies: list[str] | None = None,
    conflict_groups: list[str] | None = None,
    prd_id: str = "default",
) -> Task:
    return Task(
        id=task_id,
        feature_id="F001",
        prd_id=prd_id,
        title=f"Task {task_id}",
        description="A task for inference testing.",
        status=TaskStatus.proposed,
        priority=TaskPriority.medium,
        scores=Score(),
        acceptance_criteria=["Tests pass."],
        verification=Verification(commands=["pytest tests/ -v"]),
        likely_files=likely_files,
        dependencies=dependencies or [],
        conflict_groups=conflict_groups or [],
        created_at=_NOW,
        updated_at=_NOW,
    )


# ---------------------------------------------------------------------------
# infer_dependencies
# ---------------------------------------------------------------------------


class TestInferDependencies:
    def test_no_dependencies_when_no_overlap(self) -> None:
        """Tasks with completely disjoint likely_files get no deps inferred."""
        tasks = [
            _make_task("T001", ["src/api.py", "src/routes.py"]),
            _make_task("T002", ["tests/test_api.py", "tests/conftest.py"]),
        ]
        result = infer_dependencies(tasks)
        assert len(result) == 2
        # Neither task should have new dependencies
        t001 = next(t for t in result if t.id == "T001")
        t002 = next(t for t in result if t.id == "T002")
        assert t001.dependencies == []
        assert t002.dependencies == []

    def test_subset_creates_dependency_edge(self) -> None:
        """A.files ⊂ B.files → A depends on B."""
        # T001 has a strict subset of T002's files
        tasks = [
            _make_task("T001", ["src/api.py"]),  # subset of T002's files
            _make_task("T002", ["src/api.py", "src/utils.py", "src/models.py"]),
        ]
        result = infer_dependencies(tasks)
        t001 = next(t for t in result if t.id == "T001")
        # T001 should depend on T002 (T001 specialises T002)
        assert "T002" in t001.dependencies

    @pytest.mark.parametrize(
        "specialized_path,broader_paths",
        [
            ("src/api.py", ["src/api.py", "src/utils.py"]),
            ("./src/api.py", ["src\\api.py", ".\\src\\utils.py"]),
            ("src/./api.py", ["./src/api.py", "src/parts/../utils.py"]),
        ],
    )
    def test_equivalent_path_spellings_produce_the_same_dependency_graph(
        self,
        specialized_path: str,
        broader_paths: list[str],
    ) -> None:
        tasks = [
            _make_task("T001", [specialized_path]),
            _make_task("T002", broader_paths),
        ]

        result = infer_dependencies(tasks)

        assert {task.id: task.dependencies for task in result} == {
            "T001": ["T002"],
            "T002": [],
        }

    def test_canonicalization_preserves_callers_and_authored_dependencies(
        self,
    ) -> None:
        tasks = [
            _make_task("T001", [".\\src\\api.py"], dependencies=["T900"]),
            _make_task("T002", ["src/api.py", "./src/utils.py"]),
        ]
        before = [task.model_dump(mode="python") for task in tasks]

        result = infer_dependencies(tasks)

        assert [task.model_dump(mode="python") for task in tasks] == before
        assert result[0].likely_files == [".\\src\\api.py"]
        assert result[0].dependencies == ["T002", "T900"]

    @pytest.mark.parametrize(
        "unsafe_path",
        [
            "",
            ".",
            "../outside.py",
            "src/../../outside.py",
            "/absolute.py",
            "\\absolute.py",
            "C:/absolute.py",
            "C:drive-relative.py",
            "src/file.py:stream",
            *(f"src/control-{code:02x}-{chr(code)}.py" for code in range(0x20)),
            "src/delete-\x7f.py",
            "src/unpaired-high-\ud800.py",
            "src/unpaired-low-\udfff.py",
            *(f"src/illegal-{character}.py" for character in '<>\"|?*'),
            "src/trailing-dot.",
            "src/trailing-space ",
            "src/trailing-dot./file.py",
            "src/trailing-space /file.py",
            *_WINDOWS_DEVICE_NAMES,
            *(f"src/{name.lower()}.txt" for name in _WINDOWS_DEVICE_NAMES),
            *_WINDOWS_CONSOLE_DEVICE_NAMES,
            *(f"src/{name.lower()}" for name in _WINDOWS_CONSOLE_DEVICE_NAMES),
        ],
    )
    @pytest.mark.parametrize(
        "entrypoint",
        _PORTABLE_PATH_ENTRYPOINTS,
    )
    def test_malformed_or_escaping_paths_fail_closed(
        self,
        unsafe_path: str,
        entrypoint: Callable[[list[Task]], object],
    ) -> None:
        """Every public planner raises before returning or mutating caller state."""
        task = _make_task(
            "T001",
            [unsafe_path],
            dependencies=["T900"],
            conflict_groups=["CG-authored"],
        )
        before = task.model_dump(mode="python")

        with pytest.raises(BundlePlanningError, match="bundle planning"):
            entrypoint([task])

        assert task.model_dump(mode="python") == before

    @pytest.mark.parametrize("entrypoint", _PORTABLE_PATH_ENTRYPOINTS)
    def test_utf8_path_byte_ceiling_accepts_exact_multibyte_boundary(
        self,
        entrypoint: Callable[[list[Task]], object],
    ) -> None:
        limit = inference_module._MAX_PORTABLE_PROJECT_PATH_BYTES
        path = "é" * (limit // len("é".encode()))
        assert len(path.encode("utf-8")) == limit
        task = _make_task("T001", [path])
        before = task.model_dump(mode="python")

        entrypoint([task])

        assert task.model_dump(mode="python") == before

    @pytest.mark.parametrize("entrypoint", _PORTABLE_PATH_ENTRYPOINTS)
    def test_utf8_path_byte_ceiling_rejects_n_plus_one_before_native_cache(
        self,
        entrypoint: Callable[[list[Task]], object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        limit = inference_module._MAX_PORTABLE_PROJECT_PATH_BYTES
        path = "é" * (limit // len("é".encode())) + "a"
        assert len(path.encode("utf-8")) == limit + 1
        task = _make_task("T001", [path])
        before = task.model_dump(mode="python")
        inference_module._cached_windows_path_key.cache_clear()
        inference_module._cached_windows_paths_equal.cache_clear()
        key_before = inference_module._cached_windows_path_key.cache_info()
        comparison_before = (
            inference_module._cached_windows_paths_equal.cache_info()
        )
        monkeypatch.setattr(
            inference_module, "_uses_windows_path_identity", lambda: True
        )
        monkeypatch.setattr(
            inference_module,
            "_load_windows_path_api",
            lambda: pytest.fail("oversized path reached native identity"),
        )

        with pytest.raises(BundlePlanningError) as error:
            entrypoint([task])

        assert str(error.value) == (
            "bundle planning requires likely-file paths no longer than "
            f"{limit} UTF-8 bytes"
        )
        assert len(str(error.value)) <= 4_096
        assert str(error.value).encode("cp1252")
        assert inference_module._cached_windows_path_key.cache_info() == key_before
        assert (
            inference_module._cached_windows_paths_equal.cache_info()
            == comparison_before
        )
        assert task.model_dump(mode="python") == before

    @pytest.mark.parametrize(
        "path,expected",
        [
            (
                "a" * (inference_module._MAX_PORTABLE_PROJECT_PATH_BYTES + 1),
                "bundle planning requires likely-file paths no longer than "
                f"{inference_module._MAX_PORTABLE_PROJECT_PATH_BYTES} UTF-8 bytes",
            ),
            (
                "\n" + "a" * inference_module._MAX_PORTABLE_PROJECT_PATH_BYTES,
                "bundle planning requires likely-file paths no longer than "
                f"{inference_module._MAX_PORTABLE_PROJECT_PATH_BYTES} UTF-8 bytes",
            ),
            (
                "src/unpaired-\ud800.py",
                "bundle planning requires valid UTF-8 likely-file paths",
            ),
            (
                "/" + "é" * 2_000,
                "bundle planning requires a project-relative file path",
            ),
        ],
        ids=["huge-valid", "huge-invalid", "invalid-unicode", "bounded-invalid"],
    )
    def test_path_diagnostics_are_fixed_bounded_and_cp1252_safe(
        self,
        path: str,
        expected: str,
    ) -> None:
        with pytest.raises(BundlePlanningError) as error:
            infer_all([_make_task("T001", [path])])

        assert str(error.value) == expected
        assert len(str(error.value)) <= 4_096
        assert str(error.value).encode("cp1252")
        assert path not in str(error.value)

    @pytest.mark.parametrize(
        "malformed_path",
        [None, 17, object(), ["src/nested.py"]],
        ids=["none", "integer", "object", "list"],
    )
    @pytest.mark.parametrize("entrypoint", _PORTABLE_PATH_ENTRYPOINTS)
    def test_post_validation_non_string_paths_fail_closed_without_mutation(
        self,
        malformed_path: object,
        entrypoint: Callable[[list[Task]], object],
    ) -> None:
        tasks = [
            _make_task(
                "T001",
                ["src/valid.py"],
                conflict_groups=["CG-authored"],
            ),
            _make_task("T002", ["src/other.py"], dependencies=["T001"]),
        ]
        tasks[1].likely_files.append(malformed_path)  # type: ignore[arg-type]
        before_files = [list(task.likely_files) for task in tasks]
        before_dependencies = [list(task.dependencies) for task in tasks]
        before_conflicts = [list(task.conflict_groups) for task in tasks]

        with pytest.raises(BundlePlanningError, match="paths to be strings"):
            entrypoint(tasks)

        assert [task.likely_files for task in tasks] == before_files
        assert [task.dependencies for task in tasks] == before_dependencies
        assert [task.conflict_groups for task in tasks] == before_conflicts

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows path policy")
    def test_case_equivalent_paths_share_dependency_identity(self) -> None:
        tasks = [
            _make_task("T001", ["src/Widget.py"]),
            _make_task("T002", ["src/widget.py", "src/other.py"]),
        ]
        before = [task.model_dump(mode="python") for task in tasks]

        result = infer_dependencies(tasks)

        assert result[0].dependencies == ["T002"]
        assert result[0].likely_files == ["src/Widget.py"]
        assert [task.model_dump(mode="python") for task in tasks] == before

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows path policy")
    def test_case_equivalent_paths_share_conflict_identity(self) -> None:
        tasks = [
            _make_task("T001", ["src/Widget.py", "src/one.py"]),
            _make_task("T002", ["src/widget.py", "src/two.py"]),
        ]
        before = [task.model_dump(mode="python") for task in tasks]

        result_tasks, groups = infer_conflict_groups(tasks)

        assert [group.id for group in groups] == ["CG-T001-T002"]
        assert "src/Widget.py" in groups[0].reason
        assert result_tasks[0].likely_files == ["src/Widget.py", "src/one.py"]
        assert [task.model_dump(mode="python") for task in tasks] == before

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows path policy")
    def test_bundle_plan_coordinates_case_equivalent_paths(self) -> None:
        tasks = [
            _make_task("T001", ["src/Widget.py"]),
            _make_task("T002", ["src/widget.py"]),
        ]
        before = [task.model_dump(mode="python") for task in tasks]

        report = build_bundle_plan(tasks)

        assert report.overlap_pair_count == 1
        assert report.overlap_files == ("src/Widget.py",)
        assert report.proposed_bundles[0].task_ids == ("T001", "T002")
        assert [task.model_dump(mode="python") for task in tasks] == before

    @pytest.mark.parametrize(
        "first_spelling,second_spelling",
        [
            ("Widget", "widget"),
            ("Éclair", "éclair"),
            ("Σigma", "σigma"),
            ("\u1f80λφα", "\u1f88λφα"),
        ],
    )
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows path policy")
    def test_one_to_one_case_variants_coordinate_across_all_entrypoints(
        self,
        first_spelling: str,
        second_spelling: str,
    ) -> None:
        dependency_tasks = [
            _make_task("T001", [f"src/{first_spelling}.py"]),
            _make_task(
                "T002",
                [f"src/{second_spelling}.py", "src/other.py"],
            ),
        ]
        conflict_tasks = [
            _make_task(
                "T001",
                [f"src/{first_spelling}.py", "src/one.py"],
            ),
            _make_task(
                "T002",
                [f"src/{second_spelling}.py", "src/two.py"],
            ),
        ]
        bundle_tasks = [
            _make_task("T001", [f"src/{first_spelling}.py"]),
            _make_task("T002", [f"src/{second_spelling}.py"]),
        ]
        all_tasks = dependency_tasks + conflict_tasks + bundle_tasks
        before = [task.model_dump(mode="python") for task in all_tasks]

        dependencies = infer_dependencies(dependency_tasks)
        conflict_results, conflict_groups = infer_conflict_groups(conflict_tasks)
        combined = infer_all(conflict_tasks)
        bundle = build_bundle_plan(bundle_tasks)

        assert dependencies[0].dependencies == ["T002"]
        assert [group.id for group in conflict_groups] == ["CG-T001-T002"]
        assert [group.id for group in combined.conflict_groups] == ["CG-T001-T002"]
        assert f"src/{first_spelling}.py" in conflict_groups[0].reason
        assert conflict_results[0].likely_files[0] == f"src/{first_spelling}.py"
        assert bundle.overlap_files == (f"src/{first_spelling}.py",)
        assert [task.model_dump(mode="python") for task in all_tasks] == before

    @pytest.mark.parametrize(
        "first_spelling,second_spelling",
        [
            ("straße", "strasse"),
            ("Σ", "ς"),
            ("ẞ", "ß"),
            ("I", "ı"),
            ("İ", "i"),
            ("K", "K"),
            ("ſ", "S"),
            ("µ", "Μ"),
            ("ǅ", "Ǆ"),
            ("Ԥ", "ԥ"),
            ("𐐀", "𐐨"),
        ],
    )
    def test_expanding_or_compatibility_case_pairs_remain_distinct(
        self,
        first_spelling: str,
        second_spelling: str,
    ) -> None:
        tasks = [
            _make_task("T001", [f"src/{first_spelling}.py"]),
            _make_task("T002", [f"src/{second_spelling}.py"]),
        ]
        before = [task.model_dump(mode="python") for task in tasks]

        dependencies = infer_dependencies(tasks)
        conflict_results, conflict_groups = infer_conflict_groups(tasks)
        combined = infer_all(tasks)
        bundle = build_bundle_plan(tasks)

        assert all(not task.dependencies for task in dependencies)
        assert conflict_groups == []
        assert all(not task.conflict_groups for task in conflict_results)
        assert combined.conflict_groups == []
        assert bundle.overlap_pair_count == 0
        assert [proposal.task_ids for proposal in bundle.proposed_bundles] == [
            ("T001",),
            ("T002",),
        ]
        assert [task.model_dump(mode="python") for task in tasks] == before

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows path policy")
    @pytest.mark.parametrize(
        "left,right,expected",
        [
            ("Widget", "widget", True),
            ("straße", "strasse", False),
            ("É", "é", True),
            ("Σ", "σ", True),
            ("Σ", "ς", False),
            ("ẞ", "ß", False),
            ("I", "ı", False),
            ("İ", "i", False),
            ("K", "K", False),
            ("ſ", "S", False),
            ("µ", "Μ", False),
            ("ǅ", "Ǆ", False),
            ("Ԥ", "ԥ", False),
            ("\u1f80", "\u1f88", True),
            ("𐐀", "𐐨", False),
        ],
    )
    def test_windows_named_ordinal_matrix(
        self,
        left: str,
        right: str,
        expected: bool,
    ) -> None:
        assert inference_module._host_paths_equal(left, right) is expected
        assert inference_module._host_paths_equal(right, left) is expected

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows path policy")
    def test_windows_keys_and_registry_match_systematic_native_oracle(self) -> None:
        import ctypes

        compare = ctypes.WinDLL(
            "kernel32", use_last_error=True
        ).CompareStringOrdinal
        compare.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_int,
            ctypes.c_wchar_p,
            ctypes.c_int,
            ctypes.c_int,
        ]
        compare.restype = ctypes.c_int
        codepoints = [
            *range(0x20, 0xD800, 31),
            *range(0xE000, 0x10000, 31),
            *range(0x10000, 0x110000, 1301),
        ]
        checked_pairs = 0

        for codepoint in codepoints:
            character = chr(codepoint)
            candidates = {
                character,
                character.lower(),
                character.upper(),
                character.casefold(),
            }
            if codepoint < 0x10FFFF:
                candidates.add(chr(codepoint + 1))
            for candidate in candidates:
                native = compare(character, -1, candidate, -1, 1)
                assert native != 0
                expected = native == 2
                assert inference_module._host_paths_equal(character, candidate) is expected
                if expected:
                    assert inference_module._cached_windows_path_key(
                        character
                    ) == inference_module._cached_windows_path_key(candidate)
                registry = inference_module._PathIdentityRegistry()
                assert (registry.intern(character) == registry.intern(candidate)) is expected
                checked_pairs += 1

        assert checked_pairs >= 5_000

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows path policy")
    def test_windows_native_key_collision_is_verified_by_ordinal_comparison(
        self,
    ) -> None:
        registry = inference_module._PathIdentityRegistry()

        upper = registry.intern("src/𐐀.py")
        lower = registry.intern("src/𐐨.py")

        assert inference_module._cached_windows_path_key(
            "src/𐐀.py"
        ) == inference_module._cached_windows_path_key("src/𐐨.py")
        assert upper != lower

    def test_windows_loader_failure_is_typed_and_bounded(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ctypes

        def fail_loader(*args: object, **kwargs: object) -> object:
            raise OSError("sensitive raw loader details")

        monkeypatch.setattr(ctypes, "WinDLL", fail_loader, raising=False)
        inference_module._load_windows_path_api.cache_clear()
        with pytest.raises(BundlePlanningError) as error:
            inference_module._load_windows_path_api()
        assert str(error.value) == "Windows path API unavailable (library load failed)"
        assert "sensitive" not in str(error.value)
        inference_module._load_windows_path_api.cache_clear()

    def test_windows_missing_symbol_is_typed_and_bounded(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ctypes

        kernel = SimpleNamespace(CompareStringOrdinal=lambda *args: 2)
        monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
        inference_module._load_windows_path_api.cache_clear()
        with pytest.raises(BundlePlanningError) as error:
            inference_module._load_windows_path_api()
        assert str(error.value) == "Windows path API unavailable (required symbol missing)"
        inference_module._load_windows_path_api.cache_clear()

    def test_windows_signature_configuration_failure_is_typed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ctypes

        class RejectSignature:
            def __call__(self, *args: object) -> int:
                return 2

            def __setattr__(self, name: str, value: object) -> None:
                raise TypeError("raw signature failure")

        kernel = SimpleNamespace(
            CompareStringOrdinal=RejectSignature(),
            LCMapStringEx=RejectSignature(),
        )
        monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
        inference_module._load_windows_path_api.cache_clear()
        with pytest.raises(BundlePlanningError) as error:
            inference_module._load_windows_path_api()
        assert str(error.value) == (
            "Windows path API unavailable (signature configuration failed)"
        )
        inference_module._load_windows_path_api.cache_clear()

    def test_windows_runtime_zero_results_are_typed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ctypes

        class NativeFunction:
            argtypes: object = None
            restype: object = None

            def __init__(self, result: int) -> None:
                self.result = result

            def __call__(self, *args: object) -> int:
                return self.result

        kernel = SimpleNamespace(
            CompareStringOrdinal=NativeFunction(0),
            LCMapStringEx=NativeFunction(0),
        )
        monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
        inference_module._load_windows_path_api.cache_clear()
        api = inference_module._load_windows_path_api()
        with pytest.raises(BundlePlanningError, match="case mapping failed"):
            api.map_key("src/file.py")
        with pytest.raises(BundlePlanningError, match="comparison failed"):
            api.equivalent("a", "A")
        inference_module._load_windows_path_api.cache_clear()

    def test_non_windows_policy_uses_exact_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            inference_module,
            "_uses_windows_path_identity",
            lambda: False,
        )
        monkeypatch.setattr(
            inference_module,
            "_load_windows_path_api",
            lambda: pytest.fail("non-Windows policy loaded a native API"),
        )
        tasks = [
            _make_task("T001", ["src/Widget.py"]),
            _make_task("T002", ["src/widget.py"]),
        ]
        before = [task.model_dump(mode="python") for task in tasks]

        dependencies = infer_dependencies(tasks)
        conflict_results, conflict_groups = infer_conflict_groups(tasks)
        combined = infer_all(tasks)
        bundle = build_bundle_plan(tasks)

        assert all(not task.dependencies for task in dependencies)
        assert conflict_groups == []
        assert all(not task.conflict_groups for task in conflict_results)
        assert combined.conflict_groups == []
        assert bundle.overlap_pair_count == 0
        assert [proposal.task_ids for proposal in bundle.proposed_bundles] == [
            ("T001",),
            ("T002",),
        ]
        assert [task.model_dump(mode="python") for task in tasks] == before

    def test_infer_all_reuses_one_canonical_scope_pass(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original = inference_module._canonical_file_scopes
        calls = 0

        def counting_scopes(
            tasks: list[Task],
        ) -> tuple[dict[str, frozenset[int]], dict[int, str]]:
            nonlocal calls
            calls += 1
            return original(tasks)

        monkeypatch.setattr(inference_module, "_canonical_file_scopes", counting_scopes)
        infer_all(
            [
                _make_task("T001", ["src/a.py"]),
                _make_task("T002", ["src/a.py", "src/b.py"]),
            ]
        )

        assert calls == 1

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows path policy")
    def test_windows_registry_handles_5000_unique_paths_without_cache_thrash(
        self,
    ) -> None:
        inference_module._cached_windows_path_key.cache_clear()
        inference_module._cached_windows_paths_equal.cache_clear()
        registry = inference_module._PathIdentityRegistry()
        started = time.perf_counter()

        identities = [registry.intern(f"src/file-{index:04d}.py") for index in range(5_000)]

        assert len(set(identities)) == 5_000
        assert inference_module._cached_windows_path_key.cache_info().misses == 5_000
        assert inference_module._cached_windows_path_key.cache_info().currsize == 5_000
        assert inference_module._cached_windows_paths_equal.cache_info().misses == 0
        assert time.perf_counter() - started < 2.0

    def test_windows_collision_bucket_refuses_in_bounded_comparator_work(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        comparisons = 0

        def distinct(left: str, right: str) -> bool:
            nonlocal comparisons
            comparisons += 1
            return False

        monkeypatch.setattr(
            inference_module, "_uses_windows_path_identity", lambda: True
        )
        monkeypatch.setattr(
            inference_module, "_cached_windows_path_key", lambda path: "collision"
        )
        monkeypatch.setattr(inference_module, "_host_paths_equal", distinct)
        registry = inference_module._PathIdentityRegistry()
        started = time.perf_counter()

        with pytest.raises(PathIdentityError, match="collision limit exceeded"):
            for index in range(2_000):
                registry.intern(f"src/collision-{index}.py")

        limit = inference_module._WINDOWS_COLLISION_BUCKET_LIMIT
        assert comparisons == limit * (limit + 1) // 2
        assert time.perf_counter() - started < 0.5

    def test_full_collision_bucket_still_accepts_verified_equivalent_spelling(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            inference_module, "_uses_windows_path_identity", lambda: True
        )
        monkeypatch.setattr(
            inference_module, "_cached_windows_path_key", lambda path: "collision"
        )
        monkeypatch.setattr(
            inference_module,
            "_host_paths_equal",
            lambda left, right: left.casefold() == right.casefold(),
        )
        registry = inference_module._PathIdentityRegistry()
        identities = [
            registry.intern(f"src/collision-{index}.py")
            for index in range(inference_module._WINDOWS_COLLISION_BUCKET_LIMIT)
        ]

        assert registry.intern("SRC/COLLISION-0.PY") == identities[0]

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows path policy")
    def test_adversarial_deseret_collisions_refuse_before_quadratic_scan(
        self,
    ) -> None:
        inference_module._cached_windows_path_key.cache_clear()
        inference_module._cached_windows_paths_equal.cache_clear()
        registry = inference_module._PathIdentityRegistry()
        spellings = [
            "src/"
            + "".join("𐐀" if index & (1 << bit) else "𐐨" for bit in range(11))
            + ".py"
            for index in range(2_000)
        ]
        started = time.perf_counter()

        with pytest.raises(PathIdentityError, match="collision limit exceeded"):
            for spelling in spellings:
                registry.intern(spelling)

        assert inference_module._cached_windows_paths_equal.cache_info().misses <= 2_080
        assert time.perf_counter() - started < 1.0

    @pytest.mark.parametrize(
        "portable_path",
        [
            "./src\\module.py",
            "src/parts/../module.py",
            ".github/workflows/test.yml",
            "src/space name.py",
            "src/COM0.txt",
            "src/COM10.txt",
            "src/LPT0.txt",
            "src/LPT10.txt",
            "src/.con",
            "src/console.txt",
            "src/CONIN$.txt",
            "src/conout$.log",
            "src/CONINPUT$",
            "src/CONOUT",
        ],
    )
    @pytest.mark.parametrize("entrypoint", _PORTABLE_PATH_ENTRYPOINTS)
    def test_portable_aliases_remain_valid(
        self,
        portable_path: str,
        entrypoint: Callable[[list[Task]], object],
    ) -> None:
        entrypoint([_make_task("T001", [portable_path])])

    def test_superset_gets_no_extra_dependency(self) -> None:
        """B.files ⊃ A.files → B does NOT depend on A (A depends on B)."""
        tasks = [
            _make_task("T001", ["src/api.py"]),
            _make_task("T002", ["src/api.py", "src/utils.py"]),
        ]
        result = infer_dependencies(tasks)
        t002 = next(t for t in result if t.id == "T002")
        # T002 is the broader task, should NOT depend on T001
        assert "T001" not in t002.dependencies

    def test_explicit_inverse_edge_wins_over_inference(self) -> None:
        """An inferred inverse edge is skipped instead of cycling explicit intent."""
        tasks = [
            _make_task("T001", ["src/a.py"]),
            _make_task(
                "T002",
                ["src/a.py", "src/b.py"],
                dependencies=["T001"],
            ),
        ]

        result = infer_dependencies(tasks)
        by_id = {task.id: task for task in result}

        assert by_id["T001"].dependencies == []
        assert by_id["T002"].dependencies == ["T001"]

    def test_transitive_explicit_path_blocks_inferred_cycle(self) -> None:
        """A candidate is skipped when its prerequisite reaches it transitively."""
        tasks = [
            _make_task("T001", ["src/a.py"]),
            _make_task("T002", ["src/middle.py"], dependencies=["T001"]),
            _make_task(
                "T003",
                ["src/a.py", "src/b.py"],
                dependencies=["T002"],
            ),
        ]

        result = infer_dependencies(tasks)
        by_id = {task.id: task for task in result}

        assert by_id["T001"].dependencies == []
        assert by_id["T002"].dependencies == ["T001"]
        assert by_id["T003"].dependencies == ["T002"]

    def test_guard_considers_previously_accepted_inferred_edges(self) -> None:
        """Earlier safe inference participates in later reachability checks."""
        tasks = [
            _make_task("T001", ["src/a.py"]),
            _make_task(
                "T002",
                ["src/a.py", "src/b.py", "src/c.py"],
                dependencies=["T001"],
            ),
            _make_task("T003", ["src/a.py", "src/b.py"]),
        ]

        result = infer_dependencies(tasks)
        by_id = {task.id: task for task in result}

        assert by_id["T001"].dependencies == ["T003"]
        assert by_id["T002"].dependencies == ["T001"]
        assert by_id["T003"].dependencies == []

    def test_cycle_guard_is_deterministic_for_reordered_input(self) -> None:
        """Reordering equivalent input cannot change accepted inferred edges."""
        tasks = [
            _make_task("T001", ["src/a.py"]),
            _make_task(
                "T002",
                ["src/a.py", "src/b.py", "src/c.py"],
                dependencies=["T001"],
            ),
            _make_task("T003", ["src/a.py", "src/b.py"]),
        ]

        forward = {task.id: task.dependencies for task in infer_dependencies(tasks)}
        reverse = {
            task.id: task.dependencies for task in infer_dependencies(list(reversed(tasks)))
        }

        assert forward == reverse

    def test_dense_nested_plan_has_quadratically_bounded_closure_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dense plans close each reachable pair once, never DFS per candidate."""
        task_count = 300
        tasks = [
            _make_task(
                f"T{index:03}",
                [f"src/file-{file_index:03}.py" for file_index in range(index + 1)],
            )
            for index in range(task_count)
        ]
        trackers: list[inference_module._DependencyReachability] = []
        reachability_type = inference_module._DependencyReachability

        class _TrackingReachability(reachability_type):
            def __init__(self, dependencies: dict[str, set[str]]) -> None:
                super().__init__(dependencies)
                trackers.append(self)

        monkeypatch.setattr(
            inference_module, "_DependencyReachability", _TrackingReachability
        )

        started = time.perf_counter()
        result = infer_dependencies(tasks)
        elapsed = time.perf_counter() - started

        candidate_count = task_count * (task_count - 1) // 2
        tracker = trackers[0]
        assert sum(len(task.dependencies) for task in result) == candidate_count
        assert tracker.cycle_checks == candidate_count
        assert tracker.closure_row_updates <= candidate_count
        assert tracker.closure_pair_updates <= candidate_count
        print(  # noqa: T201 - diagnostic only; elapsed time is not an assertion
            "dense inference diagnostics: "
            f"tasks={task_count} candidates={candidate_count} "
            f"row_updates={tracker.closure_row_updates} "
            f"pair_updates={tracker.closure_pair_updates} elapsed={elapsed:.3f}s"
        )

    def test_empty_files_not_a_subset(self) -> None:
        """Task with empty likely_files does not create dependency edges."""
        tasks = [
            _make_task("T001", []),  # empty
            _make_task("T002", ["src/api.py", "src/utils.py"]),
        ]
        result = infer_dependencies(tasks)
        t001 = next(t for t in result if t.id == "T001")
        # empty set is mathematically a subset of everything, but parser skips it
        assert t001.dependencies == []

    def test_input_tasks_not_mutated(self) -> None:
        """infer_dependencies does not mutate input tasks."""
        original_task = _make_task("T001", ["src/api.py"])
        tasks = [
            original_task,
            _make_task("T002", ["src/api.py", "src/utils.py"]),
        ]
        _ = infer_dependencies(tasks)
        # Original task object unchanged
        assert original_task.dependencies == []

    def test_empty_list_returns_empty_list(self) -> None:
        """infer_dependencies on empty list returns empty list."""
        assert infer_dependencies([]) == []

    def test_single_task_returns_unchanged(self) -> None:
        """Single task has nothing to be a subset of — returns unchanged."""
        tasks = [_make_task("T001", ["src/api.py"])]
        result = infer_dependencies(tasks)
        assert len(result) == 1
        assert result[0].dependencies == []


# ---------------------------------------------------------------------------
# infer_conflict_groups
# ---------------------------------------------------------------------------


class TestInferConflictGroups:
    def test_partial_overlap_creates_conflict_group(self) -> None:
        """A ∩ B nonempty but neither subset → both in a ConflictGroup."""
        tasks = [
            _make_task("T001", ["src/api.py", "src/models.py"]),
            _make_task("T002", ["src/api.py", "src/routes.py"]),
        ]
        _, groups = infer_conflict_groups(tasks)
        assert len(groups) == 1
        cg = groups[0]
        assert "T001" in cg.task_ids
        assert "T002" in cg.task_ids

    def test_no_conflict_group_for_disjoint_tasks(self) -> None:
        """Tasks with no file overlap produce no conflict groups."""
        tasks = [
            _make_task("T001", ["src/api.py"]),
            _make_task("T002", ["tests/test_utils.py"]),
        ]
        _, groups = infer_conflict_groups(tasks)
        assert groups == []

    def test_conflict_group_naming_deterministic(self) -> None:
        """Sorted task IDs in group name → same group regardless of input order."""
        tasks_ab = [
            _make_task("T001", ["src/api.py", "src/models.py"]),
            _make_task("T002", ["src/api.py", "src/routes.py"]),
        ]
        tasks_ba = [
            _make_task("T002", ["src/api.py", "src/routes.py"]),
            _make_task("T001", ["src/api.py", "src/models.py"]),
        ]
        _, groups_ab = infer_conflict_groups(tasks_ab)
        _, groups_ba = infer_conflict_groups(tasks_ba)
        assert len(groups_ab) == 1
        assert len(groups_ba) == 1
        # Both should have the same group ID (sorted IDs)
        assert groups_ab[0].id == groups_ba[0].id
        # ID follows "CG-T001-T002" pattern (sorted)
        assert groups_ab[0].id == "CG-T001-T002"

    def test_full_conflict_records_are_deterministic_for_all_input_orders(
        self,
    ) -> None:
        """Task/file order cannot change any inferred persistence record."""
        definitions = {
            "T003": ["src/shared.py", "src/three.py"],
            "T001": ["src/shared.py", "src/one.py"],
            "T002": ["./src/shared.py", "src/two.py"],
        }
        expected_groups: list[dict[str, object]] | None = None
        expected_conflicts: dict[str, list[str]] | None = None

        for task_order in permutations(definitions):
            for reversals in product((False, True), repeat=len(definitions)):
                tasks = [
                    _make_task(
                        task_id,
                        list(
                            reversed(definitions[task_id])
                            if reverse
                            else definitions[task_id]
                        ),
                    )
                    for task_id, reverse in zip(task_order, reversals, strict=True)
                ]

                conflict_tasks, groups = infer_conflict_groups(tasks)
                combined = infer_all(tasks)
                records = [group.model_dump(mode="json") for group in groups]
                combined_records = [
                    group.model_dump(mode="json") for group in combined.conflict_groups
                ]
                conflicts = {
                    task.id: task.conflict_groups for task in conflict_tasks
                }
                combined_conflicts = {
                    task.id: task.conflict_groups for task in combined.tasks
                }

                if expected_groups is None:
                    expected_groups = records
                    expected_conflicts = conflicts
                assert records == expected_groups
                assert combined_records == expected_groups
                assert conflicts == expected_conflicts
                assert combined_conflicts == expected_conflicts

        assert expected_groups is not None
        assert [record["id"] for record in expected_groups] == [
            "CG-T001-T002",
            "CG-T001-T003",
            "CG-T002-T003",
        ]
        assert all(
            str(record["reason"]).endswith(
                "share overlapping files: src/shared.py"
            )
            for record in expected_groups
        )

    def test_strict_subset_not_a_conflict(self) -> None:
        """A ⊂ B → dependency edge, not a conflict group."""
        tasks = [
            _make_task("T001", ["src/api.py"]),  # strict subset of T002
            _make_task("T002", ["src/api.py", "src/utils.py"]),
        ]
        _, groups = infer_conflict_groups(tasks)
        # Strict subset → dependency, not conflict
        assert groups == []

    def test_empty_file_task_not_in_conflict(self) -> None:
        """Task with empty likely_files is never placed in a conflict group."""
        tasks = [
            _make_task("T001", []),  # empty
            _make_task("T002", ["src/api.py"]),
        ]
        _, groups = infer_conflict_groups(tasks)
        assert groups == []

    def test_conflict_group_task_ids_in_tasks_field(self) -> None:
        """Tasks in a conflict group have the group ID in their conflict_groups field."""
        tasks = [
            _make_task("T001", ["src/api.py", "src/models.py"]),
            _make_task("T002", ["src/api.py", "src/routes.py"]),
        ]
        result_tasks, groups = infer_conflict_groups(tasks)
        assert len(groups) == 1
        cg_id = groups[0].id
        t001 = next(t for t in result_tasks if t.id == "T001")
        t002 = next(t for t in result_tasks if t.id == "T002")
        assert cg_id in t001.conflict_groups
        assert cg_id in t002.conflict_groups

    def test_input_tasks_not_mutated_conflict(self) -> None:
        """infer_conflict_groups does not mutate input tasks."""
        original = _make_task("T001", ["src/api.py", "src/models.py"])
        tasks = [original, _make_task("T002", ["src/api.py", "src/routes.py"])]
        _ = infer_conflict_groups(tasks)
        assert original.conflict_groups == []

    def test_empty_list_returns_empty_tuple(self) -> None:
        """infer_conflict_groups on empty list returns ([], [])."""
        result_tasks, groups = infer_conflict_groups([])
        assert result_tasks == []
        assert groups == []

    def test_cross_prd_overlap_emits_single_unfiltered_group(self) -> None:
        """T013: inference spans PRDs — a PRD-A task and a PRD-B task that share
        one likely_file form a single CG-... group containing both ids, and the
        ConflictGroup carries no prd_id (inference never filters by prd_id)."""
        tasks = [
            _make_task(
                "T001",
                ["src/shared.py", "src/a_only.py"],
                prd_id="default",
            ),
            _make_task(
                "T900",
                ["src/shared.py", "src/b_only.py"],
                prd_id="v0.2",
            ),
        ]
        result_tasks, groups = infer_conflict_groups(tasks)

        # Exactly one group spanning both PRDs' tasks.
        assert len(groups) == 1
        cg = groups[0]
        assert cg.id == "CG-T001-T900"
        assert sorted(cg.task_ids) == ["T001", "T900"]

        # ConflictGroup has no prd_id field at all — coordination is global.
        assert not hasattr(cg, "prd_id")

        # Both tasks (regardless of owning PRD) record membership.
        t001 = next(t for t in result_tasks if t.id == "T001")
        t900 = next(t for t in result_tasks if t.id == "T900")
        assert cg.id in t001.conflict_groups
        assert cg.id in t900.conflict_groups


# ---------------------------------------------------------------------------
# infer_all
# ---------------------------------------------------------------------------


class TestInferAll:
    @pytest.mark.parametrize(
        "path_sets",
        [
            [
                ["src/api.py", "src/models.py"],
                ["src/api.py", "src/routes.py"],
                ["src/api.py"],
            ],
            [
                ["./src\\api.py", "src/./models.py"],
                ["src/api.py", ".\\src\\routes.py"],
                ["src/parts/../api.py"],
            ],
        ],
    )
    def test_equivalent_path_spellings_produce_the_same_full_graph(
        self,
        path_sets: list[list[str]],
    ) -> None:
        result = infer_all(
            [
                _make_task(f"T{index:03}", paths)
                for index, paths in enumerate(path_sets, start=1)
            ]
        )

        assert {
            task.id: (task.dependencies, task.conflict_groups)
            for task in result.tasks
        } == {
            "T001": ([], ["CG-T001-T002"]),
            "T002": ([], ["CG-T001-T002"]),
            "T003": (["T001", "T002"], []),
        }
        assert [group.id for group in result.conflict_groups] == ["CG-T001-T002"]

    def test_infer_all_composes_correctly(self) -> None:
        """infer_all: dependencies first, conflicts second, no double-flagging.

        Setup:
        - T001: files [A, B]
        - T002: files [A, B, C]  (T001 ⊂ T002 → T001 depends on T002; no conflict)
        - T003: files [A, D]     (overlaps T001 and T002 partially → conflicts)
        """
        tasks = [
            _make_task("T001", ["a.py", "b.py"]),
            _make_task("T002", ["a.py", "b.py", "c.py"]),
            _make_task("T003", ["a.py", "d.py"]),
        ]
        result = infer_all(tasks)
        assert isinstance(result, InferenceResult)
        assert len(result.tasks) == 3

        t001 = next(t for t in result.tasks if t.id == "T001")

        # T001 ⊂ T002 → T001 depends on T002
        assert "T002" in t001.dependencies

        # T003 partially overlaps T001 → conflict
        # (T003 has [a.py, d.py]; T001 has [a.py, b.py]; partial overlap)
        # T003 partially overlaps T002 → conflict
        # (T003 has [a.py, d.py]; T002 has [a.py, b.py, c.py]; partial overlap)
        assert len(result.conflict_groups) >= 1

        # T001 and T002 should NOT be in the same conflict group (they are subset/superset)
        t001_t002_pair = {"T001", "T002"}
        for cg in result.conflict_groups:
            assert set(cg.task_ids) != t001_t002_pair, (
                "T001 and T002 should not be in a conflict group (they have a subset relationship)"
            )

    def test_infer_all_returns_inference_result(self) -> None:
        """infer_all returns InferenceResult with tasks and conflict_groups fields."""
        tasks = [
            _make_task("T001", ["src/api.py"]),
            _make_task("T002", ["src/api.py", "src/utils.py"]),
        ]
        result = infer_all(tasks)
        assert hasattr(result, "tasks")
        assert hasattr(result, "conflict_groups")

    def test_infer_all_empty_list(self) -> None:
        """infer_all on empty list returns empty InferenceResult."""
        result = infer_all([])
        assert result.tasks == []
        assert result.conflict_groups == []

    def test_infer_all_preserves_task_count(self) -> None:
        """infer_all always returns the same number of tasks as input."""
        tasks = [
            _make_task("T001", ["a.py"]),
            _make_task("T002", ["b.py"]),
            _make_task("T003", ["a.py", "b.py"]),
        ]
        result = infer_all(tasks)
        assert len(result.tasks) == 3

    def test_infer_all_no_side_effects(self) -> None:
        """infer_all does not mutate the original task list."""
        original_t1 = _make_task("T001", ["a.py"])
        tasks = [original_t1, _make_task("T002", ["a.py", "b.py"])]
        _ = infer_all(tasks)
        # Original task remains unchanged
        assert original_t1.dependencies == []
        assert original_t1.conflict_groups == []
