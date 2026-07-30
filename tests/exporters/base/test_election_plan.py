"""Tests for base-mode key election at plan time: identity_surface stamping,
the identity + edge combination gates, self/edge column absorption via
column_renames, rename resolution against the elected domain, and
resolve_base_table_keys under a non-default election.

Sidecars are built in-memory via Sidecar.from_raw (no DuckDB needed — plan
building reads only the sidecar); the election is resolved directly via
resolve_election and threaded through build_base_plan's `election` keyword,
mirroring the engine's own resolve-then-plan sequencing
(exporters/base/engine.py).
"""

from __future__ import annotations

import pytest
from _support.notices import discard_notice_sink

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import BaseConfig, ExcludeDecl, RenameEntry
from fabulexa_forge.errors import (
    BaseRenameUnresolved,
    ElectionMixedIdentity,
    ElectionUnionUnsafe,
)
from fabulexa_forge.exporters.base.plan import (
    BasePlan,
    build_base_plan,
    resolve_base_table_keys,
)
from fabulexa_forge.exporters.election import Election, resolve_election
from fabulexa_forge.exporters.query_spec import TableKeys
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
    """The exempt discriminator column for a sub-typed kind — slice_only,
    retained despite the class because `subtype_values` is non-empty."""
    return _col(
        f"prop__{kind}_type", history_tracked=False, temporal_class="slice_only"
    )


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


def _sidecar(
    tables: list[dict[str, object]],
    enum_domains: dict[str, object] | None = None,
    presentation_keys: dict[str, object] | None = None,
) -> Sidecar:
    """Build a Sidecar directly from a raw base.json-shaped mapping."""
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "tables": tables,
    }
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


def _flat_ward_sidecar(presentation_keys: dict[str, object] | None = None) -> Sidecar:
    """A flat 'ward' kind carrying presentation_id, for self-column election
    tests."""
    return _sidecar(
        tables=[_records_table("ward", [], presentation_id=True)],
        presentation_keys=presentation_keys,
    )


def _sub_typed_entity_sidecar(
    presentation_keys: dict[str, object] | None = None,
) -> Sidecar:
    """A sub-typed 'entity' kind (alpha/beta domain), for identity gate tests."""
    entity_table = _records_table(
        "entity", [_discriminator_col("entity")], presentation_id=True
    )
    return _sidecar(
        tables=[entity_table],
        enum_domains={"entity": {"entity_type": ["alpha", "beta"]}},
        presentation_keys=presentation_keys,
    )


_ENTITY_SAFE_PRESENTATION_KEYS: dict[str, object] = {
    "entity": {
        "sub_types": {
            "alpha": _raw_counter_key("ALPHA_"),
            "beta": _raw_counter_key("BETA_"),
        },
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    }
}

# alpha/beta declared as bare (empty-prefix) counters — a union-unsafe pair.
_ENTITY_UNSAFE_PRESENTATION_KEYS: dict[str, object] = {
    "entity": {
        "sub_types": {
            "alpha": _raw_counter_key(""),
            "beta": _raw_counter_key(""),
        },
        "branch_stable": False,
        "slice_stable": False,
    }
}


def _actor_referencing_sub_typed_target_sidecar(
    presentation_keys: dict[str, object] | None = None,
) -> Sidecar:
    """`actor` references a sub-typed `target` kind (alpha/beta domain)."""
    actor_table = _records_table(
        "actor",
        [
            _reference_col("prop__lead_id", "target"),
            _col("ref_index__lead_id", "BIGINT"),
        ],
    )
    target_table = _records_table(
        "target", [_discriminator_col("target")], presentation_id=True
    )
    return _sidecar(
        tables=[actor_table, target_table],
        enum_domains={"target": {"target_type": ["alpha", "beta"]}},
        presentation_keys=presentation_keys,
    )


_TARGET_SAFE_PRESENTATION_KEYS: dict[str, object] = {
    "target": {
        "sub_types": {
            "alpha": _raw_counter_key("ALPHA_"),
            "beta": _raw_counter_key("BETA_"),
        },
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    }
}

_TARGET_UNSAFE_PRESENTATION_KEYS: dict[str, object] = {
    "target": {
        "sub_types": {
            "alpha": _raw_counter_key(""),
            "beta": _raw_counter_key(""),
        },
        "branch_stable": False,
        "slice_stable": False,
    }
}


def _flat_target_actor_sidecar(
    presentation_keys: dict[str, object] | None = None,
) -> Sidecar:
    """`actor` references a flat `target` kind, for edge-column election tests."""
    actor_table = _records_table(
        "actor",
        [
            _reference_col("prop__lead_id", "target"),
            _col("ref_index__lead_id", "BIGINT"),
        ],
    )
    target_table = _records_table("target", [], presentation_id=True)
    return _sidecar(
        tables=[actor_table, target_table], presentation_keys=presentation_keys
    )


def _plan_with_election(
    sidecar: Sidecar,
    keys: "dict[str, object] | None",
    config: "BaseConfig | None" = None,
) -> BasePlan:
    """Resolve `keys` against `sidecar`, then build the base plan under it —
    the engine's own resolve-then-plan sequencing."""
    election = resolve_election(sidecar, keys)
    return build_base_plan(sidecar, config, discard_notice_sink, election=election)


# ---------------------------------------------------------------------------
# identity_surface stamping
# ---------------------------------------------------------------------------


def test_identity_surface_default_record_id() -> None:
    """No keys block -> identity_surface defaults to record_id."""
    plan = _plan_with_election(_flat_ward_sidecar(), None)
    assert plan.tables[0].identity_surface == "record_id"


def test_identity_surface_explicit_record_id_byte_identical_to_default() -> None:
    """An explicit record_id election resolves the same self columns as the
    default (no keys) election — 'today's pair', unaffected."""
    sidecar = _flat_ward_sidecar()
    default_plan = build_base_plan(sidecar, None, discard_notice_sink)
    explicit_plan = _plan_with_election(sidecar, {"ward": "record_id"})
    assert (
        default_plan.tables[0].column_renames == explicit_plan.tables[0].column_renames
    )
    assert explicit_plan.tables[0].identity_surface == "record_id"


def test_identity_surface_explicit_presentation_id_stamped() -> None:
    """An explicit presentation_id election stamps identity_surface."""
    sidecar = _flat_ward_sidecar({"ward": {"key": _raw_counter_key("W_")}})
    plan = _plan_with_election(sidecar, {"ward": "presentation_id"})
    assert plan.tables[0].identity_surface == "presentation_id"


def test_identity_surface_explicit_record_index_stamped() -> None:
    """An explicit record_index election stamps identity_surface."""
    plan = _plan_with_election(_flat_ward_sidecar(), {"ward": "record_index"})
    assert plan.tables[0].identity_surface == "record_index"


def test_identity_surface_sub_typed_uniform_presentation_id_stamped() -> None:
    """A sub-typed kind's uniform presentation_id election, pairwise
    union-safe, stamps identity_surface — no split (base never splits)."""
    sidecar = _sub_typed_entity_sidecar(_ENTITY_SAFE_PRESENTATION_KEYS)
    plan = _plan_with_election(sidecar, {"entity": "presentation_id"})
    assert plan.tables[0].identity_surface == "presentation_id"
    assert len(plan.tables) == 1


# ---------------------------------------------------------------------------
# Identity combination gate: mixed election / union-unsafe uniform election
# ---------------------------------------------------------------------------


def test_sub_typed_kind_mixed_election_raises_election_mixed_identity() -> None:
    """A sub-typed kind whose populations elect differing surfaces raises
    ElectionMixedIdentity — base never splits, so the full domain is spanned
    by one table."""
    sidecar = _sub_typed_entity_sidecar(_ENTITY_SAFE_PRESENTATION_KEYS)
    with pytest.raises(ElectionMixedIdentity):
        _plan_with_election(sidecar, {"entity": {"alpha": "presentation_id"}})


def test_uniform_presentation_id_over_union_unsafe_siblings_raises() -> None:
    """A uniform presentation_id election over bare-counter siblings raises
    ElectionUnionUnsafe."""
    sidecar = _sub_typed_entity_sidecar(_ENTITY_UNSAFE_PRESENTATION_KEYS)
    with pytest.raises(ElectionUnionUnsafe):
        _plan_with_election(sidecar, {"entity": "presentation_id"})


# ---------------------------------------------------------------------------
# Edge gate: runs over the target kind's full declared domain
# ---------------------------------------------------------------------------


def test_edge_gate_runs_over_target_full_domain_raises_union_unsafe() -> None:
    """A reference edge's gate runs over the target's full declared domain,
    independent of the target's own identity gate — excluding `target` from
    base's own output isolates the edge gate as the sole path to the error."""
    sidecar = _actor_referencing_sub_typed_target_sidecar(
        _TARGET_UNSAFE_PRESENTATION_KEYS
    )
    config = BaseConfig(exclude=ExcludeDecl(kinds=["target"]))
    election = resolve_election(sidecar, {"target": "presentation_id"})
    with pytest.raises(ElectionUnionUnsafe):
        build_base_plan(sidecar, config, discard_notice_sink, election=election)


def test_edge_gate_passes_uniform_presentation_id_over_safe_domain() -> None:
    """The edge gate passes a uniform presentation_id election over a
    pairwise union-safe domain, resolving every ReferenceKey population."""
    sidecar = _actor_referencing_sub_typed_target_sidecar(
        _TARGET_SAFE_PRESENTATION_KEYS
    )
    election = resolve_election(sidecar, {"target": "presentation_id"})
    plan = build_base_plan(sidecar, None, discard_notice_sink, election=election)
    spec = next(t for t in plan.tables if t.kind == "actor")
    rk = spec.reference_keys[0]
    assert {surface for _, surface in rk.per_population} == {"presentation_id"}


# ---------------------------------------------------------------------------
# Target kind absent from the emit: skipped, no gate, renders verbatim
# ---------------------------------------------------------------------------


def test_reference_to_absent_target_skips_gate_renders_verbatim() -> None:
    """A property whose target kind has no records table in the emit is
    skipped before any gate call — no ReferenceKey entry, no error, even
    though the target kind is otherwise unresolvable."""
    actor_table = _records_table(
        "actor",
        [
            _reference_col("prop__ghost_id", "ghost"),
            _col("ref_index__ghost_id", "BIGINT"),
        ],
    )
    sidecar = _sidecar(tables=[actor_table])
    plan = _plan_with_election(sidecar, None)
    assert plan.tables[0].reference_keys == ()


# ---------------------------------------------------------------------------
# Self columns: presentation_id absorption, record_index drop
# ---------------------------------------------------------------------------


def test_presentation_id_election_absorbs_standalone_presentation_id_column() -> None:
    """Under presentation_id election, the elected value occupies the self
    slot (rename key 'presentation_id', default output 'id'); record_id is
    absorbed entirely — never a rename-reachable identity."""
    sidecar = _flat_ward_sidecar({"ward": {"key": _raw_counter_key("W_")}})
    plan = _plan_with_election(sidecar, {"ward": "presentation_id"})
    spec = plan.tables[0]
    assert spec.column_renames["presentation_id"] == "id"
    assert "record_id" not in spec.column_renames


def test_record_index_election_drops_self_column_keeps_kind_key_only() -> None:
    """Under record_index election, the self id-space slot is dropped
    entirely — neither record_id nor presentation_id is the self identity;
    only <kind>_key ships as the record-index self key."""
    sidecar = _flat_ward_sidecar({"ward": {"key": _raw_counter_key("W_")}})
    plan = _plan_with_election(sidecar, {"ward": "record_index"})
    spec = plan.tables[0]
    assert "record_id" not in spec.column_renames
    assert "presentation_id" not in spec.column_renames
    assert spec.column_renames["record_index"] == "ward_key"


def test_record_index_election_keeps_presentation_id_standalone_when_carried() -> None:
    """Under record_index election, a carried presentation_id column is NOT
    absorbed — it remains a plain payload column, reachable by rename."""
    sidecar = _flat_ward_sidecar({"ward": {"key": _raw_counter_key("W_")}})
    config = BaseConfig(
        rename=[RenameEntry(table="records__ward", columns={"presentation_id": "code"})]
    )
    plan = _plan_with_election(sidecar, {"ward": "record_index"}, config)
    assert plan.tables[0].column_renames["presentation_id"] == "code"


# ---------------------------------------------------------------------------
# Edge columns: uniform presentation_id / uniform record_index / mixed
# ---------------------------------------------------------------------------


def test_uniform_presentation_id_edge_ships_value_key_default_unaffected() -> None:
    """A uniform presentation_id target ships prop__<p> (rendered_type
    follows the target's declared presentation_id type); the record-index
    edge key default is unaffected by the election."""
    sidecar = _flat_target_actor_sidecar({"target": {"key": _raw_counter_key("T_")}})
    election = resolve_election(sidecar, {"target": "presentation_id"})
    plan = build_base_plan(sidecar, None, discard_notice_sink, election=election)
    spec = next(t for t in plan.tables if t.kind == "actor")
    rk = spec.reference_keys[0]
    assert rk.value_column_shipped is True
    assert rk.rendered_type == "VARCHAR"
    assert spec.column_renames["ref_index__lead_id"] == "lead_id_key"


def test_uniform_record_index_edge_drops_value_column() -> None:
    """An all-record_index target election drops prop__<p> — it would
    duplicate the always-on <p>_key edge key."""
    sidecar = _flat_target_actor_sidecar()
    election = resolve_election(sidecar, {"target": "record_index"})
    plan = build_base_plan(sidecar, None, discard_notice_sink, election=election)
    spec = next(t for t in plan.tables if t.kind == "actor")
    rk = spec.reference_keys[0]
    assert rk.value_column_shipped is False
    assert rk.rendered_type == "BIGINT"


def test_excluded_mixed_election_target_ships_varchar_per_row() -> None:
    """An excluded target kind's mixed election (only possible when the
    target is excluded from base's own output) ships prop__<p> as VARCHAR,
    rendered per admitted population."""
    sidecar = _actor_referencing_sub_typed_target_sidecar(
        _TARGET_SAFE_PRESENTATION_KEYS
    )
    config = BaseConfig(exclude=ExcludeDecl(kinds=["target"]))
    election = resolve_election(
        sidecar, {"target": {"alpha": "presentation_id", "beta": "record_index"}}
    )
    plan = build_base_plan(sidecar, config, discard_notice_sink, election=election)
    spec = next(t for t in plan.tables if t.kind == "actor")
    rk = spec.reference_keys[0]
    assert rk.value_column_shipped is True
    assert rk.rendered_type == "VARCHAR"
    assert {surface for _, surface in rk.per_population} == {
        "presentation_id",
        "record_index",
    }


# ---------------------------------------------------------------------------
# Rename against the elected domain
# ---------------------------------------------------------------------------


def test_rename_record_id_under_presentation_id_election_raises_unresolved() -> None:
    """rename keyed on record_id is unresolvable once presentation_id
    election absorbs it."""
    sidecar = _flat_ward_sidecar({"ward": {"key": _raw_counter_key("W_")}})
    config = BaseConfig(
        rename=[RenameEntry(table="records__ward", columns={"record_id": "x"})]
    )
    with pytest.raises(BaseRenameUnresolved):
        _plan_with_election(sidecar, {"ward": "presentation_id"}, config)


def test_rename_record_id_under_record_index_election_raises_unresolved() -> None:
    """rename keyed on record_id is unresolvable once record_index election
    drops the self id-space slot entirely."""
    sidecar = _flat_ward_sidecar()
    config = BaseConfig(
        rename=[RenameEntry(table="records__ward", columns={"record_id": "x"})]
    )
    with pytest.raises(BaseRenameUnresolved):
        _plan_with_election(sidecar, {"ward": "record_index"}, config)


def test_rename_dropped_edge_value_column_raises_unresolved() -> None:
    """rename keyed on a dropped edge value column (uniform record_index
    target) is unresolvable."""
    sidecar = _flat_target_actor_sidecar()
    config = BaseConfig(
        rename=[RenameEntry(table="records__actor", columns={"prop__lead_id": "x"})]
    )
    election = resolve_election(sidecar, {"target": "record_index"})
    with pytest.raises(BaseRenameUnresolved):
        build_base_plan(sidecar, config, discard_notice_sink, election=election)


def test_rename_keyed_on_presentation_id_renames_elected_id_column() -> None:
    """rename keyed on 'presentation_id' renames the elected self identity
    column — the elected surface's own contract column name."""
    sidecar = _flat_ward_sidecar({"ward": {"key": _raw_counter_key("W_")}})
    config = BaseConfig(
        rename=[RenameEntry(table="records__ward", columns={"presentation_id": "code"})]
    )
    plan = _plan_with_election(sidecar, {"ward": "presentation_id"}, config)
    assert plan.tables[0].column_renames["presentation_id"] == "code"


# ---------------------------------------------------------------------------
# resolve_base_table_keys under a non-default election
# ---------------------------------------------------------------------------


def test_resolve_base_table_keys_pk_is_kind_key_under_record_index() -> None:
    """PK is <kind>_key under record_index election."""
    sidecar = _flat_ward_sidecar()
    plan = _plan_with_election(sidecar, {"ward": "record_index"})
    keys = resolve_base_table_keys(sidecar, plan.tables[0])
    assert keys.primary_key == ("ward_key",)


def test_resolve_base_table_keys_pk_follows_elected_presentation_id_column() -> None:
    """PK follows the elected identity column under presentation_id
    election — PK-eligible, superseding the always-UNIQUE posture for that
    column alone (no doubled UNIQUE declaration)."""
    sidecar = _flat_ward_sidecar({"ward": {"key": _raw_counter_key("W_")}})
    plan = _plan_with_election(sidecar, {"ward": "presentation_id"})
    keys = resolve_base_table_keys(sidecar, plan.tables[0])
    assert keys == TableKeys(primary_key=("id",), unique=())


def test_resolve_base_table_keys_standalone_presentation_id_still_unique_under_record_index() -> (
    None
):
    """A standalone (non-absorbed) presentation_id column under record_index
    election still carries its registry claim's UNIQUE — only an ABSORBED
    column's side UNIQUE goes undeclared."""
    sidecar = _flat_ward_sidecar({"ward": {"key": _raw_counter_key("W_")}})
    plan = _plan_with_election(sidecar, {"ward": "record_index"})
    keys = resolve_base_table_keys(sidecar, plan.tables[0])
    assert keys == TableKeys(primary_key=("ward_key",), unique=(("presentation_id",),))


def test_resolve_base_table_keys_no_election_resolution_unchanged() -> None:
    """Threading an explicitly-resolved default election produces the same
    resolved keys as omitting the `election` kwarg entirely."""
    sidecar = _flat_ward_sidecar({"ward": {"key": _raw_counter_key("W_")}})
    default_election: Election = resolve_election(sidecar, None)
    plan_implicit = build_base_plan(sidecar, None, discard_notice_sink)
    plan_explicit = build_base_plan(
        sidecar, None, discard_notice_sink, election=default_election
    )
    assert resolve_base_table_keys(
        sidecar, plan_implicit.tables[0]
    ) == resolve_base_table_keys(sidecar, plan_explicit.tables[0])
