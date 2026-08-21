"""Base-mode planning: kind enumeration, presentation, election, and
exclude/rename/render resolution.

`build_base_plan` is a pure function of `(sidecar, config, election, anchor)`
— no SQL, no emit read beyond the sidecar. It applies, in order: (1)
enumeration of every records-category kind in the sidecar (base classifies
nothing — no genre trichotomy, no sub-type split); (2) `exclude`; (3) per-kind
identity election resolution (`check_identity_election` over every sub-typed
surviving kind's full domain — base never splits, so a mixed election
refuses) and per reference edge target election resolution
(`check_edge_union_safety` over the target kind's full domain); (4)
operational presentation defaults (prefix-stripped table name, the elected
self identity's contract column name `-> id`, `record_index -> <kind>_key`,
and per surviving reference edge `ref_index__<p> -> <p>_key`); (5) `rename`;
(6) `render` — the unified per-table rendering-election map, keyed on the
same pre-default column identities `rename` shares: the bare shorthand form
elects a lifecycle instant, the typed forms (`date_parse` / `instant` /
`decimal` / `json_precision`) elect a payload column
(`TemporalRenderRequiresAnchor`, `RenderKeyResolves`, `DateParseSourceColumn`,
`DecimalSourceIsDouble`, `InstantSourceIsBigint`,
`JsonPrecisionSourceIsVarchar`); (7) the collision and reserved-name checks.
See `docs/architecture/base.md` for the semantics this module implements (no
horizon here — render.py's concern) and
`docs/architecture/pending/key-election.md` § Rendering per mode (Base) for
the election semantics.

Layer-direction invariant: imports only the reader (including the
structural-temporal surface `structural_instant_columns`), the derivations
layer (the state-at derivation's column order / presentation-id helpers),
fabulexa_forge.errors, the mode-neutral reserved_names, notices (for
`Notice`, and `NoticeSink` TYPE_CHECKING-only), the mode-neutral query_spec
and election modules (`TableKeys`; `Election`, `check_identity_election`,
`check_edge_union_safety`, `resolve_election`), and slice_only modules,
config.models (the `RenderElection` typed-election classes —
`DateParseElection` / `InstantElection` / `DecimalElection` /
`JsonPrecisionElection` — imported at runtime for the render-map form
dispatch; TYPE_CHECKING only otherwise, except `KeySurface`),
fabulexa_forge.anchor (TYPE_CHECKING only), and stdlib. Never imports
exporters.dimensional.*, exporters.source.*, or exporters.streaming.*.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import (
        BaseConfig,
        BaseRenderDecl,
        ExcludeDecl,
        KeySurface,
        RenameEntry,
        RenderElection,
    )
    from fabulexa_forge.exporters.election import Election
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge.config.models import (
    DateParseElection,
    DecimalElection,
    InstantElection,
    JsonPrecisionElection,
)
from fabulexa_forge.derivations.properties import has_presentation_id
from fabulexa_forge.derivations.state_at import STATE_AT_COLUMNS
from fabulexa_forge.errors import (
    BaseExcludeUnresolved,
    BaseNameCollision,
    BaseRenameSliceOnly,
    BaseRenameUnresolved,
    DateParseSourceColumn,
    DecimalSourceIsDouble,
    ExportError,
    InstantSourceIsBigint,
    JsonPrecisionSourceIsVarchar,
    RenderKeyResolves,
    TemporalRenderRequiresAnchor,
)
from fabulexa_forge.exporters.election import (
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
from fabulexa_forge.reader.records_columns import structural_instant_columns

#: Prefix marking a records-category column as a reconstructable property.
_PROP_PREFIX = "prop__"

#: The `records__<kind>` name prefix stripped for base's default table name.
_RECORDS_PREFIX = "records__"


@dataclass(frozen=True)
class ReferenceKey:
    """One surviving reference property's index-space edge, resolved at plan time.

    Present only for edges that yield a key column in this emit: a property
    omitted by the `slice_only` policy, or one whose target kind has no records
    table, produces no entry. `<p>_key` (the always-on record-index edge key)
    is unaffected by election; `per_population` / `value_column_shipped` /
    `rendered_type` resolve the elected `prop__<p>` value column alone.
    """

    property_name: str
    """The bare property name — the edge key's default output name stem."""
    target_kind: str
    """The referenced records kind, from the property's sidecar `references`."""
    per_population: "tuple[tuple[str | None, KeySurface], ...]"
    """The target kind's full declared domain, each with its resolved
    election — `(None, surface)` for a flat target kind. Gated pairwise
    union-safe by `check_edge_union_safety`."""
    value_column_shipped: bool
    """Whether `prop__<p>` renders at all. False only when every admitted
    population elects record_index uniformly — the value would duplicate
    `<p>_key`, so it is dropped."""
    rendered_type: str
    """The `prop__<p>` value column's DuckDB type: the owner's own declared
    column type when every population elects record_id (unaffected,
    verbatim), the target's declared `presentation_id` type when uniform
    presentation_id, `'BIGINT'` when uniform record_index (unused — the
    column is dropped), else `'VARCHAR'` (a mix rendered per row,
    record_index values digit-rendered)."""


#: Emitted when a reference property's target kind has no records table in the
#: emit, so no index-space key column can be produced for that edge. The
#: id-space column is unaffected.
NOTICE_REFERENCE_KEY_TARGET_ABSENT = "reference-key-target-absent"


@dataclass(frozen=True)
class BaseTableSpec:
    """One surviving records kind's resolved flat-output shape — time-agnostic."""

    kind: str
    """The records kind (the `records__<kind>` suffix)."""
    table_name: str
    """Output table name after presentation defaults and `rename`."""
    properties: frozenset[str]
    """Bare property names to reconstruct, passed straight to the state-at builder;
    `slice_only` omissions removed, an exempt discriminator retained."""
    has_presentation_id: bool
    """Whether the kind carries presentation_id — drives base's own projection and
    rename of that column in the wrapper (the state-at builder decides for itself)."""
    identity_surface: "KeySurface"
    """The kind's own populations' uniform elected identity surface
    (`check_identity_election`-gated; `'record_id'` under no election).
    Governs the self id-space slot: `record_id` ships the pair
    byte-identical to today; `presentation_id` ships the elected value in
    the id slot (default name `id`, rename key `presentation_id`) and
    absorbs the standalone `presentation_id` payload column;
    `record_index` drops the id-space self column entirely."""
    reference_keys: tuple[ReferenceKey, ...]
    """Surviving reference edges that yield a key column, in sidecar
    column-declaration order of their `prop__<p>` columns. Empty when the kind
    has no reference property, or none that survives."""
    column_renames: "Mapping[str, str]"
    """State-at column identity -> output name; includes the self identity's
    `-> id` default (the elected surface's contract column name, absent
    under `record_index`), `record_index -> <kind>_key`, and one
    `ref_index__<p> -> <p>_key` per `reference_keys` entry defaults, each
    overridable by a `rename` entry."""
    render: "tuple[tuple[str, RenderElection], ...]" = ()
    """Resolved rendering elections, (pre-default column identity, elected
    form) pairs, the matching `BaseRenderDecl.render` iteration order; keys
    gated at plan time against the domain `rename` shares
    (`last_mutation_sim_time` is outside it — the mode never emits it),
    against `RenderKeyResolves`' form-domain check (the bare shorthand
    against the records category's instant-carrying structural columns, a
    typed election against `prop__<p>` payload columns), and against the
    election's own source-type gate (`DecimalSourceIsDouble` /
    `InstantSourceIsBigint` / `JsonPrecisionSourceIsVarchar` /
    `DateParseSourceColumn`). An explicitly-elected lifecycle rendering
    (shorthand or `instant`) additionally requires a resolved anchor
    (`TemporalRenderRequiresAnchor`). Empty when no `render` entry matches
    this kind — every lifecycle instant renders the mode-definitional
    default `timestamp`, every payload column renders verbatim. Defaults to
    empty so existing construction call sites need no change; `_resolve_specs`
    always passes it explicitly."""


@dataclass(frozen=True)
class BasePlan:
    """One `BaseTableSpec` per surviving kind, in sidecar kind-declaration order.
    Identical for full, sliced, and windowed exports — the horizon is supplied at
    render, never here."""

    tables: tuple[BaseTableSpec, ...]


# ---------------------------------------------------------------------------
# Kind enumeration
# ---------------------------------------------------------------------------


def _classify_kinds(sidecar: "Sidecar") -> tuple[str, ...]:
    """Enumerate every records-category kind in the sidecar.

    Base classifies nothing: every records-category table yields exactly one
    kind, in sidecar table order. Membership and fixed-category tables (queue
    state, history) are never a plan entry.

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


def _apply_exclude(
    kinds: tuple[str, ...],
    exclude: "ExcludeDecl | None",
) -> tuple[str, ...]:
    """Drop excluded kinds from the classified kind list.

    `exclude.kinds` matches a records kind directly; `exclude.tables` matches
    a base output table name — the prefix-stripped kind, base's only
    presentation default at this stage (before `rename`), so the two checks
    resolve against the same known set.

    Args:
        kinds: The classified kinds, in sidecar order.
        exclude: The base.exclude declaration, or None.

    Returns:
        The surviving kinds, in their original order.

    Raises:
        BaseExcludeUnresolved: An exclude.kinds or exclude.tables entry
            matches nothing base emits.
    """
    if exclude is None:
        return kinds

    known = set(kinds)
    excluded: set[str] = set()

    for kind in exclude.kinds or ():
        if kind not in known:
            raise BaseExcludeUnresolved(
                f"exclude names {kind!r}, which base does not emit"
            )
        excluded.add(kind)

    for table_name in exclude.tables or ():
        if table_name not in known:
            raise BaseExcludeUnresolved(
                f"exclude names {table_name!r}, which base does not emit"
            )
        excluded.add(table_name)

    return tuple(k for k in kinds if k not in excluded)


# ---------------------------------------------------------------------------
# Property enumeration + state-at identities
# ---------------------------------------------------------------------------


def _omitted_slice_only_columns(sidecar: "Sidecar", kind: str) -> tuple[str, ...]:
    """The kind-invariant omitted set for one records kind.

    Every non-exempt temporal_class: slice_only prop__ column of
    records__<kind>, in sidecar column-declaration order
    (is_non_exempt_slice_only per column).

    Args:
        sidecar: The open emit's sidecar.
        kind: The record kind owning the records__<kind> table.

    Returns:
        Omitted column names (prop__ prefix included), sidecar column order.

    Raises:
        TemporalClassUnavailableError: Propagated.
    """
    table = f"{_RECORDS_PREFIX}{kind}"
    return tuple(
        col.name
        for col in sidecar.columns(table)
        if is_non_exempt_slice_only(sidecar, kind, col.name)
    )


def _surviving_properties(sidecar: "Sidecar", kind: str) -> frozenset[str]:
    """The bare property names a kind's flat table reconstructs.

    Every `prop__<p>` column of `records__<kind>` not in the unit-invariant
    `slice_only` omitted set (the exempt discriminator is never omitted).

    Args:
        sidecar: The open emit's sidecar.
        kind: The record kind.

    Returns:
        Bare property names (no `prop__` prefix), unordered — emission order
        is derived at render, never stored here.

    Raises:
        TemporalClassUnavailableError: Propagated from the omission scan.
    """
    omitted = frozenset(_omitted_slice_only_columns(sidecar, kind))
    table = f"{_RECORDS_PREFIX}{kind}"
    return frozenset(
        col.name[len(_PROP_PREFIX) :]
        for col in sidecar.columns(table)
        if col.name.startswith(_PROP_PREFIX) and col.name not in omitted
    )


def _self_identity(identity_surface: "KeySurface") -> str | None:
    """The state-at identity occupying the self id-space slot, for one election.

    Args:
        identity_surface: The kind's own resolved identity election.

    Returns:
        `'record_id'` (today's shape), `'presentation_id'` (the elected
        value occupies the slot — the standalone payload column absorbed),
        or None (`record_index`: the id-space self column is dropped).
    """
    if identity_surface == "record_index":
        return None
    return identity_surface


def _dropped_reference_value_props(
    reference_keys: tuple[ReferenceKey, ...],
) -> frozenset[str]:
    """Bare property names whose `prop__<p>` value column the election drops.

    Args:
        reference_keys: The kind's resolved surviving reference edges.

    Returns:
        Property names with `value_column_shipped=False` (every admitted
        target population elects record_index uniformly).
    """
    return frozenset(
        rk.property_name for rk in reference_keys if not rk.value_column_shipped
    )


def _state_at_identities(
    properties: frozenset[str],
    has_pid: bool,
    identity_surface: "KeySurface",
    reference_keys: tuple[ReferenceKey, ...],
) -> tuple[str, ...]:
    """The full set of state-at column identities a kind's table carries.

    The domain a `rename.columns` key must belong to, and the set collision
    and reserved-name checks walk.

    Args:
        properties: The kind's surviving bare property names.
        has_pid: Whether the kind carries `presentation_id`.
        identity_surface: The kind's own resolved identity election.
        reference_keys: The kind's resolved surviving reference edges (drive
            which `prop__<p>` reference columns the election drops).

    Returns:
        The self identity (§ `_self_identity`, absent under record_index),
        then `STATE_AT_COLUMNS[1:]`, then `presentation_id` when carried and
        not absorbed into the self slot, then one `prop__<p>` per surviving,
        non-dropped property (sorted for determinism — order carries no
        meaning here; emission order is derived at render).
    """
    identities: list[str] = []
    self_identity = _self_identity(identity_surface)
    if self_identity is not None:
        identities.append(self_identity)
    identities.extend(STATE_AT_COLUMNS[1:])
    if has_pid and identity_surface != "presentation_id":
        identities.append("presentation_id")
    dropped = _dropped_reference_value_props(reference_keys)
    identities.extend(
        f"{_PROP_PREFIX}{p}" for p in sorted(properties) if p not in dropped
    )
    return tuple(identities)


# ---------------------------------------------------------------------------
# Reference-edge resolution
# ---------------------------------------------------------------------------


def _known_records_tables(sidecar: "Sidecar") -> frozenset[str]:
    """The `records__<kind>` table names present in the sidecar.

    The domain a reference property's target kind is checked against to
    decide whether its edge key can be produced in this emit.

    Args:
        sidecar: The open emit's sidecar.

    Returns:
        Every records-category table name in the sidecar.
    """
    return frozenset(
        table.name for table in sidecar.tables() if table.category == "records"
    )


def _reference_key_target_absent_notice(
    kind: str, property_name: str, target_kind: str
) -> Notice:
    """Build the 'reference-key-target-absent' notice for one kind x property.

    Args:
        kind: The record kind owning the reference property.
        property_name: The bare reference property name.
        target_kind: The property's referenced kind, absent from the sidecar.

    Returns:
        The rendered Notice, naming the kind, the property, and the absent
        target kind.
    """
    return Notice(
        code=NOTICE_REFERENCE_KEY_TARGET_ABSENT,
        message=(
            f"kind '{kind}': property '{property_name}' references kind"
            f" '{target_kind}', which has no records table in this emit;"
            " no key column produced for this edge"
        ),
    )


def _column_type(sidecar: "Sidecar", table_name: str, column_name: str) -> str:
    """One column's declared DuckDB type.

    Args:
        sidecar: The open emit's sidecar.
        table_name: A sidecar table name.
        column_name: The column to look up.

    Returns:
        The column's declared DuckDB type.

    Raises:
        ExportError: `table_name` declares no column named `column_name` — a
            caller invariant error (callers only look up columns the sidecar
            is already known to carry).
    """
    for col in sidecar.columns(table_name):
        if col.name == column_name:
            return col.type
    raise ExportError(f"table '{table_name}' declares no column '{column_name}'")


def _resolve_reference_key_surfaces(
    sidecar: "Sidecar",
    election: "Election",
    kind: str,
    property_name: str,
    target_kind: str,
) -> "tuple[tuple[str | None, KeySurface], ...]":
    """Gate and resolve one reference edge's admitted target populations.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        kind: The referencing table's own records kind (for the error label).
        property_name: The bare reference property name (for the error label).
        target_kind: The reference edge's target kind.

    Returns:
        `(sub_type, surface)` pairs over the target kind's full declared
        domain, declaration order (a `(None, surface)` singleton for a flat
        target kind).

    Raises:
        ElectionUnionUnsafe: The admitted target populations' resolved key
            spaces contain a pairwise-unsafe pair.
    """
    domain = sidecar.subtype_values(target_kind)
    edge_name = f"records__{kind}.prop__{property_name}"
    check_edge_union_safety(election, target_kind, domain, edge_name)
    return tuple((p.sub_type, p.surface) for p in election.populations_for(target_kind))


def _resolve_reference_key_rendering(
    sidecar: "Sidecar",
    kind: str,
    property_name: str,
    target_kind: str,
    per_population: "tuple[tuple[str | None, KeySurface], ...]",
) -> tuple[bool, str]:
    """Resolve one reference edge's `prop__<p>` shipping + rendered type.

    Args:
        sidecar: The open emit's sidecar.
        kind: The referencing table's own records kind.
        property_name: The bare reference property name.
        target_kind: The reference edge's target kind.
        per_population: The target's gated per-population election
            (§ `_resolve_reference_key_surfaces`).

    Returns:
        `(value_column_shipped, rendered_type)` — per the doc's per-edge
        column table: uniform record_id ships the owner's own declared type
        (verbatim, unaffected); uniform record_index drops the column
        (`'BIGINT'`, unused); uniform presentation_id ships the target's
        declared `presentation_id` type; any other mix ships `'VARCHAR'`
        (record_index values digit-rendered).
    """
    surfaces = {surface for _, surface in per_population}
    if surfaces == {"record_id"}:
        owner_table = f"{_RECORDS_PREFIX}{kind}"
        owner_column = f"{_PROP_PREFIX}{property_name}"
        return True, _column_type(sidecar, owner_table, owner_column)
    if surfaces == {"record_index"}:
        return False, "BIGINT"
    if surfaces == {"presentation_id"}:
        target_table = f"{_RECORDS_PREFIX}{target_kind}"
        return True, _column_type(sidecar, target_table, "presentation_id")
    return True, "VARCHAR"


def _resolve_reference_keys(
    sidecar: "Sidecar",
    election: "Election",
    kind: str,
    properties: frozenset[str],
    known_records_tables: frozenset[str],
    notice_sink: "NoticeSink",
) -> tuple[ReferenceKey, ...]:
    """Resolve one kind's surviving reference properties to `ReferenceKey` entries.

    Walks `records__<kind>`'s `prop__` columns in sidecar declaration order,
    selecting those in the kind's surviving property set that carry a sidecar
    `references` annotation. A property whose target kind has no records table
    in the sidecar yields no entry and one `reference-key-target-absent`
    notice instead (the id-space column is unaffected). Every surviving edge
    is gated (`check_edge_union_safety`) and its `prop__<p>` shipping and
    rendered type resolved (§ `_resolve_reference_key_rendering`); the
    always-on `<p>_key` record-index edge key is unaffected by election.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        kind: The record kind.
        properties: The kind's surviving bare property names (post `slice_only`).
        known_records_tables: Every `records__<kind>` table name in the sidecar.
        notice_sink: Receiver for `reference-key-target-absent` notices.

    Returns:
        `ReferenceKey` entries, in sidecar `prop__` column-declaration order.

    Raises:
        ElectionUnionUnsafe: A surviving edge's admitted target populations'
            resolved key spaces contain a pairwise-unsafe pair.
    """
    table = f"{_RECORDS_PREFIX}{kind}"
    keys: list[ReferenceKey] = []
    for col in sidecar.columns(table):
        if not col.name.startswith(_PROP_PREFIX) or col.references is None:
            continue
        prop = col.name[len(_PROP_PREFIX) :]
        if prop not in properties:
            continue
        target_kind = col.references
        if f"{_RECORDS_PREFIX}{target_kind}" not in known_records_tables:
            notice_sink(_reference_key_target_absent_notice(kind, prop, target_kind))
            continue
        per_population = _resolve_reference_key_surfaces(
            sidecar, election, kind, prop, target_kind
        )
        value_column_shipped, rendered_type = _resolve_reference_key_rendering(
            sidecar, kind, prop, target_kind, per_population
        )
        keys.append(
            ReferenceKey(
                property_name=prop,
                target_kind=target_kind,
                per_population=per_population,
                value_column_shipped=value_column_shipped,
                rendered_type=rendered_type,
            )
        )
    return tuple(keys)


def _key_identities(reference_keys: tuple[ReferenceKey, ...]) -> tuple[str, ...]:
    """The record-index rename/output identities a kind's table carries.

    Args:
        reference_keys: The kind's resolved surviving reference edges.

    Returns:
        `record_index`, then one `ref_index__<p>` per reference key, in
        `reference_keys` order.
    """
    return (
        "record_index",
        *(f"ref_index__{rk.property_name}" for rk in reference_keys),
    )


# ---------------------------------------------------------------------------
# rename resolution
# ---------------------------------------------------------------------------


def _slice_only_omission_notice(kind: str, column_name: str) -> Notice:
    """Build the 'slice-only-column-omitted' notice for one kind x column.

    Args:
        kind: The record kind the column was omitted from.
        column_name: The omitted prop__ column name.

    Returns:
        The rendered Notice, naming the kind and the column.
    """
    return Notice(
        code="slice-only-column-omitted",
        message=(
            f"kind '{kind}': column '{column_name}' is temporal_class: slice_only;"
            " omitted from the base export"
        ),
    )


def _default_column_renames(
    kind: str,
    identity_surface: "KeySurface",
    reference_keys: tuple[ReferenceKey, ...],
) -> dict[str, str]:
    """The kind's default column-rename map, before any `rename` entry override.

    Args:
        kind: The record kind (drives the self key's default name).
        identity_surface: The kind's own resolved identity election (drives
            the self id-space slot's rename key — `record_id` or
            `presentation_id`; absent entirely under `record_index`).
        reference_keys: The kind's resolved surviving reference edges (drive
            each edge key's default name).

    Returns:
        `<self identity> -> id` (the elected surface's contract column name,
        omitted under `record_index`), `record_index -> <kind>_key`, and one
        `ref_index__<p> -> <p>_key` per reference key.
    """
    renames = {"record_index": f"{kind}_key"}
    self_identity = _self_identity(identity_surface)
    if self_identity is not None:
        renames[self_identity] = "id"
    for rk in reference_keys:
        renames[f"ref_index__{rk.property_name}"] = f"{rk.property_name}_key"
    return renames


def _check_column_domain(
    key: str,
    valid_identities: frozenset[str],
    omitted: frozenset[str],
    table_name: str,
) -> None:
    """Verify `key` names a state-at or key column identity a kind's table
    emits — the domain `rename.columns` and `render` keys share.

    Args:
        key: The candidate column identity.
        valid_identities: The kind's full state-at + key column identity set
            (§ `_state_at_identities`, § `_key_identities`).
        omitted: The kind's `slice_only`-omitted identities — `prop__` column
            names and their `ref_index__` shadow identities.
        table_name: The kind's `records__<kind>` table, for the error.

    Raises:
        BaseRenameSliceOnly: `key` names a column the slice_only policy omits.
        BaseRenameUnresolved: `key` is not a state-at or key column identity
            this emit produces (including one an election absorbed or
            dropped).
    """
    if key in omitted:
        raise BaseRenameSliceOnly(
            f"table {table_name!r}: column {key!r} is omitted by the slice_only policy"
        )
    if key not in valid_identities:
        raise BaseRenameUnresolved(
            f"table {table_name!r}: column {key!r} is not a state-at or key"
            " column identity this emit produces"
        )


def _resolve_naming(
    kind: str,
    identity_surface: "KeySurface",
    matched_entry: "RenameEntry | None",
    valid_identities: frozenset[str],
    omitted: frozenset[str],
    reference_keys: tuple[ReferenceKey, ...],
) -> tuple[str, dict[str, str]]:
    """Resolve one kind's output table name and column-rename map.

    Args:
        kind: The record kind (its default table name).
        identity_surface: The kind's own resolved identity election.
        matched_entry: The rename entry targeting this kind's `records__<kind>`
            table, or None when unrenamed.
        valid_identities: The kind's full state-at + key column identity set
            (§ `_state_at_identities`, § `_key_identities`), against which a
            `columns` key is checked.
        omitted: The kind's `slice_only`-omitted identities — `prop__` column
            names and their `ref_index__` shadow identities.
        reference_keys: The kind's resolved surviving reference edges, driving
            the edge-key rename defaults.

    Returns:
        The resolved output table name and the column-rename map (always
        carrying the self identity's `-> id` default, `record_index ->
        <kind>_key`, and per-edge `ref_index__<p> -> <p>_key` defaults, each
        overridable). A `columns` key naming an identity the election
        absorbed or dropped is unresolvable — it is not in `valid_identities`.

    Raises:
        BaseRenameSliceOnly: A `columns` key names an omitted `slice_only` column.
        BaseRenameUnresolved: A `columns` key is not a state-at or key column
            identity this emit produces (including one an election absorbed
            or dropped).
    """
    column_renames = _default_column_renames(kind, identity_surface, reference_keys)
    if matched_entry is None:
        return kind, column_renames

    name = matched_entry.name if matched_entry.name is not None else kind
    if matched_entry.columns is not None:
        for src_key, out_val in matched_entry.columns.items():
            _check_column_domain(
                src_key, valid_identities, omitted, matched_entry.table
            )
            column_renames[src_key] = out_val
    return name, column_renames


# ---------------------------------------------------------------------------
# `render`: the unified rendering-election map
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


def _verify_render_key_is_instant(key: str, table_name: str) -> None:
    """Enforce RenderKeyResolves' bare-shorthand domain: a `render` key
    names an instant-carrying structural column of the records category.

    Args:
        key: The `render` key, already confirmed a state-at or key column
            identity this kind's table emits.
        table_name: The kind's `records__<kind>` table, for the error.

    Raises:
        RenderKeyResolves: `key` is not among the records category's
            instant-carrying structural columns.
    """
    if key not in structural_instant_columns("records"):
        raise RenderKeyResolves(
            f"table '{table_name}': render key '{key}' is not an"
            " instant-carrying structural column of category 'records'"
        )


def _verify_render_key_is_payload(key: str, table_name: str) -> None:
    """Enforce RenderKeyResolves' typed-form domain: a typed election's
    `render` key names a payload column of the records category
    (`prop__<p>`) — never a structural column, so no rendering ever has two
    spellings.

    Args:
        key: The `render` key, already confirmed a state-at or key column
            identity this kind's table emits.
        table_name: The kind's `records__<kind>` table, for the error.

    Raises:
        RenderKeyResolves: `key` is not a `prop__<p>` payload column.
    """
    if not key.startswith(_PROP_PREFIX):
        raise RenderKeyResolves(
            f"table '{table_name}': render key '{key}' names a typed"
            " election but is not a payload column of category 'records'"
            f" (typed elections require a '{_PROP_PREFIX}' key)"
        )


def _verify_date_parse_source_varchar(
    key: str, col_types: dict[str, str], table_name: str
) -> None:
    """Enforce DateParseSourceColumn: a `date_parse` election's key resolves
    to a declared VARCHAR column.

    Args:
        key: The `render` key, already confirmed a state-at or key column
            identity this kind's table emits.
        col_types: Every declared column of the kind's `records__<kind>`
            table, name -> declared DuckDB type (§ `_column_types`).
        table_name: The kind's `records__<kind>` table, for the error.

    Raises:
        DateParseSourceColumn: `key`'s declared type is not VARCHAR.
    """
    sql_type = col_types.get(key)
    if sql_type is None or sql_type.upper() != "VARCHAR":
        got = sql_type if sql_type is not None else "no declared type"
        raise DateParseSourceColumn(
            f"date_parse column '{key}' on '{table_name}': source must be an"
            f" existing VARCHAR column (got {got})"
        )


#: Per typed-election kind (excluding `date_parse`, whose message shape
#: predates this map and stays its own function): the required declared
#: source type, the error class its gate raises, and the full reason clause
#: spliced into that error's message ahead of "(got <type>)".
_TYPED_ELECTION_SOURCE_GATES: dict[str, tuple[str, type[ExportError], str]] = {
    "decimal": (
        "DOUBLE",
        DecimalSourceIsDouble,
        "decimal rendering requires a DOUBLE source",
    ),
    "instant": (
        "BIGINT",
        InstantSourceIsBigint,
        "instant rendering requires a BIGINT sim-time source",
    ),
    "json_precision": (
        "VARCHAR",
        JsonPrecisionSourceIsVarchar,
        "json_precision requires a VARCHAR JSON payload source",
    ),
}


def _verify_typed_election_source_type(
    key: str, form: str, col_types: dict[str, str], table_name: str
) -> None:
    """Enforce one typed election's source-type gate (`DecimalSourceIsDouble`
    / `InstantSourceIsBigint` / `JsonPrecisionSourceIsVarchar`) — the one
    shape every non-`date_parse` typed election's gate shares, per
    `_TYPED_ELECTION_SOURCE_GATES`.

    Args:
        key: The `render` key, already confirmed a payload column.
        form: The election's form name (`decimal` / `instant` /
            `json_precision`), keying `_TYPED_ELECTION_SOURCE_GATES`.
        col_types: Every declared column of the kind's `records__<kind>`
            table, name -> declared DuckDB type.
        table_name: The kind's `records__<kind>` table, for the error.

    Raises:
        DecimalSourceIsDouble, InstantSourceIsBigint,
            JsonPrecisionSourceIsVarchar: `key`'s declared type does not
            match `form`'s required source type.
    """
    expected_type, error_cls, reason = _TYPED_ELECTION_SOURCE_GATES[form]
    sql_type = col_types.get(key)
    if sql_type is None or sql_type.upper() != expected_type:
        got = sql_type if sql_type is not None else "no declared type"
        raise error_cls(f"render key '{key}' on '{table_name}': {reason} (got {got})")


def _verify_render_election(
    key: str,
    value: "RenderElection",
    col_types: dict[str, str],
    table_name: str,
) -> None:
    """Enforce RenderKeyResolves' form-domain check plus the election's own
    source-type gate, for one resolved `render` entry.

    Args:
        key: The `render` key, already confirmed a state-at or key column
            identity this kind's table emits.
        value: The parsed election value.
        col_types: Every declared column of the kind's `records__<kind>`
            table, name -> declared DuckDB type.
        table_name: The kind's `records__<kind>` table, for the error.

    Raises:
        RenderKeyResolves: `key` is outside `value`'s form domain.
        DecimalSourceIsDouble, InstantSourceIsBigint,
            JsonPrecisionSourceIsVarchar, DateParseSourceColumn: `key`'s
            declared type fails the election's source-type gate.
    """
    if isinstance(value, str):
        _verify_render_key_is_instant(key, table_name)
        return
    _verify_render_key_is_payload(key, table_name)
    if isinstance(value, DecimalElection):
        _verify_typed_election_source_type(key, "decimal", col_types, table_name)
    elif isinstance(value, InstantElection):
        _verify_typed_election_source_type(key, "instant", col_types, table_name)
    elif isinstance(value, JsonPrecisionElection):
        _verify_typed_election_source_type(key, "json_precision", col_types, table_name)
    else:
        assert isinstance(value, DateParseElection), (
            f"unrecognized RenderElection form for key {key!r}: {value!r}"
        )
        _verify_date_parse_source_varchar(key, col_types, table_name)


def _render_requires_anchor(value: "RenderElection") -> bool:
    """Whether an elected rendering is a temporal-family election requiring
    a resolved anchor: the bare shorthand or `instant` (both compile through
    the wallclock renderer); `decimal` / `json_precision` / `date_parse`
    read no sim_time and carry no anchor requirement.

    Args:
        value: The parsed election value.

    Returns:
        True for the bare shorthand form or `InstantElection`.
    """
    return isinstance(value, str) or isinstance(value, InstantElection)


def _resolve_table_render(
    decl: "BaseRenderDecl | None",
    valid_identities: frozenset[str],
    omitted: frozenset[str],
    table_name: str,
    anchor: "EffectiveAnchor | None",
    col_types: dict[str, str],
) -> "tuple[tuple[str, RenderElection], ...]":
    """Resolve one kind's declared unified `render` map (§ `BaseTableSpec.render`).

    Gates each key through the domain `rename` shares
    (`_check_column_domain` — `last_mutation_sim_time` is outside it, the
    mode never emits it), then `_verify_render_election` (RenderKeyResolves'
    form-domain check plus the election's own source-type gate), then
    `TemporalRenderRequiresAnchor` for a temporal-family election (the bare
    shorthand or `instant`): every explicitly-elected lifecycle rendering
    requires a resolved anchor, since base's anchor is optional and a None
    anchor has no wallclock calendar to interpolate against; `decimal` /
    `json_precision` / `date_parse` carry no such requirement.

    Args:
        decl: The `BaseRenderDecl` matching this kind's table, or None.
        valid_identities: The kind's full state-at + key column identity set.
        omitted: The kind's `slice_only`-omitted identities.
        table_name: The kind's `records__<kind>` table.
        anchor: The resolved effective anchor, or None.
        col_types: Every declared column of the kind's table, name ->
            declared DuckDB type (§ `_column_types`).

    Returns:
        The resolved (column identity, elected form) pairs, `decl.render`
        iteration order; empty when `decl` is None or carries no `render`
        map.

    Raises:
        BaseRenameSliceOnly, BaseRenameUnresolved: Propagated from
            `_check_column_domain`.
        RenderKeyResolves, DecimalSourceIsDouble, InstantSourceIsBigint,
            JsonPrecisionSourceIsVarchar, DateParseSourceColumn: Propagated
            from `_verify_render_election`.
        TemporalRenderRequiresAnchor: A temporal-family election is set and
            no anchor resolved.
    """
    if decl is None or decl.render is None:
        return ()
    resolved: list[tuple[str, "RenderElection"]] = []
    for key, value in decl.render.items():
        _check_column_domain(key, valid_identities, omitted, table_name)
        _verify_render_election(key, value, col_types, table_name)
        if _render_requires_anchor(value) and anchor is None:
            raise TemporalRenderRequiresAnchor(
                f"column '{key}': rendering '{value}' requires a resolved"
                " anchor; this emit declares no runtime calendar and none"
                " was supplied"
            )
        resolved.append((key, value))
    return tuple(resolved)


def _resolve_identity_surface(
    sidecar: "Sidecar", election: "Election", kind: str, table_name: str
) -> "KeySurface":
    """Resolve one kind's own uniform elected identity surface.

    A flat kind (no discriminator domain) has one population — trivially
    uniform, no gate needed. A sub-typed kind is gated
    (`check_identity_election`) over its full declared domain (base never
    splits, so every surviving sub-typed kind is checked whether or not any
    `keys` entry addresses it); once gated, every population resolves
    identically, so any domain member's surface is the kind's answer.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        kind: The record kind.
        table_name: The kind's `records__<kind>` table, for the gate's error.

    Returns:
        The kind's uniform elected surface (`'record_id'` under no election).

    Raises:
        ElectionMixedIdentity: The kind's populations elect differing surfaces.
        ElectionUnionUnsafe: A uniform presentation_id election whose
            populations' key spaces contain a pairwise-unsafe pair.
    """
    domain = sidecar.subtype_values(kind)
    if not domain:
        return election.surface_for(kind, None)
    check_identity_election(election, kind, domain, table_name)
    return election.surface_for(kind, domain[0])


def _resolve_specs(
    sidecar: "Sidecar",
    election: "Election",
    kinds: tuple[str, ...],
    rename: "list[RenameEntry] | None",
    render: "list[BaseRenderDecl] | None",
    anchor: "EffectiveAnchor | None",
    notice_sink: "NoticeSink",
) -> tuple[BaseTableSpec, ...]:
    """Resolve every surviving kind's election, default naming, then apply
    matching rename and render entries.

    The emission point for `slice-only-column-omitted` and
    `reference-key-target-absent`: per kind, computes the omitted set and the
    resolved reference keys and emits their notices, kind order then sidecar
    column order, before rename/render resolution and spec assembly.

    Args:
        sidecar: The open emit's sidecar.
        election: The resolved election.
        kinds: The classified, exclude-filtered kinds.
        rename: The base.rename entries, or None.
        render: The base.render entries, or None.
        anchor: The resolved effective anchor, or None.
        notice_sink: Receiver for slice-only-column-omitted and
            reference-key-target-absent notices.

    Returns:
        One BaseTableSpec per kind, in kind order.

    Raises:
        BaseRenameSliceOnly: A rename or render entry's key names a
            policy-omitted slice_only column or its ref_index__ shadow.
        BaseRenameUnresolved: A rename entry's table does not match any
            surviving kind, a render entry's table does not match any
            surviving kind, or a rename/render key is unresolved (including
            one an election absorbed or dropped).
        DateParseSourceColumn: A `date_parse` election's key does not resolve
            to a declared VARCHAR column.
        DecimalSourceIsDouble, InstantSourceIsBigint,
            JsonPrecisionSourceIsVarchar: A typed election's key does not
            resolve to its required declared source type.
        ElectionMixedIdentity: A sub-typed kind's populations elect differing
            surfaces.
        ElectionUnionUnsafe: A uniform presentation_id election, or a
            reference edge's admitted target populations, contain a
            pairwise-unsafe key-space pair.
        RenderKeyResolves: A `render` key is outside its value form's key
            domain — the bare shorthand against the records category's
            instant-carrying structural columns, a typed election against
            `prop__<p>` payload columns.
        TemporalClassUnavailableError: Propagated from the omitted-column scan.
        TemporalRenderRequiresAnchor: A temporal-family `render` entry
            (the bare shorthand or `instant`) elects a rendering and no
            anchor resolved.
    """
    rename_by_table: dict[str, RenameEntry] = {}
    if rename is not None:
        for entry in rename:
            rename_by_table[entry.table] = entry

    render_by_table: dict[str, BaseRenderDecl] = {}
    if render is not None:
        for render_entry_decl in render:
            render_by_table[render_entry_decl.table] = render_entry_decl

    known_records_tables = _known_records_tables(sidecar)

    matched_tables: set[str] = set()
    matched_render_tables: set[str] = set()
    specs: list[BaseTableSpec] = []
    for kind in kinds:
        table = f"{_RECORDS_PREFIX}{kind}"
        omitted_names = _omitted_slice_only_columns(sidecar, kind)
        for column_name in omitted_names:
            notice_sink(_slice_only_omission_notice(kind, column_name))
        omitted = frozenset(omitted_names)
        omitted_ref_index_shadow = frozenset(
            f"ref_index__{name[len(_PROP_PREFIX) :]}" for name in omitted
        )
        omitted_domain = omitted | omitted_ref_index_shadow

        properties = _surviving_properties(sidecar, kind)
        has_pid = has_presentation_id(sidecar, kind)
        identity_surface = _resolve_identity_surface(sidecar, election, kind, table)
        reference_keys = _resolve_reference_keys(
            sidecar, election, kind, properties, known_records_tables, notice_sink
        )
        valid_identities = frozenset(
            _state_at_identities(properties, has_pid, identity_surface, reference_keys)
        ) | frozenset(_key_identities(reference_keys))

        matched_entry = rename_by_table.get(table)
        if matched_entry is not None:
            matched_tables.add(table)
        name, column_renames = _resolve_naming(
            kind,
            identity_surface,
            matched_entry,
            valid_identities,
            omitted_domain,
            reference_keys,
        )

        matched_render_entry = render_by_table.get(table)
        if matched_render_entry is not None:
            matched_render_tables.add(table)
        render_pairs = _resolve_table_render(
            matched_render_entry,
            valid_identities,
            omitted_domain,
            table,
            anchor,
            _column_types(sidecar, table),
        )

        specs.append(
            BaseTableSpec(
                kind=kind,
                table_name=name,
                properties=properties,
                has_presentation_id=has_pid,
                identity_surface=identity_surface,
                reference_keys=reference_keys,
                column_renames=column_renames,
                render=render_pairs,
            )
        )

    if rename is not None:
        for entry in rename:
            if entry.table not in matched_tables:
                raise BaseRenameUnresolved(
                    f"rename targets table {entry.table!r}, which is not a"
                    " records kind base emits"
                )

    if render is not None:
        for render_entry_decl in render:
            if render_entry_decl.table not in matched_render_tables:
                raise BaseRenameUnresolved(
                    f"render targets table {render_entry_decl.table!r}, which"
                    " is not a records kind base emits"
                )

    return tuple(specs)


# ---------------------------------------------------------------------------
# Collision + reserved-name checks
# ---------------------------------------------------------------------------


def _output_columns(spec: BaseTableSpec) -> tuple[str, ...]:
    """The final output column names of one resolved table spec.

    Args:
        spec: The resolved output table spec.

    Returns:
        One output name per state-at identity the kind carries, then one per
        key identity (§ `_key_identities`), with `spec.column_renames` applied
        (falling back to the raw identity name when unrenamed).
    """
    identities = _state_at_identities(
        spec.properties,
        spec.has_presentation_id,
        spec.identity_surface,
        spec.reference_keys,
    ) + _key_identities(spec.reference_keys)
    return tuple(spec.column_renames.get(identity, identity) for identity in identities)


def _check_collisions(specs: tuple[BaseTableSpec, ...]) -> None:
    """Enforce BaseNameCollision: unique table names, unique columns per table.

    Args:
        specs: The resolved output table specs.

    Raises:
        BaseNameCollision: Two specs share a name, or one spec's columns
            share an output name.
    """
    name_counts: dict[str, int] = {}
    for spec in specs:
        name_counts[spec.table_name] = name_counts.get(spec.table_name, 0) + 1
    duplicate_names = sorted(n for n, count in name_counts.items() if count > 1)
    if duplicate_names:
        raise BaseNameCollision(
            f"output table name {duplicate_names[0]!r} is produced by two kinds"
        )

    for spec in specs:
        col_counts: dict[str, int] = {}
        for out in _output_columns(spec):
            col_counts[out] = col_counts.get(out, 0) + 1
        duplicate_cols = sorted(n for n, count in col_counts.items() if count > 1)
        if duplicate_cols:
            raise BaseNameCollision(
                f"output column name {duplicate_cols[0]!r} is produced by two"
                f" columns on table {spec.table_name!r}"
            )


def _check_reserved_names(specs: tuple[BaseTableSpec, ...]) -> None:
    """Enforce the reserved-name rule: no output name collides with cross-mode
    incremental bookkeeping names/suffixes, checked at plan build (always-on,
    full export included) so a full export and a later incremental drip on
    the same target agree; nor with the presentation-name posture
    (`last_mutation_sim_time` — a sim-internal column, never delivered under
    its own output name).

    Args:
        specs: The resolved output table specs.

    Raises:
        ExportError: A table name is `_export_meta` / `_export_windows` or ends
            in `__rows`; a column is named `__valid_from_ns`; or a column is
            named `last_mutation_sim_time`.
    """
    for spec in specs:
        if is_reserved_table_name(spec.table_name):
            raise ExportError(
                f"table '{spec.table_name}': name is reserved under incremental export"
            )
        for out in _output_columns(spec):
            if out == RESERVED_PRESENTATION_COLUMN_NAME:
                raise ExportError(
                    f"table '{spec.table_name}': column '{out}' names the"
                    " reserved last_mutation_sim_time column — it is"
                    " sim-internal bookkeeping and is never emitted by base"
                )
            if is_reserved_column_name(out):
                raise ExportError(
                    f"table '{spec.table_name}': column '{out}' is reserved"
                    " under incremental export"
                )


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------


def resolve_base_table_keys(
    sidecar: "Sidecar",
    spec: BaseTableSpec,
) -> TableKeys:
    """Resolve one base flat table's declared keys from the sidecar alone.

    Pure plan-time resolution (design doc § Key resolution per output table,
    'base' row, extended by § Interplay's `declare_keys` row); the engine
    calls it only when `declare_keys` is on. Under `record_id` / `record_index`
    election the primary key is the record-index self key's post-`rename`
    output name (`column_renames['record_index']`), unchanged; under
    `presentation_id` election the primary key follows the elected identity
    column instead (`column_renames['presentation_id']`) — PK-eligible, its
    table-wide uniqueness guard-established, superseding the always-`UNIQUE`
    posture for that column alone. `unique` contains the record-id column's
    output name only under `record_id` election (absorbed/dropped under any
    other election, so its side claim is not declared), plus the
    `presentation_id` column's output name iff the block claims whole-column
    uniqueness for the kind AND the kind's own election is not
    `presentation_id` (already the primary key there, not doubly declared): a
    flat kind's `key` entry (every entry carries a `unique_within`), or a
    partitioned kind's rollup with a non-None `unique_within`. A kind absent
    from the block, or an absent block, yields identity keys only.
    `unique_within` scope ('emit' vs 'branch') is not surfaced — both are
    table-wide under the single-branch guard.

    Args:
        sidecar: The open emit's sidecar (claims read via
            `sidecar.presentation_keys()` — strict-on-read applies).
        spec: The resolved table spec (post-rename names in
            `spec.column_renames`).

    Returns:
        The table's declared keys (never None — the base primary key is a
        contract guarantee, claim or no claim).

    Raises:
        PresentationKeysInvalidError: The sidecar block is present and
            incoherent (propagated from the accessor; plan-time, before any
            output).
    """
    if spec.identity_surface == "presentation_id":
        primary_key = (spec.column_renames["presentation_id"],)
    else:
        primary_key = (spec.column_renames["record_index"],)

    unique: list[tuple[str, ...]] = []
    if spec.identity_surface == "record_id":
        unique.append((spec.column_renames["record_id"],))

    presentation_keys = sidecar.presentation_keys()
    if (
        spec.identity_surface != "presentation_id"
        and presentation_keys is not None
        and spec.kind in presentation_keys.kinds()
    ):
        claim = presentation_keys.whole_table_claim(spec.kind)
        if claim.unique_within is not None:
            pid_out = spec.column_renames.get("presentation_id", "presentation_id")
            unique.append((pid_out,))

    return TableKeys(primary_key=primary_key, unique=tuple(unique))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_base_plan(
    sidecar: "Sidecar",
    config: "BaseConfig | None",
    notice_sink: "NoticeSink",
    *,
    election: "Election | None" = None,
    anchor: "EffectiveAnchor | None" = None,
) -> BasePlan:
    """
    Resolve the time-agnostic plan for a base export: one flat table per surviving
    records kind, its column set, election, presentation names, and the
    `slice_only` omissions.

    Classifies nothing and reshapes nothing — every non-excluded records kind
    yields exactly one table whose columns are the kind's STATE_AT_COLUMNS with
    `slice_only` properties omitted (one `slice-only-column-omitted` notice each,
    the discriminator carved out) and presentation names applied. Base never
    splits: every surviving sub-typed kind's full domain is gated uniform
    (`check_identity_election`), stamping `BaseTableSpec.identity_surface`.
    Also resolves each kind's surviving reference properties (a `prop__<p>`
    carrying a sidecar `references` annotation) to `ReferenceKey` entries,
    each gated (`check_edge_union_safety` over the target kind's full
    domain) and stamped with its per-population election, `prop__<p>`
    shipping, and rendered type; a property whose target kind has no records
    table in the sidecar yields no entry and one `reference-key-target-absent`
    notice instead (the id-space column is unaffected — it is never gated,
    per the doc's kind-exists consequence). The self-column resolution table
    (drop the id-space column under `record_index`, the elected value column
    in the id slot under `presentation_id`, absorption of the standalone
    `presentation_id` payload column) is applied to `column_renames` keying:
    the self value column's rename key is the elected surface's contract
    column name. Time selection (end-of-tape vs a horizon) is supplied at
    render, not here, so the plan is identical for a full, a sliced, and a
    windowed export.

    Args:
        sidecar: The reader's narrowing view of `base.json`; source of kinds,
            declared property order, `temporal_class`, and `subtype_values`.
        config: The `base` section, or None for a bare current-state dump.
        notice_sink: Required caller-supplied sink for omission and
            reference-key-target-absent notices.
        election: The resolved election, or None to resolve the all-default
            election internally (every population elects record_id — the
            caller has no `keys` block to thread, or is an election-free
            internal/test caller).
        anchor: The resolved effective anchor, or None to render lifecycle
            timestamps as raw sim-time ns. Base does not require one — unlike
            source, a None anchor is not itself an error; it only refuses an
            explicit `render` election (`TemporalRenderRequiresAnchor`).

    Returns:
        A `BasePlan`: one `BaseTableSpec` per surviving kind (output name, bare
        property set, presentation_id flag, identity surface, reference keys,
        column-rename map, resolved render elections), ready for
        `build_base_render_sql` to render at a caller-chosen horizon. Column
        emission order is fixed (self identity, STATE_AT_COLUMNS[1:],
        presentation_id, then `prop__<p>` in sidecar declaration order, key
        columns interleaved at render), so it is derived, not stored.

    Raises:
        BaseRenameSliceOnly: A `rename` or `render` entry names an omitted
            `slice_only` column or its `ref_index__` shadow identity.
        BaseRenameUnresolved: A `rename` or `render` entry's `table` is not
            a surviving `records__<kind>`, or a `columns`/`render` key is
            not a state-at or key column identity this emit actually
            produces (including one an election absorbed or dropped, or
            `last_mutation_sim_time` — outside the base key domain, the mode
            never emits it).
        BaseExcludeUnresolved: An `exclude.kinds`/`exclude.tables` entry matches
            nothing base emits.
        BaseNameCollision: Two output tables, or two columns of one output table,
            share a name after presentation defaults and `rename`.
        DateParseSourceColumn: A `date_parse` election's key does not resolve
            to a declared VARCHAR column.
        DecimalSourceIsDouble, InstantSourceIsBigint,
            JsonPrecisionSourceIsVarchar: A typed election's key does not
            resolve to its required declared source type.
        ElectionMixedIdentity: A sub-typed kind's surviving populations elect
            differing identity surfaces.
        ElectionUnionUnsafe: A uniform presentation_id identity election, or a
            reference edge's admitted target populations, contain a
            pairwise-unsafe key-space pair.
        ExportError: A resolved output name is reserved under incremental export
            (`_export_meta`/`_export_windows`/`*__rows`, `__valid_from_ns`,
            `last_mutation_sim_time`) — checked always-on via
            `exporters.reserved_names`, as source's `_check_reserved_names` does,
            so a full export and a later incremental drip on the same target agree.
        RenderKeyResolves: A `render` key is outside its value form's key
            domain — the bare shorthand against the records category's
            instant-carrying structural columns, a typed election against
            `prop__<p>` payload columns.
        TableNotFoundError: A declared `records__<kind>` table is absent.
        TemporalRenderRequiresAnchor: A temporal-family `render` entry (the
            bare shorthand or `instant`) elects a rendering and no anchor
            resolved.
    """
    resolved_election = (
        election if election is not None else resolve_election(sidecar, None)
    )

    kinds = _classify_kinds(sidecar)

    exclude = config.exclude if config is not None else None
    kinds = _apply_exclude(kinds, exclude)

    rename = config.rename if config is not None else None
    render = config.render if config is not None else None
    specs = _resolve_specs(
        sidecar, resolved_election, kinds, rename, render, anchor, notice_sink
    )

    _check_collisions(specs)
    _check_reserved_names(specs)

    return BasePlan(tables=specs)
