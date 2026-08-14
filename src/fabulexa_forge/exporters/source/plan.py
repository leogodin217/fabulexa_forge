"""Source-mode planning: declared-table resolution over populations.

`build_source_plan` is a pure function of `(emit, config, anchor, election,
windowed)` — every render is a pure function of the returned `SourcePlan`
(`sidecar`, `fork_path`, `anchor`, `windowed`, plus the resolved units), so
`build_source_query_specs(plan, window)` (renders.py / engine.py, a later
step) needs no further data-dependent step except the write-mode dispatch.
Resolves, in order: (0) `SourceConfig.kind_labels` to the ordered (kind,
label) pair tuple, validated against the sidecar's whole kind universe
(`SourceKindLabelUnknown` / `SourceKindLabelCollision`) and threaded onto
every junction unit; (1) every `tables` declaration to populations
(`exporters.populations.resolve_populations`) and its `state` / `junction`
column set (taxonomy classification, `columns` / `rename` selection, the
identity/edge key-election gates); (2) the `events` declaration to its
audited sources (`exporters.source.events` dataclasses), including the
per-source overlap check and the per-item-type edge union-safety gate; (3)
the collision and reserved-name checks over every resolved output name; (4)
the plan-time elected-key uniqueness guard against the open emit — the one
data-dependent step, which is why `build_source_plan` takes the open `Emit`
(design doc § 1 "Guard-move soundness": elected surfaces are
creation-constant, so guarding the full physical tape at plan time covers
every seam, conservatively strict under a windowed ask). `declare_keys`
resolves each `SourceStateTablePlan.keys` via `resolve_state_table_keys`
when `config.source.declare_keys` is true.

Layer-direction invariant: imports the reader, the derivations layer only
via the mode-neutral `election` module, `fabulexa_forge.errors`,
`fabulexa_forge._sql` (`cast_predicate_element`, the `where` constant-cast
seam), the mode-neutral `reserved_names` / `slice_only` / `query_spec`
(`TableKeys`) / `populations` modules, the sibling `source.columns`
(`_PROP_PREFIX`) and `source.events` (`SourceEventSourcePlan`,
`SourceEventLogPlan`) modules, `notices`, `derivations.guard`
(`require_single_branch`), config.models (TYPE_CHECKING only except
`KeySurface`), and stdlib. Never imports exporters.dimensional.* or
exporters.streaming.*.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import (
        ExportConfig,
        KeySurface,
        PredicateValue,
        SourceEventsDecl,
        SourceEventSourceDecl,
        SourceTableDecl,
    )
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import PresentationKeys, Sidecar

from fabulexa_forge._sql import cast_predicate_element
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.errors import (
    ExportError,
    SourceColumnNotAddressable,
    SourceColumnUnresolved,
    SourceEventSourceOverlap,
    SourceHistoryTrackedRequired,
    SourceItemTypeCollision,
    SourceKindLabelCollision,
    SourceKindLabelUnknown,
    SourceNameCollision,
    SourceSliceOnlyRead,
    SourceTableMembershipUnknown,
    SourceUnclassifiedColumn,
    SourceWhereColumnUnresolved,
    SourceWhereNotConstant,
    SourceWhereOnDiscriminator,
    SourceWhereValueUncastable,
)
from fabulexa_forge.exporters.election import (
    Election,
    _presentation_key_sql,
    _record_index_sql,
    build_population_spine_sql,
    check_edge_union_safety,
    check_elected_key_unique,
    check_identity_election,
)
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.populations import Population, resolve_populations
from fabulexa_forge.exporters.query_spec import TableKeys
from fabulexa_forge.exporters.reserved_names import (
    RESERVED_PRESENTATION_COLUMN_NAME,
    is_reserved_column_name,
    is_reserved_table_name,
)
from fabulexa_forge.exporters.slice_only import (
    is_non_exempt_slice_only,
    slice_only_refusal_message,
)
from fabulexa_forge.exporters.source.columns import _PROP_PREFIX, _scalar_properties
from fabulexa_forge.exporters.source.events import (
    SourceEventLogPlan,
    SourceEventSourcePlan,
)
from fabulexa_forge.reader.errors import TableNotFoundError
from fabulexa_forge.reader.records_columns import REF_INDEX_PREFIX, records_column_role
from fabulexa_forge.reader.sidecar import combined_claim

#: The `records__<kind>` name prefix.
_RECORDS_TABLE_PREFIX = "records__"

#: Prefixes/suffixes the presentation-default renamer strips or recognizes.
_ELEM_PREFIX = "elem__"
_MEMBER_PREFIX = "member__"
_MEMBER_KIND_SUFFIX = "__kind"
_MEMBER_ID_SUFFIX = "__id"

#: The identity column's default output name, whatever surface it carries.
_IDENTITY_DEFAULT_OUTPUT = "id"

#: Structural lifecycle columns renamed to their operational default. Neither
#: identity slot (handled separately — always `_IDENTITY_DEFAULT_OUTPUT`) nor
#: `active` / `deactivated_at` (kept verbatim) needs an entry here.
_LIFECYCLE_RENAMES: dict[str, str] = {
    "created_sim_time": "created_at",
    "last_mutation_sim_time": "updated_at",
}

#: The two non-record_id surfaces the plan-time uniqueness guard covers, in a
#: fixed order so a mixed edge's guard calls are deterministic across runs.
_GUARD_SURFACES: tuple[Literal["record_index", "presentation_id"], ...] = (
    "record_index",
    "presentation_id",
)

#: The event log's fixed output column set (design doc § The event log).
_EVENT_LOG_COLUMNS: tuple[str, ...] = (
    "item_type",
    "item_id",
    "event",
    "occurred_at",
    "changes",
)


@dataclass(frozen=True)
class SourceEdgeSurface:
    """One referencing source column's resolved target election(s).

    `target_kinds` names the referencing column's admitted target kind(s): a
    one-element tuple for a reference-valued `prop__<p>` column or a junction
    owner column, whose target kind is fixed; every kind with a declared
    `records__<kind>` table in the emit for a junction member column — member
    kind is per-row, not statically declared, so the closed, data-free
    admitted set is the full universe of kinds a `member__<f>__kind` value
    could legally name. `per_kind_populations` carries, per admitted kind,
    that kind's full declared domain with its resolved election (gated
    pairwise union-safe per kind independently — cross-kind values carry no
    uniqueness claim; `<f>_kind` disambiguates). `rendered_type` is the
    mixed-column type rule's verdict: the common declared type when every
    admitted population (across every admitted kind) agrees, else
    `'VARCHAR'` (record_index values digit-rendered at render time) — a
    junction member column always resolves `'VARCHAR'` when non-default,
    since a `member__<f>__id` column is inherently VARCHAR-typed regardless
    of election."""

    source_column: str
    target_kinds: "tuple[str, ...]"
    per_kind_populations: (
        "tuple[tuple[str, tuple[tuple[str | None, KeySurface], ...]], ...]"
    )
    rendered_type: str


@dataclass(frozen=True)
class SourceWhereEntry:
    """One resolved `where` entry: gate-passed and plan-time-typed."""

    key: str
    """The key as written (source-column or bare form)."""
    source_column: str
    """The base-table column identity (`prop__<p>`) on the subject kind's
    records table."""
    sql_type: str
    """The column's sidecar-declared DuckDB type."""
    value: "str | list[str]"
    """The config value, verbatim — what the rendering authority compiles."""
    typed_values: tuple[object, ...]
    """Per-element `cast_predicate_element` results, config element order —
    the disjointness gate's comparison set (doc § Event-source disjointness)."""


@dataclass(frozen=True)
class SourceStateTablePlan:
    """One resolved `state` table: a declared thing-table over the
    populations of exactly one kind.

    `columns` is final: the records-column taxonomy applied, `columns` /
    `rename` selection resolved, the identity column rewritten to the
    elected surface's contract name (absorption under a presentation_id
    election applied), non-exempt slice_only columns absent, the
    discriminator retained/dropped per the >= 2 populations rule, and —
    under a windowed plan — `last_mutation_sim_time` absent (horizon
    honesty). Source names are base-table column identities (`record_id` /
    `record_index` / `presentation_id` for the identity slot,
    `created_sim_time`, `active`, `deactivated_at`, `prop__<p>`), never fold
    or output names.
    """

    name: str
    """Author-verbatim output table name."""
    kind: str
    """The records kind; the source table is `records__<kind>`."""
    populations: "tuple[Population, ...]"
    """The declared populations, discriminator-domain declaration order.
    A single (kind, None) atom for a flat kind. Drives the render's
    discriminator filter and the declare_keys combined-claim derivation."""
    columns: tuple[tuple[str, str], ...]
    """Ordered (source column, output column) pairs — the table's final
    delivered set."""
    identity_surface: "KeySurface"
    """The table's uniform elected identity surface ('record_id' under no
    election), gated at plan time over exactly `populations`."""
    edge_surfaces: "tuple[SourceEdgeSurface, ...]"
    """One entry per projected reference-valued `prop__<p>` column
    resolving a target with a records table declared in the sidecar,
    `columns` order."""
    keys: TableKeys | None
    """The table's declared keys (§ 4), resolved at plan time; None when
    `declare_keys` is off."""
    where: tuple[SourceWhereEntry, ...] = ()
    """The table's resolved row predicate, `where` declaration order; empty
    when `where` is absent — config absence is already detected at the
    decl. Defaults to empty so existing construction call sites (a table
    with no `where`) need no change; `_build_state_table_plan` always
    passes it explicitly."""


@dataclass(frozen=True)
class SourceJunctionTablePlan:
    """One resolved `junction` table: a declared membership table.

    `columns` is final: the junction naming map applied (`record_id` ->
    `<K>_id`, `joined/left_sim_time` -> `joined_at`/`left_at`,
    `elem__<f>` -> `<f>`, `member__<f>__kind`/`__id` -> `<f>_kind`/`<f>_id`),
    then `columns` / `rename` selection resolved (the owner column always
    present; member pair columns selected independently). Declares no keys
    under declare_keys — the unit carries no keys field.
    """

    name: str
    """Author-verbatim output table name."""
    owner_kind: str
    """The owning kind `<K>`."""
    property: str
    """The membership property `<p>`."""
    source_table: str
    """The sidecar `membership__<K>__<p>` table name (carried verbatim —
    the sidecar owns the name mangling; the plan never re-derives it)."""
    columns: tuple[tuple[str, str], ...]
    """Ordered (source column, output column) pairs — the table's final
    delivered set."""
    edge_surfaces: "tuple[SourceEdgeSurface, ...]"
    """The owner column's entry first (when the owner kind has a declared
    records table), then one per *selected* member field, sidecar column
    order."""
    kind_labels: "tuple[tuple[str, str], ...]"
    """The resolved (kind, label) map for projected `member__<f>__kind`
    column values; identity fall-through. Empty when no labels are
    declared."""
    owner_populations: "tuple[Population, ...]" = ()
    """The unit's addressed owner population set (doc § The parent lookup):
    the owner kind's full declared domain when `sub_types` is absent, else
    the narrowed subset `sub_types` addresses. `where` never narrows this —
    it is value-level, not population-level. Drives the owner column's
    typing (`_resolve_junction_edges`) and the render's owner-narrowing
    semi-join. Defaults to empty so a unit built for pure type
    discrimination (never compiled) needs no change; `_build_junction_table_plan`
    always passes it explicitly."""
    where: "tuple[SourceWhereEntry, ...]" = ()
    """The unit's resolved owner row predicate (doc § The parent lookup),
    `where` declaration order; empty when `where` is absent. Defaults to
    empty for the same reason as `owner_populations`."""


@dataclass(frozen=True)
class SourcePlan:
    """The resolved source plan: everything `build_source_query_specs(plan,
    window)` composes from, and nothing else.

    Carries `sidecar` / `fork_path` / `anchor` (the renders' pure inputs)
    and `windowed` (the shape the plan validated against); carries no Emit
    and no Election — data-dependent guards ran at plan build, election
    facts are baked into the units as resolved surfaces. Compile is
    therefore a pure function of (plan, window).
    """

    sidecar: "Sidecar"
    """The sidecar the plan resolved against (the truncated view's sidecar
    under playback state() — the plan never re-reads the emit)."""
    fork_path: str
    """The sole branch, resolved once via require_single_branch."""
    anchor: "EffectiveAnchor"
    """The resolved wallclock anchor (source requires one)."""
    windowed: bool
    """Which state-render shape the plan validated against; must agree
    with `window` presence at compile (ValueError otherwise)."""
    tables: "tuple[SourceStateTablePlan | SourceJunctionTablePlan, ...]"
    """One unit per `tables` declaration, declaration order."""
    events: "SourceEventLogPlan | None"
    """The event-log unit, or None when no `events` block is declared."""


# ---------------------------------------------------------------------------
# Sidecar-known kinds (the edge-resolution admitted universe)
# ---------------------------------------------------------------------------


def _known_records_kinds(sidecar: "Sidecar") -> tuple[str, ...]:
    """Every kind with a declared `records__<kind>` table, sidecar table order.

    The closed, data-free universe of kinds a junction member field's
    per-row `member__<f>__kind` value could legally name. Independent of
    which kinds the author *declares* a `tables` entry for — a reference to
    an undeclared kind still renders in its elected surface (design doc §
    Populations, "an undeclared kind may still carry an election").

    Args:
        sidecar: The open emit's sidecar.

    Returns:
        Record kinds, in sidecar table-declaration order.
    """
    kinds: list[str] = []
    for table in sidecar.tables():
        if table.category == "records":
            kind = table.record_kind
            assert kind is not None, "records table must declare record_kind"
            kinds.append(kind)
    return tuple(kinds)


def _resolve_kind_labels(
    known_kinds: "tuple[str, ...]",
    kind_labels: "dict[str, str] | None",
) -> tuple[tuple[str, str], ...]:
    """Resolve and validate `SourceConfig.kind_labels` against the sidecar's
    whole kind universe.

    Injectivity beyond "two kinds map to one label" (already refused at
    parse time, `SourceConfig.kind_labels_shape`'s distinct-values rule)
    reduces to one residual case here: a label equal to an *unlabeled*
    kind's own verbatim name — two labeled kinds can never collide with each
    other, since their labels are already pairwise distinct.

    Args:
        known_kinds: Every kind with a declared records table in the emit
            (§ `_known_records_kinds`) — the whole kind universe the
            injectivity check ranges over, not just declared sources.
        kind_labels: The declared `SourceConfig.kind_labels` map, or None.

    Returns:
        The resolved (kind, label) pairs, declaration order; empty when
        `kind_labels` is None.

    Raises:
        SourceKindLabelUnknown: A key names no records kind in the sidecar.
        SourceKindLabelCollision: A label equals an unlabeled kind's own
            name.
    """
    if not kind_labels:
        return ()
    known = frozenset(known_kinds)
    for kind in kind_labels:
        if kind not in known:
            raise SourceKindLabelUnknown(f"kind_labels: kind '{kind}' not in this emit")
    labeled = frozenset(kind_labels)
    for label in kind_labels.values():
        if label in known and label not in labeled:
            raise SourceKindLabelCollision(
                f"kind_labels: label '{label}' collides with kind '{label}'"
            )
    return tuple(kind_labels.items())


# ---------------------------------------------------------------------------
# Election resolution: identity gate + reference/edge gates (kind-targeted)
# ---------------------------------------------------------------------------


def _resolve_table_identity_surface(
    election: Election,
    kind: str,
    populations: "tuple[Population, ...]",
    table_name: str,
) -> "KeySurface":
    """Resolve one declared table's uniform elected identity surface.

    A single-population table (a flat kind, or one addressed sub-type) is
    trivially uniform — no gate needed. A table combining several
    populations of one kind is gated (`check_identity_election`) over
    exactly those populations.

    Args:
        election: The resolved election.
        kind: The table's record kind.
        populations: The table's resolved population set.
        table_name: The unit's output table name, for the gate's error.

    Returns:
        The table's uniform elected surface (`'record_id'` under no
        election).

    Raises:
        ElectionMixedIdentity: The populations elect differing surfaces.
        ElectionUnionUnsafe: A uniform presentation_id election whose
            populations' key spaces contain a pairwise-unsafe pair.
    """
    if len(populations) > 1:
        sub_types = tuple(p.sub_type for p in populations if p.sub_type is not None)
        check_identity_election(election, kind, sub_types, f"table '{table_name}'")
    return election.surface_for(kind, populations[0].sub_type)


def _presentation_id_type(sidecar: "Sidecar", kind: str) -> str:
    """One kind's declared `presentation_id` column DuckDB type.

    Args:
        sidecar: The open emit's sidecar.
        kind: The record kind; its table must carry a presentation_id column
            (unreachable otherwise — the presentation_id-declared gate makes
            an uncovered population unreachable from a gated plan).

    Returns:
        The declared DuckDB type.

    Raises:
        ExportError: The kind's table declares no presentation_id column — a
            caller gating error.
    """
    table = f"{_RECORDS_TABLE_PREFIX}{kind}"
    for col in sidecar.columns(table):
        if col.name == "presentation_id":
            return col.type
    raise ExportError(f"records__{kind} declares no presentation_id column")


def _resolve_single_kind_rendered_type(
    sidecar: "Sidecar",
    target_kind: str,
    per_population: "tuple[tuple[str | None, KeySurface], ...]",
) -> str:
    """Resolve a single-target-kind edge's mixed-column type-rule verdict.

    Args:
        sidecar: The open emit's sidecar.
        target_kind: The edge's one target kind.
        per_population: The target kind's gated per-population election.

    Returns:
        `'VARCHAR'` when every admitted population elects record_id (native
        record-id type — verbatim, unaffected); `'BIGINT'` when uniform
        record_index; the target's declared presentation_id type when
        uniform presentation_id; `'VARCHAR'` for any other mix
        (record_index values digit-rendered at render time).
    """
    surfaces = {surface for _, surface in per_population}
    if surfaces == {"record_id"}:
        return "VARCHAR"
    if surfaces == {"record_index"}:
        return "BIGINT"
    if surfaces == {"presentation_id"}:
        return _presentation_id_type(sidecar, target_kind)
    return "VARCHAR"


def _resolve_single_kind_edge(
    sidecar: "Sidecar",
    election: Election,
    target_kind: str,
    populations: "tuple[Population, ...]",
    source_column: str,
    edge_name: str,
) -> SourceEdgeSurface:
    """Gate and resolve one single-target-kind referencing column over an
    explicit admitted population set.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        target_kind: The referencing column's one target kind.
        populations: The admitted target populations — the kind's full
            declared domain for a reference-annotated `prop__<p>` column
            (`_owner_kind_domain_populations`); the narrowed
            `owner_populations` for a junction owner column (doc § The
            parent lookup), which types the column by the addressed set's
            own agreement rather than always falling back to the kind's
            full domain.
        source_column: The referencing column's source identity.
        edge_name: The referencing table · column identity, for the gate's
            error.

    Returns:
        The resolved `SourceEdgeSurface`, `target_kinds` a one-element tuple.

    Raises:
        ElectionUnionUnsafe: The admitted populations' resolved key spaces
            contain a pairwise-unsafe pair.
    """
    domain = tuple(p.sub_type for p in populations if p.sub_type is not None)
    check_edge_union_safety(election, target_kind, domain, edge_name)
    per_population = tuple(
        (p.sub_type, election.surface_for(target_kind, p.sub_type)) for p in populations
    )
    rendered_type = _resolve_single_kind_rendered_type(
        sidecar, target_kind, per_population
    )
    return SourceEdgeSurface(
        source_column=source_column,
        target_kinds=(target_kind,),
        per_kind_populations=((target_kind, per_population),),
        rendered_type=rendered_type,
    )


def _resolve_member_field_edge(
    sidecar: "Sidecar",
    election: Election,
    known_kinds: "tuple[str, ...]",
    source_column: str,
    edge_name: str,
) -> SourceEdgeSurface:
    """Gate and resolve one junction member field over every admitted kind.

    Gates each admitted kind's own domain independently (cross-kind values
    carry no uniqueness claim — the `<f>_kind` column disambiguates).

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        known_kinds: Every kind with a declared records table in the emit
            (§ `_known_records_kinds`), the member field's admitted set.
        source_column: The member field's `member__<f>__id` source identity.
        edge_name: The referencing table · column identity, for the gates'
            errors.

    Returns:
        The resolved `SourceEdgeSurface`, spanning `known_kinds`.
        `rendered_type` is always `'VARCHAR'` when non-default.

    Raises:
        ElectionUnionUnsafe: Some admitted kind's own domain's resolved key
            spaces contain a pairwise-unsafe pair.
    """
    per_kind: list[tuple[str, tuple[tuple[str | None, "KeySurface"], ...]]] = []
    for kind in known_kinds:
        domain = sidecar.subtype_values(kind)
        check_edge_union_safety(
            election, kind, domain, f"{edge_name} (member kind '{kind}')"
        )
        per_population = tuple(
            (p.sub_type, p.surface) for p in election.populations_for(kind)
        )
        per_kind.append((kind, per_population))
    return SourceEdgeSurface(
        source_column=source_column,
        target_kinds=known_kinds,
        per_kind_populations=tuple(per_kind),
        rendered_type="VARCHAR",
    )


def _resolve_reference_prop_edges(
    sidecar: "Sidecar",
    election: Election,
    source_table: str,
    columns: "tuple[tuple[str, str], ...]",
    known_kinds: frozenset[str],
    table_name: str,
) -> tuple[SourceEdgeSurface, ...]:
    """Resolve every reference-annotated `prop__<p>` column a table carries.

    A property whose target kind has no declared records table in the
    sidecar yields no entry: it cannot carry an election, so the column
    renders its default verbatim record_id with no join needed.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        source_table: The table's `records__<kind>` source.
        columns: The table's final (source, output) column pairs.
        known_kinds: Every kind with a declared records table in the emit.
        table_name: The table's output name, for the gates' errors.

    Returns:
        One `SourceEdgeSurface` per surviving reference-annotated column, in
        `columns` order.

    Raises:
        ElectionUnionUnsafe: A surviving edge's admitted target populations'
            resolved key spaces contain a pairwise-unsafe pair.
    """
    references: dict[str, str] = {
        col.name: col.references
        for col in sidecar.columns(source_table)
        if col.name.startswith(_PROP_PREFIX) and col.references is not None
    }
    edges: list[SourceEdgeSurface] = []
    for src, _out in columns:
        target_kind = references.get(src)
        if target_kind is None or target_kind not in known_kinds:
            continue
        edge_name = f"table '{table_name}'.{src}"
        edges.append(
            _resolve_single_kind_edge(
                sidecar,
                election,
                target_kind,
                _owner_kind_domain_populations(sidecar, target_kind),
                src,
                edge_name,
            )
        )
    return tuple(edges)


def _resolve_junction_edges(
    sidecar: "Sidecar",
    election: Election,
    source_table: str,
    owner_kind: str,
    owner_populations: "tuple[Population, ...]",
    known_kinds: "tuple[str, ...]",
    table_name: str,
    columns: "tuple[tuple[str, str], ...]",
) -> tuple[SourceEdgeSurface, ...]:
    """Resolve a junction table's owner column and every *selected* member field.

    A membership table's owning kind ordinarily has a declared records table
    in the emit; when it does not, the owner column carries no entry — it
    renders its default verbatim record_id with no join needed.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        source_table: The `membership__<K>__<p>` table.
        owner_kind: The owning kind (`<K>`).
        owner_populations: The unit's addressed owner population set (doc §
            The parent lookup) — the owner kind's full declared domain when
            `sub_types` is absent, else the narrowed subset; types the owner
            column by this set's own agreement rather than the kind's full
            domain.
        known_kinds: Every kind with a declared records table in the emit.
        table_name: The table's output name, for the gates' errors.
        columns: The table's final (source, output) column pairs — member
            fields absent here (omitted by `columns` selection) get no edge
            entry.

    Returns:
        The owner column's `SourceEdgeSurface` first (when the owner kind
        has a declared records table), then one per selected member field,
        sidecar column order.

    Raises:
        ElectionUnionUnsafe: The owner kind's, or some member kind's, own
            domain's resolved key spaces contain a pairwise-unsafe pair.
    """
    edges: list[SourceEdgeSurface] = []
    if owner_kind in known_kinds:
        edges.append(
            _resolve_single_kind_edge(
                sidecar,
                election,
                owner_kind,
                owner_populations,
                "record_id",
                f"table '{table_name}'.{owner_kind}_id",
            )
        )
    selected_sources = {src for src, _ in columns}
    for col in sidecar.columns(source_table):
        name = col.name
        if not (name.startswith(_MEMBER_PREFIX) and name.endswith(_MEMBER_ID_SUFFIX)):
            continue
        if name not in selected_sources:
            continue
        field = name[len(_MEMBER_PREFIX) : -len(_MEMBER_ID_SUFFIX)]
        edges.append(
            _resolve_member_field_edge(
                sidecar, election, known_kinds, name, f"table '{table_name}'.{field}_id"
            )
        )
    return tuple(edges)


# ---------------------------------------------------------------------------
# state table: column resolution (candidate set, `columns`, `rename`)
# ---------------------------------------------------------------------------


def _slice_only_notice(owner_label: str, column_name: str) -> Notice:
    """Build the 'slice-only-column-omitted' notice for one unit x column.

    Args:
        owner_label: The declaring unit's message label (`table '<name>'` /
            `events source #<n>`).
        column_name: The omitted `prop__` column name.

    Returns:
        The rendered Notice.
    """
    return Notice(
        code="slice-only-column-omitted",
        message=(
            f"{owner_label}: column '{column_name}' is temporal_class:"
            " slice_only; omitted from the source export"
        ),
    )


def _state_table_candidate_columns(
    sidecar: "Sidecar",
    kind: str,
    identity_surface: "KeySurface",
    windowed: bool,
    table_name: str,
    notice_sink: "NoticeSink",
) -> tuple[tuple[str, str], ...]:
    """The state render's maximal default column set, before `columns` narrowing.

    Every source column classifies through the records-column taxonomy:
    mechanism columns (`fork_path`, `record_index` unless it is the elected
    surface, `ref_index__*`) never become a candidate; the identity slot
    (wherever `record_id` sits in sidecar order) always does, carrying
    `identity_surface` as its source name and `_IDENTITY_DEFAULT_OUTPUT` as
    its output; a standalone `presentation_id` payload column is absorbed
    when it *is* the elected surface; `last_mutation_sim_time` is absent
    under a windowed plan (horizon honesty); non-exempt slice_only columns
    are omitted with one notice each. The discriminator's single-population
    drop rule is deferred to the `columns`-selection step (it is a default a
    `columns` entry can override).

    Args:
        sidecar: The open emit's sidecar.
        kind: The table's record kind.
        identity_surface: The table's gated elected identity surface.
        windowed: Whether the invocation is windowed.
        table_name: The table's output name, for errors/notices.
        notice_sink: Receiver for slice-only-column-omitted notices.

    Returns:
        (source, output) pairs, sidecar column order.

    Raises:
        SourceUnclassifiedColumn: A column matches no records-column
            taxonomy role.
    """
    source_table = f"{_RECORDS_TABLE_PREFIX}{kind}"
    pairs: list[tuple[str, str]] = []
    for col in sidecar.columns(source_table):
        name = col.name
        role = records_column_role(name)
        if role is None:
            raise SourceUnclassifiedColumn(
                f"table '{table_name}': column '{name}' matches no"
                " records-column taxonomy role"
            )
        if role == "identity":
            if name == "record_id":
                pairs.append((identity_surface, _IDENTITY_DEFAULT_OUTPUT))
            continue
        if role == "presentation":
            if identity_surface == "presentation_id":
                continue
            pairs.append((name, name))
            continue
        if role == "lifecycle":
            if name == RESERVED_PRESENTATION_COLUMN_NAME and windowed:
                continue
            pairs.append((name, _LIFECYCLE_RENAMES.get(name, name)))
            continue
        # role == "payload"
        if is_non_exempt_slice_only(sidecar, kind, name):
            notice_sink(_slice_only_notice(f"table '{table_name}'", name))
            continue
        pairs.append((name, name[len(_PROP_PREFIX) :]))
    return tuple(pairs)


def _check_state_column_name(
    name: str,
    identity_surface: "KeySurface",
    windowed: bool,
    all_source_columns: frozenset[str],
    sidecar: "Sidecar",
    kind: str,
    table_name: str,
    *,
    allow_identity: bool,
) -> None:
    """Validate one `columns` / `rename` entry names an addressable state column.

    Args:
        name: The entry's source-column name.
        identity_surface: The table's gated elected identity surface.
        windowed: Whether the invocation is windowed.
        all_source_columns: Every real column name of the kind's records
            table.
        sidecar: The open emit's sidecar.
        kind: The table's record kind.
        table_name: The table's output name, for errors.
        allow_identity: True for `rename` (the identity's rename key is the
            elected surface's contract name); False for `columns` (identity
            is election-governed, never selection-governed).

    Raises:
        SourceColumnUnresolved: `name` is not a real column; is `record_id`
            under a non-record_id election; or is `last_mutation_sim_time`
            under a windowed plan.
        SourceColumnNotAddressable: `name` is a mechanism column
            (`fork_path`, `ref_index__*`, `record_index` when not the
            elected surface), or names the elected identity surface while
            `allow_identity` is False.
        SourceSliceOnlyRead: `name` is a non-exempt slice_only column.
    """
    if name not in all_source_columns:
        raise SourceColumnUnresolved(
            f"table '{table_name}': '{name}' not a column of its source"
        )
    if name == "fork_path" or name.startswith(REF_INDEX_PREFIX):
        raise SourceColumnNotAddressable(
            f"table '{table_name}': '{name}' is not addressable here (a"
            " mechanism column)"
        )
    if name == "record_index" and identity_surface != "record_index":
        raise SourceColumnNotAddressable(
            f"table '{table_name}': '{name}' is not addressable here (a"
            " mechanism column, and not this table's elected identity"
            f" surface '{identity_surface}')"
        )
    if name == identity_surface:
        if allow_identity:
            return
        raise SourceColumnNotAddressable(
            f"table '{table_name}': '{name}' is this table's elected identity"
            " surface; identity is election-governed, not selection-governed"
        )
    if name == "record_id" and identity_surface != "record_id":
        raise SourceColumnUnresolved(
            f"table '{table_name}': 'record_id' is not rendered — this table"
            f" elects '{identity_surface}'"
        )
    if name == RESERVED_PRESENTATION_COLUMN_NAME and windowed:
        raise SourceColumnUnresolved(
            f"table '{table_name}': '{RESERVED_PRESENTATION_COLUMN_NAME}' is"
            " omitted under a windowed export (updated_at is not"
            " reconstructible at a past horizon)"
        )
    if is_non_exempt_slice_only(sidecar, kind, name):
        raise SourceSliceOnlyRead(
            slice_only_refusal_message(table_name, name, "column", kind, name)
        )


def _apply_state_table_columns_decl(
    candidate: tuple[tuple[str, str], ...],
    decl_columns: "tuple[str, ...] | None",
    identity_surface: "KeySurface",
    windowed: bool,
    sidecar: "Sidecar",
    kind: str,
    populations: "tuple[Population, ...]",
    table_name: str,
    all_source_columns: frozenset[str],
    discriminator_col: str | None,
) -> tuple[tuple[str, str], ...]:
    """Narrow the candidate column set to a table's `columns` selection.

    Absent `columns`, every candidate projects except the discriminator,
    which drops at exactly one addressed population (retained at >= 2) —
    the default rule `columns` can override by naming it explicitly.

    Args:
        candidate: The table's maximal default (source, output) pairs.
        decl_columns: The `tables[].columns` entry, or None.
        identity_surface: The table's gated elected identity surface.
        windowed: Whether the invocation is windowed.
        sidecar: The open emit's sidecar.
        kind: The table's record kind.
        populations: The table's resolved population set.
        table_name: The table's output name, for errors.
        all_source_columns: Every real column name of the kind's records
            table.
        discriminator_col: The kind's `prop__<kind>_type` source name, or
            None for a flat kind.

    Returns:
        The narrowed (source, output) pairs, candidate order.

    Raises:
        SourceColumnUnresolved, SourceColumnNotAddressable,
            SourceSliceOnlyRead: Propagated from `_check_state_column_name`.
    """
    selected: frozenset[str] | None = None
    if decl_columns is not None:
        for name in decl_columns:
            _check_state_column_name(
                name,
                identity_surface,
                windowed,
                all_source_columns,
                sidecar,
                kind,
                table_name,
                allow_identity=False,
            )
        selected = frozenset(decl_columns)

    result: list[tuple[str, str]] = []
    for src, out in candidate:
        if src == identity_surface:
            result.append((src, out))
            continue
        if src == discriminator_col:
            keep = src in selected if selected is not None else len(populations) >= 2
        else:
            keep = src in selected if selected is not None else True
        if keep:
            result.append((src, out))
    return tuple(result)


def _apply_state_table_rename(
    columns: tuple[tuple[str, str], ...],
    rename: "dict[str, str] | None",
    identity_surface: "KeySurface",
    windowed: bool,
    sidecar: "Sidecar",
    kind: str,
    table_name: str,
    all_source_columns: frozenset[str],
) -> tuple[tuple[str, str], ...]:
    """Apply a table's `rename` map to its resolved (post-`columns`) pairs.

    A key already present among `columns`' sources resolves directly (this
    covers the identity slot — its rename key is the elected surface's
    contract name, per the design doc). A key absent from `columns` is
    diagnosed via `_check_state_column_name` (with `allow_identity=True`,
    since a rename *may* name the identity) for a specific error when one
    applies, else a generic "not among this table's projected columns".

    Args:
        columns: The table's (source, output) pairs after `columns` selection.
        rename: The `tables[].rename` map, or None.
        identity_surface: The table's gated elected identity surface.
        windowed: Whether the invocation is windowed.
        sidecar: The open emit's sidecar.
        kind: The table's record kind.
        table_name: The table's output name, for errors.
        all_source_columns: Every real column name of the kind's records
            table.

    Returns:
        The renamed (source, output) pairs, `columns` order.

    Raises:
        SourceColumnUnresolved, SourceColumnNotAddressable,
            SourceSliceOnlyRead: Propagated / raised for an unresolved key.
    """
    if rename is None:
        return columns
    sources = {src for src, _ in columns}
    for key in rename:
        if key in sources:
            continue
        _check_state_column_name(
            key,
            identity_surface,
            windowed,
            all_source_columns,
            sidecar,
            kind,
            table_name,
            allow_identity=True,
        )
        raise SourceColumnUnresolved(
            f"table '{table_name}': '{key}' is not among this table's projected columns"
        )
    return tuple((src, rename.get(src, out)) for src, out in columns)


# ---------------------------------------------------------------------------
# junction table: column resolution (candidate set, `columns`, `rename`)
# ---------------------------------------------------------------------------


def _split_member_field_name(name: str) -> str:
    """Resolve a `member__<f>__kind` / `member__<f>__id` column to its output name.

    Args:
        name: A `member__` membership-table column name.

    Returns:
        `<f>_kind` / `<f>_id`.
    """
    rest = name[len(_MEMBER_PREFIX) :]
    if rest.endswith(_MEMBER_KIND_SUFFIX):
        return f"{rest[: -len(_MEMBER_KIND_SUFFIX)]}_kind"
    return f"{rest[: -len(_MEMBER_ID_SUFFIX)]}_id"


def _junction_candidate_columns(
    sidecar: "Sidecar", source_table: str, owner_kind: str
) -> tuple[tuple[str, str], ...]:
    """The junction render's default column set, source -> output.

    Args:
        sidecar: The open emit's sidecar.
        source_table: The `membership__<K>__<p>` table name.
        owner_kind: The owning kind (`<K>`), for the `record_id -> <K>_id`
            rename.

    Returns:
        (source, output) pairs in sidecar column order: `fork_path` dropped,
        `record_id -> <K>_id`, `joined/left_sim_time ->
        joined_at`/`left_at`, `elem__<f> -> <f>`,
        `member__<f>__kind/__id -> <f>_kind`/`<f>_id`.
    """
    pairs: list[tuple[str, str]] = []
    for col in sidecar.columns(source_table):
        name = col.name
        if name == "fork_path":
            continue
        if name == "record_id":
            pairs.append((name, f"{owner_kind}_id"))
        elif name == "joined_sim_time":
            pairs.append((name, "joined_at"))
        elif name == "left_sim_time":
            pairs.append((name, "left_at"))
        elif name.startswith(_ELEM_PREFIX):
            pairs.append((name, name[len(_ELEM_PREFIX) :]))
        else:
            pairs.append((name, _split_member_field_name(name)))
    return tuple(pairs)


def _check_junction_column_name(
    name: str,
    all_source_columns: frozenset[str],
    table_name: str,
    *,
    allow_owner: bool,
) -> None:
    """Validate one `columns` / `rename` entry names an addressable junction column.

    Args:
        name: The entry's source-column name.
        all_source_columns: Every real column name of the membership table.
        table_name: The table's output name, for errors.
        allow_owner: True for `rename` (the owner's rename key is its source
            name `record_id`, whatever surface it carries); False for
            `columns` (the owner always projects — not selection-governed).

    Raises:
        SourceColumnUnresolved: `name` is not a real column.
        SourceColumnNotAddressable: `name` is `fork_path`, or names the
            owner column while `allow_owner` is False.
    """
    if name not in all_source_columns:
        raise SourceColumnUnresolved(
            f"table '{table_name}': '{name}' not a column of its source"
        )
    if name == "fork_path":
        raise SourceColumnNotAddressable(
            f"table '{table_name}': '{name}' is not addressable here (a"
            " mechanism column)"
        )
    if name == "record_id" and not allow_owner:
        raise SourceColumnNotAddressable(
            f"table '{table_name}': '{name}' is the owner column; it always"
            " projects and is not selection-governed"
        )


def _apply_junction_columns_decl(
    candidate: tuple[tuple[str, str], ...],
    decl_columns: "tuple[str, ...] | None",
    table_name: str,
    all_source_columns: frozenset[str],
) -> tuple[tuple[str, str], ...]:
    """Narrow a junction table's candidate columns to its `columns` selection.

    Args:
        candidate: The table's default (source, output) pairs.
        decl_columns: The `tables[].columns` entry, or None.
        table_name: The table's output name, for errors.
        all_source_columns: Every real column name of the membership table.

    Returns:
        The narrowed pairs, candidate order — the owner column always kept.

    Raises:
        SourceColumnUnresolved, SourceColumnNotAddressable: Propagated.
    """
    if decl_columns is None:
        return candidate
    for name in decl_columns:
        _check_junction_column_name(
            name, all_source_columns, table_name, allow_owner=False
        )
    selected = frozenset(decl_columns)
    return tuple(
        (src, out) for src, out in candidate if src == "record_id" or src in selected
    )


def _apply_junction_rename(
    columns: tuple[tuple[str, str], ...],
    rename: "dict[str, str] | None",
    table_name: str,
    all_source_columns: frozenset[str],
) -> tuple[tuple[str, str], ...]:
    """Apply a junction table's `rename` map to its resolved pairs.

    Args:
        columns: The table's (source, output) pairs after `columns` selection.
        rename: The `tables[].rename` map, or None.
        table_name: The table's output name, for errors.
        all_source_columns: Every real column name of the membership table.

    Returns:
        The renamed (source, output) pairs, `columns` order.

    Raises:
        SourceColumnUnresolved, SourceColumnNotAddressable: For an
            unresolved key.
    """
    if rename is None:
        return columns
    sources = {src for src, _ in columns}
    for key in rename:
        if key in sources:
            continue
        _check_junction_column_name(
            key, all_source_columns, table_name, allow_owner=True
        )
        raise SourceColumnUnresolved(
            f"table '{table_name}': '{key}' is not among this table's projected columns"
        )
    return tuple((src, rename.get(src, out)) for src, out in columns)


# ---------------------------------------------------------------------------
# `where` predicate resolution (the constant-column gate, doc § The
# constant-column gate)
# ---------------------------------------------------------------------------


def _column_types(sidecar: "Sidecar", table_name: str) -> dict[str, str]:
    """Map every column of `table_name` to its declared sidecar DuckDB type.

    Args:
        sidecar: The open emit's sidecar.
        table_name: A sidecar table name.

    Returns:
        {column name -> DuckDB type}, in no particular order.
    """
    return {col.name: col.type for col in sidecar.columns(table_name)}


def _where_predicate_elements(value: "str | list[str]") -> list[str]:
    """Normalize a `where` value to its element list, in config order.

    Args:
        value: A scalar (treated as a one-element list) or a list.

    Returns:
        The value's elements, in order.
    """
    return [value] if isinstance(value, str) else list(value)


def _resolve_where_selection(
    sidecar: "Sidecar",
    where: "dict[str, PredicateValue]",
    subject_kind: str,
    key_form: Literal["source_column", "bare"],
    label: str,
) -> tuple[SourceWhereEntry, ...]:
    """The constant-column gate (doc § The constant-column gate): resolve
    every `where` key against the subject kind's payload-property set in the
    unit's key form, gate class and discriminator, and constant-evaluate
    every element's cast. Declaration entry order.

    Args:
        sidecar: The open emit's sidecar.
        where: The declaration's `where` mapping (present; callers skip the
            call when the field is absent).
        subject_kind: The declared kind, or the owner kind for a membership
            unit.
        key_form: 'source_column' (`prop__<p>`, records-backed tables) or
            'bare' (events sources and membership units).
        label: The declaring unit's message label (`table '<name>'` /
            `events source #<n>`).

    Returns:
        The resolved entries, `where` declaration order.

    Raises:
        SourceWhereColumnUnresolved: A key resolves to no payload property.
        SourceWhereNotConstant: A resolved column is tracked / slice_only.
        SourceWhereOnDiscriminator: A key names the discriminator.
        SourceWhereValueUncastable: An element fails its column's cast.
        TemporalClassUnavailableError: A consulted column's class is
            unavailable (C13, reader-owned).
        ExportError: A consulted column's declared type is unrecognized.
    """
    source_table = f"{_RECORDS_TABLE_PREFIX}{subject_kind}"
    bare_names = _scalar_properties(sidecar, source_table)
    discriminator_col = (
        f"{_PROP_PREFIX}{subject_kind}_type"
        if sidecar.subtype_values(subject_kind)
        else None
    )
    col_types = _column_types(sidecar, source_table)

    entries: list[SourceWhereEntry] = []
    for key, value in where.items():
        if key_form == "source_column":
            if not key.startswith(_PROP_PREFIX):
                raise SourceWhereColumnUnresolved(
                    f"{label}: where key '{key}' not a payload property"
                    f" of kind '{subject_kind}'"
                )
            bare_prop = key[len(_PROP_PREFIX) :]
            source_column = key
        else:
            bare_prop = key
            source_column = f"{_PROP_PREFIX}{key}"

        if bare_prop not in bare_names:
            raise SourceWhereColumnUnresolved(
                f"{label}: where key '{key}' not a payload property"
                f" of kind '{subject_kind}'"
            )
        if source_column == discriminator_col:
            raise SourceWhereOnDiscriminator(
                f"{label}: '{key}' is the sub-type discriminator; select"
                " sub-types via sub_types, not where"
            )

        temporal_class = sidecar.temporal_class(source_table, source_column)
        if temporal_class == "tracked":
            raise SourceWhereNotConstant(
                f"{label}: where key '{key}' is temporal_class: tracked;"
                " under a horizon reconstruction its as-of and current"
                " values select different rows — row selection requires a"
                " constant column"
            )
        if temporal_class == "slice_only":
            raise SourceWhereNotConstant(
                f"{label}: where key '{key}' is temporal_class: slice_only;"
                " its past is unknowable, so row selection cannot read it"
            )

        sql_type = col_types[source_column]
        elements = _where_predicate_elements(value)
        typed_values: list[object] = []
        for element in elements:
            try:
                typed_values.append(cast_predicate_element(element, sql_type))
            except ValueError as exc:
                raise SourceWhereValueUncastable(
                    f"{label}: where value '{element}' for '{key}' does not"
                    f" cast to {sql_type}"
                ) from exc

        entries.append(
            SourceWhereEntry(
                key=key,
                source_column=source_column,
                sql_type=sql_type,
                value=value,
                typed_values=tuple(typed_values),
            )
        )
    return tuple(entries)


def _where_value_unobserved_message(
    label: str, key: str, element: str, wholly_unobserved: bool
) -> str:
    """Render one `where`-value-unobserved notice's message.

    Dimensional's shipped `discriminator-value-unobserved` granularity
    (`check_discriminator_value_observed`): a scalar or wholly-unobserved
    list states the unit renders no rows; a partially-covered list's
    unobserved elements take the weaker per-element wording.

    Args:
        label: The declaring unit's message label.
        key: The `where` key as written.
        element: The unobserved element.
        wholly_unobserved: Whether every element of the entry's value is
            unobserved.

    Returns:
        The notice message text.
    """
    if wholly_unobserved:
        return (
            f"{label}: where value '{element}' for '{key}' not observed;"
            " the unit renders no rows"
        )
    return (
        f"{label}: where value '{element}' for '{key}' not observed;"
        " it contributes no rows"
    )


def _check_where_values_observed(
    sidecar: "Sidecar",
    entries: "tuple[SourceWhereEntry, ...]",
    subject_kind: str,
    label: str,
    notice_sink: "NoticeSink",
) -> None:
    """Emit dimensional's `discriminator-value-unobserved` notice per
    out-of-domain `where` element — shipped code, message granularity, and
    element order reused (doc § The constant-column gate; dimensional's
    `check_discriminator_value_observed`). A column with no `enum_domains`
    entry is unchecked. Never an error.

    Args:
        sidecar: The open emit's sidecar.
        entries: The unit's resolved `where` entries.
        subject_kind: The `enum_domains` key.
        label: The declaring unit's message label.
        notice_sink: Receiver for the notices.
    """
    kind_domains = sidecar.enum_domains().get(subject_kind, {})
    for entry in entries:
        bare_prop = entry.source_column[len(_PROP_PREFIX) :]
        observed_values = kind_domains.get(bare_prop, ())
        if not observed_values:
            continue

        elements = _where_predicate_elements(entry.value)
        unobserved = [e for e in elements if e not in observed_values]
        if not unobserved:
            continue

        wholly_unobserved = len(unobserved) == len(elements)
        for element in unobserved:
            notice_sink(
                Notice(
                    code="discriminator-value-unobserved",
                    message=_where_value_unobserved_message(
                        label, entry.key, element, wholly_unobserved
                    ),
                )
            )


# ---------------------------------------------------------------------------
# `tables[]` declaration resolution
# ---------------------------------------------------------------------------


def _build_state_table_plan(
    sidecar: "Sidecar",
    election: Election,
    known_kinds: "tuple[str, ...]",
    decl: "SourceTableDecl",
    windowed: bool,
    declare_keys: bool,
    notice_sink: "NoticeSink",
) -> SourceStateTablePlan:
    """Resolve one `tables[]` records declaration to a `state` table plan.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        known_kinds: Every kind with a declared records table in the emit.
        decl: The declaration (`decl.kind` is not None).
        windowed: Whether the invocation is windowed.
        declare_keys: Whether to resolve `keys` via `resolve_state_table_keys`.
        notice_sink: Receiver for slice-only-column-omitted notices.

    Returns:
        The resolved `SourceStateTablePlan`.

    Raises:
        SourceTableKindUnknown, SourceTableSubTypeUnknown,
            SourceSubTypesOnFlatKind: Population resolution fails.
        SourceUnclassifiedColumn, SourceColumnUnresolved,
            SourceColumnNotAddressable, SourceSliceOnlyRead: Column
            resolution fails.
        ElectionMixedIdentity, ElectionUnionUnsafe: The identity/edge gates
            fail.
        PresentationKeysInvalidError: `declare_keys` and the block is
            present and incoherent.
        SourceWhereColumnUnresolved, SourceWhereNotConstant,
            SourceWhereOnDiscriminator, SourceWhereValueUncastable: `where`
            resolution fails (the constant-column gate).
    """
    assert decl.kind is not None, "a records tables[] declaration carries kind"
    kind = decl.kind
    populations = resolve_populations(
        sidecar, f"table '{decl.name}'", kind, decl.sub_types
    )

    identity_surface = _resolve_table_identity_surface(
        election, kind, populations, decl.name
    )

    candidate = _state_table_candidate_columns(
        sidecar, kind, identity_surface, windowed, decl.name, notice_sink
    )
    source_table = f"{_RECORDS_TABLE_PREFIX}{kind}"
    all_source_columns = frozenset(col.name for col in sidecar.columns(source_table))
    discriminator_col = (
        f"{_PROP_PREFIX}{kind}_type" if sidecar.subtype_values(kind) else None
    )

    columns = _apply_state_table_columns_decl(
        candidate,
        decl.columns,
        identity_surface,
        windowed,
        sidecar,
        kind,
        populations,
        decl.name,
        all_source_columns,
        discriminator_col,
    )
    columns = _apply_state_table_rename(
        columns,
        decl.rename,
        identity_surface,
        windowed,
        sidecar,
        kind,
        decl.name,
        all_source_columns,
    )

    known_kinds_set = frozenset(known_kinds)
    edge_surfaces = _resolve_reference_prop_edges(
        sidecar, election, source_table, columns, known_kinds_set, decl.name
    )

    keys = (
        resolve_state_table_keys(sidecar, kind, populations, identity_surface, columns)
        if declare_keys
        else None
    )

    where = (
        _resolve_where_selection(
            sidecar,
            decl.where,
            kind,
            key_form="source_column",
            label=f"table '{decl.name}'",
        )
        if decl.where is not None
        else ()
    )
    _check_where_values_observed(
        sidecar, where, kind, f"table '{decl.name}'", notice_sink
    )

    return SourceStateTablePlan(
        name=decl.name,
        kind=kind,
        populations=populations,
        columns=columns,
        identity_surface=identity_surface,
        edge_surfaces=edge_surfaces,
        keys=keys,
        where=where,
    )


def _build_junction_table_plan(
    sidecar: "Sidecar",
    election: Election,
    known_kinds: "tuple[str, ...]",
    decl: "SourceTableDecl",
    kind_labels: "tuple[tuple[str, str], ...]",
    notice_sink: "NoticeSink",
) -> SourceJunctionTablePlan:
    """Resolve one `tables[]` membership declaration to a `junction` table plan.

    `decl.sub_types` resolves against the **owner** kind's discriminator
    domain into `owner_populations` (doc § The parent lookup) — the addressed
    owner set the owner column's edge gate and typing range over; absent =
    the owner's full declared domain. `decl.where` resolves against the owner
    kind's payload properties in bare form (the parent lookup, doc §
    Business Rules).

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        known_kinds: Every kind with a declared records table in the emit.
        decl: The declaration (`decl.membership` is not None).
        kind_labels: The resolved `source.kind_labels` map (§
            `_resolve_kind_labels`), carried onto the unit for the render's
            `member__<f>__kind` values.
        notice_sink: Receiver for out-of-domain `where`-value notices.

    Returns:
        The resolved `SourceJunctionTablePlan`.

    Raises:
        SourceTableMembershipUnknown: The membership reference resolves to
            no sidecar table.
        SourceTableSubTypeUnknown, SourceSubTypesOnFlatKind: `sub_types`
            resolution against the owner kind's domain fails.
        SourceColumnUnresolved, SourceColumnNotAddressable: Column
            resolution fails.
        SourceWhereColumnUnresolved, SourceWhereNotConstant,
            SourceWhereOnDiscriminator, SourceWhereValueUncastable: `where`
            resolution fails (the constant-column gate, applied to the owner
            kind).
        ElectionUnionUnsafe: An edge gate fails.
    """
    assert decl.membership is not None, "a membership tables[] declaration carries it"
    owner_kind = decl.membership.kind
    property_name = decl.membership.property
    source_table = f"membership__{owner_kind}__{property_name}"
    try:
        sidecar.table(source_table)
    except TableNotFoundError as exc:
        raise SourceTableMembershipUnknown(
            f"table '{decl.name}': no membership table for"
            f" ({owner_kind}, {property_name})"
        ) from exc

    label = f"table '{decl.name}'"
    owner_populations = resolve_populations(sidecar, label, owner_kind, decl.sub_types)

    candidate = _junction_candidate_columns(sidecar, source_table, owner_kind)
    all_source_columns = frozenset(col.name for col in sidecar.columns(source_table))
    columns = _apply_junction_columns_decl(
        candidate, decl.columns, decl.name, all_source_columns
    )
    columns = _apply_junction_rename(
        columns, decl.rename, decl.name, all_source_columns
    )

    edge_surfaces = _resolve_junction_edges(
        sidecar,
        election,
        source_table,
        owner_kind,
        owner_populations,
        known_kinds,
        decl.name,
        columns,
    )

    where = (
        _resolve_where_selection(
            sidecar, decl.where, owner_kind, key_form="bare", label=label
        )
        if decl.where is not None
        else ()
    )
    _check_where_values_observed(sidecar, where, owner_kind, label, notice_sink)

    return SourceJunctionTablePlan(
        name=decl.name,
        owner_kind=owner_kind,
        property=property_name,
        source_table=source_table,
        columns=columns,
        edge_surfaces=edge_surfaces,
        kind_labels=kind_labels,
        owner_populations=owner_populations,
        where=where,
    )


# ---------------------------------------------------------------------------
# `events` declaration resolution
# ---------------------------------------------------------------------------


def _owner_kind_domain_populations(
    sidecar: "Sidecar", kind: str
) -> "tuple[Population, ...]":
    """A membership source's owner kind's full declared population domain.

    Args:
        sidecar: The open emit's sidecar.
        kind: The owning kind (`<K>`).

    Returns:
        Every declared sub-type atom, discriminator-domain order; the
        single flat atom for an unsplit kind.
    """
    domain = sidecar.subtype_values(kind)
    if not domain:
        return (Population(kind=kind, sub_type=None),)
    return tuple(Population(kind=kind, sub_type=s) for s in domain)


def _validate_event_property_name(
    name: str,
    all_bare_names: frozenset[str],
    sidecar: "Sidecar",
    kind: str,
    owner: str,
) -> None:
    """Validate one `only` / `ignore` entry names a real, addressable property.

    Args:
        name: The bare property name.
        all_bare_names: Every real `prop__` bare property name on the kind's
            records table.
        sidecar: The open emit's sidecar.
        kind: The audited kind.
        owner: The declaring events source's message label.

    Raises:
        SourceColumnUnresolved: `name` is not a real property.
        SourceSliceOnlyRead: `name` is a non-exempt slice_only property.
    """
    if name not in all_bare_names:
        raise SourceColumnUnresolved(f"{owner}: '{name}' not a column of its source")
    prop_col = f"{_PROP_PREFIX}{name}"
    if is_non_exempt_slice_only(sidecar, kind, prop_col):
        raise SourceSliceOnlyRead(
            slice_only_refusal_message(owner, name, "audited property", kind, prop_col)
        )


def _resolve_records_audited_properties(
    sidecar: "Sidecar",
    kind: str,
    only: "tuple[str, ...] | None",
    ignore: "tuple[str, ...] | None",
    owner: str,
    notice_sink: "NoticeSink",
) -> tuple[str, ...]:
    """The audited property set for one records events source.

    Every tracked- and constant-class property (the exempt discriminator
    included despite a slice_only class), narrowed by `only` or
    widened-by-subtraction via `ignore`. Every other non-exempt slice_only
    property is policy-omitted with one notice.

    Args:
        sidecar: The open emit's sidecar.
        kind: The audited kind.
        only: The source's `only` entry, or None.
        ignore: The source's `ignore` entry, or None.
        owner: The declaring events source's message label.
        notice_sink: Receiver for slice-only-column-omitted notices.

    Returns:
        Bare audited property names, sidecar column-declaration order.

    Raises:
        SourceColumnUnresolved, SourceSliceOnlyRead: An `only` / `ignore`
            entry is unresolved.
        TemporalClassUnavailableError: Propagated.
    """
    source_table = f"{_RECORDS_TABLE_PREFIX}{kind}"
    all_bare_set = _scalar_properties(sidecar, source_table)
    candidates: list[str] = []
    for col in sidecar.columns(source_table):
        name = col.name
        if not name.startswith(_PROP_PREFIX):
            continue
        bare = name[len(_PROP_PREFIX) :]
        if is_non_exempt_slice_only(sidecar, kind, name):
            notice_sink(_slice_only_notice(owner, name))
            continue
        candidates.append(bare)

    if only is not None:
        for name in only:
            _validate_event_property_name(name, all_bare_set, sidecar, kind, owner)
        selected = frozenset(only)
        return tuple(c for c in candidates if c in selected)
    if ignore is not None:
        for name in ignore:
            _validate_event_property_name(name, all_bare_set, sidecar, kind, owner)
        excluded = frozenset(ignore)
        return tuple(c for c in candidates if c not in excluded)
    return tuple(candidates)


def _apply_records_property_rename(
    audited: tuple[str, ...],
    rename: "dict[str, str] | None",
    all_bare_names: frozenset[str],
    sidecar: "Sidecar",
    kind: str,
    owner: str,
) -> tuple[tuple[str, str], ...]:
    """Resolve a records source's (bare name, changes output key) pairs via `rename`.

    A key already in the (narrowed) audited set resolves directly. A key
    absent from it is diagnosed via `_validate_event_property_name` for a
    specific error when one applies (not a real property, or a non-exempt
    slice_only property), else refused as narrowed-away by `only` /
    `ignore` with the same "not a column of its source" message.

    Args:
        audited: The source's resolved (narrowed) audited bare names,
            sidecar column-declaration order.
        rename: The source's `rename` map, or None.
        all_bare_names: Every real `prop__` bare property name of the kind.
        sidecar: The open emit's sidecar.
        kind: The audited kind.
        owner: The declaring events source's message label.

    Returns:
        (bare name, output key) pairs, `audited` order; output key equals
        the bare name absent a rename entry.

    Raises:
        SourceColumnUnresolved: A rename key is not a real property, or is
            excluded by `only` / `ignore`.
        SourceSliceOnlyRead: A rename key names a non-exempt slice_only
            property.
    """
    if rename is None:
        return tuple((name, name) for name in audited)
    audited_set = frozenset(audited)
    for key in rename:
        if key in audited_set:
            continue
        _validate_event_property_name(key, all_bare_names, sidecar, kind, owner)
        raise SourceColumnUnresolved(f"{owner}: '{key}' not a column of its source")
    return tuple((name, rename.get(name, name)) for name in audited)


def _apply_membership_field_rename(
    audited: tuple[str, ...],
    rename: "dict[str, str] | None",
    owner: str,
) -> tuple[tuple[str, str], ...]:
    """Resolve a membership source's (bare field, output key) pairs via `rename`.

    Args:
        audited: The source's resolved (narrowed) audited bare field
            names, element-schema order.
        rename: The source's `rename` map, or None.
        owner: The declaring events source's message label.

    Returns:
        (bare field name, output key) pairs, `audited` order; output key
        equals the bare name absent a rename entry. A reference field's
        pair expands at render to `<key>_kind` / `<key>_id`.

    Raises:
        SourceColumnUnresolved: A rename key is not a real field, or is
            excluded by `only` / `ignore`.
    """
    if rename is None:
        return tuple((name, name) for name in audited)
    audited_set = frozenset(audited)
    for key in rename:
        if key in audited_set:
            continue
        raise SourceColumnUnresolved(f"{owner}: '{key}' not a column of its source")
    return tuple((name, rename.get(name, name)) for name in audited)


def _check_source_changes_key_collision(owner: str, keys: tuple[str, ...]) -> None:
    """Enforce SourceNameCollision over one source's resolved `changes` keys.

    A membership reference field's expanded `<key>_kind` / `<key>_id` pair
    is included in `keys` — the check runs over the final, per-source
    `changes` key surface, never the raw (bare, output-key) pairs.

    Args:
        owner: The declaring events source's message label.
        keys: The source's final `changes` keys, in output order.

    Raises:
        SourceNameCollision: Two keys collide.
    """
    counts: dict[str, int] = {}
    for key in keys:
        counts[key] = counts.get(key, 0) + 1
    duplicates = sorted(k for k, count in counts.items() if count > 1)
    if duplicates:
        raise SourceNameCollision(
            f"{owner}: changes key collision: {duplicates}; resolve via rename"
        )


def _membership_field_names(sidecar: "Sidecar", table_name: str) -> tuple[str, ...]:
    """Every element-schema field name of a membership table, bare, first-seen order.

    Args:
        sidecar: The open emit's sidecar.
        table_name: The `membership__<K>__<p>` table.

    Returns:
        Bare field names — a reference field (`member__<f>__kind` /
        `__id`) counted once, at its first-seen column.
    """
    names: list[str] = []
    seen: set[str] = set()
    for col in sidecar.columns(table_name):
        name = col.name
        if name.startswith(_ELEM_PREFIX):
            field = name[len(_ELEM_PREFIX) :]
        elif name.startswith(_MEMBER_PREFIX) and name.endswith(_MEMBER_KIND_SUFFIX):
            field = name[len(_MEMBER_PREFIX) : -len(_MEMBER_KIND_SUFFIX)]
        elif name.startswith(_MEMBER_PREFIX) and name.endswith(_MEMBER_ID_SUFFIX):
            field = name[len(_MEMBER_PREFIX) : -len(_MEMBER_ID_SUFFIX)]
        else:
            continue
        if field not in seen:
            seen.add(field)
            names.append(field)
    return tuple(names)


def _resolve_membership_audited_fields(
    sidecar: "Sidecar",
    table_name: str,
    only: "tuple[str, ...] | None",
    ignore: "tuple[str, ...] | None",
    owner: str,
) -> tuple[str, ...]:
    """The audited field set for one membership events source.

    Args:
        sidecar: The open emit's sidecar.
        table_name: The `membership__<K>__<p>` table.
        only: The source's `only` entry, or None.
        ignore: The source's `ignore` entry, or None.
        owner: The declaring events source's message label.

    Returns:
        Bare audited field names, element-schema order.

    Raises:
        SourceColumnUnresolved: An `only` / `ignore` entry names no field.
    """
    all_fields = _membership_field_names(sidecar, table_name)
    all_fields_set = frozenset(all_fields)
    if only is not None:
        for name in only:
            if name not in all_fields_set:
                raise SourceColumnUnresolved(
                    f"{owner}: '{name}' not a column of its source"
                )
        selected = frozenset(only)
        return tuple(f for f in all_fields if f in selected)
    if ignore is not None:
        for name in ignore:
            if name not in all_fields_set:
                raise SourceColumnUnresolved(
                    f"{owner}: '{name}' not a column of its source"
                )
        excluded = frozenset(ignore)
        return tuple(f for f in all_fields if f not in excluded)
    return all_fields


def _resolve_records_change_edges(
    sidecar: "Sidecar",
    election: Election,
    source_table: str,
    audited_properties: tuple[str, ...],
    known_kinds: frozenset[str],
    owner: str,
) -> tuple[SourceEdgeSurface, ...]:
    """Resolve every audited reference-valued property of a records source.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        source_table: The audited kind's `records__<kind>` table.
        audited_properties: The source's resolved audited set, bare names.
        known_kinds: Every kind with a declared records table in the emit.
        owner: The declaring events source's message label.

    Returns:
        One `SourceEdgeSurface` per audited reference property whose target
        carries a records table in the sidecar, `audited_properties` order.

    Raises:
        ElectionUnionUnsafe: A surviving edge's admitted target populations'
            resolved key spaces contain a pairwise-unsafe pair.
    """
    references: dict[str, str] = {
        col.name: col.references
        for col in sidecar.columns(source_table)
        if col.name.startswith(_PROP_PREFIX) and col.references is not None
    }
    edges: list[SourceEdgeSurface] = []
    for prop in audited_properties:
        src = f"{_PROP_PREFIX}{prop}"
        target_kind = references.get(src)
        if target_kind is None or target_kind not in known_kinds:
            continue
        edges.append(
            _resolve_single_kind_edge(
                sidecar,
                election,
                target_kind,
                _owner_kind_domain_populations(sidecar, target_kind),
                src,
                f"{owner}.{prop}",
            )
        )
    return tuple(edges)


def _membership_reference_fields(
    sidecar: "Sidecar", table_name: str, fields: "tuple[str, ...]"
) -> frozenset[str]:
    """Which of a membership source's fields are reference-valued.

    A reference field is backed by a `member__<f>__kind` / `member__<f>__id`
    column pair; a scalar field by a single `elem__<f>` column. Drives both
    the change-edge resolution and the `changes` key expansion (a reference
    field's pair renders as `<key>_kind` / `<key>_id`).

    Args:
        sidecar: The open emit's sidecar.
        table_name: The `membership__<K>__<p>` table.
        fields: The candidate bare field names.

    Returns:
        The subset of `fields` that are reference-valued.
    """
    names = {col.name for col in sidecar.columns(table_name)}
    return frozenset(
        field
        for field in fields
        if f"member__{field}__kind" in names and f"member__{field}__id" in names
    )


def _membership_changes_keys(
    reference_fields: frozenset[str],
    audited_pairs: "tuple[tuple[str, str], ...]",
) -> tuple[str, ...]:
    """A membership source's final `changes` key set, pair-expansion applied.

    Args:
        reference_fields: The source's reference-valued bare field names
            (§ `_membership_reference_fields`).
        audited_pairs: The resolved (bare field, output key) pairs.

    Returns:
        `<output>` for a scalar field, `<output>_kind` and `<output>_id`
        for a reference field, `audited_pairs` order.
    """
    keys: list[str] = []
    for field, output in audited_pairs:
        if field in reference_fields:
            keys.append(f"{output}_kind")
            keys.append(f"{output}_id")
        else:
            keys.append(output)
    return tuple(keys)


def _resolve_membership_change_edges(
    sidecar: "Sidecar",
    election: Election,
    table_name: str,
    known_kinds: "tuple[str, ...]",
    audited_fields: tuple[str, ...],
    owner: str,
) -> tuple[SourceEdgeSurface, ...]:
    """Resolve every audited reference field of a membership source.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        table_name: The `membership__<K>__<p>` table.
        known_kinds: Every kind with a declared records table in the emit.
        audited_fields: The source's resolved audited field set, bare names.
        owner: The declaring events source's message label.

    Returns:
        One `SourceEdgeSurface` per audited reference field, audited-field
        order.

    Raises:
        ElectionUnionUnsafe: Propagated from `_resolve_member_field_edge`.
    """
    reference_fields = _membership_reference_fields(sidecar, table_name, audited_fields)
    edges: list[SourceEdgeSurface] = []
    for field in audited_fields:
        if field not in reference_fields:
            continue
        id_col = f"member__{field}__id"
        edges.append(
            _resolve_member_field_edge(
                sidecar, election, known_kinds, id_col, f"{owner}.{field}_id"
            )
        )
    return tuple(edges)


def _resolve_event_source_item_type(
    decl: "SourceEventSourceDecl",
    kind_labels_map: "dict[str, str]",
    kind: str,
    property_name: "str | None",
) -> str:
    """Resolve one events source's item-type: override -> label -> verbatim.

    Args:
        decl: The events source declaration.
        kind_labels_map: The resolved `source.kind_labels` as kind -> label.
        kind: The audited kind (records source) or owner kind `<K>`
            (membership source).
        property_name: The membership property, or None for a records
            source.

    Returns:
        `decl.item_type` verbatim when declared; else the kind's label
        (owner-half-labeled `<label(K)>.<property>` for a membership
        source), or `kind` verbatim when unlabeled.
    """
    if decl.item_type is not None:
        return decl.item_type
    label = kind_labels_map.get(kind, kind)
    if property_name is None:
        return label
    return f"{label}.{property_name}"


def _build_event_source_plan(
    sidecar: "Sidecar",
    election: Election,
    known_kinds_set: frozenset[str],
    known_kinds: "tuple[str, ...]",
    decl: "SourceEventSourceDecl",
    owner: str,
    notice_sink: "NoticeSink",
    kind_labels: "tuple[tuple[str, str], ...]",
) -> SourceEventSourcePlan:
    """Resolve one `events.sources[]` declaration to a `SourceEventSourcePlan`.

    A membership source's `decl.sub_types` resolves against the owner kind's
    discriminator domain exactly as a records source's does (doc § The
    parent lookup) — the narrowed addressed set feeds `item_surface` and the
    downstream overlap / union-safety gates. `decl.where` resolves against
    the subject kind's payload properties in bare form for both source
    shapes (records: the declared kind; membership: the owner kind).

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        known_kinds_set: `known_kinds` as a frozenset (edge admission).
        known_kinds: Every kind with a declared records table, table order
            (junction-member-column admission).
        decl: The events source declaration.
        owner: The declaring source's message label (`events source #<n>`).
        notice_sink: Receiver for slice-only-column-omitted /
            out-of-domain-`where`-value notices.
        kind_labels: The resolved `source.kind_labels` map (§
            `_resolve_kind_labels`) — item-type default resolution and the
            render's `<f>_kind` entry labeling.

    Returns:
        The resolved source, item_surface un-gated (the item-type
        union-safety gate runs separately, over every source sharing a
        resolved item_type).

    Raises:
        SourceTableKindUnknown, SourceTableSubTypeUnknown,
            SourceSubTypesOnFlatKind, SourceTableMembershipUnknown:
            Population resolution fails.
        SourceColumnUnresolved, SourceSliceOnlyRead: An `only` / `ignore` /
            `rename` entry is unresolved.
        SourceNameCollision: Two audited properties (or a membership pair's
            expanded `_kind` / `_id` names) resolve one `changes` key.
        SourceWhereColumnUnresolved, SourceWhereNotConstant,
            SourceWhereOnDiscriminator, SourceWhereValueUncastable: `where`
            resolution fails (the constant-column gate).
        ElectionUnionUnsafe: A change-edge gate fails.
    """
    kind_labels_map = dict(kind_labels)
    if decl.kind is not None:
        kind = decl.kind
        populations = resolve_populations(sidecar, owner, kind, decl.sub_types)
        source_table = f"{_RECORDS_TABLE_PREFIX}{kind}"
        audited = _resolve_records_audited_properties(
            sidecar, kind, decl.only, decl.ignore, owner, notice_sink
        )
        all_bare_names = _scalar_properties(sidecar, source_table)
        audited_pairs = _apply_records_property_rename(
            audited, decl.rename, all_bare_names, sidecar, kind, owner
        )
        _check_source_changes_key_collision(
            owner, tuple(output for _, output in audited_pairs)
        )
        change_edges = _resolve_records_change_edges(
            sidecar, election, source_table, audited, known_kinds_set, owner
        )
        item_surface = tuple(
            (p.sub_type, election.surface_for(kind, p.sub_type)) for p in populations
        )
        item_type = _resolve_event_source_item_type(decl, kind_labels_map, kind, None)
        where = (
            _resolve_where_selection(
                sidecar, decl.where, kind, key_form="bare", label=owner
            )
            if decl.where is not None
            else ()
        )
        _check_where_values_observed(sidecar, where, kind, owner, notice_sink)
        return SourceEventSourcePlan(
            item_type=item_type,
            kind=kind,
            property=None,
            populations=populations,
            audited_properties=audited_pairs,
            kind_labels=kind_labels,
            item_surface=item_surface,
            change_edges=change_edges,
            where=where,
        )

    assert decl.membership is not None, "an events source carries kind or membership"
    owner_kind = decl.membership.kind
    property_name = decl.membership.property
    table_name = f"membership__{owner_kind}__{property_name}"
    try:
        sidecar.table(table_name)
    except TableNotFoundError as exc:
        raise SourceTableMembershipUnknown(
            f"{owner}: no membership table for ({owner_kind}, {property_name})"
        ) from exc

    populations = resolve_populations(sidecar, owner, owner_kind, decl.sub_types)
    audited = _resolve_membership_audited_fields(
        sidecar, table_name, decl.only, decl.ignore, owner
    )
    audited_pairs = _apply_membership_field_rename(audited, decl.rename, owner)
    reference_fields = _membership_reference_fields(sidecar, table_name, audited)
    _check_source_changes_key_collision(
        owner, _membership_changes_keys(reference_fields, audited_pairs)
    )
    change_edges = _resolve_membership_change_edges(
        sidecar, election, table_name, known_kinds, audited, owner
    )
    item_surface = tuple(
        (p.sub_type, election.surface_for(owner_kind, p.sub_type)) for p in populations
    )
    where = (
        _resolve_where_selection(
            sidecar, decl.where, owner_kind, key_form="bare", label=owner
        )
        if decl.where is not None
        else ()
    )
    _check_where_values_observed(sidecar, where, owner_kind, owner, notice_sink)
    return SourceEventSourcePlan(
        item_type=_resolve_event_source_item_type(
            decl, kind_labels_map, owner_kind, property_name
        ),
        kind=owner_kind,
        property=property_name,
        populations=populations,
        audited_properties=audited_pairs,
        kind_labels=kind_labels,
        item_surface=item_surface,
        change_edges=change_edges,
        where=where,
    )


def _population_label(population: Population) -> str:
    """A population atom's message label (`K` or `K.sub_type`)."""
    if population.sub_type is None:
        return population.kind
    return f"{population.kind}.{population.sub_type}"


def _events_share_item_space(
    a: SourceEventSourcePlan, b: SourceEventSourcePlan
) -> bool:
    """Whether two events sources could double-log one item (doc § Event-source
    disjointness): records sources of one kind, or membership sources of one
    `(kind, property)`, whose addressed population sets intersect.

    Owner `sub_types` narrows a membership source's addressed `populations`
    exactly as `sub_types` narrows a records source's — both-declared
    disjoint owner `sub_types` sets already show up here as disjoint
    population sets, so one population-set-intersection test covers both
    source shapes; a records source and a membership source never share an
    item space (different grains).

    Args:
        a: One resolved events source.
        b: Another resolved events source.

    Returns:
        True iff `a` and `b` audit one item space.
    """
    if a.property != b.property or a.kind != b.kind:
        return False
    return not set(a.populations).isdisjoint(b.populations)


def _common_disjoint_where_column(
    a: SourceEventSourcePlan, b: SourceEventSourcePlan
) -> bool:
    """Whether `a` and `b` both declare a `where` entry on a common column
    whose typed value sets are disjoint (doc § Event-source disjointness —
    existential over common columns, entries the sources do not share never
    defeat a disjointness another common column establishes).

    Args:
        a: One resolved events source.
        b: Another resolved events source.

    Returns:
        True iff at least one shared `source_column` carries disjoint
        `typed_values` sets.
    """
    b_by_column = {entry.source_column: entry for entry in b.where}
    for a_entry in a.where:
        b_entry = b_by_column.get(a_entry.source_column)
        if b_entry is not None and set(a_entry.typed_values).isdisjoint(
            b_entry.typed_values
        ):
            return True
    return False


def _first_shared_population(
    a: SourceEventSourcePlan, b: SourceEventSourcePlan
) -> Population:
    """The first population atom (declaration order of `a`) common to both
    sources — for the overlap error's message only; callers already
    confirmed the sets intersect."""
    b_set = set(b.populations)
    for population in a.populations:
        if population in b_set:
            return population
    raise AssertionError("caller already confirmed the population sets intersect")


def _check_events_source_overlap(sources: tuple[SourceEventSourcePlan, ...]) -> None:
    """Enforce selection-aware disjointness across `events.sources` (doc §
    Event-source disjointness): two sources auditing one item space
    (`_events_share_item_space`) are legal only via a common `where` column
    whose typed value sets are disjoint (`_common_disjoint_where_column`) —
    population-disjoint sources (including both-declared disjoint owner
    `sub_types`) never reach the selection check at all.

    Args:
        sources: The resolved events sources, declaration order.

    Raises:
        SourceEventSourceOverlap: Two sources share an item space that no
            declared selection disjoins.
    """
    for index, a in enumerate(sources):
        for b in sources[index + 1 :]:
            if not _events_share_item_space(a, b):
                continue
            if _common_disjoint_where_column(a, b):
                continue
            shared = _first_shared_population(a, b)
            raise SourceEventSourceOverlap(
                f"events: sources overlap on population"
                f" '{_population_label(shared)}'; selections do not"
                " establish disjointness"
            )


def _check_item_type_pairwise_distinctness(
    sources: "tuple[SourceEventSourcePlan, ...]",
) -> None:
    """Enforce SourceItemTypeCollision's pairwise sharing rule.

    Two records sources of one kind may share one resolved item-type (the
    joint union-safety-gate group, today's shape for a kind split across
    sources), as may two membership sources of one `(kind, property)` (the
    same shape, extended to the membership grain — doc § Event-source
    disjointness); any other sharing is refused — two records sources of
    different kinds, two membership sources of differing `(kind, property)`,
    or a records source sharing with a membership source (its own owner's
    included).

    Args:
        sources: The resolved events sources, declaration order (1-based
            index = declaration order).

    Raises:
        SourceItemTypeCollision: Two sources illegally share a resolved
            item-type.
    """
    seen: dict[str, tuple[int, SourceEventSourcePlan]] = {}
    for index, source in enumerate(sources, start=1):
        prior = seen.get(source.item_type)
        if prior is None:
            seen[source.item_type] = (index, source)
            continue
        prior_index, prior_source = prior
        legal = (
            source.property == prior_source.property
            and source.kind == prior_source.kind
        )
        if not legal:
            raise SourceItemTypeCollision(
                f"events: sources #{prior_index} and #{index} resolve one"
                f" item_type '{source.item_type}' over two audited item spaces"
            )


def _check_item_type_rendered_name_collision(
    sources: "tuple[SourceEventSourcePlan, ...]",
    known_kinds: "tuple[str, ...]",
    kind_labels_map: "dict[str, str]",
) -> None:
    """Enforce SourceItemTypeCollision's rendered-kind-name clause.

    Ranges over the emit's whole kind universe (every sidecar records
    kind), not just declared sources or labeled kinds — the same range as
    the label injectivity check, for the same reason.

    Args:
        sources: The resolved events sources, declaration order.
        known_kinds: Every kind with a declared records table in the emit.
        kind_labels_map: The resolved `source.kind_labels` as kind -> label.

    Raises:
        SourceItemTypeCollision: A records source's item-type equals
            another kind's rendered name, or a membership source's
            item-type equals any kind's rendered name (its owner's
            included).
    """
    rendered = {kind: kind_labels_map.get(kind, kind) for kind in known_kinds}
    for index, source in enumerate(sources, start=1):
        for kind, name in rendered.items():
            if source.property is None and kind == source.kind:
                continue
            if source.item_type == name:
                raise SourceItemTypeCollision(
                    f"events source #{index}: item_type '{source.item_type}'"
                    f" collides with kind '{kind}'"
                )


def _check_item_type_distinctness(
    sources: "tuple[SourceEventSourcePlan, ...]",
    known_kinds: "tuple[str, ...]",
    kind_labels: "tuple[tuple[str, str], ...]",
) -> None:
    """Enforce the design doc's whole item-type distinctness table.

    Runs before the per-item-type union-safety gate (§ design doc "no layer
    outranks another — override, label, and verbatim name are one
    vocabulary that must not contradict itself").

    Args:
        sources: The resolved events sources, declaration order.
        known_kinds: Every kind with a declared records table in the emit.
        kind_labels: The resolved `source.kind_labels` map.

    Raises:
        SourceItemTypeCollision: Either distinctness clause fails.
    """
    _check_item_type_pairwise_distinctness(sources)
    _check_item_type_rendered_name_collision(sources, known_kinds, dict(kind_labels))


def _check_item_type_union_safety(
    election: Election, sources: tuple[SourceEventSourcePlan, ...]
) -> None:
    """Gate the event log's `item_id` per item-type over the union of its sources.

    No identity-uniformity gate applies (`item_id` is an edge, not a
    thing-table identity column) — only the edge union-safety gate, and
    never across item-types.

    Args:
        election: The resolved election.
        sources: The resolved events sources (post overlap check — no
            duplicate populations within one item_type group).

    Raises:
        ElectionUnionUnsafe: An item-type's union of addressed populations'
            resolved key spaces contains a pairwise-unsafe pair.
    """
    groups: dict[str, list[SourceEventSourcePlan]] = {}
    for source in sources:
        groups.setdefault(source.item_type, []).append(source)
    for item_type, group in groups.items():
        kind = group[0].kind
        sub_types = tuple(
            p.sub_type
            for source in group
            for p in source.populations
            if p.sub_type is not None
        )
        check_edge_union_safety(
            election, kind, sub_types, f"events log item_type '{item_type}'"
        )


def _resolve_log_item_id_type(
    sidecar: "Sidecar", sources: tuple[SourceEventSourcePlan, ...]
) -> str:
    """Resolve the event log's `item_id` column type over every source's item_surface.

    Args:
        sidecar: The open emit's sidecar.
        sources: The resolved events sources.

    Returns:
        `'VARCHAR'` when every source uniformly elects record_id;
        `'BIGINT'` when uniform record_index; the common presentation_id
        type when every audited/owner kind's presentation_id column agrees;
        `'VARCHAR'` otherwise.
    """
    surfaces: set[str] = set()
    kinds: set[str] = set()
    for source in sources:
        kinds.add(source.kind)
        for _, surface in source.item_surface:
            surfaces.add(surface)
    if surfaces == {"record_id"}:
        return "VARCHAR"
    if surfaces == {"record_index"}:
        return "BIGINT"
    if surfaces == {"presentation_id"}:
        types = {_presentation_id_type(sidecar, kind) for kind in kinds}
        if len(types) == 1:
            return next(iter(types))
    return "VARCHAR"


def _build_event_log_plan(
    sidecar: "Sidecar",
    election: Election,
    known_kinds: "tuple[str, ...]",
    decl: "SourceEventsDecl",
    declare_keys: bool,
    notice_sink: "NoticeSink",
    kind_labels: "tuple[tuple[str, str], ...]",
) -> SourceEventLogPlan:
    """Resolve the `events` declaration to a `SourceEventLogPlan`.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        known_kinds: Every kind with a declared records table in the emit.
        decl: The `events` declaration.
        declare_keys: Whether the log declares `PRIMARY KEY (id)`. Unlike a
            state table's, the log's key resolves nothing from the emit —
            `id` is true by construction — so it is a constant of the mode.
        notice_sink: Receiver for slice-only-column-omitted notices.
        kind_labels: The resolved `source.kind_labels` map (§
            `_resolve_kind_labels`), threaded onto every source.

    Returns:
        The resolved event-log plan.

    Raises:
        SourceTableKindUnknown, SourceTableSubTypeUnknown,
            SourceSubTypesOnFlatKind, SourceTableMembershipUnknown:
            A source's population resolution fails.
        SourceColumnUnresolved, SourceSliceOnlyRead: An `only` / `ignore` /
            `rename` entry is unresolved.
        SourceNameCollision: A source's resolved `changes` keys collide.
        SourceWhereColumnUnresolved, SourceWhereNotConstant,
            SourceWhereOnDiscriminator, SourceWhereValueUncastable: A
            source's `where` resolution fails (the constant-column gate).
        SourceEventSourceOverlap: Two sources sharing an item space are not
            disjoined by declared population or selection (doc §
            Event-source disjointness).
        SourceItemTypeCollision: Two sources illegally share a resolved
            item-type, or a resolved item-type collides with a kind's
            rendered name.
        ElectionUnionUnsafe: An item-type or change-edge gate fails.
    """
    known_kinds_set = frozenset(known_kinds)
    sources = tuple(
        _build_event_source_plan(
            sidecar,
            election,
            known_kinds_set,
            known_kinds,
            source_decl,
            f"events source #{index}",
            notice_sink,
            kind_labels,
        )
        for index, source_decl in enumerate(decl.sources, start=1)
    )
    _check_events_source_overlap(sources)
    _check_item_type_distinctness(sources, known_kinds, kind_labels)
    _check_item_type_union_safety(election, sources)
    item_id_type = _resolve_log_item_id_type(sidecar, sources)
    return SourceEventLogPlan(
        name=decl.name,
        sources=sources,
        item_id_type=item_id_type,
        keys=TableKeys(primary_key=("id",), unique=()) if declare_keys else None,
    )


# ---------------------------------------------------------------------------
# Collision + reserved-name checks (every resolved output table)
# ---------------------------------------------------------------------------


def _output_units(
    tables: "tuple[SourceStateTablePlan | SourceJunctionTablePlan, ...]",
    events: "SourceEventLogPlan | None",
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Every resolved output table's (name, output columns), tables then log.

    Args:
        tables: The resolved `tables[]` units.
        events: The resolved event-log unit, or None.

    Returns:
        (table name, output column names) pairs, plan order.
    """
    units = [(t.name, tuple(out for _, out in t.columns)) for t in tables]
    if events is not None:
        units.append((events.name, _EVENT_LOG_COLUMNS))
    return tuple(units)


def _check_output_collisions(
    tables: "tuple[SourceStateTablePlan | SourceJunctionTablePlan, ...]",
    events: "SourceEventLogPlan | None",
) -> None:
    """Enforce SourceNameCollision: unique table names, unique columns per table.

    Args:
        tables: The resolved `tables[]` units.
        events: The resolved event-log unit, or None.

    Raises:
        SourceNameCollision: Two output tables share a name, or one table's
            columns share an output name.
    """
    units = _output_units(tables, events)
    name_counts: dict[str, int] = {}
    for name, _ in units:
        name_counts[name] = name_counts.get(name, 0) + 1
    duplicate_names = sorted(n for n, count in name_counts.items() if count > 1)
    if duplicate_names:
        raise SourceNameCollision(
            f"output name collision: {duplicate_names}; resolve via rename"
        )

    for name, columns in units:
        col_counts: dict[str, int] = {}
        for out in columns:
            col_counts[out] = col_counts.get(out, 0) + 1
        duplicate_cols = sorted(n for n, count in col_counts.items() if count > 1)
        if duplicate_cols:
            raise SourceNameCollision(
                f"output name collision in table '{name}': {duplicate_cols};"
                " resolve via rename"
            )


def _check_output_reserved_names(
    tables: "tuple[SourceStateTablePlan | SourceJunctionTablePlan, ...]",
    events: "SourceEventLogPlan | None",
) -> None:
    """Enforce the reserved-name rule over every resolved output table.

    Args:
        tables: The resolved `tables[]` units.
        events: The resolved event-log unit, or None.

    Raises:
        ExportError: A table name is `_export_meta` / `_export_windows` or
            ends in `__rows`; a column is named `__valid_from_ns`; or a
            column is named `last_mutation_sim_time`.
    """
    for name, columns in _output_units(tables, events):
        if is_reserved_table_name(name):
            raise ExportError(
                f"table '{name}': name is reserved under incremental export"
            )
        for out in columns:
            if out == RESERVED_PRESENTATION_COLUMN_NAME:
                raise ExportError(
                    f"table '{name}': column '{out}' names the reserved"
                    " last_mutation_sim_time column — it is sim-internal"
                    " bookkeeping; deliver its value via the updated_at"
                    " presentation default or a different rename target"
                )
            if is_reserved_column_name(out):
                raise ExportError(
                    f"table '{name}': column '{out}' is reserved under"
                    " incremental export"
                )


# ---------------------------------------------------------------------------
# Plan-time elected-key uniqueness guard
# ---------------------------------------------------------------------------


def _guard_surface_subset(
    emit: "Emit",
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    subset: tuple[str, ...],
    surface: Literal["record_index", "presentation_id"],
    context_label: str,
) -> None:
    """Guard one (kind, surface) elected relation restricted to a population subset.

    Args:
        emit: The open emit.
        sidecar: The open emit's sidecar.
        fork_path: The sole branch.
        kind: The target kind.
        subset: The consumed sub-type subset; a no-op when empty.
        surface: The elected surface to guard.
        context_label: The table/edge identity, for the error.

    Raises:
        ElectedKeyDuplicate: The elected relation is not a bijection on
            record_id over its consumed set.
    """
    if not subset:
        return
    relation_sql = (
        _record_index_sql(sidecar, fork_path, kind, None)
        if surface == "record_index"
        else _presentation_key_sql(sidecar, fork_path, kind, None)
    )
    domain = set(sidecar.subtype_values(kind))
    spine_sql = (
        build_population_spine_sql(sidecar, fork_path, kind, subset)
        if set(subset) != domain
        else None
    )
    check_elected_key_unique(emit, relation_sql, surface, spine_sql, context_label)


def _guard_table_identity(
    emit: "Emit",
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    populations: "tuple[Population, ...]",
    identity_surface: "KeySurface",
    context_label: str,
) -> None:
    """Guard one state table's own identity relation, when non-`record_id`.

    Args:
        emit: The open emit.
        sidecar: The open emit's sidecar.
        fork_path: The sole branch.
        kind: The table's record kind.
        populations: The table's resolved population set.
        identity_surface: The table's gated elected identity surface.
        context_label: The identity column's table.column identity.

    Raises:
        ElectedKeyDuplicate: Propagated from `_guard_surface_subset`.
    """
    if identity_surface == "record_id":
        return
    subset = tuple(p.sub_type for p in populations if p.sub_type is not None)
    _guard_surface_subset(
        emit, sidecar, fork_path, kind, subset, identity_surface, context_label
    )


def _guard_edge(
    emit: "Emit",
    sidecar: "Sidecar",
    fork_path: str,
    edge: SourceEdgeSurface,
    context_label: str,
) -> None:
    """Guard one referencing column's elected relations, per admitted kind and surface.

    Args:
        emit: The open emit.
        sidecar: The open emit's sidecar.
        fork_path: The sole branch.
        edge: The resolved edge.
        context_label: The referencing table/edge.column identity.

    Raises:
        ElectedKeyDuplicate: Propagated from `_guard_surface_subset`.
    """
    multi_kind = len(edge.per_kind_populations) > 1
    for target_kind, per_population in edge.per_kind_populations:
        label = (
            f"{context_label} (member kind '{target_kind}')"
            if multi_kind
            else context_label
        )
        for surface in _GUARD_SURFACES:
            subset = tuple(
                sub_type
                for sub_type, elected in per_population
                if elected == surface and sub_type is not None
            )
            _guard_surface_subset(
                emit, sidecar, fork_path, target_kind, subset, surface, label
            )


def _guard_item_surface(
    emit: "Emit",
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    item_surface: "tuple[tuple[str | None, KeySurface], ...]",
    context_label: str,
) -> None:
    """Guard one events source's `item_id` relation, per surviving surface.

    Args:
        emit: The open emit.
        sidecar: The open emit's sidecar.
        fork_path: The sole branch.
        kind: The item target kind (audited kind, or membership owner kind).
        item_surface: The source's per-population elected surface.
        context_label: The source's `item_id` identity, for the error.

    Raises:
        ElectedKeyDuplicate: Propagated from `_guard_surface_subset`.
    """
    for surface in _GUARD_SURFACES:
        subset = tuple(
            sub_type
            for sub_type, elected in item_surface
            if elected == surface and sub_type is not None
        )
        _guard_surface_subset(
            emit, sidecar, fork_path, kind, subset, surface, context_label
        )


def _change_edge_label(source_column: str) -> str:
    """The `changes` JSON key an audited change edge's `source_column` addresses.

    Args:
        source_column: `prop__<p>` (records source) or `member__<f>__id`
            (membership source), per `events.py`'s naming convention.

    Returns:
        `<p>` or `<f>_id`.
    """
    if source_column.startswith(_PROP_PREFIX):
        return source_column[len(_PROP_PREFIX) :]
    return f"{source_column[len(_MEMBER_PREFIX) : -len(_MEMBER_ID_SUFFIX)]}_id"


def _run_plan_guards(
    emit: "Emit",
    sidecar: "Sidecar",
    fork_path: str,
    tables: "tuple[SourceStateTablePlan | SourceJunctionTablePlan, ...]",
    events: "SourceEventLogPlan | None",
) -> None:
    """Run the plan-time elected-key uniqueness guard over every resolved unit.

    Args:
        emit: The open emit.
        sidecar: The open emit's sidecar.
        fork_path: The sole branch.
        tables: The resolved `tables[]` units.
        events: The resolved event-log unit, or None.

    Raises:
        ElectedKeyDuplicate: A corrupted elected key fails the guard on some
            composed relation.
    """
    for table in tables:
        columns_map = dict(table.columns)
        table_label = f"table '{table.name}'"
        if isinstance(table, SourceStateTablePlan):
            id_output = columns_map[table.identity_surface]
            _guard_table_identity(
                emit,
                sidecar,
                fork_path,
                table.kind,
                table.populations,
                table.identity_surface,
                f"{table_label}.{id_output}",
            )
        for edge in table.edge_surfaces:
            edge_out = columns_map.get(edge.source_column, edge.source_column)
            _guard_edge(emit, sidecar, fork_path, edge, f"{table_label}.{edge_out}")

    if events is not None:
        for source in events.sources:
            label = f"events '{events.name}' item_type '{source.item_type}'"
            _guard_item_surface(
                emit,
                sidecar,
                fork_path,
                source.kind,
                source.item_surface,
                f"{label}.item_id",
            )
            for edge in source.change_edges:
                prop_label = _change_edge_label(edge.source_column)
                _guard_edge(
                    emit, sidecar, fork_path, edge, f"{label}.changes.{prop_label}"
                )


# ---------------------------------------------------------------------------
# `declare_keys` resolution
# ---------------------------------------------------------------------------


def _presentation_id_claimed(
    presentation_keys: "PresentationKeys | None",
    kind: str,
    populations: "tuple[Population, ...]",
) -> bool:
    """Whether the registry claims `presentation_id` uniqueness over exactly
    a table's resolved population set.

    Args:
        presentation_keys: The sidecar's parsed claims view, or None.
        kind: The record kind.
        populations: The table's resolved population set.

    Returns:
        True iff every addressed population carries an entry and the
        combined claim (a flat kind's whole-table `key`; a sub-typed kind's
        `combined_claim` over its addressed sub-types' `PartitionKey`
        entries) derives a non-None `unique_within`.
    """
    if presentation_keys is None or kind not in presentation_keys.kinds():
        return False
    if len(populations) == 1 and populations[0].sub_type is None:
        return presentation_keys.key(kind).unique_within is not None
    entries = []
    for population in populations:
        assert population.sub_type is not None, (
            "a sub-typed kind's population always carries a sub_type"
        )
        try:
            entries.append(presentation_keys.key_for(kind, population.sub_type))
        except KeyError:
            return False
    return combined_claim(entries).unique_within is not None


def resolve_state_table_keys(
    sidecar: "Sidecar",
    kind: str,
    populations: "tuple[Population, ...]",
    identity_surface: "KeySurface",
    columns: tuple[tuple[str, str], ...],
) -> TableKeys:
    """One state table's declared keys under declare_keys.

    Primary key: the identity column's output name — `columns`' entry
    whose source name is the elected surface's contract column name.
    Unique on `presentation_id`'s output name iff (a) the registry claims
    uniqueness for exactly this table's resolved population set —
    `combined_claim` over the populations' PartitionKey entries
    (degenerate cases: a flat kind reads the whole-table `key` claim; a
    single-population table its sub-type entry, presence-is-the-claim; any
    addressed population without an entry, or a derived no-claim
    combination, declares nothing) — AND (b) `identity_surface !=
    'presentation_id'` (already the primary key there, not doubly
    declared) AND (c) the `presentation_id` column survives in `columns`
    (absorbed under a presentation_id election, omittable via column
    selection).

    Args:
        sidecar: The plan's sidecar (claims via
            `sidecar.presentation_keys()` — strict-on-read).
        kind: The table's records kind.
        populations: The table's resolved population set.
        identity_surface: The table's gated elected surface.
        columns: The table's final (source, output) pairs — output names
            honor renames.

    Returns:
        The declared keys (primary key always; unique iff claimed).

    Raises:
        PresentationKeysInvalidError: The block is present and incoherent
            (strict accessor, propagated — plan-time, before any output).
    """
    presentation_keys = sidecar.presentation_keys()
    columns_map = dict(columns)
    id_output = columns_map[identity_surface]
    pid_output = columns_map.get("presentation_id")

    claimed = _presentation_id_claimed(presentation_keys, kind, populations)
    unique: tuple[tuple[str, ...], ...] = (
        ((pid_output,),)
        if claimed and pid_output is not None and identity_surface != "presentation_id"
        else ()
    )
    return TableKeys(primary_key=(id_output,), unique=unique)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_source_plan(
    emit: "Emit",
    config: "ExportConfig",
    anchor: "EffectiveAnchor",
    election: Election,
    windowed: bool,
    notices: "NoticeSink",
) -> SourcePlan:
    """
    Resolve the declared tables and event log against the open emit.

    Resolves every declaration to populations, classifies every projected
    column through the records-column taxonomy, resolves column selection /
    renames, runs the identity gates (uniformity, union safety) per declared
    table over the election view and the edge gates per referencing column,
    per event-log item-type, and per audited reference property, resolves
    the audited property set per events source, runs the collision and
    reserved-name checks over all resolved output names, and guards every
    elected relation against the physical tape (the plan-time uniqueness
    guard, conservatively strict under a windowed ask — § SourcePlan).
    Validation is against the shape the invocation delivers: under
    `windowed=True` the state render omits `updated_at`, so a `columns` /
    `rename` entry naming `last_mutation_sim_time` is unsatisfiable and
    refused.

    Args:
        emit: The open emit.
        config: The full export config (mode: source).
        anchor: The resolved wallclock anchor (source requires one; the
            caller has already refused a None resolution).
        election: The resolved key-election view.
        windowed: Whether the invocation is windowed (`--next` /
            `--from`/`--to`) — an invocation fact, supplied by the caller,
            selecting which state-render shape the plan validates against.
        notices: Sink for slice_only omissions and other compile notices.

    Returns:
        The resolved plan: one unit per declared table plus the event-log
        unit when declared.

    Raises:
        SourceTableKindUnknown, SourceTableSubTypeUnknown,
            SourceTableMembershipUnknown, SourceSubTypesOnFlatKind:
            declaration does not resolve in the sidecar.
        SourceColumnUnresolved: a `columns` / `rename` / `only` / `ignore`
            entry names no column / property of its source surface.
        SourceColumnNotAddressable: a `columns` / `rename` entry names a
            mechanism column.
        SourceSliceOnlyRead: a `columns` / `rename` / `only` / `ignore`
            entry names a non-exempt slice_only column.
        SourceEventSourceOverlap: two events sources resolve overlapping
            population sets.
        SourceUnclassifiedColumn: a projected records column resolves no
            taxonomy role.
        SourceKindLabelUnknown: a `source.kind_labels` key names no records
            kind in the sidecar.
        SourceKindLabelCollision: a label equals an unlabeled kind's own
            name — kind -> rendered name is not injective.
        SourceItemTypeCollision: two events sources illegally share a
            resolved item-type, or a resolved item-type collides with a
            kind's rendered name.
        SourceNameCollision: duplicate output table or column names, or a
            source's resolved `changes` keys collide.
        ElectionMixedIdentity, ElectionUnionUnsafe: the identity gates
            per declared table; the edge gates per referencing column and
            per event-log item-type.
        SourceHistoryTrackedRequired: the sidecar predates per-column
            history_tracked flags.
        TemporalClassUnavailableError: a consulted flagged column declares
            no in-enum temporal_class (reader-owned, C13).
        ExportError: reserved output-name violations; the single-branch
            guard (require_single_branch).
        ElectedKeyDuplicate: a corrupted elected key fails the plan-time
            uniqueness guard.
        PresentationKeysInvalidError: `declare_keys` is true and the
            sidecar's `presentation_keys` block is present and incoherent.
    """
    sidecar = emit.sidecar
    fork_path = require_single_branch(sidecar)

    if not sidecar.history_tracked_available():
        raise SourceHistoryTrackedRequired(
            "source export requires per-column history_tracked flags; this"
            " emit predates them"
        )

    source_config = config.source
    assert source_config is not None, "mode='source' guarantees a source section"

    known_kinds = _known_records_kinds(sidecar)
    declare_keys = source_config.declare_keys
    kind_labels = _resolve_kind_labels(known_kinds, source_config.kind_labels)

    tables: list[SourceStateTablePlan | SourceJunctionTablePlan] = []
    for decl in source_config.tables:
        if decl.kind is not None:
            tables.append(
                _build_state_table_plan(
                    sidecar,
                    election,
                    known_kinds,
                    decl,
                    windowed,
                    declare_keys,
                    notices,
                )
            )
        else:
            tables.append(
                _build_junction_table_plan(
                    sidecar, election, known_kinds, decl, kind_labels, notices
                )
            )
    tables_t = tuple(tables)

    events_plan: SourceEventLogPlan | None = None
    if source_config.events is not None:
        events_plan = _build_event_log_plan(
            sidecar,
            election,
            known_kinds,
            source_config.events,
            declare_keys,
            notices,
            kind_labels,
        )

    _check_output_collisions(tables_t, events_plan)
    _check_output_reserved_names(tables_t, events_plan)

    _run_plan_guards(emit, sidecar, fork_path, tables_t, events_plan)

    return SourcePlan(
        sidecar=sidecar,
        fork_path=fork_path,
        anchor=anchor,
        windowed=windowed,
        tables=tables_t,
        events=events_plan,
    )
