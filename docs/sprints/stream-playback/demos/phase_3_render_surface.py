#!/usr/bin/env python
"""
Demo: the render surface — resolve_stream_render / StreamRender
Sprint: stream-playback
Phase: 3

Builds a minimal one-kind emit ('widget', two tracked properties: 'status'
and 'priority') with two overlapping state-changes streams declared over the
same flat kind — 'by_status' (properties=['status']) and 'by_priority'
(properties=['priority']). Under table_identity='source_table' both streams'
events carry the same route_table leaf ('widget'), the shared-leaf case fix 1
declares: each stream's messages must embed its own schema, not the
first-declared stream's.

1. Resolves a jsonl render with no anchor (jsonl is the only anchorless
   render) and a debezium render with a resolved anchor, both over the same
   (emit, config) pair.
2. Prints render_bytes / render_key_bytes / timestamp_ms for a 'c' event, a
   'u' event, and a seeked 'r' event (via open_stream_playback's seek).
3. Shows the anchorless jsonl render's timestamp_ms raising ExportError (the
   render-scoped anchor rule) while its render_bytes still resolves.
4. Shows the seek 'r' phase's two covering-stream events — one per stream,
   both route_table='widget' — embedding distinct value schemas keyed by
   their own (topic, leaf) pair (fix 1).

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
from fabulexa_forge.errors import ExportError
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


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp)
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

        with open_emit(emit_dir) as emit:
            render_jsonl = resolve_stream_render(
                emit, config, "jsonl", None, _discard_notice
            )
            render_debezium = resolve_stream_render(
                emit, config, "debezium", anchor, _discard_notice
            )

            head = open_stream_playback(emit, config, anchor, _discard_notice)
            events = list(head.events(None, None))
            c_event = next(e for e in events if e.op == "c" and e.topic == "by_status")
            u_event = next(e for e in events if e.op == "u" and e.topic == "by_status")

            seek_events = list(head.seek(15))
            r_events = [e for e in seek_events if e.op == "r"]

        for label, event in [("c", c_event), ("u", u_event), ("r", r_events[0])]:
            print(f"--- {label} event (topic={event.topic}) ---")
            print("  jsonl render_bytes:    ", render_jsonl.render_bytes(event))
            print("  jsonl render_key_bytes:", render_jsonl.render_key_bytes(event))
            print("  debezium render_bytes: ", render_debezium.render_bytes(event))
            print(
                "  debezium timestamp_ms: ",
                render_debezium.timestamp_ms(event),
            )

        try:
            render_jsonl.timestamp_ms(c_event)
        except ExportError as exc:
            print(f"anchorless jsonl render.timestamp_ms() raised ExportError: {exc}")
        else:
            raise AssertionError(
                "an anchorless render's timestamp_ms must raise ExportError"
            )

        assert len(r_events) == 2, "seek(15) covers w1 once per declared stream"
        by_stream = {e.topic: e for e in r_events}
        assert set(by_stream) == {"by_status", "by_priority"}
        assert all(e.route_table == "widget" for e in r_events), (
            "both streams share one leaf under table_identity='source_table'"
        )
        schema_by_status = render_debezium.value_schema_for(by_stream["by_status"])
        schema_by_priority = render_debezium.value_schema_for(by_stream["by_priority"])
        assert schema_by_status is not None and schema_by_priority is not None
        assert schema_by_status != schema_by_priority, (
            "fix 1: streams sharing one leaf embed distinct per-stream schemas"
        )
        fields_by_status = {f["field"] for f in schema_by_status["fields"][1]["fields"]}
        fields_by_priority = {
            f["field"] for f in schema_by_priority["fields"][1]["fields"]
        }
        assert "status" in fields_by_status and "status" not in fields_by_priority
        assert "priority" in fields_by_priority and "priority" not in fields_by_status

    print("SUCCESS: both formats render a resolved event; overlapping streams")
    print("sharing one leaf embed distinct per-stream Debezium schemas (fix 1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
