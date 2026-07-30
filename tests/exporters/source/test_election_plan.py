"""Tests for source-mode key election at plan time: per-population
identity_surface stamping for split units, the unsplit-tracked identity
combination gate, edge gates for reference-annotated columns, the junction
owner column, and per junction member kind, rename resolution against the
elected/absorbed domain, and resolve_source_table_keys under a non-default
election.

Sidecars are built in-memory via Sidecar.from_raw (no DuckDB needed — plan
building reads only the sidecar); the election is resolved directly via
resolve_election and threaded through build_source_plan's `election` keyword,
mirroring the engine's own resolve-then-plan sequencing
(exporters/source/engine.py).
"""

from __future__ import annotations

import pytest
from _support.notices import discard_notice_sink

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import RenameEntry, SourceConfig
from fabulexa_forge.errors import (
    ElectionMixedIdentity,
    ElectionUnionUnsafe,
    SourceRenameUnresolved,
)
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.query_spec import TableKeys
from fabulexa_forge.exporters.source.plan import (
    build_source_plan,
    resolve_source_table_keys,
)
from fabulexa_forge.reader.sidecar import Sidecar

# ---------------------------------------------------------------------------
# Sidecar-building helpers
# ---------------------------------------------------------------------------


def _col(
    name: str,
    type_: str = "VARCHAR",
    history_tracked: bool | None = None,
    temporal_class: str | None = None,
    references: str | None = None,
) -> dict[str, object]:
    """Build a raw sidecar column entry."""
    col: dict[str, object] = {"name": name, "type": type_}
    if history_tracked is not None:
        col["history_tracked"] = history_tracked
    if temporal_class is not None:
        col["temporal_class"] = temporal_class
    if references is not None:
        col["references"] = references
    return col


def _discriminator_col(kind: str) -> dict[str, object]:
    """The exempt discriminator column for a sub-typed kind."""
    return _col(f"prop__{kind}_type", history_tracked=False, temporal_class="constant")


def _reference_col(name: str, target_kind: str) -> dict[str, object]:
    """Build a non-tracked (constant) reference-annotated prop__ column."""
    return _col(
        name, history_tracked=False, temporal_class="constant", references=target_kind
    )


def _records_table(
    kind: str,
    prop_cols: list[dict[str, object]],
    presentation_id: bool = False,
    rows: int = 1,
) -> dict[str, object]:
    """Build a raw records__<kind> table entry with the contract's structural prefix."""
    cols = [_col("fork_path"), _col("record_id")]
    if presentation_id:
        cols.append(_col("presentation_id"))
    cols += [
        _col("created_sim_time", "BIGINT"),
        _col("active", "BOOLEAN"),
        _col("deactivated_at", "BIGINT"),
        _col("last_mutation_sim_time", "BIGINT"),
    ]
    cols += prop_cols
    return {
        "name": f"records__{kind}",
        "category": "records",
        "record_kind": kind,
        "columns": cols,
        "rows": rows,
    }


def _membership_table(
    owner_kind: str,
    prop: str,
    extra_cols: list[dict[str, object]],
    rows: int = 1,
) -> dict[str, object]:
    """Build a raw membership__<owner_kind>__<prop> table entry."""
    cols = [
        _col("fork_path"),
        _col("record_id"),
        _col("joined_sim_time", "BIGINT"),
        _col("left_sim_time", "BIGINT"),
    ]
    cols += extra_cols
    return {
        "name": f"membership__{owner_kind}__{prop}",
        "category": "membership",
        "record_kind": owner_kind,
        "property": prop,
        "columns": cols,
        "rows": rows,
    }


def _history_table() -> dict[str, object]:
    """Build the fixed-category history table entry."""
    return {
        "name": "history",
        "category": "fixed",
        "columns": [
            _col("fork_path"),
            _col("kind"),
            _col("record_id"),
            _col("property"),
            _col("sim_time", "BIGINT"),
            _col("value"),
        ],
        "rows": 0,
    }


def _sidecar(
    tables: list[dict[str, object]],
    record_roles: dict[str, object] | None = None,
    enum_domains: dict[str, object] | None = None,
    presentation_keys: dict[str, object] | None = None,
) -> Sidecar:
    """Build a Sidecar directly from a raw base.json-shaped mapping."""
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "tables": tables,
    }
    if record_roles is not None:
        raw["record_roles"] = record_roles
    if enum_domains is not None:
        raw["enum_domains"] = enum_domains
    if presentation_keys is not None:
        raw["presentation_keys"] = presentation_keys
    return Sidecar.from_raw(raw)


def _raw_counter_key(prefix: str = "", width: int = 3) -> dict[str, object]:
    """A conformant counter-class raw partition_key (emit/false/false)."""
    return {
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
        "key_space": {"class": "counter", "prefix": prefix, "width": width},
    }


# ---------------------------------------------------------------------------
# Scenario sidecars
# ---------------------------------------------------------------------------


def _flat_location_sidecar(
    presentation_keys: dict[str, object] | None = None,
) -> Sidecar:
    """A flat, untracked 'location' kind (dimension role), for self-column
    election / rename / resolve_source_table_keys tests."""
    name_col = _col("prop__name", history_tracked=False, temporal_class="constant")
    location_table = _records_table("location", [name_col], presentation_id=True)
    return _sidecar(
        tables=[location_table, _history_table()],
        record_roles={"location": "dimension"},
        presentation_keys=presentation_keys,
    )


def _split_actor_sidecar(presentation_keys: dict[str, object] | None = None) -> Sidecar:
    """A split 'actor' kind (consultant/nurse), for per-population
    identity_surface stamping and split-unit resolve_source_table_keys tests."""
    actor_table = _records_table(
        "actor", [_discriminator_col("actor")], presentation_id=True
    )
    return _sidecar(
        tables=[actor_table, _history_table()],
        record_roles={"actor": {"consultant": "dimension", "nurse": "fact"}},
        enum_domains={"actor": {"actor_type": ["consultant", "nurse"]}},
        presentation_keys=presentation_keys,
    )


def _sub_typed_tracked_shift_sidecar(
    presentation_keys: dict[str, object] | None = None,
) -> Sidecar:
    """A tracked, sub-typed 'shift' kind (day/night domain) — tracked
    dominates role, so this kind is never split; for the unsplit identity
    combination gate and the changelog genre-eligibility test."""
    shift_table = _records_table(
        "shift",
        [
            _discriminator_col("shift"),
            _col("prop__status", history_tracked=True, temporal_class="tracked"),
        ],
        presentation_id=True,
    )
    return _sidecar(
        tables=[shift_table, _history_table()],
        record_roles={},
        enum_domains={"shift": {"shift_type": ["day", "night"]}},
        presentation_keys=presentation_keys,
    )


def _order_referencing_split_location_sidecar(
    presentation_keys: dict[str, object] | None = None,
) -> Sidecar:
    """`order` references a split 'location' kind (north/south domain); the
    edge gate runs over location's full declared domain independent of
    location's own (per-population, always-single-population) split tables."""
    order_table = _records_table(
        "order", [_reference_col("prop__location_id", "location")]
    )
    location_table = _records_table(
        "location", [_discriminator_col("location")], presentation_id=True
    )
    return _sidecar(
        tables=[order_table, location_table, _history_table()],
        record_roles={
            "order": "fact",
            "location": {"north": "dimension", "south": "dimension"},
        },
        enum_domains={"location": {"location_type": ["north", "south"]}},
        presentation_keys=presentation_keys,
    )


def _ghost_reference_sidecar() -> Sidecar:
    """`order` references a 'ghost' kind absent from the emit — the
    kind-exists gate's consequence: the edge carries no election, no gate."""
    order_table = _records_table("order", [_reference_col("prop__ghost_id", "ghost")])
    return _sidecar(
        tables=[order_table, _history_table()], record_roles={"order": "fact"}
    )


def _junction_sidecar(presentation_keys: dict[str, object] | None = None) -> Sidecar:
    """A junction owned by a split 'group' kind (alpha/beta), whose member
    field admits every known kind — including a split 'actor' kind
    (consultant/nurse) — for the owner-column and per-member-kind edge gate
    tests."""
    group_table = _records_table(
        "group", [_discriminator_col("group")], presentation_id=True
    )
    actor_table = _records_table(
        "actor", [_discriminator_col("actor")], presentation_id=True
    )
    membership = _membership_table(
        "group",
        "members",
        [_col("member__actor__kind"), _col("member__actor__id")],
    )
    return _sidecar(
        tables=[group_table, actor_table, membership, _history_table()],
        record_roles={
            "group": {"alpha": "dimension", "beta": "dimension"},
            "actor": {"consultant": "dimension", "nurse": "fact"},
        },
        enum_domains={
            "group": {"group_type": ["alpha", "beta"]},
            "actor": {"actor_type": ["consultant", "nurse"]},
        },
        presentation_keys=presentation_keys,
    )


_LOCATION_SPLIT_SAFE_KEYS: dict[str, object] = {
    "location": {
        "sub_types": {
            "north": _raw_counter_key("ALPHA_"),
            "south": _raw_counter_key("BETA_"),
        },
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    }
}

_LOCATION_SPLIT_UNSAFE_KEYS: dict[str, object] = {
    "location": {
        "sub_types": {
            "north": _raw_counter_key(""),
            "south": _raw_counter_key(""),
        },
        "branch_stable": False,
        "slice_stable": False,
    }
}

_SHIFT_KEYS: dict[str, object] = {
    "shift": {
        "sub_types": {"day": _raw_counter_key("DAY_")},
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    }
}

_SHIFT_FULL_SAFE_KEYS: dict[str, object] = {
    "shift": {
        "sub_types": {
            "day": _raw_counter_key("DAY_"),
            "night": _raw_counter_key("NIGHT_"),
        },
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    }
}

_ACTOR_CONSULTANT_KEYS: dict[str, object] = {
    "actor": {
        "sub_types": {"consultant": _raw_counter_key("CONS_")},
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    }
}

# `_junction_sidecar` stamps both 'group' and 'actor' with a presentation_id
# column unconditionally, so the sidecar's presentation_keys block — whenever
# non-None at all — must carry a kind-membership entry for *both*, even in a
# test that elects only one of them (the block's own kind-membership clause
# is a whole-sidecar coherence check, not scoped to the population under
# test). A single-sub_type entry suffices for kind membership on the kind
# that isn't itself under test.
_GROUP_SAFE_ENTRY: dict[str, object] = {
    "sub_types": {
        "alpha": _raw_counter_key("GA_"),
        "beta": _raw_counter_key("GB_"),
    },
    "unique_within": "emit",
    "branch_stable": False,
    "slice_stable": False,
}

_GROUP_UNSAFE_ENTRY: dict[str, object] = {
    "sub_types": {
        "alpha": _raw_counter_key(""),
        "beta": _raw_counter_key(""),
    },
    "branch_stable": False,
    "slice_stable": False,
}

_ACTOR_SAFE_ENTRY: dict[str, object] = {
    "sub_types": {
        "consultant": _raw_counter_key("CONS_"),
        "nurse": _raw_counter_key("NURSE_"),
    },
    "unique_within": "emit",
    "branch_stable": False,
    "slice_stable": False,
}

_ACTOR_UNSAFE_ENTRY: dict[str, object] = {
    "sub_types": {
        "consultant": _raw_counter_key(""),
        "nurse": _raw_counter_key(""),
    },
    "branch_stable": False,
    "slice_stable": False,
}

_GROUP_SAFE_KEYS: dict[str, object] = {
    "group": _GROUP_SAFE_ENTRY,
    "actor": _ACTOR_SAFE_ENTRY,
}
_GROUP_UNSAFE_KEYS: dict[str, object] = {
    "group": _GROUP_UNSAFE_ENTRY,
    "actor": _ACTOR_SAFE_ENTRY,
}
_MEMBER_ACTOR_SAFE_KEYS: dict[str, object] = {
    "group": _GROUP_SAFE_ENTRY,
    "actor": _ACTOR_SAFE_ENTRY,
}
_MEMBER_ACTOR_UNSAFE_KEYS: dict[str, object] = {
    "group": _GROUP_SAFE_ENTRY,
    "actor": _ACTOR_UNSAFE_ENTRY,
}


# ---------------------------------------------------------------------------
# identity_surface stamping: split units elect per population
# ---------------------------------------------------------------------------


def test_split_units_elect_per_population_stamp_own_identity_surface() -> None:
    """Each split unit (consultant/nurse) stamps its own identity_surface,
    independently — no combination gate needed (each is a single-population
    table)."""
    sidecar = _split_actor_sidecar(_ACTOR_CONSULTANT_KEYS)
    election = resolve_election(
        sidecar, {"actor": {"consultant": "presentation_id", "nurse": "record_index"}}
    )
    specs = build_source_plan(sidecar, None, discard_notice_sink, election=election)
    consultant_spec = next(s for s in specs if s.sub_type == "consultant")
    nurse_spec = next(s for s in specs if s.sub_type == "nurse")
    assert consultant_spec.identity_surface == "presentation_id"
    assert nurse_spec.identity_surface == "record_index"


def test_identity_surface_default_record_id_unsplit() -> None:
    """No keys block -> identity_surface defaults to record_id."""
    sidecar = _flat_location_sidecar()
    specs = build_source_plan(sidecar, None, discard_notice_sink)
    assert specs[0].identity_surface == "record_id"


# ---------------------------------------------------------------------------
# Identity combination gate: unsplit tracked sub-typed kind, mixed election
# ---------------------------------------------------------------------------


def test_unsplit_tracked_subtyped_mixed_election_raises_election_mixed_identity() -> (
    None
):
    """A tracked (never-split) sub-typed kind's populations electing
    differing surfaces raises ElectionMixedIdentity — one changelog table
    spans the whole domain."""
    sidecar = _sub_typed_tracked_shift_sidecar(_SHIFT_KEYS)
    election = resolve_election(
        sidecar, {"shift": {"day": "presentation_id", "night": "record_index"}}
    )
    with pytest.raises(ElectionMixedIdentity):
        build_source_plan(sidecar, None, discard_notice_sink, election=election)


def test_unsplit_tracked_subtyped_uniform_union_unsafe_raises() -> None:
    """A tracked sub-typed kind's uniform presentation_id election over a
    union-unsafe domain raises ElectionUnionUnsafe."""
    sidecar = _sub_typed_tracked_shift_sidecar(
        {
            "shift": {
                "sub_types": {
                    "day": _raw_counter_key(""),
                    "night": _raw_counter_key(""),
                },
                "branch_stable": False,
                "slice_stable": False,
            }
        }
    )
    election = resolve_election(sidecar, {"shift": "presentation_id"})
    with pytest.raises(ElectionUnionUnsafe):
        build_source_plan(sidecar, None, discard_notice_sink, election=election)


# ---------------------------------------------------------------------------
# Edge gate: reference-annotated prop__ column, over the target's full domain
# ---------------------------------------------------------------------------


def test_reference_edge_gate_runs_over_target_full_domain_raises_union_unsafe() -> None:
    """The reference-edge gate runs over location's full declared domain,
    independent of location's own per-population split tables."""
    sidecar = _order_referencing_split_location_sidecar(_LOCATION_SPLIT_UNSAFE_KEYS)
    election = resolve_election(sidecar, {"location": "presentation_id"})
    with pytest.raises(ElectionUnionUnsafe):
        build_source_plan(sidecar, None, discard_notice_sink, election=election)


def test_reference_edge_gate_passes_uniform_presentation_id_over_safe_domain() -> None:
    """The edge gate passes a uniform presentation_id election over a
    pairwise union-safe domain, resolving every population."""
    sidecar = _order_referencing_split_location_sidecar(_LOCATION_SPLIT_SAFE_KEYS)
    election = resolve_election(sidecar, {"location": "presentation_id"})
    specs = build_source_plan(sidecar, None, discard_notice_sink, election=election)
    order_spec = next(s for s in specs if s.source_table == "records__order")
    edge = order_spec.edge_surfaces[0]
    surfaces = {s for _, pop in edge.per_kind_populations for _, s in pop}
    assert surfaces == {"presentation_id"}
    assert edge.rendered_type == "VARCHAR"


def test_reference_to_absent_target_skips_gate_renders_verbatim() -> None:
    """A property whose target kind has no records table in the emit is
    skipped before any gate call — no edge entry, no error."""
    sidecar = _ghost_reference_sidecar()
    specs = build_source_plan(sidecar, None, discard_notice_sink)
    assert specs[0].edge_surfaces == ()


# ---------------------------------------------------------------------------
# Edge gates: junction owner column, per junction member kind
# ---------------------------------------------------------------------------


def test_junction_owner_edge_gate_raises_union_unsafe() -> None:
    """The junction owner column's gate runs over the owner kind's (group's)
    full domain."""
    sidecar = _junction_sidecar(_GROUP_UNSAFE_KEYS)
    election = resolve_election(sidecar, {"group": "presentation_id"})
    with pytest.raises(ElectionUnionUnsafe):
        build_source_plan(sidecar, None, discard_notice_sink, election=election)


def test_junction_owner_edge_gate_passes_resolves_owner_election() -> None:
    """The owner column's gate passes over a union-safe domain, resolving the
    owner kind's uniform election."""
    sidecar = _junction_sidecar(_GROUP_SAFE_KEYS)
    election = resolve_election(sidecar, {"group": "presentation_id"})
    specs = build_source_plan(sidecar, None, discard_notice_sink, election=election)
    junction_spec = next(s for s in specs if s.genre == "junction")
    owner_edge = next(
        e for e in junction_spec.edge_surfaces if e.source_column == "record_id"
    )
    surfaces = {s for _, pop in owner_edge.per_kind_populations for _, s in pop}
    assert surfaces == {"presentation_id"}


def test_junction_member_field_gates_each_known_kind_independently_raises() -> None:
    """The member field's gate runs over each admitted kind's own domain
    independently — a union-unsafe actor domain raises even though group's
    own election stays default."""
    sidecar = _junction_sidecar(_MEMBER_ACTOR_UNSAFE_KEYS)
    election = resolve_election(sidecar, {"actor": "presentation_id"})
    with pytest.raises(ElectionUnionUnsafe):
        build_source_plan(sidecar, None, discard_notice_sink, election=election)


def test_junction_member_field_mixed_election_over_safe_domain_resolves_per_kind() -> (
    None
):
    """The member field admits every known kind (group and actor); a mixed
    (per-sub-type) actor election, union-safe, resolves both admitted kinds'
    populations, always VARCHAR."""
    sidecar = _junction_sidecar(_MEMBER_ACTOR_SAFE_KEYS)
    election = resolve_election(
        sidecar, {"actor": {"consultant": "presentation_id", "nurse": "record_index"}}
    )
    specs = build_source_plan(sidecar, None, discard_notice_sink, election=election)
    junction_spec = next(s for s in specs if s.genre == "junction")
    member_edge = next(
        e for e in junction_spec.edge_surfaces if e.source_column != "record_id"
    )
    assert set(member_edge.target_kinds) == {"group", "actor"}
    actor_populations = dict(member_edge.per_kind_populations)["actor"]
    assert {s for _, s in actor_populations} == {"presentation_id", "record_index"}
    assert member_edge.rendered_type == "VARCHAR"


# ---------------------------------------------------------------------------
# Rename against the elected/absorbed domain
# ---------------------------------------------------------------------------


def test_rename_keyed_on_elected_surface_contract_name_renames_id_column() -> None:
    """rename keyed on 'presentation_id' renames the elected self identity
    column — the elected surface's own contract column name."""
    sidecar = _flat_location_sidecar({"location": {"key": _raw_counter_key("LOC_")}})
    config = SourceConfig(
        rename=[
            RenameEntry(table="records__location", columns={"presentation_id": "code"})
        ]
    )
    election = resolve_election(sidecar, {"location": "presentation_id"})
    specs = build_source_plan(sidecar, config, discard_notice_sink, election=election)
    assert dict(specs[0].columns)["presentation_id"] == "code"


def test_rename_keyed_on_absorbed_record_id_column_raises_unresolved() -> None:
    """rename keyed on record_id is unresolvable once presentation_id
    election absorbs it — 'record_id' is no longer a source key."""
    sidecar = _flat_location_sidecar({"location": {"key": _raw_counter_key("LOC_")}})
    config = SourceConfig(
        rename=[RenameEntry(table="records__location", columns={"record_id": "x"})]
    )
    election = resolve_election(sidecar, {"location": "presentation_id"})
    with pytest.raises(SourceRenameUnresolved):
        build_source_plan(sidecar, config, discard_notice_sink, election=election)


def test_rename_keyed_on_absorbed_record_id_under_record_index_raises_unresolved() -> (
    None
):
    """rename keyed on record_id is unresolvable once record_index election
    rewrites the source key to 'record_index'."""
    sidecar = _flat_location_sidecar()
    config = SourceConfig(
        rename=[RenameEntry(table="records__location", columns={"record_id": "x"})]
    )
    election = resolve_election(sidecar, {"location": "record_index"})
    with pytest.raises(SourceRenameUnresolved):
        build_source_plan(sidecar, config, discard_notice_sink, election=election)


# ---------------------------------------------------------------------------
# resolve_source_table_keys under a non-default election
# ---------------------------------------------------------------------------


def test_resolve_source_table_keys_pk_follows_renamed_elected_identity_column() -> None:
    """PK follows the elected identity column's (possibly renamed) output
    name."""
    sidecar = _flat_location_sidecar({"location": {"key": _raw_counter_key("LOC_")}})
    config = SourceConfig(
        rename=[
            RenameEntry(table="records__location", columns={"presentation_id": "code"})
        ]
    )
    election = resolve_election(sidecar, {"location": "presentation_id"})
    specs = build_source_plan(sidecar, config, discard_notice_sink, election=election)
    keys = resolve_source_table_keys(sidecar, specs[0], "changelog")
    assert keys.primary_key == ("code",)


def test_resolve_source_table_keys_no_doubled_unique_under_presentation_id_election() -> (
    None
):
    """No doubled UNIQUE declaration: presentation_id is already the PK."""
    sidecar = _flat_location_sidecar({"location": {"key": _raw_counter_key("LOC_")}})
    election = resolve_election(sidecar, {"location": "presentation_id"})
    specs = build_source_plan(sidecar, None, discard_notice_sink, election=election)
    keys = resolve_source_table_keys(sidecar, specs[0], "changelog")
    assert keys == TableKeys(primary_key=("id",), unique=())


def test_resolve_source_table_keys_standalone_presentation_id_unique_under_record_index() -> (
    None
):
    """A standalone (non-absorbed) presentation_id column under record_index
    election still carries its registry claim's UNIQUE."""
    sidecar = _flat_location_sidecar({"location": {"key": _raw_counter_key("LOC_")}})
    election = resolve_election(sidecar, {"location": "record_index"})
    specs = build_source_plan(sidecar, None, discard_notice_sink, election=election)
    keys = resolve_source_table_keys(sidecar, specs[0], "changelog")
    assert keys == TableKeys(primary_key=("id",), unique=(("presentation_id",),))


def test_resolve_source_table_keys_split_unit_pk_and_unique_per_subtype() -> None:
    """Split-unit PK follows each sub-type's own election; UNIQUE on
    presentation_id iff that sub-type's own registry entry is declared."""
    sidecar = _split_actor_sidecar(_ACTOR_CONSULTANT_KEYS)
    election = resolve_election(
        sidecar, {"actor": {"consultant": "record_index", "nurse": "record_id"}}
    )
    specs = build_source_plan(sidecar, None, discard_notice_sink, election=election)
    consultant_spec = next(s for s in specs if s.sub_type == "consultant")
    nurse_spec = next(s for s in specs if s.sub_type == "nurse")
    consultant_keys = resolve_source_table_keys(sidecar, consultant_spec, "changelog")
    nurse_keys = resolve_source_table_keys(sidecar, nurse_spec, "changelog")
    assert consultant_keys == TableKeys(
        primary_key=("id",), unique=(("presentation_id",),)
    )
    assert nurse_keys == TableKeys(primary_key=("id",), unique=())


def test_resolve_source_table_keys_changelog_cdc_still_none_under_election() -> None:
    """Genre eligibility unchanged: a change-log kind under CDC delivery
    still declares no keys, regardless of election."""
    sidecar = _sub_typed_tracked_shift_sidecar(_SHIFT_FULL_SAFE_KEYS)
    election = resolve_election(sidecar, {"shift": "presentation_id"})
    specs = build_source_plan(sidecar, None, discard_notice_sink, election=election)
    keys = resolve_source_table_keys(sidecar, specs[0], "changelog")
    assert keys is None


def test_resolve_source_table_keys_junction_still_none_under_election() -> None:
    """Genre eligibility unchanged: a junction still declares no keys,
    regardless of the owner kind's election."""
    sidecar = _junction_sidecar(_GROUP_SAFE_KEYS)
    election = resolve_election(sidecar, {"group": "presentation_id"})
    specs = build_source_plan(sidecar, None, discard_notice_sink, election=election)
    junction_spec = next(s for s in specs if s.genre == "junction")
    keys = resolve_source_table_keys(sidecar, junction_spec, "changelog")
    assert keys is None
