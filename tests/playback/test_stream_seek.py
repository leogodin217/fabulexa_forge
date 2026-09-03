"""Tests for tier-2 stream playback seek: the 'r' snapshot phase
(iter_resolved_snapshot_events) and StreamPlayback.seek's snapshot-then-
stream composition.

Materialized against minimal in-process emits built via _data_fixtures.
Covers compaction semantics, coincident-instant folding, change-scope
non-narrowing, sub_types/where scoping, phase ordering, membership-events
short-circuiting, and the seek-state upsert-log equivalence to a full play.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from _support.notices import discard_notice_sink
from _support.sidecar_builder import enum_options, identity_column, prop_column

from fabulexa_forge.anchor import render_ts
from fabulexa_forge.exporters.streaming.engine import (
    iter_resolved_stream_events,
    resolve_streams,
)
from fabulexa_forge.playback.stream import open_stream_playback
from fabulexa_forge.reader.emit import open_emit

from ._data_fixtures import MembershipSpec, RecordSpec, build_data_emit
from ._scenario import make_anchor
from ._stream_config import (
    kind_stream,
    membership_events_config,
    membership_stream,
    state_changes_config,
)

if TYPE_CHECKING:
    from pathlib import Path

    from fabulexa_forge.exporters.streaming.types import StreamEvent

_LIFECYCLE_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]

_WIDGET_COLS: list[dict[str, object]] = [
    *_LIFECYCLE_COLS,
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]


def _fold_upsert_log(events: Iterable["StreamEvent"]) -> dict[str, object]:
    """Fold a state-changes stream as an upsert log keyed by record_id:
    insert on 'c'/'r', upsert on 'u', retire (drop) on 'd'."""
    state: dict[str, object] = {}
    for event in events:
        if event.op in ("c", "r", "u"):
            state[event.record_id] = event.after
        elif event.op == "d":
            state.pop(event.record_id, None)
    return state


def _build_widget_scenario(tmp_path: "Path") -> "Path":
    """One kind 'widget', four records exercising compaction, coincident-
    instant folding, and future creation:

      w1: created@10 (status='new'), u@25 (status='active') — lives forever.
      w2: created@15 (status='temp'), deactivated@20 — created and deleted
          before every T this suite seeks past 20 at (compaction).
      w3: created@35 (status='fresh') — after every T this suite seeks
          before 35.
      w5: created@5 (status='a'), u@40 (status='b') coincident with
          deactivated@40 — compaction with a coincident update.
    """
    record_rows = [
        ("trunk", "w1", 10, True, None, 25, 0, "new"),
        ("trunk", "w2", 15, False, 20, 20, 1, "temp"),
        ("trunk", "w3", 35, True, None, 35, 2, "fresh"),
        ("trunk", "w5", 5, False, 40, 40, 3, "a"),
    ]
    history_rows = [
        ("trunk", "widget", "w1", "status", 10, "new"),
        ("trunk", "widget", "w1", "status", 25, "active"),
        ("trunk", "widget", "w2", "status", 15, "temp"),
        ("trunk", "widget", "w3", "status", 35, "fresh"),
        ("trunk", "widget", "w5", "status", 5, "a"),
        ("trunk", "widget", "w5", "status", 40, "b"),
    ]
    return build_data_emit(
        tmp_path,
        records=[RecordSpec("widget", _WIDGET_COLS, record_rows)],
        history_rows=history_rows,
    )


def _r_events(events: Iterable["StreamEvent"]) -> list["StreamEvent"]:
    """The 'r'-op subset of an event sequence, in order."""
    return [e for e in events if e.op == "r"]


# ---------------------------------------------------------------------------
# Compaction, coincidence, and phase content
# ---------------------------------------------------------------------------


class TestSnapshotContent:
    def test_created_at_exactly_t_is_in_r_phase_c_not_replayed(
        self, tmp_path: "Path"
    ) -> None:
        emit_dir = _build_widget_scenario(tmp_path)
        config = state_changes_config([kind_stream("widgets", "widget", ["status"])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            events = list(head.seek(10))
        w1_events = [e for e in events if e.record_id == "w1"]
        # w1's later u@25 still arrives via the live tail; only its create
        # is suppressed — replaced by the r-phase snapshot.
        assert not any(e.op == "c" for e in w1_events)
        r_event = next(e for e in w1_events if e.op == "r")
        assert r_event.after == {"record_id": "w1", "status": "new"}

    def test_created_and_deleted_before_t_absent_entirely(
        self, tmp_path: "Path"
    ) -> None:
        emit_dir = _build_widget_scenario(tmp_path)
        config = state_changes_config([kind_stream("widgets", "widget", ["status"])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            events = list(head.seek(25))
        assert not any(e.record_id == "w2" for e in events)

    def test_created_after_t_arrives_via_c_in_live_phase_only(
        self, tmp_path: "Path"
    ) -> None:
        emit_dir = _build_widget_scenario(tmp_path)
        config = state_changes_config([kind_stream("widgets", "widget", ["status"])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            events = list(head.seek(25))
        w3_events = [e for e in events if e.record_id == "w3"]
        assert len(w3_events) == 1
        assert w3_events[0].op == "c"

    def test_update_at_exactly_t_folded_not_replayed(self, tmp_path: "Path") -> None:
        emit_dir = _build_widget_scenario(tmp_path)
        config = state_changes_config([kind_stream("widgets", "widget", ["status"])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            events = list(head.seek(25))
        w1_events = [e for e in events if e.record_id == "w1"]
        assert len(w1_events) == 1
        assert w1_events[0].op == "r"
        assert w1_events[0].after == {"record_id": "w1", "status": "active"}

    def test_coincident_update_and_delete_at_t_absent_from_phase(
        self, tmp_path: "Path"
    ) -> None:
        emit_dir = _build_widget_scenario(tmp_path)
        config = state_changes_config([kind_stream("widgets", "widget", ["status"])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            events = list(head.seek(40))
        assert not any(e.record_id == "w5" for e in events)

    def test_no_record_live_at_t_empty_phase_then_live_stream(
        self, tmp_path: "Path"
    ) -> None:
        emit_dir = _build_widget_scenario(tmp_path)
        config = state_changes_config([kind_stream("widgets", "widget", ["status"])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            seek_events = list(head.seek(0))
            live_events = list(head.events(1, None))
        assert _r_events(seek_events) == []
        assert seek_events == live_events
        assert live_events[0].seq == 1  # N = 0 when T precedes every event


# ---------------------------------------------------------------------------
# 'r' field contract
# ---------------------------------------------------------------------------


class TestSnapshotFields:
    def test_shared_seq_event_sim_time_and_raw_ts(self, tmp_path: "Path") -> None:
        emit_dir = _build_widget_scenario(tmp_path)
        config = state_changes_config([kind_stream("widgets", "widget", ["status"])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            r_events = _r_events(head.seek(25))
        assert len(r_events) == 2  # w1 and w5 both live at T=25
        assert len({e.seq for e in r_events}) == 1
        assert all(e.event_sim_time == 25 for e in r_events)
        assert all(e.ts == 25 for e in r_events)

    def test_ts_rendered_under_anchor(self, tmp_path: "Path") -> None:
        emit_dir = _build_widget_scenario(tmp_path)
        config = state_changes_config([kind_stream("widgets", "widget", ["status"])])
        anchor = make_anchor()
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, anchor, discard_notice_sink)
            r_events = _r_events(head.seek(25))
        assert all(e.ts == render_ts(25, anchor) for e in r_events)

    def test_after_image_equals_same_instant_c_or_u_image(
        self, tmp_path: "Path"
    ) -> None:
        emit_dir = _build_widget_scenario(tmp_path)
        config = state_changes_config([kind_stream("widgets", "widget", ["status"])])
        with open_emit(emit_dir) as emit:
            resolution = resolve_streams(emit, config, discard_notice_sink)
            same_instant = list(
                iter_resolved_stream_events(emit, config, None, resolution, 25, 26)
            )
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            r_events = _r_events(head.seek(25))
        u_event = next(e for e in same_instant if e.record_id == "w1")
        r_event = next(e for e in r_events if e.record_id == "w1")
        assert u_event.op == "u"
        assert r_event.after == u_event.after


# ---------------------------------------------------------------------------
# Change scope does not narrow the phase
# ---------------------------------------------------------------------------


class TestChangeScopeDoesNotNarrowSnapshot:
    def test_ignored_property_still_snapshots_its_folded_state(
        self, tmp_path: "Path"
    ) -> None:
        record_rows = [("trunk", "g1", 5, True, None, 10, 0, "low")]
        history_rows = [
            ("trunk", "gauge", "g1", "status", 5, "low"),
            ("trunk", "gauge", "g1", "status", 10, "high"),
        ]
        emit_dir = build_data_emit(
            tmp_path,
            records=[RecordSpec("gauge", _WIDGET_COLS, record_rows)],
            history_rows=history_rows,
        )
        config = state_changes_config(
            [kind_stream("gauges", "gauge", ["status"], ignore=["status"])]
        )
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            full_play = list(head.events(None, None))
            r_events = _r_events(head.seek(15))
        # The ignored property never drives a 'u' event...
        assert [e.op for e in full_play] == ["c"]
        # ...yet the snapshot still reflects the folded post-creation state.
        assert len(r_events) == 1
        assert r_events[0].after == {"record_id": "g1", "status": "high"}


# ---------------------------------------------------------------------------
# sub_types / where scoping
# ---------------------------------------------------------------------------


class TestSnapshotScoping:
    _VEHICLE_COLS: list[dict[str, object]] = [
        *_LIFECYCLE_COLS,
        prop_column(
            "prop__vehicle_type",
            "VARCHAR",
            history_tracked=False,
            temporal_class="constant",
        ),
        prop_column(
            "prop__region", "VARCHAR", history_tracked=False, temporal_class="constant"
        ),
    ]

    def _build_vehicle_scenario(self, tmp_path: "Path") -> "Path":
        record_rows = [
            ("trunk", "veh1", 1, True, None, 1, 0, "car", "east"),
            ("trunk", "veh2", 1, True, None, 1, 1, "truck", "east"),
            ("trunk", "veh3", 1, True, None, 1, 2, "car", "west"),
        ]
        return build_data_emit(
            tmp_path,
            records=[RecordSpec("vehicle", self._VEHICLE_COLS, record_rows)],
            extra={
                "enum_domains": {
                    "vehicle": {"vehicle_type": enum_options("car", "truck")}
                }
            },
        )

    def test_sub_types_scope_excludes_out_of_scope_rows(self, tmp_path: "Path") -> None:
        emit_dir = self._build_vehicle_scenario(tmp_path)
        config = state_changes_config(
            [kind_stream("cars_only", "vehicle", ["region"], sub_types=["car"])]
        )
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            r_events = _r_events(head.seek(5))
        assert {e.record_id for e in r_events} == {"veh1", "veh3"}

    def test_where_scope_excludes_out_of_scope_rows(self, tmp_path: "Path") -> None:
        emit_dir = self._build_vehicle_scenario(tmp_path)
        config = state_changes_config(
            [kind_stream("east_only", "vehicle", ["region"], where={"region": "east"})]
        )
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            r_events = _r_events(head.seek(5))
        assert {e.record_id for e in r_events} == {"veh1", "veh2"}


# ---------------------------------------------------------------------------
# Phase order and overlapping streams
# ---------------------------------------------------------------------------


class TestSnapshotOrder:
    def test_order_is_stream_name_then_record_id(self, tmp_path: "Path") -> None:
        emit_dir = _build_widget_scenario(tmp_path)
        config = state_changes_config(
            [
                kind_stream("z_feed", "widget", ["status"]),
                kind_stream("a_feed", "widget", ["status"]),
            ]
        )
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            r_events = _r_events(head.seek(25))
        assert [(e.topic, e.record_id) for e in r_events] == [
            ("a_feed", "w1"),
            ("a_feed", "w5"),
            ("z_feed", "w1"),
            ("z_feed", "w5"),
        ]


# ---------------------------------------------------------------------------
# membership-events content: no per-key state, seek == events(T + 1, None)
# ---------------------------------------------------------------------------


class TestMembershipEventsSnapshotIsEmpty:
    _TEAM_COLS: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "joined_sim_time", "type": "BIGINT"},
        {"name": "left_sim_time", "type": "BIGINT"},
    ]

    def test_seek_equals_events_t_plus_1_with_empty_phase(
        self, tmp_path: "Path"
    ) -> None:
        emit_dir = build_data_emit(
            tmp_path,
            records=[RecordSpec("crew", _LIFECYCLE_COLS, [])],
            memberships=[
                MembershipSpec(
                    "crew",
                    "team",
                    self._TEAM_COLS,
                    [("trunk", "c1", 10, None), ("trunk", "c2", 20, 30)],
                )
            ],
        )
        config = membership_events_config(
            [membership_stream("team_feed", "crew", "team", [])]
        )
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            seek_events = list(head.seek(15))
            live_events = list(head.events(16, None))
        assert _r_events(seek_events) == []
        assert seek_events == live_events


# ---------------------------------------------------------------------------
# Seek-state equivalence
# ---------------------------------------------------------------------------


class TestSeekStateEquivalence:
    def test_seek_and_live_fold_matches_full_play_across_positions(
        self, tmp_path: "Path"
    ) -> None:
        emit_dir = _build_widget_scenario(tmp_path)
        config = state_changes_config([kind_stream("widgets", "widget", ["status"])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            full_play_state = _fold_upsert_log(list(head.events(None, None)))

            for at_sim_time in (0, 12, 25, 40, 100):
                seek_events = list(head.seek(at_sim_time))
                seek_state = _fold_upsert_log(seek_events)
                assert seek_state == full_play_state, (
                    f"seek({at_sim_time}) + live must fold to the full-play state"
                )

                live_tail = list(head.events(at_sim_time + 1, None))
                assert seek_events[len(seek_events) - len(live_tail) :] == live_tail
