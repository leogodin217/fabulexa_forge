"""Source-mode planning: classification, sub-type split, presentation, and
exclude/rename resolution.

`build_source_plan` is a pure function of `(sidecar, config)` — no SQL, no emit
read beyond the sidecar. It applies, in order: (1) the genre trichotomy and the
untracked-only sub-type split over every records and membership table in the
sidecar; (2) `exclude`; (3) operational presentation defaults (table + column
naming, delivery-dependent for a change-log kind — § `change_delivery`); (4)
`rename`; (5) the collision and reserved-name checks. See
`docs/architecture/pending/source-export.md` for the semantics this module
implements (no incremental window — window membership is renders.py's concern).

Layer-direction invariant: imports only the reader, the derivations layer
(the row-state-events fold's column-naming helper and the state-at
derivation's column order / property-partition helpers), fabulexa_forge.errors,
the mode-neutral reserved_names, query_spec (for `TableKeys`), and election
(`Election`, `check_edge_union_safety`, `check_identity_election`,
`resolve_election`) modules, notices (for `Notice`, and `NoticeSink`
TYPE_CHECKING-only), and slice_only modules, the sibling source.columns
module, config.models (TYPE_CHECKING only except `KeySurface`), and stdlib.
Never imports exporters.dimensional.* or exporters.streaming.*.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from fabulexa_forge.config.models import (
        ExcludeDecl,
        KeySurface,
        RenameEntry,
        SourceConfig,
    )
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.reader.sidecar import PresentationKeys, RecordRoles, Sidecar

from fabulexa_forge.derivations.properties import has_presentation_id
from fabulexa_forge.derivations.row_state_events import resolve_stream_columns
from fabulexa_forge.derivations.state_at import STATE_AT_COLUMNS
from fabulexa_forge.errors import (
    ExportError,
    SourceExcludeUnresolved,
    SourceHistoryTrackedRequired,
    SourceNameCollision,
    SourceRecordRolesRequired,
    SourceRenameSliceOnly,
    SourceRenameUnresolved,
    SourceRoleUnknown,
    SourceSubtypesUndeclared,
    SourceUnclassifiedColumn,
)
from fabulexa_forge.exporters.election import (
    Election,
    check_edge_union_safety,
    check_identity_election,
    resolve_election,
)
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.query_spec import TableKeys
from fabulexa_forge.exporters.reserved_names import (
    RESERVED_PRESENTATION_COLUMN_NAME,
    is_reserved_column_name,
    is_reserved_table_name,
)
from fabulexa_forge.exporters.slice_only import is_non_exempt_slice_only
from fabulexa_forge.exporters.source.columns import _PROP_PREFIX, _scalar_properties
from fabulexa_forge.reader.records_columns import records_column_role

#: The `records__<kind>` name prefix, stripped to recover a records unit's kind.
_RECORDS_TABLE_PREFIX = "records__"

#: Prefixes/suffixes the presentation-default renamer strips or recognizes.
_ELEM_PREFIX = "elem__"
_MEMBER_PREFIX = "member__"
_MEMBER_KIND_SUFFIX = "__kind"
_MEMBER_ID_SUFFIX = "__id"

#: Structural records-table columns renamed to their operational default.
#: Columns absent from this map (active, presentation_id, prop__<p> handled
#: separately) keep their source name.
_LIFECYCLE_RENAMES: dict[str, str] = {
    "record_id": "id",
    "created_sim_time": "created_at",
    "last_mutation_sim_time": "updated_at",
}


@dataclass(frozen=True)
class SourceEdgeSurface:
    """One referencing source column's resolved target election(s).

    `target_kinds` names the referencing column's admitted target kind(s): a
    one-element tuple for a reference-valued `prop__<p>` column or a junction
    owner column, whose target kind is fixed; every kind with a declared
    `records__<kind>` table in the emit for a junction member column — member
    kind is per-row, not statically declared (contract § Membership-category
    tables), so the closed, data-free admitted set is the full universe of
    kinds a `member__<f>__kind` value could legally name. `per_kind_populations`
    carries, per admitted kind, that kind's full declared domain with its
    resolved election (gated pairwise union-safe per kind independently —
    cross-kind values carry no uniqueness claim; `<f>_kind` disambiguates).
    `rendered_type` is the mixed-column type rule's verdict: the common
    declared type when every admitted population (across every admitted kind)
    agrees, else `'VARCHAR'` (record_index values digit-rendered at render
    time) — a junction member column always resolves `'VARCHAR'` when
    non-default, since a `member__<f>__id` column is inherently VARCHAR-typed
    regardless of election."""

    source_column: str
    target_kinds: "tuple[str, ...]"
    per_kind_populations: (
        "tuple[tuple[str, tuple[tuple[str | None, KeySurface], ...]], ...]"
    )
    rendered_type: str


@dataclass(frozen=True)
class SourceTableSpec:
    """One resolved output table: source identity, genre, and naming."""

    source_table: str
    """The sidecar base-table name this output table reads."""
    sub_type: str | None
    """The split unit's discriminator value; None for unsplit units and membership
    tables. Always None when genre is 'changelog' or 'junction' — only untracked
    reference/transaction units split."""
    genre: Literal["changelog", "reference", "transaction", "junction"]
    """The resolved genre driving the render."""
    name: str
    """The resolved output table name (default or renamed)."""
    columns: tuple[tuple[str, str], ...]
    """Ordered (source column, output column) pairs after defaults, drops, and renames
    — the columns this table delivers. Source columns are base/canonical-fold names;
    output names are final. Delivery-dependent for a change-log kind: the CDC fold's
    column set under `change_delivery: changelog` (default), or the snapshot
    (state-at) shape's base / state-at source names under `snapshot` — never both.
    Delivery-independent (the faithful records/membership set) for every other
    genre."""
    identity_surface: "KeySurface"
    """The table's own population(s)' uniform elected identity surface
    (`'record_id'` under no election — byte-identical). For a split unit, the
    unit's own single population's election (never gated — trivially
    uniform). For an unsplit unit whose kind is sub-typed, gated uniform over
    the kind's full domain (`check_identity_election`) before resolution.
    Junction genre carries no own identity — always `'record_id'`, unused by
    the render (owner/member columns are edges, not the table's own
    identity)."""
    edge_surfaces: "tuple[SourceEdgeSurface, ...]"
    """One entry per referencing source column this table carries: a
    reference-valued `prop__<p>` column (reference/transaction genres, and a
    change-log kind under `change_delivery: snapshot` — never a change-log
    kind under CDC delivery, per the doc), the junction owner column, and one
    entry per junction member field. Empty for a CDC change-log spec."""


@dataclass(frozen=True)
class _Unit:
    """One export unit resolved from the sidecar, before naming/exclude/rename."""

    source_table: str
    kind: str
    sub_type: str | None
    genre: Literal["changelog", "reference", "transaction", "junction"]
    property: str | None  # membership property name; None for a records unit


# ---------------------------------------------------------------------------
# Classification: the genre trichotomy + the sub-type split
# ---------------------------------------------------------------------------


def _is_kind_tracked(sidecar: "Sidecar", source_table: str) -> bool:
    """Whether any property of `source_table`'s kind genuinely changes over time.

    A kind is tracked iff one of its prop__ columns is temporal_class 'tracked'.
    Keyed on the class, not on history_tracked: every presentation column is
    history_tracked, but one bound to an immutable source is class 'constant' and
    holds exactly its genesis row — a kind carrying only such a column does not
    change, and rendering it as a change log would render a change log with no
    changes.

    Only a history_tracked column can be class 'tracked' (the contract constrains
    it), so the class is consulted only for the columns carrying the bit — resolved
    through the sidecar's temporal_class accessor, the single narrowing point. A
    kind carrying no history_tracked prop__ column is untracked without consulting
    any class — the same defensive skip signal C11 and C13 key on, unreachable past
    the version gate against a producer-written emit (coverage is total) and
    retained so the predicate is correct standalone.

    Args:
        sidecar: The open emit's sidecar.
        source_table: The kind's records__<kind> table name.

    Returns:
        True iff some prop__ column of the kind is temporal_class 'tracked'.

    Raises:
        TableNotFoundError: `source_table` is not in the sidecar.
        TemporalClassUnavailableError: A prop__ column declares history_tracked but
            no temporal_class, or declares a class outside the enum. The emit is
            non-conformant (C13); no class is inferred.
    """
    flagged = [
        col
        for col in sidecar.columns(source_table)
        if col.name.startswith(_PROP_PREFIX) and col.history_tracked is True
    ]
    if not flagged:
        return False
    return any(
        sidecar.temporal_class(source_table, col.name) == "tracked" for col in flagged
    )


def _genre_for_role(role: str) -> Literal["reference", "transaction"]:
    """Map a warehouse role string to its untracked genre.

    Args:
        role: The record_roles value ("dimension" or "fact").

    Returns:
        "reference" for "dimension", "transaction" otherwise.
    """
    return "reference" if role == "dimension" else "transaction"


def _classify_records_kind(
    sidecar: "Sidecar",
    record_roles: "RecordRoles",
    source_table: str,
    kind: str,
) -> tuple[_Unit, ...]:
    """Classify one records__<kind> table into its export unit(s).

    Args:
        sidecar: The open emit's sidecar.
        record_roles: The sidecar's typed record_roles view.
        source_table: The kind's records__<kind> table name.
        kind: The record kind.

    Returns:
        One unit for a tracked kind (whatever its role) or an untracked bare-role
        kind; one unit per declared sub-type (enum-domain order) for an untracked
        object-registry kind.

    Raises:
        SourceRoleUnknown: The kind (or a declared sub-type) has no record_roles entry.
        SourceSubtypesUndeclared: The kind's role varies by sub-type but no
            <kind>_type enum domain declares the sub-types.
    """
    if _is_kind_tracked(sidecar, source_table):
        return (
            _Unit(
                source_table=source_table,
                kind=kind,
                sub_type=None,
                genre="changelog",
                property=None,
            ),
        )

    try:
        is_subtyped = record_roles.is_subtyped(kind)
    except KeyError:
        raise SourceRoleUnknown(f"kind '{kind}': no role in record_roles") from None

    if not is_subtyped:
        role = record_roles.role_of(kind, None)
        return (
            _Unit(
                source_table=source_table,
                kind=kind,
                sub_type=None,
                genre=_genre_for_role(role),
                property=None,
            ),
        )

    sub_types = sidecar.subtype_values(kind)
    if not sub_types:
        raise SourceSubtypesUndeclared(
            f"kind '{kind}': role varies by sub-type but no {kind}_type enum"
            " domain declares the sub-types"
        )

    units: list[_Unit] = []
    for sub_type in sub_types:
        try:
            role = record_roles.role_of(kind, sub_type)
        except KeyError:
            raise SourceRoleUnknown(
                f"kind '{kind}' sub_type '{sub_type}': no role in record_roles"
            ) from None
        units.append(
            _Unit(
                source_table=source_table,
                kind=kind,
                sub_type=sub_type,
                genre=_genre_for_role(role),
                property=None,
            )
        )
    return tuple(units)


def _classify_units(sidecar: "Sidecar") -> tuple[_Unit, ...]:
    """Classify every records and membership table in the sidecar into export units.

    Total over the emit: every records-category table resolves to exactly one
    genre (or splits into per-sub-type units); every membership-category table
    resolves to 'junction' unconditionally. Fixed-category tables (history) are
    never a plan entry.

    Args:
        sidecar: The open emit's sidecar.

    Returns:
        Units in sidecar table order, with split units in enum-domain declaration
        order.

    Raises:
        SourceRecordRolesRequired: The sidecar carries no record_roles registry.
        SourceHistoryTrackedRequired: The sidecar carries no history_tracked flags.
        SourceRoleUnknown: An untracked kind (or declared sub-type) has no role.
        SourceSubtypesUndeclared: An untracked object-registry kind declares no
            <kind>_type enum domain.
    """
    record_roles = sidecar.record_roles()
    if record_roles is None:
        raise SourceRecordRolesRequired(
            "source export requires the record_roles registry; this emit predates it"
        )
    if not sidecar.history_tracked_available():
        raise SourceHistoryTrackedRequired(
            "source export requires per-column history_tracked flags; this emit"
            " predates them"
        )

    units: list[_Unit] = []
    for table in sidecar.tables():
        if table.category == "records":
            kind = table.record_kind
            assert kind is not None, "records table must declare record_kind"
            units.extend(
                _classify_records_kind(sidecar, record_roles, table.name, kind)
            )
        elif table.category == "membership":
            kind = table.record_kind
            property_name = table.property
            assert kind is not None and property_name is not None, (
                "membership table must declare record_kind and property"
            )
            units.append(
                _Unit(
                    source_table=table.name,
                    kind=kind,
                    sub_type=None,
                    genre="junction",
                    property=property_name,
                )
            )
        # category == "fixed" (history, ...): never a plan entry.
    return tuple(units)


# ---------------------------------------------------------------------------
# Election resolution
# ---------------------------------------------------------------------------


def _resolve_identity_surface(
    sidecar: "Sidecar",
    election: Election,
    kind: str,
    sub_type: str | None,
    table_name: str,
) -> "KeySurface":
    """Resolve one unit's own uniform elected identity surface.

    A split unit (`sub_type` set) is a single population — trivially uniform,
    no gate needed. A flat kind (no discriminator domain) is likewise a
    single population. An unsplit unit whose kind is sub-typed (record_roles'
    role does not vary by sub-type, so the whole domain lands in one table)
    is gated (`check_identity_election`) over the kind's full domain; once
    gated, every population resolves identically, so any domain member's
    surface is the unit's answer.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        kind: The unit's record kind.
        sub_type: The unit's own discriminator value, or None for an unsplit
            unit.
        table_name: The unit's output table name, for the gate's error.

    Returns:
        The unit's uniform elected surface (`'record_id'` under no election).

    Raises:
        ElectionMixedIdentity: An unsplit sub-typed kind's populations elect
            differing surfaces.
        ElectionUnionUnsafe: A uniform presentation_id election whose
            populations' key spaces contain a pairwise-unsafe pair.
    """
    if sub_type is not None:
        return election.surface_for(kind, sub_type)
    domain = sidecar.subtype_values(kind)
    if not domain:
        return election.surface_for(kind, None)
    check_identity_election(election, kind, domain, table_name)
    return election.surface_for(kind, domain[0])


def _known_records_kinds(sidecar: "Sidecar") -> tuple[str, ...]:
    """Every kind with a declared `records__<kind>` table, sidecar table order.

    The closed, data-free universe of kinds a junction member field's
    per-row `member__<f>__kind` value could legally name (§ per-row
    population resolution — member kind is not statically declared).

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
    carry no uniqueness claim — the `<f>_kind` column disambiguates, per the
    doc's mixed-election edge columns section).

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
        `rendered_type` is always `'VARCHAR'` when non-default (a
        `member__<f>__id` column is inherently VARCHAR-typed) — `'VARCHAR'`
        unconditionally, since the default (uniform record_id) case is also
        natively VARCHAR.

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
    """Resolve every reference-annotated `prop__<p>` column a unit carries.

    Applies to reference/transaction units, and a change-log unit under
    `change_delivery: snapshot` — never a change-log unit under CDC delivery
    (the doc's per-row rendering table omits changelog from this row; the
    fold's reference-valued payload columns stay verbatim regardless of
    election). A property whose target kind has no declared records table in
    the emit yields no entry — the kind-exists gate's consequence: it cannot
    carry an election, so the column renders its default verbatim record_id
    with no join needed.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        source_table: The unit's `records__<kind>` table.
        columns: The unit's default (source, output) column pairs.
        known_kinds: Every kind with a declared records table in the emit.
        table_name: The unit's output table name, for the gates' errors.

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
        edge_name = f"{table_name}.{src}"
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
) -> tuple[SourceEdgeSurface, ...]:
    """Resolve a junction unit's owner column and every member field.

    A membership table's owning kind ordinarily has a declared records table
    in the emit; when it does not (the kind-exists gate's consequence), the
    owner column carries no entry — it cannot carry an election, so it
    renders its default verbatim record_id with no join needed.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        source_table: The `membership__<K>__<p>` table.
        owner_kind: The owning kind (`<K>`).
        known_kinds: Every kind with a declared records table in the emit
            (§ `_known_records_kinds`).
        table_name: The unit's output table name, for the gates' errors.

    Returns:
        The owner column's `SourceEdgeSurface` first (when the owner kind
        has a declared records table), then one per member field in sidecar
        column-declaration order.

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
                f"{table_name}.{owner_kind}_id",
            )
        )
    for col in sidecar.columns(source_table):
        name = col.name
        if name.startswith(_MEMBER_PREFIX) and name.endswith(_MEMBER_ID_SUFFIX):
            field = name[len(_MEMBER_PREFIX) : -len(_MEMBER_ID_SUFFIX)]
            edges.append(
                _resolve_member_field_edge(
                    sidecar, election, known_kinds, name, f"{table_name}.{field}_id"
                )
            )
    return tuple(edges)


def _apply_identity_election(
    columns: "tuple[tuple[str, str], ...]", identity_surface: "KeySurface"
) -> "tuple[tuple[str, str], ...]":
    """Rewrite a unit's default columns for its own identity election.

    Under no election (`'record_id'`), a no-op — byte-identical. Otherwise
    the `record_id` entry's source key becomes the elected surface's contract
    column name (so `rename` addressing follows source identity, per the
    doc), and — under `presentation_id` election — the standalone
    `presentation_id` payload column entry (when present) is absorbed: it
    now occupies the identity slot, so emitting both would duplicate a
    column.

    Args:
        columns: The unit's default (source, output) column pairs (source
            names are base/canonical-fold names, so the identity column is
            always literally `'record_id'` here).
        identity_surface: The unit's own resolved identity election.

    Returns:
        The rewritten (source, output) column pairs.
    """
    if identity_surface == "record_id":
        return columns
    rewritten: list[tuple[str, str]] = []
    for src, out in columns:
        if src == "record_id":
            rewritten.append((identity_surface, out))
        elif src == "presentation_id" and identity_surface == "presentation_id":
            continue
        else:
            rewritten.append((src, out))
    return tuple(rewritten)


# ---------------------------------------------------------------------------
# Operational presentation defaults: table names + column naming
# ---------------------------------------------------------------------------


def _default_table_name(unit: _Unit) -> str:
    """The default output table name for a unit.

    Args:
        unit: The export unit.

    Returns:
        `<K>_<p>` for a junction unit; the sub-type value for a split unit;
        the bare kind name otherwise.
    """
    if unit.genre == "junction":
        assert unit.property is not None, "junction unit must carry property"
        return f"{unit.kind}_{unit.property}"
    if unit.sub_type is not None:
        return unit.sub_type
    return unit.kind


def _omitted_slice_only_columns(sidecar: "Sidecar", kind: str) -> tuple[str, ...]:
    """The unit-invariant omitted set for one records kind.

    Every non-exempt temporal_class: slice_only prop__ column of
    records__<kind>, in sidecar column-declaration order
    (is_non_exempt_slice_only per column). Never called for junction units
    (membership columns carry no class).

    Args:
        sidecar: The open emit's sidecar.
        kind: The record kind owning the records__<kind> table.

    Returns:
        Omitted column names (prop__ prefix included), sidecar column order.

    Raises:
        TemporalClassUnavailableError: Propagated.
    """
    source_table = f"records__{kind}"
    return tuple(
        col.name
        for col in sidecar.columns(source_table)
        if is_non_exempt_slice_only(sidecar, kind, col.name)
    )


def _changelog_columns(
    sidecar: "Sidecar", kind: str, omitted: frozenset[str]
) -> tuple[tuple[str, str], ...]:
    """The change-log render's fold column set, source -> output.

    Composes the row-state-events fold's after-image column order
    (`resolve_stream_columns`) with the fold's own fixed op/changed_at prefix —
    the same derivation streaming replays, invoked with the kind's scalar
    property set minus `omitted` (tracked and untracked alike; event_class is
    ordering-only and never projected).

    Args:
        sidecar: The open emit's sidecar.
        kind: The (tracked) record kind.
        omitted: The unit's policy-omitted prop__ column names (§
            _omitted_slice_only_columns), excluded from the property set fed
            to the stream-column resolution.

    Returns:
        (source, output) pairs: op, event_sim_time->changed_at, record_id->id,
        presentation_id (when carried), then one prop__<p>-><p> per non-omitted
        scalar property in sidecar column-declaration order.
    """
    source_table = f"records__{kind}"
    omitted_properties = {name[len(_PROP_PREFIX) :] for name in omitted}
    properties = _scalar_properties(sidecar, source_table) - omitted_properties
    stream_columns = resolve_stream_columns(sidecar, kind, properties)

    pairs: list[tuple[str, str]] = [("op", "op"), ("event_sim_time", "changed_at")]
    for col in stream_columns:
        if col == "record_id":
            pairs.append((col, "id"))
        elif col == "presentation_id":
            pairs.append((col, "presentation_id"))
        else:
            pairs.append((col, col[len(_PROP_PREFIX) :]))
    return tuple(pairs)


def _snapshot_columns(
    sidecar: "Sidecar", kind: str, omitted: frozenset[str]
) -> tuple[tuple[str, str], ...]:
    """The snapshot (state-at) render's column set, source -> output.

    A change-log kind delivered under `change_delivery: snapshot` renders the
    Phase-2 state-at shape instead of the CDC fold: identity, horizon-rendered
    lifecycle, payload — no `op` / `changed_at` / `updated_at`. Source names are
    the base / state-at names (`record_id`, `created_sim_time`, `active`,
    `deactivated_at`, `presentation_id`, `prop__<p>`) so a rename entry targets
    them directly, never the fold names.

    Args:
        sidecar: The open emit's sidecar.
        kind: The (tracked) record kind.
        omitted: The unit's policy-omitted prop__ column names (§
            _omitted_slice_only_columns), dropped from the returned pairs.

    Returns:
        (source, output) pairs: `STATE_AT_COLUMNS` (lifecycle-renamed per
        `_LIFECYCLE_RENAMES`), `presentation_id` when carried, then one
        `prop__<p>` -> `<p>` per non-omitted scalar property in sidecar
        column-declaration order — the same order `build_state_at_sql`
        produces.
    """
    source_table = f"records__{kind}"
    pairs: list[tuple[str, str]] = [
        (name, _LIFECYCLE_RENAMES.get(name, name)) for name in STATE_AT_COLUMNS
    ]
    if has_presentation_id(sidecar, kind):
        pairs.append(("presentation_id", "presentation_id"))
    for col in sidecar.columns(source_table):
        if col.name.startswith(_PROP_PREFIX) and col.name not in omitted:
            pairs.append((col.name, col.name[len(_PROP_PREFIX) :]))
    return tuple(pairs)


def _require_all_columns_classified(sidecar: "Sidecar", source_table: str) -> None:
    """Classify every column of a records table through the taxonomy.

    The one validation point every genre shares: called before any genre-specific
    column set is built, so a no-role column fails export planning uniformly and
    before any output is written — the taxonomy's closed-world posture (design doc
    § Semantics — the records-column taxonomy).

    Args:
        sidecar: The open emit's sidecar.
        source_table: The unit's records__<kind> table name.

    Raises:
        SourceUnclassifiedColumn: A column matches no records-column taxonomy role.
    """
    for col in sidecar.columns(source_table):
        if records_column_role(col.name) is None:
            raise SourceUnclassifiedColumn(
                f"table '{source_table}': column '{col.name}' matches no"
                " records-column taxonomy role"
            )


def _records_columns(
    sidecar: "Sidecar",
    source_table: str,
    drop_discriminator: str | None,
    omitted: frozenset[str],
) -> tuple[tuple[str, str], ...]:
    """The reference/transaction render's faithful column set, source -> output.

    Every column classifies through the records-column taxonomy: identity
    columns are dropped, following `fork_path`'s precedent — except `record_id`,
    which is identity but kept as `id` (design doc § Semantics — Phase-1 exporter
    posture). Presentation and lifecycle columns keep their operational default
    name; payload columns are prefix-stripped. `omitted` columns are dropped
    like the discriminator — the degenerate unit (every property omitted)
    still renders identity, lifecycle, and (for a split unit) the exempt
    discriminator.

    Args:
        sidecar: The open emit's sidecar.
        source_table: The unit's records__<kind> table name.
        drop_discriminator: The split unit's own `prop__<kind>_type` column name
            to drop (constant within the table, recoverable from table identity),
            or None to retain every prop__ column (unsplit unit).
        omitted: The unit's policy-omitted prop__ column names (§
            _omitted_slice_only_columns), dropped from the returned pairs.

    Returns:
        (source, output) pairs in sidecar column order, identity columns dropped
        (`record_id` kept), the lifecycle columns renamed to their operational
        default, prop__ columns prefix-stripped, `omitted` columns absent.
    """
    pairs: list[tuple[str, str]] = []
    for col in sidecar.columns(source_table):
        name = col.name
        role = records_column_role(name)
        if role == "identity" and name != "record_id":
            continue
        if drop_discriminator is not None and name == drop_discriminator:
            continue
        if name in omitted:
            continue
        if role == "payload":
            pairs.append((name, name[len(_PROP_PREFIX) :]))
        else:
            pairs.append((name, _LIFECYCLE_RENAMES.get(name, name)))
    return tuple(pairs)


def _split_member_field_name(name: str) -> str:
    """Resolve a `member__<f>__kind` / `member__<f>__id` column to its output name.

    Every membership-table column that is not `fork_path`, `record_id`,
    `joined_sim_time`, `left_sim_time`, or `elem__`-prefixed is, per the
    base-format contract's membership-table schema, a `member__<f>__kind` or
    `member__<f>__id` reference-field column — the only position
    `_junction_columns` calls this from.

    Args:
        name: A `member__` membership-table column name.

    Returns:
        `<f>_kind` / `<f>_id`.
    """
    rest = name[len(_MEMBER_PREFIX) :]
    if rest.endswith(_MEMBER_KIND_SUFFIX):
        return f"{rest[: -len(_MEMBER_KIND_SUFFIX)]}_kind"
    return f"{rest[: -len(_MEMBER_ID_SUFFIX)]}_id"


def _junction_columns(
    sidecar: "Sidecar",
    source_table: str,
    owner_kind: str,
) -> tuple[tuple[str, str], ...]:
    """The junction render's faithful column set, source -> output.

    Args:
        sidecar: The open emit's sidecar.
        source_table: The membership__<K>__<p> table name.
        owner_kind: The owning kind (`<K>`), for the record_id -> <K>_id rename.

    Returns:
        (source, output) pairs in sidecar column order: fork_path dropped,
        record_id -> <K>_id, joined/left_sim_time -> joined_at/left_at,
        elem__<f> -> <f>, member__<f>__kind/__id -> <f>_kind/<f>_id.
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


def _default_columns(
    sidecar: "Sidecar",
    unit: _Unit,
    change_delivery: Literal["changelog", "snapshot"],
    omitted: frozenset[str],
) -> tuple[tuple[str, str], ...]:
    """Dispatch a unit to its genre's default column-naming builder.

    Args:
        sidecar: The open emit's sidecar.
        unit: The export unit.
        change_delivery: The source config's delivery mode for change-log
            kinds; irrelevant to every other genre.
        omitted: The unit's policy-omitted prop__ column names (§
            _omitted_slice_only_columns) — the caller passes frozenset() for a
            junction unit, whose membership columns carry no class and whose
            builder does not consult it.

    Returns:
        The unit's default (source, output) column pairs.

    Raises:
        SourceUnclassifiedColumn: A records-category column of the unit's table
            matches no records-column taxonomy role.
    """
    if unit.genre == "junction":
        return _junction_columns(sidecar, unit.source_table, unit.kind)
    _require_all_columns_classified(sidecar, unit.source_table)
    if unit.genre == "changelog":
        if change_delivery == "snapshot":
            return _snapshot_columns(sidecar, unit.kind, omitted)
        return _changelog_columns(sidecar, unit.kind, omitted)
    drop_discriminator = (
        f"{_PROP_PREFIX}{unit.kind}_type" if unit.sub_type is not None else None
    )
    return _records_columns(sidecar, unit.source_table, drop_discriminator, omitted)


# ---------------------------------------------------------------------------
# exclude resolution
# ---------------------------------------------------------------------------


def _apply_exclude(
    units: tuple[_Unit, ...],
    exclude: "ExcludeDecl | None",
) -> tuple[_Unit, ...]:
    """Drop excluded kinds and sidecar tables from the classified units.

    `exclude.kinds` drops every unit of that kind and every membership table it
    owns. `exclude.tables` drops the named sidecar table; a `records__<kind>`
    entry is equivalent to `exclude.kinds: [kind]`.

    Args:
        units: The classified units.
        exclude: The source.exclude declaration, or None.

    Returns:
        The surviving units, in their original order.

    Raises:
        SourceExcludeUnresolved: An exclude.kinds or exclude.tables entry
            matches nothing in the classified units.
    """
    if exclude is None:
        return units

    known_kinds = {u.kind for u in units}
    known_tables = {u.source_table for u in units}

    excluded_kinds: set[str] = set()
    excluded_tables: set[str] = set()

    for kind in exclude.kinds or ():
        if kind not in known_kinds:
            raise SourceExcludeUnresolved(
                f"exclude entry '{kind}' matches nothing in this emit"
            )
        excluded_kinds.add(kind)

    for table_name in exclude.tables or ():
        if table_name not in known_tables:
            raise SourceExcludeUnresolved(
                f"exclude entry '{table_name}' matches nothing in this emit"
            )
        if table_name.startswith("records__"):
            excluded_kinds.add(table_name[len("records__") :])
        else:
            excluded_tables.add(table_name)

    return tuple(
        u
        for u in units
        if u.kind not in excluded_kinds and u.source_table not in excluded_tables
    )


# ---------------------------------------------------------------------------
# rename resolution
# ---------------------------------------------------------------------------


def _apply_rename_entry(
    entry: "RenameEntry",
    default_name: str,
    default_columns: tuple[tuple[str, str], ...],
    omitted: frozenset[str],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Apply one matched rename entry to a unit's default name/columns.

    Args:
        entry: The matched RenameEntry.
        default_name: The unit's default output table name.
        default_columns: The unit's default (source, output) column pairs
            (already narrowed — `omitted` columns are absent).
        omitted: The unit's policy-omitted prop__ column names (§
            _omitted_slice_only_columns), checked before the not-a-source-column
            check so the message names the omission reason.

    Returns:
        The (possibly overridden) table name and column pairs.

    Raises:
        SourceRenameSliceOnly: A columns key names a policy-omitted
            slice_only column.
        SourceRenameUnresolved: A columns key does not name a source column of
            this unit's default columns.
    """
    name = entry.name if entry.name is not None else default_name
    column_overrides = entry.columns
    if column_overrides is None:
        return name, default_columns

    default_sources = {src for src, _ in default_columns}
    for src_key in column_overrides:
        if src_key in omitted:
            raise SourceRenameSliceOnly(
                f"rename entry '{entry.table}': column '{src_key}' is"
                " temporal_class: slice_only and is omitted from this unit's"
                " export; the rename is unsatisfiable"
            )
        if src_key not in default_sources:
            raise SourceRenameUnresolved(
                f"rename entry '{entry.table}': column '{src_key}' is not a"
                " source column of this table"
            )
    columns = tuple(
        (src, column_overrides.get(src, out)) for src, out in default_columns
    )
    return name, columns


def _unit_label(unit: _Unit) -> str:
    """Render one export unit's identity for a notice message.

    Args:
        unit: The export unit.

    Returns:
        The source table name, with the sub-type appended for a split unit.
    """
    if unit.sub_type is not None:
        return f"{unit.source_table} (sub_type '{unit.sub_type}')"
    return unit.source_table


def _slice_only_omission_notice(unit: _Unit, column_name: str) -> Notice:
    """Build the 'slice-only-column-omitted' notice for one unit x column.

    Args:
        unit: The export unit the column was omitted from.
        column_name: The omitted prop__ column name.

    Returns:
        The rendered Notice, naming the unit and the column.
    """
    return Notice(
        code="slice-only-column-omitted",
        message=(
            f"unit '{_unit_label(unit)}': column '{column_name}' is"
            " temporal_class: slice_only; omitted from the source export"
        ),
    )


def _resolve_specs(
    sidecar: "Sidecar",
    election: Election,
    units: tuple[_Unit, ...],
    rename: "list[RenameEntry] | None",
    change_delivery: Literal["changelog", "snapshot"],
    notice_sink: "NoticeSink",
) -> tuple[SourceTableSpec, ...]:
    """Resolve every unit's election, default naming, then apply matching
    rename entries.

    The emission point for `slice-only-column-omitted`: per unit, computes the
    unit's omitted set (empty for a junction unit — membership columns carry
    no class) and emits one notice per unit x column, unit order then sidecar
    column order, before rename resolution and spec assembly. Own-identity
    election is resolved and gated (`_resolve_identity_surface`) before the
    default columns are built, so `_apply_identity_election` narrows the
    default set (absorption) prior to rename resolution — a rename keyed on
    the absorbed/dropped identity column is then unresolvable, per the doc.
    Edge elections (reference-annotated `prop__<p>` columns, any genre;
    junction owner + member columns) are resolved and gated over the
    narrowed set, using the unit's default (pre-rename) output table name for
    every gate/edge error label.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        units: The classified, exclude-filtered units.
        rename: The source.rename entries, or None.
        change_delivery: The source config's delivery mode for change-log
            kinds.
        notice_sink: Receiver for slice-only-column-omitted notices.

    Returns:
        One SourceTableSpec per unit, in unit order.

    Raises:
        SourceRenameSliceOnly: A rename entry's columns key names a
            policy-omitted slice_only column.
        SourceRenameUnresolved: A rename entry's (table, sub_type) does not
            match any unit, or one of its columns keys is unresolved
            (including one an election absorbed or dropped).
        ElectionMixedIdentity: An unsplit sub-typed unit's populations elect
            differing identity surfaces.
        ElectionUnionUnsafe: A uniform presentation_id identity election, or
            a referencing column's admitted target populations, contain a
            pairwise-unsafe key-space pair.
        TemporalClassUnavailableError: Propagated from the omitted-column scan.
    """
    rename_by_key: dict[tuple[str, str | None], RenameEntry] = {}
    if rename is not None:
        for entry in rename:
            rename_by_key[(entry.table, entry.sub_type)] = entry

    known_kinds = _known_records_kinds(sidecar)
    known_kinds_set = frozenset(known_kinds)

    matched_keys: set[tuple[str, str | None]] = set()
    specs: list[SourceTableSpec] = []
    for unit in units:
        omitted_names = (
            ()
            if unit.genre == "junction"
            else _omitted_slice_only_columns(sidecar, unit.kind)
        )
        for column_name in omitted_names:
            notice_sink(_slice_only_omission_notice(unit, column_name))
        omitted = frozenset(omitted_names)

        name = _default_table_name(unit)
        columns = _default_columns(sidecar, unit, change_delivery, omitted)

        if unit.genre == "junction":
            identity_surface: "KeySurface" = "record_id"
            edge_surfaces = _resolve_junction_edges(
                sidecar, election, unit.source_table, unit.kind, known_kinds, name
            )
        else:
            identity_surface = _resolve_identity_surface(
                sidecar, election, unit.kind, unit.sub_type, name
            )
            columns = _apply_identity_election(columns, identity_surface)
            elects_reference_edges = unit.genre != "changelog" or (
                change_delivery == "snapshot"
            )
            edge_surfaces = (
                _resolve_reference_prop_edges(
                    sidecar,
                    election,
                    unit.source_table,
                    columns,
                    known_kinds_set,
                    name,
                )
                if elects_reference_edges
                else ()
            )

        key = (unit.source_table, unit.sub_type)
        matched_entry = rename_by_key.get(key)
        if matched_entry is not None:
            matched_keys.add(key)
            name, columns = _apply_rename_entry(matched_entry, name, columns, omitted)

        specs.append(
            SourceTableSpec(
                source_table=unit.source_table,
                sub_type=unit.sub_type,
                genre=unit.genre,
                name=name,
                columns=columns,
                identity_surface=identity_surface,
                edge_surfaces=edge_surfaces,
            )
        )

    if rename is not None:
        for entry in rename:
            key = (entry.table, entry.sub_type)
            if key not in matched_keys:
                raise SourceRenameUnresolved(
                    f"rename entry '{entry.table}': table/sub_type does not"
                    " resolve to an exported unit"
                )

    return tuple(specs)


# ---------------------------------------------------------------------------
# Collision + reserved-name checks
# ---------------------------------------------------------------------------


def _check_collisions(specs: tuple[SourceTableSpec, ...]) -> None:
    """Enforce SourceNameCollision: unique table names, unique columns per table.

    Args:
        specs: The resolved output table specs.

    Raises:
        SourceNameCollision: Two specs share a name, or one spec's columns
            share an output name.
    """
    name_counts: dict[str, int] = {}
    for spec in specs:
        name_counts[spec.name] = name_counts.get(spec.name, 0) + 1
    duplicate_names = sorted(n for n, count in name_counts.items() if count > 1)
    if duplicate_names:
        raise SourceNameCollision(
            f"output name collision: {duplicate_names}; resolve via source.rename"
        )

    for spec in specs:
        col_counts: dict[str, int] = {}
        for _, out in spec.columns:
            col_counts[out] = col_counts.get(out, 0) + 1
        duplicate_cols = sorted(n for n, count in col_counts.items() if count > 1)
        if duplicate_cols:
            raise SourceNameCollision(
                f"output name collision in table '{spec.name}': {duplicate_cols};"
                " resolve via source.rename"
            )


def _check_reserved_names(specs: tuple[SourceTableSpec, ...]) -> None:
    """Enforce the reserved-name rule: no output name collides with cross-mode
    incremental bookkeeping names/suffixes, checked at plan build (always-on,
    full export included) so a full export and a later incremental drip on
    the same target agree; nor with the presentation-name posture
    (`last_mutation_sim_time` — a sim-internal column read freely, delivered
    under its own output name never).

    Args:
        specs: The resolved output table specs.

    Raises:
        ExportError: A table name is `_export_meta` / `_export_windows` or ends
            in `__rows`; a column is named `__valid_from_ns`; or a column is
            named `last_mutation_sim_time`.
    """
    for spec in specs:
        if is_reserved_table_name(spec.name):
            raise ExportError(
                f"table '{spec.name}': name is reserved under incremental export"
            )
        for _, out in spec.columns:
            if out == RESERVED_PRESENTATION_COLUMN_NAME:
                raise ExportError(
                    f"table '{spec.name}': column '{out}' names the reserved"
                    " last_mutation_sim_time column — it is sim-internal"
                    " bookkeeping; deliver its value via the updated_at"
                    " presentation default or a different source.rename target"
                )
            if is_reserved_column_name(out):
                raise ExportError(
                    f"table '{spec.name}': column '{out}' is reserved under"
                    " incremental export"
                )


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------


def _kind_from_records_table(source_table: str) -> str:
    """Recover a records unit's kind from its `records__<kind>` source table name.

    Args:
        source_table: A `SourceTableSpec.source_table` of genre 'changelog',
            'reference', or 'transaction' — never 'junction'.

    Returns:
        The bare kind name.
    """
    return source_table[len(_RECORDS_TABLE_PREFIX) :]


def _whole_table_claimed(
    presentation_keys: "PresentationKeys | None", kind: str
) -> bool:
    """Whether `kind` carries a whole-column presentation_id uniqueness claim.

    Args:
        presentation_keys: The sidecar's parsed claims view, or None when the
            block is absent.
        kind: The record kind.

    Returns:
        True iff the block carries an entry for `kind` whose whole-table claim
        (a flat kind's `key`, or a partitioned kind's rollup) derives a
        non-None `unique_within`.
    """
    if presentation_keys is None or kind not in presentation_keys.kinds():
        return False
    return presentation_keys.whole_table_claim(kind).unique_within is not None


def _sub_type_claimed(
    presentation_keys: "PresentationKeys | None", kind: str, sub_type: str
) -> bool:
    """Whether a partitioned kind's `sub_type` entry is declared — its presence
    is the claim.

    Args:
        presentation_keys: The sidecar's parsed claims view, or None when the
            block is absent.
        kind: The record kind.
        sub_type: The split unit's discriminator value.

    Returns:
        True iff the block declares `kind`'s partitioned entry and it carries
        a `sub_type` sub-entry.
    """
    if presentation_keys is None:
        return False
    try:
        presentation_keys.key_for(kind, sub_type)
    except KeyError:
        return False
    return True


def resolve_source_table_keys(
    sidecar: "Sidecar",
    spec: SourceTableSpec,
    change_delivery: Literal["changelog", "snapshot"],
) -> TableKeys | None:
    """Resolve one source output table's declared keys, or None for its genre.

    Pure plan-time resolution (design doc § Key resolution per output table,
    'source' rows); the engine calls it only when `declare_keys` is on. Genre
    rule:

    - junction → None (membership rows carry no claimed key).
    - changelog genre under `change_delivery: 'changelog'` → None (multiple
      rows per record; no honest key exists post-render).
    - changelog genre under `change_delivery: 'snapshot'` → whole-table rule
      (one row per record at the horizon; tracked kinds never sub-type
      split).
    - reference / transaction, unsplit (`spec.sub_type is None`) → primary
      key on the elected identity column's output name (`spec.columns`
      keyed on `spec.identity_surface` — the doc's `declare_keys` interplay
      row); unique on `presentation_id`'s output name iff the whole-table
      claim holds (flat `key` entry, or partitioned rollup with non-None
      `unique_within`) AND the kind's own election is not `presentation_id`
      (already the primary key there, not doubly declared) AND the column
      survives (absorbed under `presentation_id` election — never present).
    - reference / transaction, split unit (`spec.sub_type` set) → primary
      key on the elected identity column; unique on `presentation_id`
      (subject to the same non-doubly-declared/survives conditions) iff
      `key_for(kind, sub_type)` exists — the entry's presence is the claim.

    Output names are read from `spec.columns` (source → output pairs), so
    renames are honored. A kind absent from the block declares identity keys
    only.

    Args:
        sidecar: The open emit's sidecar (claims via
            `sidecar.presentation_keys()` — strict-on-read applies).
        spec: The resolved output table spec.
        change_delivery: The mode's change-log delivery, deciding the
            changelog-genre rule.

    Returns:
        The table's declared keys, or None when the genre declares nothing.

    Raises:
        PresentationKeysInvalidError: The block is present and incoherent
            (propagated; plan-time, before any output).
    """
    if spec.genre == "junction":
        return None
    if spec.genre == "changelog" and change_delivery == "changelog":
        return None

    columns = dict(spec.columns)
    id_output = columns[spec.identity_surface]
    pid_output = columns.get("presentation_id")
    kind = _kind_from_records_table(spec.source_table)
    presentation_keys = sidecar.presentation_keys()

    if spec.sub_type is None:
        claimed = _whole_table_claimed(presentation_keys, kind)
    else:
        claimed = _sub_type_claimed(presentation_keys, kind, spec.sub_type)

    unique: tuple[tuple[str, ...], ...] = (
        ((pid_output,),)
        if claimed
        and pid_output is not None
        and spec.identity_surface != "presentation_id"
        else ()
    )
    return TableKeys(primary_key=(id_output,), unique=unique)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_source_plan(
    sidecar: "Sidecar",
    config: "SourceConfig | None",
    notice_sink: "NoticeSink",
    *,
    election: "Election | None" = None,
) -> tuple[SourceTableSpec, ...]:
    """
    Classify the emit and resolve every output table's genre, name, columns,
    and key election.

    Applies the genre trichotomy and the sub-type split over every records and
    membership table in the sidecar, then exclude, presentation defaults
    (delivery-dependent for a change-log kind — § `change_delivery`), the key
    election (per unit: identity gate + resolution, absorption, referencing-
    column gates + resolution — § `_resolve_specs`), and renames, then the
    collision checks. Deterministic: sidecar table order, with split units in
    enum-domain declaration order.

    Each unit's delivered column set excludes non-exempt slice_only columns
    (one 'slice-only-column-omitted' Notice per omitted column per unit, in
    plan order); the collision check and rename resolution run over the
    election-narrowed set; a rename columns key naming an omitted or
    election-absorbed/dropped column raises SourceRenameSliceOnly /
    SourceRenameUnresolved respectively.

    Args:
        sidecar: The open emit's sidecar.
        config: The source section, or None for the bare-mode full dump.
        notice_sink: Receiver for slice-only-column-omitted notices.
        election: The resolved election, or None to resolve the all-default
            election internally (every population elects record_id — the
            caller has no `keys` block to thread, or is an election-free
            internal/test caller).

    Returns:
        One SourceTableSpec per output table, in deterministic order.

    Raises:
        SourceRecordRolesRequired: The sidecar carries no record_roles registry.
        SourceHistoryTrackedRequired: The sidecar carries no history_tracked flags.
        SourceRoleUnknown: An untracked exported kind (or declared sub-type of an
            untracked object-registry kind) has no resolvable role.
        SourceSubtypesUndeclared: An untracked object-registry kind declares no
            <kind>_type enum domain to enumerate its units from.
        SourceExcludeUnresolved: An exclude entry matches nothing in the sidecar.
        SourceRenameSliceOnly: A rename entry's columns key names a
            policy-omitted slice_only column.
        SourceRenameUnresolved: A rename entry's table or sub_type does not resolve, or
            a columns key does not name a source column of the table (including
            one an election absorbed or dropped).
        SourceNameCollision: Two output tables share a name, or two columns of one
            output table share a name, after defaults and renames.
        ExportError: A resolved output table name collides with the cross-mode
            bookkeeping names or reserved suffixes (checked at plan build so a
            full export and a later incremental drip on the same target
            agree), or a resolved output column is named
            `last_mutation_sim_time` (the presentation-name posture).
        SourceUnclassifiedColumn: A records-category column matches no
            records-column taxonomy role.
        ElectionMixedIdentity: An unsplit sub-typed unit's populations elect
            differing identity surfaces.
        ElectionUnionUnsafe: A uniform presentation_id identity election, or
            a referencing column's admitted target populations, contain a
            pairwise-unsafe key-space pair.
        TemporalClassUnavailableError: A consulted column's temporal pair is
            unavailable (non-conformant emit).
    """
    resolved_election = (
        election if election is not None else resolve_election(sidecar, None)
    )

    units = _classify_units(sidecar)

    exclude = config.exclude if config is not None else None
    units = _apply_exclude(units, exclude)

    rename = config.rename if config is not None else None
    change_delivery = config.change_delivery if config is not None else "changelog"
    specs = _resolve_specs(
        sidecar, resolved_election, units, rename, change_delivery, notice_sink
    )

    _check_collisions(specs)
    _check_reserved_names(specs)

    return specs
