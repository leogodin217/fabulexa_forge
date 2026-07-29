"""Base-mode render SQL: state-at composition, horizon selection, anchor-or-raw-ns
lifecycle rendering, cast-back to sidecar types, rename projection, and
record-index key joins.

`build_base_render_sql` composes the shipped state-at derivation verbatim —
`build_state_at_end_sql` at the tape's end (`horizon_ns is None`),
`build_state_at_sql` at an exclusive horizon otherwise — then wraps the raw
state-at relation with base's own presentation: `created_sim_time` /
`deactivated_at` render wallclock through the shared anchor renderer (raw
sim-time ns when `anchor` is None, since `render_anchor_timestamp_expr`
already handles that case); `presentation_id` and `prop__<p>` columns CAST
back from the state-at codec VARCHAR to their sidecar-declared type;
`record_id` / `active` pass through verbatim (the state-at derivation's own
native columns). It also composes the record-index resident at the same
horizon selection — once for the kind's own self key, once per
`spec.reference_keys` entry for the target kind's edge key — and LEFT JOINs
each onto the state-at spine: the self key ahead of `id`, an edge key
immediately after its own `prop__<p>` output column. Every column is
projected under `spec.column_renames`. Base never uses the
compile-indirection (`base_relations`) wrapping.

Layer-direction invariant: imports the reader (the structural-temporal
surface at runtime; `Sidecar` TYPE_CHECKING only), the derivations layer (the
state-at and record-index derivations), fabulexa_forge.anchor, the sibling
base.plan module (TYPE_CHECKING only), and stdlib. Never imports
exporters.dimensional.*, exporters.source.*, or exporters.streaming.*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.exporters.base.plan import BaseTableSpec, ReferenceKey
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge.anchor import render_anchor_timestamp_expr
from fabulexa_forge.derivations.record_index import (
    build_record_index_at_end_sql,
    build_record_index_at_sql,
)
from fabulexa_forge.derivations.state_at import (
    STATE_AT_COLUMNS,
    build_state_at_end_sql,
    build_state_at_sql,
)
from fabulexa_forge.reader.records_columns import structural_instant_columns

#: The `records__<kind>` name prefix a base spec's kind is read against.
_RECORDS_PREFIX = "records__"

#: The bare property-name prefix on a records-category column.
_PROP_PREFIX = "prop__"

#: State-at columns rendered verbatim — the derivation's own native
#: passthrough (`record_id`) or computed value (`active`); never wallclock,
#: never CAST back.
_VERBATIM_COLUMNS: frozenset[str] = frozenset({"record_id", "active"})

#: State-at columns rendered wallclock through the anchor renderer (or raw
#: sim-time ns, when `anchor` is None) — resolved through the reader's
#: structural-temporal surface. The state-at derivation never carries
#: `last_mutation_sim_time`, so this set's third member is inert here.
_WALLCLOCK_COLUMNS: frozenset[str] = frozenset(structural_instant_columns("records"))


def _column_types(sidecar: "Sidecar", table_name: str) -> dict[str, str]:
    """Map every column of `table_name` to its declared sidecar DuckDB type.

    Args:
        sidecar: The open emit's sidecar.
        table_name: A sidecar table name.

    Returns:
        {column name -> DuckDB type}, in no particular order.
    """
    return {col.name: col.type for col in sidecar.columns(table_name)}


def _state_at_column_order(
    sidecar: "Sidecar", spec: "BaseTableSpec"
) -> tuple[str, ...]:
    """The state-at column identities `spec`'s table carries, in fixed
    emission order.

    STATE_AT_COLUMNS prefix, `presentation_id` when the kind carries it, then
    one `prop__<p>` per selected property in sidecar column-declaration order
    (`spec.properties` is an unordered frozenset; order is derived here, not
    stored on the plan).

    Args:
        sidecar: The open emit's sidecar.
        spec: The resolved per-kind flat-output shape.

    Returns:
        State-at column identities, in emission order.
    """
    identities: list[str] = list(STATE_AT_COLUMNS)
    if spec.has_presentation_id:
        identities.append("presentation_id")
    table_name = f"{_RECORDS_PREFIX}{spec.kind}"
    for col in sidecar.columns(table_name):
        if not col.name.startswith(_PROP_PREFIX):
            continue
        prop = col.name[len(_PROP_PREFIX) :]
        if prop in spec.properties:
            identities.append(col.name)
    return tuple(identities)


#: The self-key join's table alias.
_SELF_KEY_ALIAS = "_key_self"


def _edge_key_alias(property_name: str) -> str:
    """The join alias for one reference property's edge-key relation.

    Args:
        property_name: The bare reference property name.

    Returns:
        A per-property alias, unique among a table's joins.
    """
    return f"_key_edge__{property_name}"


def _record_index_sql(
    sidecar: "Sidecar", fork_path: str, kind: str, horizon_ns: int | None
) -> str:
    """Compose the record-index resident for one kind at the render's horizon
    selection — the same selection `build_base_render_sql` applies to state-at
    (invariant 3: one horizon per table render).

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        kind: The record kind whose index relation to build.
        horizon_ns: The exclusive horizon, or None for the tape's end.

    Returns:
        A complete SELECT producing `RECORD_INDEX_COLUMNS` for `kind`.

    Raises:
        TableNotFoundError: `records__<kind>` is absent (propagated).
    """
    return (
        build_record_index_at_sql(sidecar, fork_path, kind, horizon_ns)
        if horizon_ns is not None
        else build_record_index_at_end_sql(sidecar, fork_path, kind)
    )


def _key_join_clauses(
    sidecar: "Sidecar",
    fork_path: str,
    spec: "BaseTableSpec",
    horizon_ns: int | None,
) -> str:
    """Build the LEFT JOIN clauses onto the record-index resident: the kind's
    own self-key relation, then one per `spec.reference_keys` entry.

    Each join is keyed one-to-one against a spine row — the self key on
    `record_id`, an edge key on the horizon-reconstructed `prop__<p>` value
    (both sides VARCHAR, no cast) — so key resolution never fans the spine's
    row set out (invariant 7).

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        spec: The resolved per-kind flat-output shape.
        horizon_ns: The render's horizon selection.

    Returns:
        The SQL fragment joining `"_base"` to the self-key relation, then
        each edge-key relation in `spec.reference_keys` order.
    """
    self_sql = _record_index_sql(sidecar, fork_path, spec.kind, horizon_ns)
    clauses = [
        f'LEFT JOIN ({self_sql}) AS "{_SELF_KEY_ALIAS}"'
        f' ON "_base"."record_id" = "{_SELF_KEY_ALIAS}"."record_id"'
    ]
    for rk in spec.reference_keys:
        edge_sql = _record_index_sql(sidecar, fork_path, rk.target_kind, horizon_ns)
        alias = _edge_key_alias(rk.property_name)
        prop_column = f"{_PROP_PREFIX}{rk.property_name}"
        clauses.append(
            f'LEFT JOIN ({edge_sql}) AS "{alias}"'
            f' ON "_base"."{prop_column}" = "{alias}"."record_id"'
        )
    return " ".join(clauses)


def _reference_keys_by_property(
    spec: "BaseTableSpec",
) -> dict[str, "ReferenceKey"]:
    """Index `spec.reference_keys` by their bare property name.

    Args:
        spec: The resolved per-kind flat-output shape.

    Returns:
        {bare property name -> ReferenceKey}, for the render loop to look up
        an edge key immediately after emitting its `prop__<p>` column.
    """
    return {rk.property_name: rk for rk in spec.reference_keys}


def build_base_render_sql(
    sidecar: "Sidecar",
    fork_path: str,
    spec: "BaseTableSpec",
    anchor: "EffectiveAnchor | None",
    horizon_ns: int | None,
) -> str:
    """Render one `BaseTableSpec` to a complete, deterministic SELECT at a horizon.

    Base's counterpart to source's `build_snapshot_render_sql`. Composes the
    shipped state-at derivation verbatim — `build_state_at_end_sql(sidecar,
    fork_path, spec.kind, spec.properties)` when `horizon_ns is None` (the
    structural tape's end, current state), `build_state_at_sql(sidecar,
    fork_path, spec.kind, spec.properties, horizon_ns)` otherwise — then wraps
    the raw relation with base's own presentation: the lifecycle timestamps
    `created_sim_time` and `deactivated_at` render through
    `render_anchor_timestamp_expr`, which already yields the raw sim-time
    column aliased when `anchor` is None (so base needs no conditional of its
    own); `prop__<p>` and `presentation_id` cast back from the state-at codec
    VARCHAR to their sidecar types (as source's snapshot render does).
    Composes the record-index resident at the same horizon selection
    (invariant 3) and `LEFT JOIN`s it in twice over: once for the kind's own
    self key, projected verbatim as the table's first column ahead of `id`;
    once per `spec.reference_keys` entry, projected immediately after its own
    `prop__<p>` output column. Every column — key or otherwise — is projected
    under `spec.column_renames` (including `record_id -> id`). Never uses the
    compile-indirection (`base_relations`) wrapping.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        spec: The resolved per-kind flat-output shape from `build_base_plan`.
        anchor: The resolved effective anchor, or None to emit raw sim-time ns.
        horizon_ns: The exclusive reconstruction horizon — `T + 1` for
            `slice_at: T`, a window's `end_ns` under incremental — or None for
            the tape's end.

    Returns:
        A complete SELECT producing the flat table (self key, `id`, lifecycle,
        `prop__<p>`/edge-key pairs interleaved), ordered by
        `(created_sim_time, record_id)` (raw, never a rendered timestamp).

    Raises:
        TableNotFoundError: `records__<kind>` or a reference edge's
            `records__<target_kind>` is absent (propagated from state-at or
            the record-index resident).
    """
    state_at_sql = (
        build_state_at_sql(sidecar, fork_path, spec.kind, spec.properties, horizon_ns)
        if horizon_ns is not None
        else build_state_at_end_sql(sidecar, fork_path, spec.kind, spec.properties)
    )
    col_types = _column_types(sidecar, f"{_RECORDS_PREFIX}{spec.kind}")
    identities = _state_at_column_order(sidecar, spec)
    reference_keys_by_property = _reference_keys_by_property(spec)

    self_key_out = spec.column_renames.get("record_index", "record_index")
    select_parts: list[str] = [
        f'"{_SELF_KEY_ALIAS}"."record_index" AS "{self_key_out}"'
    ]
    for identity in identities:
        out = spec.column_renames.get(identity, identity)
        qualified = f'"_base"."{identity}"'
        if identity in _VERBATIM_COLUMNS:
            select_parts.append(f'{qualified} AS "{out}"')
        elif identity in _WALLCLOCK_COLUMNS:
            select_parts.append(render_anchor_timestamp_expr(anchor, qualified, out))
        else:
            # presentation_id or a prop__<p> payload column: the state-at
            # derivation's value is codec VARCHAR; CAST back to the sidecar
            # type.
            select_parts.append(
                f'CAST({qualified} AS {col_types[identity]}) AS "{out}"'
            )

        if identity.startswith(_PROP_PREFIX):
            prop = identity[len(_PROP_PREFIX) :]
            rk = reference_keys_by_property.get(prop)
            if rk is not None:
                alias = _edge_key_alias(rk.property_name)
                key_identity = f"ref_index__{rk.property_name}"
                key_out = spec.column_renames.get(key_identity, key_identity)
                select_parts.append(f'"{alias}"."record_index" AS "{key_out}"')

    select_list = ", ".join(select_parts)
    key_joins_sql = _key_join_clauses(sidecar, fork_path, spec, horizon_ns)
    return (
        f'SELECT {select_list} FROM ({state_at_sql}) AS "_base" {key_joins_sql}'
        ' ORDER BY "_base"."created_sim_time", "_base"."record_id"'
    )
