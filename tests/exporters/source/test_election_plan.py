"""Tests for source-mode key election gates at plan time
(`exporters/source/plan.py`): the uniformity gate per declared table, the
declared-tables resolution of the old genre trichotomy (a mixed election
over a sub-typed kind now simply splits into per-population `tables[]`
entries), union safety under a uniform `presentation_id` election, the edge
gates per referencing column (reference-annotated `prop__` column, junction
owner column, junction member field), the events log's item-type gate over
the union of its sources' populations (never across item-types), rename
resolution against the elected/absorbed identity surface, and the plan-time
elected-key uniqueness guard.

`build_source_plan` now takes the open `Emit`, so every test resolves an
`EffectiveAnchor` and an `Election` first, mirroring the engine's own
resolve-then-plan sequencing (`exporters/source/engine.py`). Gate-only cases
(no data-dependent guard reached) use a bespoke bare emit — an empty
`run.duckdb` plus a hand-built `base.json` (`_support.sidecar_builder.write_emit`)
— since the gates that raise `ElectionMixedIdentity` / `ElectionUnionUnsafe`
run before the plan-time guard ever queries data. The guard-catches-a-corruption
case reuses `_source_fixtures.build_source_election_emit`, whose
`corrupt_device=True` variant carries real duplicated data.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column, prop_column, write_emit

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    ExportConfig,
    MembershipRef,
    SourceConfig,
    SourceEventsDecl,
    SourceEventSourceDecl,
    SourceTableDecl,
)
from fabulexa_forge.errors import (
    ElectedKeyDuplicate,
    ElectionMixedIdentity,
    ElectionUnionUnsafe,
    SourceColumnUnresolved,
)
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.populations import Population
from fabulexa_forge.exporters.source.plan import (
    SourceJunctionTablePlan,
    SourceStateTablePlan,
    build_source_plan,
)
from fabulexa_forge.reader.emit import open_emit

from ._source_fixtures import build_source_election_emit

# ---------------------------------------------------------------------------
# Config / plan-build helpers
# ---------------------------------------------------------------------------


def _config(
    tables: "tuple[SourceTableDecl, ...]" = (),
    events: "SourceEventsDecl | None" = None,
) -> ExportConfig:
    """Build a `mode: source` ExportConfig from a declared table/events set."""
    return ExportConfig(
        mode="source", source=SourceConfig(tables=tables, events=events)
    )


def _open_plan(
    emit_dir: Path,
    config: ExportConfig,
    keys: "dict[str, object] | None" = None,
):
    """Open `emit_dir` and build a SourcePlan, resolving the anchor and
    election the way the engine does."""
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None, "every fixture here declares a runtime block"
        election = resolve_election(emit.sidecar, keys)
        return build_source_plan(
            emit, config, anchor, election, False, discard_notice_sink
        )


# ---------------------------------------------------------------------------
# Bespoke bare emit: sub-typed 'device', referencing 'order', flat 'team'
# ---------------------------------------------------------------------------

_RUNTIME_EXTRA: "dict[str, object]" = {
    "runtime": {"timezone": "UTC", "start_datetime": "2024-01-01T00:00:00+00:00"}
}
_DEVICE_ENUM_DOMAIN: "dict[str, object]" = {
    "enum_domains": {"device": {"device_type": ["day", "night"]}}
}
_LIFECYCLE_COLUMNS: "list[dict[str, object]]" = [
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
]


def _col_ddl(col: "dict[str, object]") -> str:
    """Build a single column DDL fragment (name + type only)."""
    return f'"{col["name"]}" {col["type"]}'


def _write_bare_emit(
    tmp_path: Path,
    tables: "list[dict[str, object]]",
    *,
    extra: "dict[str, object] | None" = None,
) -> Path:
    """Write a minimal bare emit: a `run.duckdb` carrying every declared
    table, empty (0 rows) — a zero-row table always passes the plan-time
    guard trivially, so a gate that resolves rather than raises can still
    build the plan to completion — plus a hand-built `base.json`."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    for table in tables:
        columns = table["columns"]
        assert isinstance(columns, list)
        col_fragments = ", ".join(_col_ddl(c) for c in columns)
        conn.execute(f'CREATE TABLE "{table["name"]}" ({col_fragments})')
    conn.close()
    merged_extra = dict(_RUNTIME_EXTRA)
    if extra is not None:
        merged_extra.update(extra)
    write_emit(tmp_path, tables=tables, extra=merged_extra)
    return tmp_path


def _device_table() -> "dict[str, object]":
    """A sub-typed (day/night) 'device' records table, presentation_id-carrying."""
    columns = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "presentation_id", "type": "VARCHAR"},
        *_LIFECYCLE_COLUMNS,
        identity_column("record_index", "BIGINT"),
        prop_column(
            "prop__device_type",
            "VARCHAR",
            history_tracked=False,
            temporal_class="constant",
        ),
    ]
    return {
        "name": "records__device",
        "category": "records",
        "record_kind": "device",
        "columns": columns,
        "rows": 0,
    }


def _order_ref_device_table() -> "dict[str, object]":
    """A flat 'order' records table carrying a reference-annotated
    `prop__device_id` column targeting 'device'."""
    columns = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        *_LIFECYCLE_COLUMNS,
        identity_column("record_index", "BIGINT"),
        prop_column(
            "prop__device_id",
            "VARCHAR",
            history_tracked=False,
            temporal_class="constant",
            references="device",
        ),
        identity_column("ref_index__device_id", "BIGINT"),
    ]
    return {
        "name": "records__order",
        "category": "records",
        "record_kind": "order",
        "columns": columns,
        "rows": 0,
    }


def _team_table() -> "dict[str, object]":
    """A flat 'team' records table, no discriminator, no presentation_id."""
    columns = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        *_LIFECYCLE_COLUMNS,
        identity_column("record_index", "BIGINT"),
    ]
    return {
        "name": "records__team",
        "category": "records",
        "record_kind": "team",
        "columns": columns,
        "rows": 0,
    }


def _watchers_membership_table(owner_kind: str) -> "dict[str, object]":
    """A `membership__<owner_kind>__watchers` table whose member field
    (`party`) admits every known kind."""
    columns = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "joined_sim_time", "type": "BIGINT"},
        {"name": "left_sim_time", "type": "BIGINT"},
        {"name": "member__party__kind", "type": "VARCHAR"},
        {"name": "member__party__id", "type": "VARCHAR"},
    ]
    return {
        "name": f"membership__{owner_kind}__watchers",
        "category": "membership",
        "record_kind": owner_kind,
        "property": "watchers",
        "columns": columns,
        "rows": 0,
    }


def _device_partition(prefix: str) -> "dict[str, object]":
    """A conformant counter-class raw partition_key entry."""
    return {
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
        "key_space": {"class": "counter", "prefix": prefix, "width": 3},
    }


_DEVICE_SAFE_KEYS: "dict[str, object]" = {
    "presentation_keys": {
        "device": {
            "sub_types": {
                "day": _device_partition("DAY_"),
                "night": _device_partition("NIGHT_"),
            },
            "unique_within": "emit",
            "branch_stable": False,
            "slice_stable": False,
        }
    }
}

_DEVICE_UNSAFE_KEYS: "dict[str, object]" = {
    "presentation_keys": {
        "device": {
            "sub_types": {"day": _device_partition(""), "night": _device_partition("")},
            "branch_stable": False,
            "slice_stable": False,
        }
    }
}


def _device_only_emit(tmp_path: Path, *, safe: bool) -> Path:
    """A bare emit carrying only the sub-typed 'device' kind."""
    extra = dict(_DEVICE_ENUM_DOMAIN)
    extra.update(_DEVICE_SAFE_KEYS if safe else _DEVICE_UNSAFE_KEYS)
    return _write_bare_emit(tmp_path, [_device_table()], extra=extra)


def _device_order_emit(tmp_path: Path, *, safe: bool) -> Path:
    """A bare emit: sub-typed 'device' plus 'order' referencing it."""
    extra = dict(_DEVICE_ENUM_DOMAIN)
    extra.update(_DEVICE_SAFE_KEYS if safe else _DEVICE_UNSAFE_KEYS)
    return _write_bare_emit(
        tmp_path, [_device_table(), _order_ref_device_table()], extra=extra
    )


def _device_team_emit(tmp_path: Path, *, safe: bool) -> Path:
    """A bare emit: sub-typed 'device' plus an unrelated flat 'team' kind
    (no reference between them) — for item-type scoping tests, isolated
    from the reference-edge gate `order.device_id` would also trigger."""
    extra = dict(_DEVICE_ENUM_DOMAIN)
    extra.update(_DEVICE_SAFE_KEYS if safe else _DEVICE_UNSAFE_KEYS)
    return _write_bare_emit(tmp_path, [_device_table(), _team_table()], extra=extra)


def _device_owned_junction_emit(tmp_path: Path, *, safe: bool) -> Path:
    """A bare emit: sub-typed 'device' owning `membership__device__watchers`."""
    extra = dict(_DEVICE_ENUM_DOMAIN)
    extra.update(_DEVICE_SAFE_KEYS if safe else _DEVICE_UNSAFE_KEYS)
    return _write_bare_emit(
        tmp_path,
        [_device_table(), _watchers_membership_table("device")],
        extra=extra,
    )


def _team_owned_junction_with_device_member_emit(tmp_path: Path, *, safe: bool) -> Path:
    """A bare emit: flat 'team' owning `membership__team__watchers`, whose
    member field admits both 'team' and the sub-typed 'device' kind."""
    extra = dict(_DEVICE_ENUM_DOMAIN)
    extra.update(_DEVICE_SAFE_KEYS if safe else _DEVICE_UNSAFE_KEYS)
    return _write_bare_emit(
        tmp_path,
        [_team_table(), _device_table(), _watchers_membership_table("team")],
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Uniformity gate per declared table + the resolved trichotomy trap
# ---------------------------------------------------------------------------


def test_single_table_mixed_election_raises_election_mixed_identity(
    tmp_path: Path,
) -> None:
    """One `tables[]` entry spanning both device populations, mixed election
    (day -> presentation_id, night -> record_index): the uniformity gate
    fires — one table demands one identity surface."""
    emit_dir = build_source_election_emit(tmp_path)
    config = _config(tables=(SourceTableDecl(name="device", kind="device"),))
    with pytest.raises(ElectionMixedIdentity):
        _open_plan(
            emit_dir,
            config,
            keys={"device": {"day": "presentation_id", "night": "record_index"}},
        )


def test_mixed_election_kind_splits_into_per_population_tables_and_exports(
    tmp_path: Path,
) -> None:
    """The same mixed election over two per-population `tables[]` entries
    resolves cleanly — the trichotomy trap the old genre-fixed model forced
    (one changelog table demanding one surface) is gone: each entry is a
    single-population table, so no combination gate applies at all."""
    emit_dir = build_source_election_emit(tmp_path)
    config = _config(
        tables=(
            SourceTableDecl(name="device_day", kind="device", sub_types=("day",)),
            SourceTableDecl(name="device_night", kind="device", sub_types=("night",)),
        )
    )
    plan = _open_plan(
        emit_dir,
        config,
        keys={"device": {"day": "presentation_id", "night": "record_index"}},
    )
    day_table = next(t for t in plan.tables if t.name == "device_day")
    night_table = next(t for t in plan.tables if t.name == "device_night")
    assert isinstance(day_table, SourceStateTablePlan)
    assert isinstance(night_table, SourceStateTablePlan)
    assert day_table.identity_surface == "presentation_id"
    assert night_table.identity_surface == "record_index"


# ---------------------------------------------------------------------------
# Union safety under a uniform presentation_id election
# ---------------------------------------------------------------------------


def test_uniform_presentation_id_over_safe_domain_resolves(tmp_path: Path) -> None:
    """A uniform presentation_id election over a pairwise union-safe domain
    (device's DAY_/NIGHT_ prefixes) passes the identity gate."""
    emit_dir = build_source_election_emit(tmp_path)
    config = _config(tables=(SourceTableDecl(name="device", kind="device"),))
    plan = _open_plan(emit_dir, config, keys={"device": "presentation_id"})
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert table.identity_surface == "presentation_id"


def test_uniform_presentation_id_over_unsafe_domain_raises_union_unsafe(
    tmp_path: Path,
) -> None:
    """A uniform presentation_id election over a pairwise union-unsafe
    domain (both sub-types sharing the bare-counter prefix) raises
    ElectionUnionUnsafe."""
    emit_dir = _device_only_emit(tmp_path, safe=False)
    config = _config(tables=(SourceTableDecl(name="device", kind="device"),))
    with pytest.raises(ElectionUnionUnsafe):
        _open_plan(emit_dir, config, keys={"device": "presentation_id"})


# ---------------------------------------------------------------------------
# Edge gates: reference-annotated prop__ column
# ---------------------------------------------------------------------------


def test_reference_edge_gate_passes_uniform_presentation_id_over_safe_domain(
    tmp_path: Path,
) -> None:
    """order.device_id's edge gate runs over device's full declared domain
    and resolves every population under a safe uniform election."""
    emit_dir = build_source_election_emit(tmp_path)
    config = _config(tables=(SourceTableDecl(name="orders", kind="order"),))
    plan = _open_plan(emit_dir, config, keys={"device": "presentation_id"})
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    edge = table.edge_surfaces[0]
    surfaces = {s for _, pop in edge.per_kind_populations for _, s in pop}
    assert surfaces == {"presentation_id"}
    assert edge.rendered_type == "VARCHAR"


def test_reference_edge_gate_over_unsafe_domain_raises_union_unsafe(
    tmp_path: Path,
) -> None:
    """order.device_id's edge gate runs over device's full declared domain,
    independent of order's own (single-population, always-uniform)
    election — a union-unsafe target domain raises."""
    emit_dir = _device_order_emit(tmp_path, safe=False)
    config = _config(tables=(SourceTableDecl(name="orders", kind="order"),))
    with pytest.raises(ElectionUnionUnsafe):
        _open_plan(emit_dir, config, keys={"device": "presentation_id"})


# ---------------------------------------------------------------------------
# Edge gates: junction owner column, per junction member kind
# ---------------------------------------------------------------------------


def test_junction_owner_edge_gate_passes_resolves_owner_election(
    tmp_path: Path,
) -> None:
    """The junction owner column's gate runs over the owner kind's (device's)
    full domain and resolves under a safe uniform election."""
    emit_dir = _device_owned_junction_emit(tmp_path, safe=True)
    config = _config(
        tables=(
            SourceTableDecl(
                name="watchers",
                membership=MembershipRef(kind="device", property="watchers"),
            ),
        )
    )
    plan = _open_plan(emit_dir, config, keys={"device": "presentation_id"})
    table = plan.tables[0]
    assert isinstance(table, SourceJunctionTablePlan)
    owner_edge = next(e for e in table.edge_surfaces if e.source_column == "record_id")
    surfaces = {s for _, pop in owner_edge.per_kind_populations for _, s in pop}
    assert surfaces == {"presentation_id"}


def test_junction_owner_edge_gate_raises_union_unsafe(tmp_path: Path) -> None:
    """The junction owner column's gate raises over an owner kind's
    union-unsafe domain."""
    emit_dir = _device_owned_junction_emit(tmp_path, safe=False)
    config = _config(
        tables=(
            SourceTableDecl(
                name="watchers",
                membership=MembershipRef(kind="device", property="watchers"),
            ),
        )
    )
    with pytest.raises(ElectionUnionUnsafe):
        _open_plan(emit_dir, config, keys={"device": "presentation_id"})


def test_junction_owner_edge_unrestricted_mixed_election_falls_back_to_varchar(
    tmp_path: Path,
) -> None:
    """Without `sub_types`, a junction's owner column ranges over the owner
    kind's full domain: a mixed election (day -> presentation_id, night ->
    record_index) falls back to VARCHAR, as any mixed-column edge does."""
    emit_dir = _device_owned_junction_emit(tmp_path, safe=True)
    config = _config(
        tables=(
            SourceTableDecl(
                name="watchers",
                membership=MembershipRef(kind="device", property="watchers"),
            ),
        )
    )
    plan = _open_plan(
        emit_dir,
        config,
        keys={"device": {"day": "presentation_id", "night": "record_index"}},
    )
    table = plan.tables[0]
    assert isinstance(table, SourceJunctionTablePlan)
    assert table.owner_populations == (
        Population(kind="device", sub_type="day"),
        Population(kind="device", sub_type="night"),
    )
    owner_edge = next(e for e in table.edge_surfaces if e.source_column == "record_id")
    assert owner_edge.rendered_type == "VARCHAR"


def test_junction_owner_edge_narrowed_by_sub_types_resolves_agreed_type(
    tmp_path: Path,
) -> None:
    """A junction narrowed to one sub-type via `sub_types` types its owner
    column by that population's own election (doc § The parent lookup) — the
    same mixed election that falls back to VARCHAR unrestricted resolves
    BIGINT once narrowed to the record_index-electing population alone."""
    emit_dir = _device_owned_junction_emit(tmp_path, safe=True)
    config = _config(
        tables=(
            SourceTableDecl(
                name="watchers",
                membership=MembershipRef(kind="device", property="watchers"),
                sub_types=("night",),
            ),
        )
    )
    plan = _open_plan(
        emit_dir,
        config,
        keys={"device": {"day": "presentation_id", "night": "record_index"}},
    )
    table = plan.tables[0]
    assert isinstance(table, SourceJunctionTablePlan)
    assert table.owner_populations == (Population(kind="device", sub_type="night"),)
    owner_edge = next(e for e in table.edge_surfaces if e.source_column == "record_id")
    assert owner_edge.rendered_type == "BIGINT"


def test_junction_member_field_gates_each_known_kind_independently_raises(
    tmp_path: Path,
) -> None:
    """The member field's gate runs over each admitted kind's own domain
    independently — a union-unsafe device domain raises even though the
    owner kind's (team's, flat and never gated) own election stays
    default."""
    emit_dir = _team_owned_junction_with_device_member_emit(tmp_path, safe=False)
    config = _config(
        tables=(
            SourceTableDecl(
                name="watchers",
                membership=MembershipRef(kind="team", property="watchers"),
            ),
        )
    )
    with pytest.raises(ElectionUnionUnsafe):
        _open_plan(emit_dir, config, keys={"device": "presentation_id"})


def test_junction_member_field_gate_resolves_over_safe_domain(tmp_path: Path) -> None:
    """The member field's gate resolves each admitted kind's domain
    (team's default, device's safe uniform election) independently."""
    emit_dir = _team_owned_junction_with_device_member_emit(tmp_path, safe=True)
    config = _config(
        tables=(
            SourceTableDecl(
                name="watchers",
                membership=MembershipRef(kind="team", property="watchers"),
            ),
        )
    )
    plan = _open_plan(emit_dir, config, keys={"device": "presentation_id"})
    table = plan.tables[0]
    assert isinstance(table, SourceJunctionTablePlan)
    member_edge = next(e for e in table.edge_surfaces if e.source_column != "record_id")
    assert set(member_edge.target_kinds) == {"team", "device"}
    device_populations = dict(member_edge.per_kind_populations)["device"]
    assert {s for _, s in device_populations} == {"presentation_id"}


# ---------------------------------------------------------------------------
# Events log: item-type gate over the union of sources' populations
# ---------------------------------------------------------------------------


def test_item_type_gate_single_source_spanning_both_populations_raises(
    tmp_path: Path,
) -> None:
    """One events source addressing device's full domain: the item-type
    gate raises over the union-unsafe domain, naming item_type 'device'."""
    emit_dir = _device_only_emit(tmp_path, safe=False)
    config = _config(
        events=SourceEventsDecl(
            name="log", sources=(SourceEventSourceDecl(kind="device"),)
        )
    )
    with pytest.raises(ElectionUnionUnsafe, match="device"):
        _open_plan(emit_dir, config, keys={"device": "presentation_id"})


def test_item_type_gate_two_disjoint_sub_type_sources_raises(tmp_path: Path) -> None:
    """The exact same union-unsafe domain, declared as two disjoint
    single-sub-type sources instead of one spanning source, still raises —
    the gate runs over the union of an item-type's sources' populations,
    not per source."""
    emit_dir = _device_only_emit(tmp_path, safe=False)
    config = _config(
        events=SourceEventsDecl(
            name="log",
            sources=(
                SourceEventSourceDecl(kind="device", sub_types=("day",)),
                SourceEventSourceDecl(kind="device", sub_types=("night",)),
            ),
        )
    )
    with pytest.raises(ElectionUnionUnsafe, match="device"):
        _open_plan(emit_dir, config, keys={"device": "presentation_id"})


def test_item_type_gate_scoped_to_own_group_no_cross_item_type_check(
    tmp_path: Path,
) -> None:
    """A single-sub-type device source (item_type 'device', one population —
    never gated) alongside an unrelated flat-kind source (item_type
    'team') resolves cleanly even though device's *full* domain is
    globally union-unsafe: each item-type's gate ranges only over its own
    group's populations, never merged across item-types."""
    emit_dir = _device_team_emit(tmp_path, safe=False)
    config = _config(
        events=SourceEventsDecl(
            name="log",
            sources=(
                SourceEventSourceDecl(kind="device", sub_types=("day",)),
                SourceEventSourceDecl(kind="team"),
            ),
        )
    )
    plan = _open_plan(emit_dir, config, keys={"device": "presentation_id"})
    assert plan.events is not None
    assert {s.item_type for s in plan.events.sources} == {"device", "team"}


# ---------------------------------------------------------------------------
# Rename against the elected/absorbed domain
# ---------------------------------------------------------------------------


def test_rename_keyed_on_elected_surface_contract_name_renames_id_column(
    tmp_path: Path,
) -> None:
    """`rename` keyed on 'presentation_id' renames the elected self-identity
    column — the elected surface's own contract column name."""
    emit_dir = build_source_election_emit(tmp_path)
    config = ExportConfig(
        mode="source",
        source=SourceConfig(
            tables=(
                SourceTableDecl(
                    name="device", kind="device", rename={"presentation_id": "code"}
                ),
            )
        ),
    )
    plan = _open_plan(emit_dir, config, keys={"device": "presentation_id"})
    table = plan.tables[0]
    assert isinstance(table, SourceStateTablePlan)
    assert dict(table.columns)["presentation_id"] == "code"


def test_rename_keyed_on_absorbed_record_id_raises_unresolved(tmp_path: Path) -> None:
    """`rename` keyed on `record_id` is unresolvable once a presentation_id
    election absorbs it — 'record_id' is no longer a source key."""
    emit_dir = build_source_election_emit(tmp_path)
    config = ExportConfig(
        mode="source",
        source=SourceConfig(
            tables=(
                SourceTableDecl(
                    name="device", kind="device", rename={"record_id": "x"}
                ),
            )
        ),
    )
    with pytest.raises(SourceColumnUnresolved):
        _open_plan(emit_dir, config, keys={"device": "presentation_id"})


def test_rename_keyed_on_absorbed_record_id_under_record_index_raises_unresolved(
    tmp_path: Path,
) -> None:
    """`rename` keyed on `record_id` is unresolvable once a record_index
    election rewrites the source key to 'record_index'."""
    emit_dir = build_source_election_emit(tmp_path)
    config = ExportConfig(
        mode="source",
        source=SourceConfig(
            tables=(
                SourceTableDecl(
                    name="device", kind="device", rename={"record_id": "x"}
                ),
            )
        ),
    )
    with pytest.raises(SourceColumnUnresolved):
        _open_plan(emit_dir, config, keys={"device": "record_index"})


# ---------------------------------------------------------------------------
# Plan-time elected-key uniqueness guard
# ---------------------------------------------------------------------------


def test_plan_time_guard_refuses_corrupted_elected_key(tmp_path: Path) -> None:
    """A corrupted self-identity presentation_id (dev_day/dev_night sharing
    one value) fails the plan-time guard — build_source_plan itself raises,
    before any render or write."""
    emit_dir = build_source_election_emit(tmp_path, corrupt_device=True)
    config = _config(tables=(SourceTableDecl(name="device", kind="device"),))
    with pytest.raises(ElectedKeyDuplicate):
        _open_plan(emit_dir, config, keys={"device": "presentation_id"})
