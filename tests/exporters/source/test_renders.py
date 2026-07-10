"""Tests for the four genre render SQL builders.

Runs each render's SQL directly against the DuckDB-backed spanning fixture
(`_source_fixtures.build_source_test_emit`), asserting the fold/relation
composition, wallclock rendering, CAST-back typing, and per-genre ordering the
design doc specifies. `window=None` call sites exercise the full-export
contract (byte-identical to Unit 1); the windowed fixture
(`_source_fixtures.build_windowed_source_test_emit`) exercises per-genre
window membership: change-log by `event_sim_time`, transaction by
`last_mutation_sim_time`, reference's unconditional full snapshot, and
junction extract-on-change with `left_at` horizon-masking. Also covers
`build_snapshot_render_sql` (change_delivery: snapshot, Unit 3): composing
`build_state_at_sql` at `window.end_ns`, omitting `updated_at`, and
horizon-rendering `active`/`deactivated_at`.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from fabulexa_forge.anchor import (
    EffectiveAnchor,
    render_anchor_timestamp_expr,
    resolve_effective_anchor,
)
from fabulexa_forge.config.models import SourceConfig
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.derivations.state_at import build_state_at_sql
from fabulexa_forge.exporters.source.plan import SourceTableSpec, build_source_plan
from fabulexa_forge.exporters.source.renders import (
    build_changelog_render_sql,
    build_junction_render_sql,
    build_records_render_sql,
    build_render_sql,
    build_snapshot_render_sql,
)
from fabulexa_forge.reader.emit import Emit, open_emit

from ._source_fixtures import (
    build_source_test_emit,
    build_windowed_source_test_emit,
    windowed_test_windows,
)


@contextmanager
def _spanning_emit(
    tmp_path: Path,
) -> Iterator[tuple[Emit, tuple[SourceTableSpec, ...], str, EffectiveAnchor]]:
    """Open the spanning fixture emit and resolve its plan, fork_path, and anchor."""
    emit_dir = build_source_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        specs = build_source_plan(emit.sidecar, None)
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        yield emit, specs, fork_path, anchor


@contextmanager
def _windowed_emit(
    tmp_path: Path,
    config: "SourceConfig | None" = None,
) -> Iterator[tuple[Emit, tuple[SourceTableSpec, ...], str, EffectiveAnchor]]:
    """Open the windowed fixture emit and resolve its plan, fork_path, and anchor.

    Args:
        tmp_path: Directory to write the emit artifacts into.
        config: The source config to resolve the plan under, or None for the
            bare-mode defaults (change_delivery='changelog').
    """
    emit_dir = build_windowed_source_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        fork_path = require_single_branch(emit.sidecar)
        specs = build_source_plan(emit.sidecar, config)
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        yield emit, specs, fork_path, anchor


def _spec_for(specs: tuple[SourceTableSpec, ...], source_table: str) -> SourceTableSpec:
    """Return the sole spec matching source_table (assumed unsplit)."""
    return next(s for s in specs if s.source_table == source_table)


def _spec_for_subtype(
    specs: tuple[SourceTableSpec, ...], source_table: str, sub_type: str
) -> SourceTableSpec:
    """Return the spec matching (source_table, sub_type)."""
    return next(
        s for s in specs if s.source_table == source_table and s.sub_type == sub_type
    )


def _col_map(spec: SourceTableSpec, row: tuple[object, ...]) -> dict[str, object]:
    """Zip a result row against spec.columns' output names."""
    return {out: value for (_, out), value in zip(spec.columns, row)}


def _mapped_rows(
    emit: Emit, spec: SourceTableSpec, sql: str
) -> list[dict[str, object]]:
    """Execute sql and zip every row against spec.columns' output names."""
    return [_col_map(spec, row) for row in emit.query(sql, ())]


# ---------------------------------------------------------------------------
# Change-log render
# ---------------------------------------------------------------------------


def test_changelog_render_ops_and_ordering(tmp_path: Path) -> None:
    """One 'c' per record, one coalesced 'u', one 'd'; ordered by (time, class, id)."""
    with _spanning_emit(tmp_path) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "records__visit")
        sql = build_changelog_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
        assert [(r["id"], r["op"]) for r in rows] == [
            ("v001", "c"),
            ("v002", "c"),
            ("v003", "c"),
            ("v002", "u"),
            ("v003", "d"),
        ]


def test_changelog_render_delete_payload_null(tmp_path: Path) -> None:
    """A 'd' row carries NULL presentation_id and NULL payload columns."""
    with _spanning_emit(tmp_path) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "records__visit")
        sql = build_changelog_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
        delete_row = next(r for r in rows if r["op"] == "d")
        assert delete_row["id"] == "v003"
        assert delete_row["presentation_id"] is None
        assert delete_row["status"] is None
        assert delete_row["priority"] is None


def test_changelog_render_coincident_changes_coalesced(tmp_path: Path) -> None:
    """v002's coincident status+priority change folds into exactly one 'u' row."""
    with _spanning_emit(tmp_path) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "records__visit")
        sql = build_changelog_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
        update_rows = [r for r in rows if r["op"] == "u" and r["id"] == "v002"]
        assert len(update_rows) == 1
        assert update_rows[0]["status"] == "closed"
        assert update_rows[0]["priority"] == 5


def test_changelog_render_casts_back_to_sidecar_types(tmp_path: Path) -> None:
    """Payload/presentation_id columns are typed per sidecar type, not VARCHAR."""
    with _spanning_emit(tmp_path) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "records__visit")
        sql = build_changelog_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
        create_row = next(r for r in rows if r["op"] == "c" and r["id"] == "v001")
        assert isinstance(create_row["presentation_id"], int)
        assert create_row["status"] == "open"
        # priority is history-tracked; its ASOF-joined after-image only resolves
        # once a history row exists at or before the event (v002's coalesced 'u').
        update_row = next(r for r in rows if r["op"] == "u" and r["id"] == "v002")
        assert isinstance(update_row["priority"], int)
        assert update_row["priority"] == 5


def test_changelog_render_wallclock_changed_at(tmp_path: Path) -> None:
    """changed_at renders wallclock through the shared anchor renderer."""
    with _spanning_emit(tmp_path) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "records__visit")
        sql = build_changelog_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
        create_row = next(r for r in rows if r["op"] == "c" and r["id"] == "v001")
        assert "2024-01-01" in str(create_row["changed_at"])


def test_changelog_never_split_tracked_subtyped(tmp_path: Path) -> None:
    """A tracked sub-typed kind is one changelog table; discriminator retained."""
    with _spanning_emit(tmp_path) as (emit, specs, fork_path, anchor):
        shift_specs = [s for s in specs if s.source_table == "records__shift"]
        assert len(shift_specs) == 1
        spec = shift_specs[0]
        assert spec.sub_type is None

        sql = build_changelog_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
        create_row = next(r for r in rows if r["op"] == "c")
        delete_row = next(r for r in rows if r["op"] == "d")
        assert create_row["shift_type"] == "day"
        assert delete_row["shift_type"] is None


def test_changelog_render_uses_shared_anchor_renderer(tmp_path: Path) -> None:
    """changed_at's rendering is byte-identical to render_anchor_timestamp_expr."""
    with _spanning_emit(tmp_path) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "records__visit")
        sql = build_changelog_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        expected = render_anchor_timestamp_expr(
            anchor, '"_fold"."event_sim_time"', "changed_at"
        )
        assert expected in sql
        order_clause = sql.split("ORDER BY", 1)[1]
        assert '"_fold"."event_sim_time"' in order_clause
        assert "changed_at" not in order_clause


# ---------------------------------------------------------------------------
# Reference / transaction render
# ---------------------------------------------------------------------------


def test_reference_render_deactivated_at_null_iff_active(tmp_path: Path) -> None:
    """deactivated_at is NULL exactly for the active record; fork_path dropped."""
    with _spanning_emit(tmp_path) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "records__location")
        sql = build_records_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = {r["id"]: r for r in _mapped_rows(emit, spec, sql)}
        assert rows["loc001"]["active"] is True
        assert rows["loc001"]["deactivated_at"] is None
        assert rows["loc002"]["active"] is False
        assert "2024-01-01" in str(rows["loc002"]["deactivated_at"])
        assert "fork_path" not in rows["loc001"]


def test_reference_render_reference_column_id_only_unjoined(tmp_path: Path) -> None:
    """A reference-annotated prop__ column lands verbatim, id-only, unjoined."""
    with _spanning_emit(tmp_path) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "records__order")
        sql = build_records_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
        assert rows[0]["location_id"] == "loc001"


def test_reference_render_split_unit_discriminator_filtered(tmp_path: Path) -> None:
    """A split unit's query is filtered to its sub-type; discriminator dropped."""
    with _spanning_emit(tmp_path) as (emit, specs, fork_path, anchor):
        consultant_spec = _spec_for_subtype(specs, "records__actor", "consultant")
        sql = build_records_render_sql(
            emit.sidecar, fork_path, consultant_spec, anchor, None
        )
        rows = _mapped_rows(emit, consultant_spec, sql)
        assert len(rows) == 1
        assert rows[0]["id"] == "act001"
        assert "actor_type" not in rows[0]


def test_records_render_uses_shared_anchor_renderer_and_raw_ordering(
    tmp_path: Path,
) -> None:
    """created_at renders through the shared renderer; ORDER BY is raw sim-time."""
    with _spanning_emit(tmp_path) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "records__location")
        sql = build_records_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        expected = render_anchor_timestamp_expr(
            anchor, '"_rec"."created_sim_time"', "created_at"
        )
        assert expected in sql
        order_clause = sql.split("ORDER BY", 1)[1]
        assert '"_rec"."created_sim_time"' in order_clause
        assert "created_at" not in order_clause


# ---------------------------------------------------------------------------
# Junction render
# ---------------------------------------------------------------------------


def test_junction_render_naming_and_open_interval(tmp_path: Path) -> None:
    """record_id-><K>_id; left_at NULL while open; elem__/member__ projected."""
    with _spanning_emit(tmp_path) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "membership__visit__team")
        sql = build_junction_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        rows = _mapped_rows(emit, spec, sql)
        assert len(rows) == 2
        for row in rows:
            assert row["visit_id"] == "v001"
        closed = next(r for r in rows if r["role_name"] == "lead")
        still_open = next(r for r in rows if r["role_name"] == "support")
        assert closed["left_at"] is not None
        assert still_open["left_at"] is None
        assert "2024-01-01" in str(closed["joined_at"])
        assert closed["actor_kind"] == "actor"
        assert closed["actor_id"] == "act001"
        assert still_open["actor_id"] == "act002"


def test_junction_render_uses_shared_anchor_renderer_and_raw_ordering(
    tmp_path: Path,
) -> None:
    """joined_at renders through the shared renderer; ORDER BY is raw sim-time."""
    with _spanning_emit(tmp_path) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "membership__visit__team")
        sql = build_junction_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        expected = render_anchor_timestamp_expr(
            anchor, '"_mem"."joined_sim_time"', "joined_at"
        )
        assert expected in sql
        order_clause = sql.split("ORDER BY", 1)[1]
        assert '"_mem"."joined_sim_time"' in order_clause
        assert "joined_at" not in order_clause


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_build_render_sql_dispatches_per_genre(tmp_path: Path) -> None:
    """build_render_sql routes each genre to its own render builder."""
    with _spanning_emit(tmp_path) as (emit, specs, fork_path, anchor):
        changelog_spec = _spec_for(specs, "records__visit")
        reference_spec = _spec_for(specs, "records__location")
        junction_spec = _spec_for(specs, "membership__visit__team")

        assert build_render_sql(
            emit.sidecar, fork_path, changelog_spec, anchor, None
        ) == build_changelog_render_sql(
            emit.sidecar, fork_path, changelog_spec, anchor, None
        )
        assert build_render_sql(
            emit.sidecar, fork_path, reference_spec, anchor, None
        ) == build_records_render_sql(
            emit.sidecar, fork_path, reference_spec, anchor, None
        )
        assert build_render_sql(
            emit.sidecar, fork_path, junction_spec, anchor, None
        ) == build_junction_render_sql(
            emit.sidecar, fork_path, junction_spec, anchor, None
        )


# ---------------------------------------------------------------------------
# Windowed rendering (Unit 2)
# ---------------------------------------------------------------------------


def test_changelog_render_windowed_filters_by_event_sim_time(tmp_path: Path) -> None:
    """change-log render filters to event_sim_time in [window.start_ns, window.end_ns)."""
    w0, w1, w2 = windowed_test_windows()
    with _windowed_emit(tmp_path) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "records__visit")
        rows_w0 = _mapped_rows(
            emit,
            spec,
            build_changelog_render_sql(emit.sidecar, fork_path, spec, anchor, w0),
        )
        rows_w1 = _mapped_rows(
            emit,
            spec,
            build_changelog_render_sql(emit.sidecar, fork_path, spec, anchor, w1),
        )
        rows_w2 = _mapped_rows(
            emit,
            spec,
            build_changelog_render_sql(emit.sidecar, fork_path, spec, anchor, w2),
        )
    assert [(r["id"], r["op"]) for r in rows_w0] == [("v001", "c")]
    assert [(r["id"], r["op"]) for r in rows_w1] == [("v001", "u"), ("v002", "c")]
    assert [(r["id"], r["op"]) for r in rows_w2] == [("v003", "c"), ("v002", "d")]


def test_records_render_windowed_transaction_filters_by_last_mutation_sim_time(
    tmp_path: Path,
) -> None:
    """Transaction render filters to last_mutation_sim_time in the window."""
    w0, w1, w2 = windowed_test_windows()
    with _windowed_emit(tmp_path) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "records__order")
        ids_w0 = {
            r["id"]
            for r in _mapped_rows(
                emit,
                spec,
                build_records_render_sql(emit.sidecar, fork_path, spec, anchor, w0),
            )
        }
        ids_w1 = {
            r["id"]
            for r in _mapped_rows(
                emit,
                spec,
                build_records_render_sql(emit.sidecar, fork_path, spec, anchor, w1),
            )
        }
        ids_w2 = {
            r["id"]
            for r in _mapped_rows(
                emit,
                spec,
                build_records_render_sql(emit.sidecar, fork_path, spec, anchor, w2),
            )
        }
    assert ids_w0 == {"ord001"}
    assert ids_w1 == {"ord002"}
    assert ids_w2 == {"ord003"}


def test_records_render_windowed_reference_full_snapshot_every_window(
    tmp_path: Path,
) -> None:
    """Reference render carries no predicate: same full snapshot every window."""
    w0, w1, w2 = windowed_test_windows()
    with _windowed_emit(tmp_path) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "records__location")
        full_sql = build_records_render_sql(emit.sidecar, fork_path, spec, anchor, None)
        full_rows = _mapped_rows(emit, spec, full_sql)
        for window in (w0, w1, w2):
            sql = build_records_render_sql(
                emit.sidecar, fork_path, spec, anchor, window
            )
            assert sql == full_sql
            assert _mapped_rows(emit, spec, sql) == full_rows


def test_junction_render_windowed_extract_on_change(tmp_path: Path) -> None:
    """Junction render extracts-on-change: join-only masks left_at, a later leave
    re-emits it set, a same-window join+leave emits one closed row, and an
    interval touching neither bound in a window emits no row for it.
    """
    w0, w1, w2 = windowed_test_windows()
    with _windowed_emit(tmp_path) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "membership__visit__team")
        rows_w0 = {
            r["visit_id"]: r
            for r in _mapped_rows(
                emit,
                spec,
                build_junction_render_sql(emit.sidecar, fork_path, spec, anchor, w0),
            )
        }
        rows_w1 = {
            r["visit_id"]: r
            for r in _mapped_rows(
                emit,
                spec,
                build_junction_render_sql(emit.sidecar, fork_path, spec, anchor, w1),
            )
        }
        rows_w2 = {
            r["visit_id"]: r
            for r in _mapped_rows(
                emit,
                spec,
                build_junction_render_sql(emit.sidecar, fork_path, spec, anchor, w2),
            )
        }

    # w0: m_A (v001) and m_C (v002) both join-only here; left_at masked.
    assert set(rows_w0) == {"v001", "v002"}
    assert rows_w0["v001"]["left_at"] is None
    assert rows_w0["v002"]["left_at"] is None

    # w1: m_A (v001) leaves here -> re-emitted with left_at set. m_C (v002)
    # never leaves -> no row this window.
    assert set(rows_w1) == {"v001"}
    assert rows_w1["v001"]["left_at"] is not None

    # w2: m_B (v003) joins and leaves within this one window -> one closed
    # row. m_C (v002) still open -> no row.
    assert set(rows_w2) == {"v003"}
    assert rows_w2["v003"]["left_at"] is not None


# ---------------------------------------------------------------------------
# Snapshot render (change_delivery: snapshot, Unit 3)
# ---------------------------------------------------------------------------

_SNAPSHOT_CONFIG = SourceConfig(change_delivery="snapshot")


def test_snapshot_render_composes_build_state_at_sql(tmp_path: Path) -> None:
    """The snapshot render composes build_state_at_sql at window.end_ns."""
    w0, _, _ = windowed_test_windows()
    with _windowed_emit(tmp_path, _SNAPSHOT_CONFIG) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "records__visit")
        sql = build_snapshot_render_sql(emit.sidecar, fork_path, spec, anchor, w0)
        properties = frozenset(
            src[len("prop__") :] for src, _ in spec.columns if src.startswith("prop__")
        )
        expected_state_at = build_state_at_sql(
            emit.sidecar, fork_path, "visit", properties, w0.end_ns
        )
    assert expected_state_at in sql


def test_snapshot_render_omits_updated_at(tmp_path: Path) -> None:
    """The snapshot shape carries no updated_at column (there is no per-event
    timestamp to render)."""
    w0, _, _ = windowed_test_windows()
    with _windowed_emit(tmp_path, _SNAPSHOT_CONFIG) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "records__visit")
        assert all(out != "updated_at" for _, out in spec.columns)
        sql = build_snapshot_render_sql(emit.sidecar, fork_path, spec, anchor, w0)
    assert "updated_at" not in sql


def test_snapshot_render_horizon_renders_active_deactivated_at(tmp_path: Path) -> None:
    """active/deactivated_at are the fold's own horizon-rendered columns: a record
    deactivated after the horizon shows active=True/deactivated_at=NULL;
    deactivated before, active=False with deactivated_at rendered wallclock."""
    _, w1, w2 = windowed_test_windows()
    with _windowed_emit(tmp_path, _SNAPSHOT_CONFIG) as (emit, specs, fork_path, anchor):
        spec = _spec_for(specs, "records__visit")
        rows_w1 = {
            r["id"]: r
            for r in _mapped_rows(
                emit,
                spec,
                build_snapshot_render_sql(emit.sidecar, fork_path, spec, anchor, w1),
            )
        }
        rows_w2 = {
            r["id"]: r
            for r in _mapped_rows(
                emit,
                spec,
                build_snapshot_render_sql(emit.sidecar, fork_path, spec, anchor, w2),
            )
        }
    # v002 (created w1, deactivated w2): deactivation lands after w1's horizon.
    assert rows_w1["v002"]["active"] is True
    assert rows_w1["v002"]["deactivated_at"] is None
    # ...but at or before w2's horizon.
    assert rows_w2["v002"]["active"] is False
    assert "2024-01-01" in str(rows_w2["v002"]["deactivated_at"])
