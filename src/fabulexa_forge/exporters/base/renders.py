"""Base-mode render SQL: state-at composition, horizon selection, anchor-or-raw-ns
lifecycle rendering, cast-back to sidecar types, rename projection, election
joins, and record-index key joins.

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
immediately after its own `prop__<p>` output column. A non-`record_id`
election adds further joins onto the same spine (§ `_key_join_clauses`): the
presentation-key resident for a `presentation_id` self election or a uniform
`presentation_id` edge, and a per-population CASE relation
(§ `_mixed_edge_relation_sql`) for an edge whose admitted target populations
elect differing surfaces (only possible for an `exclude`d target kind — base
never splits its own emitted tables). Every column is projected under
`spec.column_renames`. Base never uses the compile-indirection
(`base_relations`) wrapping.

Layer-direction invariant: imports the reader (the structural-temporal
surface at runtime; `Sidecar` TYPE_CHECKING only), the derivations layer (the
state-at, record-index, and presentation-key derivations),
fabulexa_forge.anchor, fabulexa_forge._sql, the sibling base.plan module
(`_self_identity` at runtime; `BaseTableSpec` / `ReferenceKey` TYPE_CHECKING
only), and stdlib. Never imports exporters.dimensional.*, exporters.source.*,
or exporters.streaming.*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import KeySurface
    from fabulexa_forge.exporters.base.plan import BaseTableSpec, ReferenceKey
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge._sql import _sql_literal
from fabulexa_forge.anchor import render_anchor_timestamp_expr
from fabulexa_forge.derivations.presentation_key import (
    build_presentation_key_at_end_sql,
    build_presentation_key_at_sql,
)
from fabulexa_forge.derivations.record_index import (
    build_record_index_at_end_sql,
    build_record_index_at_sql,
)
from fabulexa_forge.derivations.state_at import (
    STATE_AT_COLUMNS,
    build_state_at_end_sql,
    build_state_at_sql,
)
from fabulexa_forge.exporters.base.plan import _self_identity
from fabulexa_forge.reader.records_columns import structural_instant_columns
from fabulexa_forge.reader.relations import build_records_relation_sql

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

    The self identity (§ `plan._self_identity` — the elected surface's
    contract column name, absent under `record_index`), then
    `STATE_AT_COLUMNS[1:]`, then `presentation_id` when the kind carries it
    and it is not absorbed into the self slot, then one `prop__<p>` per
    selected property in sidecar column-declaration order (`spec.properties`
    is an unordered frozenset; order is derived here, not stored on the
    plan). Unlike `plan._state_at_identities`'s validation-domain view, a
    reference property whose `prop__<p>` value column the election drops
    (`ReferenceKey.value_column_shipped=False`) stays in this order — the
    render loop visits its position to emit the always-on `<p>_key`, just
    without a value SELECT part (§ `build_base_render_sql`).

    Args:
        sidecar: The open emit's sidecar.
        spec: The resolved per-kind flat-output shape.

    Returns:
        State-at column identities, in emission order.
    """
    identities: list[str] = []
    self_identity = _self_identity(spec.identity_surface)
    if self_identity is not None:
        identities.append(self_identity)
    identities.extend(STATE_AT_COLUMNS[1:])
    if spec.has_presentation_id and spec.identity_surface != "presentation_id":
        identities.append("presentation_id")
    table_name = f"{_RECORDS_PREFIX}{spec.kind}"
    for col in sidecar.columns(table_name):
        if not col.name.startswith(_PROP_PREFIX):
            continue
        prop = col.name[len(_PROP_PREFIX) :]
        if prop in spec.properties:
            identities.append(col.name)
    return tuple(identities)


#: The self-key join's table alias (always-on record-index self key).
_SELF_KEY_ALIAS = "_key_self"

#: The self-value join's table alias (presentation-key self election only).
_SELF_PID_ALIAS = "_pid_self"


def _edge_key_alias(property_name: str) -> str:
    """The join alias for one reference property's edge-key relation.

    Args:
        property_name: The bare reference property name.

    Returns:
        A per-property alias, unique among a table's joins.
    """
    return f"_key_edge__{property_name}"


def _edge_value_alias(property_name: str) -> str:
    """The join alias for one reference property's elected edge-value relation.

    Args:
        property_name: The bare reference property name.

    Returns:
        A per-property alias, unique among a table's joins.
    """
    return f"_value_edge__{property_name}"


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


def _presentation_key_sql(
    sidecar: "Sidecar", fork_path: str, kind: str, horizon_ns: int | None
) -> str:
    """Compose the presentation-key resident for one kind at the render's
    horizon selection — `_record_index_sql`'s exact sibling.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        kind: The record kind whose presentation-key relation to build.
        horizon_ns: The exclusive horizon, or None for the tape's end.

    Returns:
        A complete SELECT producing `PRESENTATION_KEY_COLUMNS` for `kind`.

    Raises:
        TableNotFoundError: `records__<kind>` is absent (propagated).
        ExportError: `records__<kind>` declares no `presentation_id` column
            — a caller gating error (the election gates make it unreachable
            from a gated plan).
    """
    return (
        build_presentation_key_at_sql(sidecar, fork_path, kind, horizon_ns)
        if horizon_ns is not None
        else build_presentation_key_at_end_sql(sidecar, fork_path, kind)
    )


def _mixed_edge_relation_sql(
    sidecar: "Sidecar",
    fork_path: str,
    target_kind: str,
    horizon_ns: int | None,
    per_population: "tuple[tuple[str | None, KeySurface], ...]",
) -> str:
    """Compose one (record_id, rendered_value) VARCHAR relation for a
    reference edge whose admitted target populations elect differing
    surfaces (only possible for an `exclude`d target kind — base gates every
    surviving kind's own populations uniform).

    Reads the per-row population from the target's own records-spine
    discriminator (never a fold after-image, per the doc's per-row
    resolution rule) via a CASE keyed on `prop__<target_kind>_type`, each
    arm sourcing its admitted population's elected surface: the target's own
    `record_id` (verbatim, unaffected by horizon — the shipped record_id
    posture), the record-index resident's value (horizon-bound,
    digit-rendered), or the presentation-key resident's value
    (horizon-bound). A target absent from the target kind's own records
    relation (dangled sentinel) has no row in this relation at all, so the
    consuming LEFT JOIN yields NULL — the doc's "unresolvable edge renders
    NULL under a joined surface" posture, generalized to the mixed case.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        target_kind: The reference edge's target kind.
        horizon_ns: The render's horizon selection.
        per_population: The target kind's full declared domain, each with
            its resolved election (`ReferenceKey.per_population`).

    Returns:
        A complete SELECT producing `(record_id, rendered_value)`, one row
        per record of `target_kind`, `rendered_value` VARCHAR.

    Raises:
        TableNotFoundError: `records__<target_kind>` is absent (propagated).
    """
    records_sql = build_records_relation_sql(sidecar, fork_path, target_kind, {})
    index_sql = _record_index_sql(sidecar, fork_path, target_kind, horizon_ns)
    presentation_sql = _presentation_key_sql(
        sidecar, fork_path, target_kind, horizon_ns
    )
    discriminator_column = f"{_PROP_PREFIX}{target_kind}_type"

    arms: list[str] = []
    for sub_type, surface in per_population:
        assert sub_type is not None, "a mixed edge target is always sub-typed"
        condition = f'"_rec"."{discriminator_column}" = {_sql_literal(sub_type)}'
        if surface == "record_id":
            value_expr = 'CAST("_rec"."record_id" AS VARCHAR)'
        elif surface == "record_index":
            value_expr = 'CAST("_idx"."record_index" AS VARCHAR)'
        else:
            value_expr = 'CAST("_pid"."presentation_id" AS VARCHAR)'
        arms.append(f"WHEN {condition} THEN {value_expr}")
    case_sql = "CASE " + " ".join(arms) + " END"

    return (
        f'SELECT "_rec"."record_id" AS "record_id", {case_sql} AS "rendered_value"'
        f' FROM ({records_sql}) AS "_rec"'
        f' LEFT JOIN ({index_sql}) AS "_idx"'
        ' ON "_rec"."record_id" = "_idx"."record_id"'
        f' LEFT JOIN ({presentation_sql}) AS "_pid"'
        ' ON "_rec"."record_id" = "_pid"."record_id"'
    )


def _edge_value_join_sql(
    sidecar: "Sidecar",
    fork_path: str,
    rk: "ReferenceKey",
    horizon_ns: int | None,
) -> str | None:
    """The elected edge-value relation to LEFT JOIN for one reference key, if any.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        rk: The resolved reference key.
        horizon_ns: The render's horizon selection.

    Returns:
        None when `prop__<p>` renders verbatim (uniform record_id) or is
        dropped (uniform record_index — no value join needed, the always-on
        `<p>_key` join covers it); the presentation-key resident when the
        admitted populations uniformly elect presentation_id; the mixed
        per-row relation (§ `_mixed_edge_relation_sql`) otherwise.
    """
    if not rk.value_column_shipped:
        return None
    surfaces = {surface for _, surface in rk.per_population}
    if surfaces == {"record_id"}:
        return None
    if surfaces == {"presentation_id"}:
        return _presentation_key_sql(sidecar, fork_path, rk.target_kind, horizon_ns)
    return _mixed_edge_relation_sql(
        sidecar, fork_path, rk.target_kind, horizon_ns, rk.per_population
    )


def _key_join_clauses(
    sidecar: "Sidecar",
    fork_path: str,
    spec: "BaseTableSpec",
    horizon_ns: int | None,
) -> str:
    """Build the LEFT JOIN clauses onto the record-index resident and, under a
    non-`record_id` election, the elected-surface relations: the kind's own
    self-key relation (always), the self-value relation (`presentation_id`
    self election only), then per `spec.reference_keys` entry its edge-key
    relation (always) and its elected edge-value relation when one applies
    (§ `_edge_value_join_sql`).

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
        The SQL fragment joining `"_base"` to every relation this table's
        render needs.
    """
    self_sql = _record_index_sql(sidecar, fork_path, spec.kind, horizon_ns)
    clauses = [
        f'LEFT JOIN ({self_sql}) AS "{_SELF_KEY_ALIAS}"'
        f' ON "_base"."record_id" = "{_SELF_KEY_ALIAS}"."record_id"'
    ]
    if spec.identity_surface == "presentation_id":
        self_pid_sql = _presentation_key_sql(sidecar, fork_path, spec.kind, horizon_ns)
        clauses.append(
            f'LEFT JOIN ({self_pid_sql}) AS "{_SELF_PID_ALIAS}"'
            f' ON "_base"."record_id" = "{_SELF_PID_ALIAS}"."record_id"'
        )
    for rk in spec.reference_keys:
        edge_sql = _record_index_sql(sidecar, fork_path, rk.target_kind, horizon_ns)
        alias = _edge_key_alias(rk.property_name)
        prop_column = f"{_PROP_PREFIX}{rk.property_name}"
        clauses.append(
            f'LEFT JOIN ({edge_sql}) AS "{alias}"'
            f' ON "_base"."{prop_column}" = "{alias}"."record_id"'
        )
        value_sql = _edge_value_join_sql(sidecar, fork_path, rk, horizon_ns)
        if value_sql is not None:
            value_alias = _edge_value_alias(rk.property_name)
            clauses.append(
                f'LEFT JOIN ({value_sql}) AS "{value_alias}"'
                f' ON "_base"."{prop_column}" = "{value_alias}"."record_id"'
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


def _render_reference_value(
    rk: "ReferenceKey", out: str, col_types: dict[str, str], identity: str
) -> str:
    """Render one surviving `prop__<p>` reference column's SELECT expression.

    Args:
        rk: The resolved reference key (per the doc's per-edge column table).
        out: The output column name.
        col_types: The owning kind's own declared column types (§ `_column_types`).
        identity: The state-at identity (`prop__<p>`), for the owner's own
            CAST under a uniform record_id election.

    Returns:
        `CAST("_base"."prop__<p>" AS <type>) AS "<out>"` under a uniform
        record_id election (verbatim, unaffected); `"<alias>"."presentation_id"`
        joined and CAST to `rk.rendered_type` under a uniform presentation_id
        election; `"<alias>"."rendered_value"` (already `rk.rendered_type` —
        VARCHAR) under any other admitted mix.
    """
    surfaces = {surface for _, surface in rk.per_population}
    if surfaces == {"record_id"}:
        qualified = f'"_base"."{identity}"'
        return f'CAST({qualified} AS {col_types[identity]}) AS "{out}"'
    alias = _edge_value_alias(rk.property_name)
    if surfaces == {"presentation_id"}:
        return f'CAST("{alias}"."presentation_id" AS {rk.rendered_type}) AS "{out}"'
    return f'"{alias}"."rendered_value" AS "{out}"'


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
    VARCHAR to their sidecar types (as source's snapshot render does), except
    a `prop__<p>` reference column whose admitted target populations elect a
    non-uniform-record_id surface, which reads its elected-surface join
    instead (§ `_render_reference_value`). Composes the record-index resident
    at the same horizon selection (invariant 3) and `LEFT JOIN`s it in: once
    for the kind's own self key (always), once per `spec.reference_keys`
    entry for its always-on `<p>_key`; under a non-`record_id` election it
    also joins the elected-surface relations spec's identity_surface and each
    edge's per_population resolve (§ `_key_join_clauses`). The self id-space
    slot follows the doc's self-column table: `record_id` (unchanged
    verbatim, ahead of `id`), `presentation_id` (the elected value in the id
    slot, the standalone payload column absorbed), or `record_index` (the
    slot dropped entirely — only `<kind>_key` ships). Every column — key or
    otherwise — is projected under `spec.column_renames`. Never uses the
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
        A complete SELECT producing the flat table (self key, self id-space
        slot, lifecycle, `prop__<p>`/edge-key pairs interleaved), ordered by
        `(created_sim_time, record_id)` (raw, never a rendered timestamp).

    Raises:
        TableNotFoundError: `records__<kind>` or a reference edge's
            `records__<target_kind>` is absent (propagated from state-at,
            the record-index resident, or the presentation-key resident).
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
        is_prop = identity.startswith(_PROP_PREFIX)
        prop = identity[len(_PROP_PREFIX) :] if is_prop else None
        rk = reference_keys_by_property.get(prop) if prop is not None else None
        is_elected_self = (
            identity == "presentation_id" and spec.identity_surface == "presentation_id"
        )

        if is_elected_self:
            select_parts.append(f'"{_SELF_PID_ALIAS}"."presentation_id" AS "{out}"')
        elif identity in _VERBATIM_COLUMNS:
            select_parts.append(f'{qualified} AS "{out}"')
        elif identity in _WALLCLOCK_COLUMNS:
            select_parts.append(render_anchor_timestamp_expr(anchor, qualified, out))
        elif rk is not None:
            # A dropped value column (uniform record_index target election)
            # emits no SELECT part at all — the always-on `<p>_key` below is
            # unaffected and still ships.
            if rk.value_column_shipped:
                select_parts.append(
                    _render_reference_value(rk, out, col_types, identity)
                )
        else:
            # presentation_id (standalone, non-elected) or a non-reference
            # prop__<p> payload column: the state-at derivation's value is
            # codec VARCHAR; CAST back to the sidecar type.
            select_parts.append(
                f'CAST({qualified} AS {col_types[identity]}) AS "{out}"'
            )

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
