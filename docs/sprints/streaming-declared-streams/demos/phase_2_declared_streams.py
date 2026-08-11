#!/usr/bin/env python
"""
Demo: The declared-stream grammar end-to-end (models -> engine -> jsonl sink)
Sprint: streaming-declared-streams
Phase: 2

Builds a sub-typed-kind emit (kind `entity`, discriminator values `product` /
`infrastructure` / `gadget`, the last never populated) plus a flat kind
`order`, then streams a `StreamConfig` declaring:

  - `product_feed`   — one stream scoped to sub_types=[product]
  - `infra_notify`   — sub_types=[infrastructure], properties=[] (a
                        notification feed: the full c/u/d event set with an
                        identity-only after-image)
  - `catalog`        — a combined stream, sub_types=[product, infrastructure],
                        properties=[category, status] (one column list; each
                        row carries NULL in whichever column its sub-type does
                        not declare)
  - `gadget_feed`     — sub_types=[gadget]; declared but zero events (no
                        gadget rows exist) — the declared-but-empty guarantee
  - `orders_feed`     — a renamed flat kind (`order` -> topic `orders_feed`)

`product_feed` and `catalog` both cover the `product` population, so `e1`'s
create and update events appear once per covering stream — distinct `seq`,
identical key, after-image content differing only by each stream's
`properties` projection (design doc § Merge order, the multiplicity
divergence).

Exercises exactly the Phase 2 step-1 surface (config models + engine +
Layer-A routing): `iter_stream_events`, `build_topic_set`, and the shipped
jsonl sink — no driver/CLI, migrated in a later step.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

import duckdb  # noqa: E402
from _support.sidecar_builder import (  # noqa: E402
    identity_column,
    prop_column,
    write_emit,
)

from fabulexa_forge.config.models import StreamConfig  # noqa: E402
from fabulexa_forge.exporters.streaming.engine import (  # noqa: E402
    build_topic_set,
    iter_stream_events,
)
from fabulexa_forge.exporters.streaming.jsonl import write_jsonl_stream  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402

_ENTITY_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    # Discriminator: declared slice_only, yet exempt as <kind>_type — the
    # class is never consulted for it (StreamPropertySliceOnly's exemption).
    prop_column(
        "prop__entity_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="slice_only",
    ),
    prop_column(
        "prop__category", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_ORDER_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__state", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_HISTORY_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

# e1: a 'product' -- category changes t10 -> t20; status never applies (NULL).
# e2: an 'infrastructure' -- status changes t15 -> t25, deactivated at t40;
#     category never applies (NULL).
_ENTITY_ROWS = [
    ("trunk", "e1", 10, True, None, 20, 0, "product", "shoes", None),
    ("trunk", "e2", 15, False, 40, 25, 1, "infrastructure", None, "up"),
]
_ORDER_ROWS = [("trunk", "o1", 5, True, None, 35, 0, "new")]
_HISTORY_ROWS = [
    ("trunk", "entity", "e1", "category", 10, "shoes"),
    ("trunk", "entity", "e1", "category", 20, "boots"),
    ("trunk", "entity", "e2", "status", 15, "up"),
    ("trunk", "entity", "e2", "status", 25, "down"),
    ("trunk", "order", "o1", "state", 5, "new"),
    ("trunk", "order", "o1", "state", 35, "shipped"),
]


def _ddl(table: str, cols: list[dict[str, object]]) -> str:
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in cols)
    return f'CREATE TABLE "{table}" ({parts})'


def _build_demo_emit(emit_dir: Path) -> None:
    """Write a sub-typed `entity` kind + a flat `order` kind emit to `emit_dir`."""
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_ddl("records__entity", _ENTITY_COLS))
    conn.execute(_ddl("records__order", _ORDER_COLS))
    conn.execute(_ddl("history", _HISTORY_COLS))
    entity_placeholders = ", ".join("?" for _ in _ENTITY_COLS)
    for row in _ENTITY_ROWS:
        conn.execute(
            f'INSERT INTO "records__entity" VALUES ({entity_placeholders})', list(row)
        )
    order_placeholders = ", ".join("?" for _ in _ORDER_COLS)
    for row in _ORDER_ROWS:
        conn.execute(
            f'INSERT INTO "records__order" VALUES ({order_placeholders})', list(row)
        )
    for row in _HISTORY_ROWS:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    write_emit(
        emit_dir,
        tables=[
            {
                "name": "records__entity",
                "category": "records",
                "columns": _ENTITY_COLS,
                "rows": len(_ENTITY_ROWS),
                "record_kind": "entity",
            },
            {
                "name": "records__order",
                "category": "records",
                "columns": _ORDER_COLS,
                "rows": len(_ORDER_ROWS),
                "record_kind": "order",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLS,
                "rows": len(_HISTORY_ROWS),
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
        extra={
            "enum_domains": {
                "entity": {"entity_type": ["product", "infrastructure", "gadget"]}
            }
        },
    )


def _build_demo_config() -> StreamConfig:
    """The declared-stream config exercising every Phase 2 behavior."""
    return StreamConfig(
        content="state-changes",
        streams=[
            {
                "name": "product_feed",
                "kind": "entity",
                "sub_types": ["product"],
                "properties": ["category"],
            },
            {
                "name": "infra_notify",
                "kind": "entity",
                "sub_types": ["infrastructure"],
                "properties": [],
            },
            {
                "name": "catalog",
                "kind": "entity",
                "sub_types": ["product", "infrastructure"],
                "properties": ["category", "status"],
            },
            {
                "name": "gadget_feed",
                "kind": "entity",
                "sub_types": ["gadget"],
                "properties": [],
            },
            {"name": "orders_feed", "kind": "order", "properties": ["state"]},
        ],
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp)
        _build_demo_emit(emit_dir)
        config = _build_demo_config()

        topic_set = build_topic_set(config)
        print(f"topic set (declaration order): {topic_set}")

        out_dir = emit_dir / "out"
        os.makedirs(out_dir, exist_ok=True)

        with open_emit(emit_dir) as emit:
            events = list(iter_stream_events(emit, config, None))
            outcome = write_jsonl_stream(
                iter(events), "file", out_dir, topic_set=topic_set
            )

        # The driver applies this zero-count / empty-file guarantee (Phase 2
        # step 2, not touched here); mirrored inline so the demo can show a
        # declared-but-empty topic's file without the driver.
        for topic in topic_set:
            file_path = out_dir / f"{topic}.jsonl"
            if not file_path.exists():
                file_path.write_text("", encoding="utf-8")

        print("\nevents_per_topic (includes the declared-but-empty 'gadget_feed'):")
        for topic in topic_set:
            print(f"  {topic}: {outcome.events_per_topic[topic]}")

        print(f"\ntotal_events: {outcome.total_events}")
        print("\ntopic files written:")
        for topic in sorted(out_dir.iterdir()):
            print(f"  {topic.name} ({topic.stat().st_size} bytes)")

        print("\ne1 (a 'product') appears once per covering stream:")
        e1_events = [e for e in events if e.record_id == "e1"]
        for e in e1_events:
            print(f"  seq={e.seq} topic={e.topic!r} op={e.op} after={e.after}")

        # -- Assertions the demo proves --

        # Declared name list, declaration order; unaffected by which streams
        # actually yield rows.
        assert topic_set == (
            "product_feed",
            "infra_notify",
            "catalog",
            "gadget_feed",
            "orders_feed",
        )

        # Declared-but-empty: gadget_feed exists with zero events.
        assert outcome.events_per_topic["gadget_feed"] == 0
        assert (out_dir / "gadget_feed.jsonl").exists()
        assert (out_dir / "gadget_feed.jsonl").stat().st_size == 0

        # properties=[] is a notification feed: the full c/u/d event set,
        # identity-only after-image.
        infra_ops = [e.op for e in events if e.topic == "infra_notify"]
        assert infra_ops == ["c", "u", "d"]
        infra_after = [e.after for e in events if e.topic == "infra_notify" and e.after]
        assert all(set(a) == {"record_id"} for a in infra_after)

        # Combined stream: one column list; a row's inapplicable column is NULL.
        catalog_after = {
            e.record_id: e.after for e in events if e.topic == "catalog" and e.op == "c"
        }
        assert catalog_after["e1"] == {
            "record_id": "e1",
            "prop__category": "shoes",
            "prop__status": None,
        }
        assert catalog_after["e2"] == {
            "record_id": "e2",
            "prop__category": None,
            "prop__status": "up",
        }

        # Overlapping streams: e1's c/u events appear once per covering
        # stream (product_feed and catalog), distinct seq, same op/record_id,
        # after-image differing only by projection.
        assert [e.topic for e in e1_events] == [
            "catalog",
            "product_feed",
            "catalog",
            "product_feed",
        ]
        assert len({e.seq for e in e1_events}) == len(e1_events)
        c_events = [e for e in e1_events if e.op == "c"]
        assert c_events[0].event_sim_time == c_events[1].event_sim_time == 10

        # Renamed flat kind: kind 'order' streams under the author-declared
        # topic name 'orders_feed', not the kind name.
        assert {e.topic for e in events if e.record_id == "o1"} == {"orders_feed"}

    print("\nSUCCESS: declared streams -- payload-independent events, combined-stream")
    print("NULLs, overlapping-stream multiplicity, and declared-but-empty topics")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
