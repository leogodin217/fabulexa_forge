"""Tests for ColumnSpec.history_tracked, Sidecar.history_tracked_available, and C11."""

from __future__ import annotations

from pathlib import Path

import duckdb
from _support.sidecar_builder import identity_column

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.reader.emit import open_emit

from ._emit_helpers import write_emit

# The records-table positional prefix through record_index (fork_path,
# record_id, lifecycle tail, record_index) -- kind-independent, so it is
# reused verbatim for records__patient and records__nurse alike.
_RECORDS_PATIENT_STATUS_DDL = (
    "CREATE TABLE records__patient "
    "(fork_path VARCHAR, record_id VARCHAR, created_sim_time BIGINT, "
    "active BOOLEAN, deactivated_at BIGINT, last_mutation_sim_time BIGINT, "
    "record_index BIGINT, prop__status VARCHAR)"
)

_RECORDS_NURSE_NAME_DDL = (
    "CREATE TABLE records__nurse "
    "(fork_path VARCHAR, record_id VARCHAR, created_sim_time BIGINT, "
    "active BOOLEAN, deactivated_at BIGINT, last_mutation_sim_time BIGINT, "
    "record_index BIGINT, prop__name VARCHAR)"
)

_RECORDS_PATIENT_ZPROP_DDL = (
    "CREATE TABLE records__patient "
    "(fork_path VARCHAR, record_id VARCHAR, created_sim_time BIGINT, "
    "active BOOLEAN, deactivated_at BIGINT, last_mutation_sim_time BIGINT, "
    "record_index BIGINT, prop__z_prop VARCHAR)"
)


def _records_prefix() -> list[dict[str, object]]:
    """The records-table positional prefix through record_index."""
    return [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
    ]


def _sidecar_with_history_tracked(with_flag: bool) -> dict[str, object]:
    """Build a sidecar whose records__patient table carries or omits history_tracked."""
    prop_col: dict[str, object] = {
        "name": "prop__status",
        "type": "VARCHAR",
    }
    if with_flag:
        prop_col["history_tracked"] = True

    return {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [
            {
                "name": "firings",
                "category": "fixed",
                "columns": [
                    {"name": "fork_path", "type": "VARCHAR"},
                    {"name": "sim_time", "type": "BIGINT"},
                ],
                "rows": 0,
            },
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": [*_records_prefix(), prop_col],
                "rows": 0,
            },
        ],
    }


def test_column_spec_history_tracked_true(tmp_path: Path) -> None:
    """ColumnSpec.history_tracked is True when the column declares history_tracked: true."""
    sidecar = _sidecar_with_history_tracked(with_flag=True)
    emit_dir = write_emit(tmp_path, sidecar=sidecar)
    with open_emit(emit_dir) as emit:
        cols = emit.sidecar.columns("records__patient")
        prop_col = next(c for c in cols if c.name == "prop__status")
        assert prop_col.history_tracked is True


def test_column_spec_history_tracked_false(tmp_path: Path) -> None:
    """ColumnSpec.history_tracked is False when the column declares history_tracked: false."""
    sidecar = _sidecar_with_history_tracked(with_flag=True)
    # Overwrite the prop_col to use False
    records_table = sidecar["tables"][1]  # type: ignore[index]
    prop_col = records_table["columns"][-1]  # type: ignore[index]
    prop_col["history_tracked"] = False  # type: ignore[index]
    emit_dir = write_emit(tmp_path, sidecar=sidecar)
    with open_emit(emit_dir) as emit:
        cols = emit.sidecar.columns("records__patient")
        prop_col_spec = next(c for c in cols if c.name == "prop__status")
        assert prop_col_spec.history_tracked is False


def test_column_spec_history_tracked_none_when_absent(tmp_path: Path) -> None:
    """ColumnSpec.history_tracked is None when the column does not declare the field."""
    sidecar = _sidecar_with_history_tracked(with_flag=False)
    emit_dir = write_emit(tmp_path, sidecar=sidecar)
    with open_emit(emit_dir) as emit:
        cols = emit.sidecar.columns("records__patient")
        prop_col = next(c for c in cols if c.name == "prop__status")
        assert prop_col.history_tracked is None


def test_history_tracked_available_true_when_any_column_has_flag(
    tmp_path: Path,
) -> None:
    """Sidecar.history_tracked_available() returns True when any column carries the flag."""
    sidecar = _sidecar_with_history_tracked(with_flag=True)
    emit_dir = write_emit(tmp_path, sidecar=sidecar)
    with open_emit(emit_dir) as emit:
        assert emit.sidecar.history_tracked_available() is True


def test_history_tracked_available_false_when_no_column_has_flag(
    tmp_path: Path,
) -> None:
    """Sidecar.history_tracked_available() returns False when no column carries the flag."""
    sidecar = _sidecar_with_history_tracked(with_flag=False)
    emit_dir = write_emit(tmp_path, sidecar=sidecar)
    with open_emit(emit_dir) as emit:
        assert emit.sidecar.history_tracked_available() is False


def test_history_tracked_passes_validate_c1(tmp_path: Path) -> None:
    """A sidecar with history_tracked columns passes C1 conformance (re-vendored schema)."""
    from fabulexa_forge.reader.conformance import validate

    sidecar = _sidecar_with_history_tracked(with_flag=True)
    emit_dir = write_emit(
        tmp_path,
        sidecar=sidecar,
        db_tables={
            "firings": "CREATE TABLE firings (fork_path VARCHAR, sim_time BIGINT)",
            "records__patient": _RECORDS_PATIENT_STATUS_DDL,
        },
    )
    with open_emit(emit_dir) as emit:
        report = validate(emit)
    check_map = {r.check: r for r in report.results}
    assert check_map["C1"].passed, str(check_map["C1"].messages)


# ---------------------------------------------------------------------------
# C11: Column SCD class consistency
# ---------------------------------------------------------------------------


def _build_c11_sidecar(
    prop_history_tracked: bool | None,
    include_history_table: bool = True,
) -> dict[str, object]:
    """Build a sidecar for C11 testing.

    Args:
        prop_history_tracked: The history_tracked value for prop__status.
            None means omit the field (predates the attribute).
        include_history_table: Whether to include the history table in the sidecar.
    """
    prop_col: dict[str, object] = {
        "name": "prop__status",
        "type": "VARCHAR",
    }
    if prop_history_tracked is not None:
        prop_col["history_tracked"] = prop_history_tracked

    tables: list[dict[str, object]] = [
        {
            "name": "firings",
            "category": "fixed",
            "columns": [
                {"name": "fork_path", "type": "VARCHAR"},
                {"name": "sim_time", "type": "BIGINT"},
            ],
            "rows": 0,
        },
        {
            "name": "records__patient",
            "category": "records",
            "record_kind": "patient",
            "columns": [*_records_prefix(), prop_col],
            "rows": 0,
        },
    ]
    if include_history_table:
        tables.append(
            {
                "name": "history",
                "category": "fixed",
                "columns": [
                    {"name": "fork_path", "type": "VARCHAR"},
                    {"name": "kind", "type": "VARCHAR"},
                    {"name": "record_id", "type": "VARCHAR"},
                    {"name": "property", "type": "VARCHAR"},
                    {"name": "sim_time", "type": "BIGINT"},
                    {"name": "value", "type": "VARCHAR"},
                ],
                "rows": 0,
            }
        )
    return {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": tables,
    }


def _insert_history_row(db_path: Path, kind: str, prop: str, value: str) -> None:
    """Insert a history row for (kind, prop) into run.duckdb."""
    conn = duckdb.connect(str(db_path))
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ["trunk", kind, "r001", prop, 10, value],
    )
    conn.close()


def test_c11_positive_tracked_col_with_history_row_passes(tmp_path: Path) -> None:
    """C11 passes when history row (kind, property) has prop__ col with history_tracked=True."""
    from fabulexa_forge.reader.conformance import run_check

    sidecar = _build_c11_sidecar(prop_history_tracked=True)
    emit_dir = write_emit(
        tmp_path,
        sidecar=sidecar,
        db_tables={
            "firings": "CREATE TABLE firings (fork_path VARCHAR, sim_time BIGINT)",
            "records__patient": _RECORDS_PATIENT_STATUS_DDL,
            "history": (
                "CREATE TABLE history "
                "(fork_path VARCHAR, kind VARCHAR, record_id VARCHAR, "
                "property VARCHAR, sim_time BIGINT, value VARCHAR)"
            ),
        },
    )
    # Insert a history row for (patient, status) → prop__status must have history_tracked=True
    _insert_history_row(emit_dir / "run.duckdb", "patient", "status", "active")

    with open_emit(emit_dir) as emit:
        result = run_check(emit, "C11")
    assert result.passed, f"C11 should pass; messages={result.messages}"
    assert result.messages == ()


def test_c11_skip_no_history_tracked_flags(tmp_path: Path) -> None:
    """C11 skips when no records-category prop__ column carries history_tracked."""
    from fabulexa_forge.reader.conformance import run_check

    # prop_history_tracked=None → omit the field → predates the attribute → skip
    sidecar = _build_c11_sidecar(prop_history_tracked=None)
    emit_dir = write_emit(
        tmp_path,
        sidecar=sidecar,
        db_tables={
            "firings": "CREATE TABLE firings (fork_path VARCHAR, sim_time BIGINT)",
            "records__patient": _RECORDS_PATIENT_STATUS_DDL,
            "history": (
                "CREATE TABLE history "
                "(fork_path VARCHAR, kind VARCHAR, record_id VARCHAR, "
                "property VARCHAR, sim_time BIGINT, value VARCHAR)"
            ),
        },
    )
    # Even with history rows, C11 skips because no prop__ column has history_tracked set
    _insert_history_row(emit_dir / "run.duckdb", "patient", "status", "active")

    with open_emit(emit_dir) as emit:
        result = run_check(emit, "C11")
    assert result.passed, f"C11 should pass (skip); messages={result.messages}"
    assert len(result.skips) > 0


def test_c11_negative_absent_prop_column(tmp_path: Path) -> None:
    """C11 fails when history has (kind, property) but prop__ column absent from sidecar."""
    from fabulexa_forge.reader.conformance import run_check

    # Build a sidecar with prop__status tracked, but history will have a different prop
    sidecar = _build_c11_sidecar(prop_history_tracked=True)
    emit_dir = write_emit(
        tmp_path,
        sidecar=sidecar,
        db_tables={
            "firings": "CREATE TABLE firings (fork_path VARCHAR, sim_time BIGINT)",
            "records__patient": _RECORDS_PATIENT_STATUS_DDL,
            "history": (
                "CREATE TABLE history "
                "(fork_path VARCHAR, kind VARCHAR, record_id VARCHAR, "
                "property VARCHAR, sim_time BIGINT, value VARCHAR)"
            ),
        },
    )
    # Insert a history row for (patient, name) — prop__name is NOT in the sidecar
    _insert_history_row(emit_dir / "run.duckdb", "patient", "name", "Alice")

    with open_emit(emit_dir) as emit:
        result = run_check(emit, "C11")
    assert not result.passed, "C11 should fail — prop__name absent from sidecar"
    assert any("name" in m for m in result.messages), str(result.messages)


def test_c11_negative_flagged_false(tmp_path: Path) -> None:
    """C11 fails when history row references a prop__ column with history_tracked=False."""
    from fabulexa_forge.reader.conformance import run_check

    # prop__status has history_tracked=False but appears in history
    sidecar = _build_c11_sidecar(prop_history_tracked=False)
    emit_dir = write_emit(
        tmp_path,
        sidecar=sidecar,
        db_tables={
            "firings": "CREATE TABLE firings (fork_path VARCHAR, sim_time BIGINT)",
            "records__patient": _RECORDS_PATIENT_STATUS_DDL,
            "history": (
                "CREATE TABLE history "
                "(fork_path VARCHAR, kind VARCHAR, record_id VARCHAR, "
                "property VARCHAR, sim_time BIGINT, value VARCHAR)"
            ),
        },
    )
    _insert_history_row(emit_dir / "run.duckdb", "patient", "status", "active")

    with open_emit(emit_dir) as emit:
        result = run_check(emit, "C11")
    assert not result.passed, (
        "C11 should fail — history_tracked=False but history row exists"
    )
    assert any("status" in m for m in result.messages), str(result.messages)


def test_c11_one_directional_no_history_rows(tmp_path: Path) -> None:
    """C11 passes when prop__ column has history_tracked=True but zero history rows."""
    from fabulexa_forge.reader.conformance import run_check

    # tracked col + no history rows → one-directional; C11 passes
    sidecar = _build_c11_sidecar(prop_history_tracked=True)
    emit_dir = write_emit(
        tmp_path,
        sidecar=sidecar,
        db_tables={
            "firings": "CREATE TABLE firings (fork_path VARCHAR, sim_time BIGINT)",
            "records__patient": _RECORDS_PATIENT_STATUS_DDL,
            "history": (
                "CREATE TABLE history "
                "(fork_path VARCHAR, kind VARCHAR, record_id VARCHAR, "
                "property VARCHAR, sim_time BIGINT, value VARCHAR)"
            ),
        },
    )
    # No history rows inserted → C11 has nothing to check → passes

    with open_emit(emit_dir) as emit:
        result = run_check(emit, "C11")
    assert result.passed, (
        f"C11 should pass (one-directional); messages={result.messages}"
    )
    assert result.messages == ()


def test_c11_skip_when_history_table_absent(tmp_path: Path) -> None:
    """C11 records a SKIP when the history table is absent from the emit catalog.

    Even though the sidecar declares history_tracked columns, C11 cannot run
    without a history table and must SKIP (not fail) per conformance.py:1403-1407.
    """
    from fabulexa_forge.reader.conformance import run_check

    # Use include_history_table=False so neither the sidecar nor the DuckDB has history
    sidecar = _build_c11_sidecar(prop_history_tracked=True, include_history_table=False)
    emit_dir = write_emit(
        tmp_path,
        sidecar=sidecar,
        db_tables={
            "firings": "CREATE TABLE firings (fork_path VARCHAR, sim_time BIGINT)",
            "records__patient": _RECORDS_PATIENT_STATUS_DDL,
            # history table intentionally absent
        },
    )
    with open_emit(emit_dir) as emit:
        result = run_check(emit, "C11")

    # C11 must PASS (skip), not fail — no history table means no data to check
    assert result.passed, f"C11 should pass (skip); messages={result.messages}"
    # At least one skip reason must mention the history table being absent
    assert any("history" in s for s in result.skips), (
        f"Expected skip mentioning history table; skips={result.skips}"
    )


def test_c11_multiple_kinds_partial_failure(tmp_path: Path) -> None:
    """C11 fails and names the offending kind when one kind violates and another passes.

    Two kinds:
    - patient: prop__status has history_tracked=True and a matching history row → passes
    - nurse: history row references prop__score which is absent from the sidecar → fails
    C11 must fail overall and the message must name 'score' (the offending property).
    """
    from fabulexa_forge.reader.conformance import run_check

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [
            {
                "name": "firings",
                "category": "fixed",
                "columns": [
                    {"name": "fork_path", "type": "VARCHAR"},
                    {"name": "sim_time", "type": "BIGINT"},
                ],
                "rows": 0,
            },
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": [
                    *_records_prefix(),
                    # prop__status is tracked and will have a matching history row
                    {
                        "name": "prop__status",
                        "type": "VARCHAR",
                        "history_tracked": True,
                    },
                ],
                "rows": 0,
            },
            {
                "name": "records__nurse",
                "category": "records",
                "record_kind": "nurse",
                "columns": [
                    *_records_prefix(),
                    # prop__name is tracked but history will reference prop__score (absent)
                    {
                        "name": "prop__name",
                        "type": "VARCHAR",
                        "history_tracked": True,
                    },
                ],
                "rows": 0,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": [
                    {"name": "fork_path", "type": "VARCHAR"},
                    {"name": "kind", "type": "VARCHAR"},
                    {"name": "record_id", "type": "VARCHAR"},
                    {"name": "property", "type": "VARCHAR"},
                    {"name": "sim_time", "type": "BIGINT"},
                    {"name": "value", "type": "VARCHAR"},
                ],
                "rows": 0,
            },
        ],
    }
    emit_dir = write_emit(
        tmp_path,
        sidecar=sidecar,
        db_tables={
            "firings": "CREATE TABLE firings (fork_path VARCHAR, sim_time BIGINT)",
            "records__patient": _RECORDS_PATIENT_STATUS_DDL,
            "records__nurse": _RECORDS_NURSE_NAME_DDL,
            "history": (
                "CREATE TABLE history "
                "(fork_path VARCHAR, kind VARCHAR, record_id VARCHAR, "
                "property VARCHAR, sim_time BIGINT, value VARCHAR)"
            ),
        },
    )
    # patient.status passes: prop__status is in sidecar with history_tracked=True
    _insert_history_row(emit_dir / "run.duckdb", "patient", "status", "active")
    # nurse.score fails: prop__score is absent from the sidecar for records__nurse
    _insert_history_row(emit_dir / "run.duckdb", "nurse", "score", "95")

    with open_emit(emit_dir) as emit:
        result = run_check(emit, "C11")

    # C11 must fail overall — the nurse kind has a violation
    assert not result.passed, "C11 should fail — nurse.score absent from sidecar"
    # The failure message must name the offending property
    assert any("score" in m for m in result.messages), (
        f"Expected 'score' in messages; messages={result.messages}"
    )
    # The passing kind (patient) must NOT produce a message
    assert not any("status" in m for m in result.messages), (
        f"patient.status passes C11; should not appear in messages; messages={result.messages}"
    )


def test_c11_message_order_deterministic(tmp_path: Path) -> None:
    """C11 emits failure messages in sorted (kind, property) order."""
    from fabulexa_forge.reader.conformance import run_check

    # Build a sidecar with prop__status tracked but no other props
    # We'll put two history rows for different properties to test sorting
    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [
            {
                "name": "firings",
                "category": "fixed",
                "columns": [
                    {"name": "fork_path", "type": "VARCHAR"},
                    {"name": "sim_time", "type": "BIGINT"},
                ],
                "rows": 0,
            },
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": [
                    *_records_prefix(),
                    # Only prop__z_prop tracked; prop__a_prop absent
                    {
                        "name": "prop__z_prop",
                        "type": "VARCHAR",
                        "history_tracked": True,
                    },
                ],
                "rows": 0,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": [
                    {"name": "fork_path", "type": "VARCHAR"},
                    {"name": "kind", "type": "VARCHAR"},
                    {"name": "record_id", "type": "VARCHAR"},
                    {"name": "property", "type": "VARCHAR"},
                    {"name": "sim_time", "type": "BIGINT"},
                    {"name": "value", "type": "VARCHAR"},
                ],
                "rows": 0,
            },
        ],
    }
    emit_dir = write_emit(
        tmp_path,
        sidecar=sidecar,
        db_tables={
            "firings": "CREATE TABLE firings (fork_path VARCHAR, sim_time BIGINT)",
            "records__patient": _RECORDS_PATIENT_ZPROP_DDL,
            "history": (
                "CREATE TABLE history "
                "(fork_path VARCHAR, kind VARCHAR, record_id VARCHAR, "
                "property VARCHAR, sim_time BIGINT, value VARCHAR)"
            ),
        },
    )
    # Insert two failing history rows: b_prop and a_prop both absent from sidecar
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ["trunk", "patient", "r001", "b_prop", 10, "x"],
    )
    conn.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
        ["trunk", "patient", "r001", "a_prop", 20, "y"],
    )
    conn.close()

    with open_emit(emit_dir) as emit:
        result = run_check(emit, "C11")
    assert not result.passed
    # Messages should mention a_prop before b_prop (sorted order)
    a_idx = next(i for i, m in enumerate(result.messages) if "a_prop" in m)
    b_idx = next(i for i, m in enumerate(result.messages) if "b_prop" in m)
    assert a_idx < b_idx, f"Expected a_prop before b_prop; messages={result.messages}"
