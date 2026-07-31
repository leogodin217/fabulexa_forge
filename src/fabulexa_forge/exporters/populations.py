"""Shared population-set resolver (config population address -> sub-type atoms).

`Population` is the unit the declared-table grammar resolves to — the same
atom key election addresses. `resolve_populations` is presence-driven from
the sidecar: a kind with a declared discriminator domain refines to
per-sub-type atoms, a flat kind resolves to the single `(kind, None)` atom.
Consumed by source mode's `tables` entries and `events` sources alike; not
consumed by election resolution, which keeps its own resolution gates
(`ElectionKindUnknown` / `ElectionSubTypeUnknown`).
"""

from __future__ import annotations

from dataclasses import dataclass

from fabulexa_forge.errors import (
    SourceSubTypesOnFlatKind,
    SourceTableKindUnknown,
    SourceTableSubTypeUnknown,
)
from fabulexa_forge.reader.errors import TableNotFoundError
from fabulexa_forge.reader.sidecar import Sidecar


@dataclass(frozen=True)
class Population:
    """One sub-type atom: (kind, sub_type), sub_type None for a flat kind.

    The unit the declared-table grammar resolves to — the same atom key
    election addresses. Election resolution's richer `ElectedPopulation`
    (the atom plus its resolved surface and key space) is unchanged; it is
    not refactored over this type.
    """

    kind: str
    sub_type: str | None


def resolve_populations(
    sidecar: Sidecar,
    owner: str,
    kind: str,
    sub_types: tuple[str, ...] | None,
) -> tuple[Population, ...]:
    """Resolve a config population address to its sub-type atoms.

    Presence-driven from the sidecar: a kind with a declared discriminator
    domain refines to per-sub-type atoms; a flat kind resolves to the single
    (kind, None) atom. `sub_types` selects an explicit subset of the
    declared domain, in declaration order.

    The Source-prefixed errors surface only on declaration resolution —
    election resolution keeps its own resolution gates
    (`ElectionKindUnknown` / `ElectionSubTypeUnknown`) and is not rerouted
    through this function's error surface.

    Args:
        sidecar: The open emit's typed sidecar.
        owner: The declaring unit's message label, used verbatim as the
            error-message prefix — "table '<name>'" for a tables entry,
            "events source #<n>" (1-based, declaration order) for an
            events source.
        kind: A records kind name.
        sub_types: Explicit sub-type subset, or None for the full set.

    Returns:
        The resolved atoms, discriminator-domain declaration order.

    Raises:
        SourceTableKindUnknown: `kind` has no records table in the sidecar.
        SourceTableSubTypeUnknown: an entry is outside the kind's
            discriminator domain.
        SourceSubTypesOnFlatKind: `sub_types` given for a kind with no
            discriminator domain.
    """
    try:
        sidecar.table(f"records__{kind}")
    except TableNotFoundError as exc:
        raise SourceTableKindUnknown(
            f"{owner}: kind '{kind}' not in this emit"
        ) from exc

    domain = sidecar.subtype_values(kind)

    if not domain:
        if sub_types is not None:
            raise SourceSubTypesOnFlatKind(
                f"{owner}: kind '{kind}' declares no sub-types"
            )
        return (Population(kind=kind, sub_type=None),)

    if sub_types is None:
        return tuple(Population(kind=kind, sub_type=sub_type) for sub_type in domain)

    domain_set = frozenset(domain)
    for sub_type in sub_types:
        if sub_type not in domain_set:
            raise SourceTableSubTypeUnknown(
                f"{owner}: sub_type '{sub_type}' not declared for kind '{kind}'"
            )
    requested = frozenset(sub_types)
    return tuple(
        Population(kind=kind, sub_type=sub_type)
        for sub_type in domain
        if sub_type in requested
    )
