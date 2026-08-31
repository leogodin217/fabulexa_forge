#!/usr/bin/env python
"""
Demo: Bounded resolved iteration matches the whole-tape stream, seq offset holds
Sprint: stream-playback
Phase: 1

Builds a minimal two-stream emit (two kind-shaped streams, four creates spread
across sim_time) entirely in a temp directory, then shows:

1. `resolve_streams` (the promoted eager pass) + `iter_resolved_stream_events`
   called with `(None, None)` reproduces `iter_stream_events`'s whole-tape
   output event-for-event, `seq` included.
2. A bounded ask `(T1, T2)` selects only the in-window events, each
   byte-identical to its whole-tape self, and the first selected event's
   `seq` equals `1 + N` where `N` is the count of in-scope events strictly
   before `T1`.

No external dependencies beyond fabulexa_forge itself and duckdb (already a
declared dependency of the package under demo).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import KindStream, StreamConfig
from fabulexa_forge.exporters.streaming.engine import (
    iter_resolved_stream_events,
    iter_stream_events,
    resolve_streams,
)
from fabulexa_forge.reader.emit import open_emit

#: The record columns both demo kinds share: identity surfaces, lifecycle
#: bookkeeping, and one constant property — no tracked property, so every
#: record's only event is its create (keeps the demo's event set to exactly
#: the four creates the walkthrough narrates).
_RECORD_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__label",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

#: The `history` fixed table every records-category kind requires present
#: (C1-C5 conformance) — zero rows here, since no property is tracked.
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


def _build_two_kind_emit(emit_dir: Path) -> None:
    """Write run.duckdb + base.json: kinds 'alpha' (creates at 10, 40) and
    'beta' (creates at 20, 30) — canonical order interleaves the two kinds
    by sim_time."""
    alpha_rows = [
        ("trunk", "a1", 10, True, None, 10, 0, "x"),
        ("trunk", "a2", 40, True, None, 40, 1, "x"),
    ]
    beta_rows = [
        ("trunk", "b1", 20, True, None, 20, 0, "y"),
        ("trunk", "b2", 30, True, None, 30, 1, "y"),
    ]

    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__alpha", _RECORD_COLS))
    conn.execute(_ddl("records__beta", _RECORD_COLS))
    conn.execute(_ddl("history", _HISTORY_COLS))
    placeholders = ", ".join("?" for _ in _RECORD_COLS)
    for row in alpha_rows:
        conn.execute(f'INSERT INTO "records__alpha" VALUES ({placeholders})', list(row))
    for row in beta_rows:
        conn.execute(f'INSERT INTO "records__beta" VALUES ({placeholders})', list(row))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        "tables": [
            _table_spec("records__alpha", "records", _RECORD_COLS, 2, "alpha"),
            _table_spec("records__beta", "records", _RECORD_COLS, 2, "beta"),
            _table_spec("history", "fixed", _HISTORY_COLS, 0, "alpha"),
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _discard_notice(_notice: object) -> None:
    """A NoticeSink indifferent to notices — this demo's config declares
    no `where` selection, so nothing is ever sent through it."""


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp)
        _build_two_kind_emit(emit_dir)

        config = StreamConfig(
            content="state-changes",
            streams=[
                KindStream(name="alpha", kind="alpha", properties=[]),
                KindStream(name="beta", kind="beta", properties=[]),
            ],
        )

        with open_emit(emit_dir) as emit:
            whole_tape = list(
                iter_stream_events(emit, config, None, notice_sink=_discard_notice)
            )
            resolution = resolve_streams(emit, config, _discard_notice)
            resolved_whole = list(
                iter_resolved_stream_events(emit, config, None, resolution, None, None)
            )
            bounded = list(
                iter_resolved_stream_events(emit, config, None, resolution, 15, 35)
            )

        assert len(whole_tape) == 4, f"expected 4 events, got {len(whole_tape)}"
        assert [e.event_sim_time for e in whole_tape] == [10, 20, 30, 40]
        assert [e.seq for e in whole_tape] == [1, 2, 3, 4]

        assert resolved_whole == whole_tape, (
            "iter_resolved_stream_events(..., None, None) must be "
            "byte-identical to iter_stream_events"
        )

        by_seq = {e.seq: e for e in whole_tape}
        assert bounded == [by_seq[2], by_seq[3]], (
            "the bounded window (15, 35) must select exactly the whole-tape "
            "events at seq 2 and 3, byte-identical"
        )
        assert bounded[0].seq == 2, (
            "first bounded event's seq must be 1 + N, N = count of in-scope "
            "events strictly before T1=15 (here N=1, the a1 create at t=10)"
        )

    print("SUCCESS: bounded resolved iteration matches whole-tape output;")
    print(f"  whole tape: {len(whole_tape)} events, seq {[e.seq for e in whole_tape]}")
    print(f"  bounded(15, 35): seq {[e.seq for e in bounded]} (offset = 1 + N)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
