"""Tests for jsonl.py: render_jsonl_object, write_jsonl_stream.

Covers format shape, encoder settings, byte-identity, sink routing,
empty-stream handling, and defensive preconditions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

from fabulexa_export.errors import ExportRuntimeError
from fabulexa_export.exporters.streaming.jsonl import (
    render_jsonl_object,
    write_jsonl_stream,
)
from fabulexa_export.exporters.streaming.types import StreamEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    seq: int = 1,
    op: Literal["c", "u", "d"] = "c",
    kind: str = "item",
    record_id: str = "r1",
    presentation_id: str | None = None,
    event_sim_time: int = 1000,
    ts: str | int = "2026-01-01T00:00:00+00:00",
    after: dict[str, object] | None = None,
) -> StreamEvent:
    """Build a StreamEvent for tests.

    Under default routing (no sub-typing), topic == route_table == kind.
    """
    if after is None and op != "d":
        after = {"record_id": record_id, "prop__status": "active"}
    return StreamEvent(
        seq=seq,
        op=op,
        kind=kind,
        record_id=record_id,
        presentation_id=presentation_id,
        event_sim_time=event_sim_time,
        ts=ts,
        after=after,
        topic=kind,
        route_table=kind,
    )


# ---------------------------------------------------------------------------
# render_jsonl_object
# ---------------------------------------------------------------------------


class TestRenderJsonlObject:
    """Tests for render_jsonl_object shape and key ordering."""

    def test_key_order_is_seq_op_ts_kind_key_after(self) -> None:
        """Keys must appear in the exact order: seq, op, ts, kind, key, after."""
        event = _make_event()
        obj = render_jsonl_object(event)
        assert list(obj.keys()) == ["seq", "op", "ts", "kind", "key", "after"]

    def test_key_is_record_id_dict(self) -> None:
        """key is always {"record_id": ...}, even when presentation_id is set."""
        event = _make_event(record_id="r42", presentation_id="pid-99")
        obj = render_jsonl_object(event)
        assert obj["key"] == {"record_id": "r42"}

    def test_after_is_row_map_on_create(self) -> None:
        """after carries the full row map on a 'c' event."""
        after = {"record_id": "r1", "prop__name": "Alice"}
        event = _make_event(op="c", after=after)
        obj = render_jsonl_object(event)
        assert obj["after"] == after

    def test_after_is_row_map_on_update(self) -> None:
        """after carries the full row map on a 'u' event."""
        after = {"record_id": "r1", "prop__name": "Bob"}
        event = _make_event(op="u", after=after)
        obj = render_jsonl_object(event)
        assert obj["after"] == after

    def test_after_is_none_on_delete(self) -> None:
        """after is None on a 'd' event."""
        event = _make_event(op="d", after=None)
        obj = render_jsonl_object(event)
        assert obj["after"] is None

    def test_seq_value(self) -> None:
        """seq in the rendered object matches the event seq."""
        event = _make_event(seq=7)
        obj = render_jsonl_object(event)
        assert obj["seq"] == 7

    def test_ts_value_string(self) -> None:
        """ts is passed through as-is when it is a string."""
        event = _make_event(ts="2026-06-21T12:00:00+02:00")
        obj = render_jsonl_object(event)
        assert obj["ts"] == "2026-06-21T12:00:00+02:00"

    def test_ts_value_int(self) -> None:
        """ts is passed through as-is when it is a raw int."""
        event = _make_event(ts=86_400_000_000_000)
        obj = render_jsonl_object(event)
        assert obj["ts"] == 86_400_000_000_000


# ---------------------------------------------------------------------------
# write_jsonl_stream — stdout sink
# ---------------------------------------------------------------------------


class TestWriteJsonlStreamStdout:
    """Tests for write_jsonl_stream with sink='stdout'."""

    def test_stdout_writes_one_json_per_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Each event produces exactly one JSON object on one line."""
        events = [_make_event(seq=1), _make_event(seq=2, record_id="r2")]
        write_jsonl_stream(events, "stdout", None)
        captured = capsys.readouterr()
        lines = captured.out.splitlines()
        assert len(lines) == 2
        for line in lines:
            json.loads(line)  # must be valid JSON

    def test_stdout_compact_separators(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No inter-token whitespace — separators are (',', ':')."""
        event = _make_event()
        write_jsonl_stream([event], "stdout", None)
        captured = capsys.readouterr()
        line = captured.out.rstrip("\n")
        assert ": " not in line
        assert ", " not in line

    def test_stdout_ensure_ascii_false(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Non-ASCII characters pass through as UTF-8, not \\uXXXX escapes."""
        after = {"record_id": "r1", "prop__name": "Ångström"}
        event = _make_event(after=after)
        write_jsonl_stream([event], "stdout", None)
        captured = capsys.readouterr()
        assert "Ångström" in captured.out
        assert "\\u" not in captured.out

    def test_stdout_sort_keys_false_construction_order_preserved(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Key construction order is preserved (sort_keys=False)."""
        event = _make_event()
        write_jsonl_stream([event], "stdout", None)
        captured = capsys.readouterr()
        line = captured.out.rstrip("\n")
        obj = json.loads(line)
        keys = list(obj.keys())
        assert keys == ["seq", "op", "ts", "kind", "key", "after"]

    def test_stdout_exactly_one_trailing_newline_per_record(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Each record ends with exactly one '\\n'."""
        events = [_make_event(seq=1), _make_event(seq=2, record_id="r2")]
        write_jsonl_stream(events, "stdout", None)
        captured = capsys.readouterr()
        # Two records → two newlines; no BOM; ends with exactly one newline
        assert captured.out.count("\n") == 2
        assert captured.out.endswith("\n")
        assert not captured.out.startswith("﻿")

    def test_stdout_no_bom(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No BOM at the start of output."""
        event = _make_event()
        write_jsonl_stream([event], "stdout", None)
        captured = capsys.readouterr()
        assert not captured.out.startswith("﻿")

    def test_stdout_kinds_interleaved_in_seq_order(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Events from different kinds interleave by seq (arrival) order."""
        events = [
            _make_event(seq=1, kind="alpha", record_id="a1"),
            _make_event(seq=2, kind="beta", record_id="b1"),
            _make_event(seq=3, kind="alpha", record_id="a2"),
        ]
        write_jsonl_stream(events, "stdout", None)
        captured = capsys.readouterr()
        lines = [ln for ln in captured.out.splitlines() if ln]
        kinds = [json.loads(ln)["kind"] for ln in lines]
        assert kinds == ["alpha", "beta", "alpha"]

    def test_byte_identity(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Writing the same event sequence twice yields byte-identical output."""
        events = [
            _make_event(seq=1, kind="alpha", record_id="a1"),
            _make_event(seq=2, kind="beta", record_id="b1"),
        ]
        write_jsonl_stream(events, "stdout", None)
        first = capsys.readouterr().out

        write_jsonl_stream(events, "stdout", None)
        second = capsys.readouterr().out

        assert first == second

    def test_empty_stream_writes_nothing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A fully empty event list writes no bytes to stdout."""
        outcome = write_jsonl_stream([], "stdout", None)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert outcome.total_events == 0

    def test_outcome_counts_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        """StreamOutcome has correct total_events and events_per_topic."""
        events = [
            _make_event(seq=1, kind="alpha"),
            _make_event(seq=2, kind="alpha", record_id="r2"),
            _make_event(seq=3, kind="beta"),
        ]
        outcome = write_jsonl_stream(events, "stdout", None)
        capsys.readouterr()
        assert outcome.total_events == 3
        assert outcome.events_per_topic == {"alpha": 2, "beta": 1}


# ---------------------------------------------------------------------------
# write_jsonl_stream — file sink
# ---------------------------------------------------------------------------


class TestWriteJsonlStreamFile:
    """Tests for write_jsonl_stream with sink='file'."""

    def test_file_one_jsonl_per_kind(self, tmp_path: Path) -> None:
        """One <kind>.jsonl file per kind under `out`."""
        events = [
            _make_event(seq=1, kind="alpha"),
            _make_event(seq=2, kind="beta"),
        ]
        write_jsonl_stream(events, "file", tmp_path)
        assert (tmp_path / "alpha.jsonl").exists()
        assert (tmp_path / "beta.jsonl").exists()

    def test_file_each_in_seq_order(self, tmp_path: Path) -> None:
        """Events in each per-kind file appear in seq order."""
        events = [
            _make_event(seq=1, kind="alpha", record_id="a1"),
            _make_event(seq=2, kind="beta", record_id="b1"),
            _make_event(seq=3, kind="alpha", record_id="a2"),
        ]
        write_jsonl_stream(events, "file", tmp_path)
        alpha_lines = (
            (tmp_path / "alpha.jsonl").read_text(encoding="utf-8").splitlines()
        )
        objs = [json.loads(ln) for ln in alpha_lines if ln]
        seqs = [o["seq"] for o in objs]
        assert seqs == [1, 3]
        assert objs[0]["key"]["record_id"] == "a1"
        assert objs[1]["key"]["record_id"] == "a2"

    def test_file_outcome_counts(self, tmp_path: Path) -> None:
        """StreamOutcome counts are correct for the file sink."""
        events = [
            _make_event(seq=1, kind="alpha"),
            _make_event(seq=2, kind="alpha", record_id="r2"),
        ]
        outcome = write_jsonl_stream(events, "file", tmp_path)
        assert outcome.total_events == 2
        assert outcome.events_per_topic == {"alpha": 2}


# ---------------------------------------------------------------------------
# Empty-stream / zero-event-kind (write_jsonl_stream level)
# ---------------------------------------------------------------------------


class TestEmptyStream:
    """Empty-stream handling at the write_jsonl_stream level."""

    def test_file_empty_event_list_no_files_written(self, tmp_path: Path) -> None:
        """An empty event list writes no files (no selected-kinds info here)."""
        outcome = write_jsonl_stream([], "file", tmp_path)
        assert list(tmp_path.iterdir()) == []
        assert outcome.total_events == 0
        assert outcome.events_per_topic == {}


# ---------------------------------------------------------------------------
# Precondition / defensive errors
# ---------------------------------------------------------------------------


class TestPreconditions:
    """Defensive ExportRuntimeError on sink/out mismatches."""

    def test_file_sink_with_out_none_raises(self) -> None:
        """sink='file' with out=None raises ExportRuntimeError."""
        with pytest.raises(ExportRuntimeError):
            write_jsonl_stream([], "file", None)

    def test_stdout_sink_with_out_set_raises(self, tmp_path: Path) -> None:
        """sink='stdout' with a non-None out raises ExportRuntimeError."""
        with pytest.raises(ExportRuntimeError):
            write_jsonl_stream([], "stdout", tmp_path)


# ---------------------------------------------------------------------------
# paced=True — stdout
# ---------------------------------------------------------------------------


class TestWriteJsonlStreamStdoutPaced:
    """Tests for write_jsonl_stream with paced=True and sink='stdout'."""

    def test_paced_stdout_byte_identical_to_unpaced(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """paced=True stdout output is byte-identical to paced=False."""
        events = [
            _make_event(seq=1, kind="alpha", record_id="a1"),
            _make_event(seq=2, kind="beta", record_id="b1"),
        ]
        write_jsonl_stream(events, "stdout", None, paced=False)
        unpaced = capsys.readouterr().out

        write_jsonl_stream(events, "stdout", None, paced=True)
        paced = capsys.readouterr().out

        assert paced == unpaced

    def test_paced_empty_stream_writes_nothing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """paced=True with empty stream writes nothing and succeeds."""
        outcome = write_jsonl_stream([], "stdout", None, paced=True)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert outcome.total_events == 0

    def test_paced_outcome_counts_independent(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """StreamOutcome counts are identical between paced=True and paced=False."""
        events = [
            _make_event(seq=1, kind="alpha"),
            _make_event(seq=2, kind="alpha", record_id="r2"),
            _make_event(seq=3, kind="beta"),
        ]
        outcome_unpaced = write_jsonl_stream(events, "stdout", None, paced=False)
        capsys.readouterr()
        outcome_paced = write_jsonl_stream(events, "stdout", None, paced=True)
        capsys.readouterr()

        assert outcome_paced.total_events == outcome_unpaced.total_events
        assert outcome_paced.events_per_topic == outcome_unpaced.events_per_topic


# ---------------------------------------------------------------------------
# paced=True — file sink
# ---------------------------------------------------------------------------


class TestWriteJsonlStreamFilePaced:
    """Tests for write_jsonl_stream with paced=True and sink='file'."""

    def test_paced_file_byte_identical_per_topic(self, tmp_path: Path) -> None:
        """paced=True per-topic file content is byte-identical to paced=False."""
        events = [
            _make_event(seq=1, kind="alpha", record_id="a1"),
            _make_event(seq=2, kind="beta", record_id="b1"),
            _make_event(seq=3, kind="alpha", record_id="a2"),
        ]
        out_unpaced = tmp_path / "unpaced"
        out_paced = tmp_path / "paced"
        out_unpaced.mkdir()
        out_paced.mkdir()

        write_jsonl_stream(events, "file", out_unpaced, paced=False)
        write_jsonl_stream(events, "file", out_paced, paced=True)

        for topic in ("alpha", "beta"):
            unpaced_content = (out_unpaced / f"{topic}.jsonl").read_text(
                encoding="utf-8"
            )
            paced_content = (out_paced / f"{topic}.jsonl").read_text(encoding="utf-8")
            assert paced_content == unpaced_content, f"mismatch on topic={topic}"

    def test_paced_multi_topic_correct_lines_and_counts(self, tmp_path: Path) -> None:
        """paced=True file run writes correct per-topic lines and counts."""
        events = [
            _make_event(seq=1, kind="alpha", record_id="a1"),
            _make_event(seq=2, kind="beta", record_id="b1"),
            _make_event(seq=3, kind="alpha", record_id="a2"),
        ]
        outcome = write_jsonl_stream(events, "file", tmp_path, paced=True)

        alpha_lines = (
            (tmp_path / "alpha.jsonl").read_text(encoding="utf-8").splitlines()
        )
        beta_lines = (tmp_path / "beta.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(alpha_lines) == 2
        assert len(beta_lines) == 1
        assert outcome.events_per_topic == {"alpha": 2, "beta": 1}
        assert outcome.total_events == 3

    def test_paced_zero_event_topic_opens_no_handle(self, tmp_path: Path) -> None:
        """paced=True: a topic in topic_set with zero events produces no file at writer level."""
        events = [_make_event(seq=1, kind="alpha", record_id="a1")]
        outcome = write_jsonl_stream(
            events, "file", tmp_path, topic_set=("alpha", "beta"), paced=True
        )
        assert (tmp_path / "alpha.jsonl").exists()
        assert not (tmp_path / "beta.jsonl").exists()
        assert outcome.events_per_topic["beta"] == 0

    def test_paced_outcome_counts_independent(self, tmp_path: Path) -> None:
        """StreamOutcome counts are identical between paced=True and paced=False."""
        events = [
            _make_event(seq=1, kind="alpha"),
            _make_event(seq=2, kind="beta", record_id="r2"),
        ]
        out_unpaced = tmp_path / "unpaced"
        out_paced = tmp_path / "paced"
        out_unpaced.mkdir()
        out_paced.mkdir()

        outcome_unpaced = write_jsonl_stream(events, "file", out_unpaced, paced=False)
        outcome_paced = write_jsonl_stream(events, "file", out_paced, paced=True)

        assert outcome_paced.total_events == outcome_unpaced.total_events
        assert outcome_paced.events_per_topic == outcome_unpaced.events_per_topic
