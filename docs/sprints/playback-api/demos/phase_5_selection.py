#!/usr/bin/env python
"""
Demo: The tier-1 selection surface (resolve_selection)

Sprint: playback-api
Phase: 5

resolve_selection is sidecar-only (no data reads), so this demo builds a
base.json in-memory and parses it straight into a Sidecar — no run.duckdb
needed. It resolves a RecordAtomSelection over one sub-typed kind three
ways (properties=None / empty tuple / a named tuple in caller order), then
shows three business-rule failures and their messages: a non-exempt
slice_only property, an undeclared sub-type value, and an empty id set.
"""

from __future__ import annotations

import sys

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.playback.errors import PlaybackError
from fabulexa_forge.playback.selection import resolve_selection
from fabulexa_forge.playback.types import PlaybackSelection, RecordAtomSelection
from fabulexa_forge.reader.sidecar import Sidecar

_KIND = "patient"

_PATIENT_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    # The exempt sub-typed discriminator: selectable whatever its class.
    {
        "name": "prop__patient_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
    },
    # Non-exempt slice_only — outside the selectable domain.
    {
        "name": "prop__notes",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "slice_only",
    },
]

_SIDECAR: dict[str, object] = {
    "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
    "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 9999}],
    "tables": [
        {
            "name": "records__patient",
            "category": "records",
            "columns": _PATIENT_COLUMNS,
            "rows": 0,
            "record_kind": "patient",
        },
    ],
    "enum_domains": {"patient": {"patient_type": ["doctor", "nurse"]}},
}


def _record_selection(
    sub_types: tuple[str, ...] = (),
    properties: tuple[str, ...] | None = (),
    record_ids: frozenset[str] | None = None,
) -> RecordAtomSelection:
    return RecordAtomSelection(
        kind=_KIND,
        sub_types=sub_types,
        properties=properties,
        record_ids=record_ids,
    )


def main() -> int:
    sidecar = Sidecar.from_raw(_SIDECAR)

    resolved_full = resolve_selection(
        sidecar,
        PlaybackSelection(
            records=(_record_selection(properties=None),), memberships=()
        ),
    )
    full_props = resolved_full.records[0].properties
    print(f"properties=None -> {full_props}")

    resolved_identity = resolve_selection(
        sidecar,
        PlaybackSelection(records=(_record_selection(properties=()),), memberships=()),
    )
    print(f"properties=() -> {resolved_identity.records[0].properties}")

    resolved_named = resolve_selection(
        sidecar,
        PlaybackSelection(
            records=(_record_selection(properties=("status", "name")),), memberships=()
        ),
    )
    named_props = resolved_named.records[0].properties
    print(f"properties=('status', 'name') (caller order) -> {named_props}")

    failures: list[str] = []

    try:
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(_record_selection(properties=("notes",)),), memberships=()
            ),
        )
    except PlaybackError as exc:
        print(f"PropertiesNotSliceOnly failure: {exc}")
        failures.append("slice_only")

    try:
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(_record_selection(sub_types=("orderly",)),), memberships=()
            ),
        )
    except PlaybackError as exc:
        print(f"SubTypesDeclared failure: {exc}")
        failures.append("sub_type")

    try:
        resolve_selection(
            sidecar,
            PlaybackSelection(
                records=(_record_selection(record_ids=frozenset()),), memberships=()
            ),
        )
    except PlaybackError as exc:
        print(f"InstanceSetNonEmpty failure: {exc}")
        failures.append("empty_ids")

    if full_props != ("patient_type", "name", "status"):
        print(
            "FAIL: expected properties=None to resolve to "
            f"('patient_type', 'name', 'status'), got {full_props}",
            file=sys.stderr,
        )
        return 1
    if resolved_identity.records[0].properties != ():
        print(
            "FAIL: expected properties=() to resolve to identity only", file=sys.stderr
        )
        return 1
    if named_props != ("name", "status"):
        print(
            "FAIL: expected a named tuple to resolve to sidecar declaration order "
            f"('name', 'status'), got {named_props}",
            file=sys.stderr,
        )
        return 1
    if failures != ["slice_only", "sub_type", "empty_ids"]:
        print(
            f"FAIL: expected all three rule failures, got {failures}", file=sys.stderr
        )
        return 1

    print(
        "SUCCESS: properties=None resolved to the full tracked+constant+exempt-"
        "discriminator set in sidecar order (never the non-exempt slice_only "
        "'notes'); a named tuple's caller order does not affect the resolved "
        "order; and the slice_only / undeclared sub-type / empty id-set rule "
        "failures all raised PlaybackError with the documented message shapes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
