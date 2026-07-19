#!/usr/bin/env python
"""
Demo: Streaming refusal (StreamPropertySliceOnly)

Sprint: slice-only-policy
Phase: 4

Builds a standalone emit with two kinds:
  - item (non-sub-typed): prop__status (tracked) and prop__secret (non-exempt
    slice_only).
  - widget (sub-typed via enum_domains): prop__widget_type declared
    temporal_class: slice_only, yet exempt as the kind's <kind>_type
    discriminator.

Demonstrates, directly against iter_stream_events:
  - a kinds[].properties entry naming prop__secret raises ExportError before
    the first event, naming the kind, the property, and the class.
  - the same shape of config selecting the exempt discriminator column (via
    a 'types' sub-type selection) streams normally — the exemption is
    mechanical and never consults the column's declared class.
  - a tracked property (prop__status) selected alongside the slice_only
    column is unaffected as long as it is not itself selected.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import StreamConfig, StreamKindSelection
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.streaming.engine import iter_stream_events
from fabulexa_forge.reader.emit import open_emit

# ---------------------------------------------------------------------------
# The emit: item (one refused column) + widget (exempt discriminator)
# ---------------------------------------------------------------------------

_ITEM_COLUMNS: list[dict[str, object]] = [
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
        "name": "prop__secret",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "slice_only",  # non-exempt: refused when selected
    },
]

_WIDGET_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__widget_type",
        "type": "VARCHAR",
        "history_tracked": False,
        # Declared slice_only, yet exempt as the discriminator — its class
        # is never consulted.
        "temporal_class": "slice_only",
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


def _build_emit(emit_dir: Path) -> None:
    """Write the item + widget run.duckdb + base.json emit."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))

    item_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _ITEM_COLUMNS)
    conn.execute(f'CREATE TABLE "records__item" ({item_ddl})')
    conn.execute(
        'INSERT INTO "records__item" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "i1", 0, True, 0, 0, "active", "s3cr3t"],
    )

    widget_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _WIDGET_COLUMNS)
    conn.execute(f'CREATE TABLE "records__widget" ({widget_ddl})')
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "w1", 0, True, 0, 0, "alpha"],
    )

    history_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _HISTORY_COLUMNS)
    conn.execute(f'CREATE TABLE "history" ({history_ddl})')
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "item", "i1", "status", 0, "active"],
    )
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "tables": [
            {
                "name": "records__item",
                "category": "records",
                "columns": _ITEM_COLUMNS,
                "rows": 1,
                "record_kind": "item",
            },
            {
                "name": "records__widget",
                "category": "records",
                "columns": _WIDGET_COLUMNS,
                "rows": 1,
                "record_kind": "widget",
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": 1,
            },
        ],
        "enum_domains": {"widget": {"widget_type": ["alpha", "beta"]}},
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "emit"
        _build_emit(emit_dir)

        with open_emit(emit_dir) as emit:
            # --- Non-exempt slice_only property: refused before the first event ---
            refused_config = StreamConfig(
                content="state-changes",
                kinds=[StreamKindSelection(kind="item", properties=["secret"])],
            )
            try:
                # No list() — the error must come from the call itself, before
                # any event is yielded.
                iter_stream_events(emit, refused_config, None)
            except ExportError as exc:
                message = str(exc)
                for fragment in ("kind 'item'", "property 'secret'", "slice_only"):
                    if fragment not in message:
                        _fail(f"refusal message missing {fragment!r}: {message}")
                print(f"REFUSED: {message}")
            else:
                _fail("selecting a non-exempt slice_only property did not raise")

            # --- Tracked property alongside the slice_only column: unaffected ---
            tracked_config = StreamConfig(
                content="state-changes",
                kinds=[StreamKindSelection(kind="item", properties=["status"])],
            )
            tracked_events = list(iter_stream_events(emit, tracked_config, None))
            if len(tracked_events) != 1:
                _fail(f"expected 1 tracked-property event, got {len(tracked_events)}")
            print(f"STREAMED (tracked, unaffected): {len(tracked_events)} event(s)")

            # --- Exempt discriminator: passes StreamPropertySliceOnly at any
            # declared class, and a 'types' sub-type selection streams normally.
            discriminator_config = StreamConfig(
                content="state-changes",
                kinds=[
                    StreamKindSelection(
                        kind="widget", properties=["widget_type"], types=["alpha"]
                    )
                ],
            )
            discriminator_events = list(
                iter_stream_events(emit, discriminator_config, None)
            )
            if len(discriminator_events) != 1:
                _fail(
                    f"expected 1 discriminator event, got {len(discriminator_events)}"
                )
            after = discriminator_events[0].after
            if after is None or after.get("prop__widget_type") != "alpha":
                _fail(f"discriminator after-image unexpected: {after}")
            print(
                "STREAMED (exempt discriminator, types=['alpha']):"
                f" {len(discriminator_events)} event(s), after={after}"
            )

        print(
            "SUCCESS: a non-exempt slice_only property is refused before the"
            " first event, naming the kind/property/class; tracked properties"
            " and the exempt discriminator (with a 'types' selection) stream"
            " normally"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
