#!/usr/bin/env python
"""
Demo: Companion data dictionary -- the README ordering delta and the
manifest's machine-readable documentation mirror, both resolved through
`emit.sidecar.documentation()` via the report's carried provenance
(`exporters/companion/dictionary.py`).

Sprint: documentation-channel
Phase: 5

Builds a documented fixture emit (records__team, records__actor -- table
description, a described-only property, a described+unit property, a
closed-domain property with glossed values, and one wholly undocumented
property) plus a top-level `scenario_description`. Full-exports it in
source mode (one `actor_state` table, no rename) with an author README
overlay, and prints:

- The README's Overview section: overlay prose first, then the scenario
  narrative.
- The `actor_state` table section: forwarded table description, the
  documented column inventory (description-only, description+unit, and
  name/type-only for the undocumented column), and the `status` gloss list.
- The manifest's per-column entries -- `null` for the undocumented column,
  the closed-domain `enum_options` list for `status`, and
  `manifest_format_version: 2`.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import ExportConfig, SourceConfig, SourceTableDecl
from fabulexa_forge.exporters.companion.manifest import build_manifest_document
from fabulexa_forge.exporters.companion.overlay import ReadmeOverlay
from fabulexa_forge.exporters.companion.readme import render_readme
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.query_spec import write_query_specs
from fabulexa_forge.exporters.source.engine import build_source_query_specs
from fabulexa_forge.exporters.source.plan import build_source_plan
from fabulexa_forge.reader.emit import open_emit, pin_session_timezone

_TEAM_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__team_name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_ACTOR_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__full_name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
        "description": "Staff member's full legal name.",
    },
    {
        "name": "prop__status",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
        "description": "Current duty status.",
    },
    {
        "name": "prop__shift_minutes",
        "type": "BIGINT",
        "history_tracked": False,
        "temporal_class": "constant",
        "description": "Length of the current shift.",
        "unit": "minutes",
    },
    {
        "name": "prop__team_id",
        "type": "VARCHAR",
        "references": "team",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {"name": "ref_index__team_id", "type": "BIGINT"},
]


def _create_ddl(table_name: str, columns: list[dict[str, object]]) -> str:
    """Build a CREATE TABLE DDL statement from a column spec list."""
    frags = ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)
    return f'CREATE TABLE "{table_name}" ({frags})'


def _build_emit(emit_dir: Path) -> None:
    """Write a documented fixture emit: one team, one actor referencing it."""
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))

    conn.execute(_create_ddl("records__team", _TEAM_COLUMNS))
    conn.execute(
        'INSERT INTO "records__team" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "t1", 0, True, None, 0, 0, "Cardiology"],
    )

    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a1", 10, True, None, 10, 0, "Dr. Smith", "A", 480, "t1", 0],
    )

    history_columns: list[dict[str, object]] = [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "kind", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "property", "type": "VARCHAR"},
        {"name": "sim_time", "type": "BIGINT"},
        {"name": "value", "type": "VARCHAR"},
    ]
    conn.execute(_create_ddl("history", history_columns))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "scenario_description": (
            "A hospital shift-handoff simulation, tracking staff duty status"
            " across care teams."
        ),
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "runtime": {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"},
        "enum_domains": {
            "actor": {
                "status": [
                    {"value": "A", "description": "Active and on duty."},
                    {"value": "I", "description": "Inactive; off duty."},
                ]
            }
        },
        "tables": [
            {
                "name": "records__team",
                "category": "records",
                "record_kind": "team",
                "rows": 1,
                "columns": _TEAM_COLUMNS,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "rows": 1,
                "description": "Hospital staff members.",
                "columns": _ACTOR_COLUMNS,
            },
            {
                "name": "history",
                "category": "fixed",
                "rows": 0,
                "columns": history_columns,
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _source_config() -> SourceConfig:
    """One state table, no rename -- output columns are the bare property names."""
    return SourceConfig(tables=(SourceTableDecl(name="actor_state", kind="actor"),))


def _discard(_notice: Notice) -> None:
    """Discard a plan notice -- the demo is indifferent to them."""


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp)
        _build_emit(emit_dir)

        with open_emit(emit_dir) as emit:
            sidecar = emit.sidecar
            anchor = resolve_effective_anchor(sidecar.runtime(), None, None, None)
            assert anchor is not None, "the fixture declares a runtime block"
            pin_session_timezone(emit, anchor)
            election = resolve_election(sidecar, None)

            config = ExportConfig(mode="source", source=_source_config())
            plan = build_source_plan(
                emit, config, anchor, election, windowed=False, notices=_discard
            )
            specs = list(build_source_query_specs(plan, None))
            report = write_query_specs(
                emit, specs, emit_dir / "export.duckdb", fmt="duckdb"
            )

            overlay = ReadmeOverlay(
                overview="This export supports the shift-handoff training exercise.",
                table_notes={},
            )
            readme_text = render_readme(
                mode="source",
                emit=emit,
                report=report,
                overlay=overlay,
                anchor=anchor,
                manifest_filename="export-manifest.json",
            )
            manifest = build_manifest_document(
                emit, config, "duckdb", anchor, report, windowed=None
            )

    print("=== README: Overview section ===")
    overview_start = readme_text.index("## Overview")
    overview_end = readme_text.index("\n\n#", overview_start + 1)
    print(readme_text[overview_start:overview_end])

    print("\n=== README: actor_state table section ===")
    table_start = readme_text.index("### actor_state")
    table_end = readme_text.index("\n\n##", table_start)
    print(readme_text[table_start:table_end])

    print("\n=== Manifest: actor_state's columns ===")
    actor_table = next(t for t in manifest["tables"] if t["name"] == "actor_state")
    print(json.dumps(actor_table, indent=2, sort_keys=True))

    # --- Assertions -------------------------------------------------------
    ok = True
    overview_text = readme_text[overview_start:overview_end]
    if "shift-handoff training exercise" not in overview_text:
        ok = False
    if "hospital shift-handoff simulation" not in overview_text:
        ok = False
    if overview_text.index("training exercise") > overview_text.index("simulation"):
        ok = False  # overlay prose must precede the scenario narrative

    table_text = readme_text[table_start:table_end]
    if "Hospital staff members." not in table_text:
        ok = False
    if "Staff member's full legal name." not in table_text:
        ok = False
    if (
        "Length of the current shift." not in table_text
        or "[minutes]" not in table_text
    ):
        ok = False
    if "`A`: Active and on duty." not in table_text:
        ok = False
    if "`team_id`" not in table_text or "records__team" in table_text:
        ok = False  # team_id itself is undocumented -- no placeholder prose

    if manifest["manifest_format_version"] != 2:
        ok = False
    if manifest["scenario_description"] != (
        "A hospital shift-handoff simulation, tracking staff duty status"
        " across care teams."
    ):
        ok = False
    columns_by_name = {c["name"]: c for c in actor_table["columns"]}
    if columns_by_name["team_id"]["description"] is not None:
        ok = False
    if columns_by_name["status"]["enum_options"] != [
        {"value": "A", "description": "Active and on duty."},
        {"value": "I", "description": "Inactive; off duty."},
    ]:
        ok = False
    if columns_by_name["shift_minutes"]["unit"] != "minutes":
        ok = False
    if actor_table["description"] != "Hospital staff members.":
        ok = False

    if not ok:
        return 1

    print(
        "\nSUCCESS: the README overview orders overlay prose before the"
        " scenario narrative; the table section forwards its description,"
        " documented columns, and a closed-domain gloss list; the manifest"
        " mirrors the same resolution with null for undocumented columns"
        " under manifest_format_version 2"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
