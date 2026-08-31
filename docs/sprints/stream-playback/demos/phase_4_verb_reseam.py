#!/usr/bin/env python
"""
Demo: the verb re-seam — stream_export over head + render
Sprint: stream-playback
Phase: 4

Builds a minimal one-kind emit ('widget', two tracked properties: 'status'
and 'priority') with two overlapping state-changes streams declared over the
same flat kind — 'by_status' (properties=['status']) and 'by_priority'
(properties=['priority']). Under table_identity='source_table' (the default)
both streams' events carry the same route_table leaf ('widget') — the
shared-leaf case fix 1 declares: each stream's messages must embed its own
schema, not the first-declared stream's.

1. Runs `stream_export` (the re-seamed verb) twice — fmt='jsonl' and
   fmt='debezium' — both with sink='file', over the same fixture emit.
2. Independently composes the same answer by hand: `open_stream_playback` +
   `resolve_stream_render`, `head.events(None, None)`, and the driver's own
   `write_line_stream` — the exact primitives `stream_export` composes
   internally — and writes to a second output directory.
3. Shows the verb's per-topic files are byte-identical to the hand-composed
   files, for both formats.
4. Shows fix 1: 'by_status' and 'by_priority' share one route_table leaf
   ('widget') yet their rendered Debezium messages embed distinct value
   schemas (each carrying only its own declared property field).

No external dependencies beyond fabulexa_forge itself and duckdb (already a
declared dependency of the package under demo).
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import (
    DebeziumConfig,
    DebeziumSourceIdentity,
    KindStream,
    StreamConfig,
)
from fabulexa_forge.exporters.streaming import stream_export
from fabulexa_forge.exporters.streaming.driver import write_line_stream
from fabulexa_forge.exporters.streaming.engine import build_topic_set
from fabulexa_forge.playback import open_stream_playback, resolve_stream_render
from fabulexa_forge.reader.emit import open_emit

_RECORD_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__priority",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
]

_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    """Return a CREATE TABLE statement for one table's column list."""
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _table_spec(
    name: str, category: str, cols: list[dict[str, object]], rows: int, kind: str
) -> dict[str, object]:
    """One sidecar table entry."""
    return {
        "name": name,
        "category": category,
        "columns": cols,
        "rows": rows,
        "record_kind": kind,
    }


def _build_widget_emit(emit_dir: Path) -> None:
    """Write run.duckdb + base.json for kind 'widget': w1 created @10
    (status='new', priority='low'), status updated @20 (status='active')."""
    record_rows = [("trunk", "w1", 10, True, None, 20, 0, "active", "low")]
    history_rows = [
        ("trunk", "widget", "w1", "status", 10, "new"),
        ("trunk", "widget", "w1", "priority", 10, "low"),
        ("trunk", "widget", "w1", "status", 20, "active"),
    ]

    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__widget", _RECORD_COLS))
    conn.execute(_ddl("history", _HISTORY_COLS))
    record_placeholders = ", ".join("?" for _ in _RECORD_COLS)
    for record_row in record_rows:
        conn.execute(
            f'INSERT INTO "records__widget" VALUES ({record_placeholders})',
            list(record_row),
        )
    for history_row in history_rows:
        conn.execute(
            'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(history_row)
        )
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
            _table_spec("records__widget", "records", _RECORD_COLS, 1, "widget"),
            _table_spec("history", "fixed", _HISTORY_COLS, 3, "widget"),
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _discard_notice(_notice: object) -> None:
    """A NoticeSink indifferent to notices — this demo declares no `where`
    selection, so nothing is ever sent through it."""


def _compare_topic_files(
    verb_dir: Path, hand_dir: Path, topic_set: tuple[str, ...]
) -> None:
    """Assert every topic's file is byte-identical between the two directories."""
    for topic in topic_set:
        verb_bytes = (verb_dir / f"{topic}.jsonl").read_bytes()
        hand_bytes = (hand_dir / f"{topic}.jsonl").read_bytes()
        assert verb_bytes == hand_bytes, (
            f"topic {topic!r}: stream_export's output diverged from the "
            "hand-composed head+render output"
        )
        print(f"  {topic}.jsonl: byte-identical ({len(verb_bytes)} bytes)")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        emit_dir = root / "emit"
        emit_dir.mkdir()
        _build_widget_emit(emit_dir)

        config = StreamConfig(
            content="state-changes",
            streams=[
                KindStream(name="by_status", kind="widget", properties=["status"]),
                KindStream(name="by_priority", kind="widget", properties=["priority"]),
            ],
            debezium=DebeziumConfig(
                source=DebeziumSourceIdentity(
                    connector="postgres",
                    name="demo",
                    db="demo_db",
                    schema="public",
                    version="2.0",
                )
            ),
        )
        anchor = EffectiveAnchor(
            start_instant=datetime(2024, 1, 1, tzinfo=timezone.utc),
            timezone=ZoneInfo("UTC"),
        )
        topic_set = build_topic_set(config)

        for fmt in ("jsonl", "debezium"):
            print(f"=== fmt={fmt} ===")
            verb_dir = root / f"verb_{fmt}"
            hand_dir = root / f"hand_{fmt}"
            verb_dir.mkdir()
            hand_dir.mkdir()

            with open_emit(emit_dir) as emit:
                outcome = stream_export(
                    emit,
                    config,
                    fmt,
                    "file",
                    verb_dir,
                    anchor,
                    _discard_notice,
                )
            print(f"  stream_export outcome: {outcome.events_per_topic}")

            # Independently compose the same answer by hand: the exact
            # primitives stream_export composes internally.
            with open_emit(emit_dir) as emit:
                head = open_stream_playback(emit, config, anchor, _discard_notice)
                render = resolve_stream_render(
                    emit, config, fmt, anchor, _discard_notice
                )
                events = head.events(None, None)
                write_line_stream(
                    events,
                    render.render_bytes,
                    "file",
                    hand_dir,
                    topic_set=topic_set,
                    paced=False,
                )

            _compare_topic_files(verb_dir, hand_dir, topic_set)

        # Fix 1: overlapping streams sharing one route_table leaf embed
        # distinct per-stream Debezium value schemas.
        debezium_dir = root / "verb_debezium"
        status_line = (debezium_dir / "by_status.jsonl").read_text().splitlines()[0]
        priority_line = (debezium_dir / "by_priority.jsonl").read_text().splitlines()[0]
        status_msg = json.loads(status_line)
        priority_msg = json.loads(priority_line)
        schema_status = status_msg["schema"]
        schema_priority = priority_msg["schema"]
        assert schema_status != schema_priority, (
            "fix 1: streams sharing one leaf must embed distinct per-stream schemas"
        )
        fields_status = {f["field"] for f in schema_status["fields"][1]["fields"]}
        fields_priority = {f["field"] for f in schema_priority["fields"][1]["fields"]}
        assert "status" in fields_status and "status" not in fields_priority
        assert "priority" in fields_priority and "priority" not in fields_status
        print("=== fix 1 ===")
        print(f"  by_status schema fields:   {sorted(fields_status)}")
        print(f"  by_priority schema fields: {sorted(fields_priority)}")

    print("SUCCESS: stream_export's per-topic output is byte-identical to the")
    print("hand-composed head+render output for both formats; overlapping")
    print("streams sharing one leaf embed distinct per-stream schemas (fix 1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
