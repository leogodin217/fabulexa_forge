"""Tests for the event-log render (`exporters/source/events.py`).

`SourceEventSourcePlan` / `SourceEventLogPlan` are hand-constructed directly
(bypassing the plan builder) against `build_events_test_emit` (a tracked,
sub-typed `ticket` kind referencing a flat `agent` kind, plus a
`ticket.watchers` membership table) and `build_windowed_source_test_emit`
(the windowed visit/order/location/junction fixture, reused for the window
test). Every hand-constructed source here leaves `SourceEventSourcePlan.render`
at its default `()` (uniformly silent), so every `changes` entry stays raw
codec text — the render-election dispatch at the codec seam (`ElectionKindConflict`,
elected `changes` text, the export-time guards) is `test_value_election_events.py`'s
charter, plan-builder-driven end to end.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import duckdb

from fabulexa_forge.anchor import TemporalRender, resolve_effective_anchor
from fabulexa_forge.config.models import KeySurface
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.exporters.populations import Population
from fabulexa_forge.exporters.selection_spine import WhereEntry
from fabulexa_forge.exporters.source.events import (
    SourceEventLogPlan,
    SourceEventSourcePlan,
    build_changes_object_expr,
    build_event_log_sql,
)
from fabulexa_forge.exporters.source.plan import SourceEdgeSurface
from fabulexa_forge.reader.emit import open_emit

from ._event_log_helpers import changes_of as _changes
from ._event_log_helpers import event_log_rows as _rows
from ._event_log_helpers import row_for as _row_for
from ._source_fixtures import (
    build_event_log_suppressed_update_test_emit,
    build_event_tie_test_emit,
    build_events_test_emit,
    build_source_junction_selection_emit,
    build_windowed_source_test_emit,
    windowed_test_windows,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_RECORD_ID_SURFACE: tuple[tuple[str | None, KeySurface], ...] = (
    ("bug", "record_id"),
    ("feature", "record_id"),
)


def _agent_record_index_edge(source_column: str) -> SourceEdgeSurface:
    """A single-target-kind change edge translating `source_column` through
    `agent`'s record_index surface (digit-rendered)."""
    return SourceEdgeSurface(
        source_column=source_column,
        target_kinds=("agent",),
        per_kind_populations=(("agent", ((None, "record_index"),)),),
        rendered_type="VARCHAR",
    )


def _identity_pairs(
    bare_names: tuple[str, ...], rename: dict[str, str] | None = None
) -> tuple[tuple[str, str], ...]:
    """(bare, output) pairs for `audited_properties`: the bare name, unless
    `rename` maps it to an output key."""
    mapping = rename or {}
    return tuple((bare, mapping.get(bare, bare)) for bare in bare_names)


def _ticket_source(
    audited_properties: tuple[str, ...],
    *,
    sub_types: tuple[str, ...] | None = None,
    item_surface: tuple[tuple[str | None, KeySurface], ...] = _RECORD_ID_SURFACE,
    change_edges: tuple[SourceEdgeSurface, ...] = (),
    item_type: str = "ticket",
    rename: dict[str, str] | None = None,
    kind_labels: tuple[tuple[str, str], ...] = (),
    where: tuple[WhereEntry, ...] = (),
) -> SourceEventSourcePlan:
    """A records-source unit over `ticket`, addressing `sub_types` (default:
    both bug and feature)."""
    domain = sub_types if sub_types is not None else ("bug", "feature")
    return SourceEventSourcePlan(
        item_type=item_type,
        kind="ticket",
        property=None,
        populations=tuple(Population(kind="ticket", sub_type=st) for st in domain),
        audited_properties=_identity_pairs(audited_properties, rename),
        kind_labels=kind_labels,
        item_surface=item_surface,
        change_edges=change_edges,
        where=where,
    )


def _watchers_source(
    *,
    audited_properties: tuple[str, ...] = ("note", "party"),
    rename: dict[str, str] | None = None,
    kind_labels: tuple[tuple[str, str], ...] = (),
    item_type: str = "ticket.watchers",
) -> SourceEventSourcePlan:
    """A membership-source unit over `ticket.watchers`."""
    return SourceEventSourcePlan(
        item_type=item_type,
        kind="ticket",
        property="watchers",
        populations=(
            Population(kind="ticket", sub_type="bug"),
            Population(kind="ticket", sub_type="feature"),
        ),
        audited_properties=_identity_pairs(audited_properties, rename),
        kind_labels=kind_labels,
        item_surface=_RECORD_ID_SURFACE,
        change_edges=(_agent_record_index_edge("member__party__id"),),
    )


def _visit_source() -> SourceEventSourcePlan:
    """A flat, untyped records-source unit over `visit` (the windowed
    fixture)."""
    return SourceEventSourcePlan(
        item_type="visit",
        kind="visit",
        property=None,
        populations=(Population(kind="visit", sub_type=None),),
        audited_properties=_identity_pairs(("status", "priority")),
        kind_labels=(),
        item_surface=((None, "record_id"),),
        change_edges=(),
    )


def _worker_ward_source(*, where: tuple[WhereEntry, ...] = ()) -> SourceEventSourcePlan:
    """A membership-source unit over `worker.ward`
    (`build_source_junction_selection_emit`): two owners, day/night,
    `prop__region` constant east/west, one interval each — the owner
    `where` narrowing fixture (doc § The parent lookup)."""
    return SourceEventSourcePlan(
        item_type="worker.ward",
        kind="worker",
        property="ward",
        populations=(
            Population(kind="worker", sub_type="day"),
            Population(kind="worker", sub_type="night"),
        ),
        audited_properties=_identity_pairs(("desk",)),
        kind_labels=(),
        item_surface=(("day", "record_id"), ("night", "record_id")),
        change_edges=(),
        where=where,
    )


# ---------------------------------------------------------------------------
# build_changes_object_expr
# ---------------------------------------------------------------------------


def _eval_scalar(sql_expr: str) -> object:
    con = duckdb.connect()
    try:
        return con.execute(f"SELECT {sql_expr}").fetchone()[0]  # type: ignore[index]
    finally:
        con.close()


class TestBuildChangesObjectExpr:
    def test_empty_tuple_is_empty_object(self) -> None:
        assert _eval_scalar(build_changes_object_expr(())) == "{}"

    def test_sql_null_renders_json_null(self) -> None:
        expr = build_changes_object_expr((("a", "CAST(NULL AS VARCHAR)", "'x'"),))
        assert _eval_scalar(expr) == '{"a":[null,"x"]}'

    def test_escapes_quotes_backslashes_control_chars_in_values(self) -> None:
        expr = build_changes_object_expr(
            (("k", "CAST(NULL AS VARCHAR)", "'a\"b\\c' || chr(10)"),)
        )
        parsed = json.loads(_eval_scalar(expr))  # type: ignore[arg-type]
        assert parsed["k"] == [None, 'a"b\\c\n']

    def test_escapes_quotes_in_keys(self) -> None:
        expr = build_changes_object_expr((('k"1', "'x'", "'y'"),))
        parsed = json.loads(_eval_scalar(expr))  # type: ignore[arg-type]
        assert parsed == {'k"1': ["x", "y"]}

    def test_entry_order_preserved_byte_exactly(self) -> None:
        expr = build_changes_object_expr((("b", "'1'", "'2'"), ("a", "'3'", "'4'")))
        assert _eval_scalar(expr) == '{"b":["1","2"],"a":["3","4"]}'


# ---------------------------------------------------------------------------
# Records source: create / update / destroy
# ---------------------------------------------------------------------------


class TestRecordsSourceCreateUpdateDestroy:
    def _log(self) -> SourceEventLogPlan:
        source = _ticket_source(
            ("ticket_type", "status", "priority", "assignee_id"),
            change_edges=(_agent_record_index_edge("prop__assignee_id"),),
        )
        return SourceEventLogPlan(
            name="versions", sources=(source,), item_id_type="VARCHAR", keys=None
        )

    def test_create_every_audited_property_null_to_value(self, tmp_path: Path) -> None:
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(
                emit.sidecar, fork_path, self._log(), anchor, None
            )
            rows = _rows(emit, sql)

        t001_create = _row_for(rows, "t001", "create")
        assert t001_create["item_type"] == "ticket"
        assert _changes(t001_create) == {
            "ticket_type": [None, "bug"],
            "status": [None, "open"],
            "priority": [None, "1"],
            "assignee_id": [None, "0"],
        }

        # t003's assignee_id is NULL — translation stays NULL, not omitted.
        t003_create = _row_for(rows, "t003", "create")
        assert _changes(t003_create) == {
            "ticket_type": [None, "feature"],
            "status": [None, "pending"],
            "priority": [None, "9"],
            "assignee_id": [None, None],
        }

    def test_update_exactly_differing_entries_no_discriminator(
        self, tmp_path: Path
    ) -> None:
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(
                emit.sidecar, fork_path, self._log(), anchor, None
            )
            rows = _rows(emit, sql)

        updates = [r for r in rows if r["item_id"] == "t001" and r["event"] == "update"]
        assert len(updates) == 2
        by_changes = sorted((_changes(r) for r in updates), key=lambda c: list(c))
        assert by_changes[1] == {"status": ["open", "closed"]}
        assert by_changes[0] == {"priority": ["1", "5"]}
        # The discriminator never appears in an update changeset.
        for changes in by_changes:
            assert "ticket_type" not in changes

    def test_destroy_last_value_and_item_id_never_null(self, tmp_path: Path) -> None:
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(
                emit.sidecar, fork_path, self._log(), anchor, None
            )
            rows = _rows(emit, sql)

        t002_destroy = _row_for(rows, "t002", "destroy")
        assert t002_destroy["item_id"] is not None
        assert _changes(t002_destroy) == {
            "ticket_type": ["bug", None],
            "status": ["open", None],
            "priority": ["2", None],
            "assignee_id": ["1", None],
        }


class TestSubTypesNarrowedRecordsSource:
    def test_only_addressed_populations_emit_events(self, tmp_path: Path) -> None:
        source = _ticket_source(
            ("status",), sub_types=("bug",), item_surface=(("bug", "record_id"),)
        )
        log = SourceEventLogPlan(
            name="versions", sources=(source,), item_id_type="VARCHAR", keys=None
        )
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            rows = _rows(emit, sql)
        item_ids = {r["item_id"] for r in rows}
        assert item_ids == {"t001", "t002"}
        assert "t003" not in item_ids


# ---------------------------------------------------------------------------
# `where` narrowing (source-row-selection sprint § Phase 3, doc § Row
# selection): the fold input narrows to the selection spine; every event of
# an excluded record/owner is excluded, create and destroy (join/leave)
# alike.
# ---------------------------------------------------------------------------


class TestWhereNarrowedRecordsSource:
    def test_where_excludes_every_event_of_a_non_satisfying_record(
        self, tmp_path: Path
    ) -> None:
        """Only the satisfying record's events remain — its own create and
        destroy both included; the excluded records' events (including
        their own destroy) are entirely absent."""
        where = (
            WhereEntry(
                key="assignee_id",
                source_column="prop__assignee_id",
                sql_type="VARCHAR",
                value="agent_b",
                typed_values=(),
            ),
        )
        source = _ticket_source(("status",), where=where)
        log = SourceEventLogPlan(
            name="versions", sources=(source,), item_id_type="VARCHAR", keys=None
        )
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            rows = _rows(emit, sql)

        assert {r["item_id"] for r in rows} == {"t002"}
        assert {r["event"] for r in rows} == {"create", "destroy"}

    def test_predicated_property_need_not_be_audited(self, tmp_path: Path) -> None:
        """`where` is orthogonal to the audited property set: a property may
        be predicated and never appear in `changes`."""
        where = (
            WhereEntry(
                key="assignee_id",
                source_column="prop__assignee_id",
                sql_type="VARCHAR",
                value="agent_a",
                typed_values=(),
            ),
        )
        source = _ticket_source(("status",), where=where)
        log = SourceEventLogPlan(
            name="versions", sources=(source,), item_id_type="VARCHAR", keys=None
        )
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            rows = _rows(emit, sql)

        assert {r["item_id"] for r in rows} == {"t001"}
        for row in rows:
            assert "assignee_id" not in _changes(row)

    def test_where_narrowed_id_stays_dense_and_one_based(self, tmp_path: Path) -> None:
        """`id` is dense and 1-based over the narrowed whole-tape set —
        excluded records reserve no numbers."""
        where = (
            WhereEntry(
                key="assignee_id",
                source_column="prop__assignee_id",
                sql_type="VARCHAR",
                value="agent_b",
                typed_values=(),
            ),
        )
        source = _ticket_source(("status",), where=where)
        log = SourceEventLogPlan(
            name="versions", sources=(source,), item_id_type="VARCHAR", keys=None
        )
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            rows = _rows(emit, sql)

        ids = [r["id"] for r in rows]
        assert ids == list(range(1, len(rows) + 1))


class TestWhereNarrowedMembershipSource:
    def test_owner_where_excludes_both_join_and_leave_of_excluded_owner(
        self, tmp_path: Path
    ) -> None:
        """The owner `where` narrows through the parent lookup: w1 (region
        east) satisfies and contributes both its join and leave; w2's
        (region west) collection is excluded wholesale — it never even
        contributes its own join-only row."""
        where = (
            WhereEntry(
                key="region",
                source_column="prop__region",
                sql_type="VARCHAR",
                value="east",
                typed_values=(),
            ),
        )
        log = SourceEventLogPlan(
            name="versions",
            sources=(_worker_ward_source(where=where),),
            item_id_type="VARCHAR",
            keys=None,
        )
        emit_dir = build_source_junction_selection_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            rows = _rows(emit, sql)

        assert {r["item_id"] for r in rows} == {"w1"}
        assert {r["event"] for r in rows} == {"create", "destroy"}


class TestEmptyAuditedSet:
    def test_create_and_destroy_use_empty_object(self, tmp_path: Path) -> None:
        source = _ticket_source(
            (), sub_types=("bug",), item_surface=(("bug", "record_id"),)
        )
        log = SourceEventLogPlan(
            name="versions", sources=(source,), item_id_type="VARCHAR", keys=None
        )
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            rows = _rows(emit, sql)
        assert not [r for r in rows if r["event"] == "update"]
        t002_create = _row_for(rows, "t002", "create")
        t002_destroy = _row_for(rows, "t002", "destroy")
        assert t002_create["changes"] == "{}"
        assert t002_destroy["changes"] == "{}"


class TestItemIdTypeRule:
    def test_non_varchar_item_id_type_casts(self, tmp_path: Path) -> None:
        source = _ticket_source(
            ("status",),
            item_surface=(("bug", "record_index"), ("feature", "record_index")),
        )
        log = SourceEventLogPlan(
            name="versions", sources=(source,), item_id_type="BIGINT", keys=None
        )
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            rows = _rows(emit, sql)
        t001_create = _row_for(rows, 0, "create")
        assert isinstance(t001_create["item_id"], int)


# ---------------------------------------------------------------------------
# Membership source
# ---------------------------------------------------------------------------


class TestMembershipSource:
    def _log(self) -> SourceEventLogPlan:
        return SourceEventLogPlan(
            name="versions",
            sources=(_watchers_source(),),
            item_id_type="VARCHAR",
            keys=None,
        )

    def test_join_creates_leave_destroys_field_expansion(self, tmp_path: Path) -> None:
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(
                emit.sidecar, fork_path, self._log(), anchor, None
            )
            rows = _rows(emit, sql)

        assert {r["item_type"] for r in rows} == {"ticket.watchers"}
        assert {r["item_id"] for r in rows} == {"t001"}

        creates = [r for r in rows if r["event"] == "create"]
        destroys = [r for r in rows if r["event"] == "destroy"]
        assert len(creates) == 2
        assert len(destroys) == 1

        urgent_create = next(
            r for r in creates if _changes(r).get("note") == [None, "urgent"]
        )
        assert _changes(urgent_create) == {
            "note": [None, "urgent"],
            "party_kind": [None, "agent"],
            "party_id": [None, "0"],
        }
        urgent_destroy = destroys[0]
        assert _changes(urgent_destroy) == {
            "note": ["urgent", None],
            "party_kind": ["agent", None],
            "party_id": ["0", None],
        }
        fyi_create = next(
            r for r in creates if _changes(r).get("note") == [None, "fyi"]
        )
        assert _changes(fyi_create) == {
            "note": [None, "fyi"],
            "party_kind": [None, "agent"],
            "party_id": [None, "1"],
        }


# ---------------------------------------------------------------------------
# Total order
# ---------------------------------------------------------------------------


class TestTotalOrderTieFree:
    def test_coincident_event_sim_time_broken_by_item_type(
        self, tmp_path: Path
    ) -> None:
        """t002's records destroy and t001's watchers 'fyi' join both land at
        event_sim_time=180ms; `item_type` ('ticket' < 'ticket.watchers')
        must break the tie deterministically."""
        ticket_source = _ticket_source(("status",))
        watchers_source = _watchers_source()
        log = SourceEventLogPlan(
            name="versions",
            sources=(ticket_source, watchers_source),
            item_id_type="VARCHAR",
            keys=None,
        )
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            rows = _rows(emit, sql)

        t002_destroy_idx = next(
            i
            for i, r in enumerate(rows)
            if r["item_type"] == "ticket"
            and r["item_id"] == "t002"
            and r["event"] == "destroy"
        )
        fyi_create_idx = next(
            i
            for i, r in enumerate(rows)
            if r["item_type"] == "ticket.watchers"
            and r["event"] == "create"
            and _changes(r).get("note") == [None, "fyi"]
        )
        # Both events genuinely coincide on event_sim_time (180ms).
        assert (
            rows[t002_destroy_idx]["occurred_at"] == rows[fyi_create_idx]["occurred_at"]
        )
        assert t002_destroy_idx < fyi_create_idx


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_two_renders_produce_identical_sql(self, tmp_path: Path) -> None:
        source = _ticket_source(("status", "priority"))
        log = SourceEventLogPlan(
            name="versions", sources=(source,), item_id_type="VARCHAR", keys=None
        )
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql_a = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            sql_b = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
        assert sql_a == sql_b


# ---------------------------------------------------------------------------
# Windowed
# ---------------------------------------------------------------------------


class TestWindowed:
    def test_window_selects_by_event_sim_time_keeping_correct_old_new(
        self, tmp_path: Path
    ) -> None:
        log = SourceEventLogPlan(
            name="versions",
            sources=(_visit_source(),),
            item_id_type="VARCHAR",
            keys=None,
        )
        emit_dir = build_windowed_source_test_emit(tmp_path)
        _, w1, _ = windowed_test_windows()
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, w1)
            rows = _rows(emit, sql)

        assert {(r["item_id"], r["event"]) for r in rows} == {
            ("v001", "update"),
            ("v002", "create"),
        }
        v001_update = _row_for(rows, "v001", "update")
        assert _changes(v001_update) == {"status": ["open", "closed"]}


# ---------------------------------------------------------------------------
# `id`: dense ROW_NUMBER, suppression-transparent, tape-anchored under a window
# ---------------------------------------------------------------------------


class TestEventLogId:
    def test_full_export_id_runs_dense_and_monotone(self, tmp_path: Path) -> None:
        """A full export's `id` is 1..N with no gaps, ascending in emitted
        row order (rows already arrive `ORDER BY "id"`)."""
        ticket_source = _ticket_source(("status", "priority"))
        watchers_source = _watchers_source()
        log = SourceEventLogPlan(
            name="versions",
            sources=(ticket_source, watchers_source),
            item_id_type="VARCHAR",
            keys=None,
        )
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            rows = _rows(emit, sql)

        ids = [r["id"] for r in rows]
        assert ids == list(range(1, len(rows) + 1))

    def test_suppressed_update_consumes_no_id_density_holds(
        self, tmp_path: Path
    ) -> None:
        """A reasserted-at-its-current-value update is dropped (empty
        `changes`), yet the surviving rows' `id` values stay consecutive —
        the ROW_NUMBER sits beneath the arm's own suppression filter, so the
        dropped row never reserved a number to begin with."""
        source = _ticket_source(("status",))
        log = SourceEventLogPlan(
            name="versions", sources=(source,), item_id_type="VARCHAR", keys=None
        )
        emit_dir = build_event_log_suppressed_update_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            rows = _rows(emit, sql)

        # No no-op update survives: exactly one 'update' row for t600.
        updates = [r for r in rows if r["item_id"] == "t600" and r["event"] == "update"]
        assert len(updates) == 1
        assert _changes(updates[0]) == {"status": ["open", "closed"]}

        ids = [r["id"] for r in rows]
        assert ids == list(range(1, len(rows) + 1))

    def test_windowed_ids_match_full_export_as_a_contiguous_block(
        self, tmp_path: Path
    ) -> None:
        """A window's rows carry the same `id` values they carry in a full
        export — `id` is tape-anchored, never renumbered per window — and
        those ids form one contiguous ascending block within the full
        export's numbering."""
        log = SourceEventLogPlan(
            name="versions",
            sources=(_visit_source(),),
            item_id_type="VARCHAR",
            keys=None,
        )
        emit_dir = build_windowed_source_test_emit(tmp_path)
        _, w1, _ = windowed_test_windows()
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            full_sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            full_rows = _rows(emit, full_sql)
            window_sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, w1)
            window_rows = _rows(emit, window_sql)

        by_id = {r["id"]: (r["item_id"], r["event"]) for r in full_rows}
        window_ids = sorted(r["id"] for r in window_rows)

        # Every windowed row's id, and its (item_id, event) identity, agree
        # with the full export's numbering for that same id — not renumbered.
        for row in window_rows:
            assert by_id[row["id"]] == (row["item_id"], row["event"])

        # The window's ids form one contiguous ascending block.
        assert window_ids == list(range(window_ids[0], window_ids[0] + len(window_ids)))


# ---------------------------------------------------------------------------
# Before-image ordering: coincident update and destroy
# ---------------------------------------------------------------------------


class TestCoincidentUpdateAndDestroy:
    """A record whose change and deactivation share one sim_time.

    The fold emits two events at that instant (event_class 1 and 2). The
    before-image LAG must break the tie on `event_class`; ordering on
    `event_sim_time` alone leaves the pair orderable either way, and the
    swap corrupts BOTH rows — the update reads the destroy's nulled
    after-image, the destroy reads the pre-update value.
    """

    @staticmethod
    def _log() -> SourceEventLogPlan:
        return SourceEventLogPlan(
            name="audit_log",
            sources=(_ticket_source(("status",), sub_types=("bug",)),),
            item_id_type="VARCHAR",
            keys=None,
        )

    def test_lag_window_breaks_the_tie_on_event_class(self, tmp_path: Path) -> None:
        """The compiled lag window orders by (event_sim_time, event_class).

        Asserted on the SQL rather than only on results: a tie resolved by
        chance would let a result-only test pass against the broken order.
        """
        emit_dir = build_event_tie_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(
                emit.sidecar, fork_path, self._log(), anchor, None
            )
        assert 'ORDER BY "_valued"."event_sim_time", "_valued"."event_class"' in sql, (
            "the before-image LAG must carry a total order within a record"
        )

    def test_before_images_chain_through_the_tied_pair(self, tmp_path: Path) -> None:
        """create -> update -> destroy each carry the prior committed value."""
        emit_dir = build_event_tie_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(
                emit.sidecar, fork_path, self._log(), anchor, None
            )
            rows = _rows(emit, sql)

        assert _changes(_row_for(rows, "t900", "create")) == {"status": [None, "open"]}
        assert _changes(_row_for(rows, "t900", "update")) == {
            "status": ["open", "closed"]
        }
        assert _changes(_row_for(rows, "t900", "destroy")) == {
            "status": ["closed", None]
        }

    def test_the_update_and_destroy_do_share_an_instant(self, tmp_path: Path) -> None:
        """Guards the fixture itself: without the tie the test above is vacuous."""
        emit_dir = build_event_tie_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(
                emit.sidecar, fork_path, self._log(), anchor, None
            )
            rows = _rows(emit, sql)

        assert (
            _row_for(rows, "t900", "update")["occurred_at"]
            == _row_for(rows, "t900", "destroy")["occurred_at"]
        )


# ---------------------------------------------------------------------------
# `changes` key resolution: `rename`
# ---------------------------------------------------------------------------


class TestRenamedRecordsProperty:
    def test_renamed_property_changes_key_used_order_preserved(
        self, tmp_path: Path
    ) -> None:
        """A `rename`d bare property's `changes` entry uses the output key
        in create, update, and destroy rows; key order stays the given
        `audited_properties` order — rename relabels, never reorders."""
        source = _ticket_source(
            ("ticket_type", "status", "priority", "assignee_id"),
            rename={"priority": "level"},
            change_edges=(_agent_record_index_edge("prop__assignee_id"),),
        )
        log = SourceEventLogPlan(
            name="versions", sources=(source,), item_id_type="VARCHAR", keys=None
        )
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            rows = _rows(emit, sql)

        t001_create = _row_for(rows, "t001", "create")
        raw = t001_create["changes"]
        assert isinstance(raw, str)
        assert list(json.loads(raw).keys()) == [
            "ticket_type",
            "status",
            "level",
            "assignee_id",
        ]
        assert _changes(t001_create)["level"] == [None, "1"]
        assert "priority" not in _changes(t001_create)

        t001_updates = [
            r for r in rows if r["item_id"] == "t001" and r["event"] == "update"
        ]
        assert any(_changes(r).get("level") == ["1", "5"] for r in t001_updates)
        for r in t001_updates:
            assert "priority" not in _changes(r)

        t002_destroy = _row_for(rows, "t002", "destroy")
        assert _changes(t002_destroy)["level"] == ["2", None]


class TestRenamedMembershipField:
    def test_renamed_reference_field_expands_to_g_kind_g_id(
        self, tmp_path: Path
    ) -> None:
        """A membership reference field renamed `party -> handler` yields
        `handler_kind` / `handler_id` entries in place of `party_kind` /
        `party_id`."""
        log = SourceEventLogPlan(
            name="versions",
            sources=(_watchers_source(rename={"party": "handler"}),),
            item_id_type="VARCHAR",
            keys=None,
        )
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            rows = _rows(emit, sql)

        creates = [r for r in rows if r["event"] == "create"]
        urgent_create = next(
            r for r in creates if _changes(r).get("note") == [None, "urgent"]
        )
        assert _changes(urgent_create) == {
            "note": [None, "urgent"],
            "handler_kind": [None, "agent"],
            "handler_id": [None, "0"],
        }
        assert "party_kind" not in _changes(urgent_create)
        assert "party_id" not in _changes(urgent_create)


# ---------------------------------------------------------------------------
# `<f>_kind` labeling
# ---------------------------------------------------------------------------


class TestMembershipKindLabeling:
    def _urgent_rows(
        self, tmp_path: Path, kind_labels: "tuple[tuple[str, str], ...]"
    ) -> list[dict[str, object]]:
        log = SourceEventLogPlan(
            name="versions",
            sources=(_watchers_source(kind_labels=kind_labels),),
            item_id_type="VARCHAR",
            keys=None,
        )
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            return _rows(emit, sql)

    def test_labeled_kind_renders_label_in_old_and_new_halves(
        self, tmp_path: Path
    ) -> None:
        rows = self._urgent_rows(tmp_path, (("agent", "clinician"),))
        urgent_create = next(
            r
            for r in rows
            if r["event"] == "create" and _changes(r).get("note") == [None, "urgent"]
        )
        urgent_destroy = next(
            r
            for r in rows
            if r["event"] == "destroy" and _changes(r).get("note") == ["urgent", None]
        )
        assert _changes(urgent_create)["party_kind"] == [None, "clinician"]
        assert _changes(urgent_destroy)["party_kind"] == ["clinician", None]

    def test_unlabeled_kind_renders_verbatim(self, tmp_path: Path) -> None:
        rows = self._urgent_rows(tmp_path, (("resource", "consultant"),))
        fyi_create = next(
            r
            for r in rows
            if r["event"] == "create" and _changes(r).get("note") == [None, "fyi"]
        )
        assert _changes(fyi_create)["party_kind"] == [None, "agent"]

    def test_corrupted_kind_value_renders_verbatim(self, tmp_path: Path) -> None:
        """A member-kind value naming no sidecar kind (a corrupted emit's
        mutated cell) renders verbatim — never masked, never an error."""
        emit_dir = build_events_test_emit(tmp_path)
        with duckdb.connect(str(emit_dir / "run.duckdb")) as conn:
            conn.execute(
                'UPDATE "membership__ticket__watchers"'
                " SET \"member__party__kind\" = 'mutant_kind'"
                " WHERE \"elem__note\" = 'urgent'"
            )
        log = SourceEventLogPlan(
            name="versions",
            sources=(_watchers_source(kind_labels=(("agent", "clinician"),)),),
            item_id_type="VARCHAR",
            keys=None,
        )
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            rows = _rows(emit, sql)

        urgent_create = next(
            r
            for r in rows
            if r["event"] == "create" and _changes(r).get("note") == [None, "urgent"]
        )
        assert _changes(urgent_create)["party_kind"] == [None, "mutant_kind"]


class TestNoLabelsByteIdenticalToday:
    def test_membership_kind_passthrough_composes_no_case(self, tmp_path: Path) -> None:
        """With `kind_labels=()`, the member-kind value expression is the
        raw column, unwrapped — no labeling CASE composes."""
        log = SourceEventLogPlan(
            name="versions",
            sources=(_watchers_source(),),
            item_id_type="VARCHAR",
            keys=None,
        )
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
        assert 'WHEN "_fold"."member__party__kind" = ' not in sql


# ---------------------------------------------------------------------------
# Resolved `item_type`: stamped value and order-key component
# ---------------------------------------------------------------------------


class TestResolvedItemTypeOrdering:
    def test_records_source_item_type_override_replaces_kind_name(
        self, tmp_path: Path
    ) -> None:
        """The stamped `item_type` is the plan's resolved value, not the
        kind name it was constructed from."""
        source = _ticket_source(("status",), item_type="issue")
        log = SourceEventLogPlan(
            name="versions", sources=(source,), item_id_type="VARCHAR", keys=None
        )
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            rows = _rows(emit, sql)
        assert {r["item_type"] for r in rows} == {"issue"}

    def test_two_sources_aliased_to_one_item_type_interleave_by_time(
        self, tmp_path: Path
    ) -> None:
        """Two split records sources resolving one aliased item_type union
        and order as a single population — later events from either source
        sort after earlier ones from the other, not grouped by source."""
        bug_source = _ticket_source(
            ("status",),
            sub_types=("bug",),
            item_surface=(("bug", "record_id"),),
            item_type="issue",
        )
        feature_source = _ticket_source(
            ("status",),
            sub_types=("feature",),
            item_surface=(("feature", "record_id"),),
            item_type="issue",
        )
        log = SourceEventLogPlan(
            name="versions",
            sources=(bug_source, feature_source),
            item_id_type="VARCHAR",
            keys=None,
        )
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            rows = _rows(emit, sql)

        assert {r["item_type"] for r in rows} == {"issue"}
        creates_by_item_id = {
            r["item_id"]: r["id"] for r in rows if r["event"] == "create"
        }
        # t001/t002 (bug, 100ms) precede t003 (feature, 120ms).
        assert creates_by_item_id["t001"] < creates_by_item_id["t003"]
        assert creates_by_item_id["t002"] < creates_by_item_id["t003"]

    def test_aliased_split_orders_by_resolved_names_not_natural_kind(
        self, tmp_path: Path
    ) -> None:
        """A coincident-time tie between two sources breaks on the RESOLVED
        item_type, not the kind's natural name — overriding the ticket
        source's item_type to sort after 'ticket.watchers' flips the tie
        order relative to the natural-name case."""
        ticket_source = _ticket_source(("status",), item_type="zzz_ticket")
        log = SourceEventLogPlan(
            name="versions",
            sources=(ticket_source, _watchers_source()),
            item_id_type="VARCHAR",
            keys=None,
        )
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(emit.sidecar, fork_path, log, anchor, None)
            rows = _rows(emit, sql)

        t002_destroy_idx = next(
            i
            for i, r in enumerate(rows)
            if r["item_type"] == "zzz_ticket"
            and r["item_id"] == "t002"
            and r["event"] == "destroy"
        )
        fyi_create_idx = next(
            i
            for i, r in enumerate(rows)
            if r["item_type"] == "ticket.watchers"
            and r["event"] == "create"
            and _changes(r).get("note") == [None, "fyi"]
        )
        # Both events genuinely coincide on event_sim_time (180ms).
        assert (
            rows[t002_destroy_idx]["occurred_at"] == rows[fyi_create_idx]["occurred_at"]
        )
        # Flipped from the natural-name case (TestTotalOrderTieFree): 'ticket'
        # < 'ticket.watchers' but 'zzz_ticket' > 'ticket.watchers'.
        assert fyi_create_idx < t002_destroy_idx


# ---------------------------------------------------------------------------
# `render`: the log's one instant column (`event_sim_time` -> `occurred_at`)
# ---------------------------------------------------------------------------


class TestEventLogRender:
    def _log(self, render: TemporalRender = "timestamp") -> SourceEventLogPlan:
        source = _ticket_source(("status",))
        return SourceEventLogPlan(
            name="versions",
            sources=(source,),
            item_id_type="VARCHAR",
            keys=None,
            render=render,
        )

    def test_default_render_is_naive_local_timestamp(self, tmp_path: Path) -> None:
        """Absent an election, `occurred_at` renders the mode-definitional
        default: a naive local `datetime.datetime`, not a `datetime.date`."""
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(
                emit.sidecar, fork_path, self._log(), anchor, None
            )
            rows = _rows(emit, sql)
        t001_create = _row_for(rows, "t001", "create")
        assert type(t001_create["occurred_at"]) is datetime

    def test_render_date_elects_a_date_value_on_occurred_at(
        self, tmp_path: Path
    ) -> None:
        """`log.render == 'date'` renders `occurred_at` as a `datetime.date`
        through the shared anchor renderer — the same election every mode
        shares."""
        emit_dir = build_events_test_emit(tmp_path)
        with open_emit(emit_dir) as emit:
            fork_path = require_single_branch(emit.sidecar)
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None
            sql = build_event_log_sql(
                emit.sidecar, fork_path, self._log("date"), anchor, None
            )
            rows = _rows(emit, sql)
        t001_create = _row_for(rows, "t001", "create")
        assert isinstance(t001_create["occurred_at"], date)
