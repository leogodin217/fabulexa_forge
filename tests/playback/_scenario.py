"""The shared full-scenario data-bearing emit for tier-1 snapshot/seek tests.

Two record kinds (patient, sub-typed doctor/nurse; widget, not sub-typed) +
two membership tables (membership__patient__team, membership__widget__tags),
spanning a cross-family tape with a coincident-instant boundary (p1's
membership 'leave' and record 'd' both at t=25):

  patient p1 (doctor): c@10, status u@15, d@25.
  patient p2 (nurse):  c@12.
  widget  w1:          c@8, count u@20.
  membership__patient__team: p1 join@10 leave@25 (role=lead);
                              p2 join@12 (role=member, no leave).
  membership__widget__tags:  w1 join@8 (tag=blue, no leave).

Shared by test_snapshot.py and test_consistency.py so both back the same
golden scenario.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from _support.sidecar_builder import enum_options, identity_column, prop_column

from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.playback.types import (
    MembershipAtomSelection,
    PlaybackSelection,
    RecordAtomSelection,
)

from ._data_fixtures import MembershipSpec, RecordSpec, build_data_emit

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

PATIENT_COLS: list[dict[str, object]] = [
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
]

WIDGET_COLS: list[dict[str, object]] = [
    *_LIFECYCLE_COLS,
    prop_column(
        "prop__label", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__count", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

TEAM_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__role", "type": "VARCHAR"},
]

TAGS_COLS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__tag", "type": "VARCHAR"},
]

ENUM_DOMAINS = {
    "enum_domains": {"patient": {"patient_type": enum_options("doctor", "nurse")}}
}


def make_anchor() -> EffectiveAnchor:
    """A fixed UTC anchor for _ts-rendering tests."""
    return EffectiveAnchor(
        start_instant=datetime(2026, 1, 1, tzinfo=timezone.utc),
        timezone=ZoneInfo("UTC"),
    )


def build_full_scenario(tmp_path: "Path") -> "Path":
    """Build the module-docstring scenario: two kinds, two membership tables."""
    patient_rows = [
        ("trunk", "p1", 10, False, 25, 25, 0, "doctor", "Alice", "checked-in"),
        ("trunk", "p2", 12, True, None, 12, 1, "nurse", "Bob", "waiting"),
    ]
    widget_rows = [
        ("trunk", "w1", 8, True, None, 20, 0, "Gadget", "2"),
    ]
    team_rows = [
        ("trunk", "p1", 10, 25, "lead"),
        ("trunk", "p2", 12, None, "member"),
    ]
    tags_rows = [
        ("trunk", "w1", 8, None, "blue"),
    ]
    history_rows = [
        ("trunk", "patient", "p1", "status", 10, "waiting"),
        ("trunk", "patient", "p1", "status", 15, "checked-in"),
        ("trunk", "patient", "p2", "status", 12, "waiting"),
        ("trunk", "widget", "w1", "count", 8, "1"),
        ("trunk", "widget", "w1", "count", 20, "2"),
    ]
    return build_data_emit(
        tmp_path,
        records=[
            RecordSpec("patient", PATIENT_COLS, patient_rows),
            RecordSpec("widget", WIDGET_COLS, widget_rows),
        ],
        memberships=[
            MembershipSpec("patient", "team", TEAM_COLS, team_rows),
            MembershipSpec("widget", "tags", TAGS_COLS, tags_rows),
        ],
        history_rows=history_rows,
        extra=ENUM_DOMAINS,
    )


def full_selection() -> PlaybackSelection:
    """Select every atom of the full scenario, full properties/fields."""
    return PlaybackSelection(
        records=(
            RecordAtomSelection("patient", (), None, None),
            RecordAtomSelection("widget", (), None, None),
        ),
        memberships=(
            MembershipAtomSelection("patient", (), "team", None, None),
            MembershipAtomSelection("widget", (), "tags", None, None),
        ),
    )
