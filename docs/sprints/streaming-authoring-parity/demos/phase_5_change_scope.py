#!/usr/bin/env python
"""
Demo: Change scope — only/ignore + init trailing comment
Sprint: streaming-authoring-parity
Phase: 5

Builds a `widget` emit — two tracked properties (`status`, `priority`) and one
constant property (`label`) — and streams it three ways:

  1. No `only` / `ignore`: the audited default fires a `u` at every tracked
     change point (byte-identical to the shipped full-property-set
     invocation).
  2. `only: [priority]`: `status`'s changes fire no `u` (its as-of value
     still rides every surviving after-image); `priority`'s changes fire
     `u`; a coinciding change fires exactly one `u`.
  3. `ignore: [status, priority]`: every tracked property excluded — a
     lifecycle-only feed, `c`/`d` events only.

Then generates a candidate `init --mode streaming` config and shows its
trailing comment naming the never-proposed authoring fields (`rename` /
`kind_label` / `kind_labels` / `where` / `only` / `ignore` / membership
`sub_types`).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

# The vendored fixture-sidecar authority lives under tests/_support — reused
# here (as pytest itself does) rather than hand-rolling a base.json.
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import duckdb  # noqa: E402
from _support.notices import discard_notice_sink  # noqa: E402
from _support.sidecar_builder import identity_column  # noqa: E402
from _support.sidecar_builder import write_emit as _write_sidecar  # noqa: E402

from fabulexa_forge.config.models import KindStream, StreamConfig  # noqa: E402
from fabulexa_forge.exporters.streaming.engine import iter_stream_events  # noqa: E402
from fabulexa_forge.exporters.streaming.init import (  # noqa: E402
    generate_stream_init_config,
)
from fabulexa_forge.reader.emit import open_emit  # noqa: E402

_RECORD_COLS: list[dict[str, object]] = [
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
    {
        "name": "prop__priority",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    {
        "name": "prop__label",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
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

# w1: created t=0. status changes alone at t=100; priority changes alone at
# t=200; both coincide at t=300.
_RECORD_ROWS: list[tuple[Any, ...]] = [
    ("trunk", "w1", 0, True, None, 0, 0, "queued", "low", "widget"),
]

_HISTORY_ROWS: list[tuple[Any, ...]] = [
    ("trunk", "widget", "w1", "status", 100, "active"),
    ("trunk", "widget", "w1", "priority", 200, "medium"),
    ("trunk", "widget", "w1", "status", 300, "done"),
    ("trunk", "widget", "w1", "priority", 300, "high"),
]


def _build_demo_emit(tmp_path: Path) -> Path:
    """Write the demo's scratch emit: one `widget` kind, two tracked properties."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _RECORD_COLS)
    conn.execute(f'CREATE TABLE "records__widget" ({ddl})')
    placeholders = ", ".join("?" for _ in _RECORD_COLS)
    conn.executemany(
        f'INSERT INTO "records__widget" VALUES ({placeholders})', _RECORD_ROWS
    )
    hist_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _HISTORY_COLS)
    conn.execute(f'CREATE TABLE "history" ({hist_ddl})')
    conn.executemany('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', _HISTORY_ROWS)
    conn.close()

    _write_sidecar(
        tmp_path,
        tables=[
            {
                "name": "records__widget",
                "category": "records",
                "record_kind": "widget",
                "columns": _RECORD_COLS,
                "rows": len(_RECORD_ROWS),
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLS,
                "rows": len(_HISTORY_ROWS),
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    )
    return tmp_path


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = _build_demo_emit(Path(tmp))

        # 1. No only / ignore -- the audited default fires u at every tracked
        # change point (byte-identical to the shipped full-property-set
        # invocation).
        default_stream = KindStream(
            name="widgets_default", kind="widget", properties=["status", "priority"]
        )
        default_config = StreamConfig(content="state-changes", streams=[default_stream])
        with open_emit(emit_dir) as emit:
            default_events = list(
                iter_stream_events(emit, default_config, None, discard_notice_sink)
            )
        print("widgets_default (no only/ignore):")
        for event in default_events:
            print(f"  seq={event.seq} op={event.op} t={event.event_sim_time}")
        assert [e.op for e in default_events] == ["c", "u", "u", "u"]
        assert [e.event_sim_time for e in default_events] == [0, 100, 200, 300]
        print("  u fires at every tracked change point: t=100, t=200, t=300\n")

        # 2. only: [priority] -- status's t=100 change fires no u (its as-of
        # value still rides the after-image); priority's t=200 change fires
        # u; the t=300 coincidence fires exactly one u.
        only_stream = KindStream(
            name="widgets_priority_only",
            kind="widget",
            properties=["status", "priority"],
            only=["priority"],
        )
        only_config = StreamConfig(content="state-changes", streams=[only_stream])
        with open_emit(emit_dir) as emit:
            only_events = list(
                iter_stream_events(emit, only_config, None, discard_notice_sink)
            )
        print("widgets_priority_only (only: [priority]):")
        for event in only_events:
            print(f"  seq={event.seq} op={event.op} t={event.event_sim_time}", end=" ")
            print(f"after={event.after}")
        assert [e.op for e in only_events] == ["c", "u", "u"]
        assert [e.event_sim_time for e in only_events] == [0, 200, 300]
        u_at_200 = only_events[1]
        assert u_at_200.after is not None
        assert u_at_200.after["status"] == "active"  # rides the t=100 as-of value
        assert u_at_200.after["priority"] == "medium"
        print(
            "  t=100 status-only change fires no u; status still rides t=200's"
            " after-image as 'active'\n"
        )

        # 3. ignore: [status, priority] -- every tracked property excluded:
        # a lifecycle-only feed, c/d events only.
        ignore_stream = KindStream(
            name="widgets_lifecycle_only",
            kind="widget",
            properties=["status", "priority", "label"],
            ignore=["status", "priority"],
        )
        ignore_config = StreamConfig(content="state-changes", streams=[ignore_stream])
        with open_emit(emit_dir) as emit:
            ignore_events = list(
                iter_stream_events(emit, ignore_config, None, discard_notice_sink)
            )
        print("widgets_lifecycle_only (ignore: [status, priority]):")
        for event in ignore_events:
            print(f"  seq={event.seq} op={event.op} t={event.event_sim_time}")
        assert [e.op for e in ignore_events] == ["c"]
        print("  every tracked property ignored: lifecycle-only feed, no u\n")

        # 4. init's trailing comment names the never-proposed authoring fields.
        with open_emit(emit_dir) as emit:
            candidate = generate_stream_init_config(emit, discard_notice_sink)
        trailing_comment = (
            "# rename: / kind_label: / kind_labels: / where: / only: / ignore: /"
            " sub_types: (membership) --\n"
            "# never proposed either; each is author intent with no sidecar-derived"
            " value (proposing one would be invention). Add them yourself.\n"
        )
        assert trailing_comment in candidate
        print("init --mode streaming trailing comment:")
        print(trailing_comment)

    print(
        "SUCCESS: default change scope, only-narrowed scope (with riding"
        " as-of values), ignore-narrowed lifecycle-only feed, and init's"
        " never-proposed authoring-field trailing comment all verified"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
