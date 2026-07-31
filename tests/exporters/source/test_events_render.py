"""Tests for the event-log render (`exporters/source/events.py`, Phase 2).

`SourceEventSourcePlan` / `SourceEventLogPlan` are hand-constructed directly
(no plan builder exists yet) against `build_events_test_emit` (a tracked,
sub-typed `ticket` kind referencing a flat `agent` kind, plus a
`ticket.watchers` membership table) and `build_windowed_source_test_emit`
(the windowed visit/order/location/junction fixture, reused for the window
test).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import duckdb

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import KeySurface
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.exporters.populations import Population
from fabulexa_forge.exporters.source.events import (
    SourceEventLogPlan,
    SourceEventSourcePlan,
    build_changes_object_expr,
    build_event_log_sql,
)
from fabulexa_forge.exporters.source.plan import SourceEdgeSurface
from fabulexa_forge.reader.emit import open_emit

from ._source_fixtures import (
    build_events_test_emit,
    build_windowed_source_test_emit,
    windowed_test_windows,
)

if TYPE_CHECKING:
    from fabulexa_forge.reader.emit import Emit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rows(emit: "Emit", sql: str) -> list[dict[str, object]]:
    """Execute sql and zip every row against the event log's fixed columns."""
    columns = ("item_type", "item_id", "event", "occurred_at", "changes")
    return [dict(zip(columns, row)) for row in emit.query(sql, ())]


def _changes(row: dict[str, object]) -> dict[str, object]:
    """Parse one row's `changes` VARCHAR cell as JSON."""
    assert isinstance(row["changes"], str)
    return cast("dict[str, object]", json.loads(row["changes"]))


def _row_for(
    rows: list[dict[str, object]], item_id: object, event: str
) -> dict[str, object]:
    """The sole row matching (item_id, event); asserts exactly one match."""
    matches = [r for r in rows if r["item_id"] == item_id and r["event"] == event]
    assert len(matches) == 1, (
        f"expected exactly one ({item_id}, {event}) row: {matches}"
    )
    return matches[0]


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


def _ticket_source(
    audited_properties: tuple[str, ...],
    *,
    sub_types: tuple[str, ...] | None = None,
    item_surface: tuple[tuple[str | None, KeySurface], ...] = _RECORD_ID_SURFACE,
    change_edges: tuple[SourceEdgeSurface, ...] = (),
) -> SourceEventSourcePlan:
    """A records-source unit over `ticket`, addressing `sub_types` (default:
    both bug and feature)."""
    domain = sub_types if sub_types is not None else ("bug", "feature")
    return SourceEventSourcePlan(
        item_type="ticket",
        kind="ticket",
        property=None,
        populations=tuple(Population(kind="ticket", sub_type=st) for st in domain),
        audited_properties=audited_properties,
        item_surface=item_surface,
        change_edges=change_edges,
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
            name="versions", sources=(source,), item_id_type="VARCHAR"
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
            name="versions", sources=(source,), item_id_type="VARCHAR"
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


class TestEmptyAuditedSet:
    def test_create_and_destroy_use_empty_object(self, tmp_path: Path) -> None:
        source = _ticket_source(
            (), sub_types=("bug",), item_surface=(("bug", "record_id"),)
        )
        log = SourceEventLogPlan(
            name="versions", sources=(source,), item_id_type="VARCHAR"
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
            name="versions", sources=(source,), item_id_type="BIGINT"
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
        source = SourceEventSourcePlan(
            item_type="ticket.watchers",
            kind="ticket",
            property="watchers",
            populations=(
                Population(kind="ticket", sub_type="bug"),
                Population(kind="ticket", sub_type="feature"),
            ),
            audited_properties=("note", "party"),
            item_surface=_RECORD_ID_SURFACE,
            change_edges=(_agent_record_index_edge("member__party__id"),),
        )
        return SourceEventLogPlan(
            name="versions", sources=(source,), item_id_type="VARCHAR"
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
        watchers_source = SourceEventSourcePlan(
            item_type="ticket.watchers",
            kind="ticket",
            property="watchers",
            populations=(
                Population(kind="ticket", sub_type="bug"),
                Population(kind="ticket", sub_type="feature"),
            ),
            audited_properties=("note", "party"),
            item_surface=_RECORD_ID_SURFACE,
            change_edges=(_agent_record_index_edge("member__party__id"),),
        )
        log = SourceEventLogPlan(
            name="versions",
            sources=(ticket_source, watchers_source),
            item_id_type="VARCHAR",
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
            name="versions", sources=(source,), item_id_type="VARCHAR"
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
        source = SourceEventSourcePlan(
            item_type="visit",
            kind="visit",
            property=None,
            populations=(Population(kind="visit", sub_type=None),),
            audited_properties=("status", "priority"),
            item_surface=((None, "record_id"),),
            change_edges=(),
        )
        log = SourceEventLogPlan(
            name="versions", sources=(source,), item_id_type="VARCHAR"
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
