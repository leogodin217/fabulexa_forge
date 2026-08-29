"""Tests for `exporters.source.init.generate_source_init_config`.

Covers:
- One state table per population: name verbatim `<kind>` for a flat kind;
  one stub per declared sub-type (`<kind>_<sub_type>`, `sub_types:
  [<sub_type>]`) for a sub-typed kind -- `init`'s default split -- with a
  header comment naming the full domain and a commented combine-alternative
  after the last sub-type's stub.
- One junction per membership table, named `<K>_<p>` for a flat owner; one
  stub per declared sub-type (`<K>_<sub_type>_<p>`, `sub_types:
  [<sub_type>]`) for a sub-typed owner, with a commented combine-alternative
  after the last stub -- mirroring the state-table split.
- Membership event-source entries: one commented entry per declared
  sub-type, carrying `sub_types: [<sub_type>]`; uncommenting the full set
  still parses and plans clean.
- The `versions` events stub: one active source per tracked-property kind,
  membership sources and lifecycle-only kinds appended commented-out.
- No tracked property anywhere -> the events stub is fully commented, with
  the lifecycle-only note.
- A name collision (underscore-bearing identifiers) comments out the later
  proposal with a collision note; the emitted config still parses and plans
  clean.
- The `keys:` proposal: uniform `record_index` active for every population,
  with `record_id` always and `presentation_id` (only where registry-declared)
  offered as commented alternatives -- shape (scalar vs. per-sub-type map)
  follows whether >= 1 sub-type is declared, never the active values, and
  the retired degradation mechanism never emits `NOTE: ElectionUnionUnsafe`.
- Non-exempt `slice_only` columns are never proposed; one notice each.
- An emit predating `history_tracked` -> `SourceHistoryTrackedRequired`; an
  incoherent `presentation_keys` block -> `PresentationKeysInvalidError`.
- Round-trip: generated YAML -> `load_export_config` -> `build_source_plan`
  clean, over an emit spanning both flat and sub-typed kinds.
- Proposal order follows sidecar table declaration order; output
  deterministic across two runs.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink
from _support.sidecar_builder import (
    enum_options,
    identity_column,
    prop_column,
    write_emit,
)

from exporters._emit_fixtures import _create_ddl, _table_spec
from exporters.source._source_fixtures import _HISTORY_COLUMNS, build_source_test_emit
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.errors import SourceHistoryTrackedRequired
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.source.init import generate_source_init_config
from fabulexa_forge.exporters.source.plan import build_source_plan
from fabulexa_forge.reader import open_emit
from fabulexa_forge.reader.errors import PresentationKeysInvalidError

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _generate(emit_dir: Path) -> str:
    """Generate the candidate config, discarding notices."""
    with open_emit(emit_dir) as emit:
        return generate_source_init_config(emit, discard_notice_sink)


def _generate_recording_notices(emit_dir: Path) -> tuple[str, RecordingNoticeSink]:
    """Generate the candidate config, recording every notice."""
    sink = RecordingNoticeSink()
    with open_emit(emit_dir) as emit:
        content = generate_source_init_config(emit, sink)
    return content, sink


def _assert_round_trip_plans_clean(
    emit_dir: Path, content: str, tmp_path: Path
) -> None:
    """Load the generated YAML and build a source plan against it — must not raise."""
    cfg_path = tmp_path / "candidate.yaml"
    cfg_path.write_text(content, encoding="utf-8")
    config = load_export_config(cfg_path)
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(
            emit.sidecar.runtime(), config.rebase, None, None
        )
        election = resolve_election(emit.sidecar, config.keys)
        build_source_plan(emit, config, anchor, election, False, discard_notice_sink)


def _flat_records_emit(
    tmp_path: Path,
    kind: str,
    columns: list[dict[str, object]],
    row_values: list[object],
    extra: dict[str, object] | None = None,
) -> Path:
    """Build a single-kind, single-row source-mode emit."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir(parents=True, exist_ok=True)
    table_name = f"records__{kind}"
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_create_ddl(table_name, columns))
    placeholders = ", ".join(["?"] * len(row_values))
    conn.execute(f'INSERT INTO "{table_name}" VALUES ({placeholders})', row_values)
    conn.close()
    write_emit(
        emit_dir,
        tables=[_table_spec(table_name, "records", columns, 1, record_kind=kind)],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra=extra,
    )
    return emit_dir


_UNTRACKED_FLAT_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__name", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_UNTRACKED_FLAT_ROW: list[object] = ["trunk", "w1", 10, True, None, 10, 0, "Widget"]


# ---------------------------------------------------------------------------
# State tables: one per population -- split by default for a sub-typed kind,
# with a commented combine-alternative
# ---------------------------------------------------------------------------


def test_state_table_one_per_flat_kind_verbatim_name(tmp_path: Path) -> None:
    """A flat kind proposes exactly `- name: <kind>\\n  kind: <kind>`."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    build_source_test_emit(emit_dir)
    content = _generate(emit_dir)
    assert "    - name: location\n      kind: location\n" in content


def test_subtyped_kind_splits_per_subtype_with_combine_alternative(
    tmp_path: Path,
) -> None:
    """A sub-typed kind proposes one live stub per declared sub-type, header
    comment naming the full domain, with a commented combine-alternative
    after the last one."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    build_source_test_emit(emit_dir)
    content = _generate(emit_dir)
    assert (
        "    # kind 'actor' declares sub-types: consultant, nurse"
        " (one table per sub-type below)\n"
        "    - name: actor_consultant\n"
        "      kind: actor\n"
        "      sub_types: [consultant]\n" in content
    )
    assert (
        "    - name: actor_nurse\n"
        "      kind: actor\n"
        "      sub_types: [nurse]\n"
        "    # Combine alternative: one shared table across every declared"
        " sub-type instead of the per-sub-type split above (valid when the"
        " sub-types share an identical column set)\n"
        "    # - name: actor\n"
        "    #   kind: actor\n" in content
    )
    # The active proposals are the split stubs; the combine alternative never
    # uncomments a live '- name: actor' entry.
    assert "\n    - name: actor\n" not in content


# ---------------------------------------------------------------------------
# Junction tables: one per membership table, named <K>_<p>
# ---------------------------------------------------------------------------


def test_junction_named_owner_kind_property(tmp_path: Path) -> None:
    """A membership table proposes `- name: <K>_<p>\\n  membership: {...}`."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    build_source_test_emit(emit_dir)
    content = _generate(emit_dir)
    assert (
        "    - name: visit_team\n"
        "      membership: {kind: visit, property: team}\n" in content
    )


# ---------------------------------------------------------------------------
# Sub-typed owner membership estate (Phase 4)
# ---------------------------------------------------------------------------

_SUBTYPED_TRACKED_ACTOR_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__actor_type", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_SUBTYPED_TRACKED_ACTOR_ROW: list[object] = [
    "trunk",
    "a1",
    10,
    True,
    10,
    0,
    "consultant",
    "on-duty",
]

_OWNER_MEMBERSHIP_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__seat", "type": "VARCHAR"},
]


def _build_subtyped_owner_membership_emit(tmp_path: Path) -> Path:
    """A sub-typed `actor` owner (consultant/nurse, itself tracked, so the
    events block stays live) plus a `membership__actor__team` table it owns
    -- the sub-typed membership estate `init` splits per sub-type."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_create_ddl("records__actor", _SUBTYPED_TRACKED_ACTOR_COLUMNS))
    conn.execute(_create_ddl("membership__actor__team", _OWNER_MEMBERSHIP_COLUMNS))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        _SUBTYPED_TRACKED_ACTOR_ROW,
    )
    conn.execute(
        'INSERT INTO "membership__actor__team" VALUES (?, ?, ?, NULL, ?)',
        ["trunk", "a1", 10, "front"],
    )
    conn.close()
    write_emit(
        emit_dir,
        tables=[
            _table_spec(
                "records__actor",
                "records",
                _SUBTYPED_TRACKED_ACTOR_COLUMNS,
                1,
                record_kind="actor",
            ),
            _table_spec(
                "membership__actor__team",
                "membership",
                _OWNER_MEMBERSHIP_COLUMNS,
                1,
                record_kind="actor",
                property_name="team",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra={
            "enum_domains": {
                "actor": {"actor_type": enum_options("consultant", "nurse")}
            }
        },
    )
    return emit_dir


def test_subtyped_owner_junction_splits_per_subtype_with_combine_alternative(
    tmp_path: Path,
) -> None:
    """A sub-typed owner's membership table proposes one junction stub per
    declared sub-type, named `<K>_<sub_type>_<p>` with `sub_types:
    [<sub_type>]`; the last stub carries the commented combine-alternative."""
    emit_dir = _build_subtyped_owner_membership_emit(tmp_path)
    content = _generate(emit_dir)
    assert (
        "    - name: actor_consultant_team\n"
        "      membership: {kind: actor, property: team}\n"
        "      sub_types: [consultant]\n" in content
    )
    assert (
        "    - name: actor_nurse_team\n"
        "      membership: {kind: actor, property: team}\n"
        "      sub_types: [nurse]\n"
        "    # Combine alternative: one shared junction across every"
        " declared sub-type instead of the per-sub-type split above"
        " (valid when the sub-types share an identical column set)\n"
        "    # - name: actor_team\n"
        "    #   membership: {kind: actor, property: team}\n" in content
    )
    assert "\n    - name: actor_team\n" not in content


def test_membership_event_source_entries_carry_subtypes(tmp_path: Path) -> None:
    """Membership event-source entries: one commented entry per declared
    sub-type, carrying `sub_types: [<sub_type>]`."""
    emit_dir = _build_subtyped_owner_membership_emit(tmp_path)
    content = _generate(emit_dir)
    assert (
        "      # - membership: {kind: actor, property: team}\n"
        "      #   sub_types: [consultant]\n"
        "      # - membership: {kind: actor, property: team}\n"
        "      #   sub_types: [nurse]\n" in content
    )


def test_uncommenting_membership_event_sources_plans_clean(tmp_path: Path) -> None:
    """Uncommenting the full per-sub-type membership event-source set stays
    plan-clean: the entries share the default item-type `actor.team` under
    the extended sharing exception, and their both-declared disjoint
    `sub_types` satisfy the overlap gate."""
    emit_dir = _build_subtyped_owner_membership_emit(tmp_path)
    content = _generate(emit_dir)
    uncommented = content.replace("# - membership:", "- membership:").replace(
        "#   sub_types:", "  sub_types:"
    )
    _assert_round_trip_plans_clean(emit_dir, uncommented, tmp_path)


def test_no_where_ever_proposed(tmp_path: Path) -> None:
    """No `where` key is ever proposed anywhere in `init` output."""
    emit_dir = _build_subtyped_owner_membership_emit(tmp_path)
    content = _generate(emit_dir)
    assert "where" not in content


# ---------------------------------------------------------------------------
# Events stub
# ---------------------------------------------------------------------------


def test_events_stub_active_per_tracked_kind_rest_commented(tmp_path: Path) -> None:
    """`versions` carries one active source per tracked kind; lifecycle-only
    kinds and membership sources are appended commented-out."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    build_source_test_emit(emit_dir)
    content = _generate(emit_dir)
    assert "  events:\n    name: versions\n    sources:\n" in content
    assert "      - kind: visit\n" in content
    assert "      - kind: shift\n" in content
    assert (
        "      # - kind: location  # lifecycle-only: no tracked property\n" in content
    )
    assert "      # - kind: order  # lifecycle-only: no tracked property\n" in content
    assert "      # - kind: actor  # lifecycle-only: no tracked property\n" in content
    assert "      # - membership: {kind: visit, property: team}\n" in content


def test_no_tracked_property_fully_comments_events_stub(tmp_path: Path) -> None:
    """No kind carries a tracked property -> the whole `events:` block, name
    included, is commented out with the lifecycle-only note."""
    emit_dir = _flat_records_emit(
        tmp_path, "widget", _UNTRACKED_FLAT_COLUMNS, _UNTRACKED_FLAT_ROW
    )
    content = _generate(emit_dir)
    assert "  # events:  # this emit's declared history is lifecycle-only" in content
    assert "  #   name: versions\n" in content
    assert "  #   sources:\n" in content
    assert "  #     - kind: widget\n" in content
    assert "\n  events:\n" not in content


# ---------------------------------------------------------------------------
# Name collisions
# ---------------------------------------------------------------------------


def _build_collision_emit(tmp_path: Path) -> Path:
    """A records kind literally named `team_roster` declared before the
    `membership__team__roster` table it collides with (auto-derived name
    `team_roster`)."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    team_cols = _UNTRACKED_FLAT_COLUMNS
    roster_cols = _UNTRACKED_FLAT_COLUMNS
    membership_cols: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "joined_sim_time", "type": "BIGINT"},
        {"name": "left_sim_time", "type": "BIGINT"},
        {"name": "elem__seat", "type": "VARCHAR"},
    ]
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_create_ddl("records__team", team_cols))
    conn.execute(_create_ddl("records__team_roster", roster_cols))
    conn.execute(_create_ddl("membership__team__roster", membership_cols))
    conn.execute(
        'INSERT INTO "records__team" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "t1", 10, True, 10, 0, "Team A"],
    )
    conn.execute(
        'INSERT INTO "records__team_roster" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "r1", 10, True, 10, 0, "Roster"],
    )
    conn.execute(
        'INSERT INTO "membership__team__roster" VALUES (?, ?, ?, NULL, ?)',
        ["trunk", "t1", 10, "front"],
    )
    conn.close()
    write_emit(
        emit_dir,
        tables=[
            _table_spec("records__team", "records", team_cols, 1, record_kind="team"),
            _table_spec(
                "records__team_roster",
                "records",
                roster_cols,
                1,
                record_kind="team_roster",
            ),
            _table_spec(
                "membership__team__roster",
                "membership",
                membership_cols,
                1,
                record_kind="team",
                property_name="roster",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
    )
    return emit_dir


def test_name_collision_comments_out_later_proposal(tmp_path: Path) -> None:
    """The later `team_roster` proposal (the junction) is commented, with a
    collision note; the earlier `records__team_roster` state table stays live."""
    emit_dir = _build_collision_emit(tmp_path)
    content = _generate(emit_dir)
    assert "    - name: team_roster\n      kind: team_roster\n" in content
    assert (
        "    # NOTE: name 'team_roster' collides with an earlier proposal"
        " above; rename one before uncommenting\n"
        "    # - name: team_roster\n"
        "    #   membership: {kind: team, property: roster}\n" in content
    )
    _assert_round_trip_plans_clean(emit_dir, content, tmp_path)


def _build_subtyped_junction_collision_emit(tmp_path: Path) -> Path:
    """A flat kind literally named `actor_consultant_team` declared before the
    sub-typed `actor` owner's `membership__actor__team` table -- collides with
    the per-sub-type junction stub for the `consultant` sub-type (auto-derived
    name `actor_consultant_team`)."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    flat_cols = _UNTRACKED_FLAT_COLUMNS
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_create_ddl("records__actor_consultant_team", flat_cols))
    conn.execute(_create_ddl("records__actor", _SUBTYPED_TRACKED_ACTOR_COLUMNS))
    conn.execute(_create_ddl("membership__actor__team", _OWNER_MEMBERSHIP_COLUMNS))
    conn.execute(
        'INSERT INTO "records__actor_consultant_team" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "w1", 10, True, 10, 0, "Widget"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        _SUBTYPED_TRACKED_ACTOR_ROW,
    )
    conn.execute(
        'INSERT INTO "membership__actor__team" VALUES (?, ?, ?, NULL, ?)',
        ["trunk", "a1", 10, "front"],
    )
    conn.close()
    write_emit(
        emit_dir,
        tables=[
            _table_spec(
                "records__actor_consultant_team",
                "records",
                flat_cols,
                1,
                record_kind="actor_consultant_team",
            ),
            _table_spec(
                "records__actor",
                "records",
                _SUBTYPED_TRACKED_ACTOR_COLUMNS,
                1,
                record_kind="actor",
            ),
            _table_spec(
                "membership__actor__team",
                "membership",
                _OWNER_MEMBERSHIP_COLUMNS,
                1,
                record_kind="actor",
                property_name="team",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra={
            "enum_domains": {
                "actor": {"actor_type": enum_options("consultant", "nurse")}
            }
        },
    )
    return emit_dir


def test_subtyped_junction_name_collision_comments_out_proposal(
    tmp_path: Path,
) -> None:
    """A sub-typed owner's per-sub-type junction stub can itself collide with
    an earlier proposal; the commented fallback still carries the stub's own
    `sub_types` line."""
    emit_dir = _build_subtyped_junction_collision_emit(tmp_path)
    content = _generate(emit_dir)
    assert (
        "    - name: actor_consultant_team\n      kind: actor_consultant_team\n"
        in content
    )
    assert (
        "    # NOTE: name 'actor_consultant_team' collides with an earlier"
        " proposal above; rename one before uncommenting\n"
        "    # - name: actor_consultant_team\n"
        "    #   membership: {kind: actor, property: team}\n"
        "    #   sub_types: [consultant]\n" in content
    )
    _assert_round_trip_plans_clean(emit_dir, content, tmp_path)


# ---------------------------------------------------------------------------
# `keys:` proposal — uniform record_index active, resolvability alternatives
# ---------------------------------------------------------------------------


_SUBTYPED_ACTOR_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "presentation_id", "type": "BIGINT"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__actor_type", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__name", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]

_SUBTYPED_ACTOR_ROW: list[object] = [
    "trunk",
    "a1",
    1,
    10,
    True,
    None,
    10,
    0,
    "driver",
    "Alice",
]


def _build_subtyped_actor_emit(
    tmp_path: Path, sub_types: list[str], presentation_keys: dict[str, object] | None
) -> Path:
    """A sub-typed `actor` kind, with an optional `presentation_keys` block."""
    extra: dict[str, object] = {
        "enum_domains": {"actor": {"actor_type": enum_options(*sub_types)}},
    }
    if presentation_keys is not None:
        extra["presentation_keys"] = presentation_keys
    return _flat_records_emit(
        tmp_path, "actor", _SUBTYPED_ACTOR_COLUMNS, _SUBTYPED_ACTOR_ROW, extra=extra
    )


_ACTOR_PARTIAL_KEYS: dict[str, object] = {
    "actor": {
        "sub_types": {
            "driver": {
                "unique_within": "branch",
                "branch_stable": True,
                "slice_stable": True,
                "key_space": {"class": "record_index", "prefix": "DRV_", "width": 4},
            },
            "bus": {
                "unique_within": "branch",
                "branch_stable": True,
                "slice_stable": True,
                "key_space": {"class": "record_index", "prefix": "BUS_", "width": 4},
            },
        },
        "unique_within": "branch",
        "branch_stable": True,
        "slice_stable": True,
    }
}


def test_registry_partial_declaration_proposes_per_subtype_dict(tmp_path: Path) -> None:
    """`driver`/`bus` declared and `ride` undeclared: the uniform
    `record_index` active election still proposes a per-sub-type dict (shape
    follows the alternatives, not the active values); only driver/bus offer
    the `presentation_id` alternative."""
    emit_dir = _build_subtyped_actor_emit(
        tmp_path, ["driver", "bus", "ride"], _ACTOR_PARTIAL_KEYS
    )
    content = _generate(emit_dir)
    assert (
        "keys:\n"
        "  actor:\n"
        "    # NOTE: an uncommented alternative below SWAPS the active line"
        " for this population -- delete the active line, don't just uncomment\n"
        "    # driver: record_id\n"
        "    # driver: presentation_id\n"
        "    driver: record_index\n"
        "    # NOTE: an uncommented alternative below SWAPS the active line"
        " for this population -- delete the active line, don't just uncomment\n"
        "    # bus: record_id\n"
        "    # bus: presentation_id\n"
        "    bus: record_index\n"
        "    # NOTE: an uncommented alternative below SWAPS the active line"
        " for this population -- delete the active line, don't just uncomment\n"
        "    # ride: record_id\n"
        "    ride: record_index\n" in content
    )
    assert "# ride: presentation_id" not in content
    _assert_round_trip_plans_clean(emit_dir, content, tmp_path)


_ORDER_REFERENCING_ACTOR_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__actor_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="actor",
    ),
    identity_column("ref_index__actor_id", "BIGINT"),
]

#: `driver`/`bus` both declare a bare (empty-prefix) `counter` key space --
#: pairwise-unsafe under a uniform `presentation_id` election.
_ACTOR_BARE_COUNTER_KEYS: dict[str, object] = {
    "actor": {
        "sub_types": {
            "driver": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "", "width": 3},
            },
            "bus": {
                "unique_within": "emit",
                "branch_stable": False,
                "slice_stable": False,
                "key_space": {"class": "counter", "prefix": "", "width": 3},
            },
        },
        "branch_stable": False,
        "slice_stable": False,
    }
}


def _build_subtyped_actor_with_referencing_order_emit(tmp_path: Path) -> Path:
    """A sub-typed `actor` (driver/bus, bare/ambiguous counter prefixes) plus
    an `order` table referencing it -- the edge gate needs a referencing
    column to gate actor's election against, mirroring dimensional's
    `build_actor_with_referencing_booking_emit`."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_create_ddl("records__actor", _SUBTYPED_ACTOR_COLUMNS))
    conn.execute(_create_ddl("records__order", _ORDER_REFERENCING_ACTOR_COLUMNS))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a1", 1, 10, True, None, 10, 0, "driver", "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__order" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "o1", 10, True, None, 10, 0, "a1", 0],
    )
    conn.close()
    write_emit(
        emit_dir,
        tables=[
            _table_spec(
                "records__actor",
                "records",
                _SUBTYPED_ACTOR_COLUMNS,
                1,
                record_kind="actor",
            ),
            _table_spec(
                "records__order",
                "records",
                _ORDER_REFERENCING_ACTOR_COLUMNS,
                1,
                record_kind="order",
            ),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra={
            "enum_domains": {"actor": {"actor_type": enum_options("driver", "bus")}},
            "presentation_keys": _ACTOR_BARE_COUNTER_KEYS,
        },
    )
    return emit_dir


def test_referencing_column_proposes_clean_no_degradation(tmp_path: Path) -> None:
    """`order.prop__actor_id` references the sub-typed `actor` kind: driver
    and bus both declaring pairwise-unsafe bare counters proposes the uniform
    `record_index` map cleanly -- the degradation mechanism is retired, so no
    `NOTE: ElectionUnionUnsafe` comment appears anywhere in the proposal."""
    emit_dir = _build_subtyped_actor_with_referencing_order_emit(tmp_path)
    content = _generate(emit_dir)
    assert "ElectionUnionUnsafe" not in content
    assert (
        "keys:\n"
        "  actor:\n"
        "    # NOTE: an uncommented alternative below SWAPS the active line"
        " for this population -- delete the active line, don't just uncomment\n"
        "    # driver: record_id\n"
        "    # driver: presentation_id\n"
        "    driver: record_index\n" in content
    )
    _assert_round_trip_plans_clean(emit_dir, content, tmp_path)


_ACTOR_FULL_KEYS: dict[str, object] = {
    "actor": {
        "sub_types": {
            "driver": {
                "unique_within": "branch",
                "branch_stable": True,
                "slice_stable": True,
                "key_space": {"class": "record_index", "prefix": "DRV_", "width": 4},
            },
            "bus": {
                "unique_within": "branch",
                "branch_stable": True,
                "slice_stable": True,
                "key_space": {"class": "record_index", "prefix": "BUS_", "width": 4},
            },
        },
        "unique_within": "branch",
        "branch_stable": True,
        "slice_stable": True,
    }
}


def test_registry_full_declaration_still_proposes_per_subtype_map(
    tmp_path: Path,
) -> None:
    """Every declared sub-type still proposes the per-sub-type map -- shape
    never collapses to scalar once >= 1 sub-type is declared, independent of
    the `tables:` layout, which still splits one stub per sub-type."""
    emit_dir = _build_subtyped_actor_emit(tmp_path, ["driver", "bus"], _ACTOR_FULL_KEYS)
    content = _generate(emit_dir)
    assert "keys:\n  actor:\n" in content
    assert "keys:\n  actor: record_index\n" not in content
    for sub_type in ("driver", "bus"):
        assert f"    # {sub_type}: presentation_id\n    {sub_type}: record_index\n" in (
            content
        )
    assert (
        "    - name: actor_driver\n      kind: actor\n      sub_types: [driver]\n"
        in content
    )
    assert (
        "    - name: actor_bus\n      kind: actor\n      sub_types: [bus]\n" in content
    )
    _assert_round_trip_plans_clean(emit_dir, content, tmp_path)


def test_undeclared_registry_proposes_record_index(tmp_path: Path) -> None:
    """No `presentation_keys` block at all -> every kind proposes the
    `record_index` scalar, with only the record_id alternative commented."""
    emit_dir = _flat_records_emit(
        tmp_path, "widget", _UNTRACKED_FLAT_COLUMNS, _UNTRACKED_FLAT_ROW
    )
    content = _generate(emit_dir)
    assert "  # widget: record_id\n  widget: record_index\n" in content
    assert "# widget: presentation_id" not in content


# ---------------------------------------------------------------------------
# `init` documentation annotations (documentation-channel Phase 6)
# ---------------------------------------------------------------------------

#: A sub-typed, tracked `vehicle` kind (car/truck) that owns a
#: `passengers` membership table -- documented table descriptions on both,
#: a partially-glossed sub-type domain (car glossed, truck not).
_DOCUMENTED_VEHICLE_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__vehicle_type",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
    ),
    prop_column(
        "prop__status", "VARCHAR", history_tracked=True, temporal_class="tracked"
    ),
]

_DOCUMENTED_PASSENGERS_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "joined_sim_time", "type": "BIGINT"},
    {"name": "left_sim_time", "type": "BIGINT"},
    {"name": "elem__seat", "type": "VARCHAR"},
]


def _build_documented_source_emit(
    tmp_path: Path, *, scenario_description: str | None
) -> Path:
    """A documented, sub-typed `vehicle` owner with a `passengers` junction."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute(_create_ddl("records__vehicle", _DOCUMENTED_VEHICLE_COLUMNS))
    conn.execute(
        _create_ddl("membership__vehicle__passengers", _DOCUMENTED_PASSENGERS_COLUMNS)
    )
    conn.execute(_create_ddl("history", _HISTORY_COLUMNS))
    conn.execute(
        'INSERT INTO "records__vehicle" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
        ["trunk", "veh1", 10, True, 10, 0, "car", "idle"],
    )
    conn.execute(
        'INSERT INTO "membership__vehicle__passengers" VALUES (?, ?, ?, NULL, ?)',
        ["trunk", "veh1", 10, "1A"],
    )
    # Creation-seed history row (contract § history) for the tracked property.
    conn.execute(
        'INSERT INTO "history" VALUES (?, ?, ?, ?, ?, ?)',
        ["trunk", "vehicle", "veh1", "status", 10, "idle"],
    )
    conn.close()
    car_option, truck_option = enum_options("car", "truck")
    car_option["description"] = "A passenger car."
    vehicle_table = _table_spec(
        "records__vehicle",
        "records",
        _DOCUMENTED_VEHICLE_COLUMNS,
        1,
        record_kind="vehicle",
    )
    vehicle_table["description"] = "Vehicles operating in the fleet."
    passengers_table = _table_spec(
        "membership__vehicle__passengers",
        "membership",
        _DOCUMENTED_PASSENGERS_COLUMNS,
        1,
        record_kind="vehicle",
        property_name="passengers",
    )
    passengers_table["description"] = "Passengers riding each vehicle."
    extra: dict[str, object] = {
        "enum_domains": {"vehicle": {"vehicle_type": [car_option, truck_option]}}
    }
    if scenario_description is not None:
        extra["scenario_description"] = scenario_description
    write_emit(
        emit_dir,
        tables=[
            vehicle_table,
            passengers_table,
            _table_spec("history", "fixed", _HISTORY_COLUMNS, 1),
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra=extra,
    )
    return emit_dir


def test_scenario_comment_present_when_declared(tmp_path: Path) -> None:
    """A declared `scenario_description` renders as a `# Scenario:` block."""
    emit_dir = _build_documented_source_emit(
        tmp_path, scenario_description="A city fleet simulation."
    )
    content = _generate(emit_dir)
    assert "# Scenario:\n#   A city fleet simulation.\n" in content


def test_scenario_comment_absent_when_not_declared(tmp_path: Path) -> None:
    """No `scenario_description` -> no `# Scenario:` block anywhere."""
    emit_dir = _build_documented_source_emit(tmp_path, scenario_description=None)
    content = _generate(emit_dir)
    assert "# Scenario:" not in content


def test_table_description_annotates_state_and_junction_stubs(tmp_path: Path) -> None:
    """Each state/junction stub carries its source table's description --
    once per declared sub-type, plus once more on the trailing commented
    combine-alternative."""
    emit_dir = _build_documented_source_emit(tmp_path, scenario_description=None)
    content = _generate(emit_dir)
    assert content.count("    # Vehicles operating in the fleet.") == 3
    assert content.count("    # Passengers riding each vehicle.") == 3


def test_sub_type_gloss_on_state_and_junction_stubs(tmp_path: Path) -> None:
    """A glossed sub-type value's `sub_types:` line carries the gloss; an
    unglossed sub-type value's line carries none."""
    emit_dir = _build_documented_source_emit(tmp_path, scenario_description=None)
    content = _generate(emit_dir)
    assert "sub_types: [car]  # A passenger car.\n" in content
    assert "sub_types: [truck]\n" in content
    assert "sub_types: [truck]  #" not in content


def test_documented_proposal_plans_clean(tmp_path: Path) -> None:
    """The annotated candidate config still loads and plans clean."""
    emit_dir = _build_documented_source_emit(
        tmp_path, scenario_description="A city fleet simulation."
    )
    content = _generate(emit_dir)
    _assert_round_trip_plans_clean(emit_dir, content, tmp_path)


# ---------------------------------------------------------------------------
# `slice_only` columns
# ---------------------------------------------------------------------------


_SLICE_ONLY_COLUMNS: list[dict[str, object]] = [
    *_UNTRACKED_FLAT_COLUMNS,
    prop_column(
        "prop__region", "VARCHAR", history_tracked=False, temporal_class="slice_only"
    ),
]

_SLICE_ONLY_ROW: list[object] = [*_UNTRACKED_FLAT_ROW, "north"]


def test_slice_only_column_never_proposed_one_notice(tmp_path: Path) -> None:
    """A non-exempt `slice_only` column is never proposed; exactly one
    'slice-only-column-omitted' notice fires for it."""
    emit_dir = _flat_records_emit(
        tmp_path, "widget", _SLICE_ONLY_COLUMNS, _SLICE_ONLY_ROW
    )
    content, sink = _generate_recording_notices(emit_dir)
    assert "prop__region" not in content
    slice_only_notices = [
        n for n in sink.notices if n.code == "slice-only-column-omitted"
    ]
    assert len(slice_only_notices) == 1
    assert "prop__region" in slice_only_notices[0].message
    assert "widget" in slice_only_notices[0].message


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


_NO_HISTORY_TRACKED_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    {"name": "prop__name", "type": "VARCHAR"},
]

_NO_HISTORY_TRACKED_ROW: list[object] = ["trunk", "w1", 10, True, None, 10, 0, "Widget"]


def test_emit_predating_history_tracked_raises(tmp_path: Path) -> None:
    """No column anywhere in the emit declares `history_tracked` ->
    `SourceHistoryTrackedRequired`, before any proposal is written."""
    emit_dir = _flat_records_emit(
        tmp_path, "widget", _NO_HISTORY_TRACKED_COLUMNS, _NO_HISTORY_TRACKED_ROW
    )
    with pytest.raises(SourceHistoryTrackedRequired):
        _generate(emit_dir)


_LOCATION_KEYS_INCOHERENT: dict[str, object] = {
    "widget": {
        "key": {
            "unique_within": "branch",
            "branch_stable": True,
            "slice_stable": True,
            # 'counter' expects unique_within='emit', branch_stable=False,
            # slice_stable=False — disagrees with the declared scalars.
            "key_space": {"class": "counter", "prefix": "", "width": 3},
        }
    }
}


def test_incoherent_presentation_keys_raises(tmp_path: Path) -> None:
    """A present-but-incoherent `presentation_keys` block ->
    `PresentationKeysInvalidError`, from the sidecar's strict accessor."""
    emit_dir = _flat_records_emit(
        tmp_path,
        "widget",
        _UNTRACKED_FLAT_COLUMNS,
        _UNTRACKED_FLAT_ROW,
        extra={"presentation_keys": _LOCATION_KEYS_INCOHERENT},
    )
    with pytest.raises(PresentationKeysInvalidError):
        _generate(emit_dir)


# ---------------------------------------------------------------------------
# Round-trip, ordering, determinism
# ---------------------------------------------------------------------------


def test_round_trip_flat_and_subtyped_kinds_plan_clean(tmp_path: Path) -> None:
    """The full candidate — flat and sub-typed state tables, a junction, and
    the events stub — loads and plans clean."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    build_source_test_emit(emit_dir)
    content = _generate(emit_dir)
    _assert_round_trip_plans_clean(emit_dir, content, tmp_path)


def test_proposal_order_follows_sidecar_declaration_order(tmp_path: Path) -> None:
    """`tables:` entries appear in sidecar table-declaration order."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    build_source_test_emit(emit_dir)
    content = _generate(emit_dir)
    names_in_order = [
        line.strip()[len("- name: ") :]
        for line in content.splitlines()
        if line.strip().startswith("- name:")
    ]
    assert names_in_order == [
        "visit",
        "shift_day",
        "shift_night",
        "location",
        "order",
        "actor_consultant",
        "actor_nurse",
        "visit_team",
    ]


def test_output_deterministic_across_two_runs(tmp_path: Path) -> None:
    """Two independent generations of the same emit are byte-identical."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    build_source_test_emit(emit_dir)
    first = _generate(emit_dir)
    second = _generate(emit_dir)
    assert first == second
