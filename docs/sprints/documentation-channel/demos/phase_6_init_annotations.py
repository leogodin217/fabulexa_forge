#!/usr/bin/env python
"""
Demo: `init` documentation annotations -- the three proposal engines
(dimensional, source, streaming) annotate their emitted YAML with comments
drawn from the emit's documentation view (`exporters/init_annotations.py`).

Sprint: documentation-channel
Phase: 6

Builds a documented fixture emit in-process (a flat `location` kind and a
`vehicle` kind sub-typed into `car`/`truck`, plus a `vehicle.passengers`
membership table with one reference field and one undocumented field) and
runs all three engines against it, printing each candidate config and
checking for:

- A scenario comment block at the top of every generated config.
- A source-table description comment on the dim/state/stream stubs.
- A discriminator gloss on each `sub_types: [<v>]` line (source, streaming).
- A property `description` (+ unit) comment on proposed column/property
  entries, including an annotated commented-out membership alternative
  (streaming) whose reference field reads the `member__rider__kind` column.
- Undocumented items (a `notes` property, the `seat` membership field, the
  discriminator column itself) carry no comment at all.

Then parses each emitted config back through its own self-gate (dimensional:
`load_export_config` + `validate_table`; source: `load_export_config` +
`build_source_plan`; streaming: `load_stream_config` + `iter_stream_events`,
including the membership alternative uncommented wholesale) to prove the
annotations never touched grammar.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config, load_stream_config
from fabulexa_forge.exporters.dimensional.init import generate_init_config
from fabulexa_forge.exporters.dimensional.validation import validate_table
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.source.init import generate_source_init_config
from fabulexa_forge.exporters.source.plan import build_source_plan
from fabulexa_forge.exporters.streaming.engine import iter_stream_events
from fabulexa_forge.exporters.streaming.init import generate_stream_init_config
from fabulexa_forge.reader.emit import open_emit

_SCENARIO = (
    "A city fleet-tracking simulation, following the movement of shuttle"
    " vehicles between depots and city locations."
)

_LOCATION_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": True,
        "temporal_class": "tracked",
        "description": "Human-readable location name.",
    },
    {
        "name": "prop__capacity",
        "type": "BIGINT",
        "history_tracked": False,
        "temporal_class": "constant",
        "description": "Maximum occupancy.",
        "unit": "people",
    },
    {
        "name": "prop__notes",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_VEHICLE_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__vehicle_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
    {
        "name": "prop__label",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
        "description": "Vehicle's fleet label.",
    },
]

_MEMBERSHIP_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__seat", "type": "VARCHAR"},
    {
        "name": "member__rider__kind",
        "type": "VARCHAR",
        "description": "Kind of the passenger riding this vehicle.",
    },
    {"name": "member__rider__id", "type": "VARCHAR"},
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
    """Write the documented fixture emit: location, vehicle (car/truck), passengers."""
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))

    conn.execute(_create_ddl("records__location", _LOCATION_COLUMNS))
    conn.execute(
        'INSERT INTO "records__location" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "loc1", 0, True, None, 0, 0, "Central Depot", 40, None],
    )

    conn.execute(_create_ddl("records__vehicle", _VEHICLE_COLUMNS))
    conn.execute(
        'INSERT INTO "records__vehicle" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "v1", 0, True, None, 0, 0, "car", "Shuttle-1"],
    )
    conn.execute(
        'INSERT INTO "records__vehicle" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "v2", 0, True, None, 0, 1, "truck", "Cargo-1"],
    )

    conn.execute(_create_ddl("membership__vehicle__passengers", _MEMBERSHIP_COLUMNS))
    conn.execute(
        'INSERT INTO "membership__vehicle__passengers" VALUES (?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "v1", 5, None, "1A", "location", "loc1"],
    )

    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "scenario_description": _SCENARIO,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "runtime": {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"},
        "record_roles": {"location": "dimension", "vehicle": "dimension"},
        "enum_domains": {
            "vehicle": {
                "vehicle_type": [
                    {"value": "car", "description": "A wheeled passenger vehicle."},
                    {"value": "truck", "description": "A cargo-carrying vehicle."},
                ]
            }
        },
        "tables": [
            {
                "name": "records__location",
                "category": "records",
                "record_kind": "location",
                "description": "Physical locations recorded during the simulation.",
                "rows": 1,
                "columns": _LOCATION_COLUMNS,
            },
            {
                "name": "records__vehicle",
                "category": "records",
                "record_kind": "vehicle",
                "description": "Vehicles operating during the simulation.",
                "rows": 2,
                "columns": _VEHICLE_COLUMNS,
            },
            {
                "name": "membership__vehicle__passengers",
                "category": "membership",
                "record_kind": "vehicle",
                "property": "passengers",
                "description": "Passengers riding each vehicle.",
                "rows": 1,
                "columns": _MEMBERSHIP_COLUMNS,
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


def _discard(_notice: Notice) -> None:
    """Discard a proposal/plan notice -- the demo is indifferent to them."""


def _uncomment_membership_alternative(content: str) -> str:
    """Turn streaming's fully-commented membership alternative into a live config.

    Strips exactly one leading '#' (and one following space, when present)
    from every line from `# content: membership-events` onward, and pairs it
    with the original `keys:` block.
    """
    marker = "# content: membership-events\n"
    alt_tail = content[content.index(marker) :]
    uncommented = "\n".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in alt_tail.splitlines()
    )
    keys_start = content.index("keys:")
    keys_end = content.index("\n# rebase:")
    keys_block = content[keys_start:keys_end]
    return f"{uncommented}\n\n{keys_block}"


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        emit_dir = Path(tmp)
        _build_emit(emit_dir)

        with open_emit(emit_dir) as emit:
            dimensional_yaml = generate_init_config(emit, _discard)
            source_yaml = generate_source_init_config(emit, _discard)
            stream_yaml = generate_stream_init_config(emit, _discard)

        print("=== Dimensional candidate config ===")
        print(dimensional_yaml)
        print("=== Source candidate config ===")
        print(source_yaml)
        print("=== Streaming candidate config ===")
        print(stream_yaml)

        # --- Dimensional: scenario, table description, property docs -----
        if "# Scenario:" not in dimensional_yaml or _SCENARIO not in (
            dimensional_yaml.replace("\n#   ", " ")
        ):
            ok = False
        if (
            "# Physical locations recorded during the simulation."
            not in dimensional_yaml
        ):
            ok = False
        if (
            dimensional_yaml.count("# Vehicles operating during the simulation.") != 2
        ):  # one per sub-type stub (car, truck)
            ok = False
        if (
            "from: prop__name}  # tracked -> per-version;"
            " versions/record unknown (no row_census in this emit);"
            " Human-readable location name." not in dimensional_yaml
        ):
            ok = False
        if (
            "from: prop__capacity}  # Maximum occupancy. (people)"
            not in dimensional_yaml
        ):
            ok = False
        if "        - {name: notes, from: prop__notes}\n" not in dimensional_yaml:
            ok = False  # prop__notes is undocumented -- no trailing comment

        # --- Source: scenario, table description, sub_types gloss --------
        if "# Scenario:" not in source_yaml:
            ok = False
        if "# Vehicles operating during the simulation." not in source_yaml:
            ok = False
        if "sub_types: [car]  # A wheeled passenger vehicle." not in source_yaml:
            ok = False
        if "sub_types: [truck]  # A cargo-carrying vehicle." not in source_yaml:
            ok = False

        # --- Streaming: scenario, table description, sub_types gloss,
        #     block-style properties with per-entry docs, membership field
        #     docs (reference field via member__rider__kind; undocumented
        #     'seat' carries no comment) -----------------------------------
        if "# Scenario:" not in stream_yaml:
            ok = False
        if "sub_types: [car]  # A wheeled passenger vehicle." not in stream_yaml:
            ok = False
        if (
            "        - name  # Human-readable location name.\n"
            "        - capacity  # Maximum occupancy. (people)\n"
            "        - notes\n" not in stream_yaml
        ):
            ok = False
        if "        - label  # Vehicle's fleet label.\n" not in stream_yaml:
            ok = False
        if "#   # Passengers riding each vehicle." not in stream_yaml:
            ok = False
        if (
            "#       - seat\n"
            "#       - rider  # Kind of the passenger riding this vehicle.\n"
            not in stream_yaml
        ):
            ok = False

        # --- Self-gate: every annotated config still parses and plans/streams
        with open_emit(emit_dir) as emit:
            dim_config = load_export_config(_write_tmp(emit_dir, dimensional_yaml))
            assert dim_config.dimensional is not None
            dim_election = resolve_election(emit.sidecar, dim_config.keys)
            for table_decl in dim_config.dimensional.tables:
                validate_table(
                    table_decl,
                    dim_config.dimensional,
                    emit.sidecar,
                    None,
                    _discard,
                    election=dim_election,
                )

            source_config = load_export_config(_write_tmp(emit_dir, source_yaml))
            source_anchor = resolve_effective_anchor(
                emit.sidecar.runtime(), source_config.rebase, None, None
            )
            source_election = resolve_election(emit.sidecar, source_config.keys)
            build_source_plan(
                emit, source_config, source_anchor, source_election, False, _discard
            )

            stream_config = load_stream_config(_write_tmp(emit_dir, stream_yaml))
            list(iter_stream_events(emit, stream_config, None, notice_sink=_discard))

            uncommented = _uncomment_membership_alternative(stream_yaml)
            uncommented_config = load_stream_config(_write_tmp(emit_dir, uncommented))
            list(
                iter_stream_events(emit, uncommented_config, None, notice_sink=_discard)
            )

    if not ok:
        print("\nFAILURE: one or more annotation checks did not hold")
        return 1

    print(
        "\nSUCCESS: all three engines annotated their proposals with the"
        " scenario, table-description, sub_types-gloss, and property-doc"
        " comments (including inside the commented membership alternative),"
        " undocumented items stayed comment-free, and every annotated"
        " config -- the uncommented alternative included -- still parses"
        " and plans/streams clean"
    )
    return 0


def _write_tmp(emit_dir: Path, content: str) -> Path:
    """Write a generated candidate config beside the emit and return its path."""
    path = emit_dir / "candidate.yaml"
    path.write_text(content, encoding="utf-8")
    return path


if __name__ == "__main__":
    raise SystemExit(main())
