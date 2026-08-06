"""Source-mode planning: declared-table resolution over populations.

`build_source_plan` is a pure function of `(emit, config, anchor, election,
windowed)` — every render is a pure function of the returned `SourcePlan`
(`sidecar`, `fork_path`, `anchor`, `windowed`, plus the resolved units), so
`build_source_query_specs(plan, window)` (renders.py / engine.py, a later
step) needs no further data-dependent step except the write-mode dispatch.
Resolves, in order: (1) every `tables` declaration to populations
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
via the mode-neutral `election` module, `fabulexa_forge.errors`, the
mode-neutral `reserved_names` / `slice_only` / `query_spec` (`TableKeys`) /
`populations` modules, the sibling `source.columns` (`_PROP_PREFIX`) and
`source.events` (`SourceEventSourcePlan`, `SourceEventLogPlan`) modules,
`notices`, `derivations.guard` (`require_single_branch`), config.models
(TYPE_CHECKING only except `KeySurface`), and stdlib. Never imports
exporters.dimensional.* or exporters.streaming.*.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import (
        ExportConfig,
        KeySurface,
        SourceEventsDecl,
        SourceEventSourceDecl,
        SourceTableDecl,
    )
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.reader.emit import Emit
    from fabulexa_forge.reader.sidecar import PresentationKeys, Sidecar

from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.errors import (
    ExportError,
    SourceColumnNotAddressable,
    SourceColumnUnresolved,
    SourceEventSourceOverlap,
    SourceHistoryTrackedRequired,
    SourceNameCollision,
    SourceSliceOnlyRead,
    SourceTableMembershipUnknown,
    SourceUnclassifiedColumn,
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
from fabulexa_forge.exporters.source.columns import _PROP_PREFIX
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
    source_column: str,
    edge_name: str,
) -> SourceEdgeSurface:
    """Gate and resolve one single-target-kind referencing column.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        target_kind: The referencing column's one target kind.
        source_column: The referencing column's source identity.
        edge_name: The referencing table · column identity, for the gate's
            error.

    Returns:
        The resolved `SourceEdgeSurface`, `target_kinds` a one-element tuple.

    Raises:
        ElectionUnionUnsafe: The target kind's admitted populations' resolved
            key spaces contain a pairwise-unsafe pair.
    """
    domain = sidecar.subtype_values(target_kind)
    check_edge_union_safety(election, target_kind, domain, edge_name)
    per_population = tuple(
        (p.sub_type, p.surface) for p in election.populations_for(target_kind)
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
            _resolve_single_kind_edge(sidecar, election, target_kind, src, edge_name)
        )
    return tuple(edges)


def _resolve_junction_edges(
    sidecar: "Sidecar",
    election: Election,
    source_table: str,
    owner_kind: str,
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

    return SourceStateTablePlan(
        name=decl.name,
        kind=kind,
        populations=populations,
        columns=columns,
        identity_surface=identity_surface,
        edge_surfaces=edge_surfaces,
        keys=keys,
    )


def _build_junction_table_plan(
    sidecar: "Sidecar",
    election: Election,
    known_kinds: "tuple[str, ...]",
    decl: "SourceTableDecl",
) -> SourceJunctionTablePlan:
    """Resolve one `tables[]` membership declaration to a `junction` table plan.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        known_kinds: Every kind with a declared records table in the emit.
        decl: The declaration (`decl.membership` is not None).

    Returns:
        The resolved `SourceJunctionTablePlan`.

    Raises:
        SourceTableMembershipUnknown: The membership reference resolves to
            no sidecar table.
        SourceColumnUnresolved, SourceColumnNotAddressable: Column
            resolution fails.
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

    candidate = _junction_candidate_columns(sidecar, source_table, owner_kind)
    all_source_columns = frozenset(col.name for col in sidecar.columns(source_table))
    columns = _apply_junction_columns_decl(
        candidate, decl.columns, decl.name, all_source_columns
    )
    columns = _apply_junction_rename(
        columns, decl.rename, decl.name, all_source_columns
    )

    edge_surfaces = _resolve_junction_edges(
        sidecar, election, source_table, owner_kind, known_kinds, decl.name, columns
    )

    return SourceJunctionTablePlan(
        name=decl.name,
        owner_kind=owner_kind,
        property=property_name,
        source_table=source_table,
        columns=columns,
        edge_surfaces=edge_surfaces,
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
    all_bare_names: list[str] = []
    candidates: list[str] = []
    for col in sidecar.columns(source_table):
        name = col.name
        if not name.startswith(_PROP_PREFIX):
            continue
        bare = name[len(_PROP_PREFIX) :]
        all_bare_names.append(bare)
        if is_non_exempt_slice_only(sidecar, kind, name):
            notice_sink(_slice_only_notice(owner, name))
            continue
        candidates.append(bare)

    all_bare_set = frozenset(all_bare_names)
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
                sidecar, election, target_kind, src, f"{owner}.{prop}"
            )
        )
    return tuple(edges)


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
    names = {col.name for col in sidecar.columns(table_name)}
    edges: list[SourceEdgeSurface] = []
    for field in audited_fields:
        id_col = f"member__{field}__id"
        kind_col = f"member__{field}__kind"
        if id_col not in names or kind_col not in names:
            continue
        edges.append(
            _resolve_member_field_edge(
                sidecar, election, known_kinds, id_col, f"{owner}.{field}_id"
            )
        )
    return tuple(edges)


def _build_event_source_plan(
    sidecar: "Sidecar",
    election: Election,
    known_kinds_set: frozenset[str],
    known_kinds: "tuple[str, ...]",
    decl: "SourceEventSourceDecl",
    owner: str,
    notice_sink: "NoticeSink",
) -> SourceEventSourcePlan:
    """Resolve one `events.sources[]` declaration to a `SourceEventSourcePlan`.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        known_kinds_set: `known_kinds` as a frozenset (edge admission).
        known_kinds: Every kind with a declared records table, table order
            (junction-member-column admission).
        decl: The events source declaration.
        owner: The declaring source's message label (`events source #<n>`).
        notice_sink: Receiver for slice-only-column-omitted notices.

    Returns:
        The resolved source, item_surface un-gated (the item-type
        union-safety gate runs separately, over every source sharing an
        item_type).

    Raises:
        SourceTableKindUnknown, SourceTableSubTypeUnknown,
            SourceSubTypesOnFlatKind, SourceTableMembershipUnknown:
            Population resolution fails.
        SourceColumnUnresolved, SourceSliceOnlyRead: An `only` / `ignore`
            entry is unresolved.
        ElectionUnionUnsafe: A change-edge gate fails.
    """
    if decl.kind is not None:
        kind = decl.kind
        populations = resolve_populations(sidecar, owner, kind, decl.sub_types)
        source_table = f"{_RECORDS_TABLE_PREFIX}{kind}"
        audited = _resolve_records_audited_properties(
            sidecar, kind, decl.only, decl.ignore, owner, notice_sink
        )
        change_edges = _resolve_records_change_edges(
            sidecar, election, source_table, audited, known_kinds_set, owner
        )
        item_surface = tuple(
            (p.sub_type, election.surface_for(kind, p.sub_type)) for p in populations
        )
        return SourceEventSourcePlan(
            item_type=kind,
            kind=kind,
            property=None,
            populations=populations,
            audited_properties=audited,
            item_surface=item_surface,
            change_edges=change_edges,
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

    populations = _owner_kind_domain_populations(sidecar, owner_kind)
    audited = _resolve_membership_audited_fields(
        sidecar, table_name, decl.only, decl.ignore, owner
    )
    change_edges = _resolve_membership_change_edges(
        sidecar, election, table_name, known_kinds, audited, owner
    )
    item_surface = tuple(
        (p.sub_type, election.surface_for(owner_kind, p.sub_type)) for p in populations
    )
    return SourceEventSourcePlan(
        item_type=f"{owner_kind}.{property_name}",
        kind=owner_kind,
        property=property_name,
        populations=populations,
        audited_properties=audited,
        item_surface=item_surface,
        change_edges=change_edges,
    )


def _population_label(population: Population) -> str:
    """A population atom's message label (`K` or `K.sub_type`)."""
    if population.sub_type is None:
        return population.kind
    return f"{population.kind}.{population.sub_type}"


def _check_events_source_overlap(sources: tuple[SourceEventSourcePlan, ...]) -> None:
    """Enforce pairwise-disjoint population sets across `events.sources`.

    Records sources are compared by population atom; membership sources by
    `(kind, property)` identity (each resolves to one fixed population set,
    so two membership sources sharing that identity always fully overlap).

    Args:
        sources: The resolved events sources, declaration order.

    Raises:
        SourceEventSourceOverlap: Two sources resolve an overlapping
            population.
    """
    seen_populations: dict[Population, None] = {}
    seen_membership: dict[tuple[str, str], None] = {}
    for source in sources:
        if source.property is None:
            for population in source.populations:
                if population in seen_populations:
                    raise SourceEventSourceOverlap(
                        f"events: sources overlap on population"
                        f" '{_population_label(population)}'"
                    )
                seen_populations[population] = None
        else:
            key = (source.kind, source.property)
            if key in seen_membership:
                raise SourceEventSourceOverlap(
                    f"events: sources overlap on population '{source.item_type}'"
                )
            seen_membership[key] = None


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

    Returns:
        The resolved event-log plan.

    Raises:
        SourceTableKindUnknown, SourceTableSubTypeUnknown,
            SourceSubTypesOnFlatKind, SourceTableMembershipUnknown:
            A source's population resolution fails.
        SourceColumnUnresolved, SourceSliceOnlyRead: An `only` / `ignore`
            entry is unresolved.
        SourceEventSourceOverlap: Two sources resolve overlapping populations.
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
        )
        for index, source_decl in enumerate(decl.sources, start=1)
    )
    _check_events_source_overlap(sources)
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
        SourceNameCollision: duplicate output table or column names.
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
                _build_junction_table_plan(sidecar, election, known_kinds, decl)
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
