"""Tests for the playback event stream: PlaybackEvent, open_playback, events.

Materialized against minimal in-process emits built via _data_fixtures. Tests
cover the Phase 6 spec's canonical order, seq entry-point invariance,
projection/population semantics, ts rendering, laziness, bounds, and
permissive playback over corrupted tapes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from _support.sidecar_builder import identity_column, prop_column

from fabulexa_forge.anchor import EffectiveAnchor, render_ts
from fabulexa_forge.errors import ExportError
from fabulexa_forge.playback import (
    MembershipAtomSelection,
    PlaybackError,
    PlaybackEvent,
    PlaybackSelection,
    RecordAtomSelection,
    open_playback,
)
from fabulexa_forge.reader.emit import open_emit

from ._data_fixtures import MembershipSpec, RecordSpec, build_data_emit

# ---------------------------------------------------------------------------
# Shared column shapes
# ---------------------------------------------------------------------------

_LIFECYCLE_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]

_PATIENT_COLS: list[dict[str, object]] = [
    *_LIFECYCLE_COLS,
    prop_column(
        "prop__patient_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
    ),
    prop_column(
        "prop__name", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_WIDGET_COLS: list[dict[str, object]] = [
    *_LIFECYCLE_COLS,
    prop_column(
        "prop__label", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__count", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_TEAM_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__role", "type": "VARCHAR"},
]

_TAGS_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__tag", "type": "VARCHAR"},
]

_ENUM_DOMAINS = {"enum_domains": {"patient": {"patient_type": ["doctor", "nurse"]}}}


def _make_anchor() -> EffectiveAnchor:
    """A fixed UTC anchor for ts-rendering tests."""
    return EffectiveAnchor(
        start_instant=datetime(2026, 1, 1, tzinfo=timezone.utc),
        timezone=ZoneInfo("UTC"),
    )


def _build_full_scenario(tmp_path: Path) -> Path:
    """Two record kinds + two membership tables, spanning a cross-family tape.

    patient (sub-typed doctor/nurse): p1 (doctor) c@10 u@15 d@25;
    p2 (nurse) c@12. widget (not sub-typed): w1 c@8 u@20.
    membership__patient__team: p1 join@10 leave@25; p2 join@12.
    membership__widget__tags: w1 join@8.
    """
    patient_rows = [
        ("trunk", "p1", 10, False, 25, 25, 0, "doctor", "Alice", "checked-in"),
        ("trunk", "p2", 12, True, None, 12, 1, "nurse", "Bob", "waiting"),
    ]
    widget_rows = [
        ("trunk", "w1", 8, True, None, 20, 0, "Gadget", "2"),
    ]
    team_rows = [
        ("trunk", "p1", 10, 25, "lead"),
        ("trunk", "p2", 12, None, "member"),
    ]
    tags_rows = [
        ("trunk", "w1", 8, None, "blue"),
    ]
    history_rows = [
        ("trunk", "patient", "p1", "status", 10, "waiting"),
        ("trunk", "patient", "p1", "status", 15, "checked-in"),
        ("trunk", "patient", "p2", "status", 12, "waiting"),
        ("trunk", "widget", "w1", "count", 8, "1"),
        ("trunk", "widget", "w1", "count", 20, "2"),
    ]
    return build_data_emit(
        tmp_path,
        records=[
            RecordSpec("patient", _PATIENT_COLS, patient_rows),
            RecordSpec("widget", _WIDGET_COLS, widget_rows),
        ],
        memberships=[
            MembershipSpec("patient", "team", _TEAM_COLS, team_rows),
            MembershipSpec("widget", "tags", _TAGS_COLS, tags_rows),
        ],
        history_rows=history_rows,
        extra=_ENUM_DOMAINS,
    )


def _full_selection() -> PlaybackSelection:
    """Select every atom of the full scenario, full properties/fields."""
    return PlaybackSelection(
        records=(
            RecordAtomSelection("patient", (), None, None),
            RecordAtomSelection("widget", (), None, None),
        ),
        memberships=(
            MembershipAtomSelection("patient", (), "team", None, None),
            MembershipAtomSelection("widget", (), "tags", None, None),
        ),
    )


def _keys(events: list[PlaybackEvent]) -> list[tuple[str, str, str]]:
    """(op, kind-or-owner_kind, record_id) triples, for compact order assertions."""
    result: list[tuple[str, str, str]] = []
    for e in events:
        kind = getattr(e.atom, "kind", None) or getattr(e.atom, "owner_kind")
        result.append((e.op, kind, e.record_id))
    return result


# ---------------------------------------------------------------------------
# Canonical order
# ---------------------------------------------------------------------------


class TestCanonicalOrder:
    def test_owner_create_precedes_coincident_join(self, tmp_path: Path) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _full_selection(), None)
            events = list(playback.events(None, None))

        ordered = _keys(events)
        assert ordered.index(("c", "patient", "p1")) < ordered.index(
            ("join", "patient", "p1")
        )

    def test_leave_precedes_owner_coincident_delete(self, tmp_path: Path) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _full_selection(), None)
            events = list(playback.events(None, None))

        ordered = _keys(events)
        assert ordered.index(("leave", "patient", "p1")) < ordered.index(
            ("d", "patient", "p1")
        )

    def test_full_cross_family_order(self, tmp_path: Path) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _full_selection(), None)
            events = list(playback.events(None, None))

        assert _keys(events) == [
            ("c", "widget", "w1"),
            ("join", "widget", "w1"),
            ("c", "patient", "p1"),
            ("join", "patient", "p1"),
            ("c", "patient", "p2"),
            ("join", "patient", "p2"),
            ("u", "patient", "p1"),
            ("u", "widget", "w1"),
            ("leave", "patient", "p1"),
            ("d", "patient", "p1"),
        ]

    def test_source_identity_tiebreak_two_kinds(self, tmp_path: Path) -> None:
        """Two record kinds tie at the same (sim_time, class) — kind orders it."""
        alpha_rows = [("trunk", "a1", 10, True, None, 10, 0, "x")]
        beta_rows = [("trunk", "b1", 10, True, None, 10, 0, "y")]
        cols = [
            *_LIFECYCLE_COLS,
            prop_column(
                "prop__label",
                "VARCHAR",
                history_tracked=False,
                temporal_class="constant",
            ),
        ]
        emit_dir = build_data_emit(
            tmp_path,
            records=[
                RecordSpec("alpha", cols, alpha_rows),
                RecordSpec("beta", cols, beta_rows),
            ],
        )
        selection = PlaybackSelection(
            records=(
                RecordAtomSelection("alpha", (), None, None),
                RecordAtomSelection("beta", (), None, None),
            ),
            memberships=(),
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            events = list(playback.events(None, None))

        kinds = [e.atom.kind for e in events]
        assert kinds == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# seq
# ---------------------------------------------------------------------------


class TestSeq:
    def test_entry_point_invariant(self, tmp_path: Path) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _full_selection(), None)
            full = list(playback.events(None, None))

        cut_time = full[4].event_sim_time
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _full_selection(), None)
            tail = list(playback.events(cut_time, None))

        expected_tail = [e for e in full if e.event_sim_time >= cut_time]
        assert [e.seq for e in tail] == [e.seq for e in expected_tail]
        assert [(e.op, e.record_id) for e in tail] == [
            (e.op, e.record_id) for e in expected_tail
        ]

    def test_duplicate_membership_intervals_tie_deterministically(
        self, tmp_path: Path
    ) -> None:
        """Byte-identical duplicate intervals get consecutive seq, stable order."""
        team_rows = [
            ("trunk", "p1", 10, None, "lead"),
            ("trunk", "p1", 10, None, "lead"),
        ]
        emit_dir = build_data_emit(
            tmp_path,
            records=[RecordSpec("patient", _PATIENT_COLS[:-1], [])],
            memberships=[MembershipSpec("patient", "team", _TEAM_COLS, team_rows)],
        )
        selection = PlaybackSelection(
            records=(),
            memberships=(MembershipAtomSelection("patient", (), "team", None, None),),
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            events1 = list(playback.events(None, None))
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            events2 = list(playback.events(None, None))

        assert len(events1) == 2
        assert [e.seq for e in events1] == [1, 2]
        assert [e.after for e in events1] == [e.after for e in events2]


# ---------------------------------------------------------------------------
# Projection (properties / fields)
# ---------------------------------------------------------------------------


class TestProjection:
    def test_u_touching_only_unselected_properties_still_plays(
        self, tmp_path: Path
    ) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        selection = PlaybackSelection(
            records=(RecordAtomSelection("patient", (), ("name",), None),),
            memberships=(),
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            events = list(playback.events(None, None))

        updates = [e for e in events if e.op == "u"]
        assert len(updates) == 1
        assert updates[0].after == {"record_id": "p1", "prop__name": "Alice"}

    def test_seq_invariant_under_properties_selection(self, tmp_path: Path) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _full_selection(), None)
            full = list(playback.events(None, None))

        narrow = PlaybackSelection(
            records=(
                RecordAtomSelection("patient", (), (), None),
                RecordAtomSelection("widget", (), (), None),
            ),
            memberships=(
                MembershipAtomSelection("patient", (), "team", (), None),
                MembershipAtomSelection("widget", (), "tags", (), None),
            ),
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, narrow, None)
            narrowed = list(playback.events(None, None))

        assert [e.seq for e in narrowed] == [e.seq for e in full]
        assert [(e.op, e.record_id) for e in narrowed] == [
            (e.op, e.record_id) for e in full
        ]

    def test_empty_fields_projects_to_identity_only(self, tmp_path: Path) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        selection = PlaybackSelection(
            records=(),
            memberships=(MembershipAtomSelection("patient", (), "team", (), None),),
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            events = list(playback.events(None, None))

        for e in events:
            assert e.after == {"record_id": e.record_id}


# ---------------------------------------------------------------------------
# Population restriction
# ---------------------------------------------------------------------------


class TestPopulationRestriction:
    def test_sub_types_restriction_changes_scope(self, tmp_path: Path) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        selection = PlaybackSelection(
            records=(RecordAtomSelection("patient", ("doctor",), None, None),),
            memberships=(),
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            events = list(playback.events(None, None))

        assert {e.record_id for e in events} == {"p1"}

    def test_record_ids_restriction_is_pure_row_selection(self, tmp_path: Path) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _full_selection(), None)
            full = list(playback.events(None, None))
        unrestricted_p1 = [
            e for e in full if e.record_id == "p1" and e.op in ("c", "u", "d")
        ]

        restricted = PlaybackSelection(
            records=(RecordAtomSelection("patient", (), None, frozenset({"p1"})),),
            memberships=(),
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, restricted, None)
            events = list(playback.events(None, None))

        assert [e.record_id for e in events] == ["p1", "p1", "p1"]
        assert [(e.op, e.after) for e in events] == [
            (e.op, e.after) for e in unrestricted_p1
        ]


# ---------------------------------------------------------------------------
# ts rendering
# ---------------------------------------------------------------------------


class TestTsRendering:
    def test_ts_with_anchor_matches_shared_renderer(self, tmp_path: Path) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        anchor = _make_anchor()
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _full_selection(), anchor)
            events = list(playback.events(None, None))

        for e in events:
            assert e.ts == render_ts(e.event_sim_time, anchor)
            assert isinstance(e.ts, str)

    def test_ts_without_anchor_is_raw_int(self, tmp_path: Path) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _full_selection(), None)
            events = list(playback.events(None, None))

        for e in events:
            assert e.ts == e.event_sim_time
            assert isinstance(e.ts, int)


# ---------------------------------------------------------------------------
# Laziness + independent pullability
# ---------------------------------------------------------------------------


class TestLaziness:
    def test_no_reads_until_pulled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _full_selection(), None)
            calls = {"n": 0}
            original_query = emit.query

            def _tracking_query(
                sql: str, parameters: tuple[object, ...]
            ) -> list[tuple[object, ...]]:
                calls["n"] += 1
                return original_query(sql, parameters)

            monkeypatch.setattr(emit, "query", _tracking_query)

            iterator = playback.events(None, None)
            assert calls["n"] == 0

            next(iterator)
            assert calls["n"] > 0

    def test_two_iterators_advance_independently(self, tmp_path: Path) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _full_selection(), None)
            it1 = playback.events(None, None)
            it2 = playback.events(None, None)

            first1 = next(it1)
            first2 = next(it2)
            assert first1 == first2

            second1 = next(it1)
            # it2 has only been advanced once so far
            second2 = next(it2)
            assert second1 == second2


# ---------------------------------------------------------------------------
# Bounds + open_playback error passthrough
# ---------------------------------------------------------------------------


class TestBounds:
    def test_equal_bounds_yields_empty(self, tmp_path: Path) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _full_selection(), None)
            events = list(playback.events(10, 10))

        assert events == []

    def test_start_greater_than_end_raises(self, tmp_path: Path) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _full_selection(), None)
            with pytest.raises(PlaybackError):
                playback.events(20, 10)

    def test_negative_start_raises(self, tmp_path: Path) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _full_selection(), None)
            with pytest.raises(PlaybackError):
                playback.events(-1, None)

    def test_negative_end_raises(self, tmp_path: Path) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, _full_selection(), None)
            with pytest.raises(PlaybackError):
                playback.events(None, -1)

    def test_open_playback_performs_no_table_reads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:

            def _fail_query(
                sql: str, parameters: tuple[object, ...]
            ) -> list[tuple[object, ...]]:
                raise AssertionError("open_playback must not read tables")

            monkeypatch.setattr(emit, "query", _fail_query)
            open_playback(emit, _full_selection(), None)

    def test_open_playback_passes_through_single_branch_guard(
        self, tmp_path: Path
    ) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        # Rewrite the sidecar's branches list to enumerate two branches.
        import json

        base_json = emit_dir / "base.json"
        raw = json.loads(base_json.read_text(encoding="utf-8"))
        raw["branches"].append(
            {"fork_path": "trunk@alt", "parent": "trunk", "slice_at": 100}
        )
        base_json.write_text(json.dumps(raw), encoding="utf-8")

        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError):
                open_playback(emit, _full_selection(), None)


# ---------------------------------------------------------------------------
# Corrupted tapes: permissive playback
# ---------------------------------------------------------------------------


class TestCorruptedTapes:
    def test_resampled_discriminator_plays_as_cell_value(self, tmp_path: Path) -> None:
        rows = [("trunk", "p1", 10, True, None, 10, 0, "phantom", "Alice", "x")]
        emit_dir = build_data_emit(
            tmp_path,
            records=[RecordSpec("patient", _PATIENT_COLS, rows)],
            extra=_ENUM_DOMAINS,
        )
        selection = PlaybackSelection(
            records=(RecordAtomSelection("patient", (), None, None),), memberships=()
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            events = list(playback.events(None, None))

        assert events[0].atom.sub_type == "phantom"

    def test_string_dirt_and_null_stamp_verbatim(self, tmp_path: Path) -> None:
        rows = [
            ("trunk", "p1", 10, True, None, 10, 0, "###dirt###", "Alice", "x"),
            ("trunk", "p2", 11, True, None, 11, 1, None, "Bob", "y"),
        ]
        emit_dir = build_data_emit(
            tmp_path,
            records=[RecordSpec("patient", _PATIENT_COLS, rows)],
            extra=_ENUM_DOMAINS,
        )
        selection = PlaybackSelection(
            records=(RecordAtomSelection("patient", (), None, None),), memberships=()
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            events = {e.record_id: e for e in playback.events(None, None)}

        assert events["p1"].atom.sub_type == "###dirt###"
        assert events["p2"].atom.sub_type is None

    def test_orphan_membership_row_plays_with_owner_sub_type_null(
        self, tmp_path: Path
    ) -> None:
        team_rows = [("trunk", "ghost123", 5, None, "lead")]
        emit_dir = build_data_emit(
            tmp_path,
            records=[RecordSpec("patient", _PATIENT_COLS, [])],
            memberships=[MembershipSpec("patient", "team", _TEAM_COLS, team_rows)],
            extra=_ENUM_DOMAINS,
        )
        selection = PlaybackSelection(
            records=(),
            memberships=(MembershipAtomSelection("patient", (), "team", None, None),),
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            events = list(playback.events(None, None))

        assert len(events) == 1
        assert events[0].record_id == "ghost123"
        assert events[0].atom.owner_sub_type is None

    def test_deleted_records_ids_select_nothing(self, tmp_path: Path) -> None:
        emit_dir = _build_full_scenario(tmp_path)
        selection = PlaybackSelection(
            records=(
                RecordAtomSelection("patient", (), None, frozenset({"deleted-pin-id"})),
            ),
            memberships=(),
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            events = list(playback.events(None, None))

        assert events == []
