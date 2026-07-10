"""Tests for the elapsed DerivedSpec (F10-grammar): cross-row time-delta.

Covers:
  (a) happy path — 2 rows same journey, assessment wait = 45.0 minutes
  (b) missing arrival row → NULL output
  (c) duplicate arrival rows → MIN picks earliest, deterministic
  (d) other_where key absent from table → ExportError at validate_table
  (e) unit=seconds → 2700.0
  (f) fractional quotient → DOUBLE, not integer-truncated (1.5 minutes)
  (g) counterpart later than grain row → negative elapsed, no abs()
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import (
    ColumnDecl,
    DerivedSpec,
    ElapsedSpec,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.columns import (
    build_elapsed_expr,
)
from fabulexa_forge.exporters.dimensional.validation import (
    check_elapsed_columns_exist,
)
from fabulexa_forge.reader.sidecar import Sidecar

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TABLE = "records__tick_decision"
_COLUMNS = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "prop__journey_instance", "type": "VARCHAR"},
    {"name": "prop__decision_type", "type": "VARCHAR"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
]


def _make_sidecar() -> Sidecar:
    """Build a minimal Sidecar for tick_decision records."""
    raw: dict = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
            {
                "name": _TABLE,
                "category": "records",
                "record_kind": "tick_decision",
                "columns": _COLUMNS,
                "rows": 0,
            }
        ],
    }
    return Sidecar.from_raw(raw)


def _make_col(unit: str = "minutes") -> ColumnDecl:
    """Build a wait_minutes ColumnDecl with elapsed spec."""
    return ColumnDecl(
        name="wait_minutes",
        derived=DerivedSpec(
            elapsed=ElapsedSpec(
                correlate_on="prop__journey_instance",
                other_where={"prop__decision_type": "ed_arrival"},
                start_source="last_mutation_sim_time",
                end_source="last_mutation_sim_time",
                unit=unit,  # type: ignore[arg-type]
            )
        ),
    )


def _build_db(tmp_path: Path, rows: list[tuple]) -> Path:
    """Create a DuckDB with tick_decision rows and return its path.

    Each row tuple: (fork_path, record_id, journey_instance, decision_type, sim_time)
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(
        f'CREATE TABLE "{_TABLE}" ('
        '"fork_path" VARCHAR, "record_id" VARCHAR,'
        '"prop__journey_instance" VARCHAR, "prop__decision_type" VARCHAR,'
        '"last_mutation_sim_time" BIGINT'
        ")"
    )
    for row in rows:
        conn.execute(f'INSERT INTO "{_TABLE}" VALUES (?, ?, ?, ?, ?)', list(row))
    conn.close()
    return db_path


def _run_elapsed_sql(db_path: Path, col: ColumnDecl, sidecar: Sidecar) -> list:
    """Execute the elapsed SELECT + JOIN SQL against a DuckDB file and return rows."""
    expr, joins = build_elapsed_expr(col, _TABLE, sidecar)
    join_sql = " ".join(joins)
    sql = (
        f"SELECT {expr}"
        f' FROM "{_TABLE}" AS "_grain"'
        f" {join_sql}"
        f' WHERE "_grain"."prop__decision_type" = \'ed_assessment\''
        f' ORDER BY "_grain"."record_id"'
    )
    conn = duckdb.connect(str(db_path), read_only=True)
    result = conn.execute(sql).fetchall()
    conn.close()
    return result


# ---------------------------------------------------------------------------
# (a) Happy path — 45.0 minutes
# ---------------------------------------------------------------------------


def test_elapsed_happy_wait_minutes(tmp_path: Path) -> None:
    """arrival sim_time=0, assessment sim_time=2_700_000_000_000 → 45.0 minutes."""
    rows = [
        ("trunk", "r1", "j1", "ed_arrival", 0),
        ("trunk", "r2", "j1", "ed_assessment", 2_700_000_000_000),
    ]
    db_path = _build_db(tmp_path, rows)
    col = _make_col("minutes")
    sidecar = _make_sidecar()
    result = _run_elapsed_sql(db_path, col, sidecar)
    assert len(result) == 1
    assert result[0][0] == pytest.approx(45.0)


# ---------------------------------------------------------------------------
# (b) Missing arrival → NULL
# ---------------------------------------------------------------------------


def test_elapsed_missing_arrival_produces_null(tmp_path: Path) -> None:
    """When no matching arrival row exists, elapsed yields NULL."""
    rows = [
        ("trunk", "r1", "j99", "ed_assessment", 1_000_000_000),
    ]
    db_path = _build_db(tmp_path, rows)
    col = _make_col("minutes")
    sidecar = _make_sidecar()
    result = _run_elapsed_sql(db_path, col, sidecar)
    assert len(result) == 1
    assert result[0][0] is None


# ---------------------------------------------------------------------------
# (c) Duplicate arrival rows → MIN picks earliest
# ---------------------------------------------------------------------------


def test_elapsed_duplicate_arrivals_picks_min(tmp_path: Path) -> None:
    """Duplicate arrival rows: MIN(start_ns) selects the earliest, no fan-out."""
    rows = [
        # Two arrival rows for the same journey — 100 and 200 ns
        ("trunk", "r1", "j1", "ed_arrival", 100),
        ("trunk", "r2", "j1", "ed_arrival", 200),
        # One assessment row
        ("trunk", "r3", "j1", "ed_assessment", 2_700_000_000_100),
    ]
    db_path = _build_db(tmp_path, rows)
    col = _make_col("minutes")
    sidecar = _make_sidecar()
    result = _run_elapsed_sql(db_path, col, sidecar)
    # Exactly one assessment row, no fan-out
    assert len(result) == 1
    # (2_700_000_000_100 - 100) / 60_000_000_000 = 2_700_000_000_000 / 60B = 45.0
    assert result[0][0] == pytest.approx(45.0)


# ---------------------------------------------------------------------------
# (d) other_where key absent → ExportError at validate_table
# ---------------------------------------------------------------------------


def test_elapsed_absent_other_where_key_raises_export_error() -> None:
    """other_where key absent from source table columns raises ExportError."""
    sidecar = _make_sidecar()
    col = ColumnDecl(
        name="wait_minutes",
        derived=DerivedSpec(
            elapsed=ElapsedSpec(
                correlate_on="prop__journey_instance",
                other_where={"prop__nonexistent_col": "ed_arrival"},
                start_source="last_mutation_sim_time",
                end_source="last_mutation_sim_time",
                unit="minutes",
            )
        ),
    )
    from fabulexa_forge.config.models import SourceDecl, TableDecl

    table_decl = TableDecl(
        name="fact_ed_assessment",
        role="fact",
        source=SourceDecl(grain="records", kind="tick_decision"),
        key=["decision_id"],
        columns=[col],
    )
    with pytest.raises(ExportError, match="prop__nonexistent_col"):
        check_elapsed_columns_exist(col, table_decl, _TABLE, sidecar)


# ---------------------------------------------------------------------------
# (e) unit=seconds → 2700.0
# ---------------------------------------------------------------------------


def test_elapsed_seconds_unit(tmp_path: Path) -> None:
    """unit=seconds: 2_700_000_000_000 ns / 1_000_000_000 = 2700.0 seconds."""
    rows = [
        ("trunk", "r1", "j1", "ed_arrival", 0),
        ("trunk", "r2", "j1", "ed_assessment", 2_700_000_000_000),
    ]
    db_path = _build_db(tmp_path, rows)
    col = _make_col("seconds")
    sidecar = _make_sidecar()
    result = _run_elapsed_sql(db_path, col, sidecar)
    assert len(result) == 1
    assert result[0][0] == pytest.approx(2700.0)


# ---------------------------------------------------------------------------
# (f) Fractional quotient → DOUBLE, not integer-truncated
# ---------------------------------------------------------------------------


def test_elapsed_fractional_quotient_is_not_truncated(tmp_path: Path) -> None:
    """90e9 ns / 60e9 = 1.5 minutes — DOUBLE division, not truncated to 1.

    Pins the DOUBLE-output decision: the exact-multiple happy-path cases (45.0,
    2700.0) pass under both `/` and `//` because 45 == approx(45.0). A `//`
    regression yields 1 here and fails, so this is the case that actually guards
    against integer truncation.
    """
    rows = [
        ("trunk", "r1", "j1", "ed_arrival", 0),
        ("trunk", "r2", "j1", "ed_assessment", 90_000_000_000),
    ]
    db_path = _build_db(tmp_path, rows)
    col = _make_col("minutes")
    sidecar = _make_sidecar()
    result = _run_elapsed_sql(db_path, col, sidecar)
    assert len(result) == 1
    assert result[0][0] == pytest.approx(1.5)
    assert isinstance(result[0][0], float)


# ---------------------------------------------------------------------------
# (g) Counterpart later than grain row → negative elapsed, no abs()
# ---------------------------------------------------------------------------


def test_elapsed_negative_delta_is_not_abs(tmp_path: Path) -> None:
    """Counterpart later than the grain row → negative result; SQL has no abs().

    Deliberately inverts the usual ordering so the arrival ("start") is later
    than the assessment ("end"): (0 − 2_700_000_000_000) / 60e9 = -45.0. An
    abs() regression would yield +45.0, so this pins `end − start` with sign.
    """
    rows = [
        # Arrival (the other_where "start" row) is LATER than the assessment.
        ("trunk", "r1", "j1", "ed_arrival", 2_700_000_000_000),
        ("trunk", "r2", "j1", "ed_assessment", 0),
    ]
    db_path = _build_db(tmp_path, rows)
    col = _make_col("minutes")
    sidecar = _make_sidecar()
    result = _run_elapsed_sql(db_path, col, sidecar)
    assert len(result) == 1
    assert result[0][0] == pytest.approx(-45.0)


# ---------------------------------------------------------------------------
# SQL shape tests (no DB needed)
# ---------------------------------------------------------------------------


def test_build_elapsed_expr_join_clause_shape() -> None:
    """build_elapsed_expr produces a subquery JOIN aliased _el_<colname>."""
    col = _make_col("minutes")
    sidecar = _make_sidecar()
    _, joins = build_elapsed_expr(col, _TABLE, sidecar)
    assert len(joins) == 1
    j = joins[0]
    assert "_el_wait_minutes" in j
    assert "LEFT JOIN" in j
    assert "MIN(CAST" in j
    assert "GROUP BY" in j
    assert "ed_arrival" in j


def test_build_elapsed_expr_select_expr_shape() -> None:
    """build_elapsed_expr SELECT fragment contains the divisor and alias."""
    col = _make_col("minutes")
    sidecar = _make_sidecar()
    expr, _ = build_elapsed_expr(col, _TABLE, sidecar)
    assert "60000000000" in expr
    assert '"wait_minutes"' in expr
    assert "CAST" in expr


def test_build_elapsed_expr_hours_divisor() -> None:
    """hours unit uses 3_600_000_000_000 divisor."""
    col = _make_col("hours")
    sidecar = _make_sidecar()
    expr, _ = build_elapsed_expr(col, _TABLE, sidecar)
    assert "3600000000000" in expr


def test_build_elapsed_expr_seconds_divisor() -> None:
    """seconds unit uses 1_000_000_000 divisor."""
    col = _make_col("seconds")
    sidecar = _make_sidecar()
    expr, _ = build_elapsed_expr(col, _TABLE, sidecar)
    assert "1000000000" in expr


# ---------------------------------------------------------------------------
# Validation: correlate_on / start_source / end_source absent → ExportError
# ---------------------------------------------------------------------------


def _make_table_decl_with(col: ColumnDecl) -> "object":
    from fabulexa_forge.config.models import SourceDecl, TableDecl

    return TableDecl(
        name="fact_ed_assessment",
        role="fact",
        source=SourceDecl(grain="records", kind="tick_decision"),
        key=["id"],
        columns=[col],
    )


def test_elapsed_absent_correlate_on_raises() -> None:
    """correlate_on column absent from source table raises ExportError."""
    sidecar = _make_sidecar()
    col = ColumnDecl(
        name="wait_minutes",
        derived=DerivedSpec(
            elapsed=ElapsedSpec(
                correlate_on="prop__missing_key",
                other_where={"prop__decision_type": "ed_arrival"},
                start_source="last_mutation_sim_time",
                end_source="last_mutation_sim_time",
                unit="minutes",
            )
        ),
    )
    table_decl = _make_table_decl_with(col)
    with pytest.raises(ExportError, match="prop__missing_key"):
        check_elapsed_columns_exist(col, table_decl, _TABLE, sidecar)  # type: ignore[arg-type]


def test_elapsed_absent_start_source_raises() -> None:
    """start_source column absent from source table raises ExportError."""
    sidecar = _make_sidecar()
    col = ColumnDecl(
        name="wait_minutes",
        derived=DerivedSpec(
            elapsed=ElapsedSpec(
                correlate_on="prop__journey_instance",
                other_where={"prop__decision_type": "ed_arrival"},
                start_source="nonexistent_start",
                end_source="last_mutation_sim_time",
                unit="minutes",
            )
        ),
    )
    table_decl = _make_table_decl_with(col)
    with pytest.raises(ExportError, match="nonexistent_start"):
        check_elapsed_columns_exist(col, table_decl, _TABLE, sidecar)  # type: ignore[arg-type]


def test_elapsed_absent_end_source_raises() -> None:
    """end_source column absent from source table raises ExportError."""
    sidecar = _make_sidecar()
    col = ColumnDecl(
        name="wait_minutes",
        derived=DerivedSpec(
            elapsed=ElapsedSpec(
                correlate_on="prop__journey_instance",
                other_where={"prop__decision_type": "ed_arrival"},
                start_source="last_mutation_sim_time",
                end_source="nonexistent_end",
                unit="minutes",
            )
        ),
    )
    table_decl = _make_table_decl_with(col)
    with pytest.raises(ExportError, match="nonexistent_end"):
        check_elapsed_columns_exist(col, table_decl, _TABLE, sidecar)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Grammar: ElapsedSpec parse requires all fields
# ---------------------------------------------------------------------------


def test_elapsed_spec_all_fields_required_missing_unit() -> None:
    """ElapsedSpec missing unit raises ValidationError at parse time."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ElapsedSpec(
            correlate_on="prop__journey_instance",
            other_where={"prop__decision_type": "ed_arrival"},
            start_source="last_mutation_sim_time",
            end_source="last_mutation_sim_time",
            # unit missing
        )  # type: ignore[call-arg]


def test_derived_spec_elapsed_exactly_one_validates() -> None:
    """DerivedSpec with elapsed set passes exactly_one_derived."""
    spec = DerivedSpec(
        elapsed=ElapsedSpec(
            correlate_on="prop__journey_instance",
            other_where={"prop__decision_type": "ed_arrival"},
            start_source="last_mutation_sim_time",
            end_source="last_mutation_sim_time",
            unit="minutes",
        )
    )
    assert spec.elapsed is not None
    assert spec.ordinal is None
    assert spec.timestamp is None
