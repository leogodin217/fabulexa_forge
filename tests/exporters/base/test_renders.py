"""Tests for build_base_render_sql: state-at composition, horizon selection,
anchor-or-raw-ns lifecycle rendering, cast-back typing, and rename projection.

Runs the render's SQL directly against the DuckDB-backed `patient` fixture
(`_base_fixtures.build_base_test_emit`), asserting: horizon-less composes
`build_state_at_end_sql` verbatim, a horizon composes `build_state_at_sql`
verbatim; the as-of value at a horizon differs from the tape's-end value; a
record created at-or-after a horizon is absent; a still-open-at-horizon
record shows active=True/deactivated_at=NULL, flipping at the tape's end;
anchor-or-raw-ns lifecycle rendering; cast-back typing of prop__/
presentation_id columns; `record_id -> id` renaming; the absence of
last_mutation_sim_time/updated_at; the (created_sim_time, record_id)
ordering; and an empty property set rendering identity + lifecycle only.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import date, time
from pathlib import Path
from typing import Iterator

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import write_emit

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import DateParseElection
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.derivations.state_at import (
    build_state_at_end_sql,
    build_state_at_sql,
)
from fabulexa_forge.exporters.base.plan import BaseTableSpec, build_base_plan
from fabulexa_forge.exporters.base.renders import build_base_render_sql
from fabulexa_forge.reader.emit import Emit, open_emit
from fabulexa_forge.reader.errors import RunDatabaseError

from ._base_fixtures import (
    DAY_NS,
    build_base_render_election_emit,
    build_base_test_emit,
)

# Fixed emission order for the fixture's default (unrenamed) plan: the self key
# (record_index -> patient_key; the fixture's `patient` kind has no reference
# property, so no edge key appears), STATE_AT_COLUMNS (record_id -> id),
# presentation_id, then prop__<p> in sidecar declaration order (prop__status,
# prop__age).
_COLUMN_ORDER = (
    "patient_key",
    "id",
    "created_sim_time",
    "active",
    "deactivated_at",
    "presentation_id",
    "prop__status",
    "prop__age",
)


@contextmanager
def _patient_emit(tmp_path: Path) -> Iterator[tuple[Emit, BaseTableSpec, str]]:
    """Open the patient fixture emit and resolve its plan and fork_path."""
    emit_dir = build_base_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        plan = build_base_plan(emit.sidecar, None, notice_sink=discard_notice_sink)
        spec = next(t for t in plan.tables if t.kind == "patient")
        yield emit, spec, fork_path


def _rows(emit: Emit, sql: str) -> list[dict[str, object]]:
    """Execute sql and zip every row against the fixed default column order."""
    return [dict(zip(_COLUMN_ORDER, row)) for row in emit.query(sql, ())]


# Emission order for the election fixture's default (unrenamed) plan: adds
# prop__signup_date after prop__status (sidecar declaration order).
_ELECTION_COLUMN_ORDER = (
    "patient_key",
    "id",
    "created_sim_time",
    "active",
    "deactivated_at",
    "presentation_id",
    "prop__status",
    "prop__signup_date",
)


@contextmanager
def _election_emit(tmp_path: Path) -> Iterator[tuple[Emit, BaseTableSpec, str]]:
    """Open the render-election fixture emit and resolve its plan and fork_path."""
    emit_dir = build_base_render_election_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        plan = build_base_plan(emit.sidecar, None, notice_sink=discard_notice_sink)
        spec = next(t for t in plan.tables if t.kind == "patient")
        yield emit, spec, fork_path


def _election_rows(emit: Emit, sql: str) -> list[dict[str, object]]:
    """Execute sql and zip every row against the election fixture's column order."""
    return [dict(zip(_ELECTION_COLUMN_ORDER, row)) for row in emit.query(sql, ())]


# ---------------------------------------------------------------------------
# Horizon selection composes the right state-at builder
# ---------------------------------------------------------------------------


def test_horizon_none_composes_build_state_at_end_sql(tmp_path: Path) -> None:
    """horizon_ns=None composes build_state_at_end_sql (no horizon predicate)."""
    with _patient_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        expected = build_state_at_end_sql(
            emit.sidecar, fork_path, spec.kind, spec.properties
        )
    assert expected in sql


def test_horizon_ns_composes_build_state_at_sql_at_exactly_t(tmp_path: Path) -> None:
    """horizon_ns=T composes build_state_at_sql at exactly T."""
    horizon = 2 * DAY_NS + 1
    with _patient_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, anchor, horizon)
        expected = build_state_at_sql(
            emit.sidecar, fork_path, spec.kind, spec.properties, horizon
        )
    assert expected in sql


# ---------------------------------------------------------------------------
# As-of vs tape's-end property values
# ---------------------------------------------------------------------------


def test_tape_end_reflects_latest_history_value(tmp_path: Path) -> None:
    """At the tape's end, p001.prop__status is 'discharged' (its latest value)."""
    with _patient_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = {r["id"]: r for r in _rows(emit, sql)}
    assert rows["p001"]["prop__status"] == "discharged"


def test_horizon_reflects_as_of_value_not_the_later_one(tmp_path: Path) -> None:
    """At horizon_ns=2*DAY+1, p001.prop__status is 'active', not 'discharged'."""
    horizon = 2 * DAY_NS + 1
    with _patient_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, anchor, horizon)
        rows = {r["id"]: r for r in _rows(emit, sql)}
    assert rows["p001"]["prop__status"] == "active"


def test_record_created_at_or_after_horizon_is_absent(tmp_path: Path) -> None:
    """At horizon_ns=1*DAY, p003 (created exactly at 1*DAY) is absent."""
    horizon = DAY_NS
    with _patient_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, anchor, horizon)
        rows = {r["id"]: r for r in _rows(emit, sql)}
    assert "p003" not in rows


def test_still_open_before_horizon_active_true_deactivated_at_null(
    tmp_path: Path,
) -> None:
    """a002 (deactivated at 2*DAY) rendered at horizon_ns=1*DAY: active=True,
    deactivated_at=NULL."""
    horizon = DAY_NS
    with _patient_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, anchor, horizon)
        rows = {r["id"]: r for r in _rows(emit, sql)}
    assert rows["a002"]["active"] is True
    assert rows["a002"]["deactivated_at"] is None


def test_deactivated_after_horizon_at_tape_end(tmp_path: Path) -> None:
    """The same record at the tape's end: active=False, deactivated_at set."""
    with _patient_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = {r["id"]: r for r in _rows(emit, sql)}
    assert rows["a002"]["active"] is False
    assert rows["a002"]["deactivated_at"] is not None


# ---------------------------------------------------------------------------
# Anchor-or-raw-ns lifecycle rendering
# ---------------------------------------------------------------------------


def test_with_anchor_lifecycle_columns_render_wallclock(tmp_path: Path) -> None:
    """With an anchor, created_sim_time / deactivated_at come back wallclock."""
    with _patient_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = {r["id"]: r for r in _rows(emit, sql)}
    assert "2024-01-01" in str(rows["p001"]["created_sim_time"])
    assert "2024-01-03" in str(rows["a002"]["deactivated_at"])  # 2 days after start


def test_without_anchor_lifecycle_columns_render_raw_bigint(tmp_path: Path) -> None:
    """With anchor=None, created_sim_time / deactivated_at stay raw sim-time ns."""
    with _patient_emit(tmp_path) as (emit, spec, fork_path):
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, None, None)
        rows = {r["id"]: r for r in _rows(emit, sql)}
    assert rows["p001"]["created_sim_time"] == 0
    assert rows["a002"]["deactivated_at"] == 2 * DAY_NS


# ---------------------------------------------------------------------------
# Cast-back typing, renaming, reserved columns, ordering
# ---------------------------------------------------------------------------


def test_prop_columns_cast_back_to_sidecar_type(tmp_path: Path) -> None:
    """prop__<p> columns come back as their declared sidecar type, not VARCHAR."""
    with _patient_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = {r["id"]: r for r in _rows(emit, sql)}
    assert isinstance(rows["p001"]["prop__age"], int)
    assert rows["p001"]["prop__age"] == 30
    assert isinstance(rows["p001"]["prop__status"], str)


def test_presentation_id_cast_back_to_sidecar_type(tmp_path: Path) -> None:
    """presentation_id is cast back to its sidecar type (BIGINT), not VARCHAR."""
    with _patient_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = {r["id"]: r for r in _rows(emit, sql)}
    assert isinstance(rows["p001"]["presentation_id"], int)
    assert rows["p001"]["presentation_id"] == 1001


def test_column_renames_applied_record_id_emitted_as_id(tmp_path: Path) -> None:
    """column_renames are applied: record_id is emitted as id."""
    with _patient_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _rows(emit, sql)
    assert {r["id"] for r in rows} == {"p001", "a002", "p003"}
    assert 'AS "record_id"' not in sql


def test_no_last_mutation_sim_time_or_updated_at_column(tmp_path: Path) -> None:
    """No last_mutation_sim_time or updated_at column ever appears in the output."""
    with _patient_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, anchor, None)
    assert "last_mutation_sim_time" not in sql
    assert "updated_at" not in sql


def test_ordered_by_created_sim_time_record_id_over_raw_ns(tmp_path: Path) -> None:
    """The emitted SQL orders by (created_sim_time, record_id) over raw ns."""
    with _patient_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, anchor, None)
    order_clause = sql.rsplit("ORDER BY", 1)[1]
    assert order_clause.strip() == '"_base"."created_sim_time", "_base"."record_id"'


def test_empty_property_set_renders_identity_and_lifecycle_only(
    tmp_path: Path,
) -> None:
    """A property set that is empty renders identity + lifecycle columns only."""
    with _patient_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        narrowed_spec = replace(spec, properties=frozenset())
        sql = build_base_render_sql(
            emit.sidecar, fork_path, narrowed_spec, anchor, None
        )
        rows = emit.query(sql, ())
    # patient_key, id, created_sim_time, active, deactivated_at,
    # presentation_id — no prop__.
    assert all(len(row) == 6 for row in rows)
    assert "prop__" not in sql


# ---------------------------------------------------------------------------
# `render`: temporal rendering elections (bare shorthand + `date_parse` form)
# ---------------------------------------------------------------------------


def test_render_election_created_sim_time_renders_date(tmp_path: Path) -> None:
    """An elected `date` rendering on created_sim_time yields a DATE value,
    not the default TIMESTAMP."""
    with _election_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        elected_spec = replace(spec, render=(("created_sim_time", "date"),))
        sql = build_base_render_sql(emit.sidecar, fork_path, elected_spec, anchor, None)
        rows = {r["id"]: r for r in _election_rows(emit, sql)}
    assert rows["p001"]["created_sim_time"] == date(2024, 1, 1)


def test_date_parse_on_prop_renders_date_and_nulls_flow_through(
    tmp_path: Path,
) -> None:
    """A date_parse election on a prop__ VARCHAR renders DATE; NULL flows
    through as NULL."""
    with _election_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        elected_spec = replace(
            spec,
            render=(("prop__signup_date", DateParseElection(date_parse="%Y-%m-%d")),),
        )
        sql = build_base_render_sql(emit.sidecar, fork_path, elected_spec, anchor, None)
        rows = {r["id"]: r for r in _election_rows(emit, sql)}
    assert rows["p001"]["prop__signup_date"] == date(2024, 1, 15)
    assert rows["p002"]["prop__signup_date"] is None


_TIME_PARSE_PATIENT_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__meeting_time",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_TIME_PARSE_COLUMN_ORDER = (
    "patient_key",
    "id",
    "created_sim_time",
    "active",
    "deactivated_at",
    "presentation_id",
    "prop__meeting_time",
)


@contextmanager
def _time_parse_patient_emit(
    tmp_path: Path,
) -> Iterator[tuple[Emit, BaseTableSpec, str]]:
    """Build and open a minimal `patient` emit whose `prop__meeting_time`
    payload column carries a time-of-day string — the widened parse
    family's TIME-denotation flow-through fixture."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(
        'CREATE TABLE "records__patient" ('
        '"fork_path" VARCHAR, "record_id" VARCHAR, "presentation_id" BIGINT,'
        ' "created_sim_time" BIGINT, "active" BOOLEAN, "deactivated_at" BIGINT,'
        ' "last_mutation_sim_time" BIGINT, "record_index" BIGINT,'
        ' "prop__meeting_time" VARCHAR)'
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "p001", 1001, 0, True, 0, 0, "14:30"],
    )
    conn.close()
    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": _TIME_PARSE_PATIENT_COLUMNS,
                "rows": 1,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": DAY_NS}],
        extra={
            "record_roles": {"patient": "dimension"},
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    with open_emit(tmp_path) as emit:
        fork_path = require_single_branch(emit.sidecar)
        plan = build_base_plan(emit.sidecar, None, notice_sink=discard_notice_sink)
        spec = next(t for t in plan.tables if t.kind == "patient")
        yield emit, spec, fork_path


def test_date_parse_time_only_format_renders_time_end_to_end(tmp_path: Path) -> None:
    """A date_parse format carrying only time directives (the widened parse
    family) denotes and renders TIME through the base map form, end-to-end."""
    with _time_parse_patient_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        elected_spec = replace(
            spec,
            render=(("prop__meeting_time", DateParseElection(date_parse="%H:%M")),),
        )
        sql = build_base_render_sql(emit.sidecar, fork_path, elected_spec, anchor, None)
        rows = [dict(zip(_TIME_PARSE_COLUMN_ORDER, row)) for row in emit.query(sql, ())]
    assert rows[0]["prop__meeting_time"] == time(14, 30)


def test_date_parse_mismatch_fails_loudly_naming_table_column_value(
    tmp_path: Path,
) -> None:
    """A non-matching non-NULL date_parse value fails the export loudly,
    naming the table, source column, and offending value."""
    with _election_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        elected_spec = replace(
            spec,
            render=(("prop__status", DateParseElection(date_parse="%Y-%m-%d")),),
        )
        sql = build_base_render_sql(emit.sidecar, fork_path, elected_spec, anchor, None)
        with pytest.raises(RunDatabaseError) as excinfo:
            emit.query(sql, ())
    message = str(excinfo.value)
    assert spec.table_name in message
    assert "prop__status" in message
    assert "admitted" in message


def test_render_election_applies_identically_under_a_window(tmp_path: Path) -> None:
    """A windowed export applies the same election as a full export; the
    cast-back posture is unaffected for unelected columns."""
    with _election_emit(tmp_path) as (emit, spec, fork_path):
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        elected_spec = replace(spec, render=(("created_sim_time", "date"),))
        horizon_sql = build_base_render_sql(
            emit.sidecar, fork_path, elected_spec, anchor, DAY_NS
        )
        rows = {r["id"]: r for r in _election_rows(emit, horizon_sql)}
    assert rows["p001"]["created_sim_time"] == date(2024, 1, 1)
    assert isinstance(rows["p001"]["prop__status"], str)


def test_no_anchor_default_rendering_keeps_raw_ns(tmp_path: Path) -> None:
    """With no election and no anchor, created_sim_time stays raw sim-time
    ns — the no-anchor default path is unaffected by the render surface."""
    with _election_emit(tmp_path) as (emit, spec, fork_path):
        sql = build_base_render_sql(emit.sidecar, fork_path, spec, None, None)
        rows = {r["id"]: r for r in _election_rows(emit, sql)}
    assert rows["p001"]["created_sim_time"] == 0
