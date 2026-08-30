"""Shared emit fixtures for companion writer tests (artifacts / manifest /
readme). `write_minimal_emit` covers the placement/field-set tests, which
read only sidecar identity facts (version, branch, runtime) -- never table
contents -- so one bare `fixed`-category table and an empty run.duckdb
suffice. `write_documented_emit` + `documented_actor_table_report` cover the
data-dictionary resolution tests, which need a records-category source with
every documentation shape the dictionary module resolves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import duckdb
from _support.sidecar_builder import identity_column, prop_column, write_emit

from fabulexa_forge.exporters.query_spec import (
    ColumnProvenance,
    KindValueEntry,
    TableReport,
)

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Mapping

_MINIMAL_TABLES: list[dict[str, object]] = [
    {
        "name": "clinic_settings",
        "category": "fixed",
        "rows": 1,
        "columns": [
            {"name": "setting_key", "type": "VARCHAR"},
            {"name": "setting_value", "type": "VARCHAR"},
        ],
    }
]


def write_minimal_emit(
    dest: "Path",
    *,
    branches: list[dict[str, object]] | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    """Write a minimal one-table emit (sidecar + empty run.duckdb) to `dest`.

    Args:
        dest: The emit directory; base.json and run.duckdb are written inside it.
        branches: Overrides the default single-trunk branch, when given.
        extra: Optional extra top-level sidecar blocks (e.g. `runtime`).
    """
    write_emit(dest, tables=_MINIMAL_TABLES, branches=branches, extra=extra)
    duckdb.connect(str(dest / "run.duckdb")).close()


# ---------------------------------------------------------------------------
# Documented fixture -- one records table per dictionary resolution rule
# ---------------------------------------------------------------------------

SCENARIO_DESCRIPTION = (
    "A hospital shift-handoff simulation, tracking staff duty status across care teams."
)
ACTOR_TABLE_DESCRIPTION = "Hospital staff members."
FULL_NAME_DESCRIPTION = "Staff member's full legal name."
STATUS_DESCRIPTION = "Current duty status."
SHIFT_MINUTES_DESCRIPTION = "Length of the current shift."


def _team_records_columns() -> list[dict[str, object]]:
    """`records__team`'s columns -- identical whether or not the emit is
    documented; the kind itself carries no `description` either way, so the
    dictionary's "label without prose" case needs no doc/no-doc variant."""
    return [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        prop_column(
            "prop__team_name",
            "VARCHAR",
            history_tracked=False,
            temporal_class="constant",
        ),
    ]


def _actor_records_columns(*, documented: bool) -> list[dict[str, object]]:
    """`records__actor`'s columns, name/type/reference shape identical either
    way -- only the description/unit attributes vary with `documented`."""
    return [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "presentation_id", "type": "VARCHAR"},
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        prop_column(
            "prop__full_name",
            "VARCHAR",
            history_tracked=False,
            temporal_class="constant",
            description=FULL_NAME_DESCRIPTION if documented else None,
        ),
        prop_column(
            "prop__status",
            "VARCHAR",
            history_tracked=False,
            temporal_class="constant",
            description=STATUS_DESCRIPTION if documented else None,
        ),
        prop_column(
            "prop__shift_minutes",
            "BIGINT",
            history_tracked=False,
            temporal_class="constant",
            description=SHIFT_MINUTES_DESCRIPTION if documented else None,
            unit="minutes" if documented else None,
        ),
        prop_column(
            "prop__team_id",
            "VARCHAR",
            history_tracked=False,
            temporal_class="constant",
            references="team",
        ),
        identity_column("ref_index__team_id", "BIGINT"),
    ]


_DOCUMENTED_ENUM_DOMAINS: dict[str, object] = {
    "actor": {
        "status": [
            {"value": "A", "description": "Active and on duty."},
            {"value": "I", "description": "Inactive; off duty."},
        ]
    }
}


def _history_columns() -> list[dict[str, object]]:
    """The fixed `history` table's contract-minimum columns -- present so the
    dictionary's `history`-sourced resolutions (`sim_time`, the virtual
    `lead_sim_time`) have a declared table to answer from; documentation is
    contract-pinned either way."""
    return [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "kind", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "property", "type": "VARCHAR"},
        {"name": "sim_time", "type": "BIGINT"},
        {"name": "value", "type": "VARCHAR"},
    ]


def write_documented_emit(dest: "Path", *, documented: bool = True) -> None:
    """Write a records-category emit exercising every dictionary resolution
    rule: a forwarded table description (`records__actor`), an undocumented
    kind (`records__team`, no `description`), a description-only property
    (`prop__full_name`), a description+unit property (`prop__shift_minutes`),
    a closed-domain property (`prop__status`), an undocumented property
    (`prop__team_id`), the fixed `history` table for the
    `sim_time`/`lead_sim_time` structural resolutions, a
    `membership__actor__team` table for the membership-family pinned
    strings, and a `presentation_id` column (with its mandatory
    `presentation_keys` entry) for the presentation pinned string.

    Args:
        dest: The emit directory; base.json and run.duckdb are written inside it.
        documented: False strips every description/unit/enum_domains/
            scenario_description value while keeping table/column names,
            types, and references identical -- the inertness fixture pair.
    """
    extra: dict[str, object] = {
        # Structural coherence, not documentation: a records table carrying
        # presentation_id must have a presentation_keys entry, documented
        # or not.
        "presentation_keys": {
            "actor": {
                "key": {
                    "unique_within": "branch",
                    "branch_stable": True,
                    "slice_stable": True,
                    "key_space": {"class": "uuid"},
                }
            }
        },
    }
    if documented:
        extra["scenario_description"] = SCENARIO_DESCRIPTION
        extra["enum_domains"] = _DOCUMENTED_ENUM_DOMAINS
    write_emit(
        dest,
        tables=[
            {
                "name": "records__team",
                "category": "records",
                "record_kind": "team",
                "rows": 1,
                "columns": _team_records_columns(),
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "rows": 1,
                **({"description": ACTOR_TABLE_DESCRIPTION} if documented else {}),
                "columns": _actor_records_columns(documented=documented),
            },
            {
                "name": "history",
                "category": "fixed",
                "rows": 0,
                "columns": _history_columns(),
            },
            {
                "name": "membership__actor__team",
                "category": "membership",
                "record_kind": "actor",
                "property": "team",
                "rows": 0,
                "columns": [
                    identity_column("fork_path", "VARCHAR"),
                    identity_column("record_id", "VARCHAR"),
                    {"name": "joined_sim_time", "type": "BIGINT"},
                    {"name": "left_sim_time", "type": "BIGINT"},
                ],
            },
        ],
        extra=extra,
    )
    duckdb.connect(str(dest / "run.duckdb")).close()


def documented_actor_table_report(
    *,
    table_name: str = "actor_state",
    row_count: int | None = 1,
    author_descriptions: Mapping[str, str] | None = None,
) -> TableReport:
    """One `actor_state` output table's report, faithfully carried from
    `write_documented_emit`'s `records__actor` (+ one `records__team` gloss).

    Column coverage, one per dictionary resolution rule:

    - `full_name`: description only.
    - `status`: description + closed-domain `enum_options`.
    - `shift_minutes`: description + non-`ns` unit (kept under any rendering).
    - `team_id`: carried, but undocumented (name/type only).
    - `created_sim_time`: structural `ns` unit, kept (still an integer render).
    - `created_at`: same structural source, `ns` unit dropped (a `TIMESTAMPTZ`
      render left the raw-nanosecond form behind), description kept.
    - `kind`: a kind-name-as-value column glossed by `kind_values` -- one
      label ("Actor") glossed from `records__actor`'s description, one
      ("Team") from `records__team`'s absent description.

    Args:
        table_name: The output table's name.
        row_count: The report's row count (None for a windowed invocation).
        author_descriptions: Per-column author overrides, keyed by output
            column name; empty when omitted.

    Returns:
        The `TableReport`.
    """
    source = "records__actor"
    return TableReport(
        name=table_name,
        columns=(
            ("full_name", "VARCHAR"),
            ("status", "VARCHAR"),
            ("shift_minutes", "DECIMAL(10,2)"),
            ("team_id", "VARCHAR"),
            ("created_sim_time", "BIGINT"),
            ("created_at", "TIMESTAMPTZ"),
            ("kind", "VARCHAR"),
        ),
        row_count=row_count,
        keys=None,
        provenance={
            "full_name": ColumnProvenance(source, "prop__full_name"),
            "status": ColumnProvenance(source, "prop__status"),
            "shift_minutes": ColumnProvenance(source, "prop__shift_minutes"),
            "team_id": ColumnProvenance(source, "prop__team_id"),
            "created_sim_time": ColumnProvenance(source, "created_sim_time"),
            "created_at": ColumnProvenance(source, "created_sim_time"),
        },
        kind_values={
            "kind": (
                KindValueEntry(label="Actor", source_kind="actor"),
                KindValueEntry(label="Team", source_kind="team"),
            )
        },
        author_descriptions=author_descriptions or {},
        author_table_description=None,
        event_log=False,
    )


def history_interval_table_report(
    *,
    table_name: str = "actor_status_interval",
    row_count: int | None = 1,
    author_descriptions: Mapping[str, str] | None = None,
) -> TableReport:
    """One history_interval-shaped output table's report: the [start, end)
    pair carried from `history`'s `sim_time` / virtual `lead_sim_time` --
    the interval-end description resolution's fixture. Kept separate from
    `documented_actor_table_report` so that report's provenance stays
    single-source (its table-description forwarding depends on it).

    Args:
        table_name: The output table's name.
        row_count: The report's row count (None for a windowed invocation).
        author_descriptions: Per-column author overrides, keyed by output
            column name; empty when omitted.

    Returns:
        The `TableReport`.
    """
    return TableReport(
        name=table_name,
        columns=(
            ("entered_at", "TIMESTAMP"),
            ("exited_at", "TIMESTAMP"),
        ),
        row_count=row_count,
        keys=None,
        provenance={
            "entered_at": ColumnProvenance("history", "sim_time"),
            "exited_at": ColumnProvenance("history", "lead_sim_time"),
        },
        kind_values={},
        author_descriptions=author_descriptions or {},
        author_table_description=None,
        event_log=False,
    )


def value_mapped_table_report(
    *,
    table_name: str = "actor_events",
    row_count: int | None = 1,
    author_descriptions: Mapping[str, str] | None = None,
) -> TableReport:
    """One output table whose `status` column is a `derived: value_map`
    carry of `prop__status`: the map renders `A` as `active` and omits `I`
    -- the post-map declared-values resolution's fixture.

    Args:
        table_name: The output table's name.
        row_count: The report's row count (None for a windowed invocation).
        author_descriptions: Per-column author overrides, keyed by output
            column name; empty when omitted.

    Returns:
        The `TableReport`.
    """
    return TableReport(
        name=table_name,
        columns=(("status", "VARCHAR"),),
        row_count=row_count,
        keys=None,
        provenance={
            "status": ColumnProvenance(
                "records__actor", "prop__status", value_map=(("A", "active"),)
            ),
        },
        kind_values={},
        author_descriptions=author_descriptions or {},
        author_table_description=None,
        event_log=False,
    )


def structural_identity_table_report(
    *,
    table_name: str = "actor_identity",
    row_count: int | None = 1,
    author_descriptions: Mapping[str, str] | None = None,
) -> TableReport:
    """One output table carrying every pinned identity surface whose contract
    string points at base-layer structure -- the export-rewrite fixture:

    - `event_id` <- `records__actor.record_id` (the "use record_index" advice)
    - `public_id` <- `records__actor.presentation_id` (the sidecar clause)
    - `owner_id` <- `membership__actor__team.record_id` (the table-name
      segment claim)
    - `changed_id` <- `history.record_id` (the records__<kind> join advice)

    Args:
        table_name: The output table's name.
        row_count: The report's row count (None for a windowed invocation).
        author_descriptions: Per-column author overrides, keyed by output
            column name; empty when omitted.

    Returns:
        The `TableReport`.
    """
    return TableReport(
        name=table_name,
        columns=(
            ("event_id", "VARCHAR"),
            ("public_id", "VARCHAR"),
            ("owner_id", "VARCHAR"),
            ("changed_id", "VARCHAR"),
        ),
        row_count=row_count,
        keys=None,
        provenance={
            "event_id": ColumnProvenance("records__actor", "record_id"),
            "public_id": ColumnProvenance("records__actor", "presentation_id"),
            "owner_id": ColumnProvenance("membership__actor__team", "record_id"),
            "changed_id": ColumnProvenance("history", "record_id"),
        },
        kind_values={},
        author_descriptions=author_descriptions or {},
        author_table_description=None,
        event_log=False,
    )
