#!/usr/bin/env python
"""
Demo: Notice-sink threading — required sink on the stream entry points
Sprint: streaming-authoring-parity
Phase: 2

Builds a minimal emit (one `widget` kind, two records: one created-then-
updated, one created-then-deactivated), then exercises all three
notice-sink-carrying entry points this phase adds the parameter to:

  1. `iter_stream_events` drained with a recording sink — zero notices today
     (the channel lands ahead of the feature that populates it), events
     unchanged from the pre-phase shape.
  2. `stream_export` run end to end to the file sink with
     `render_notice_stderr` (the CLI's own sink) — the written JSONL matches
     the drained events exactly.
  3. `seed_mixer_run` seeded with a discarding sink — the per-topic buffers
     partition the same event set.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

# The vendored fixture-sidecar authority lives under tests/_support — reused
# here (as pytest itself does) rather than hand-rolling a base.json, so the
# demo's scratch emit is built through the one sidecar-conformant authority
# every fixture in this repo goes through. The recording/discarding notice
# sinks are the same migration sinks every test call site threads.
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import duckdb  # noqa: E402
from _support.notices import RecordingNoticeSink, discard_notice_sink  # noqa: E402
from _support.sidecar_builder import identity_column  # noqa: E402
from _support.sidecar_builder import write_emit as _write_sidecar  # noqa: E402

from fabulexa_forge.anchor import resolve_effective_anchor  # noqa: E402
from fabulexa_forge.config.models import KindStream, StreamConfig  # noqa: E402
from fabulexa_forge.exporters.notices import render_notice_stderr  # noqa: E402
from fabulexa_forge.exporters.streaming import (  # noqa: E402
    Transport,
    iter_stream_events,
    seed_mixer_run,
    stream_export,
)
from fabulexa_forge.exporters.streaming.types import StreamEvent  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402

_MS = 1_000_000  # one sim-time "tick", in nanoseconds

_WIDGET_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _build_demo_emit(tmp_path: Path) -> Path:
    """Write the demo's scratch emit: one `widget` kind, two records.

    `w1` is created then updated (a 'u' history change to `prop__status`).
    `w2` is created then deactivated (a 'd' tombstone).

    Args:
        tmp_path: Directory to write the emit artifacts into.

    Returns:
        tmp_path (the emit directory).
    """
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    columns_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _WIDGET_COLUMNS)
    conn.execute(f'CREATE TABLE "records__widget" ({columns_ddl})')
    conn.executemany(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        [
            ("trunk", "w1", 0, True, None, 5 * _MS, 0, "pending"),
            ("trunk", "w2", 0, False, 10 * _MS, 10 * _MS, 1, "pending"),
        ],
    )
    history_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _HISTORY_COLUMNS)
    conn.execute(f'CREATE TABLE "history" ({history_ddl})')
    conn.executemany(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        [
            ("trunk", "widget", "w1", "status", 0, "pending"),
            ("trunk", "widget", "w1", "status", 5 * _MS, "active"),
            ("trunk", "widget", "w2", "status", 0, "pending"),
        ],
    )
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            {
                "name": "records__widget",
                "category": "records",
                "columns": _WIDGET_COLUMNS,
                "rows": 2,
                "record_kind": "widget",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 3,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100 * _MS}],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
        },
    )
    return tmp_path


def _event_ops(events: list[StreamEvent]) -> list[tuple[str, str]]:
    """The (record_id, op) pairs of a drained event list, in seq order."""
    return [(e.record_id, e.op) for e in events]


def _read_jsonl_ops(path: Path) -> list[tuple[str, str]]:
    """The (record_id, op) pairs written to one streamed JSONL file, in order."""
    pairs: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        obj: dict[str, Any] = json.loads(line)
        record_id = next(iter(obj["key"].values()))
        pairs.append((record_id, obj["op"]))
    return pairs


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = _build_demo_emit(Path(tmp))

        config = StreamConfig(
            content="state-changes",
            streams=[KindStream(name="widgets", kind="widget", properties=["status"])],
        )

        with open_emit(emit_dir) as emit:
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)

            # 1. iter_stream_events with a recording sink.
            recording_sink = RecordingNoticeSink()
            events = list(iter_stream_events(emit, config, anchor, recording_sink))
            print(f"iter_stream_events: {len(events)} events, ops={_event_ops(events)}")
            print(f"  notices recorded: {recording_sink.notices!r}")
            assert recording_sink.notices == []
            assert _event_ops(events) == [
                ("w1", "c"),
                ("w2", "c"),
                ("w1", "u"),
                ("w2", "d"),
            ]

            # 2. stream_export end to end with the CLI's render_notice_stderr sink.
            out_dir = Path(tmp) / "out"
            out_dir.mkdir()
            outcome = stream_export(
                emit,
                config,
                "jsonl",
                "file",
                out_dir,
                anchor,
                render_notice_stderr,
            )
            print(f"stream_export: events_per_topic={outcome.events_per_topic!r}")
            written_ops = _read_jsonl_ops(out_dir / "widgets.jsonl")
            assert written_ops == _event_ops(events)
            print(f"  written JSONL ops match drained events: {written_ops}")

            # 3. seed_mixer_run with a discarding sink.
            transport = Transport(playing=False, speed=1.0)
            buffers, control, frontier = seed_mixer_run(
                emit, config, anchor, emit.sidecar, transport, discard_notice_sink
            )
            playing = control.transport.playing
            print(f"seed_mixer_run: topics={list(buffers)}, playing={playing}")
            assert frontier.frontier_sim_time is None
            assert list(buffers["widgets"]) == events
            print(f"  buffer['widgets'] holds all {len(buffers['widgets'])} events")

    print(
        "SUCCESS: iter_stream_events / stream_export / seed_mixer_run all take"
        " a required notice_sink; no streaming notice is emitted yet"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
