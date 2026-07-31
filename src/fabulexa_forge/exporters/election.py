"""Key election: shared exporter layer.

Resolves the config `keys` block against an emit's sidecar into a typed,
gate-checked `Election` view, and carries the combination gates
(`check_identity_election` / `check_edge_union_safety`) and the two
data-touching pieces of the design — the population spine
(`build_population_spine_sql`) and the render-time uniqueness guard
(`check_elected_key_unique`). Every mode engine imports this module; it never
imports any mode's own package (`exporters.dimensional.*` /
`exporters.source.*` / `exporters.base.*`), mirroring `exporters/query_spec.py`'s
layering (`docs/sprints/key-election/contracts.md` § module placement).

Layer-direction invariant: imports the reader (`Emit`, `Sidecar`,
`reader.relations`, `KeySpace` / `union_safe`), `config.models` (`KeySurface`),
`fabulexa_forge._sql`, `fabulexa_forge.errors`, and the derivations layer's
record-index / presentation-key entry points (for the shared horizon-dispatch
helpers `_record_index_sql` / `_presentation_key_sql`, below). Never imports a
mode package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Mapping, Sequence, cast

if TYPE_CHECKING:
    from fabulexa_forge.config.models import KeySurface
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge._sql import _sql_literal
from fabulexa_forge.derivations.presentation_key import (
    build_presentation_key_at_end_sql,
    build_presentation_key_at_sql,
)
from fabulexa_forge.derivations.record_index import (
    build_record_index_at_end_sql,
    build_record_index_at_sql,
)
from fabulexa_forge.errors import (
    ElectedKeyDuplicate,
    ElectionKindUnknown,
    ElectionMixedIdentity,
    ElectionPresentationUndeclared,
    ElectionSubTypeUnknown,
    ElectionUnionUnsafe,
    ExportError,
)
from fabulexa_forge.reader.relations import build_records_relation_sql
from fabulexa_forge.reader.sidecar import KeySpace, union_safe

_RECORDS_TABLE_PREFIX = "records__"

#: The synthesized key-space identity of the built-in `record_id` surface —
#: the contract's `record_id` class, forge-synthesized rather than
#: registry-declared (doc § The election grammar).
_RECORD_ID_KEY_SPACE = KeySpace(space_class="record_id", prefix=None, width=None)

#: The synthesized key-space identity of the built-in `record_index` surface —
#: the contract's `record_index` class with the empty prefix, incomparable
#: with any non-empty non-digit prefix.
_RECORD_INDEX_KEY_SPACE = KeySpace(space_class="record_index", prefix="", width=0)


@dataclass(frozen=True)
class ElectedPopulation:
    """One population's resolved election.

    `sub_type` is None for a flat kind's whole-table population. `key_space`
    is the surface's key-space identity: the built-in record_id /
    record_index spaces, or the population's registry entry's space for
    presentation_id — the value the combination gates' union-safety
    checks range over.
    """

    kind: str
    sub_type: str | None
    surface: "KeySurface"
    key_space: KeySpace


@dataclass(frozen=True)
class Election:
    """The resolved, gate-checked election for one export invocation.

    Constructed by `resolve_election` only; construction implies the
    resolution gates have passed (kind existence, sub-type existence,
    presentation_id declaration). The combination gates need mode
    knowledge — which tables span several populations, which edges a table
    carries — and run at each mode's plan step through
    `check_identity_election` / `check_edge_union_safety`. Populations
    absent from the config resolve to record_id — the view is total over
    the emit's kinds.
    """

    _by_key: Mapping[tuple[str, str | None], ElectedPopulation]
    _by_kind: Mapping[str, tuple[ElectedPopulation, ...]]
    _sidecar: "Sidecar"

    def surface_for(self, kind: str, sub_type: str | None) -> "KeySurface":
        """The elected surface for a population.

        Args:
            kind: A kind with a declared records table in the emit.
            sub_type: The population's discriminator value, or None for a
                flat kind (and for a sub-typed kind under the uniform-scalar
                shorthand, any declared sub_type resolves identically).

        Returns:
            The elected surface; 'record_id' for any population the config
            does not address.

        Raises:
            KeyError: `kind` has no records table in the emit, or `sub_type`
                is not in the kind's discriminator domain.
        """
        if kind not in self._by_kind:
            raise KeyError(f"election: no records table for kind '{kind}' in the emit")
        try:
            return self._by_key[(kind, sub_type)].surface
        except KeyError:
            raise KeyError(
                f"election: '{sub_type}' is not a declared sub-type of kind '{kind}'"
            ) from None

    def populations_for(self, kind: str) -> tuple[ElectedPopulation, ...]:
        """Every population of a kind with its resolved election.

        One entry for a flat kind; one per declared discriminator-domain
        sub-type for a sub-typed kind, declaration order.

        Args:
            kind: A kind with a declared records table in the emit.

        Returns:
            The kind's populations, resolved.

        Raises:
            KeyError: `kind` has no records table in the emit.
        """
        try:
            return self._by_kind[kind]
        except KeyError:
            raise KeyError(
                f"election: no records table for kind '{kind}' in the emit"
            ) from None

    def is_default(self, kind: str) -> bool:
        """Whether every population of a kind resolves to record_id.

        A kind-local fact — it covers the kind's own identity columns
        only. A table's referencing columns follow their *target*
        populations' elections, so the election-free render fast-path
        test is `is_default` over the kind AND every kind the table's
        referencing columns target (junction owner and member kinds
        included), never this call alone.

        Args:
            kind: A kind with a declared records table in the emit.

        Returns:
            True iff no population of `kind` elects a non-record_id surface.

        Raises:
            KeyError: `kind` has no records table in the emit.
        """
        return all(pop.surface == "record_id" for pop in self.populations_for(kind))


def _records_kind_from_table(table_name: str) -> str | None:
    """The kind name for a `records__<kind>` table, or None for any other table."""
    if not table_name.startswith(_RECORDS_TABLE_PREFIX):
        return None
    return table_name[len(_RECORDS_TABLE_PREFIX) :]


def _kind_domains(sidecar: "Sidecar") -> dict[str, tuple[str, ...]]:
    """Every kind with a declared records table, mapped to its sub-type domain.

    Args:
        sidecar: The open emit's sidecar.

    Returns:
        kind -> declared sub-type domain (`()` for a flat kind), covering
        every `records__<kind>` table the sidecar declares.
    """
    domains: dict[str, tuple[str, ...]] = {}
    for table in sidecar.tables():
        kind = _records_kind_from_table(table.name)
        if kind is not None:
            domains[kind] = sidecar.subtype_values(kind)
    return domains


def _resolve_overrides(
    keys: "dict[str, KeySurface | dict[str, KeySurface]] | None",
    domains: Mapping[str, tuple[str, ...]],
) -> dict[tuple[str, str | None], "KeySurface"]:
    """Apply the config `keys` block's resolution gates, flattened per population.

    The kind-exists and sub-type-exists gates (doc § Static gates); the
    presentation_id-declared gate is applied later, during key-space
    resolution, so it fires only for a population that actually elects
    presentation_id.

    Args:
        keys: The config `keys` block, verbatim.
        domains: Every emit kind's sub-type domain, from `_kind_domains`.

    Returns:
        (kind, sub_type) -> elected surface, for every population an
        override addresses; a scalar on a sub-typed kind expands to every
        domain sub-type (the uniform-scalar shorthand).

    Raises:
        ElectionKindUnknown: A `keys` key names no declared records kind.
        ElectionSubTypeUnknown: A map addresses a flat kind, or a map key is
            outside the kind's discriminator domain.
    """
    overrides: dict[tuple[str, str | None], "KeySurface"] = {}
    if keys is None:
        return overrides
    for kind, election in keys.items():
        if kind not in domains:
            raise ElectionKindUnknown(
                f"keys.{kind}: kind '{kind}' has no declared records table in the emit"
            )
        domain = domains[kind]
        if isinstance(election, dict):
            if not domain:
                raise ElectionSubTypeUnknown(
                    f"keys.{kind}: kind '{kind}' is flat; a per-sub-type map has no "
                    "populations to address"
                )
            for sub_type, surface in election.items():
                if sub_type not in domain:
                    raise ElectionSubTypeUnknown(
                        f"keys.{kind}.{sub_type}: '{sub_type}' is not a declared "
                        f"sub-type of kind '{kind}'"
                    )
                overrides[(kind, sub_type)] = surface
        elif domain:
            for sub_type in domain:
                overrides[(kind, sub_type)] = election
        else:
            overrides[(kind, None)] = election
    return overrides


def _presentation_key_space(
    sidecar: "Sidecar", kind: str, sub_type: str | None
) -> KeySpace:
    """Resolve a population's presentation_id registry entry to its key space.

    Args:
        sidecar: The open emit's sidecar.
        kind: The population's kind.
        sub_type: The population's discriminator value, or None for a flat
            kind.

    Returns:
        The registry entry's key space.

    Raises:
        ElectionPresentationUndeclared: The population has no registry entry
            covering it; the message distinguishes an absent
            presentation_keys block entirely from an uncovered population.
        PresentationKeysInvalidError: The block is present and incoherent
            (propagated from the strict accessor).
    """
    label = kind if sub_type is None else f"{kind}.{sub_type}"
    presentation_keys = sidecar.presentation_keys()
    if presentation_keys is None:
        raise ElectionPresentationUndeclared(
            f"keys.{label} elects presentation_id, but the emit carries no "
            "presentation_keys claims"
        )
    try:
        partition_key = (
            presentation_keys.key(kind)
            if sub_type is None
            else presentation_keys.key_for(kind, sub_type)
        )
    except (KeyError, ValueError) as exc:
        raise ElectionPresentationUndeclared(
            f"keys.{label} elects presentation_id, but the population has no "
            "presentation_keys registry entry"
        ) from exc
    return partition_key.key_space


def _key_space_for_surface(
    sidecar: "Sidecar", kind: str, sub_type: str | None, surface: "KeySurface"
) -> KeySpace:
    """The key-space identity of one population's given surface.

    Args:
        sidecar: The open emit's sidecar.
        kind: The population's kind.
        sub_type: The population's discriminator value, or None for a flat
            kind.
        surface: The surface to resolve — built-in surfaces synthesize their
            space; presentation_id reads the registry.

    Returns:
        The surface's key space for this population.

    Raises:
        ElectionPresentationUndeclared: `surface` is presentation_id and the
            population has no registry entry.
        PresentationKeysInvalidError: The registry block is present and
            incoherent (propagated).
    """
    if surface == "record_id":
        return _RECORD_ID_KEY_SPACE
    if surface == "record_index":
        return _RECORD_INDEX_KEY_SPACE
    return _presentation_key_space(sidecar, kind, sub_type)


def resolve_election(
    sidecar: "Sidecar",
    keys: "dict[str, KeySurface | dict[str, KeySurface]] | None",
) -> Election:
    """Resolve and gate the config's key election against an emit.

    Pure function of (sidecar, config); consults
    `sidecar.presentation_keys()` — and therefore shares its
    strict-on-read refusal — exactly when some population elects
    presentation_id. `keys=None` resolves to the all-default election.

    Args:
        sidecar: The emit's sidecar view.
        keys: The config `keys` block, verbatim.

    Returns:
        The resolved election, total over the emit's kinds.

    Raises:
        ElectionKindUnknown: A config key names no declared records kind.
        ElectionSubTypeUnknown: A map key is outside the kind's
            discriminator domain, or a map addresses a flat kind.
        ElectionPresentationUndeclared: A population elects presentation_id
            without a registry entry (the uniform-scalar shorthand requires
            every domain sub_type declared); the message names the
            population and whether the block is absent entirely.
        PresentationKeysInvalidError: The registry block is present and
            incoherent (propagated from the strict accessor).
    """
    domains = _kind_domains(sidecar)
    overrides = _resolve_overrides(keys, domains)

    by_key: dict[tuple[str, str | None], ElectedPopulation] = {}
    by_kind: dict[str, tuple[ElectedPopulation, ...]] = {}
    for kind, domain in domains.items():
        sub_types: tuple[str | None, ...] = domain if domain else (None,)
        populations: list[ElectedPopulation] = []
        for sub_type in sub_types:
            surface = overrides.get((kind, sub_type), "record_id")
            key_space = _key_space_for_surface(sidecar, kind, sub_type, surface)
            population = ElectedPopulation(
                kind=kind, sub_type=sub_type, surface=surface, key_space=key_space
            )
            populations.append(population)
            by_key[(kind, sub_type)] = population
        by_kind[kind] = tuple(populations)

    return Election(_by_key=by_key, _by_kind=by_kind, _sidecar=sidecar)


def check_identity_election(
    election: Election,
    kind: str,
    populations: Sequence[str],
    table_name: str,
) -> None:
    """Gate one output table's identity column against its population mix.

    Called by the source and base plan steps for every output table whose
    rows span more than one population of one kind (an unsplit sub-typed
    source table, a base flat table over a sub-typed kind). A
    single-population table needs no call. Dimensional never calls this
    gate: its identity columns are author-declared (`TableDecl.key` +
    `from:`), never election-rendered — its identity discipline is the
    dim-key agreement check and the guard's dim-side leg. Passes
    when every spanned population elects the same surface (one table, one
    identity surface) and — under a uniform presentation_id election — the
    populations' key spaces are pairwise union-safe.

    Args:
        election: The resolved election.
        kind: The table's records kind.
        populations: The discriminator values whose rows the table carries
            (the kind's full declared domain for an unfiltered table).
        table_name: The output table identity, for the error.

    Returns:
        None.

    Raises:
        ElectionMixedIdentity: The spanned populations elect differing
            surfaces; the message names `table_name`, the (population,
            surface) pairs, and the remedy (per-population tables where the
            mode offers them, unifying the election, or no election).
        ElectionUnionUnsafe: A uniform presentation_id election whose key
            spaces contain a pairwise-unsafe pair (bare-counter siblings);
            the message names `table_name`, the pair, and the remedy
            (electing record_index for every population of the kind).
    """
    pop_set = set(populations)
    spanned = [p for p in election.populations_for(kind) if p.sub_type in pop_set]
    if not spanned:
        return

    surfaces = {p.surface for p in spanned}
    if len(surfaces) > 1:
        pairs = ", ".join(f"{p.sub_type}={p.surface}" for p in spanned)
        raise ElectionMixedIdentity(
            f"table '{table_name}': populations of kind '{kind}' elect differing "
            f"identity surfaces ({pairs}) — one table requires one identity "
            "surface; unify the election, split into per-population tables where "
            "the mode offers them, or remove the election"
        )

    if spanned[0].surface != "presentation_id":
        return
    for i, a in enumerate(spanned):
        for b in spanned[i + 1 :]:
            if not union_safe(a.key_space, b.key_space):
                raise ElectionUnionUnsafe(
                    f"table '{table_name}': populations '{a.sub_type}' and "
                    f"'{b.sub_type}' of kind '{kind}' elect presentation_id with "
                    "union-unsafe key spaces — elect record_index for every "
                    "population of the kind"
                )


def check_edge_union_safety(
    election: Election,
    target_kind: str,
    populations: Sequence[str],
    edge_name: str,
    surface_override: "KeySurface | None" = None,
) -> None:
    """Gate one referencing column against its admitted target populations.

    Called by each mode's plan step per referencing column: per reference
    edge, per junction owner column, and per junction member kind.
    `populations` is the target population set the column admits: the
    target kind's full declared domain in source and base (edges are
    kind-targeted; the owner kind's domain for a junction owner column,
    per member kind for a junction member column), the destination dim's
    source population set in dimensional.

    The gated key spaces are the edge's *resolved* surfaces.
    `surface_override=None` (the kind-targeted modes) resolves each
    population through `election`. Dimensional always passes the FK's one
    resolved surface — the inherited election or the explicit
    `target_key` — and every admitted population resolves to it:
    `presentation_id` through the population's registry entry (an
    uncovered population is refused), the built-ins through their
    synthesized spaces. A single-population set, or a mixed set whose
    resolved key spaces are pairwise union-safe, passes.

    Args:
        election: The resolved election.
        target_kind: The referencing column's `references` target kind.
        populations: The admitted target populations' discriminator values
            (the kind's full declared domain for a kind-targeted edge).
        edge_name: The referencing table · column identity, for the error.
        surface_override: The edge's uniformly resolved surface
            (dimensional FKs), or None to resolve each population through
            `election` (kind-targeted edges).

    Returns:
        None.

    Raises:
        ElectionUnionUnsafe: The admitted populations' resolved key spaces
            contain a pairwise-unsafe pair; the message names `edge_name`,
            the pair, and the contract's remedy (per-population targets, or
            a record_index election for the colliding populations).
        ElectionPresentationUndeclared: `surface_override` is
            presentation_id and an admitted population has no registry
            entry; the message names `edge_name` and the population.
        KeyError: `target_kind` has no records table in the emit — a caller
            error: a kind absent from the emit cannot carry an election
            (the kind-exists gate), so callers skip gating such edges and
            render the default verbatim record_id.
    """
    domain = election.populations_for(target_kind)  # raises KeyError

    resolved: list[ElectedPopulation]
    if surface_override is None:
        by_sub_type = {p.sub_type: p for p in domain}
        resolved = [by_sub_type[sub_type] for sub_type in populations]
    else:
        resolved = [
            ElectedPopulation(
                kind=target_kind,
                sub_type=sub_type,
                surface=surface_override,
                key_space=_key_space_for_surface(
                    election._sidecar, target_kind, sub_type, surface_override
                ),
            )
            for sub_type in populations
        ]

    for i, a in enumerate(resolved):
        for b in resolved[i + 1 :]:
            if not union_safe(a.key_space, b.key_space):
                raise ElectionUnionUnsafe(
                    f"{edge_name}: admitted target populations '{a.sub_type}' and "
                    f"'{b.sub_type}' of kind '{target_kind}' have union-unsafe key "
                    "spaces — elect record_index for the colliding populations, or "
                    "split into per-population targets"
                )


def build_population_spine_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    sub_types: Sequence[str],
) -> str:
    """A proper-subset population set's record_id spine, for semi-join use.

    Composes the reader's faithful records relation
    (`build_records_relation_sql` — reader-first, Principle #10; never a raw
    table name) and projects `record_id` filtered to the records-spine
    discriminator. The discriminator is read from the records spine, never a
    fold after-image (doc § Per-row population resolution): a row's
    discriminator is a per-record constant, so the spine is temporally
    honest at any horizon — one spine serves every horizon a render
    composes, which is why the function takes no horizon parameter. Values
    render as SQL string literals with embedded single quotes doubled;
    `sub_types` order is preserved verbatim.

    Callers pass proper subsets only: the full domain needs no restriction
    (the doc's rule), and an empty set restricts to nothing — both are
    caller logic errors, refused rather than silently composed
    (Principle #7).

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from `require_single_branch`.
        kind: A sub-typed records kind (`sidecar.subtype_values(kind)`
            non-empty).
        sub_types: The population set's discriminator values — a non-empty
            proper subset of the kind's declared domain, in declaration
            order.

    Returns:
        A complete SELECT producing a single `record_id` column.

    Raises:
        ExportError: `sub_types` is empty, equals the kind's full declared
            domain, contains a value outside it, or `kind` is not sub-typed
            (`subtype_values` returns `()`).
        TableNotFoundError: `records__<kind>` is absent (propagated from the
            reader relation).
    """
    domain = sidecar.subtype_values(kind)
    if not domain:
        raise ExportError(f"build_population_spine_sql: kind '{kind}' is not sub-typed")
    if not sub_types:
        raise ExportError(
            f"build_population_spine_sql: sub_types must not be empty for kind '{kind}'"
        )
    domain_set = set(domain)
    for value in sub_types:
        if value not in domain_set:
            raise ExportError(
                f"build_population_spine_sql: '{value}' is not a declared "
                f"sub-type of kind '{kind}'"
            )
    if set(sub_types) == domain_set:
        raise ExportError(
            f"build_population_spine_sql: sub_types equals kind '{kind}''s full "
            "declared domain; the full domain needs no spine restriction"
        )

    relation_sql = build_records_relation_sql(sidecar, fork_path, kind, {})
    values = ", ".join(_sql_literal(value) for value in sub_types)
    return (
        'SELECT "record_id" FROM ('
        f"{relation_sql}"
        f') AS "_spine" WHERE "_spine"."prop__{kind}_type" IN ({values})'
    )


def _record_index_sql(
    sidecar: "Sidecar", fork_path: str, kind: str, horizon_ns: int | None
) -> str:
    """Compose the record-index resident for one kind at a render's horizon
    selection. Shared by every mode's render module and recomputed by each
    mode's engine to guard the exact relation the render embeds — the two
    computations cannot disagree, being pure functions of their arguments
    (`docs/sprints/key-election/contracts.md` § module placement).

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
    """Compose the presentation-key resident for one kind at a render's
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


def _identity_case_expr(
    discriminator_expr: str,
    per_population: "tuple[tuple[str | None, KeySurface], ...]",
    exprs: Mapping["KeySurface", str],
) -> str:
    """Build the per-row CASE dispatch choosing one population's surface value.

    Mirrors `exporters.source.renders._population_case_expr` in shape
    (private to that module; not imported here — election never imports a
    mode package, so this is a deliberate, temporary duplication until
    Phase 3 rebuilds renders.py atop `build_identity_translation_sql`). A
    flat (single, `sub_type=None`) population needs no CASE — its lone
    surface applies unconditionally.

    Args:
        discriminator_expr: The qualified `prop__<kind>_type` expression to
            dispatch on; unused for a flat population.
        per_population: The kind's gated per-population election.
        exprs: Surface -> the value expression to select for that surface.

    Returns:
        A bare SQL value expression (a CASE, or the single arm unconditionally).
    """
    if len(per_population) == 1 and per_population[0][0] is None:
        return exprs[per_population[0][1]]
    arms = []
    for sub_type, surface in per_population:
        assert sub_type is not None, "a multi-population set is always sub-typed"
        arms.append(
            f"WHEN {discriminator_expr} = {_sql_literal(sub_type)}"
            f" THEN {exprs[surface]}"
        )
    return "CASE " + " ".join(arms) + " END"


def build_identity_translation_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    per_population: "tuple[tuple[str | None, KeySurface], ...]",
) -> str:
    """One kind's record_id -> elected-surface translation relation.

    A two-column relation `(record_id, elected_value)`, one row per record
    of `kind` restricted to the listed populations: per population, the
    elected surface's value — record_id verbatim, record_index
    digit-rendered, presentation_id via the presentation-key derivation —
    resolved per row through the records-spine discriminator when the
    listed populations elect differing surfaces (the per-row
    mixed-election device the design doc's event log requires).
    `elected_value` is always VARCHAR — the union-safe common carrier; a
    caller needing a typed column (a uniform-surface item_id) CASTs the
    joined value to its resolved rendered type. Horizon-free: elected
    surfaces are creation-constant, so no as-of position exists to pass.

    Composes `_record_index_sql` / `_presentation_key_sql` and the
    records-spine read; a `per_population` uniformly electing 'record_id'
    still composes (identity projection) so callers need no special case.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch.
        kind: The target kind (must carry a `records__<kind>` table).
        per_population: (sub_type, surface) pairs — the populations rows
            may resolve to, each with its gated elected surface. A flat
            kind passes the single (None, surface) pair.

    Returns:
        The relation SELECT (composable as a subquery / CTE body).

    Raises:
        TableNotFoundError: `records__<kind>` is absent (propagated).
    """
    surfaces = {surface for _, surface in per_population}
    records_sql = build_records_relation_sql(sidecar, fork_path, kind, {})
    discriminator_expr = f'"_rec"."prop__{kind}_type"'
    exprs: Mapping["KeySurface", str] = {
        "record_id": 'CAST("_rec"."record_id" AS VARCHAR)',
        "record_index": 'CAST("_idx"."record_index" AS VARCHAR)',
        "presentation_id": 'CAST("_pid"."presentation_id" AS VARCHAR)',
    }
    value_sql = _identity_case_expr(discriminator_expr, per_population, exprs)

    joins = ""
    if "record_index" in surfaces:
        index_sql = _record_index_sql(sidecar, fork_path, kind, None)
        joins += (
            f' LEFT JOIN ({index_sql}) AS "_idx"'
            ' ON "_rec"."record_id" = "_idx"."record_id"'
        )
    if "presentation_id" in surfaces:
        presentation_sql = _presentation_key_sql(sidecar, fork_path, kind, None)
        joins += (
            f' LEFT JOIN ({presentation_sql}) AS "_pid"'
            ' ON "_rec"."record_id" = "_pid"."record_id"'
        )

    flat = len(per_population) == 1 and per_population[0][0] is None
    where_clause = ""
    if not flat:
        values = ", ".join(
            _sql_literal(sub_type)
            for sub_type, _ in per_population
            if sub_type is not None
        )
        where_clause = f" WHERE {discriminator_expr} IN ({values})"

    return (
        f'SELECT "_rec"."record_id" AS "record_id", {value_sql} AS "elected_value"'
        f' FROM ({records_sql}) AS "_rec"{joins}{where_clause}'
    )


def check_elected_key_unique(
    emit: "Emit",
    relation_sql: str,
    surface: Literal["record_index", "presentation_id"],
    population_spine_sql: str | None,
    context_label: str,
) -> None:
    """Assert one composed identity relation is a bijection on its consumed set.

    The render-time uniqueness guard (doc § The elected-key uniqueness guard;
    business rule ElectedKeyUnique). Executes exactly one aggregate query over
    the emit and passes iff the row count, the count of distinct record_id,
    and the count of distinct elected values are all equal, with the elected
    value never NULL. The check ranges over the join relation, never output
    rows. Deterministic: no sampling, no thresholds; a pure function of
    (emit, arguments).

    The elected column name is not a separate parameter: both identity
    relations project exactly (record_id, <surface>) under the surface's
    contract column name (RECORD_INDEX_COLUMNS / PRESENTATION_KEY_COLUMNS),
    so `surface` names the counted column AND the surface reported in the
    error — one value, no drift. `record_id` needs no guard call (verbatim
    structural identity; the doc scopes the guard to non-record_id
    elections), hence the two-member Literal, not KeySurface.

    Args:
        emit: The open emit (the engine's own handle; the guard reads through
            `emit.query` — one row of four counts, no Arrow surface needed).
        relation_sql: The composed identity relation, verbatim — the exact
            SELECT the consuming render joins (the record-index or
            presentation-key derivation entry point at the table's horizon,
            or its end-of-tape entry point for horizonless tables). Callers
            pass the same string they embed in the render SQL; the guard
            never re-derives a relation.
        surface: The elected surface — names the counted column on the
            relation and appears in the error.
        population_spine_sql: A complete SELECT producing a single
            `record_id` column (from `build_population_spine_sql`) that
            enumerates the consuming population set, composed as a semi-join
            restriction; None when the consumer draws from the kind's full
            domain. Never an interpolated predicate fragment — a whole
            relation or nothing.
        context_label: The table or edge identity for the error, rendered by
            the caller (e.g. "orders.id", "fact_ride.driver_id",
            "dim_driver (dim-side leg)", suffixed with the window label under
            an incremental invocation). Free text; the guard never parses it.

    Returns:
        None.

    Raises:
        ElectedKeyDuplicate: The three-way equality fails or an elected value
            is NULL inside the consumed set; the message names
            `context_label`, `surface`, and the four counts (rows, distinct
            record_id, distinct elected value, NULL count) so a corrupted
            emit is diagnosable without re-running.
        RunDatabaseError: The aggregate fails to execute (propagated from
            `emit.query`).
    """
    where_clause = (
        f' WHERE "_rel"."record_id" IN ({population_spine_sql})'
        if population_spine_sql is not None
        else ""
    )
    sql = (
        "SELECT COUNT(*), "
        'COUNT(DISTINCT "record_id"), '
        f'COUNT(DISTINCT "{surface}"), '
        f'COUNT(*) FILTER (WHERE "{surface}" IS NULL) '
        f'FROM ({relation_sql}) AS "_rel"'
        f"{where_clause}"
    )
    rows = emit.query(sql, ())
    row = rows[0]
    row_count = cast(int, row[0])
    distinct_record_id = cast(int, row[1])
    distinct_surface = cast(int, row[2])
    null_count = cast(int, row[3])

    if (
        row_count != distinct_record_id
        or row_count != distinct_surface
        or null_count != 0
    ):
        raise ElectedKeyDuplicate(
            f"{context_label}: elected {surface} key is not a bijection on "
            f"record_id — rows={row_count}, distinct record_id="
            f"{distinct_record_id}, distinct {surface}={distinct_surface}, "
            f"NULL {surface}={null_count}"
        )
