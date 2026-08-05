"""Tests for source-mode key election at render time: the `state` render's
self-identity value surface (`build_state_render_sql`), reference-annotated
edge rendering (uniform + mixed per-row population resolution reading the
records-spine discriminator), the junction owner and member-kind edge
columns (`build_junction_render_sql`), the event log's elected `item_id`
column (`build_event_log_sql`), and the plan-time elected-key uniqueness
guard (`exporters/source/plan.py`'s `_run_plan_guards`, reached through
`build_source_plan` — the guard moved off the engine in this phase, so it
now surfaces from plan construction itself, before any render or write).

Renders are built directly via `build_source_plan` + the render builders
(bypassing the engine) when only the render SQL matters; the guard section
goes through `build_source_plan` itself (via `open_emit`), and the
"before any write" case through `export_source`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _support.notices import discard_notice_sink

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    ExportConfig,
    MembershipRef,
    SourceConfig,
    SourceEventsDecl,
    SourceEventSourceDecl,
    SourceTableDecl,
)
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.errors import ElectedKeyDuplicate
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.source.engine import export_source
from fabulexa_forge.exporters.source.events import build_event_log_sql
from fabulexa_forge.exporters.source.plan import (
    SourceJunctionTablePlan,
    SourceStateTablePlan,
    build_source_plan,
)
from fabulexa_forge.exporters.source.renders import (
    build_junction_render_sql,
    build_state_render_sql,
)
from fabulexa_forge.reader.emit import open_emit

from ._source_fixtures import (
    build_corrupted_junction_member_emit,
    build_source_election_emit,
    build_split_actor_presentation_id_emit,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    tables: "tuple[SourceTableDecl, ...]" = (),
    events: "SourceEventsDecl | None" = None,
) -> ExportConfig:
    """Build a `mode: source` ExportConfig from a declared table/events set."""
    return ExportConfig(
        mode="source", source=SourceConfig(tables=tables, events=events)
    )


def _open_plan(
    emit_dir: Path,
    config: ExportConfig,
    keys: "dict[str, object] | None" = None,
):
    """Open `emit_dir` and build a SourcePlan, resolving the anchor and
    election the way the engine does."""
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, keys)
        return build_source_plan(
            emit, config, anchor, election, False, discard_notice_sink
        )


def _col_map(
    table: "SourceStateTablePlan | SourceJunctionTablePlan", row: "tuple[object, ...]"
) -> "dict[str, object]":
    """Zip a result row against a table unit's output column names."""
    return {out: value for (_, out), value in zip(table.columns, row)}


def _rows_for(emit_dir: Path, config: ExportConfig, keys: "dict[str, object] | None"):
    """Open `emit_dir`, build the plan, render its sole table, and return
    (plan, mapped rows)."""
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, keys)
        plan = build_source_plan(
            emit, config, anchor, election, False, discard_notice_sink
        )
        table = plan.tables[0]
        builder = (
            build_junction_render_sql
            if isinstance(table, SourceJunctionTablePlan)
            else build_state_render_sql
        )
        sql = builder(plan.sidecar, plan.fork_path, table, plan.anchor, None)
        rows = [_col_map(table, row) for row in emit.query(sql, ())]
    return table, rows


# ---------------------------------------------------------------------------
# Self identity: state render
# ---------------------------------------------------------------------------


def test_state_self_identity_presentation_id_renders_elected_value(
    tmp_path: Path,
) -> None:
    """device's state render's 'id' column renders the elected
    presentation_id codes — including dev_night, deactivated but still
    present in the current-row-per-record read."""
    emit_dir = build_source_election_emit(tmp_path)
    config = _config(tables=(SourceTableDecl(name="device", kind="device"),))
    _, rows = _rows_for(emit_dir, config, {"device": "presentation_id"})
    ids = {r["id"] for r in rows}
    assert ids == {"DAY_001", "NIGHT_001"}


def test_state_self_identity_record_index_bigint_keeps_standalone_presentation_id(
    tmp_path: Path,
) -> None:
    """record_index election renders the 'id' column BIGINT-typed; the
    standalone presentation_id payload column (not absorbed) still carries
    its own verbatim value."""
    emit_dir = build_source_election_emit(tmp_path)
    config = _config(tables=(SourceTableDecl(name="device", kind="device"),))
    table, rows = _rows_for(emit_dir, config, {"device": "record_index"})
    assert dict(table.columns)["presentation_id"] == "presentation_id"
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {0, 1}
    assert all(isinstance(k, int) for k in by_id)
    day_row = next(r for r in rows if r["presentation_id"] == "DAY_001")
    assert isinstance(day_row["id"], int)


# ---------------------------------------------------------------------------
# Edge rendering: reference-annotated prop__ column
# ---------------------------------------------------------------------------


def test_reference_edge_uniform_presentation_id_renders_target_codes(
    tmp_path: Path,
) -> None:
    """order.device_id renders device's elected presentation_id codes,
    resolved independent of order's own (default) identity election."""
    emit_dir = build_source_election_emit(tmp_path)
    config = _config(tables=(SourceTableDecl(name="orders", kind="order"),))
    table, rows = _rows_for(emit_dir, config, {"device": "presentation_id"})
    assert isinstance(table, SourceStateTablePlan)
    edge = table.edge_surfaces[0]
    assert edge.rendered_type == "VARCHAR"
    assert {r["device_id"] for r in rows} == {"DAY_001", "NIGHT_001"}


def test_reference_edge_uniform_record_index_renders_bigint(tmp_path: Path) -> None:
    """order.device_id renders device's elected record_index, BIGINT-typed."""
    emit_dir = build_source_election_emit(tmp_path)
    config = _config(tables=(SourceTableDecl(name="orders", kind="order"),))
    table, rows = _rows_for(emit_dir, config, {"device": "record_index"})
    assert isinstance(table, SourceStateTablePlan)
    edge = table.edge_surfaces[0]
    assert edge.rendered_type == "BIGINT"
    assert {r["device_id"] for r in rows} == {0, 1}
    assert all(isinstance(r["device_id"], int) for r in rows)


def test_edge_mixed_population_resolution_reads_spine_deactivated_target_resolves(
    tmp_path: Path,
) -> None:
    """A mixed (per-sub-type) device election dispatches per row on device's
    own records-spine discriminator (never a fold after-image): ord_a's edge
    to dev_day resolves its elected presentation_id, while ord_b's edge to
    the deactivated dev_night still resolves its elected record_index value,
    digit-rendered in the shared VARCHAR column. device is never declared as
    its own `tables[]` output here — a mixed per-population election is only
    reachable for a target no output table itself demands a uniform surface
    for."""
    emit_dir = build_source_election_emit(tmp_path)
    config = _config(tables=(SourceTableDecl(name="orders", kind="order"),))
    table, rows = _rows_for(
        emit_dir,
        config,
        {"device": {"day": "presentation_id", "night": "record_index"}},
    )
    assert isinstance(table, SourceStateTablePlan)
    edge = table.edge_surfaces[0]
    by_id = {r["id"]: r for r in rows}
    assert edge.rendered_type == "VARCHAR"
    assert by_id["ord_a"]["device_id"] == "DAY_001"
    assert by_id["ord_b"]["device_id"] == "1"


# ---------------------------------------------------------------------------
# Edge rendering: junction owner column, mixed-kind member column
# ---------------------------------------------------------------------------


def test_junction_owner_column_follows_owner_election(tmp_path: Path) -> None:
    """membership__order__watchers' owner column (order_id) follows order's
    own election."""
    emit_dir = build_source_election_emit(tmp_path)
    config = _config(
        tables=(
            SourceTableDecl(
                name="watchers",
                membership=MembershipRef(kind="order", property="watchers"),
            ),
        )
    )
    _, rows = _rows_for(emit_dir, config, {"order": "presentation_id"})
    owner_ids = {r["order_id"] for r in rows}
    assert owner_ids == {"ORD_001", "ORD_002"}


def test_junction_member_mixed_kind_renders_per_row_varchar_with_kind_disambiguator(
    tmp_path: Path,
) -> None:
    """The member field admits both known kinds (device, order); electing
    device presentation_id and order record_index renders one shared VARCHAR
    column: a presentation code beside a digit-rendered record_index,
    disambiguated by the `<f>_kind` column."""
    emit_dir = build_source_election_emit(tmp_path)
    config = _config(
        tables=(
            SourceTableDecl(
                name="watchers",
                membership=MembershipRef(kind="order", property="watchers"),
            ),
        )
    )
    table, rows = _rows_for(
        emit_dir, config, {"device": "presentation_id", "order": "record_index"}
    )
    assert isinstance(table, SourceJunctionTablePlan)
    member_edge = next(e for e in table.edge_surfaces if e.source_column != "record_id")
    by_kind = {r["party_kind"]: r["party_id"] for r in rows}
    assert member_edge.rendered_type == "VARCHAR"
    assert by_kind["device"] == "DAY_001"
    assert by_kind["order"] == "0"


# ---------------------------------------------------------------------------
# Event log: elected item_id
# ---------------------------------------------------------------------------


def test_event_log_item_id_renders_elected_presentation_id(tmp_path: Path) -> None:
    """The event log's `item_id` column renders device's elected
    presentation_id codes, including on dev_night's 'destroy' row (its
    identity is read off the fold's own record_id, never the nulled
    after-image)."""
    emit_dir = build_source_election_emit(tmp_path)
    config = _config(
        events=SourceEventsDecl(
            name="log", sources=(SourceEventSourceDecl(kind="device"),)
        )
    )
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, {"device": "presentation_id"})
        plan = build_source_plan(
            emit, config, anchor, election, False, discard_notice_sink
        )
        assert plan.events is not None
        assert plan.events.item_id_type == "VARCHAR"
        sql = build_event_log_sql(
            plan.sidecar, fork_path, plan.events, plan.anchor, None
        )
        rows = emit.query(sql, ())
    item_ids = {row[2] for row in rows}
    assert item_ids == {"DAY_001", "NIGHT_001"}


def test_event_log_item_id_renders_elected_record_index_cast(tmp_path: Path) -> None:
    """A record_index election casts the event log's `item_id` column to
    BIGINT — the log-wide type-rule verdict, resolved over the union of
    every source's item_surface."""
    emit_dir = build_source_election_emit(tmp_path)
    config = _config(
        events=SourceEventsDecl(
            name="log", sources=(SourceEventSourceDecl(kind="device"),)
        )
    )
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, {"device": "record_index"})
        plan = build_source_plan(
            emit, config, anchor, election, False, discard_notice_sink
        )
        assert plan.events is not None
        assert plan.events.item_id_type == "BIGINT"
        sql = build_event_log_sql(
            plan.sidecar, fork_path, plan.events, plan.anchor, None
        )
        rows = emit.query(sql, ())
    item_ids = {row[2] for row in rows}
    assert item_ids == {0, 1}
    assert all(isinstance(v, int) for v in item_ids)


# ---------------------------------------------------------------------------
# Plan-time elected-key uniqueness guard
# ---------------------------------------------------------------------------


def test_plan_guard_catches_corrupted_device_self_identity(tmp_path: Path) -> None:
    """A corrupted self-identity presentation_id (dev_day/dev_night sharing
    one value) fails `build_source_plan` before any render or write."""
    emit_dir = build_source_election_emit(tmp_path, corrupt_device=True)
    config = _config(tables=(SourceTableDecl(name="device", kind="device"),))
    with pytest.raises(ElectedKeyDuplicate):
        _open_plan(emit_dir, config, {"device": "presentation_id"})


def test_plan_guard_catches_corrupted_reference_edge_target(tmp_path: Path) -> None:
    """A corrupted edge-target presentation_id fails `build_source_plan` —
    isolated from device's own self-identity guard by never declaring a
    `tables[]` output for device (omission-as-exclusion)."""
    emit_dir = build_source_election_emit(tmp_path, corrupt_device=True)
    config = _config(tables=(SourceTableDecl(name="orders", kind="order"),))
    with pytest.raises(ElectedKeyDuplicate):
        _open_plan(emit_dir, config, {"device": "presentation_id"})


def test_plan_guard_catches_corrupted_junction_member_target(tmp_path: Path) -> None:
    """The junction member-edge guard catches a corrupted target reachable
    only through the member field's closed-kind universe — no
    reference-annotated `prop__` column anywhere touches the target kind."""
    emit_dir = build_corrupted_junction_member_emit(tmp_path)
    config = _config(
        tables=(
            SourceTableDecl(
                name="watchers",
                membership=MembershipRef(kind="team", property="watchers"),
            ),
        )
    )
    with pytest.raises(ElectedKeyDuplicate):
        _open_plan(emit_dir, config, {"device": "presentation_id"})


def test_corrupted_key_fails_before_any_writer_runs(tmp_path: Path) -> None:
    """`export_source` raises on a corrupted elected key and writes no
    output at all — the plan-time guard runs inside `build_source_plan`,
    before `export_source` compiles or writes anything."""
    emit_dir = build_source_election_emit(tmp_path, corrupt_device=True)
    config = _config(
        tables=(SourceTableDecl(name="device", kind="device"),),
    )
    config = ExportConfig(
        mode="source", source=config.source, keys={"device": "presentation_id"}
    )
    out_path = tmp_path / "out.duckdb"
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        with pytest.raises(ElectedKeyDuplicate):
            export_source(emit, config, out_path, "duckdb", anchor, discard_notice_sink)
    assert not out_path.exists()


# ---------------------------------------------------------------------------
# Split-unit identity guard, restricted to the sub_type spine
# ---------------------------------------------------------------------------


def test_split_unit_guard_restricted_to_own_subtype_spine_ignores_cross_population(
    tmp_path: Path,
) -> None:
    """consultant's c1 and nurse's n1 share one presentation_id value — a
    cross-population coincidence each declared table's own restricted-spine
    guard does not catch, since consultant and nurse are separate output
    tables."""
    emit_dir = build_split_actor_presentation_id_emit(tmp_path)
    config = _config(
        tables=(
            SourceTableDecl(name="consultant", kind="actor", sub_types=("consultant",)),
            SourceTableDecl(name="nurse", kind="actor", sub_types=("nurse",)),
        )
    )
    plan = _open_plan(
        emit_dir,
        config,
        keys={"actor": {"consultant": "presentation_id", "nurse": "presentation_id"}},
    )
    assert {t.name for t in plan.tables} == {"consultant", "nurse"}


def test_split_unit_guard_catches_duplicate_within_own_subtype_spine(
    tmp_path: Path,
) -> None:
    """A genuine duplicate within consultant's own spine (c1/c2 sharing one
    value) still fails, despite the spine restriction."""
    emit_dir = build_split_actor_presentation_id_emit(
        tmp_path, duplicate_within_consultant=True
    )
    config = _config(
        tables=(
            SourceTableDecl(name="consultant", kind="actor", sub_types=("consultant",)),
        )
    )
    with pytest.raises(ElectedKeyDuplicate):
        _open_plan(emit_dir, config, keys={"actor": {"consultant": "presentation_id"}})
