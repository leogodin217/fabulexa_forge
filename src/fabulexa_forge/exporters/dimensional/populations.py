"""Dimensional key election: the destination dim's source population set.

`DimSourcePopulations` / `resolve_dim_source_populations` / `resolve_fk_surface`
are the doc's Dimensional-rendering rule (§ Rendering per mode) as pure
functions, per `docs/sprints/key-election/contracts.md` § 3: an `fk` edge
targets a declared dim *table*, and the identity relation it joins is
restricted to that dim's source population set — the kind's whole population
set, or the single sub-type its `source.filter`'s discriminator conjunct
selects. One resolution, four consumers: FK inheritance resolution
(`fk.py`), the dimensional edge gates and dim-key agreement check
(`validation.py`), and the guard's dim-side leg (`engine.py`).

Layer-direction invariant: imports `config.models` (`KeySurface`, `TableDecl`),
the reader, `derivations.record_index` / `derivations.presentation_key`,
`exporters.election`, and `errors`. Importable by `fk.py`, `validation.py`,
and `engine.py` with no cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fabulexa_forge.config.models import KeySurface, TableDecl
    from fabulexa_forge.exporters.election import Election
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge.derivations.presentation_key import (
    build_presentation_key_at_end_sql,
)
from fabulexa_forge.derivations.record_index import build_record_index_at_end_sql
from fabulexa_forge.errors import ElectionInheritanceAmbiguous, ExportError


@dataclass(frozen=True)
class DimSourcePopulations:
    """The destination dim's source population set, resolved from its
    SourceDecl per doc § Rendering per mode (Dimensional).

    `populations` matches `Election.surface_for`'s sub_type argument shape:
    `(None,)` for a flat kind's whole-table population; the selected
    sub-type singleton when the dim's filter carries a discriminator
    conjunct; the kind's full declared domain otherwise (declaration
    order). `proper_subset` is True iff `populations` is a strict subset of
    the kind's declared domain — the one fact that decides whether a
    restriction spine is composed at all (relation restriction and guard
    legs both key on it), computed once here so no consumer re-derives it.
    """

    kind: str
    populations: tuple[str | None, ...]
    proper_subset: bool


def resolve_dim_source_populations(
    sidecar: "Sidecar",
    source_kind: str,
    source_filter: "Mapping[str, object] | None",
) -> DimSourcePopulations:
    """Resolve a dim's source population set from its kind + filter.

    Implements the doc's set rule verbatim: the filter grammar is an
    equality conjunction over records columns, so at most one conjunct can
    address the synthesized discriminator `prop__<source_kind>_type`; when
    present, its value set — a scalar's singleton, or a list's elements in
    config order — selects exactly those populations (further conjuncts
    narrow rows within the set, never the set); absent, the set is the
    kind's whole population set — the full declared domain for a sub-typed
    kind, the `(None,)` whole-table population for a flat kind. A
    `prop__<kind>_type` conjunct on a kind whose `subtype_values` is empty
    is an ordinary column conjunct (no declared domain means no
    populations to select among) and yields the flat-kind set.

    Pure function of (sidecar, arguments); consulted by FK inheritance
    resolution (`resolve_fk_surface`), the dimensional edge gates
    (`check_edge_union_safety` callers), the FK identity-relation
    restriction, the guard's dim-side leg, and the dim-key agreement check
    — one resolution, five consumers, zero re-derivation.

    Args:
        sidecar: The open emit's sidecar.
        source_kind: The destination dim's `source.kind`.
        source_filter: The dim's `source.filter` mapping, verbatim from the
            TableDecl; None when the dim declares none.

    Returns:
        The resolved population set, populations in selection order.

    Raises:
        ExportError: An element of the discriminator conjunct's value set
            is not a string in the kind's declared domain — evaluated per
            element, naming the offending element. The dim's scope selects
            a population that cannot exist, which on any election-
            consuming path must fail loudly rather than resolve to an
            empty set (Principle #7). (Reachable only when the kind is
            sub-typed.)
    """
    domain = sidecar.subtype_values(source_kind)
    if not domain:
        return DimSourcePopulations(
            kind=source_kind, populations=(None,), proper_subset=False
        )

    discriminator_col = f"prop__{source_kind}_type"
    conjunct = source_filter.get(discriminator_col) if source_filter else None
    if conjunct is None:
        return DimSourcePopulations(
            kind=source_kind, populations=domain, proper_subset=False
        )

    elements = conjunct if isinstance(conjunct, list) else [conjunct]
    for element in elements:
        if not isinstance(element, str) or element not in domain:
            raise ExportError(
                f"dim source kind '{source_kind}': filter.{discriminator_col}="
                f"{element!r} is not a declared sub-type of the kind's discriminator"
                f" domain {list(domain)}"
            )
    populations = cast("tuple[str, ...]", tuple(elements))
    return DimSourcePopulations(
        kind=source_kind,
        populations=populations,
        proper_subset=set(populations) != set(domain),
    )


def resolve_fk_surface(
    election: "Election",
    dim_populations: DimSourcePopulations,
    target_key: "KeySurface | None",
    edge_name: str,
) -> "KeySurface":
    """Resolve one FK edge's single rendered surface.

    The doc's inheritance rule as one pure function so `validate_table`
    (gating) and the render path (`build_fk_expr` callers) consume the
    identical answer: an explicit `target_key` wins per edge; absent, the
    edge inherits the population set's one distinct election
    (`election.surface_for` over `dim_populations.populations`); a set
    carrying more than one distinct election has nothing coherent to
    inherit. Resolution-time only — the author's config value is never
    rewritten. Gating of the resolved surface (registry declaration, union
    safety) is NOT here: callers pass the result to
    `check_edge_union_safety(..., surface_override=<result>)` per the doc's
    contract.

    Args:
        election: The resolved election.
        dim_populations: The destination dim's source population set.
        target_key: The edge's explicit override, verbatim from FkClause;
            None to inherit.
        edge_name: The referencing table · column identity, for the error.

    Returns:
        The edge's one resolved surface ('record_id' when the set carries
        no election and no override is given).

    Raises:
        ElectionInheritanceAmbiguous: `target_key` is None and the set's
            populations elect more than one distinct surface; names
            `edge_name` and the differing (population, surface) pairs.
        KeyError: A population outside the emit's declared domain
            (propagated from `Election.surface_for`; unreachable after
            `resolve_dim_source_populations`, which gates the domain).
    """
    if target_key is not None:
        return target_key

    resolved = [
        (sub_type, election.surface_for(dim_populations.kind, sub_type))
        for sub_type in dim_populations.populations
    ]
    surfaces = {surface for _, surface in resolved}
    if len(surfaces) > 1:
        pairs = ", ".join(f"{sub_type}={surface}" for sub_type, surface in resolved)
        raise ElectionInheritanceAmbiguous(
            f"{edge_name}: destination dim's source population set for kind"
            f" '{dim_populations.kind}' elects differing surfaces ({pairs}) —"
            " nothing coherent to inherit; unify the election, filter the dim"
            " to a single sub-type, or set an explicit target_key on the edge"
        )
    return resolved[0][1]


def dim_population_sub_types(dim_populations: DimSourcePopulations) -> tuple[str, ...]:
    """The population set's discriminator values, `()` for a flat kind.

    Mirrors `sidecar.subtype_values`'s empty-tuple convention for a flat
    kind's whole-table population (`DimSourcePopulations.populations ==
    (None,)`), so `check_edge_union_safety`'s `Sequence[str]` domain
    argument and `build_population_spine_sql`'s `sub_types` accept the set
    directly.

    Args:
        dim_populations: The resolved population set.

    Returns:
        `()` for a flat kind's whole-table population; `dim_populations.
        populations` verbatim (typed `tuple[str, ...]`) otherwise.
    """
    if dim_populations.populations == (None,):
        return ()
    return cast("tuple[str, ...]", dim_populations.populations)


def dim_key_projects_surface(table_decl: "TableDecl", surface: "KeySurface") -> bool:
    """Whether a dim's declared key sources a column from: the given surface.

    The dim-key agreement condition (doc § Dim-key agreement): the
    destination dim's declared `key` must include a column whose
    declaration projects the elected surface (`from:` the surface's
    contract column name — `record_index` or `presentation_id`) directly
    off the dim's own records grain. Shared by `check_dim_key_agreement`
    (which raises when this is False for an inherited non-default surface)
    and the engine's dim-side guard leg (which includes a dim exactly when
    this is True, inherited or explicit).

    Args:
        table_decl: The destination dim's output table declaration.
        surface: The surface to test for.

    Returns:
        True iff some declared key column's `from_` equals `surface`.
    """
    key_set = set(table_decl.key)
    return any(
        col.name in key_set and col.from_ == surface for col in table_decl.columns
    )


def dim_identity_relation_at_end_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    surface: "Literal['record_index', 'presentation_id']",
) -> str:
    """The dimensional mode's identity-relation entry point for one surface.

    Dimensional is horizonless — the shipped FK resolution is slice-state —
    so every identity relation it composes, the FK render's restricted join
    (`fk.py`) and the guard's checked relation (`engine.py`) alike, uses the
    end-of-tape entry point, never a horizon. One dispatch, reused by both,
    so the two agree by construction.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        kind: The records kind whose identity relation to build.
        surface: The non-`record_id` surface to build the relation for.

    Returns:
        A complete SELECT producing (record_id, `surface`).

    Raises:
        TableNotFoundError: `records__<kind>` is absent from the sidecar.
        ExportError: `surface` is presentation_id and the kind's table
            declares no presentation_id column.
    """
    if surface == "record_index":
        return build_record_index_at_end_sql(sidecar, fork_path, kind)
    return build_presentation_key_at_end_sql(sidecar, fork_path, kind)
