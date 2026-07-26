"""Tests for build_base_render_sql's key-column joins: the self key
(record_index -> <kind>_key) and per-edge reference keys (ref_index__<p>'s
target-side record_index), over dedicated reference-edge fixtures.

Runs the render's SQL directly against the DuckDB-backed `actor`/`target`
fixture (`_base_fixtures.build_reference_edge_emit`), asserting: the self key
is first and never NULL; a resolved edge key equals the target's
record_index; a dangling reference, an absent property, and a target created
at-or-after the horizon each yield id-present/key-NULL; a target deactivated
before the horizon still resolves; the same emit renders different edge
populations at a mid-tape horizon vs the tape's end; two reference properties
on one kind yield two independently-named key columns; a row-duplicated
target (`_base_fixtures.build_duplicated_target_emit`) does not fan the
spine's row set out; and renamed key columns are projected under their
renamed names.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from _support.notices import discard_notice_sink

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import BaseConfig, RenameEntry
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.exporters.base.plan import BaseTableSpec, build_base_plan
from fabulexa_forge.exporters.base.renders import build_base_render_sql
from fabulexa_forge.reader.emit import Emit, open_emit

from ._base_fixtures import (
    DAY_NS,
    build_duplicated_target_emit,
    build_reference_edge_emit,
)

#: The mid-tape horizon used throughout: strictly after t002's deactivation
#: (1*DAY), strictly before t003's creation (3*DAY).
_MID_TAPE_HORIZON = 2 * DAY_NS + 1

#: The `actor` fixture's state-at identity emission order (unrenamed).
_IDENTITY_ORDER = (
    "record_index",
    "record_id",
    "created_sim_time",
    "active",
    "deactivated_at",
    "prop__lead_id",
    "ref_index__lead_id",
    "prop__backup_id",
    "ref_index__backup_id",
)

#: The identity -> default output name map for `_IDENTITY_ORDER`'s renamed
#: entries; every other identity is projected under its own name.
_DEFAULT_RENAMES = {
    "record_index": "actor_key",
    "record_id": "id",
    "ref_index__lead_id": "lead_id_key",
    "ref_index__backup_id": "backup_id_key",
}


def _column_order(overrides: "dict[str, str] | None" = None) -> tuple[str, ...]:
    """The actor fixture's column emission order, with any rename overrides
    substituted in place of their default output name.

    Args:
        overrides: A `rename.columns`-shaped identity -> output name map, or
            None for the unrenamed defaults.

    Returns:
        One output name per `_IDENTITY_ORDER` entry, in that order.
    """
    renames = {**_DEFAULT_RENAMES, **(overrides or {})}
    return tuple(renames.get(identity, identity) for identity in _IDENTITY_ORDER)


def _rows_by_id(
    emit: Emit, sql: str, column_order: tuple[str, ...]
) -> dict[str, dict[str, object]]:
    """Execute sql, zip each row against column_order, and index by its id
    column value.

    Args:
        emit: The open emit to query.
        sql: The render SQL to execute.
        column_order: The column names to zip against each result row, in
            `_column_order`'s output-name space.

    Returns:
        {id -> row dict}, one entry per result row.
    """
    id_name = column_order[_IDENTITY_ORDER.index("record_id")]
    rows = [dict(zip(column_order, row)) for row in emit.query(sql, ())]
    return {row[id_name]: row for row in rows}


@contextmanager
def _actor_emit(
    tmp_path: Path, config: "BaseConfig | None" = None
) -> Iterator[tuple[Emit, BaseTableSpec, str]]:
    """Open the reference-edge fixture and resolve its `actor` spec and fork_path."""
    emit_dir = build_reference_edge_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        plan = build_base_plan(emit.sidecar, config, notice_sink=discard_notice_sink)
        spec = next(t for t in plan.tables if t.kind == "actor")
        yield emit, spec, fork_path


def _render(
    emit: Emit, spec: BaseTableSpec, fork_path: str, horizon_ns: "int | None"
) -> str:
    """Resolve the emit's runtime anchor and render `spec` at `horizon_ns`."""
    anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
    return build_base_render_sql(emit.sidecar, fork_path, spec, anchor, horizon_ns)


# ---------------------------------------------------------------------------
# Self key: position and null-ness
# ---------------------------------------------------------------------------


def test_self_key_is_first_projected_column(tmp_path: Path) -> None:
    """The self key is the table's first projected column, ahead of id."""
    with _actor_emit(tmp_path) as (emit, spec, fork_path):
        sql = _render(emit, spec, fork_path, None)
    select_clause = sql.split(" FROM ", 1)[0]
    first_column = select_clause[len("SELECT ") :].split(",", 1)[0].strip()
    assert first_column == '"_key_self"."record_index" AS "actor_key"'


def test_self_key_never_null(tmp_path: Path) -> None:
    """The self key is projected verbatim and is never NULL."""
    with _actor_emit(tmp_path) as (emit, spec, fork_path):
        sql = _render(emit, spec, fork_path, None)
        rows = _rows_by_id(emit, sql, _column_order())
    assert all(row["actor_key"] is not None for row in rows.values())


def test_edge_key_sits_immediately_after_its_prop_column(tmp_path: Path) -> None:
    """An edge key sits immediately after its own prop__<p> output column."""
    with _actor_emit(tmp_path) as (emit, spec, fork_path):
        sql = _render(emit, spec, fork_path, None)
    select_clause = sql.split(" FROM ", 1)[0]
    projected = [p.strip() for p in select_clause[len("SELECT ") :].split(",")]
    lead_idx = next(
        i for i, p in enumerate(projected) if p.endswith('AS "prop__lead_id"')
    )
    assert projected[lead_idx + 1].endswith('AS "lead_id_key"')


# ---------------------------------------------------------------------------
# Edge key resolution scenarios
# ---------------------------------------------------------------------------


def test_resolved_edge_key_equals_target_record_index(tmp_path: Path) -> None:
    """A resolved edge's key equals the target's record_index."""
    with _actor_emit(tmp_path) as (emit, spec, fork_path):
        sql = _render(emit, spec, fork_path, None)
        rows = _rows_by_id(emit, sql, _column_order())
    assert rows["a001"]["prop__lead_id"] == "t001"
    assert rows["a001"]["lead_id_key"] == 0


def test_dangling_reference_id_present_key_null(tmp_path: Path) -> None:
    """A dangling reference (id names no record): id present, key NULL."""
    with _actor_emit(tmp_path) as (emit, spec, fork_path):
        sql = _render(emit, spec, fork_path, None)
        rows = _rows_by_id(emit, sql, _column_order())
    assert rows["a002"]["prop__lead_id"] == "t999"
    assert rows["a002"]["lead_id_key"] is None


def test_absent_property_id_null_key_null(tmp_path: Path) -> None:
    """An absent property: id NULL, key NULL."""
    with _actor_emit(tmp_path) as (emit, spec, fork_path):
        sql = _render(emit, spec, fork_path, None)
        rows = _rows_by_id(emit, sql, _column_order())
    assert rows["a003"]["prop__lead_id"] is None
    assert rows["a003"]["lead_id_key"] is None


def test_target_created_at_or_after_horizon_id_present_key_null(
    tmp_path: Path,
) -> None:
    """A target created at-or-after the horizon: id present, key NULL."""
    with _actor_emit(tmp_path) as (emit, spec, fork_path):
        sql = _render(emit, spec, fork_path, _MID_TAPE_HORIZON)
        rows = _rows_by_id(emit, sql, _column_order())
    assert rows["a004"]["prop__lead_id"] == "t003"
    assert rows["a004"]["lead_id_key"] is None


def test_target_deactivated_before_horizon_key_resolves(tmp_path: Path) -> None:
    """A target deactivated before the horizon still resolves (active is
    never a join predicate)."""
    with _actor_emit(tmp_path) as (emit, spec, fork_path):
        sql = _render(emit, spec, fork_path, _MID_TAPE_HORIZON)
        rows = _rows_by_id(emit, sql, _column_order())
    assert rows["a001"]["backup_id_key"] == 1


def test_horizon_binding_resolves_against_respective_horizon_population(
    tmp_path: Path,
) -> None:
    """The same emit rendered at the tape's end vs a mid-tape horizon resolves
    edges against the respective horizon populations."""
    with _actor_emit(tmp_path) as (emit, spec, fork_path):
        sql_horizon = _render(emit, spec, fork_path, _MID_TAPE_HORIZON)
        sql_end = _render(emit, spec, fork_path, None)
        rows_horizon = _rows_by_id(emit, sql_horizon, _column_order())
        rows_end = _rows_by_id(emit, sql_end, _column_order())
    assert rows_horizon["a004"]["lead_id_key"] is None
    assert rows_end["a004"]["lead_id_key"] == 2


def test_two_properties_on_one_kind_yield_two_named_key_columns(
    tmp_path: Path,
) -> None:
    """Two properties on one kind referencing the same target kind yield two
    key columns, each named per its own property."""
    with _actor_emit(tmp_path) as (emit, spec, fork_path):
        assert [rk.property_name for rk in spec.reference_keys] == [
            "lead_id",
            "backup_id",
        ]
        sql = _render(emit, spec, fork_path, None)
        rows = _rows_by_id(emit, sql, _column_order())
    assert rows["a001"]["lead_id_key"] == 0
    assert rows["a001"]["backup_id_key"] == 1


# ---------------------------------------------------------------------------
# Row-duplicated target: no fan-out
# ---------------------------------------------------------------------------


def test_duplicated_target_row_does_not_fan_out(tmp_path: Path) -> None:
    """A row-duplicated target (identical (record_id, record_index) pair)
    yields no more output rows than the spine — no fan-out."""
    emit_dir = build_duplicated_target_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        plan = build_base_plan(emit.sidecar, None, notice_sink=discard_notice_sink)
        spec = next(t for t in plan.tables if t.kind == "actor")
        sql = _render(emit, spec, fork_path, None)
        rows = emit.query(sql, ())
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Renamed key columns
# ---------------------------------------------------------------------------


def test_renamed_key_columns_appear_under_their_renamed_names(tmp_path: Path) -> None:
    """A rename.columns entry over record_index / ref_index__<p> is honored:
    the renamed key columns appear under their renamed names."""
    overrides = {"record_index": "actor_sk", "ref_index__lead_id": "lead_sk"}
    config = BaseConfig(rename=[RenameEntry(table="records__actor", columns=overrides)])
    with _actor_emit(tmp_path, config) as (emit, spec, fork_path):
        sql = _render(emit, spec, fork_path, None)
        rows = _rows_by_id(emit, sql, _column_order(overrides))
    assert rows["a001"]["actor_sk"] == 0
    assert rows["a001"]["lead_sk"] == 0
    assert '"actor_key"' not in sql
