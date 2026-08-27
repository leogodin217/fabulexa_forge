#!/usr/bin/env python
"""
Demo: Streaming identity projection — `identity` widens a topic's published set.

Streams a `patient` kind-shaped stream declaring `identity: [record_index,
presentation_id]` under a `record_index` election, with a `rename` on both
surfaces. Shows that:

  - The after-image carries both surfaces under their wire names (`id` from
    the `record_index` election, `nhs_number` from the `presentation_id`
    relation) — the elected surface alone widened by the declared set,
    rendered in sidecar column order regardless of declaration order.
  - The message key still carries only the elected surface (`id`) — a
    published non-elected surface never reaches the key.
  - Publishing `presentation_id` on a population the presentation-key
    registry does not declare raises `ElectionPresentationUndeclared` at
    call time, before any row is read — the deliberate tightening: a
    published surrogate requires the claim.
  - Listing an identity surface (`record_index`) in `properties` raises
    `StreamPropertyNotAddressable` at parse-independent business-rule time
    — identity is projected through `identity`, never selected through
    `properties`.

Sprint: author-selectable-identity
Phase: 3
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.loader import load_stream_config
from fabulexa_forge.errors import ElectionPresentationUndeclared, ExportError
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

#: A registry-declared `patient` presentation_id claim — the success run's
#: sidecar carries this; the tightening-trigger run's sidecar omits it
#: entirely.
_PATIENT_REGISTRY: dict[str, object] = {
    "patient": {
        "key": {
            "unique_within": "emit",
            "branch_stable": False,
            "slice_stable": False,
            "key_space": {"class": "counter", "prefix": "PT_", "width": 3},
        }
    }
}

_WIDENED_STREAM_CONFIG = """
content: state-changes
keys:
  patient: record_index
streams:
  - name: patient
    kind: patient
    identity: [presentation_id, record_index]
    properties: [status]
    rename:
      record_index: id
      presentation_id: nhs_number
"""

_UNDECLARED_STREAM_CONFIG = """
content: state-changes
keys:
  patient: record_index
streams:
  - name: patient
    kind: patient
    identity: [record_index, presentation_id]
    properties: [status]
"""

_PROPERTY_COLLISION_STREAM_CONFIG = """
content: state-changes
keys:
  patient: record_index
streams:
  - name: patient
    kind: patient
    properties: [status, record_index]
"""


def _discard_notice(notice: Notice) -> None:
    """A NoticeSink that drops every notice — the demo has none to show."""
    del notice


def _ddl(table: str, columns: list[dict[str, object]]) -> str:
    """Build a CREATE TABLE statement from a base.json-shaped column list."""
    parts = ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)
    return f'CREATE TABLE "{table}" ({parts})'


def _build_demo_emit(
    emit_dir: Path, *, presentation_keys: dict[str, object] | None
) -> None:
    """Write a minimal two-record `patient` emit: one 'c'+'u', one 'c'+'d'.

    p1 is created with status 'new', then updated to 'active' — a 'u' event.
    p2 is created with status 'waiting', then deactivated — a 'd' event.

    Args:
        emit_dir: The directory to write run.duckdb + base.json into.
        presentation_keys: The sidecar `presentation_keys` block, or None to
            leave `patient` registry-undeclared.
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
    if presentation_keys is not None:
        sidecar["presentation_keys"] = presentation_keys
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


def _check_widened_events(events: list[dict[str, object]]) -> list[str]:
    """Verify the widened-identity run's key map and after-image.

    Args:
        events: The `identity: [presentation_id, record_index]` run's events.

    Returns:
        Empty when every invariant holds; otherwise one message per failure.
    """
    errors: list[str] = []
    for event in events:
        key = event["key"]
        assert isinstance(key, dict)
        if set(key) != {"id"}:
            errors.append(f"message key carries more than the elected surface: {key}")

        after = event["after"]
        if after is None:
            continue
        assert isinstance(after, dict)
        if "id" not in after or "nhs_number" not in after:
            errors.append(f"after-image missing a published surface: {after}")
        # Sidecar column order is record_id, presentation_id, record_index —
        # nhs_number (presentation_id) must precede id (record_index)
        # regardless of the config's declared identity order.
        keys = list(after)
        if "nhs_number" in keys and "id" in keys:
            if keys.index("nhs_number") > keys.index("id"):
                errors.append(f"published set not in sidecar column order: {keys}")
    return errors


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        print("identity: [presentation_id, record_index] + rename on both surfaces")
        with_registry_dir = tmp_dir / "with_registry"
        with_registry_dir.mkdir()
        _build_demo_emit(with_registry_dir, presentation_keys=_PATIENT_REGISTRY)
        widened_events = _run_stream(
            with_registry_dir,
            _WIDENED_STREAM_CONFIG,
            with_registry_dir / "widened_config.yaml",
        )
        for event in widened_events:
            print(f"  {json.dumps(event)}")

        errors = _check_widened_events(widened_events)

        print(
            "\ntriggering ElectionPresentationUndeclared:"
            " presentation_id published on an undeclared population"
        )
        undeclared_dir = tmp_dir / "undeclared"
        undeclared_dir.mkdir()
        _build_demo_emit(undeclared_dir, presentation_keys=None)
        try:
            _run_stream(
                undeclared_dir,
                _UNDECLARED_STREAM_CONFIG,
                undeclared_dir / "undeclared_config.yaml",
            )
            errors.append("expected ElectionPresentationUndeclared, none raised")
        except ElectionPresentationUndeclared as exc:
            print(f"  raised as expected: {exc}")

        print(
            "\ntriggering StreamPropertyNotAddressable:"
            " an identity surface listed in properties"
        )
        try:
            _run_stream(
                with_registry_dir,
                _PROPERTY_COLLISION_STREAM_CONFIG,
                with_registry_dir / "property_collision_config.yaml",
            )
            errors.append("expected StreamPropertyNotAddressable, none raised")
        except ExportError as exc:
            if type(exc).__name__ != "StreamPropertyNotAddressable":
                errors.append(f"wrong error class raised: {type(exc).__name__}: {exc}")
            else:
                print(f"  raised as expected: {exc}")

        for error in errors:
            print(f"FAILURE: {error}")
        if errors:
            return 1

    print(
        "SUCCESS: identity widens the published set in sidecar column order; "
        "the message key stays the elected surface alone; publishing an "
        "undeclared presentation_id and listing an identity surface in "
        "properties both fail at call time"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
