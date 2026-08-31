#!/usr/bin/env python
"""
Demo: seek(T) = 'r' snapshot phase + live tail; folds to the same state as a full play
Sprint: stream-playback
Phase: 2

Builds a minimal one-stream emit (kind 'widget', one tracked property
'status') entirely in a temp directory, with three records:

  - w1: created at t=10 (status='new'), updated at t=25 (status='active')
  - w2: created at t=15 (status='temp'), deleted (deactivated) at t=20
  - w3: created at t=35 (status='fresh') — after the seek position

Opens a StreamPlayback head via `open_stream_playback` and seeks to T=30:

1. Prints the 'r' phase (w1 is live at T=30 with its folded after-image;
   w2 was created and deleted before T=30, so it is absent entirely —
   compaction semantics; w3 has not been created yet).
2. Prints the live tail (w3's 'c' at t=35), whose first event's `seq`
   equals the r-phase's shared `seq` (N) + 1.
3. Folds both `seek(T) + live` and a full `events(None, None)` play as an
   upsert log (insert on c/r, upsert on u, retire on d) and shows the two
   folds agree.

No external dependencies beyond fabulexa_forge itself and duckdb (already a
declared dependency of the package under demo).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Iterable

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import KindStream, StreamConfig
from fabulexa_forge.exporters.streaming.types import StreamEvent
from fabulexa_forge.playback import open_stream_playback
from fabulexa_forge.reader.emit import open_emit

#: records__widget's columns: identity + lifecycle, plus one history-tracked
#: property ('status') — its record-row value is the creation-time value,
#: later changes ride the history table.
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
    """Write run.duckdb + base.json for kind 'widget': w1 (create @10,
    update @25), w2 (create @15, delete @20), w3 (create @35)."""
    record_rows = [
        ("trunk", "w1", 10, True, None, 25, 0, "new"),
        ("trunk", "w2", 15, False, 20, 20, 1, "temp"),
        ("trunk", "w3", 35, True, None, 35, 2, "fresh"),
    ]
    history_rows = [
        ("trunk", "widget", "w1", "status", 10, "new"),
        ("trunk", "widget", "w1", "status", 25, "active"),
        ("trunk", "widget", "w2", "status", 15, "temp"),
        ("trunk", "widget", "w3", "status", 35, "fresh"),
    ]

    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__widget", _RECORD_COLS))
    conn.execute(_ddl("history", _HISTORY_COLS))
    record_placeholders = ", ".join("?" for _ in _RECORD_COLS)
    for row in record_rows:
        conn.execute(
            f'INSERT INTO "records__widget" VALUES ({record_placeholders})', list(row)
        )
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
            _table_spec("records__widget", "records", _RECORD_COLS, 3, "widget"),
            _table_spec("history", "fixed", _HISTORY_COLS, 4, "widget"),
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _discard_notice(_notice: object) -> None:
    """A NoticeSink indifferent to notices — this demo's config declares
    no `where` selection, so nothing is ever sent through it."""


def _fold_upsert_log(events: Iterable[StreamEvent]) -> dict[str, object]:
    """Fold a state-changes stream as an upsert log keyed by record_id:
    insert on 'c'/'r', upsert on 'u', retire (drop) on 'd'."""
    state: dict[str, object] = {}
    for event in events:
        if event.op in ("c", "r", "u"):
            state[event.record_id] = event.after
        elif event.op == "d":
            state.pop(event.record_id, None)
    return state


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp)
        _build_widget_emit(emit_dir)

        config = StreamConfig(
            content="state-changes",
            streams=[KindStream(name="widgets", kind="widget", properties=["status"])],
        )

        with open_emit(emit_dir) as emit:
            head = open_stream_playback(emit, config, None, _discard_notice)
            print("topics:", head.topics())

            seek_at = 30
            seek_events = list(head.seek(seek_at))
            r_phase = [e for e in seek_events if e.op == "r"]
            live_tail = [e for e in seek_events if e.op != "r"]

            print(f"seek({seek_at}) 'r' phase:")
            for e in r_phase:
                print(f"  seq={e.seq} record={e.record_id} after={e.after}")
            print(f"seek({seek_at}) live tail:")
            for e in live_tail:
                print(f"  seq={e.seq} op={e.op} record={e.record_id} after={e.after}")

            seek_and_live_state = _fold_upsert_log(seek_events)
            full_play = list(head.events(None, None))
            full_play_state = _fold_upsert_log(full_play)

        assert [e.record_id for e in r_phase] == ["w1"], (
            "only w1 is live at T=30 (w2 was created and deleted before T;"
            " w3 is created after T)"
        )
        assert (
            r_phase[0].after is not None and r_phase[0].after["status"] == "active"
        ), "w1's r-phase after-image must fold in the t=25 update"
        assert all(e.seq == r_phase[0].seq for e in r_phase), (
            "every 'r' event of the phase shares one seq"
        )
        assert [e.record_id for e in live_tail] == ["w3"], (
            "only w3 arrives via the live tail after T=30"
        )
        assert live_tail[0].seq == r_phase[0].seq + 1, "the live phase begins at N + 1"
        assert "w2" not in seek_and_live_state, (
            "w2's key was retired (created and deleted before T) — compaction"
        )
        assert seek_and_live_state == full_play_state, (
            "seek(T) + live must fold to the same state as a full play"
        )

    print("SUCCESS: seek(T) r-phase + live tail folds to the same state as a full play")
    print(f"  folded state: {seek_and_live_state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
