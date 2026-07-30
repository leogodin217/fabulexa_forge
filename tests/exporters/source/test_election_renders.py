"""Tests for source-mode key election at render time: the self identity
value surface per genre (reference/transaction, change-log post-fold join,
snapshot delivery), the fold's own row-state-events SQL untouched by
election, per-edge target rendering (uniform + mixed per-row population
resolution reading the records-spine discriminator), the junction owner and
member-kind edge columns, and the engine's render-time uniqueness guard
(exporters/source/renders.py, exporters/source/engine.py).

Renders are built directly via build_source_plan + the render builders
(bypassing the engine) when only the render SQL matters; the guard section
goes through build_source_query_specs / export_source, since the guard is an
engine-level concern.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _support.notices import discard_notice_sink

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import ExcludeDecl, ExportConfig, SourceConfig
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.derivations.row_state_events import build_row_state_events_sql
from fabulexa_forge.errors import ElectedKeyDuplicate
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.source.engine import (
    build_source_query_specs,
    export_source,
)
from fabulexa_forge.exporters.source.plan import SourceTableSpec, build_source_plan
from fabulexa_forge.exporters.source.renders import (
    build_changelog_render_sql,
    build_junction_render_sql,
    build_records_render_sql,
    build_snapshot_render_sql,
)
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import Emit, open_emit

from ._source_fixtures import (
    build_corrupted_junction_member_emit,
    build_source_election_emit,
    build_split_actor_presentation_id_emit,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec_for(specs: tuple[SourceTableSpec, ...], source_table: str) -> SourceTableSpec:
    """Return the sole spec matching source_table (assumed unsplit)."""
    return next(s for s in specs if s.source_table == source_table)


def _col_map(spec: SourceTableSpec, row: tuple[object, ...]) -> dict[str, object]:
    """Zip a result row against spec.columns' output names."""
    return {out: value for (_, out), value in zip(spec.columns, row)}


def _mapped_rows(
    emit: Emit, spec: SourceTableSpec, sql: str
) -> list[dict[str, object]]:
    """Execute sql and zip every row against spec.columns' output names."""
    return [_col_map(spec, row) for row in emit.query(sql, ())]


# ---------------------------------------------------------------------------
# Self identity: change-log (post-fold join, populated on 'd' rows)
# ---------------------------------------------------------------------------


def test_changelog_self_identity_presentation_id_populated_on_d_row(
    tmp_path: Path,
) -> None:
    """device's own change-log export: presentation_id election populates the
    'id' column on dev_night's 'd' row, superseding the fold's own
    NULL-on-d after-image."""
    emit_dir = build_source_election_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, {"device": "presentation_id"})
        specs = build_source_plan(
            emit.sidecar, None, discard_notice_sink, election=election
        )
        spec = _spec_for(specs, "records__device")
        sql = build_changelog_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
        d_row = next(r for r in rows if r["op"] == "d")
    assert d_row["id"] == "NIGHT_001"


def test_changelog_self_identity_record_index_bigint_keeps_standalone_presentation_id(
    tmp_path: Path,
) -> None:
    """record_index election renders the 'id' column BIGINT, populated on the
    'd' row; the standalone presentation_id payload column (not absorbed) is
    unaffected — still the fold's own after-image, NULL on d."""
    emit_dir = build_source_election_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, {"device": "record_index"})
        specs = build_source_plan(
            emit.sidecar, None, discard_notice_sink, election=election
        )
        spec = _spec_for(specs, "records__device")
        assert dict(spec.columns)["presentation_id"] == "presentation_id"
        sql = build_changelog_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
        d_row = next(r for r in rows if r["op"] == "d")
        c_row = next(r for r in rows if r["op"] == "c" and r["id"] == 1)
    assert d_row["id"] == 1
    assert isinstance(d_row["id"], int)
    assert d_row["presentation_id"] is None
    assert c_row["presentation_id"] == "NIGHT_001"


def test_changelog_fold_subquery_byte_identical_across_elections(
    tmp_path: Path,
) -> None:
    """The fold's own row-state-events SQL is unaffected by election: the
    exact string `build_row_state_events_sql` composes appears verbatim in
    both the default and the presentation_id-elected change-log render."""
    emit_dir = build_source_election_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None

        default_specs = build_source_plan(emit.sidecar, None, discard_notice_sink)
        default_spec = _spec_for(default_specs, "records__device")
        default_sql = build_changelog_render_sql(
            emit.sidecar, fork_path, default_spec, anchor, None
        )

        election = resolve_election(emit.sidecar, {"device": "presentation_id"})
        elected_specs = build_source_plan(
            emit.sidecar, None, discard_notice_sink, election=election
        )
        elected_spec = _spec_for(elected_specs, "records__device")
        elected_sql = build_changelog_render_sql(
            emit.sidecar, fork_path, elected_spec, anchor, None
        )

        fold_sql = build_row_state_events_sql(
            emit.sidecar, fork_path, "device", frozenset({"device_type", "status"})
        )
    assert fold_sql in default_sql
    assert fold_sql in elected_sql


# ---------------------------------------------------------------------------
# Self identity: reference/transaction, snapshot delivery
# ---------------------------------------------------------------------------


def test_reference_self_identity_presentation_id_renders_elected_value(
    tmp_path: Path,
) -> None:
    """order's (transaction genre) own 'id' column renders the elected
    presentation_id codes."""
    emit_dir = build_source_election_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, {"order": "presentation_id"})
        specs = build_source_plan(
            emit.sidecar, None, discard_notice_sink, election=election
        )
        spec = _spec_for(specs, "records__order")
        sql = build_records_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
        ids = {r["id"] for r in rows}
    assert ids == {"ORD_001", "ORD_002"}


def test_snapshot_self_identity_presentation_id_renders_elected_value(
    tmp_path: Path,
) -> None:
    """Under change_delivery: snapshot, device's state-at render's 'id'
    column renders the elected presentation_id codes at the tape's end —
    including dev_night, deactivated but still present in the horizon
    reconstruction."""
    emit_dir = build_source_election_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        config = SourceConfig(change_delivery="snapshot")
        election = resolve_election(emit.sidecar, {"device": "presentation_id"})
        specs = build_source_plan(
            emit.sidecar, config, discard_notice_sink, election=election
        )
        spec = _spec_for(specs, "records__device")
        sql = build_snapshot_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
        ids = {r["id"] for r in rows}
    assert ids == {"DAY_001", "NIGHT_001"}


# ---------------------------------------------------------------------------
# Edge rendering: reference-annotated prop__ column
# ---------------------------------------------------------------------------


def test_reference_edge_uniform_presentation_id_renders_target_codes(
    tmp_path: Path,
) -> None:
    """order.device_id renders device's elected presentation_id codes at the
    table's horizon."""
    emit_dir = build_source_election_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, {"device": "presentation_id"})
        specs = build_source_plan(
            emit.sidecar, None, discard_notice_sink, election=election
        )
        spec = _spec_for(specs, "records__order")
        edge = spec.edge_surfaces[0]
        sql = build_records_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
        by_id = {r["id"]: r for r in rows}
    assert edge.rendered_type == "VARCHAR"
    assert by_id["ord_a"]["device_id"] == "DAY_001"
    assert by_id["ord_b"]["device_id"] == "NIGHT_001"


def test_reference_edge_uniform_record_index_renders_bigint(tmp_path: Path) -> None:
    """order.device_id renders device's elected record_index, BIGINT-typed."""
    emit_dir = build_source_election_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, {"device": "record_index"})
        specs = build_source_plan(
            emit.sidecar, None, discard_notice_sink, election=election
        )
        spec = _spec_for(specs, "records__order")
        edge = spec.edge_surfaces[0]
        sql = build_records_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
        by_id = {r["id"]: r for r in rows}
    assert edge.rendered_type == "BIGINT"
    assert by_id["ord_a"]["device_id"] == 0
    assert by_id["ord_b"]["device_id"] == 1


def test_edge_mixed_population_resolution_reads_spine_deactivated_target_resolves(
    tmp_path: Path,
) -> None:
    """A mixed (per-sub-type) device election dispatches per row on device's
    own records-spine discriminator (never a fold after-image): ord_b's edge
    to the deactivated dev_night still resolves its elected record_index
    value, digit-rendered in the shared VARCHAR column. device is excluded
    from its own output — a mixed per-population election is only reachable
    for an excluded (edges-only) target; an included unsplit kind's own
    identity gate requires one uniform surface."""
    emit_dir = build_source_election_emit(tmp_path)
    config = SourceConfig(exclude=ExcludeDecl(kinds=["device"]))
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(
            emit.sidecar,
            {"device": {"day": "presentation_id", "night": "record_index"}},
        )
        specs = build_source_plan(
            emit.sidecar, config, discard_notice_sink, election=election
        )
        spec = _spec_for(specs, "records__order")
        edge = spec.edge_surfaces[0]
        sql = build_records_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
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
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, {"order": "presentation_id"})
        specs = build_source_plan(
            emit.sidecar, None, discard_notice_sink, election=election
        )
        spec = next(s for s in specs if s.genre == "junction")
        sql = build_junction_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
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
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(
            emit.sidecar, {"device": "presentation_id", "order": "record_index"}
        )
        specs = build_source_plan(
            emit.sidecar, None, discard_notice_sink, election=election
        )
        spec = next(s for s in specs if s.genre == "junction")
        member_edge = next(
            e for e in spec.edge_surfaces if e.source_column != "record_id"
        )
        sql = build_junction_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
        by_kind = {r["party_kind"]: r["party_id"] for r in rows}
    assert member_edge.rendered_type == "VARCHAR"
    assert by_kind["device"] == "DAY_001"
    assert by_kind["order"] == "0"


# ---------------------------------------------------------------------------
# Engine: the render-time uniqueness guard
# ---------------------------------------------------------------------------


def test_self_identity_guard_catches_corrupted_device_presentation_id(
    tmp_path: Path,
) -> None:
    """A corrupted self-identity presentation_id (dev_day/dev_night sharing
    one value) fails build_source_query_specs before any writer runs."""
    emit_dir = build_source_election_emit(tmp_path, corrupt_device=True)
    config = ExportConfig(mode="source", keys={"device": "presentation_id"})
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        with pytest.raises(ElectedKeyDuplicate):
            build_source_query_specs(
                emit, config, anchor, None, discard_notice_sink, base_relations=None
            )


def test_reference_edge_guard_catches_corrupted_edge_target(tmp_path: Path) -> None:
    """A corrupted edge-target presentation_id fails build_source_query_specs
    before any writer runs — isolated from device's own self-identity guard
    by excluding device from its own output."""
    emit_dir = build_source_election_emit(tmp_path, corrupt_device=True)
    config = ExportConfig(
        mode="source",
        source=SourceConfig(exclude=ExcludeDecl(kinds=["device"])),
        keys={"device": "presentation_id"},
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        with pytest.raises(ElectedKeyDuplicate):
            build_source_query_specs(
                emit, config, anchor, None, discard_notice_sink, base_relations=None
            )


def test_junction_member_guard_catches_corrupted_target(tmp_path: Path) -> None:
    """The junction member-edge guard catches a corrupted target reachable
    only through the member field's closed-kind universe — no
    reference-annotated column touches the target kind at all."""
    emit_dir = build_corrupted_junction_member_emit(tmp_path)
    config = ExportConfig(
        mode="source",
        source=SourceConfig(exclude=ExcludeDecl(kinds=["device"])),
        keys={"device": "presentation_id"},
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        with pytest.raises(ElectedKeyDuplicate):
            build_source_query_specs(
                emit, config, anchor, None, discard_notice_sink, base_relations=None
            )


def test_corrupted_key_fails_before_any_writer_runs(tmp_path: Path) -> None:
    """export_source raises on a corrupted elected key and writes no output
    at all — the guard runs before build_source_query_specs returns."""
    emit_dir = build_source_election_emit(tmp_path, corrupt_device=True)
    config = ExportConfig(mode="source", keys={"device": "presentation_id"})
    out_path = tmp_path / "out.duckdb"
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        with pytest.raises(ElectedKeyDuplicate):
            export_source(emit, config, out_path, "duckdb", anchor, discard_notice_sink)
    assert not out_path.exists()


def test_per_window_guard_fires_for_corrupted_key(tmp_path: Path) -> None:
    """An incremental (windowed) invocation still guards the elected key,
    labeling the failure with the window's display label."""
    emit_dir = build_source_election_emit(tmp_path, corrupt_device=True)
    config = ExportConfig(mode="source", keys={"device": "presentation_id"})
    window = Window(index=0, start_ns=0, end_ns=50_000_000, label="w0")
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        with pytest.raises(ElectedKeyDuplicate, match=r"\(w0\)"):
            build_source_query_specs(
                emit, config, anchor, window, discard_notice_sink, base_relations=None
            )


# ---------------------------------------------------------------------------
# Engine: split-unit identity guard, restricted to the sub_type spine
# ---------------------------------------------------------------------------


def test_split_unit_guard_restricted_to_own_subtype_spine_ignores_cross_population(
    tmp_path: Path,
) -> None:
    """consultant's c1 and nurse's n1 share one presentation_id value —
    a cross-population coincidence each sub-type's own restricted-spine
    guard does not catch, since consultant and nurse are separate output
    tables."""
    emit_dir = build_split_actor_presentation_id_emit(tmp_path)
    config = ExportConfig(
        mode="source",
        keys={"actor": {"consultant": "presentation_id", "nurse": "presentation_id"}},
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        specs = build_source_query_specs(
            emit, config, anchor, None, discard_notice_sink, base_relations=None
        )
    assert {spec.table_name for spec in specs} == {"consultant", "nurse"}


def test_split_unit_guard_catches_duplicate_within_own_subtype_spine(
    tmp_path: Path,
) -> None:
    """A genuine duplicate within consultant's own spine (c1/c2 sharing one
    value) still fails, despite the spine restriction."""
    emit_dir = build_split_actor_presentation_id_emit(
        tmp_path, duplicate_within_consultant=True
    )
    config = ExportConfig(
        mode="source", keys={"actor": {"consultant": "presentation_id"}}
    )
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        with pytest.raises(ElectedKeyDuplicate):
            build_source_query_specs(
                emit, config, anchor, None, discard_notice_sink, base_relations=None
            )
