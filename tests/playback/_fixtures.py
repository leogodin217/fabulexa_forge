"""Shared sidecar-only fixture scaffold for playback package tests.

resolve_selection is sidecar-only (no data reads), so the fixture builds only
base.json (no run.duckdb, no DuckDB import) and returns the parsed Sidecar
directly — the lightest scaffold that still routes through the one sidecar
authority (`_support.sidecar_builder.write_emit`).

Scenario:
  - records__patient: sub-typed (doctor / nurse via enum_domains), discriminator
      column declared (prop__patient_type, exempt, constant); prop__name
      (constant), prop__status (tracked), prop__notes (slice_only, non-exempt).
  - records__widget: not sub-typed (no enum_domains entry); prop__label
      (constant), prop__count (tracked), prop__internal (slice_only, non-exempt).
  - records__drifted_patient: sub-typed via enum_domains, but its discriminator
      column is undeclared (a drifted tape); prop__name (constant).
  - records__device: not sub-typed; mints presentation_id; prop__serial
      (constant).
  - membership__patient__team: owner patient; elem__role (scalar), member__lead
      (reference).
  - membership__widget__tags: owner widget (not sub-typed); elem__tag (scalar).
  - membership__drifted_patient__team: owner drifted_patient (drifted
      discriminator); elem__role (scalar).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from _support.sidecar_builder import identity_column, prop_column, write_emit

from fabulexa_forge.reader.sidecar import Sidecar

if TYPE_CHECKING:
    from pathlib import Path

FORK_PATH = "trunk"

_LIFECYCLE_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
]

_PATIENT_COLS: list[dict[str, object]] = [
    *_LIFECYCLE_COLS,
    prop_column(
        "prop__patient_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
    ),
    prop_column(
        "prop__name", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__notes", "VARCHAR", history_tracked=False, temporal_class="slice_only"
    ),
]

_WIDGET_COLS: list[dict[str, object]] = [
    *_LIFECYCLE_COLS,
    prop_column(
        "prop__label", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__count", "BIGINT", history_tracked=True, temporal_class="tracked"
    ),
    prop_column(
        "prop__internal", "VARCHAR", history_tracked=False, temporal_class="slice_only"
    ),
]

_DRIFTED_PATIENT_COLS: list[dict[str, object]] = [
    *_LIFECYCLE_COLS,
    prop_column(
        "prop__name", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_DEVICE_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__serial", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_PATIENT_TEAM_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__role", "type": "VARCHAR"},
    {"name": "member__lead__kind", "type": "VARCHAR"},
    {"name": "member__lead__id", "type": "VARCHAR"},
]

_WIDGET_TAGS_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__tag", "type": "VARCHAR"},
]

_DRIFTED_PATIENT_TEAM_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__role", "type": "VARCHAR"},
]


def _table_spec(
    name: str,
    category: str,
    cols: list[dict[str, object]],
    *,
    record_kind: str | None = None,
    property_name: str | None = None,
) -> dict[str, object]:
    """Build one sidecar table entry."""
    spec: dict[str, object] = {
        "name": name,
        "category": category,
        "columns": cols,
        "rows": 0,
    }
    if record_kind is not None:
        spec["record_kind"] = record_kind
    if property_name is not None:
        spec["property"] = property_name
    return spec


def build_fixture_sidecar(tmp_path: "Path") -> Sidecar:
    """Build the phase-5 selection fixture's sidecar (no run.duckdb needed).

    Args:
        tmp_path: A pytest tmp_path directory to write base.json into.

    Returns:
        The parsed Sidecar over the scenario described in this module's
        docstring.
    """
    tables = [
        _table_spec(
            "records__patient", "records", _PATIENT_COLS, record_kind="patient"
        ),
        _table_spec("records__widget", "records", _WIDGET_COLS, record_kind="widget"),
        _table_spec(
            "records__drifted_patient",
            "records",
            _DRIFTED_PATIENT_COLS,
            record_kind="drifted_patient",
        ),
        _table_spec("records__device", "records", _DEVICE_COLS, record_kind="device"),
        _table_spec(
            "membership__patient__team",
            "membership",
            _PATIENT_TEAM_COLS,
            record_kind="patient",
            property_name="team",
        ),
        _table_spec(
            "membership__widget__tags",
            "membership",
            _WIDGET_TAGS_COLS,
            record_kind="widget",
            property_name="tags",
        ),
        _table_spec(
            "membership__drifted_patient__team",
            "membership",
            _DRIFTED_PATIENT_TEAM_COLS,
            record_kind="drifted_patient",
            property_name="team",
        ),
    ]
    write_emit(
        tmp_path,
        tables=tables,
        branches=[{"fork_path": FORK_PATH, "parent": None, "slice_at": 9999}],
        extra={
            "enum_domains": {
                "patient": {"patient_type": ["doctor", "nurse"]},
                "drifted_patient": {"drifted_patient_type": ["a", "b"]},
            }
        },
    )
    raw = json.loads((tmp_path / "base.json").read_text(encoding="utf-8"))
    return Sidecar.from_raw(raw)
