"""The consistency algebra: snapshot(T2-1) == snapshot(T1-1) OP events(T1, T2).

For several (T1, T2) pairs — including a pair whose boundary excludes a
coincident-instant event pair and a pair whose boundary includes it —
replaying events(T1, T2) in seq order over snapshot(T1-1)'s Python-side
state ('c' insert, 'u' replace, 'd' deactivate at the event key, 'join' add,
'leave' remove one matching row) reproduces snapshot(T2-1) exactly.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from fabulexa_forge.playback import PlaybackEvent, RecordAtom, open_playback
from fabulexa_forge.reader.emit import open_emit

from ._scenario import build_full_scenario, full_selection

if TYPE_CHECKING:
    import pyarrow

    from fabulexa_forge.playback.head import Playback

_RecordRow = dict[str, object]
_MembershipRow = dict[str, object]

_RECORD_KINDS: tuple[str, ...] = ("patient", "widget")
_MEMBERSHIPS: tuple[tuple[str, str], ...] = (("patient", "team"), ("widget", "tags"))


def _table_rows_by_record_id(table: "pyarrow.Table") -> dict[str, _RecordRow]:
    """A record_state table's rows keyed by record_id."""
    return {str(row["record_id"]): row for row in table.to_pylist()}


def _sorted_membership_rows(table: "pyarrow.Table") -> list[_MembershipRow]:
    """A membership_state table's rows, in a comparison-stable order."""
    rows = table.to_pylist()
    return sorted(rows, key=lambda row: (str(row["record_id"]), row["joined_sim_time"]))


def _record_state_dict(
    playback: "Playback", at_sim_time: int
) -> dict[str, dict[str, _RecordRow]]:
    """Every selected kind's record_state rows at T, keyed by kind then id."""
    snapshot = playback.snapshot(at_sim_time)
    return {
        kind: _table_rows_by_record_id(snapshot.record_state(kind))
        for kind in _RECORD_KINDS
    }


def _membership_state_dict(
    playback: "Playback", at_sim_time: int
) -> dict[tuple[str, str], list[_MembershipRow]]:
    """Every selected membership table's containment rows at T, sorted."""
    snapshot = playback.snapshot(at_sim_time)
    return {
        key: _sorted_membership_rows(snapshot.membership_state(*key))
        for key in _MEMBERSHIPS
    }


def _apply_record_event(records: dict[str, _RecordRow], event: PlaybackEvent) -> None:
    """Apply one 'c' / 'u' / 'd' event onto a record_id -> row dict, in place."""
    if event.op == "c":
        assert event.after is not None
        row: _RecordRow = dict(event.after)
        row["created_sim_time"] = event.event_sim_time
        row["active"] = True
        row["deactivated_at"] = None
        row["sub_type"] = event.atom.sub_type
        records[event.record_id] = row
    elif event.op == "u":
        assert event.after is not None
        records[event.record_id].update(event.after)
    else:  # 'd'
        records[event.record_id]["active"] = False
        records[event.record_id]["deactivated_at"] = event.event_sim_time


def _apply_membership_event(rows: list[_MembershipRow], event: PlaybackEvent) -> None:
    """Apply one 'join' / 'leave' event: 'join' adds a row, 'leave' removes
    one matching row (matched on record_id plus every payload field)."""
    if event.op == "join":
        assert event.after is not None
        row: _MembershipRow = dict(event.after)
        row["joined_sim_time"] = event.event_sim_time
        row["owner_sub_type"] = event.atom.owner_sub_type
        rows.append(row)
        return

    assert event.after is not None
    payload = {key: value for key, value in event.after.items() if key != "record_id"}
    for index, row in enumerate(rows):
        if row["record_id"] != event.record_id:
            continue
        if all(row[key] == value for key, value in payload.items()):
            del rows[index]
            break


def _apply_events(
    records: dict[str, dict[str, _RecordRow]],
    memberships: dict[tuple[str, str], list[_MembershipRow]],
    events: list[PlaybackEvent],
) -> None:
    """Apply a seq-ordered event window onto the algebra's Python-side state."""
    for event in events:
        if isinstance(event.atom, RecordAtom):
            _apply_record_event(records.setdefault(event.atom.kind, {}), event)
        else:
            key = (event.atom.owner_kind, event.atom.property_name)
            _apply_membership_event(memberships.setdefault(key, []), event)


@pytest.mark.parametrize(
    "t1,t2",
    [
        (1, 9),  # tape start through widget's creation/join at t=8
        (9, 11),  # patient p1's creation/join at t=10
        (11, 13),  # patient p2's creation/join at t=12
        (13, 16),  # patient p1's status update at t=15
        (16, 21),  # widget's count update at t=20
        (21, 25),  # boundary excludes the coincident t=25 leave/d pair
        (21, 26),  # boundary includes the coincident t=25 leave/d pair
    ],
)
def test_snapshot_t2_minus_1_equals_snapshot_t1_minus_1_plus_events(
    tmp_path: Path, t1: int, t2: int
) -> None:
    emit_dir = build_full_scenario(tmp_path)
    with open_emit(emit_dir) as emit:
        playback = open_playback(emit, full_selection(), None)

        records = _record_state_dict(playback, t1 - 1)
        memberships = _membership_state_dict(playback, t1 - 1)
        events = list(playback.events(t1, t2))
        _apply_events(records, memberships, events)

        expected_records = _record_state_dict(playback, t2 - 1)
        expected_memberships = _membership_state_dict(playback, t2 - 1)

    for kind in _RECORD_KINDS:
        assert records[kind] == expected_records[kind]
    for key in _MEMBERSHIPS:
        got = sorted(
            memberships[key],
            key=lambda row: (str(row["record_id"]), row["joined_sim_time"]),
        )
        assert got == expected_memberships[key]


def test_algebra_over_the_whole_tape(tmp_path: Path) -> None:
    """T1=1, T2 past the tape's end: the same identity over the full span."""
    emit_dir = build_full_scenario(tmp_path)
    with open_emit(emit_dir) as emit:
        playback = open_playback(emit, full_selection(), None)

        records = _record_state_dict(playback, 0)
        memberships = _membership_state_dict(playback, 0)
        events = list(playback.events(1, None))
        _apply_events(records, memberships, events)

        expected_records = _record_state_dict(playback, 1_000)
        expected_memberships = _membership_state_dict(playback, 1_000)

    for kind in _RECORD_KINDS:
        assert records[kind] == expected_records[kind]
    for key in _MEMBERSHIPS:
        got = sorted(
            memberships[key],
            key=lambda row: (str(row["record_id"]), row["joined_sim_time"]),
        )
        assert got == expected_memberships[key]
