"""Tests for tier-1 snapshot / seek: PlaybackSnapshot, PlaybackPosition.

Materialized against minimal in-process emits built via _data_fixtures /
_scenario. Covers the Phase 7 spec's column-order contract, boundary
semantics, stamp semantics, seek composition, and accessor errors.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _support.sidecar_builder import identity_column, prop_column

from fabulexa_forge.anchor import render_ts
from fabulexa_forge.playback import (
    MembershipAtomSelection,
    PlaybackError,
    PlaybackSelection,
    RecordAtomSelection,
    open_playback,
)
from fabulexa_forge.reader.emit import open_emit

from ._data_fixtures import MembershipSpec, RecordSpec, build_data_emit
from ._scenario import (
    ENUM_DOMAINS,
    PATIENT_COLS,
    TEAM_COLS,
    WIDGET_COLS,
    build_full_scenario,
    full_selection,
    make_anchor,
)

# ---------------------------------------------------------------------------
# Column order + typed-at-zero-rows
# ---------------------------------------------------------------------------


class TestRecordStateColumns:
    def test_column_order_fold_then_stamp_then_ts(self, tmp_path: Path) -> None:
        emit_dir = build_full_scenario(tmp_path)
        anchor = make_anchor()
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, full_selection(), anchor)
            table = playback.snapshot(20).record_state("widget")

        assert table.column_names == [
            "record_id",
            "created_sim_time",
            "active",
            "deactivated_at",
            "prop__label",
            "prop__count",
            "sub_type",
            "created_sim_time_ts",
            "deactivated_at_ts",
        ]

    def test_no_ts_siblings_without_anchor(self, tmp_path: Path) -> None:
        emit_dir = build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, full_selection(), None)
            table = playback.snapshot(20).record_state("widget")

        assert "created_sim_time_ts" not in table.column_names
        assert "deactivated_at_ts" not in table.column_names

    def test_typed_at_zero_rows(self, tmp_path: Path) -> None:
        emit_dir = build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, full_selection(), None)
            table = playback.snapshot(5).record_state("widget")

        assert table.num_rows == 0
        assert "prop__label" in table.column_names

    def test_presentation_id_present_when_kind_carries_one(
        self, tmp_path: Path
    ) -> None:
        cols = [
            identity_column("fork_path", "VARCHAR"),
            identity_column("record_id", "VARCHAR"),
            {"name": "presentation_id", "type": "BIGINT"},
            {"name": "created_sim_time", "type": "BIGINT"},
            {"name": "active", "type": "BOOLEAN"},
            {"name": "deactivated_at", "type": "BIGINT"},
            {"name": "last_mutation_sim_time", "type": "BIGINT"},
            identity_column("record_index", "BIGINT"),
            prop_column(
                "prop__name",
                "VARCHAR",
                history_tracked=False,
                temporal_class="constant",
            ),
        ]
        rows = [("trunk", "g1", 5001, 10, True, None, 10, 0, "Alice")]
        emit_dir = build_data_emit(tmp_path, records=[RecordSpec("guest", cols, rows)])
        selection = PlaybackSelection(
            records=(RecordAtomSelection("guest", (), None, None),), memberships=()
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            table = playback.snapshot(10).record_state("guest")

        assert "presentation_id" in table.column_names
        assert table.column("presentation_id").to_pylist() == ["5001"]


class TestMembershipStateColumns:
    def test_left_sim_time_never_present_stamp_and_ts_ordered(
        self, tmp_path: Path
    ) -> None:
        emit_dir = build_full_scenario(tmp_path)
        anchor = make_anchor()
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, full_selection(), anchor)
            table = playback.snapshot(20).membership_state("patient", "team")

        assert table.column_names == [
            "record_id",
            "joined_sim_time",
            "elem__role",
            "owner_sub_type",
            "joined_sim_time_ts",
        ]
        assert "left_sim_time" not in table.column_names

    def test_typed_at_zero_rows(self, tmp_path: Path) -> None:
        emit_dir = build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, full_selection(), None)
            table = playback.snapshot(5).membership_state("patient", "team")

        assert table.num_rows == 0
        assert "elem__role" in table.column_names


# ---------------------------------------------------------------------------
# Population and boundary semantics
# ---------------------------------------------------------------------------


class TestBoundarySemantics:
    def test_record_created_after_t_absent(self, tmp_path: Path) -> None:
        emit_dir = build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, full_selection(), None)
            before = playback.snapshot(7).record_state("widget")
            at = playback.snapshot(8).record_state("widget")

        assert before.num_rows == 0
        assert at.column("record_id").to_pylist() == ["w1"]

    def test_zero_width_membership_interval_contains_no_t(self, tmp_path: Path) -> None:
        team_rows = [("trunk", "p1", 10, 10, "lead")]
        emit_dir = build_data_emit(
            tmp_path,
            records=[RecordSpec("patient", PATIENT_COLS, [])],
            memberships=[MembershipSpec("patient", "team", TEAM_COLS, team_rows)],
            extra=ENUM_DOMAINS,
        )
        selection = PlaybackSelection(
            records=(),
            memberships=(MembershipAtomSelection("patient", (), "team", None, None),),
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            table = playback.snapshot(10).membership_state("patient", "team")

        assert table.num_rows == 0

    def test_snapshot_0_includes_records_created_at_0(self, tmp_path: Path) -> None:
        widget_rows = [("trunk", "w0", 0, True, None, 0, 0, "Gadget", "1")]
        emit_dir = build_data_emit(
            tmp_path, records=[RecordSpec("widget", WIDGET_COLS, widget_rows)]
        )
        selection = PlaybackSelection(
            records=(RecordAtomSelection("widget", (), None, None),), memberships=()
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            table = playback.snapshot(0).record_state("widget")

        assert table.column("record_id").to_pylist() == ["w0"]

    def test_at_sim_time_past_slice_bound_final_state_no_error(
        self, tmp_path: Path
    ) -> None:
        emit_dir = build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, full_selection(), None)
            far_future = playback.snapshot(10_000).record_state("patient")

        rows = {r["record_id"]: r for r in far_future.to_pylist()}
        assert rows["p1"]["active"] is False
        assert rows["p1"]["deactivated_at"] == 25
        assert rows["p2"]["active"] is True


# ---------------------------------------------------------------------------
# Stamp semantics
# ---------------------------------------------------------------------------


class TestStampSemantics:
    def test_sub_type_null_for_non_subtyped_kind(self, tmp_path: Path) -> None:
        emit_dir = build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, full_selection(), None)
            table = playback.snapshot(20).record_state("widget")

        assert table.column("sub_type").to_pylist() == [None]

    def test_sub_type_null_for_null_discriminator_cell(self, tmp_path: Path) -> None:
        rows = [("trunk", "p1", 10, True, None, 10, 0, None, "Alice", "x")]
        emit_dir = build_data_emit(
            tmp_path,
            records=[RecordSpec("patient", PATIENT_COLS, rows)],
            extra=ENUM_DOMAINS,
        )
        selection = PlaybackSelection(
            records=(RecordAtomSelection("patient", (), None, None),), memberships=()
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            table = playback.snapshot(10).record_state("patient")

        assert table.column("sub_type").to_pylist() == [None]

    def test_sub_type_null_for_undeclared_discriminator(self, tmp_path: Path) -> None:
        cols = [
            *[c for c in PATIENT_COLS if c["name"] != "prop__patient_type"],
        ]
        rows = [("trunk", "p1", 10, True, None, 10, 0, "Alice", "x")]
        emit_dir = build_data_emit(
            tmp_path,
            records=[RecordSpec("drifted_patient", cols, rows)],
            extra={
                "enum_domains": {"drifted_patient": {"drifted_patient_type": ["a"]}}
            },
        )
        selection = PlaybackSelection(
            records=(RecordAtomSelection("drifted_patient", (), None, None),),
            memberships=(),
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            table = playback.snapshot(10).record_state("drifted_patient")

        assert table.column("sub_type").to_pylist() == [None]

    def test_sub_type_verbatim_for_out_of_domain_value(self, tmp_path: Path) -> None:
        rows = [("trunk", "p1", 10, True, None, 10, 0, "phantom", "Alice", "x")]
        emit_dir = build_data_emit(
            tmp_path,
            records=[RecordSpec("patient", PATIENT_COLS, rows)],
            extra=ENUM_DOMAINS,
        )
        selection = PlaybackSelection(
            records=(RecordAtomSelection("patient", (), None, None),), memberships=()
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            table = playback.snapshot(10).record_state("patient")

        assert table.column("sub_type").to_pylist() == ["phantom"]

    def test_owner_sub_type_null_for_orphan_membership_row(
        self, tmp_path: Path
    ) -> None:
        team_rows = [("trunk", "ghost123", 5, None, "lead")]
        emit_dir = build_data_emit(
            tmp_path,
            records=[RecordSpec("patient", PATIENT_COLS, [])],
            memberships=[MembershipSpec("patient", "team", TEAM_COLS, team_rows)],
            extra=ENUM_DOMAINS,
        )
        selection = PlaybackSelection(
            records=(),
            memberships=(MembershipAtomSelection("patient", (), "team", None, None),),
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            table = playback.snapshot(10).membership_state("patient", "team")

        assert table.column("record_id").to_pylist() == ["ghost123"]
        assert table.column("owner_sub_type").to_pylist() == [None]


# ---------------------------------------------------------------------------
# ts rendering
# ---------------------------------------------------------------------------


class TestTsRendering:
    def test_ts_sibling_matches_shared_renderer(self, tmp_path: Path) -> None:
        emit_dir = build_full_scenario(tmp_path)
        anchor = make_anchor()
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, full_selection(), anchor)
            table = playback.snapshot(20).record_state("widget")

        row = table.to_pylist()[0]
        assert row["created_sim_time_ts"] == render_ts(row["created_sim_time"], anchor)


# ---------------------------------------------------------------------------
# seek
# ---------------------------------------------------------------------------


class TestSeek:
    def test_position_snapshot_equals_playback_snapshot(self, tmp_path: Path) -> None:
        emit_dir = build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, full_selection(), None)
            position = playback.seek(20)
            from_position = position.snapshot().record_state("widget").to_pylist()
            from_playback = playback.snapshot(20).record_state("widget").to_pylist()

        assert from_position == from_playback

    def test_position_events_equal_events_t_plus_1(self, tmp_path: Path) -> None:
        emit_dir = build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, full_selection(), None)
            position = playback.seek(20)
            from_position = list(position.events())
            from_playback = list(playback.events(21, None))

        assert [(e.seq, e.op, e.record_id) for e in from_position] == [
            (e.seq, e.op, e.record_id) for e in from_playback
        ]

    def test_both_halves_lazy_and_independently_pullable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        emit_dir = build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, full_selection(), None)
            calls = {"n": 0}
            original_query_arrow = emit.query_arrow

            def _tracking_query_arrow(
                sql: str, parameters: tuple[object, ...]
            ) -> object:
                calls["n"] += 1
                return original_query_arrow(sql, parameters)

            monkeypatch.setattr(emit, "query_arrow", _tracking_query_arrow)

            position = playback.seek(20)
            events_iter = position.events()
            assert calls["n"] == 0

            next(events_iter)
            assert calls["n"] == 0  # events reads via emit.query, not query_arrow

            position.snapshot().record_state("widget")
            assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Accessor errors
# ---------------------------------------------------------------------------


class TestAccessorErrors:
    def test_unselected_kind_raises(self, tmp_path: Path) -> None:
        emit_dir = build_full_scenario(tmp_path)
        selection = PlaybackSelection(
            records=(RecordAtomSelection("widget", (), None, None),), memberships=()
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            snapshot = playback.snapshot(20)
            with pytest.raises(PlaybackError):
                snapshot.record_state("patient")

    def test_unselected_membership_raises(self, tmp_path: Path) -> None:
        emit_dir = build_full_scenario(tmp_path)
        selection = PlaybackSelection(
            records=(),
            memberships=(MembershipAtomSelection("widget", (), "tags", None, None),),
        )
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, selection, None)
            snapshot = playback.snapshot(20)
            with pytest.raises(PlaybackError):
                snapshot.membership_state("patient", "team")

    def test_repeated_access_returns_identical_table(self, tmp_path: Path) -> None:
        emit_dir = build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, full_selection(), None)
            snapshot = playback.snapshot(20)
            first = snapshot.record_state("widget")
            second = snapshot.record_state("widget")

        assert first is second

    def test_repeated_membership_access_returns_identical_table(
        self, tmp_path: Path
    ) -> None:
        emit_dir = build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, full_selection(), None)
            snapshot = playback.snapshot(20)
            first = snapshot.membership_state("widget", "tags")
            second = snapshot.membership_state("widget", "tags")

        assert first is second


# ---------------------------------------------------------------------------
# at_sim_time validation
# ---------------------------------------------------------------------------


class TestAtSimTimeBounds:
    def test_negative_snapshot_raises(self, tmp_path: Path) -> None:
        emit_dir = build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, full_selection(), None)
            with pytest.raises(PlaybackError):
                playback.snapshot(-1)

    def test_negative_seek_raises(self, tmp_path: Path) -> None:
        emit_dir = build_full_scenario(tmp_path)
        with open_emit(emit_dir) as emit:
            playback = open_playback(emit, full_selection(), None)
            with pytest.raises(PlaybackError):
                playback.seek(-1)
