"""Shared `keys:` proposal primitives for the cross-mode `init` engines.

The key-election `init` contract (docs/architecture/key-election.md §
`init` proposals) is one natural rule — declared population -> presentation_id,
undeclared -> record_index — shared verbatim by every mode's proposal engine.
Both dimensional's dims and source's `state` tables (since source's
per-sub-type split became `init`'s default) propose only single-population
output tables, so the one gate either mode's proposal needs is edge safety —
`check_edge_union_safety` over the sidecar's reference graph, never the
identity-mixing gate (`check_identity_election`), which only ever fires for a
table spanning more than one population of a kind. This module holds the
mode-neutral pieces — natural-surface computation, the reference graph, the
shared self-gate, the `ExportConfig.keys`-shaped assembly, and the block
writer — so each mode's own `init` module composes them rather than
reimplementing the shared half.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Iterable

from fabulexa_forge.errors import ElectionUnionUnsafe
from fabulexa_forge.exporters.election import check_edge_union_safety, resolve_election
from fabulexa_forge.reader.sidecar import TableSpec

if TYPE_CHECKING:
    from fabulexa_forge.config.models import KeySurface
    from fabulexa_forge.reader.sidecar import PresentationKeys, Sidecar


def domains_for_kinds(
    sidecar: "Sidecar", kinds: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    """Every named kind's declared sub-type domain, `()` for a flat kind.

    Args:
        sidecar: The open emit's sidecar.
        kinds: The kinds the caller's proposal covers.

    Returns:
        kind -> `sidecar.subtype_values(kind)`, over `kinds`.
    """
    return {kind: sidecar.subtype_values(kind) for kind in kinds}


def population_declared(
    presentation_keys: "PresentationKeys | None", kind: str, sub_type: str | None
) -> bool:
    """Whether one population carries a `presentation_keys` declaration.

    A flat kind's `key` entry, or a partitioned kind's per-sub-type entry
    (`key_for`) — presence alone, independent of the kind's rollup claim
    (a rollup with no claim still leaves each individually-declared
    sub-type's own entry present).

    Args:
        presentation_keys: The open emit's `presentation_keys` view, or None.
        kind: The population's kind.
        sub_type: The population's discriminator value, or None for a flat kind.

    Returns:
        True iff the population has its own registry entry.
    """
    if presentation_keys is None or kind not in presentation_keys.kinds():
        return False
    try:
        if sub_type is None:
            presentation_keys.key(kind)
        else:
            presentation_keys.key_for(kind, sub_type)
    except (KeyError, ValueError):
        return False
    return True


def natural_expanded_surfaces(
    presentation_keys: "PresentationKeys | None",
    domains: "dict[str, tuple[str, ...]]",
) -> "dict[tuple[str, str | None], KeySurface]":
    """The doc's natural per-population proposal: declared -> presentation_id,
    undeclared -> record_index — total over every population `domains` covers.

    Args:
        presentation_keys: The open emit's `presentation_keys` view, or None.
        domains: Every proposed kind's sub-type domain, from `domains_for_kinds`.

    Returns:
        (kind, sub_type) -> the natural election, one entry per population.
    """
    expanded: "dict[tuple[str, str | None], KeySurface]" = {}
    for kind, domain in domains.items():
        sub_types: tuple[str | None, ...] = domain if domain else (None,)
        for sub_type in sub_types:
            expanded[(kind, sub_type)] = (
                "presentation_id"
                if population_declared(presentation_keys, kind, sub_type)
                else "record_index"
            )
    return expanded


def build_keys_config(
    expanded: "dict[tuple[str, str | None], KeySurface]",
    domains: "dict[str, tuple[str, ...]]",
) -> "dict[str, KeySurface | dict[str, KeySurface]]":
    """The config `keys` block shape from an expanded per-population map.

    Mirrors the registry's own shape (doc § `init` proposals): a flat kind
    proposes the scalar; a partitioned kind the per-sub-type map, collapsed
    to the scalar when every sub-type agrees.

    Args:
        expanded: (kind, sub_type) -> elected surface, total over `domains`.
        domains: Every proposed kind's sub-type domain.

    Returns:
        The `ExportConfig.keys`-shaped proposal.
    """
    config: "dict[str, KeySurface | dict[str, KeySurface]]" = {}
    for kind, domain in domains.items():
        if not domain:
            config[kind] = expanded[(kind, None)]
            continue
        sub_map: "dict[str, KeySurface]" = {
            sub_type: expanded[(kind, sub_type)] for sub_type in domain
        }
        values = set(sub_map.values())
        config[kind] = next(iter(values)) if len(values) == 1 else sub_map
    return config


def reference_edges(all_tables: tuple[TableSpec, ...]) -> list[tuple[str, str, str]]:
    """Every `references` column across every records table — the reference graph.

    Args:
        all_tables: All sidecar TableSpec objects.

    Returns:
        (source_kind, column_name, target_kind) triples, in sidecar order.
    """
    edges: list[tuple[str, str, str]] = []
    for table in all_tables:
        if not isinstance(table, TableSpec):
            continue
        if not table.name.startswith("records__"):
            continue
        kind = table.name[len("records__") :]
        for col in table.columns:
            if col.references:
                edges.append((kind, col.name, col.references))
    return edges


def self_gate_edge_safety(
    sidecar: "Sidecar",
    all_tables: tuple[TableSpec, ...],
    domains: "dict[str, tuple[str, ...]]",
    expanded: "dict[tuple[str, str | None], KeySurface]",
) -> "tuple[dict[str, KeySurface | dict[str, KeySurface]], dict[str, str]]":
    """Gate the natural proposal through `resolve_election` + edge union safety.

    Shared by dimensional and source's `init`: `init` runs its own proposal
    through the exact machinery the export would run. Neither mode's
    proposal ever combines two populations of one kind into one output table
    (dimensional's dims never did; source's `state` tables don't since the
    per-sub-type split became `init`'s default), so `check_edge_union_safety`
    over the reference graph is the one gate needed — per `references`
    column, gated against the target kind's full declared domain with no
    `target_key` / surface override. A kind implicated in a failure degrades
    to uniform `record_index` — always passing, by construction. One pass
    suffices: each edge's verdict depends only on its own target kind's
    populations, so degrading the implicated kinds cannot newly break an
    edge that previously passed.

    Args:
        sidecar: The open emit's sidecar.
        all_tables: All sidecar TableSpec objects.
        domains: Every proposed kind's sub-type domain.
        expanded: The natural per-population proposal, mutated in place with
            any degradations.

    Returns:
        (keys_config, degraded) — the gated `ExportConfig.keys`-shaped
        proposal, and kind -> a one-line reason naming the forcing gate, for
        every kind the gate degraded.
    """
    election = resolve_election(sidecar, build_keys_config(expanded, domains))
    degraded: dict[str, str] = {}
    for source_kind, column, target_kind in reference_edges(all_tables):
        if target_kind not in domains:
            continue
        if target_kind in degraded:
            continue
        edge_name = f"{source_kind}.{column}"
        try:
            check_edge_union_safety(
                election,
                target_kind,
                domains[target_kind],
                edge_name,
                surface_override=None,
            )
        except ElectionUnionUnsafe as exc:
            degraded[target_kind] = f"ElectionUnionUnsafe: {exc}"

    if not degraded:
        return build_keys_config(expanded, domains), degraded

    for kind in degraded:
        sub_types: tuple[str | None, ...] = domains[kind] if domains[kind] else (None,)
        for sub_type in sub_types:
            expanded[(kind, sub_type)] = "record_index"
    return build_keys_config(expanded, domains), degraded


def write_keys_block(
    w: Callable[[str], None],
    keys_config: "dict[str, KeySurface | dict[str, KeySurface]]",
    degraded: dict[str, str],
) -> None:
    """Write the proposed `keys:` block, one line per kind (or per sub-type).

    A degraded kind always renders as a scalar `record_index` (uniform
    election collapses by construction) with a trailing comment naming the
    forcing gate.

    Args:
        w: Line-writing callable.
        keys_config: The gated `ExportConfig.keys`-shaped proposal.
        degraded: kind -> reason, for every kind the self-gate forced.
    """
    w("keys:")
    for kind, election in keys_config.items():
        if isinstance(election, dict):
            w(f"  {kind}:")
            for sub_type, surface in election.items():
                w(f"    {sub_type}: {surface}")
        else:
            reason = f"  # NOTE: {degraded[kind]}" if kind in degraded else ""
            w(f"  {kind}: {election}{reason}")
    w("")
