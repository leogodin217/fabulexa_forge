#!/usr/bin/env python
"""
Demo: Source + base provenance stamping -- ColumnProvenance stamped by the
source plan builders (state / junction / event log's kind_values) and the
base plan builder, copied verbatim onto QuerySpec (and thus TableReport) by
both engines.

Sprint: documentation-channel
Phase: 4

Builds a fixture emit (records__team, records__actor referencing team,
membership__actor__badge) and compiles:

- A source plan: `actor_state` (state table, `prop__full_name -> name`
  rename), `actor_badges` (junction over membership__actor__badge), and
  `activity_log` (event log with two sources -- team and actor -- and a
  `kind_labels: {team: Teams}` mapping).
- A base plan: one flat table per surviving kind, `actor` carrying a
  reference edge to `team`.

Prints each source table's provenance map, the event log's ordered
`kind_values['item_type']` gloss list, and base's `actor` table's
provenance -- `id` / `prop__team_id` (the id-space self and edge columns)
carry entries while `actor_key` / `team_id_key` (the re-derived index-space
edge keys) do not. Then compiles + writes both plans and confirms the
written TableReport's provenance/kind_values equal the compiled QuerySpec's
-- the engine copy delta this phase adds.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    ExportConfig,
    MembershipRef,
    SourceConfig,
    SourceEventsDecl,
    SourceEventSourceDecl,
    SourceTableDecl,
)
from fabulexa_forge.exporters.base.engine import build_base_query_specs
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.query_spec import write_query_specs
from fabulexa_forge.exporters.source.engine import build_source_query_specs
from fabulexa_forge.exporters.source.plan import build_source_plan
from fabulexa_forge.reader.emit import open_emit, pin_session_timezone

_MS = 1_000_000

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

_BADGE_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__role_name", "type": "VARCHAR"},
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _create_ddl(table_name: str, columns: list[dict[str, object]]) -> str:
    """Build a CREATE TABLE DDL statement from a column spec list."""
    frags = ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)
    return f'CREATE TABLE "{table_name}" ({frags})'


def _build_emit(emit_dir: Path) -> None:
    """Write a fixture emit: one team, one actor (referencing it), two badges."""
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))

    conn.execute(_create_ddl("records__team", _TEAM_COLUMNS))
    conn.execute(
        'INSERT INTO "records__team" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "t1", 0, True, None, 0, 0, "Cardiology"],
    )

    conn.execute(_create_ddl("records__actor", _ACTOR_COLUMNS))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a1", 10, True, None, 10, 0, "Dr. Smith", "t1", 0],
    )

    conn.execute(_create_ddl("membership__actor__badge", _BADGE_COLUMNS))
    conn.execute(
        'INSERT INTO "membership__actor__badge" VALUES (?, ?, ?, ?, ?)',
        ["trunk", "a1", 100 * _MS, 200 * _MS, "lead"],
    )
    conn.execute(
        'INSERT INTO "membership__actor__badge" VALUES (?, ?, ?, ?, ?)',
        ["trunk", "a1", 250 * _MS, None, "support"],
    )

    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 300 * _MS}],
        "runtime": {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"},
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
                "columns": _ACTOR_COLUMNS,
            },
            {
                "name": "membership__actor__badge",
                "category": "membership",
                "record_kind": "actor",
                "property": "badge",
                "rows": 2,
                "columns": _BADGE_COLUMNS,
            },
            {
                "name": "history",
                "category": "fixed",
                "rows": 0,
                "columns": _HISTORY_COLUMNS,
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _source_config() -> SourceConfig:
    """State table (rename), junction, and a two-source event log with a label."""
    return SourceConfig(
        tables=(
            SourceTableDecl(
                name="actor_state", kind="actor", rename={"prop__full_name": "name"}
            ),
            SourceTableDecl(
                name="actor_badges",
                membership=MembershipRef(kind="actor", property="badge"),
            ),
        ),
        events=SourceEventsDecl(
            name="activity_log",
            sources=(
                SourceEventSourceDecl(kind="team"),
                SourceEventSourceDecl(kind="actor"),
            ),
        ),
        kind_labels={"team": "Teams"},
    )


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

            source_config = ExportConfig(mode="source", source=_source_config())
            source_plan = build_source_plan(
                emit, source_config, anchor, election, windowed=False, notices=_discard
            )

            print("=== Source plan: per-table provenance ===")
            by_name = {unit.name: unit for unit in source_plan.tables}
            for name, unit in by_name.items():
                print(f"\n{name}:")
                for col_name, entry in unit.provenance.items():
                    print(
                        f"  {col_name} -> ({entry.source_table}, {entry.source_column})"
                    )

            assert source_plan.events is not None
            print(f"\n{source_plan.events.name} (event log):")
            print(f"  provenance: {dict(source_plan.events.provenance)!r}")
            gloss = [
                (entry.label, entry.source_kind)
                for entry in source_plan.events.kind_values["item_type"]
            ]
            print(f"  kind_values['item_type'] (event-source compile order): {gloss}")

            source_specs = list(build_source_query_specs(source_plan, None))
            source_report = write_query_specs(
                emit, source_specs, emit_dir / "source.duckdb", fmt="duckdb"
            )

            base_config = ExportConfig(mode="base")
            base_specs = build_base_query_specs(
                emit, base_config, anchor, None, _discard
            )
            print("\n=== Base plan: 'actor' table provenance ===")
            actor_spec = next(s for s in base_specs if s.table_name == "actor")
            for col_name, entry in actor_spec.provenance.items():
                print(f"  {col_name} -> ({entry.source_table}, {entry.source_column})")
            print("  absent (re-derived edge keys): actor_key, team_id_key")

            base_report = write_query_specs(
                emit, base_specs, emit_dir / "base.duckdb", fmt="duckdb"
            )

    # --- Assertions -----------------------------------------------------
    if set(by_name["actor_state"].provenance) != {
        "id",
        "created_at",
        "active",
        "deactivated_at",
        "updated_at",
        "name",
        "team_id",
    }:
        return 1
    if by_name["actor_state"].provenance["name"].source_column != "prop__full_name":
        return 1
    if set(by_name["actor_badges"].provenance) != {
        "actor_id",
        "joined_at",
        "left_at",
        "role_name",
    }:
        return 1
    if source_plan.events.provenance != {}:
        return 1
    if gloss != [("Teams", "team"), ("actor", "actor")]:
        return 1

    if (
        "prop__team_id" not in actor_spec.provenance
        or "id" not in actor_spec.provenance
    ):
        return 1
    if "actor_key" in actor_spec.provenance or "team_id_key" in actor_spec.provenance:
        return 1

    source_by_name = {spec.table_name: spec for spec in source_specs}
    for table in source_report.tables:
        spec = source_by_name[table.name]
        if table.provenance != spec.provenance or table.kind_values != spec.kind_values:
            return 1
    base_by_name = {spec.table_name: spec for spec in base_specs}
    for table in base_report.tables:
        spec = base_by_name[table.name]
        if table.provenance != spec.provenance:
            return 1

    print(
        "\nSUCCESS: source state/junction/event-log units and base flat table"
        " specs stamp provenance at plan build; both engines copy it"
        " verbatim onto QuerySpec, and write_query_specs forwards it"
        " verbatim onto TableReport"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
