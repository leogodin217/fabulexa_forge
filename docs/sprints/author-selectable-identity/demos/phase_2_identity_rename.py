#!/usr/bin/env python
"""
Demo: Streaming elected-identity naming — `rename` reaches the elected surface.

Streams a small emit under a `record_index` election. Runs it once with
`rename: {record_index: id}` and once with no rename at all, and shows that:

  - The renamed run's message-key map and after-image both carry `id` — the
    single resolved output key at both sites.
  - The default run carries the elected surface's own contract column name,
    `record_index`, unchanged.
  - Neither run's after-image carries `presentation_id`, even though the
    `patient` kind mints one — the auto-published surrogate and the
    presentation_id-absorption branch are gone; a stream publishes exactly
    its elected surface unless it declares `identity` to widen the set
    (Phase 3).
  - The underlying values are byte-identical across both runs; only the
    output key name changes.

Sprint: author-selectable-identity
Phase: 2
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.loader import load_stream_config
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.streaming.engine import iter_stream_events
from fabulexa_forge.exporters.streaming.jsonl import render_jsonl_object
from fabulexa_forge.reader.emit import open_emit

#: records__patient column order: identity columns, state columns,
#: record_index, then the one tracked prop__ column — the shape every
#: streaming election fixture in this codebase follows.
_PATIENT_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "VARCHAR"},
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

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]

_RENAMED_STREAM_CONFIG = """
content: state-changes
keys:
  patient: record_index
streams:
  - name: patient
    kind: patient
    properties: [status]
    rename:
      record_index: id
"""

_DEFAULT_STREAM_CONFIG = """
content: state-changes
keys:
  patient: record_index
streams:
  - name: patient
    kind: patient
    properties: [status]
"""


def _discard_notice(notice: Notice) -> None:
    """A NoticeSink that drops every notice — the demo has none to show."""
    del notice


def _ddl(table: str, columns: list[dict[str, object]]) -> str:
    """Build a CREATE TABLE statement from a base.json-shaped column list."""
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)
    return f'CREATE TABLE "{table}" ({parts})'


def _build_demo_emit(emit_dir: Path) -> None:
    """Write a minimal two-record `patient` emit: one 'c'+'u', one 'c'+'d'.

    p1 is created with status 'new', then updated to 'active' — a 'u' event.
    p2 is created with status 'waiting', then deactivated — a 'd' event.
    Both carry a `presentation_id` surrogate the kind mints but this demo's
    `record_index` election never elects, and a `record_index` the election
    does elect.

    Args:
        emit_dir: The directory to write run.duckdb + base.json into.
    """
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_ddl("records__patient", _PATIENT_COLUMNS))
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "p1", "PT_001", 0, True, 100, 0, "active"],
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p2", "PT_002", 0, False, 200, 200, 1, "waiting"],
    )

    conn.execute(_ddl("history", _HISTORY_COLUMNS))
    history_rows = [
        ("trunk", "patient", "p1", "status", 0, "new"),
        ("trunk", "patient", "p1", "status", 100, "active"),
        ("trunk", "patient", "p2", "status", 0, "waiting"),
    ]
    for row in history_rows:
        conn.execute('INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)', list(row))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1_000_000}],
        "tables": [
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": _PATIENT_COLUMNS,
                "rows": 2,
            },
            {
                "name": "history",
                "category": "fixed",
                "columns": _HISTORY_COLUMNS,
                "rows": len(history_rows),
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _run_stream(
    emit_dir: Path, stream_config_yaml: str, config_path: Path
) -> list[dict[str, object]]:
    """Run one streaming config end to end and return the rendered JSONL objects.

    Args:
        emit_dir: The emit directory built by `_build_demo_emit`.
        stream_config_yaml: The stream config's YAML text.
        config_path: Where to write the config before loading it.

    Returns:
        Every event, rendered as `render_jsonl_object` would serialize it.
    """
    config_path.write_text(stream_config_yaml, encoding="utf-8")
    config = load_stream_config(config_path)
    with open_emit(emit_dir) as emit:
        events = iter_stream_events(emit, config, None, _discard_notice)
        return [render_jsonl_object(event) for event in events]


def _check_events(
    renamed: list[dict[str, object]], default: list[dict[str, object]]
) -> list[str]:
    """Compare the renamed and default runs; return a list of failure messages.

    Args:
        renamed: Events from the `rename: {record_index: id}` run.
        default: Events from the no-rename run.

    Returns:
        Empty when every invariant holds; otherwise one message per failure.
    """
    errors: list[str] = []
    if len(renamed) != len(default):
        errors.append(f"event count differs: {len(renamed)} vs {len(default)}")
        return errors

    for r, d in zip(renamed, default):
        if r["seq"] != d["seq"] or r["op"] != d["op"]:
            errors.append(f"seq/op mismatch: {r} vs {d}")
            continue

        r_key = r["key"]
        d_key = d["key"]
        assert isinstance(r_key, dict) and isinstance(d_key, dict)
        if "id" not in r_key:
            errors.append(f"renamed run's key map missing 'id': {r_key}")
        if "record_index" not in d_key:
            errors.append(f"default run's key map missing 'record_index': {d_key}")
        if r_key.get("id") != d_key.get("record_index"):
            errors.append(f"key value diverged: {r_key} vs {d_key}")

        r_after = r["after"]
        d_after = d["after"]
        if r_after is not None and "presentation_id" in r_after:
            errors.append(
                f"renamed run's after-image carries presentation_id: {r_after}"
            )
        if d_after is not None and "presentation_id" in d_after:
            errors.append(
                f"default run's after-image carries presentation_id: {d_after}"
            )
        if r_after is not None:
            assert isinstance(d_after, dict)
            if "id" not in r_after or "record_index" not in d_after:
                errors.append(
                    f"after-image identity key missing: {r_after} vs {d_after}"
                )
            elif r_after.get("id") != d_after.get("record_index"):
                errors.append(
                    f"after-image identity value diverged: {r_after} vs {d_after}"
                )
            if r_after.get("status") != d_after.get("status"):
                errors.append(f"after-image status diverged: {r_after} vs {d_after}")

    return errors


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _build_demo_emit(tmp_dir)

        print("rename: {record_index: id}")
        renamed_events = _run_stream(
            tmp_dir, _RENAMED_STREAM_CONFIG, tmp_dir / "renamed_config.yaml"
        )
        for event in renamed_events:
            print(f"  {json.dumps(event)}")

        print("no rename (contract-name default)")
        default_events = _run_stream(
            tmp_dir, _DEFAULT_STREAM_CONFIG, tmp_dir / "default_config.yaml"
        )
        for event in default_events:
            print(f"  {json.dumps(event)}")

        errors = _check_events(renamed_events, default_events)
        for error in errors:
            print(f"FAILURE: {error}")
        if errors:
            return 1

    print(
        "SUCCESS: renamed run carries 'id' at the key map and after-image; "
        "default run carries 'record_index'; neither carries presentation_id; "
        "values agree"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
