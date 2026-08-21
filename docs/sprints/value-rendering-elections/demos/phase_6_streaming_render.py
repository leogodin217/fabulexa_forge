#!/usr/bin/env python
"""
Demo: Streaming attach — a per-stream `render:` map (`decimal` +
`json_precision`) applied at the codec seam, upstream of after-image
assembly.
Sprint: value-rendering-elections
Phase: 6

Builds a scratch emit (one `widget` records kind carrying a tracked DOUBLE
property and a constant JSON-payload property), declares one `KindStream`
electing `decimal` on the DOUBLE property and `json_precision` on the JSON
property, and streams its events. Prints the 'c' and 'u' events' elected
after-image text (byte-identical to the table modes' render of the same
values), the unaffected 'd' tombstone (no after-image to elect), and the
unchanged Debezium value schema (elected entries remain string-typed by
codec — the election changes value text only).
"""

import sys
import tempfile
from pathlib import Path

# The vendored fixture-sidecar authority lives under tests/_support — reused
# here (as pytest itself does) rather than hand-rolling a base.json, so the
# demo's scratch emit is built through the one sidecar-conformant authority
# every fixture in this repo goes through.
_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import duckdb  # noqa: E402
from _support.sidecar_builder import (  # noqa: E402
    identity_column,
    prop_column,
    write_emit,
)

from fabulexa_forge.anchor import resolve_effective_anchor  # noqa: E402
from fabulexa_forge.config.models import KindStream, StreamConfig  # noqa: E402
from fabulexa_forge.exporters.streaming.debezium import (  # noqa: E402
    build_debezium_value_schema,
)
from fabulexa_forge.exporters.streaming.engine import iter_stream_events  # noqa: E402
from fabulexa_forge.exporters.streaming.jsonl import render_jsonl_object  # noqa: E402
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
    prop_column(
        "prop__volume", "DOUBLE", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__context", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
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

    `w1` is created then updated (a 'u' history change to `prop__volume`) —
    the 'c'/'u' pair the demo elects. `w2` is created then deactivated (a
    'd' tombstone) — the unaffected-tombstone pair.

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
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        [
            (
                "trunk",
                "w1",
                0,
                True,
                None,
                5 * _MS,
                0,
                45.6789,
                '{"discount_pct": 0.125}',
            ),
            (
                "trunk",
                "w2",
                0,
                False,
                10 * _MS,
                10 * _MS,
                1,
                7.0,
                '{"discount_pct": 0.5}',
            ),
        ],
    )
    history_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _HISTORY_COLUMNS)
    conn.execute(f'CREATE TABLE "history" ({history_ddl})')
    conn.executemany(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        [
            ("trunk", "widget", "w1", "volume", 0, "12.3456"),
            ("trunk", "widget", "w1", "volume", 5 * _MS, "45.6789"),
            ("trunk", "widget", "w2", "volume", 0, "7.0"),
        ],
    )
    conn.close()

    write_emit(
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


def _events_by_op(events: list[StreamEvent], record_id: str) -> dict[str, StreamEvent]:
    """Index a record's events by op (one event per op, this demo's fixture)."""
    return {e.op: e for e in events if e.record_id == record_id}


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = _build_demo_emit(Path(tmp))

        config = StreamConfig(
            content="state-changes",
            streams=[
                KindStream(
                    name="widgets",
                    kind="widget",
                    properties=["volume", "context"],
                    render={
                        "volume": {"decimal": [6, 3]},
                        "context": {"json_precision": {"discount_pct": 2}},
                    },
                )
            ],
        )

        with open_emit(emit_dir) as emit:
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            events = list(iter_stream_events(emit, config, anchor))

        by_w1 = _events_by_op(events, "w1")
        by_w2 = _events_by_op(events, "w2")

        print("w1 'c' event (elected after-image):")
        c_obj = render_jsonl_object(by_w1["c"])
        print(f"  {c_obj}")
        assert c_obj["after"]["prop__volume"] == "12.346"
        assert c_obj["after"]["prop__context"] == '{"discount_pct": 0.13}'

        print("w1 'u' event (elected after-image):")
        u_obj = render_jsonl_object(by_w1["u"])
        print(f"  {u_obj}")
        assert u_obj["after"]["prop__volume"] == "45.679"
        assert u_obj["after"]["prop__context"] == '{"discount_pct": 0.13}'

        print("w2 'd' event (tombstone — no after-image to elect):")
        d_obj = render_jsonl_object(by_w2["d"])
        print(f"  {d_obj}")
        assert d_obj["after"] is None

        schema = build_debezium_value_schema(
            table="widget",
            columns=["record_id", "prop__volume", "prop__context"],
            source_name="fabulexa",
            connector="postgresql",
        )
        after_struct = next(f for f in schema["fields"] if f["field"] == "after")
        value_fields = {f["field"]: f["type"] for f in after_struct["fields"]}
        print("Debezium value schema field types (unchanged — string-typed):")
        print(f"  {value_fields}")
        assert set(value_fields.values()) == {"string"}

    print(
        "SUCCESS: streaming's decimal/json_precision render elects c/u"
        " after-image text; d tombstones and the Debezium value schema"
        " are unaffected"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
