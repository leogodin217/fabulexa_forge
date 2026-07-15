"""Tests for conformance framework + C1–C12 structural checks.

Driven by the session-scoped `base_fixtures` mapping from conftest.py.
In-memory fixtures supplement where the pre-built set lacks a specific variant.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.reader import (
    CheckResult,
    ConformanceReport,
    open_emit,
    run_check,
    validate,
)
from fabulexa_forge.reader._schema import _load_vendored_schema
from fabulexa_forge.reader.conformance import (
    _check_c5_table,
    _check_c13_structural,
)
from fabulexa_forge.reader.sidecar import ColumnSpec

from ._fixtures_build import (
    _HISTORY_COLUMNS,
    _RECORDS_ACTOR_COLUMNS,
    _create_table_ddl,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_emit(
    dest: Path,
    sidecar: dict[str, object],
    db_setup: dict[str, list[dict[str, object]]] | None = None,
) -> Path:
    """Write a minimal emit (base.json + run.duckdb) into dest.

    Args:
        dest: Directory to write into.
        sidecar: The base.json dict.
        db_setup: Mapping of {table_name: columns_list} for tables to create.

    Returns:
        dest path.
    """
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    if db_setup:
        for tname, cols in db_setup.items():
            conn.execute(_create_table_ddl(tname, cols))
    conn.close()
    return dest


def _minimal_sidecar(
    tables: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build a minimal valid v4 sidecar with only history (sanitised: no firings)."""
    default_tables: list[dict[str, object]] = [
        {
            "name": "history",
            "category": "fixed",
            "columns": list(_HISTORY_COLUMNS),
            "rows": 0,
        },
    ]
    return {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": tables if tables is not None else default_tables,
    }


# ---------------------------------------------------------------------------
# _load_vendored_schema
# ---------------------------------------------------------------------------


class TestLoadVendoredSchema:
    """_load_vendored_schema returns a cached, usable JSON Schema mapping."""

    def test_returns_mapping_with_schema_key(self) -> None:
        """The returned schema has a $schema key (is a JSON Schema object)."""
        schema = _load_vendored_schema()
        assert "$schema" in schema

    def test_returns_same_object_on_repeated_calls(self) -> None:
        """Repeated calls return the same cached object (identity check)."""
        s1 = _load_vendored_schema()
        s2 = _load_vendored_schema()
        assert s1 is s2

    def test_has_properties_and_required(self) -> None:
        """Schema has properties and required keys."""
        schema = _load_vendored_schema()
        assert "properties" in schema
        assert "required" in schema


# ---------------------------------------------------------------------------
# CheckResult and ConformanceReport
# ---------------------------------------------------------------------------


class TestCheckResult:
    """CheckResult is a frozen dataclass with passed, messages, skips."""

    def test_frozen(self) -> None:
        """CheckResult is immutable."""
        r = CheckResult(check="C1", passed=True, messages=(), skips=())
        with pytest.raises((AttributeError, TypeError)):
            r.passed = False  # type: ignore[misc]


class TestConformanceReport:
    """ConformanceReport.ok is True iff all results passed."""

    def test_ok_all_pass(self) -> None:
        """ok is True when every result passed."""
        results = tuple(
            CheckResult(check=f"C{i}", passed=True, messages=(), skips=())
            for i in range(1, 6)
        )
        report = ConformanceReport(results=results)
        assert report.ok is True

    def test_ok_one_fail(self) -> None:
        """ok is False when any result failed."""
        results = (
            CheckResult(check="C1", passed=True, messages=(), skips=()),
            CheckResult(check="C2", passed=False, messages=("bad",), skips=()),
            CheckResult(check="C3", passed=True, messages=(), skips=()),
            CheckResult(check="C4", passed=True, messages=(), skips=()),
            CheckResult(check="C5", passed=True, messages=(), skips=()),
        )
        report = ConformanceReport(results=results)
        assert report.ok is False


# ---------------------------------------------------------------------------
# C1 tests
# ---------------------------------------------------------------------------


class TestC1:
    """C1: base.json validates against the vendored v4 JSON Schema."""

    def test_passes_on_spanning(self, base_fixtures: dict[str, Path]) -> None:
        """C1 passes on the spanning fixture."""
        with open_emit(base_fixtures["spanning"]) as emit:
            result = run_check(emit, "C1")
        assert result.passed is True
        assert result.messages == ()

    def test_unknown_top_level_field_passes_with_skip(self, tmp_path: Path) -> None:
        """An unknown top-level sidecar field passes C1 and is recorded in skips."""
        sidecar = _minimal_sidecar()
        sidecar["unknown_extra_field"] = "hello"
        dest = _write_emit(
            tmp_path / "c1_unknown_top",
            sidecar,
            {"history": list(_HISTORY_COLUMNS)},
        )
        with open_emit(dest) as emit:
            result = run_check(emit, "C1")
        assert result.passed is True
        assert any("unknown_extra_field" in s for s in result.skips)

    def test_unknown_nested_field_fails_c1(self, tmp_path: Path) -> None:
        """An unknown field inside a branch object fails C1."""
        sidecar = _minimal_sidecar()
        # Inject an unknown key into the first branch object
        branches = list(sidecar["branches"])  # type: ignore[arg-type]
        branches[0] = dict(branches[0])  # type: ignore[arg-type]
        branches[0]["unknown_branch_field"] = "boom"  # type: ignore[index]
        sidecar["branches"] = branches
        dest = _write_emit(
            tmp_path / "c1_nested_unknown",
            sidecar,
            {"history": list(_HISTORY_COLUMNS)},
        )
        with open_emit(dest) as emit:
            result = run_check(emit, "C1")
        assert result.passed is False
        assert len(result.messages) > 0


# ---------------------------------------------------------------------------
# C2 tests
# ---------------------------------------------------------------------------


class TestC2:
    """C2: DuckDB catalog matches the sidecar."""

    def test_passes_on_spanning(self, base_fixtures: dict[str, Path]) -> None:
        """C2 passes on the spanning fixture."""
        with open_emit(base_fixtures["spanning"]) as emit:
            result = run_check(emit, "C2")
        assert result.passed is True

    def test_fails_on_schema_mismatch_names_phantom_column(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """C2 fails on schema_mismatch and names the phantom sidecar column."""
        with open_emit(base_fixtures["schema_mismatch"]) as emit:
            result = run_check(emit, "C2")
        assert result.passed is False
        assert any("prop__phantom_column" in m for m in result.messages)

    def test_fails_on_c5_prop_missing_names_missing_column(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """C2 fails on c5_prop_missing and names the dropped DuckDB column."""
        with open_emit(base_fixtures["c5_prop_missing"]) as emit:
            result = run_check(emit, "C2")
        assert result.passed is False
        assert any("prop__name" in m for m in result.messages)

    def test_type_mismatch_fails_c2(self, tmp_path: Path) -> None:
        """A type-literal mismatch between catalog and sidecar fails C2."""
        db_cols = [{"name": "fork_path", "type": "INTEGER"}]
        sidecar_cols: list[dict[str, object]] = [
            {"name": "fork_path", "type": "BIGINT"}
        ]
        sidecar: dict[str, object] = {
            "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
            "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
            "tables": [
                {
                    "name": "history",
                    "category": "fixed",
                    "columns": sidecar_cols,
                    "rows": 0,
                }
            ],
        }
        dest = _write_emit(
            tmp_path / "c2_type_mismatch",
            sidecar,
            {"history": db_cols},
        )
        with open_emit(dest) as emit:
            result = run_check(emit, "C2")
        assert result.passed is False
        assert any("type mismatch" in m for m in result.messages)

    def test_row_count_mismatch_fails_c2(self, tmp_path: Path) -> None:
        """A row count mismatch between catalog and sidecar fails C2."""
        sidecar_cols = list(_HISTORY_COLUMNS)
        sidecar: dict[str, object] = {
            "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
            "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
            "tables": [
                {
                    "name": "history",
                    "category": "fixed",
                    "columns": sidecar_cols,
                    "rows": 5,  # wrong
                }
            ],
        }
        dest = _write_emit(
            tmp_path / "c2_row_count",
            sidecar,
            {"history": list(_HISTORY_COLUMNS)},
        )
        with open_emit(dest) as emit:
            result = run_check(emit, "C2")
        assert result.passed is False
        assert any("row count mismatch" in m for m in result.messages)

    def test_cardinality_strict_extra_catalog_column(self, tmp_path: Path) -> None:
        """C2 fails when catalog has more columns than sidecar (no zip-truncation)."""
        db_cols = [
            {"name": "fork_path", "type": "VARCHAR"},
            {"name": "sim_time", "type": "BIGINT"},
        ]
        sidecar_cols: list[dict[str, object]] = [
            {"name": "fork_path", "type": "VARCHAR"}
        ]
        sidecar: dict[str, object] = {
            "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
            "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
            "tables": [
                {
                    "name": "history",
                    "category": "fixed",
                    "columns": sidecar_cols,
                    "rows": 0,
                }
            ],
        }
        dest = _write_emit(
            tmp_path / "c2_extra_catalog",
            sidecar,
            {"history": db_cols},
        )
        with open_emit(dest) as emit:
            result = run_check(emit, "C2")
        assert result.passed is False

    def test_cardinality_strict_extra_sidecar_column(self, tmp_path: Path) -> None:
        """C2 fails when sidecar declares more columns than catalog (no zip-truncation)."""
        db_cols = [{"name": "fork_path", "type": "VARCHAR"}]
        sidecar_cols: list[dict[str, object]] = [
            {"name": "fork_path", "type": "VARCHAR"},
            {"name": "sim_time", "type": "BIGINT"},
        ]
        sidecar: dict[str, object] = {
            "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
            "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
            "tables": [
                {
                    "name": "history",
                    "category": "fixed",
                    "columns": sidecar_cols,
                    "rows": 0,
                }
            ],
        }
        dest = _write_emit(
            tmp_path / "c2_extra_sidecar",
            sidecar,
            {"history": db_cols},
        )
        with open_emit(dest) as emit:
            result = run_check(emit, "C2")
        assert result.passed is False

    def test_same_names_different_cardinality_fails_c2(self, tmp_path: Path) -> None:
        """C2 fails with a count mismatch when column *sets* agree but counts differ.

        The sidecar declares fork_path twice while the catalog holds it once:
        cat_set == sc_set, so neither the surplus nor the missing branch fires,
        and the duplicate is reported as a column count mismatch.
        """
        db_cols = [{"name": "fork_path", "type": "VARCHAR"}]
        sidecar_cols: list[dict[str, object]] = [
            {"name": "fork_path", "type": "VARCHAR"},
            {"name": "fork_path", "type": "VARCHAR"},  # duplicate declaration
        ]
        sidecar: dict[str, object] = {
            "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
            "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
            "tables": [
                {
                    "name": "history",
                    "category": "fixed",
                    "columns": sidecar_cols,
                    "rows": 0,
                }
            ],
        }
        dest = _write_emit(
            tmp_path / "c2_dup_sidecar_col",
            sidecar,
            {"history": db_cols},
        )
        with open_emit(dest) as emit:
            result = run_check(emit, "C2")
        assert result.passed is False
        assert any("column count mismatch" in m for m in result.messages), (
            f"Expected a column count mismatch message, got {result.messages}"
        )


# ---------------------------------------------------------------------------
# C3 tests
# ---------------------------------------------------------------------------


class TestC3:
    """C3: Required tables present; table names well-formed per category."""

    def test_passes_on_spanning(self, base_fixtures: dict[str, Path]) -> None:
        """C3 passes on the spanning fixture (history only, no firings required)."""
        with open_emit(base_fixtures["spanning"]) as emit:
            result = run_check(emit, "C3")
        assert result.passed is True

    def test_records_none_record_kind_fails_c3(self, tmp_path: Path) -> None:
        """A records table with absent record_kind fails C3 as name-composition mismatch."""
        sidecar: dict[str, object] = {
            "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
            "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
            "tables": [
                {
                    "name": "history",
                    "category": "fixed",
                    "columns": list(_HISTORY_COLUMNS),
                    "rows": 0,
                },
                {
                    "name": "records__actor",
                    "category": "records",
                    # record_kind intentionally omitted
                    "columns": list(_RECORDS_ACTOR_COLUMNS),
                    "rows": 0,
                },
            ],
        }
        dest = _write_emit(
            tmp_path / "c3_no_record_kind",
            sidecar,
            {
                "history": list(_HISTORY_COLUMNS),
                "records__actor": list(_RECORDS_ACTOR_COLUMNS),
            },
        )
        with open_emit(dest) as emit:
            result = run_check(emit, "C3")
        assert result.passed is False
        assert isinstance(result, CheckResult)  # never raised, always a CheckResult

    def test_membership_none_property_fails_c3(self, tmp_path: Path) -> None:
        """A membership table with absent property fails C3 as name-composition mismatch."""
        membership_cols: list[dict[str, object]] = [
            {"name": "fork_path", "type": "VARCHAR"},
            {"name": "record_id", "type": "VARCHAR"},
            {"name": "joined_sim_time", "type": "BIGINT"},
            {"name": "left_sim_time", "type": "BIGINT"},
        ]
        sidecar: dict[str, object] = {
            "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
            "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
            "tables": [
                {
                    "name": "history",
                    "category": "fixed",
                    "columns": list(_HISTORY_COLUMNS),
                    "rows": 0,
                },
                {
                    "name": "membership__actor__appointments",
                    "category": "membership",
                    "record_kind": "actor",
                    # property intentionally omitted
                    "columns": membership_cols,
                    "rows": 0,
                },
            ],
        }
        dest = _write_emit(
            tmp_path / "c3_no_property",
            sidecar,
            {
                "history": list(_HISTORY_COLUMNS),
                "membership__actor__appointments": membership_cols,
            },
        )
        with open_emit(dest) as emit:
            result = run_check(emit, "C3")
        assert result.passed is False
        assert isinstance(result, CheckResult)  # never raised


# ---------------------------------------------------------------------------
# C4 tests
# ---------------------------------------------------------------------------


class TestC4:
    """C4: history cols 1-6 match the pinned spec exactly (sanitised: no firings)."""

    def test_passes_on_spanning(self, base_fixtures: dict[str, Path]) -> None:
        """C4 passes on the spanning fixture (history 6 base cols only)."""
        with open_emit(base_fixtures["spanning"]) as emit:
            result = run_check(emit, "C4")
        assert result.passed is True

    def test_fails_on_c4_wrong_history_type(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """C4 fails on c4_wrong_history_type (fork_path is BIGINT, not VARCHAR)."""
        with open_emit(base_fixtures["c4_wrong_history_type"]) as emit:
            result = run_check(emit, "C4")
        assert result.passed is False


# ---------------------------------------------------------------------------
# C5 tests
# ---------------------------------------------------------------------------


class TestC5:
    """C5: records__K shape: head + optional presentation_id + tail prefix, then a
    contiguous prop__ block (no provenance suffix in the sanitised shape)."""

    def test_passes_on_spanning(self, base_fixtures: dict[str, Path]) -> None:
        """C5 passes on the spanning fixture (6-col prefix, prop__ block, no provenance)."""
        with open_emit(base_fixtures["spanning"]) as emit:
            result = run_check(emit, "C5")
        assert result.passed is True

    def test_fails_on_c5_prop_missing(self, base_fixtures: dict[str, Path]) -> None:
        """C5 fails on c5_prop_missing (prop__name dropped from DuckDB, sidecar mismatch)."""
        with open_emit(base_fixtures["c5_prop_missing"]) as emit:
            result = run_check(emit, "C5")
        assert result.passed is False

    def test_fails_on_schema_mismatch(self, base_fixtures: dict[str, Path]) -> None:
        """C5 fails on schema_mismatch (phantom sidecar column)."""
        with open_emit(base_fixtures["schema_mismatch"]) as emit:
            result = run_check(emit, "C5")
        assert result.passed is False


def _col(name: str, type_: str = "VARCHAR") -> ColumnSpec:
    return ColumnSpec(
        name=name,
        type=type_,
        references=None,
        history_tracked=None,
        temporal_class=None,
    )


# fork_path, record_id
_HEAD = [_col("fork_path"), _col("record_id")]
# created_sim_time, active, deactivated_at, last_mutation_sim_time
_TAIL = [
    _col("created_sim_time", "BIGINT"),
    _col("active", "BOOLEAN"),
    _col("deactivated_at", "BIGINT"),
    _col("last_mutation_sim_time", "BIGINT"),
]


class TestC5PresentationId:
    """C5 positional contract for the optional projection-minted presentation_id.

    presentation_id is permitted only at index 2 (immediately after record_id),
    where its name and position are pinned but its type is taken from the sidecar.
    Anywhere else it must fail. These exercise _check_c5_table directly because the
    check operates purely on the ordered sidecar ColumnSpec list.
    """

    def _check(self, cols: list[ColumnSpec]) -> list[str]:
        messages: list[str] = []
        _check_c5_table("records__actor", cols, messages)
        return messages

    def test_mechanism_emit_no_presentation_id_passes(self) -> None:
        """A mechanism emit (no presentation_id) still passes — regression guard."""
        assert self._check(_HEAD + _TAIL + [_col("prop__name")]) == []

    def test_bigint_presentation_id_at_index_2_passes(self) -> None:
        """A sequential-strategy emit mints a BIGINT presentation_id at index 2."""
        cols = (
            _HEAD + [_col("presentation_id", "BIGINT")] + _TAIL + [_col("prop__name")]
        )
        assert self._check(cols) == []

    def test_varchar_presentation_id_at_index_2_passes(self) -> None:
        """A prefixed/uuid-strategy emit mints a VARCHAR presentation_id at index 2."""
        cols = (
            _HEAD + [_col("presentation_id", "VARCHAR")] + _TAIL + [_col("prop__name")]
        )
        assert self._check(cols) == []

    def test_presentation_id_only_no_props_passes(self) -> None:
        """presentation_id with no scalar properties is a valid minimal projected table."""
        cols = _HEAD + [_col("presentation_id", "BIGINT")] + _TAIL
        assert self._check(cols) == []

    def test_misplaced_presentation_id_after_active_fails(self) -> None:
        """presentation_id past index 2 is not consumed and displaces the tail."""
        cols = [
            _col("fork_path"),
            _col("record_id"),
            _col("created_sim_time", "BIGINT"),
            _col("active", "BOOLEAN"),
            _col("presentation_id", "BIGINT"),
            _col("deactivated_at", "BIGINT"),
            _col("last_mutation_sim_time", "BIGINT"),
            _col("prop__name"),
        ]
        messages = self._check(cols)
        assert any("presentation_id" in m for m in messages)

    def test_presentation_id_in_prop_block_fails(self) -> None:
        """presentation_id after the lifecycle prefix lands in the prop block and fails."""
        cols = _HEAD + _TAIL + [_col("presentation_id", "BIGINT"), _col("prop__name")]
        messages = self._check(cols)
        assert any("presentation_id" in m and "prop__" in m for m in messages)

    def test_presentation_id_table_too_short_fails(self) -> None:
        """presentation_id present but the tail is truncated → length failure."""
        cols = _HEAD + [_col("presentation_id", "BIGINT")]
        messages = self._check(cols)
        assert any("expected at least 7 columns" in m for m in messages)


# ---------------------------------------------------------------------------
# C13 structural clauses (direct, via _check_c13_structural)
# ---------------------------------------------------------------------------


def _c13_messages(col: ColumnSpec) -> list[str]:
    """Run _check_c13_structural for one column; return its failure messages."""
    messages: list[str] = []
    _check_c13_structural("records__actor", col, messages)
    return messages


class TestC13Structural:
    """_check_c13_structural's four clauses, exercised directly per the design
    doc: (history_tracked present) == (temporal_class present); a present
    temporal_class is one of the three declared values; 'tracked' implies
    history_tracked True; 'slice_only' implies history_tracked False."""

    def test_conformant_tracked_column_passes(self) -> None:
        """A properly paired 'tracked' column passes all four clauses."""
        col = ColumnSpec(
            name="prop__name",
            type="VARCHAR",
            references=None,
            history_tracked=True,
            temporal_class="tracked",
        )
        assert _c13_messages(col) == []

    def test_conformant_constant_column_passes(self) -> None:
        """A properly paired 'constant' column passes (no implication applies)."""
        col = ColumnSpec(
            name="prop__doctor_id",
            type="VARCHAR",
            references=None,
            history_tracked=False,
            temporal_class="constant",
        )
        assert _c13_messages(col) == []

    def test_conformant_slice_only_column_passes(self) -> None:
        """A properly paired 'slice_only' column passes all four clauses."""
        col = ColumnSpec(
            name="prop__status",
            type="VARCHAR",
            references=None,
            history_tracked=False,
            temporal_class="slice_only",
        )
        assert _c13_messages(col) == []

    def test_history_tracked_present_temporal_class_absent_fails(self) -> None:
        """Clause 1 (pairing): history_tracked present with no temporal_class fails."""
        col = ColumnSpec(
            name="prop__name",
            type="VARCHAR",
            references=None,
            history_tracked=True,
            temporal_class=None,
        )
        messages = _c13_messages(col)
        assert any("history_tracked present" in m for m in messages)

    def test_temporal_class_present_history_tracked_absent_fails(self) -> None:
        """Clause 1 (pairing), other direction: temporal_class with no
        history_tracked fails."""
        col = ColumnSpec(
            name="prop__name",
            type="VARCHAR",
            references=None,
            history_tracked=None,
            temporal_class="tracked",
        )
        messages = _c13_messages(col)
        assert any("history_tracked present" in m for m in messages)

    def test_out_of_enum_temporal_class_fails(self) -> None:
        """Clause 2 (enum): a declared value outside the three-value enum fails."""
        col = ColumnSpec(
            name="prop__name",
            type="VARCHAR",
            references=None,
            history_tracked=True,
            temporal_class="bogus",
        )
        messages = _c13_messages(col)
        assert any("outside" in m for m in messages)

    def test_tracked_requires_history_tracked_true(self) -> None:
        """Clause 3: temporal_class='tracked' requires history_tracked=True."""
        col = ColumnSpec(
            name="prop__name",
            type="VARCHAR",
            references=None,
            history_tracked=False,
            temporal_class="tracked",
        )
        messages = _c13_messages(col)
        assert any("'tracked' requires" in m for m in messages)

    def test_slice_only_requires_history_tracked_false(self) -> None:
        """Clause 4: temporal_class='slice_only' requires history_tracked=False."""
        col = ColumnSpec(
            name="prop__name",
            type="VARCHAR",
            references=None,
            history_tracked=True,
            temporal_class="slice_only",
        )
        messages = _c13_messages(col)
        assert any("'slice_only' requires" in m for m in messages)


# ---------------------------------------------------------------------------
# C8 tests
# ---------------------------------------------------------------------------


class TestC8:
    """C8: Exactly one branch; distinct fork_path across all tables equals that branch."""

    def test_passes_on_spanning(self, base_fixtures: dict[str, Path]) -> None:
        """C8 passes on the spanning fixture (single trunk branch)."""
        with open_emit(base_fixtures["spanning"]) as emit:
            result = run_check(emit, "C8")
        assert result.passed is True

    def test_multi_branch_fails_c8(self, tmp_path: Path) -> None:
        """An emit whose branches has != 1 entry fails C8."""
        sidecar: dict[str, object] = {
            "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
            "branches": [
                {"fork_path": "trunk", "parent": None, "slice_at": 0},
                {"fork_path": "branch-a", "parent": "trunk", "slice_at": 10},
            ],
            "tables": [
                {
                    "name": "history",
                    "category": "fixed",
                    "columns": list(_HISTORY_COLUMNS),
                    "rows": 0,
                }
            ],
        }
        dest = _write_emit(
            tmp_path / "c8_multi_branch",
            sidecar,
            {"history": list(_HISTORY_COLUMNS)},
        )
        with open_emit(dest) as emit:
            result = run_check(emit, "C8")
        assert result.passed is False
        assert any("exactly 1" in m for m in result.messages)


# ---------------------------------------------------------------------------
# validate tests
# ---------------------------------------------------------------------------


class TestValidate:
    """validate never raises; returns ConformanceReport with one result per check."""

    def test_never_raises_on_c4_wrong_history_type(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """validate does not raise on the c4_wrong_history_type fixture."""
        with open_emit(base_fixtures["c4_wrong_history_type"]) as emit:
            report = validate(emit)
        assert isinstance(report, ConformanceReport)

    def test_never_raises_on_schema_mismatch(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """validate does not raise on the schema_mismatch fixture (catalog disagreement)."""
        with open_emit(base_fixtures["schema_mismatch"]) as emit:
            report = validate(emit)
        assert isinstance(report, ConformanceReport)

    def test_never_raises_on_c5_prop_missing(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """validate does not raise on the c5_prop_missing fixture."""
        with open_emit(base_fixtures["c5_prop_missing"]) as emit:
            report = validate(emit)
        assert isinstance(report, ConformanceReport)

    def test_results_in_c1_to_c13_order(self, base_fixtures: dict[str, Path]) -> None:
        """validate returns results in C1..C13 order."""
        with open_emit(base_fixtures["spanning"]) as emit:
            report = validate(emit)
        check_ids = [r.check for r in report.results]
        assert check_ids == [
            "C1",
            "C2",
            "C3",
            "C4",
            "C5",
            "C6",
            "C7",
            "C8",
            "C9",
            "C10",
            "C11",
            "C12",
            "C13",
        ]

    def test_c4_wrong_history_type_fails_c4_not_c2(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """c4_wrong_history_type sidecar matches DuckDB (C2 passes) but C4 fails."""
        with open_emit(base_fixtures["c4_wrong_history_type"]) as emit:
            report = validate(emit)
        c2 = next(r for r in report.results if r.check == "C2")
        c4 = next(r for r in report.results if r.check == "C4")
        assert c2.passed is True
        assert c4.passed is False

    def test_schema_mismatch_fails_c2_and_c5(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """schema_mismatch fixture fails C2 and C5."""
        with open_emit(base_fixtures["schema_mismatch"]) as emit:
            report = validate(emit)
        c2 = next(r for r in report.results if r.check == "C2")
        c5 = next(r for r in report.results if r.check == "C5")
        assert c2.passed is False
        assert c5.passed is False

    def test_c5_prop_missing_fails_c2_and_c5(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """c5_prop_missing fixture fails C2 and C5."""
        with open_emit(base_fixtures["c5_prop_missing"]) as emit:
            report = validate(emit)
        c2 = next(r for r in report.results if r.check == "C2")
        c5 = next(r for r in report.results if r.check == "C5")
        assert c2.passed is False
        assert c5.passed is False


# ---------------------------------------------------------------------------
# run_check tests
# ---------------------------------------------------------------------------


class TestRunCheck:
    """run_check returns the same result as validate for a recognized id."""

    def test_unknown_id_raises_value_error(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """run_check raises ValueError for an unrecognized check id."""
        with open_emit(base_fixtures["spanning"]) as emit:
            with pytest.raises(ValueError, match="C99"):
                run_check(emit, "C99")

    def test_recognized_id_matches_validate(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """run_check for a recognized id returns the same result as validate."""
        with open_emit(base_fixtures["spanning"]) as emit:
            report = validate(emit)
            for result in report.results:
                single = run_check(emit, result.check)
                assert single.check == result.check
                assert single.passed == result.passed

    def test_run_check_c4_on_c4_fixture(self, base_fixtures: dict[str, Path]) -> None:
        """run_check C4 on c4_wrong_history_type fixture fails."""
        with open_emit(base_fixtures["c4_wrong_history_type"]) as emit:
            result = run_check(emit, "C4")
        assert result.passed is False

    def test_run_check_c5_and_c2_on_c5_prop_missing(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """run_check C5 and C2 on c5_prop_missing both fail."""
        with open_emit(base_fixtures["c5_prop_missing"]) as emit:
            c5 = run_check(emit, "C5")
            c2 = run_check(emit, "C2")
        assert c5.passed is False
        assert c2.passed is False

    def test_run_check_c2_and_c5_on_schema_mismatch(
        self, base_fixtures: dict[str, Path]
    ) -> None:
        """run_check C2 and C5 on schema_mismatch both fail."""
        with open_emit(base_fixtures["schema_mismatch"]) as emit:
            c2 = run_check(emit, "C2")
            c5 = run_check(emit, "C5")
        assert c2.passed is False
        assert c5.passed is False
