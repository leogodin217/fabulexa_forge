"""Tests for the two declared-table render SQL builders: `build_state_render_sql`
and `build_junction_render_sql` (`exporters/source/renders.py`, § 3b).

Runs each render's SQL directly against the DuckDB-backed spanning fixture
(`_source_fixtures.build_source_test_emit`), asserting the faithful-read
composition, wallclock rendering, default-election join-free SQL, and total
ordering the design doc specifies. `window=None` call sites exercise the
full-export contract (one row per record, `updated_at` included, native
types); the windowed fixture (`_source_fixtures.build_windowed_source_test_emit`)
exercises the `state` render's horizon reconstruction (`build_state_at_sql`
composed at `window.end_ns`: one row per record created strictly before the
horizon, horizon-rendered `active`/`deactivated_at`, codec-VARCHAR after-image
CAST back to the sidecar's declared type) and the `junction` render's
extract-on-change window membership with `left_at` horizon-masking. The
event-log render is its own suite (`test_events_render.py`); key-election
joins are `test_election_renders.py`'s.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import write_emit

from fabulexa_forge.anchor import render_anchor_temporal_expr, resolve_effective_anchor
from fabulexa_forge.config.models import (
    ExportConfig,
    MembershipRef,
    SourceConfig,
    SourceTableDecl,
)
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.source.plan import (
    SourceJunctionTablePlan,
    SourcePlan,
    SourceStateTablePlan,
    build_source_plan,
)
from fabulexa_forge.exporters.source.renders import (
    build_junction_render_sql,
    build_state_render_sql,
)
from fabulexa_forge.incremental.windows import Window
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import RunDatabaseError

from ._source_fixtures import (
    build_degenerate_slice_only_source_emit,
    build_slice_only_source_emit,
    build_source_junction_selection_emit,
    build_source_test_emit,
    build_windowed_source_test_emit,
    slice_only_horizon_window,
    windowed_test_windows,
)

if TYPE_CHECKING:
    from fabulexa_forge.reader.emit import Emit

# ---------------------------------------------------------------------------
# Plan-building + row-mapping helpers
# ---------------------------------------------------------------------------


@contextmanager
def _plan(
    emit_dir: Path,
    tables: "tuple[SourceTableDecl, ...]",
    *,
    windowed: bool = False,
) -> "Iterator[tuple[Emit, SourcePlan]]":
    """Open `emit_dir` and build a SourcePlan over `tables`, resolving the
    anchor and election the way the engine does."""
    config = ExportConfig(mode="source", source=SourceConfig(tables=tables))
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None
        election = resolve_election(emit.sidecar, config.keys)
        plan = build_source_plan(
            emit, config, anchor, election, windowed, discard_notice_sink
        )
        yield emit, plan


def _state(plan: SourcePlan, name: str) -> SourceStateTablePlan:
    """The sole `state` unit named `name`."""
    table = next(t for t in plan.tables if t.name == name)
    assert isinstance(table, SourceStateTablePlan)
    return table


def _junction(plan: SourcePlan, name: str) -> SourceJunctionTablePlan:
    """The sole `junction` unit named `name`."""
    table = next(t for t in plan.tables if t.name == name)
    assert isinstance(table, SourceJunctionTablePlan)
    return table


def _col_map(
    table: "SourceStateTablePlan | SourceJunctionTablePlan", row: tuple[object, ...]
) -> dict[str, object]:
    """Zip a result row against a table unit's output column names."""
    return {out: value for (_, out), value in zip(table.columns, row)}


def _mapped_rows(
    emit: "Emit", table: "SourceStateTablePlan | SourceJunctionTablePlan", sql: str
) -> list[dict[str, object]]:
    """Execute sql and zip every row against `table`'s output column names."""
    return [_col_map(table, row) for row in emit.query(sql, ())]


def _rows_by(
    emit: "Emit",
    table: "SourceStateTablePlan | SourceJunctionTablePlan",
    fork_path: str,
    anchor: object,
    window: "Window | None",
    key_col: str,
    *,
    junction: bool = False,
) -> dict[object, dict[str, object]]:
    """Render `table` at `window` and index its rows by `key_col`."""
    builder = build_junction_render_sql if junction else build_state_render_sql
    sql = builder(emit.sidecar, fork_path, table, anchor, window)  # type: ignore[arg-type]
    return {r[key_col]: r for r in _mapped_rows(emit, table, sql)}


_SPANNING_TABLES: "tuple[SourceTableDecl, ...]" = (
    SourceTableDecl(name="visit", kind="visit"),
    SourceTableDecl(name="shift", kind="shift"),
    SourceTableDecl(name="location", kind="location"),
    SourceTableDecl(name="order", kind="order"),
    SourceTableDecl(name="consultant", kind="actor", sub_types=("consultant",)),
    SourceTableDecl(name="nurse", kind="actor", sub_types=("nurse",)),
    SourceTableDecl(
        name="visit_team", membership=MembershipRef(kind="visit", property="team")
    ),
)

_WINDOWED_TABLES: "tuple[SourceTableDecl, ...]" = (
    SourceTableDecl(name="visit", kind="visit"),
    SourceTableDecl(name="order", kind="order"),
    SourceTableDecl(name="location", kind="location"),
    SourceTableDecl(
        name="visit_team", membership=MembershipRef(kind="visit", property="team")
    ),
)


# ---------------------------------------------------------------------------
# `state` render: full export
# ---------------------------------------------------------------------------


def test_state_render_wallclock_created_at_and_raw_ordering(tmp_path: Path) -> None:
    """created_at renders wallclock through the shared anchor renderer; ORDER
    BY is raw sim-time, never the rendered column."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        table = _state(plan, "visit")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
    expected = render_anchor_temporal_expr(
        plan.anchor, '"_rec"."created_sim_time"', "created_at", "timestamp"
    )
    assert expected in sql
    order_clause = sql.split("ORDER BY", 1)[1]
    assert '"_rec"."created_sim_time"' in order_clause
    assert "created_at" not in order_clause


def test_state_render_full_snapshot_active_deactivated_at(tmp_path: Path) -> None:
    """deactivated_at is NULL exactly for the active record; fork_path dropped."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        table = _state(plan, "location")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = {r["id"]: r for r in _mapped_rows(emit, table, sql)}
    assert rows["loc001"]["active"] is True
    assert rows["loc001"]["deactivated_at"] is None
    assert rows["loc002"]["active"] is False
    assert "2024-01-01" in str(rows["loc002"]["deactivated_at"])
    assert "fork_path" not in rows["loc001"]


def test_state_render_reference_column_id_only_unjoined(tmp_path: Path) -> None:
    """A reference-annotated prop__ column lands verbatim, id-only, unjoined."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        table = _state(plan, "order")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    assert rows[0]["location_id"] == "loc001"


def test_state_render_default_identity_composes_join_free_sql(tmp_path: Path) -> None:
    """A table whose identity/edge surfaces are all at their default
    (record_id) composes byte-identical, join-free SQL."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        table = _state(plan, "order")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
    assert "LEFT JOIN" not in sql


def test_state_render_split_unit_discriminator_dropped_and_filtered(
    tmp_path: Path,
) -> None:
    """A single-sub_types-addressed table filters to its sub-type and drops
    the discriminator column from its projection."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        table = _state(plan, "consultant")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    assert "'consultant'" in sql
    assert len(rows) == 1
    assert rows[0]["id"] == "act001"
    assert "actor_type" not in rows[0]


def test_state_render_multi_population_discriminator_retained_no_filter(
    tmp_path: Path,
) -> None:
    """A table addressing a kind's full sub-type domain retains its
    discriminator column and composes no discriminator WHERE — the
    no-op-filter-not-composed rule."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        table = _state(plan, "shift")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    assert '"prop__shift_type" IN' not in sql
    assert rows[0]["shift_type"] == "day"


def test_state_render_full_export_includes_updated_at(tmp_path: Path) -> None:
    """A full export renders `updated_at` (last_mutation_sim_time), wallclock."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        table = _state(plan, "visit")
        assert any(out == "updated_at" for _, out in table.columns)
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = {r["id"]: r for r in _mapped_rows(emit, table, sql)}
    assert "2024-01-01" in str(rows["v001"]["updated_at"])


def test_state_render_determinism(tmp_path: Path) -> None:
    """Two renders of the same table compose byte-identical SQL."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        table = _state(plan, "visit")
        sql_a = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        sql_b = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
    assert sql_a == sql_b


# ---------------------------------------------------------------------------
# `junction` render: full export
# ---------------------------------------------------------------------------


def test_junction_render_naming_and_open_interval(tmp_path: Path) -> None:
    """record_id-><K>_id; left_at NULL while open; elem__/member__ projected."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        table = _junction(plan, "visit_team")
        sql = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
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


def test_junction_render_wallclock_joined_at_and_raw_ordering(tmp_path: Path) -> None:
    """joined_at renders through the shared renderer; ORDER BY is raw sim-time."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        table = _junction(plan, "visit_team")
        sql = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
    expected = render_anchor_temporal_expr(
        plan.anchor, '"_mem"."joined_sim_time"', "joined_at", "timestamp"
    )
    assert expected in sql
    order_clause = sql.split("ORDER BY", 1)[1]
    assert '"_mem"."joined_sim_time"' in order_clause
    assert "joined_at" not in order_clause


def test_junction_render_default_identity_composes_join_free_sql(
    tmp_path: Path,
) -> None:
    """A junction whose owner/member edges are all at their default composes
    no join."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        table = _junction(plan, "visit_team")
        sql = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
    assert "LEFT JOIN" not in sql


def test_junction_render_determinism(tmp_path: Path) -> None:
    """Two renders of the same junction table compose byte-identical SQL."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        table = _junction(plan, "visit_team")
        sql_a = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        sql_b = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
    assert sql_a == sql_b


# ---------------------------------------------------------------------------
# `junction` render: owner selection (`sub_types` / `where`, the parent
# lookup, source-row-selection sprint § Phase 2)
# ---------------------------------------------------------------------------

_WARD_TABLES: "tuple[SourceTableDecl, ...]" = (
    SourceTableDecl(
        name="day_ward",
        membership=MembershipRef(kind="worker", property="ward"),
        sub_types=("day",),
    ),
)


def test_junction_sub_types_renders_only_narrowed_owner_intervals(
    tmp_path: Path,
) -> None:
    """A junction narrowed by owner `sub_types` renders only the intervals
    of owners in that sub-type."""
    with _plan(build_source_junction_selection_emit(tmp_path), _WARD_TABLES) as (
        emit,
        plan,
    ):
        table = _junction(plan, "day_ward")
        sql = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    assert len(rows) == 1
    assert rows[0]["worker_id"] == "w1"
    assert rows[0]["desk"] == "A"


def test_junction_where_renders_only_satisfying_owner_intervals(
    tmp_path: Path,
) -> None:
    """A junction narrowed by owner `where` renders only the intervals of
    satisfying owners — no owner attribute (`region`) projects."""
    tables = (
        SourceTableDecl(
            name="east_ward",
            membership=MembershipRef(kind="worker", property="ward"),
            where={"region": "west"},
        ),
    )
    with _plan(build_source_junction_selection_emit(tmp_path), tables) as (emit, plan):
        table = _junction(plan, "east_ward")
        sql = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    assert len(rows) == 1
    assert rows[0]["worker_id"] == "w2"
    assert "region" not in rows[0]


def test_junction_sub_types_and_where_and_composed(tmp_path: Path) -> None:
    """`sub_types` and `where` AND-compose: a sub_types match whose `where`
    predicate fails renders no rows."""
    tables = (
        SourceTableDecl(
            name="day_west_ward",
            membership=MembershipRef(kind="worker", property="ward"),
            sub_types=("day",),
            where={"region": "west"},
        ),
    )
    with _plan(build_source_junction_selection_emit(tmp_path), tables) as (emit, plan):
        table = _junction(plan, "day_west_ward")
        sql = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    assert rows == []


def test_junction_unrestricted_owner_selection_composes_no_semi_join(
    tmp_path: Path,
) -> None:
    """No `sub_types` / `where`: the owner's full domain needs no
    restriction — no `record_id IN (...)` semi-join composed, byte-identical
    to a junction declared with no selection at all."""
    tables = (
        SourceTableDecl(
            name="all_ward", membership=MembershipRef(kind="worker", property="ward")
        ),
    )
    with _plan(build_source_junction_selection_emit(tmp_path), tables) as (emit, plan):
        table = _junction(plan, "all_ward")
        sql = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    assert '"_mem"."record_id" IN' not in sql
    assert len(rows) == 2


def test_junction_sub_types_windowed_membership_window_invariant(
    tmp_path: Path,
) -> None:
    """Owner selection is window-invariant (constant-gated): the narrowed
    junction's windowed render still excludes the unselected owner's
    interval at every window, never re-evaluating the predicate per-window."""
    ms = 1_000_000
    window0 = Window(index=0, start_ns=0, end_ns=100 * ms, label="w0")
    window1 = Window(index=1, start_ns=100 * ms, end_ns=200 * ms, label="w1")
    with _plan(
        build_source_junction_selection_emit(tmp_path), _WARD_TABLES, windowed=True
    ) as (emit, plan):
        table = _junction(plan, "day_ward")
        sql0 = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, window0
        )
        sql1 = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, window1
        )
        rows0 = _mapped_rows(emit, table, sql0)
        rows1 = _mapped_rows(emit, table, sql1)
    assert all(r["worker_id"] == "w1" for r in rows0)
    assert all(r["worker_id"] == "w1" for r in rows1)


# ---------------------------------------------------------------------------
# `junction` render: kind_labels
# ---------------------------------------------------------------------------


def test_junction_render_no_kind_labels_byte_identical_to_default(
    tmp_path: Path,
) -> None:
    """A junction unit with no `kind_labels` renders the member kind column
    verbatim — byte-identical to a plain passthrough column, the no-labels
    no-op guard."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        table = _junction(plan, "visit_team")
        assert table.kind_labels == ()
        sql = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
    assert '"_mem"."member__actor__kind" AS "actor_kind"' in sql
    assert "CASE" not in sql


def test_junction_render_labeled_member_kind_renders_label(tmp_path: Path) -> None:
    """A labeled member kind renders the label; the owner column, ids, and
    timestamps are untouched."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        table = _junction(plan, "visit_team")
        labeled = replace(table, kind_labels=(("actor", "clinician"),))
        sql = build_junction_render_sql(
            plan.sidecar, plan.fork_path, labeled, plan.anchor, None
        )
        rows = _mapped_rows(emit, labeled, sql)
    assert {r["actor_kind"] for r in rows} == {"clinician"}
    assert {r["actor_id"] for r in rows} == {"act001", "act002"}
    assert all(r["visit_id"] == "v001" for r in rows)


def test_junction_render_unlabeled_kind_renders_verbatim(tmp_path: Path) -> None:
    """A `kind_labels` map naming a different kind leaves an unlabeled
    member kind verbatim."""
    with _plan(build_source_test_emit(tmp_path), _SPANNING_TABLES) as (emit, plan):
        table = _junction(plan, "visit_team")
        labeled = replace(table, kind_labels=(("location", "site"),))
        sql = build_junction_render_sql(
            plan.sidecar, plan.fork_path, labeled, plan.anchor, None
        )
        rows = _mapped_rows(emit, labeled, sql)
    assert {r["actor_kind"] for r in rows} == {"actor"}


def test_junction_render_null_member_kind_cell_stays_null(tmp_path: Path) -> None:
    """A NULL member-kind cell (open-interval / non-reference row) stays
    NULL under a `kind_labels` map — the CASE's identity fall-through never
    turns NULL into a rendered string."""
    emit_dir = build_source_test_emit(tmp_path)
    with duckdb.connect(str(emit_dir / "run.duckdb")) as conn:
        conn.execute(
            'UPDATE "membership__visit__team" SET "member__actor__kind" = NULL'
            " WHERE \"elem__role_name\" = 'support'"
        )
    with _plan(emit_dir, _SPANNING_TABLES) as (emit, plan):
        table = _junction(plan, "visit_team")
        labeled = replace(table, kind_labels=(("actor", "clinician"),))
        sql = build_junction_render_sql(
            plan.sidecar, plan.fork_path, labeled, plan.anchor, None
        )
        rows = _mapped_rows(emit, labeled, sql)
    still_open = next(r for r in rows if r["role_name"] == "support")
    assert still_open["actor_kind"] is None


def test_junction_render_corrupted_member_kind_value_renders_verbatim(
    tmp_path: Path,
) -> None:
    """A member-kind value naming no sidecar kind (a corrupted emit's
    mutated cell) renders verbatim — never masked, never an error."""
    emit_dir = build_source_test_emit(tmp_path)
    with duckdb.connect(str(emit_dir / "run.duckdb")) as conn:
        conn.execute(
            'UPDATE "membership__visit__team" SET "member__actor__kind" = ?'
            " WHERE \"elem__role_name\" = 'lead'",
            ["mutant_kind"],
        )
    with _plan(emit_dir, _SPANNING_TABLES) as (emit, plan):
        table = _junction(plan, "visit_team")
        labeled = replace(table, kind_labels=(("actor", "clinician"),))
        sql = build_junction_render_sql(
            plan.sidecar, plan.fork_path, labeled, plan.anchor, None
        )
        rows = _mapped_rows(emit, labeled, sql)
    closed = next(r for r in rows if r["role_name"] == "lead")
    assert closed["actor_kind"] == "mutant_kind"


# ---------------------------------------------------------------------------
# `state` render: windowed (horizon reconstruction)
# ---------------------------------------------------------------------------


def test_state_render_windowed_omits_updated_at(tmp_path: Path) -> None:
    """A windowed `state` table's projection carries no updated_at — a past
    horizon cannot reconstruct the sim-internal mutation instant."""
    with _plan(
        build_windowed_source_test_emit(tmp_path), _WINDOWED_TABLES, windowed=True
    ) as (emit, plan):
        table = _state(plan, "visit")
    assert all(out != "updated_at" for _, out in table.columns)


def test_state_render_windowed_reconstructs_at_horizon(tmp_path: Path) -> None:
    """Windowed `state` composes build_state_at_sql at window.end_ns: one row
    per record created strictly before the horizon; active/deactivated_at
    horizon-rendered — a deactivation after the horizon stays masked."""
    w0, w1, w2 = windowed_test_windows()
    with _plan(
        build_windowed_source_test_emit(tmp_path), _WINDOWED_TABLES, windowed=True
    ) as (emit, plan):
        table = _state(plan, "visit")
        rows_w0 = _rows_by(emit, table, plan.fork_path, plan.anchor, w0, "id")
        rows_w1 = _rows_by(emit, table, plan.fork_path, plan.anchor, w1, "id")
        rows_w2 = _rows_by(emit, table, plan.fork_path, plan.anchor, w2, "id")
    assert set(rows_w0) == {"v001"}
    assert set(rows_w1) == {"v001", "v002"}
    assert set(rows_w2) == {"v001", "v002", "v003"}
    # v002: created w1 (150ms), deactivated w2 (250ms) — still active at w1's horizon.
    assert rows_w1["v002"]["active"] is True
    assert rows_w1["v002"]["deactivated_at"] is None
    assert rows_w2["v002"]["active"] is False
    assert "2024-01-01" in str(rows_w2["v002"]["deactivated_at"])


def test_state_render_windowed_casts_back_to_sidecar_types(tmp_path: Path) -> None:
    """A windowed reconstruction's codec-VARCHAR after-image CASTs back to
    the sidecar's declared type; a tracked property reflects its as-of value."""
    _, w1, _ = windowed_test_windows()
    with _plan(
        build_windowed_source_test_emit(tmp_path), _WINDOWED_TABLES, windowed=True
    ) as (emit, plan):
        table = _state(plan, "visit")
        rows = _rows_by(emit, table, plan.fork_path, plan.anchor, w1, "id")
    assert isinstance(rows["v001"]["priority"], int)
    assert rows["v001"]["priority"] == 1
    assert rows["v001"]["status"] == "closed"  # v001's w1 status change (120ms)


def test_state_render_windowed_horizon_snapshot_cumulative(tmp_path: Path) -> None:
    """An untracked kind's windowed reconstruction is a cumulative horizon
    snapshot: every record created strictly before the horizon, not a
    per-window delta."""
    w0, w1, w2 = windowed_test_windows()
    with _plan(
        build_windowed_source_test_emit(tmp_path), _WINDOWED_TABLES, windowed=True
    ) as (emit, plan):
        table = _state(plan, "order")
        ids_w0 = set(_rows_by(emit, table, plan.fork_path, plan.anchor, w0, "id"))
        ids_w1 = set(_rows_by(emit, table, plan.fork_path, plan.anchor, w1, "id"))
        ids_w2 = set(_rows_by(emit, table, plan.fork_path, plan.anchor, w2, "id"))
    assert ids_w0 == {"ord001"}
    assert ids_w1 == {"ord001", "ord002"}
    assert ids_w2 == {"ord001", "ord002", "ord003"}


# ---------------------------------------------------------------------------
# `junction` render: windowed (extract-on-change)
# ---------------------------------------------------------------------------


def test_junction_render_windowed_extract_on_change(tmp_path: Path) -> None:
    """Junction render extracts-on-change: join-only masks left_at, a later
    leave re-emits it set, a same-window join+leave emits one closed row, and
    an interval touching neither bound in a window emits no row for it."""
    w0, w1, w2 = windowed_test_windows()
    with _plan(
        build_windowed_source_test_emit(tmp_path), _WINDOWED_TABLES, windowed=True
    ) as (emit, plan):
        table = _junction(plan, "visit_team")
        rows_w0 = _rows_by(
            emit, table, plan.fork_path, plan.anchor, w0, "visit_id", junction=True
        )
        rows_w1 = _rows_by(
            emit, table, plan.fork_path, plan.anchor, w1, "visit_id", junction=True
        )
        rows_w2 = _rows_by(
            emit, table, plan.fork_path, plan.anchor, w2, "visit_id", junction=True
        )

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
# `state` render: `where` predicate (source-row-selection sprint, Phase 1)
# ---------------------------------------------------------------------------


def test_state_render_where_scalar_compiles_equals_and_filters(tmp_path: Path) -> None:
    """A scalar `where` value compiles `=`; only the satisfying row renders."""
    tables = (
        SourceTableDecl(name="loc", kind="location", where={"prop__name": "Ward A"}),
    )
    with _plan(build_source_test_emit(tmp_path), tables) as (emit, plan):
        table = _state(plan, "loc")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    assert "_rec.\"prop__name\" = 'Ward A'" in sql
    assert len(rows) == 1
    assert rows[0]["id"] == "loc001"


def test_state_render_where_list_compiles_in(tmp_path: Path) -> None:
    """A list `where` value compiles `IN`; every satisfying row renders."""
    tables = (
        SourceTableDecl(
            name="loc", kind="location", where={"prop__name": ["Ward A", "Ward B"]}
        ),
    )
    with _plan(build_source_test_emit(tmp_path), tables) as (emit, plan):
        table = _state(plan, "loc")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    assert '_rec."prop__name" IN' in sql
    assert {r["id"] for r in rows} == {"loc001", "loc002"}


def test_state_render_where_multiple_entries_and_composed(tmp_path: Path) -> None:
    """Two `where` entries AND-join; a row matching only one entry is
    excluded."""
    tables = (
        SourceTableDecl(
            name="loc",
            kind="location",
            where={"prop__name": "Ward A", "prop__region": "South"},
        ),
    )
    with _plan(build_source_test_emit(tmp_path), tables) as (emit, plan):
        table = _state(plan, "loc")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    where_clause = sql.split(" WHERE ", 1)[1]
    assert " AND " in where_clause
    assert rows == []  # loc001's name matches but region doesn't; loc002 neither


def test_state_render_where_zero_match_emits_empty_table(tmp_path: Path) -> None:
    """A `where` matching no row emits the table empty — declared intent
    drives existence, as for an empty population."""
    tables = (
        SourceTableDecl(name="loc", kind="location", where={"prop__name": "Nowhere"}),
    )
    with _plan(build_source_test_emit(tmp_path), tables) as (emit, plan):
        table = _state(plan, "loc")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    assert rows == []


def test_state_render_where_null_valued_column_never_selected(tmp_path: Path) -> None:
    """A row whose predicated column is NULL is never selected, even under
    an `IN` list nominally covering every other row's value — `=`/`IN` is
    never satisfied by NULL."""
    emit_dir = build_source_test_emit(tmp_path)
    with duckdb.connect(str(emit_dir / "run.duckdb")) as conn:
        conn.execute(
            'UPDATE "records__location" SET "prop__region" = NULL'
            " WHERE \"record_id\" = 'loc002'"
        )
    tables = (
        SourceTableDecl(
            name="loc", kind="location", where={"prop__region": ["North", "South"]}
        ),
    )
    with _plan(emit_dir, tables) as (emit, plan):
        table = _state(plan, "loc")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    assert {r["id"] for r in rows} == {"loc001"}


def test_state_render_where_sub_types_and_composed(tmp_path: Path) -> None:
    """`where` narrows within a `sub_types`-selected population — AND, not
    OR."""
    tables = (
        SourceTableDecl(
            name="actor",
            kind="actor",
            sub_types=("consultant", "nurse"),
            where={"prop__name": "Dr. Lee"},
        ),
    )
    with _plan(build_source_test_emit(tmp_path), tables) as (emit, plan):
        table = _state(plan, "actor")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    assert len(rows) == 1
    assert rows[0]["id"] == "act001"


def test_state_render_where_column_omitted_from_columns_still_selects(
    tmp_path: Path,
) -> None:
    """A predicated column absent from `columns` still selects — selection
    and projection are orthogonal; the predicate reads the subject relation,
    not the projected output."""
    tables = (
        SourceTableDecl(
            name="loc",
            kind="location",
            columns=("prop__region",),
            where={"prop__name": "Ward A"},
        ),
    )
    with _plan(build_source_test_emit(tmp_path), tables) as (emit, plan):
        table = _state(plan, "loc")
        assert all(src != "prop__name" for src, _ in table.columns)
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    assert len(rows) == 1
    assert rows[0]["id"] == "loc001"
    assert "name" not in rows[0]


def test_state_render_where_reference_valued_column_compares_record_ids(
    tmp_path: Path,
) -> None:
    """A `where` on a reference-valued constant property compares base-layer
    record ids, no special case."""
    tables = (
        SourceTableDecl(
            name="ord_match", kind="order", where={"prop__location_id": "loc001"}
        ),
        SourceTableDecl(
            name="ord_nomatch", kind="order", where={"prop__location_id": "loc002"}
        ),
    )
    with _plan(build_source_test_emit(tmp_path), tables) as (emit, plan):
        match_table = _state(plan, "ord_match")
        nomatch_table = _state(plan, "ord_nomatch")
        match_sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, match_table, plan.anchor, None
        )
        nomatch_sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, nomatch_table, plan.anchor, None
        )
        match_rows = _mapped_rows(emit, match_table, match_sql)
        nomatch_rows = _mapped_rows(emit, nomatch_table, nomatch_sql)
    assert [r["id"] for r in match_rows] == ["ord001"]
    assert nomatch_rows == []


# ---------------------------------------------------------------------------
# `state` render: `where` predicate, windowed (horizon reconstruction)
# ---------------------------------------------------------------------------

_WHERE_WINDOWED_MATCH_ALL: "tuple[SourceTableDecl, ...]" = (
    SourceTableDecl(name="order", kind="order", where={"prop__location_id": "loc001"}),
)
_WHERE_WINDOWED_MATCH_NONE: "tuple[SourceTableDecl, ...]" = (
    SourceTableDecl(name="order", kind="order", where={"prop__location_id": "loc002"}),
)


def test_state_render_windowed_where_growth_mirrors_lifecycle(tmp_path: Path) -> None:
    """A `where` matching every record applies unchanged at every window
    horizon; presence growth mirrors created_sim_time only, exactly the
    unfiltered case — the predicate is applied, never re-evaluated, per
    window."""
    w0, w1, w2 = windowed_test_windows()
    with _plan(
        build_windowed_source_test_emit(tmp_path),
        _WHERE_WINDOWED_MATCH_ALL,
        windowed=True,
    ) as (emit, plan):
        table = _state(plan, "order")
        ids_w0 = set(_rows_by(emit, table, plan.fork_path, plan.anchor, w0, "id"))
        ids_w1 = set(_rows_by(emit, table, plan.fork_path, plan.anchor, w1, "id"))
        ids_w2 = set(_rows_by(emit, table, plan.fork_path, plan.anchor, w2, "id"))
    assert ids_w0 == {"ord001"}
    assert ids_w1 == {"ord001", "ord002"}
    assert ids_w2 == {"ord001", "ord002", "ord003"}


def test_state_render_windowed_where_excludes_consistently_across_horizons(
    tmp_path: Path,
) -> None:
    """A `where` matching no record excludes at every window horizon —
    never partially applied at some horizon and not another."""
    w0, w1, w2 = windowed_test_windows()
    with _plan(
        build_windowed_source_test_emit(tmp_path),
        _WHERE_WINDOWED_MATCH_NONE,
        windowed=True,
    ) as (emit, plan):
        table = _state(plan, "order")
        ids_w0 = set(_rows_by(emit, table, plan.fork_path, plan.anchor, w0, "id"))
        ids_w1 = set(_rows_by(emit, table, plan.fork_path, plan.anchor, w1, "id"))
        ids_w2 = set(_rows_by(emit, table, plan.fork_path, plan.anchor, w2, "id"))
    assert ids_w0 == set()
    assert ids_w1 == set()
    assert ids_w2 == set()


# ---------------------------------------------------------------------------
# slice_only column omission
# ---------------------------------------------------------------------------


def test_state_render_slice_only_omission_preserves_row_values(
    tmp_path: Path,
) -> None:
    """A non-exempt slice_only column is absent from a full-export state
    render; the row identities and tracked values are unaffected — the
    column-projection-only invariance the render's docstring documents (an
    untracked property never drives reconstruction)."""
    tables = (SourceTableDecl(name="patient", kind="patient"),)
    with _plan(build_slice_only_source_emit(tmp_path), tables) as (emit, plan):
        table = _state(plan, "patient")
        assert all(src != "prop__loyalty_tier" for src, _ in table.columns)

        narrowed_sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        narrowed_rows = _mapped_rows(emit, table, narrowed_sql)

        control_table = replace(
            table, columns=table.columns + (("prop__loyalty_tier", "loyalty_tier"),)
        )
        control_sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, control_table, plan.anchor, None
        )
        control_rows = _mapped_rows(emit, control_table, control_sql)

    narrowed_shape = [(r["id"], r["status"]) for r in narrowed_rows]
    control_shape = [(r["id"], r["status"]) for r in control_rows]
    assert narrowed_shape == control_shape
    assert narrowed_shape == [("p001", "open"), ("p002", "closed")]


def test_state_render_slice_only_omission_preserves_row_values_windowed(
    tmp_path: Path,
) -> None:
    """Under a windowed reconstruction, the slice_only column is absent from
    the state-at projection; the row set (identity, active, tracked
    property) is unchanged from a control render carrying the column."""
    horizon = slice_only_horizon_window()
    tables = (SourceTableDecl(name="patient", kind="patient"),)
    with _plan(build_slice_only_source_emit(tmp_path), tables, windowed=True) as (
        emit,
        plan,
    ):
        table = _state(plan, "patient")
        assert all(src != "prop__loyalty_tier" for src, _ in table.columns)

        narrowed_sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, horizon
        )
        narrowed_rows = _mapped_rows(emit, table, narrowed_sql)

        control_table = replace(
            table, columns=table.columns + (("prop__loyalty_tier", "loyalty_tier"),)
        )
        control_sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, control_table, plan.anchor, horizon
        )
        control_rows = _mapped_rows(emit, control_table, control_sql)

    narrowed_shape = {r["id"]: (r["active"], r["status"]) for r in narrowed_rows}
    control_shape = {r["id"]: (r["active"], r["status"]) for r in control_rows}
    assert narrowed_shape == control_shape
    assert narrowed_shape == {"p001": (True, "open"), "p002": (True, "closed")}


def test_state_render_degenerate_unit_still_renders_identity_and_lifecycle(
    tmp_path: Path,
) -> None:
    """A unit whose every property is non-exempt slice_only is never
    suppressed: it still renders its row, carrying identity and lifecycle
    columns with every prop__ column omitted."""
    tables = (SourceTableDecl(name="member", kind="member"),)
    with _plan(build_degenerate_slice_only_source_emit(tmp_path), tables) as (
        emit,
        plan,
    ):
        table = _state(plan, "member")
        assert all(not src.startswith("prop__") for src, _ in table.columns)
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)

    assert len(rows) == 1
    assert rows[0]["id"] == "mem001"
    assert rows[0]["active"] is True


# ---------------------------------------------------------------------------
# `render`: structural-instant rendering elections
# ---------------------------------------------------------------------------


def test_state_render_elects_date_on_created_sim_time(tmp_path: Path) -> None:
    """A `date`-elected `created_sim_time` renders a `datetime.date` value in
    place of the mode-definitional default timestamp."""
    tables = (
        SourceTableDecl(
            name="visit", kind="visit", render={"created_sim_time": "date"}
        ),
    )
    with _plan(build_source_test_emit(tmp_path), tables) as (emit, plan):
        table = _state(plan, "visit")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = {r["id"]: r for r in _mapped_rows(emit, table, sql)}
    assert rows["v001"]["created_at"] == date(2024, 1, 1)


def test_state_render_elects_date_on_deactivated_at(tmp_path: Path) -> None:
    """A `date`-elected `deactivated_at` renders a `datetime.date` for a
    deactivated record and stays NULL for an active one."""
    tables = (
        SourceTableDecl(
            name="location", kind="location", render={"deactivated_at": "date"}
        ),
    )
    with _plan(build_source_test_emit(tmp_path), tables) as (emit, plan):
        table = _state(plan, "location")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = {r["id"]: r for r in _mapped_rows(emit, table, sql)}
    assert rows["loc001"]["deactivated_at"] is None
    assert rows["loc002"]["deactivated_at"] == date(2024, 1, 1)


def test_state_render_elects_timestamptz_composes_absolute_instant_expr(
    tmp_path: Path,
) -> None:
    """A `timestamptz`-elected instant column composes the absolute-instant
    expression through the shared anchor renderer — checked as rendered SQL
    (never executed via the row-tuple path, which needs an optional pytz
    dependency this package never requires)."""
    tables = (
        SourceTableDecl(
            name="visit", kind="visit", render={"created_sim_time": "timestamptz"}
        ),
    )
    with _plan(build_source_test_emit(tmp_path), tables) as (emit, plan):
        table = _state(plan, "visit")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
    expected = render_anchor_temporal_expr(
        plan.anchor, '"_rec"."created_sim_time"', "created_at", "timestamptz"
    )
    assert expected in sql


def test_junction_render_elects_date_on_joined_and_left_at(tmp_path: Path) -> None:
    """A junction table's twin: `date`-elected `joined_sim_time` /
    `left_sim_time` render `datetime.date` values, NULL preserved for the
    still-open interval."""
    tables = (
        SourceTableDecl(
            name="visit_team",
            membership=MembershipRef(kind="visit", property="team"),
            render={"joined_sim_time": "date", "left_sim_time": "date"},
        ),
    )
    with _plan(build_source_test_emit(tmp_path), tables) as (emit, plan):
        table = _junction(plan, "visit_team")
        sql = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    closed = next(r for r in rows if r["role_name"] == "lead")
    still_open = next(r for r in rows if r["role_name"] == "support")
    assert closed["joined_at"] == date(2024, 1, 1)
    assert closed["left_at"] == date(2024, 1, 1)
    assert still_open["left_at"] is None


def test_junction_render_windowed_left_at_masking_honors_render_election(
    tmp_path: Path,
) -> None:
    """The windowed masked `left_at` expression renders through the elected
    type, not always the mode-definitional default timestamp: NULL while
    masked (w0, m_A still open), a `datetime.date` once the leave lands in
    window (w1, m_A leaves)."""
    w0, w1, _ = windowed_test_windows()
    tables = (
        SourceTableDecl(
            name="visit_team",
            membership=MembershipRef(kind="visit", property="team"),
            render={"left_sim_time": "date"},
        ),
    )
    with _plan(build_windowed_source_test_emit(tmp_path), tables, windowed=True) as (
        emit,
        plan,
    ):
        table = _junction(plan, "visit_team")
        sql_w0 = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, w0
        )
        rows_w0 = {r["visit_id"]: r for r in _mapped_rows(emit, table, sql_w0)}
        sql_w1 = build_junction_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, w1
        )
        rows_w1 = {r["visit_id"]: r for r in _mapped_rows(emit, table, sql_w1)}
    assert rows_w0["v001"]["left_at"] is None
    assert rows_w1["v001"]["left_at"] == date(2024, 1, 1)


# ---------------------------------------------------------------------------
# `render` map: `date_parse` elections
# ---------------------------------------------------------------------------

_DATE_PARSE_PATIENT_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__dob",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]


def _build_date_parse_patient_emit(tmp_path: Path, dob_value: str) -> Path:
    """A single flat untracked kind (`patient`) with one row whose
    `prop__dob` payload column carries `dob_value` — the `date_parse`
    render's happy-path and mismatch-attribution fixtures."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(
        'CREATE TABLE "records__patient" ('
        '"fork_path" VARCHAR, "record_id" VARCHAR, "created_sim_time" BIGINT,'
        ' "active" BOOLEAN, "deactivated_at" BIGINT,'
        ' "last_mutation_sim_time" BIGINT, "record_index" BIGINT,'
        ' "prop__dob" VARCHAR)'
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "p001", 0, True, 0, 0, dob_value],
    )
    conn.close()
    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": _DATE_PARSE_PATIENT_COLUMNS,
                "rows": 1,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


def test_state_render_date_parse_renders_date_in_place(tmp_path: Path) -> None:
    """`date_parse` on a payload VARCHAR renders DATE in place, output name
    still governed by defaults + `rename`."""
    tables = (
        SourceTableDecl(
            name="patients",
            kind="patient",
            rename={"prop__dob": "birth_date"},
            render={"prop__dob": {"date_parse": "%Y-%m-%d"}},
        ),
    )
    with _plan(_build_date_parse_patient_emit(tmp_path, "2024-06-01"), tables) as (
        emit,
        plan,
    ):
        table = _state(plan, "patients")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    assert rows[0]["birth_date"] == date(2024, 6, 1)


def test_state_render_date_parse_mismatch_fails_loudly_with_attribution(
    tmp_path: Path,
) -> None:
    """A non-matching value fails the export loudly at query time, naming
    the table, the column, and the offending value — never a silent NULL."""
    tables = (
        SourceTableDecl(
            name="patients",
            kind="patient",
            render={"prop__dob": {"date_parse": "%Y-%m-%d"}},
        ),
    )
    with _plan(_build_date_parse_patient_emit(tmp_path, "not-a-date"), tables) as (
        emit,
        plan,
    ):
        table = _state(plan, "patients")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        with pytest.raises(RunDatabaseError) as exc_info:
            _mapped_rows(emit, table, sql)
    message = str(exc_info.value)
    assert "patients.prop__dob" in message
    assert "not-a-date" in message
    assert "%Y-%m-%d" in message


def test_state_render_date_parse_datetime_format_renders_timestamp(
    tmp_path: Path,
) -> None:
    """A `date_parse` format carrying time directives (the widened family)
    renders naive TIMESTAMP through the declared-table map form, end-to-end."""
    tables = (
        SourceTableDecl(
            name="patients",
            kind="patient",
            rename={"prop__dob": "registered_at"},
            render={"prop__dob": {"date_parse": "%Y-%m-%d %H:%M:%S"}},
        ),
    )
    with _plan(
        _build_date_parse_patient_emit(tmp_path, "2024-06-01 14:30:05"), tables
    ) as (emit, plan):
        table = _state(plan, "patients")
        sql = build_state_render_sql(
            plan.sidecar, plan.fork_path, table, plan.anchor, None
        )
        rows = _mapped_rows(emit, table, sql)
    assert rows[0]["registered_at"] == datetime(2024, 6, 1, 14, 30, 5)
