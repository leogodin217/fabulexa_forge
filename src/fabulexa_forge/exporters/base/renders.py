"""Base-mode render SQL: state-at composition, horizon selection, anchor-or-raw-ns
lifecycle rendering, cast-back to sidecar types, and rename projection.

`build_base_render_sql` composes the shipped state-at derivation verbatim —
`build_state_at_end_sql` at the tape's end (`horizon_ns is None`),
`build_state_at_sql` at an exclusive horizon otherwise — then wraps the raw
state-at relation with base's own presentation: `created_sim_time` /
`deactivated_at` render wallclock through the shared anchor renderer (raw
sim-time ns when `anchor` is None, since `render_anchor_timestamp_expr`
already handles that case); `presentation_id` and `prop__<p>` columns CAST
back from the state-at codec VARCHAR to their sidecar-declared type;
`record_id` / `active` pass through verbatim (the state-at derivation's own
native columns). Every column is projected under `spec.column_renames`.
Base never uses the compile-indirection (`base_relations`) wrapping.

Layer-direction invariant: imports the reader (TYPE_CHECKING only), the
derivations layer (the state-at derivation), fabulexa_forge.anchor, the
sibling base.plan module (TYPE_CHECKING only), and stdlib. Never imports
exporters.dimensional.*, exporters.source.*, or exporters.streaming.*.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.exporters.base.plan import BaseTableSpec
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge.anchor import render_anchor_timestamp_expr
from fabulexa_forge.derivations.state_at import (
    STATE_AT_COLUMNS,
    build_state_at_end_sql,
    build_state_at_sql,
)

#: The `records__<kind>` name prefix a base spec's kind is read against.
_RECORDS_PREFIX = "records__"

#: The bare property-name prefix on a records-category column.
_PROP_PREFIX = "prop__"

#: State-at columns rendered verbatim — the derivation's own native
#: passthrough (`record_id`) or computed value (`active`); never wallclock,
#: never CAST back.
_VERBATIM_COLUMNS: frozenset[str] = frozenset({"record_id", "active"})

#: State-at columns rendered wallclock through the anchor renderer (or raw
#: sim-time ns, when `anchor` is None).
_WALLCLOCK_COLUMNS: frozenset[str] = frozenset({"created_sim_time", "deactivated_at"})


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
    VARCHAR to their sidecar types (as source's snapshot render does); every
    column is projected under `spec.column_renames` (including `record_id ->
    id`). Never uses the compile-indirection (`base_relations`) wrapping.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        spec: The resolved per-kind flat-output shape from `build_base_plan`.
        anchor: The resolved effective anchor, or None to emit raw sim-time ns.
        horizon_ns: The exclusive reconstruction horizon — `T + 1` for
            `slice_at: T`, a window's `end_ns` under incremental — or None for
            the tape's end.

    Returns:
        A complete SELECT producing the flat table, ordered by
        `(created_sim_time, record_id)` (raw, never a rendered timestamp).

    Raises:
        TableNotFoundError: `records__<kind>` is absent (propagated from
            state-at).
    """
    state_at_sql = (
        build_state_at_sql(sidecar, fork_path, spec.kind, spec.properties, horizon_ns)
        if horizon_ns is not None
        else build_state_at_end_sql(sidecar, fork_path, spec.kind, spec.properties)
    )
    col_types = _column_types(sidecar, f"{_RECORDS_PREFIX}{spec.kind}")
    identities = _state_at_column_order(sidecar, spec)

    select_parts: list[str] = []
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

    select_list = ", ".join(select_parts)
    return (
        f'SELECT {select_list} FROM ({state_at_sql}) AS "_base"'
        ' ORDER BY "_base"."created_sim_time", "_base"."record_id"'
    )
