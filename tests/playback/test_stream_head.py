"""Tests for tier-2 stream playback open: open_stream_playback, StreamPlayback
`topics()` / `events()`, and the seam's PlaybackError bound checks.

Materialized against minimal in-process emits built via _data_fixtures.
Covers declared-topic independence from data, bounded/whole-tape equality
against the engine's own resolved iterator, PlaybackError vs. data
conditions, open-time eager validation and laziness, and independently
pullable heads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink
from _support.sidecar_builder import enum_options, identity_column, prop_column

from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.streaming.engine import (
    iter_resolved_stream_events,
    resolve_streams,
)
from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.playback.stream import open_stream_playback
from fabulexa_forge.reader.emit import open_emit

from ._data_fixtures import RecordSpec, build_data_emit
from ._stream_config import kind_stream, state_changes_config

if TYPE_CHECKING:
    from pathlib import Path

_RECORD_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]


def _build_two_kind_emit(tmp_path: "Path") -> "Path":
    """One emit, two kinds: 'alpha' (two records + a history update) and
    'beta' (declared-but-empty — zero rows)."""
    alpha_rows = [
        ("trunk", "a1", 10, True, None, 10, 0, "new"),
        ("trunk", "a2", 20, True, None, 30, 1, "new"),
    ]
    alpha_history = [("trunk", "alpha", "a2", "status", 30, "active")]
    return build_data_emit(
        tmp_path,
        records=[
            RecordSpec("alpha", _RECORD_COLS, alpha_rows),
            RecordSpec("beta", _RECORD_COLS, []),
        ],
        history_rows=alpha_history,
    )


def _enum_where_scenario(tmp_path: "Path") -> "Path":
    """One emit, kind 'item', prop__status carries an enum_domains entry
    ('open', 'closed') that a `where` value outside the declared domain
    triggers the out-of-domain notice against."""
    cols = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        prop_column(
            "prop__status", "VARCHAR", history_tracked=False, temporal_class="constant"
        ),
    ]
    return build_data_emit(
        tmp_path,
        records=[
            RecordSpec("item", cols, [("trunk", "i1", 10, True, None, 10, 0, "open")])
        ],
        extra={"enum_domains": {"item": {"status": enum_options("open", "closed")}}},
    )


# ---------------------------------------------------------------------------
# topics()
# ---------------------------------------------------------------------------


class TestTopics:
    def test_declaration_order_independent_of_data(self, tmp_path: "Path") -> None:
        """topics() returns declared stream names in declaration order, even
        when the later-declared stream's kind has zero rows."""
        emit_dir = _build_two_kind_emit(tmp_path)
        config = state_changes_config(
            [
                kind_stream("b_feed", "beta", []),
                kind_stream("a_feed", "alpha", []),
            ]
        )
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            assert head.topics() == ("b_feed", "a_feed")


# ---------------------------------------------------------------------------
# events(): equals the engine, bounds are total
# ---------------------------------------------------------------------------


class TestEvents:
    def test_whole_tape_equals_engine(self, tmp_path: "Path") -> None:
        emit_dir = _build_two_kind_emit(tmp_path)
        config = state_changes_config([kind_stream("alphas", "alpha", ["status"])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            from_head = list(head.events(None, None))
            resolution = resolve_streams(emit, config, discard_notice_sink)
            from_engine = list(
                iter_resolved_stream_events(emit, config, None, resolution, None, None)
            )
        assert from_head == from_engine
        assert len(from_head) == 3  # a1 create, a2 create, a2 update

    def test_bounded_ask_equals_engine_bounded_ask(self, tmp_path: "Path") -> None:
        emit_dir = _build_two_kind_emit(tmp_path)
        config = state_changes_config([kind_stream("alphas", "alpha", ["status"])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            from_head = list(head.events(15, 30))
            resolution = resolve_streams(emit, config, discard_notice_sink)
            from_engine = list(
                iter_resolved_stream_events(emit, config, None, resolution, 15, 30)
            )
        assert from_head == from_engine
        assert [e.record_id for e in from_head] == ["a2"]

    def test_bound_past_tape_is_a_data_condition_not_an_error(
        self, tmp_path: "Path"
    ) -> None:
        emit_dir = _build_two_kind_emit(tmp_path)
        config = state_changes_config([kind_stream("alphas", "alpha", [])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            assert list(head.events(1_000_000, None)) == []

    def test_start_greater_than_end_raises_playback_error(
        self, tmp_path: "Path"
    ) -> None:
        emit_dir = _build_two_kind_emit(tmp_path)
        config = state_changes_config([kind_stream("alphas", "alpha", [])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            with pytest.raises(PlaybackError):
                head.events(30, 10)

    def test_negative_start_raises_playback_error(self, tmp_path: "Path") -> None:
        emit_dir = _build_two_kind_emit(tmp_path)
        config = state_changes_config([kind_stream("alphas", "alpha", [])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            with pytest.raises(PlaybackError):
                head.events(-1, None)

    def test_negative_end_raises_playback_error(self, tmp_path: "Path") -> None:
        emit_dir = _build_two_kind_emit(tmp_path)
        config = state_changes_config([kind_stream("alphas", "alpha", [])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            with pytest.raises(PlaybackError):
                head.events(None, -1)


class TestSeekBoundCheck:
    def test_negative_position_raises_playback_error(self, tmp_path: "Path") -> None:
        emit_dir = _build_two_kind_emit(tmp_path)
        config = state_changes_config([kind_stream("alphas", "alpha", [])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            with pytest.raises(PlaybackError):
                head.seek(-1)

    def test_position_past_tape_is_a_data_condition_not_an_error(
        self, tmp_path: "Path"
    ) -> None:
        emit_dir = _build_two_kind_emit(tmp_path)
        config = state_changes_config([kind_stream("alphas", "alpha", [])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)
            list(head.seek(1_000_000))  # no raise


# ---------------------------------------------------------------------------
# Open-time behavior: eager gates, notices, laziness
# ---------------------------------------------------------------------------


class TestOpenTimeBehavior:
    def test_failing_gate_raises_export_error_before_any_pull(
        self, tmp_path: "Path"
    ) -> None:
        emit_dir = _build_two_kind_emit(tmp_path)
        config = state_changes_config([kind_stream("ghosts", "ghost", [])])
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="records__ghost"):
                open_stream_playback(emit, config, None, discard_notice_sink)

    def test_open_emits_the_pass_notices_to_the_supplied_sink(
        self, tmp_path: "Path"
    ) -> None:
        emit_dir = _enum_where_scenario(tmp_path)
        config = state_changes_config(
            [kind_stream("items", "item", [], where={"status": "archived"})]
        )
        sink = RecordingNoticeSink()
        with open_emit(emit_dir) as emit:
            open_stream_playback(emit, config, None, sink)
        assert len(sink.notices) == 1
        assert sink.notices[0].code == "discriminator-value-unobserved"

    def test_nothing_computes_until_an_iterator_is_pulled(
        self, tmp_path: "Path", monkeypatch: pytest.MonkeyPatch
    ) -> None:
        emit_dir = _build_two_kind_emit(tmp_path)
        config = state_changes_config([kind_stream("alphas", "alpha", ["status"])])
        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, discard_notice_sink)

            calls = {"n": 0}
            original_query = emit.query

            def _tracking_query(sql: str, parameters: tuple[object, ...]) -> object:
                calls["n"] += 1
                return original_query(sql, parameters)

            monkeypatch.setattr(emit, "query", _tracking_query)

            events_iter = head.events(None, None)
            after_construct = calls["n"]
            assert after_construct == 0

            next(events_iter)
            assert calls["n"] > after_construct


class TestTwoHeads:
    def test_independently_pullable(self, tmp_path: "Path") -> None:
        emit_dir = _build_two_kind_emit(tmp_path)
        config = state_changes_config([kind_stream("alphas", "alpha", ["status"])])
        with open_emit(emit_dir) as emit:
            head_a = open_stream_playback(emit, config, None, discard_notice_sink)
            head_b = open_stream_playback(emit, config, None, discard_notice_sink)

            iter_a = head_a.events(None, None)
            iter_b = head_b.events(None, None)

            first_from_a = next(iter_a)
            first_from_b = next(iter_b)
            assert first_from_a == first_from_b

            rest_a = list(iter_a)
            rest_b = list(iter_b)
            assert rest_a == rest_b
