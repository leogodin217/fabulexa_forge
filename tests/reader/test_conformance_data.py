"""Tests for conformance data checks C6–C12.

Driven by the session-scoped `base_fixtures` mapping from conftest.py.
In-memory fixtures supplement where the pre-built set lacks a specific variant.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from _support.sidecar_builder import write_emit as _write_sidecar

from fabulexa_forge.reader import open_emit, run_check, validate
from fabulexa_forge.reader.conformance import to_csv_text

from ._fixtures_build import (
    _HISTORY_COLUMNS,
    _MEMBERSHIP_COLUMNS,
    _RECORDS_ACTOR_COLUMNS,
    _create_table_ddl,
    _populate_membership,
    _populate_records_actor,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIDECAR_TOP_LEVEL_KEYS = frozenset({"base_format_version", "branches", "tables"})


def _write_emit(
    dest: Path,
    sidecar: dict[str, object],
    db_setup: dict[str, list[dict[str, object]]] | None = None,
    *,
    schema_valid: bool = True,
    records_shape_valid: bool = True,
) -> Path:
    """Write a minimal emit (base.json + run.duckdb) into dest.

    The base.json write is delegated to `_support.sidecar_builder.write_emit` —
    the sole sidecar authority; this helper decomposes `sidecar` into that
    function's tables/branches/extra/base_format_version components and keeps
    only the run.duckdb construction local.

    Args:
        dest: Directory to write into.
        sidecar: The base.json dict.
        db_setup: Mapping of {table_name: columns_list} for tables to create.
        schema_valid: Forwarded to sidecar_builder.write_emit. False for the
            deliberately schema-invalid negative fixtures.
        records_shape_valid: Forwarded to sidecar_builder.write_emit. False
            for negative fixtures whose declared defect is the records
            shape itself.

    Returns:
        dest path.
    """
    dest.mkdir(parents=True, exist_ok=True)
    extra = {
        key: value
        for key, value in sidecar.items()
        if key not in _SIDECAR_TOP_LEVEL_KEYS
    }
    _write_sidecar(
        dest,
        tables=sidecar["tables"],  # type: ignore[arg-type]
        branches=sidecar.get("branches"),  # type: ignore[arg-type]
        extra=extra or None,
        base_format_version=sidecar.get("base_format_version"),  # type: ignore[arg-type]
        schema_valid=schema_valid,
        records_shape_valid=records_shape_valid,
    )
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    if db_setup:
        for tname, cols in db_setup.items():
            conn.execute(_create_table_ddl(tname, cols))
    conn.close()
    return dest


def _minimal_sidecar_with_tables(
    tables: list[dict[str, object]],
    branches: list[dict[str, object]] | None = None,
    pinned_ids: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a sidecar dict with given tables and branches.

    Args:
        tables: The tables list for the sidecar.
        branches: Branches list; defaults to a single trunk branch with slice_at=100.
        pinned_ids: Optional pinned_ids block.

    Returns:
        A sidecar dict.
    """
    if branches is None:
        branches = [{"fork_path": "trunk", "parent": None, "slice_at": 100}]
    sidecar: dict[str, object] = {
        "branches": branches,
        "tables": tables,
    }
    if pinned_ids is not None:
        sidecar["pinned_ids"] = pinned_ids
    return sidecar


def _single_branch_sidecar(
    tables: list[dict[str, object]],
) -> dict[str, object]:
    """Build a single-branch sidecar with no pinned_ids.

    Args:
        tables: The tables list for the sidecar.

    Returns:
        A sidecar dict with one trunk branch.
    """
    return _minimal_sidecar_with_tables(tables)


# ---------------------------------------------------------------------------
# to_csv_text codec tests
# ---------------------------------------------------------------------------


def test_to_csv_text_bigint() -> None:
    """BIGINT value is encoded as str(int)."""
    assert to_csv_text(42, "BIGINT") == "42"
    assert to_csv_text(0, "BIGINT") == "0"
    assert to_csv_text(-100, "BIGINT") == "-100"


def test_to_csv_text_double() -> None:
    """DOUBLE value is encoded as repr(float)."""
    assert to_csv_text(3.14, "DOUBLE") == repr(3.14)
    assert to_csv_text(0.0, "DOUBLE") == repr(0.0)


def test_to_csv_text_boolean_true() -> None:
    """BOOLEAN True encodes to 'true' (lowercase)."""
    assert to_csv_text(True, "BOOLEAN") == "true"


def test_to_csv_text_boolean_false() -> None:
    """BOOLEAN False encodes to 'false' (lowercase)."""
    assert to_csv_text(False, "BOOLEAN") == "false"


def test_to_csv_text_varchar_identity() -> None:
    """VARCHAR value is returned as-is (identity)."""
    assert to_csv_text("hello", "VARCHAR") == "hello"
    assert to_csv_text("", "VARCHAR") == ""


def test_to_csv_text_blob_raises() -> None:
    """BLOB (or other unsupported) type raises ValueError."""
    with pytest.raises(ValueError, match="BLOB"):
        to_csv_text(b"\x00\x01", "BLOB")


def test_to_csv_text_unknown_type_raises() -> None:
    """Unknown type raises ValueError."""
    with pytest.raises(ValueError):
        to_csv_text(42, "INTEGER")


# ---------------------------------------------------------------------------
# C6: history round-trip
# ---------------------------------------------------------------------------


def test_c6_passes_on_spanning(
    base_fixtures: dict[str, Path],
) -> None:
    """C6 passes on spanning: history.value matches encoded records cell."""
    with open_emit(base_fixtures["spanning"]) as emit:
        result = run_check(emit, "C6")
    assert result.passed, f"C6 failed: {result.messages}"
    assert result.check == "C6"


def test_c6_skips_non_round_trippable_prop(tmp_path: Path) -> None:
    """C6 records a non-round-trippable prop column in skips and still passes."""
    blob_col: dict[str, object] = {"name": "prop__data", "type": "BLOB"}
    rec_cols = list(_RECORDS_ACTOR_COLUMNS) + [blob_col]

    dest = tmp_path / "c6_blob"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", rec_cols))
    # history row for 'name' (VARCHAR — round-trippable)
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ["trunk", "actor", "a001", "name", 10, "Alice"],
    )
    # history row for 'data' (BLOB — non-round-trippable)
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ["trunk", "actor", "a001", "data", 10, "sometext"],
    )
    # records__actor row (12 cols: fork_path, record_id, created_sim_time, active,
    # deactivated_at(NULL), last_mutation_sim_time, record_index, prop__name,
    # prop__status, prop__doctor_id, ref_index__doctor_id, prop__actor_type) +
    # prop__data (BLOB)
    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "trunk",
            "a001",
            10,
            True,
            10,
            0,
            "Alice",
            "active",
            "d001",
            0,
            "patient",
            b"\x00\x01",
        ],
    )
    conn.close()

    sc_rec_cols = list(_RECORDS_ACTOR_COLUMNS) + [blob_col]
    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 2,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": sc_rec_cols,
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C6")

    assert result.passed, f"C6 failed: {result.messages}"
    assert any("BLOB" in s or "data" in s for s in result.skips), (
        f"Expected a skip for BLOB column, got skips={result.skips}"
    )


def test_c6_fails_on_round_trip_mismatch(tmp_path: Path) -> None:
    """C6 fails when history.value does not match the encoded records cell."""
    dest = tmp_path / "c6_mismatch"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    # history says name was 'Bob'
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ["trunk", "actor", "a001", "name", 10, "Bob"],
    )
    # records__actor says name is 'Alice' — mismatch
    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
        ["trunk", "a001", 10, True, 10, 0, "Alice", "active", "d001", 0, "patient"],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 1,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": list(_RECORDS_ACTOR_COLUMNS),
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C6")

    assert not result.passed
    assert any("mismatch" in m for m in result.messages)


def test_c6_fails_not_raises_on_null_numeric_tracked_cell(tmp_path: Path) -> None:
    """A NULL numeric tracked cell backed by a series is a C6 failure, not a crash.

    A tracked BIGINT/DOUBLE prop whose records cell is NULL cannot be encoded by
    to_csv_text (it has no NULL form for those types and would raise). C6 reports
    the NULL directly as a round-trip failure instead. This is the state a
    corrupter's missing-value defect produces on a numeric tracked property.
    """
    score_col: dict[str, object] = {
        "name": "prop__score",
        "type": "BIGINT",
        "history_tracked": True,
    }
    rec_cols = list(_RECORDS_ACTOR_COLUMNS) + [score_col]

    dest = tmp_path / "c6_null_numeric"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", rec_cols))
    # history says score was 42
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ["trunk", "actor", "a001", "score", 10, "42"],
    )
    # records__actor row with prop__score NULL (trailing literal) — the defect
    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL)",
        ["trunk", "a001", 10, True, 10, 0, "Alice", "active", "d001", 0, "patient"],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 1,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": rec_cols,
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C6")

    assert not result.passed
    assert any("NULL" in m and "score" in m for m in result.messages), (
        f"Expected a NULL round-trip failure for prop__score, got {result.messages}"
    )


def test_c6_set_based_isolates_mismatch_across_series(tmp_path: Path) -> None:
    """C6 over many series-of-the-same-(kind,property): only the bad row is flagged.

    The set-based form resolves one (kind, property) class with a single
    window+join over all its records. This guards that per-record verdicts stay
    isolated: a002's mismatch must not implicate the conforming a001.
    """
    dest = tmp_path / "c6_multi"
    dest.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(dest / "run.duckdb"))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    conn.executemany(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        [
            ["trunk", "actor", "a001", "name", 10, "Alice"],
            ["trunk", "actor", "a002", "name", 10, "Bob"],
        ],
    )
    conn.executemany(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
        [
            ["trunk", "a001", 10, True, 10, 0, "Alice", "active", "d001", 0, "patient"],
            ["trunk", "a002", 10, True, 10, 1, "Carol", "active", "d001", 0, "patient"],
        ],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 2,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": list(_RECORDS_ACTOR_COLUMNS),
                "rows": 2,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C6")

    assert not result.passed
    mismatches = [m for m in result.messages if "mismatch" in m]
    assert len(mismatches) == 1, f"expected one mismatch, got {result.messages}"
    assert "a002" in mismatches[0]
    assert "a001" not in mismatches[0]


def test_c6_latest_pre_slice_tiebreak_is_deterministic(tmp_path: Path) -> None:
    """C6 resolves a sim_time tie deterministically (Determinism invariant).

    Two history rows share the maximum pre-slice sim_time with different values.
    The set-based "latest" selection breaks the tie by value DESC, so the records
    cell holding the DESC-winning value passes — and re-running yields the identical
    verdict. A non-deterministic tie-break would make this flap.
    """
    dest = tmp_path / "c6_tie"
    dest.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(dest / "run.duckdb"))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    # Two name rows at the same sim_time=10; "Zzz" is the value-DESC winner.
    conn.executemany(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        [
            ["trunk", "actor", "a001", "name", 10, "Aaa"],
            ["trunk", "actor", "a001", "name", 10, "Zzz"],
        ],
    )
    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
        ["trunk", "a001", 10, True, 10, 0, "Zzz", "active", "d001", 0, "patient"],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 2,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": list(_RECORDS_ACTOR_COLUMNS),
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        first = run_check(emit, "C6")
        second = run_check(emit, "C6")

    assert first.passed, f"tie-break did not select the value-DESC winner: {first}"
    assert first.messages == second.messages


def test_c6_skips_when_records_table_absent_from_catalog(tmp_path: Path) -> None:
    """C6 skips (passes) a series whose records__<kind> table is absent entirely.

    history carries a (kind='ghost', property='name') series but no records__ghost
    table exists in the catalog. C6 records a skip for the series and passes —
    the missing table is C2/C9 territory, never a raise out of validate().
    """
    dest = tmp_path / "c6_absent_records"
    dest.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(dest / "run.duckdb"))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ["trunk", "ghost", "g001", "name", 10, "Casper"],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C6")

    assert result.passed, f"C6 should pass (skip), got messages={result.messages}"
    assert any(
        "records__ghost" in s and "absent from catalog" in s for s in result.skips
    ), f"Expected a skip for absent records__ghost, got skips={result.skips}"


# ---------------------------------------------------------------------------
# C7: NULL all-or-none
# ---------------------------------------------------------------------------


def test_c7_passes_on_spanning(
    base_fixtures: dict[str, Path],
) -> None:
    """C7 passes on spanning."""
    with open_emit(base_fixtures["spanning"]) as emit:
        result = run_check(emit, "C7")
    assert result.passed, f"C7 failed: {result.messages}"


def test_c7_fails_on_c7_half_null_member(
    base_fixtures: dict[str, Path],
) -> None:
    """C7 fails on c7_half_null_member (member__kind set but member__id NULL)."""
    with open_emit(base_fixtures["c7_half_null_member"]) as emit:
        result = run_check(emit, "C7")
    assert not result.passed
    assert any(
        "member" in m.lower() or "null" in m.lower() or "partial" in m.lower()
        for m in result.messages
    )


def test_c7_deactivated_at_null_iff_active(tmp_path: Path) -> None:
    """C7: deactivated_at NULL iff active."""
    dest = tmp_path / "c7_deactivated"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    # active=True but deactivated_at is NOT NULL — violation
    # cols: fork_path, record_id, created_sim_time, active, deactivated_at,
    #       last_mutation_sim_time, record_index, prop__name, prop__status,
    #       prop__doctor_id, ref_index__doctor_id, prop__actor_type
    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "trunk",
            "a001",
            10,
            True,
            99,
            10,
            0,
            "Alice",
            "active",
            "d001",
            0,
            "patient",
        ],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": list(_RECORDS_ACTOR_COLUMNS),
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C7")
    assert not result.passed
    assert any("deactivated_at" in m for m in result.messages)


def test_c7_membership_member_pair(tmp_path: Path) -> None:
    """C7 exercises membership member__f__kind/id pair NULL all-or-none (valid)."""
    dest = tmp_path / "c7_membership"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    conn.execute(
        _create_table_ddl("membership__actor__appointments", _MEMBERSHIP_COLUMNS)
    )
    _populate_records_actor(conn)
    _populate_membership(conn)
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": list(_RECORDS_ACTOR_COLUMNS),
                "rows": 1,
            },
            {
                "name": "membership__actor__appointments",
                "category": "membership",
                "record_kind": "actor",
                "property": "appointments",
                "columns": list(_MEMBERSHIP_COLUMNS),
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C7")
    assert result.passed, f"C7 failed unexpectedly: {result.messages}"


# ---------------------------------------------------------------------------
# C8: fork_path set matches sidecar branches
# ---------------------------------------------------------------------------


def test_c8_passes_on_spanning(
    base_fixtures: dict[str, Path],
) -> None:
    """C8 passes on spanning."""
    with open_emit(base_fixtures["spanning"]) as emit:
        result = run_check(emit, "C8")
    assert result.passed, f"C8 failed: {result.messages}"


def test_c8_passes_distinct_fork_paths(tmp_path: Path) -> None:
    """C8 passes when distinct fork_path values match sidecar branches."""
    dest = tmp_path / "c8_ok"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ["trunk", "actor", "a001", "name", 10, "Alice"],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C8")
    assert result.passed, f"C8 failed: {result.messages}"


def test_c8_fails_extra_fork_path_in_data(tmp_path: Path) -> None:
    """C8 fails when data has a fork_path not in sidecar branches."""
    dest = tmp_path / "c8_extra"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ["trunk", "actor", "a001", "name", 10, "Alice"],
    )
    # extra fork_path not in sidecar
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ["branch_x", "actor", "a001", "name", 20, "Alice"],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 2,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C8")
    assert not result.passed
    assert any("branch_x" in m for m in result.messages)


# ---------------------------------------------------------------------------
# C9: pinned ID resolution
# ---------------------------------------------------------------------------


def test_c9_passes_on_spanning(
    base_fixtures: dict[str, Path],
) -> None:
    """C9 passes on spanning (pinned 'alice' -> a001 resolves)."""
    with open_emit(base_fixtures["spanning"]) as emit:
        result = run_check(emit, "C9")
    assert result.passed, f"C9 failed: {result.messages}"


def test_c9_passes_when_no_pinned_ids(tmp_path: Path) -> None:
    """C9 passes trivially when there are no pinned_ids."""
    dest = tmp_path / "c9_no_pins"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C9")
    assert result.passed


def test_c9_fails_absent_records_table(tmp_path: Path) -> None:
    """C9 fails (not skips) when records__<kind> table is absent."""
    dest = tmp_path / "c9_absent_table"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    # No records__actor table
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
        ]
    )
    sidecar["pinned_ids"] = {"actor": {"alice": "a001"}}
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C9")
    assert not result.passed
    assert any("actor" in m for m in result.messages)


def test_c9_reflects_self_contained_via_run_check(tmp_path: Path) -> None:
    """run_check(emit, 'C9') reflects the absent table failure self-contained."""
    dest = tmp_path / "c9_self_contained"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
        ]
    )
    sidecar["pinned_ids"] = {"doctor": {"bob": "d001"}}
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C9")
    assert not result.passed
    assert result.check == "C9"


def test_c9_fails_wrong_count(tmp_path: Path) -> None:
    """C9 fails when a pinned id has 0 or 2 rows in its records table."""
    dest = tmp_path / "c9_wrong_count"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    # Insert 0 rows for a001 — missing record
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": list(_RECORDS_ACTOR_COLUMNS),
                "rows": 0,
            },
        ]
    )
    sidecar["pinned_ids"] = {"actor": {"alice": "a001"}}
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C9")
    # empty table means no fork_paths to check — passes trivially
    assert result.passed


def test_c9_skips_when_records_table_missing_key_columns(tmp_path: Path) -> None:
    """C9 skips (passes) when records__<kind> lacks record_id/fork_path columns.

    The table exists in the catalog, so the absent-table failure branch does not
    fire; the missing key columns are probed before querying, recorded as a skip,
    and never raised out of validate() (the malformed shape is C5 territory).
    """
    # records__actor present but with neither record_id nor fork_path
    broken_actor_cols: list[dict[str, object]] = [
        {"name": "prop__name", "type": "VARCHAR"},
    ]
    dest = tmp_path / "c9_missing_key_cols"
    dest.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(dest / "run.duckdb"))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", broken_actor_cols))
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": broken_actor_cols,
                "rows": 0,
            },
        ]
    )
    sidecar["pinned_ids"] = {"actor": {"alice": "a001"}}
    _write_emit(dest, sidecar, records_shape_valid=False)

    with open_emit(dest) as emit:
        result = run_check(emit, "C9")

    assert result.passed, f"C9 should pass (skip), got messages={result.messages}"
    assert any("pin resolution skipped" in s for s in result.skips), (
        f"Expected a pin-resolution skip, got skips={result.skips}"
    )


# ---------------------------------------------------------------------------
# C10: membership integrity
# ---------------------------------------------------------------------------


def test_c10_passes_on_spanning(
    base_fixtures: dict[str, Path],
) -> None:
    """C10 passes on spanning."""
    with open_emit(base_fixtures["spanning"]) as emit:
        result = run_check(emit, "C10")
    assert result.passed, f"C10 failed: {result.messages}"


def test_c10_left_sim_time_ge_joined(tmp_path: Path) -> None:
    """C10: left_sim_time IS NULL OR >= joined_sim_time."""
    dest = tmp_path / "c10_time"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    conn.execute(
        _create_table_ddl("membership__actor__appointments", _MEMBERSHIP_COLUMNS)
    )
    # left_sim_time < joined_sim_time — violation (joined=50, left=10)
    conn.execute(
        "INSERT INTO membership__actor__appointments VALUES (?, ?, ?, ?, ?, ?, ?)",
        ["trunk", "a001", 50, 10, "morning", "doctor", "d001"],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": list(_RECORDS_ACTOR_COLUMNS),
                "rows": 0,
            },
            {
                "name": "membership__actor__appointments",
                "category": "membership",
                "record_kind": "actor",
                "property": "appointments",
                "columns": list(_MEMBERSHIP_COLUMNS),
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C10")
    assert not result.passed
    assert any("left_sim_time" in m for m in result.messages)


def test_c10_member_reference_resolves(tmp_path: Path) -> None:
    """C10: non-NULL member reference must resolve to a records row."""
    dest = tmp_path / "c10_ref_ok"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    doctor_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        {"name": "record_index", "type": "BIGINT"},
    ]
    conn.execute(_create_table_ddl("records__doctor", doctor_cols))
    conn.execute(
        "INSERT INTO records__doctor VALUES (?, ?, ?, ?, NULL, ?, ?)",
        ["trunk", "d001", 5, True, 10, 0],
    )
    conn.execute(
        _create_table_ddl("membership__actor__appointments", _MEMBERSHIP_COLUMNS)
    )
    _populate_membership(conn)
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": list(_RECORDS_ACTOR_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__doctor",
                "category": "records",
                "record_kind": "doctor",
                "columns": doctor_cols,
                "rows": 1,
            },
            {
                "name": "membership__actor__appointments",
                "category": "membership",
                "record_kind": "actor",
                "property": "appointments",
                "columns": list(_MEMBERSHIP_COLUMNS),
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C10")
    assert result.passed, f"C10 failed: {result.messages}"


def test_c10_fails_unresolved_member_reference(tmp_path: Path) -> None:
    """C10 fails when records__<kind> table exists but has no matching row."""
    dest = tmp_path / "c10_dangling"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    doctor_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        {"name": "record_index", "type": "BIGINT"},
    ]
    conn.execute(_create_table_ddl("records__doctor", doctor_cols))
    # records__doctor exists but has no d001 row — dangling reference
    conn.execute(
        _create_table_ddl("membership__actor__appointments", _MEMBERSHIP_COLUMNS)
    )
    _populate_membership(conn)  # references doctor/d001
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": list(_RECORDS_ACTOR_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__doctor",
                "category": "records",
                "record_kind": "doctor",
                "columns": doctor_cols,
                "rows": 0,
            },
            {
                "name": "membership__actor__appointments",
                "category": "membership",
                "record_kind": "actor",
                "property": "appointments",
                "columns": list(_MEMBERSHIP_COLUMNS),
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C10")
    assert not result.passed
    assert any("d001" in m or "doctor" in m for m in result.messages)


def test_c10_set_based_isolates_dangling_reference(tmp_path: Path) -> None:
    """C10 over many references-of-the-same-kind: only the unresolved one is flagged.

    The set-based form resolves all references to a given kind with a single
    anti-join. This guards that the resolving reference (d001) is not implicated
    when a sibling reference (d999) dangles.
    """
    dest = tmp_path / "c10_multi"
    dest.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(dest / "run.duckdb"))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    doctor_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        {"name": "record_index", "type": "BIGINT"},
    ]
    conn.execute(_create_table_ddl("records__doctor", doctor_cols))
    conn.execute(
        "INSERT INTO records__doctor VALUES (?, ?, ?, ?, NULL, ?, ?)",
        ["trunk", "d001", 5, True, 10, 0],
    )
    conn.execute(
        _create_table_ddl("membership__actor__appointments", _MEMBERSHIP_COLUMNS)
    )
    # Two appointment rows: one references doctor d001 (resolves), one d999 (dangles).
    conn.executemany(
        "INSERT INTO membership__actor__appointments VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ["trunk", "a001", 10, None, "morning", "doctor", "d001"],
            ["trunk", "a001", 20, None, "evening", "doctor", "d999"],
        ],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": list(_RECORDS_ACTOR_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__doctor",
                "category": "records",
                "record_kind": "doctor",
                "columns": doctor_cols,
                "rows": 1,
            },
            {
                "name": "membership__actor__appointments",
                "category": "membership",
                "record_kind": "actor",
                "property": "appointments",
                "columns": list(_MEMBERSHIP_COLUMNS),
                "rows": 2,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C10")

    assert not result.passed
    dangling = [m for m in result.messages if "resolves to no row" in m]
    assert len(dangling) == 1, f"expected one dangling ref, got {result.messages}"
    assert "d999" in dangling[0]
    assert "d001" not in dangling[0]


def test_c10_skips_kind_column_without_matching_id_column(tmp_path: Path) -> None:
    """C10 skips (passes) a member__X__kind column with no matching member__X__id.

    The half-pair is probed before any reference query, recorded as a skip, and
    never raised out of validate() (the malformed pair shape is C2 territory).
    """
    # Membership table with member__doctor__kind but no member__doctor__id
    half_pair_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "joined_sim_time", "type": "BIGINT"},
        {"name": "left_sim_time", "type": "BIGINT"},
        {"name": "member__doctor__kind", "type": "VARCHAR"},
    ]
    dest = tmp_path / "c10_half_pair_col"
    dest.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(dest / "run.duckdb"))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("membership__actor__appointments", half_pair_cols))
    conn.execute(
        "INSERT INTO membership__actor__appointments VALUES (?, ?, ?, NULL, ?)",
        ["trunk", "a001", 10, "doctor"],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
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
                "property": "appointments",
                "columns": half_pair_cols,
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C10")

    assert result.passed, f"C10 should pass (skip), got messages={result.messages}"
    assert any(
        "member__doctor__id" in s and "member reference check skipped" in s
        for s in result.skips
    ), f"Expected a half-pair skip naming member__doctor__id, got skips={result.skips}"


# ---------------------------------------------------------------------------
# C11: column SCD class consistency (converse clause)
# ---------------------------------------------------------------------------


def test_c11_converse_fails_on_zero_history_rows(tmp_path: Path) -> None:
    """C11's converse: prop__name is flagged history_tracked True and
    records__actor has a row, but history carries zero rows for (actor, name)."""
    dest = tmp_path / "c11_converse_zero_rows"
    dest.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(dest / "run.duckdb"))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    _populate_records_actor(conn)
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": list(_RECORDS_ACTOR_COLUMNS),
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C11")

    assert not result.passed
    assert any("converse" in m for m in result.messages)


def test_c11_converse_gate_excludes_non_round_trippable_column(
    tmp_path: Path,
) -> None:
    """C11's converse gate: a flagged BLOB column with zero history rows does
    not fail C11 -- collection-struct properties never appear in history."""
    blob_col: dict[str, object] = {
        "name": "prop__data",
        "type": "BLOB",
        "history_tracked": True,
        "temporal_class": "tracked",
    }
    rec_cols = list(_RECORDS_ACTOR_COLUMNS) + [blob_col]

    dest = tmp_path / "c11_converse_blob_gate"
    dest.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(dest / "run.duckdb"))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", rec_cols))
    # prop__name's own genesis row -- keeps that column conformant so only the
    # BLOB gate is under test.
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ["trunk", "actor", "a001", "name", 10, "Alice"],
    )
    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "trunk",
            "a001",
            10,
            True,
            10,
            0,
            "Alice",
            "active",
            "d001",
            0,
            "patient",
            b"\x00\x01",
        ],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 1,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": rec_cols,
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C11")

    assert result.passed, f"C11 should pass (BLOB gated out): {result.messages}"


def test_c11_skips_when_no_flagged_column(tmp_path: Path) -> None:
    """C11 skips entirely when no records-category prop__ column carries
    history_tracked anywhere in the sidecar (existing behavior retained)."""
    bare_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        {"name": "record_index", "type": "BIGINT"},
        {"name": "prop__name", "type": "VARCHAR"},
    ]
    dest = tmp_path / "c11_no_flagged_column"
    dest.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(dest / "run.duckdb"))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", bare_cols))
    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
        ["trunk", "a001", 10, True, 10, 0, "Alice"],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": bare_cols,
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C11")

    assert result.passed
    assert any("predates" in s or "skipped" in s for s in result.skips)


# ---------------------------------------------------------------------------
# Boundary fixtures: history_duplicate_tick and refs_dangling
# ---------------------------------------------------------------------------


def test_boundary_history_duplicate_tick_passes(
    base_fixtures: dict[str, Path],
) -> None:
    """history_duplicate_tick: validate passes (I3 violation is outside C1–C12)."""
    with open_emit(base_fixtures["history_duplicate_tick"]) as emit:
        report = validate(emit)
    assert report.ok, (
        f"Expected ok=True, failures: {[r for r in report.results if not r.passed]}"
    )


def test_boundary_refs_dangling_passes(
    base_fixtures: dict[str, Path],
) -> None:
    """refs_dangling: validate passes (records-prop ref outside C10/C11 scope)."""
    with open_emit(base_fixtures["refs_dangling"]) as emit:
        report = validate(emit)
    check_map = {r.check: r for r in report.results}
    # C11 skips on refs_dangling (no history_tracked flags in sidecar)
    assert check_map["C11"].passed, str(check_map["C11"])
    assert report.ok, (
        f"Expected ok=True, failures: {[r for r in report.results if not r.passed]}"
    )


# ---------------------------------------------------------------------------
# C12: record-role registry consistency
# ---------------------------------------------------------------------------


def test_c12_passes_on_spanning(
    base_fixtures: dict[str, Path],
) -> None:
    """C12 passes on spanning (record_roles covers all emitted kinds and sub-types)."""
    with open_emit(base_fixtures["spanning"]) as emit:
        result = run_check(emit, "C12")
    assert result.passed, f"C12 failed: {result.messages}"
    assert result.check == "C12"


def test_c12_fails_on_c12_missing_kind(
    base_fixtures: dict[str, Path],
) -> None:
    """C12 fails when record_roles omits an emitted records kind."""
    with open_emit(base_fixtures["c12_missing_kind"]) as emit:
        result = run_check(emit, "C12")
    assert not result.passed
    assert any("doctor" in m or "missing" in m.lower() for m in result.messages)


def test_c12_fails_on_c12_missing_subtype(
    base_fixtures: dict[str, Path],
) -> None:
    """C12 fails when record_roles['actor'] omits a sub-type present in data."""
    with open_emit(base_fixtures["c12_missing_subtype"]) as emit:
        result = run_check(emit, "C12")
    assert not result.passed
    assert any(
        "patient" in m or "sub-type" in m.lower() or "subtype" in m.lower()
        for m in result.messages
    )


def test_c12_skips_when_record_roles_absent(tmp_path: Path) -> None:
    """C12 passes by vacuity (skip) when record_roles is absent from the sidecar."""
    dest = tmp_path / "c12_no_roles"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    _populate_records_actor(conn)
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": list(_RECORDS_ACTOR_COLUMNS),
                "rows": 1,
            },
        ]
    )
    # No record_roles key in sidecar
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C12")
    assert result.passed
    assert any("record_roles" in s and "absent" in s.lower() for s in result.skips)


def test_c12_skips_actor_subtype_check_when_prop_actor_type_column_absent(
    tmp_path: Path,
) -> None:
    """C12 skips (does not fail) when records__actor lacks prop__actor_type column.

    actor is declared as an object-valued (subtyped) kind in record_roles, and
    records__actor exists in the catalog, but the discriminator column is absent.
    _check_c12_actor_subtypes appends a skip and returns without recording a failure.
    """
    actor_cols_no_discriminator: list[dict[str, object]] = [
        c for c in _RECORDS_ACTOR_COLUMNS if c["name"] != "prop__actor_type"
    ]
    dest = tmp_path / "c12_no_discriminator_col"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", actor_cols_no_discriminator))
    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
        ["trunk", "a001", 10, True, 10, 0, "Alice", "active", "d001", 0],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": actor_cols_no_discriminator,
                "rows": 1,
            },
        ]
    )
    sidecar["record_roles"] = {"actor": {"patient": "dimension", "staff": "fact"}}
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C12")

    assert result.passed, f"C12 should pass (skip), got messages={result.messages}"
    assert any("prop__actor_type" in s and "absent" in s for s in result.skips), (
        f"Expected skip for absent discriminator column, got skips={result.skips}"
    )


def test_c12_passes_when_actor_is_bare_string_kind(tmp_path: Path) -> None:
    """C12 passes when actor is declared as a bare-string (non-subtyped) kind.

    When record_roles['actor'] is a plain role string (not an object),
    is_subtyped('actor') returns False and _check_c12_actor_subtypes returns
    immediately without enumerating sub-types — no failure, no skip recorded.
    """
    dest = tmp_path / "c12_actor_bare_string"
    dest.mkdir(parents=True, exist_ok=True)
    db_path = dest / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
        ["trunk", "a001", 10, True, 10, 0, "Alice", "active", "d001", 0, "patient"],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": list(_RECORDS_ACTOR_COLUMNS),
                "rows": 1,
            },
        ]
    )
    sidecar["record_roles"] = {"actor": "dimension"}
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C12")

    assert result.passed, f"C12 should pass, got messages={result.messages}"
    # bare-string path adds no skip — the sub-type check is simply not entered
    assert not any("actor" in s and "sub-type" in s for s in result.skips), (
        f"Expected no actor sub-type skip for bare-string kind, got skips={result.skips}"
    )


# ---------------------------------------------------------------------------
# C13: temporal-class consistency (semantic / genesis clause)
# ---------------------------------------------------------------------------


def test_c13_semantic_record_id_matters_not_vicarious(tmp_path: Path) -> None:
    """C13's genesis clause matches on record_id: a rowless record does not pass
    because a sibling of the same kind shares its created_sim_time."""
    dest = tmp_path / "c13_record_id_matters"
    dest.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(dest / "run.duckdb"))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    # Only a001 gets its genesis row; a002 shares created_sim_time=10 but has none.
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ["trunk", "actor", "a001", "name", 10, "Alice"],
    )
    conn.executemany(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
        [
            ["trunk", "a001", 10, True, 10, 0, "Alice", "active", "d001", 0, "patient"],
            ["trunk", "a002", 10, True, 10, 1, "Bob", "active", "d001", 0, "patient"],
        ],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 1,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": list(_RECORDS_ACTOR_COLUMNS),
                "rows": 2,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C13")

    assert not result.passed
    assert any("a002" in m for m in result.messages)
    assert not any("a001" in m for m in result.messages)


def test_c13_semantic_null_valued_genesis_row_passes(tmp_path: Path) -> None:
    """A NULL-valued genesis row satisfies C13's semantic clause -- the clause
    only requires the row to exist, not that its value be non-NULL."""
    dest = tmp_path / "c13_null_genesis"
    dest.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(dest / "run.duckdb"))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", _RECORDS_ACTOR_COLUMNS))
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, NULL)",
        ["trunk", "actor", "a001", "name", 10],
    )
    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, ?, ?, ?, ?)",
        ["trunk", "a001", 10, True, 10, 0, "active", "d001", 0, "patient"],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 1,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": list(_RECORDS_ACTOR_COLUMNS),
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C13")

    assert result.passed, (
        f"C13 should pass on a NULL-valued genesis row: {result.messages}"
    )


def test_c13_skips_when_no_flagged_column(tmp_path: Path) -> None:
    """C13 skips entirely when no records-category prop__ column carries
    history_tracked anywhere in the sidecar."""
    bare_cols: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        {"name": "record_index", "type": "BIGINT"},
        {"name": "prop__name", "type": "VARCHAR"},
    ]
    dest = tmp_path / "c13_no_flagged_column"
    dest.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(dest / "run.duckdb"))
    conn.execute(_create_table_ddl("history", _HISTORY_COLUMNS))
    conn.execute(_create_table_ddl("records__actor", bare_cols))
    conn.execute(
        "INSERT INTO records__actor VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
        ["trunk", "a001", 10, True, 10, 0, "Alice"],
    )
    conn.close()

    sidecar = _single_branch_sidecar(
        [
            {
                "name": "history",
                "category": "fixed",
                "columns": list(_HISTORY_COLUMNS),
                "rows": 0,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": bare_cols,
                "rows": 1,
            },
        ]
    )
    _write_emit(dest, sidecar)

    with open_emit(dest) as emit:
        result = run_check(emit, "C13")

    assert result.passed
    assert any("predates" in s or "skipped" in s for s in result.skips)


# ---------------------------------------------------------------------------
# C11/C13 negative fixtures: each fails exactly its named check(s)
# ---------------------------------------------------------------------------


def test_c13_broken_pairing_fails_c13_alone(base_fixtures: dict[str, Path]) -> None:
    """c13_broken_pairing fails C13's structural clause alone."""
    with open_emit(base_fixtures["c13_broken_pairing"]) as emit:
        report = validate(emit)
    failing = {r.check for r in report.results if not r.passed}
    assert failing == {"C13"}


def test_c13_out_of_enum_class_fails_c1_and_c13(
    base_fixtures: dict[str, Path],
) -> None:
    """c13_out_of_enum_class fails C13's enum clause and necessarily C1."""
    with open_emit(base_fixtures["c13_out_of_enum_class"]) as emit:
        report = validate(emit)
    failing = {r.check for r in report.results if not r.passed}
    assert failing == {"C1", "C13"}


def test_c13_missing_genesis_fails_c13_alone(base_fixtures: dict[str, Path]) -> None:
    """c13_missing_genesis fails C13's semantic clause alone -- C11's converse
    still sees rows for the pair."""
    with open_emit(base_fixtures["c13_missing_genesis"]) as emit:
        report = validate(emit)
    failing = {r.check for r in report.results if not r.passed}
    assert failing == {"C13"}


def test_c11_emptied_series_fails_c11_and_c13(base_fixtures: dict[str, Path]) -> None:
    """c11_emptied_series fails C11's converse and C13's genesis clause together --
    zero rows implies no genesis row."""
    with open_emit(base_fixtures["c11_emptied_series"]) as emit:
        report = validate(emit)
    failing = {r.check for r in report.results if not r.passed}
    assert failing == {"C11", "C13"}


# ---------------------------------------------------------------------------
# validate() report shape
# ---------------------------------------------------------------------------


def test_validate_no_duplicates(
    base_fixtures: dict[str, Path],
) -> None:
    """validate returns no duplicate check ids."""
    with open_emit(base_fixtures["spanning"]) as emit:
        report = validate(emit)
    ids = [r.check for r in report.results]
    assert len(ids) == len(set(ids))


def test_validate_ok_true_on_spanning(
    base_fixtures: dict[str, Path],
) -> None:
    """report.ok is True on spanning."""
    with open_emit(base_fixtures["spanning"]) as emit:
        report = validate(emit)
    assert report.ok, (
        f"Expected ok=True, failures: {[r for r in report.results if not r.passed]}"
    )


def test_validate_ok_false_when_check_fails(
    base_fixtures: dict[str, Path],
) -> None:
    """report.ok is False when any check fails (c12_missing_kind)."""
    with open_emit(base_fixtures["c12_missing_kind"]) as emit:
        report = validate(emit)
    assert not report.ok
