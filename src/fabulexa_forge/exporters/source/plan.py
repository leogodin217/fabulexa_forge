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
the mode-neutral reserved_names module, the sibling source.columns module,
config.models (TYPE_CHECKING only), and stdlib. Never imports
exporters.dimensional.* or exporters.streaming.*.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from fabulexa_forge.config.models import ExcludeDecl, RenameEntry, SourceConfig
    from fabulexa_forge.reader.sidecar import RecordRoles, Sidecar

from fabulexa_forge.derivations.properties import has_presentation_id
from fabulexa_forge.derivations.row_state_events import resolve_stream_columns
from fabulexa_forge.derivations.state_at import STATE_AT_COLUMNS
from fabulexa_forge.errors import (
    ExportError,
    SourceExcludeUnresolved,
    SourceHistoryTrackedRequired,
    SourceNameCollision,
    SourceRecordRolesRequired,
    SourceRenameUnresolved,
    SourceRoleUnknown,
    SourceSubtypesUndeclared,
    SourceUnclassifiedColumn,
)
from fabulexa_forge.exporters.reserved_names import (
    is_reserved_column_name,
    is_reserved_table_name,
)
from fabulexa_forge.exporters.source.columns import _PROP_PREFIX, _scalar_properties
from fabulexa_forge.reader.records_columns import records_column_role

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


def _changelog_columns(sidecar: "Sidecar", kind: str) -> tuple[tuple[str, str], ...]:
    """The change-log render's fold column set, source -> output.

    Composes the row-state-events fold's after-image column order
    (`resolve_stream_columns`) with the fold's own fixed op/changed_at prefix —
    the same derivation streaming replays, invoked with the kind's full scalar
    property set (tracked and untracked alike; event_class is ordering-only and
    never projected).

    Args:
        sidecar: The open emit's sidecar.
        kind: The (tracked) record kind.

    Returns:
        (source, output) pairs: op, event_sim_time->changed_at, record_id->id,
        presentation_id (when carried), then one prop__<p>-><p> per scalar
        property in sidecar column-declaration order.
    """
    source_table = f"records__{kind}"
    properties = _scalar_properties(sidecar, source_table)
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


def _snapshot_columns(sidecar: "Sidecar", kind: str) -> tuple[tuple[str, str], ...]:
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

    Returns:
        (source, output) pairs: `STATE_AT_COLUMNS` (lifecycle-renamed per
        `_LIFECYCLE_RENAMES`), `presentation_id` when carried, then one
        `prop__<p>` -> `<p>` per scalar property in sidecar column-declaration
        order — the same order `build_state_at_sql` produces.
    """
    source_table = f"records__{kind}"
    pairs: list[tuple[str, str]] = [
        (name, _LIFECYCLE_RENAMES.get(name, name)) for name in STATE_AT_COLUMNS
    ]
    if has_presentation_id(sidecar, kind):
        pairs.append(("presentation_id", "presentation_id"))
    for col in sidecar.columns(source_table):
        if col.name.startswith(_PROP_PREFIX):
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
) -> tuple[tuple[str, str], ...]:
    """The reference/transaction render's faithful column set, source -> output.

    Every column classifies through the records-column taxonomy: identity
    columns are dropped, following `fork_path`'s precedent — except `record_id`,
    which is identity but kept as `id` (design doc § Semantics — Phase-1 exporter
    posture). Presentation and lifecycle columns keep their operational default
    name; payload columns are prefix-stripped.

    Args:
        sidecar: The open emit's sidecar.
        source_table: The unit's records__<kind> table name.
        drop_discriminator: The split unit's own `prop__<kind>_type` column name
            to drop (constant within the table, recoverable from table identity),
            or None to retain every prop__ column (unsplit unit).

    Returns:
        (source, output) pairs in sidecar column order, identity columns dropped
        (`record_id` kept), the lifecycle columns renamed to their operational
        default, prop__ columns prefix-stripped.
    """
    pairs: list[tuple[str, str]] = []
    for col in sidecar.columns(source_table):
        name = col.name
        role = records_column_role(name)
        if role == "identity" and name != "record_id":
            continue
        if drop_discriminator is not None and name == drop_discriminator:
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
) -> tuple[tuple[str, str], ...]:
    """Dispatch a unit to its genre's default column-naming builder.

    Args:
        sidecar: The open emit's sidecar.
        unit: The export unit.
        change_delivery: The source config's delivery mode for change-log
            kinds; irrelevant to every other genre.

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
            return _snapshot_columns(sidecar, unit.kind)
        return _changelog_columns(sidecar, unit.kind)
    drop_discriminator = (
        f"{_PROP_PREFIX}{unit.kind}_type" if unit.sub_type is not None else None
    )
    return _records_columns(sidecar, unit.source_table, drop_discriminator)


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
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Apply one matched rename entry to a unit's default name/columns.

    Args:
        entry: The matched RenameEntry.
        default_name: The unit's default output table name.
        default_columns: The unit's default (source, output) column pairs.

    Returns:
        The (possibly overridden) table name and column pairs.

    Raises:
        SourceRenameUnresolved: A columns key does not name a source column of
            this unit's default columns.
    """
    name = entry.name if entry.name is not None else default_name
    column_overrides = entry.columns
    if column_overrides is None:
        return name, default_columns

    default_sources = {src for src, _ in default_columns}
    for src_key in column_overrides:
        if src_key not in default_sources:
            raise SourceRenameUnresolved(
                f"rename entry '{entry.table}': column '{src_key}' is not a"
                " source column of this table"
            )
    columns = tuple(
        (src, column_overrides.get(src, out)) for src, out in default_columns
    )
    return name, columns


def _resolve_specs(
    sidecar: "Sidecar",
    units: tuple[_Unit, ...],
    rename: "list[RenameEntry] | None",
    change_delivery: Literal["changelog", "snapshot"],
) -> tuple[SourceTableSpec, ...]:
    """Resolve every unit's default naming, then apply matching rename entries.

    Args:
        sidecar: The open emit's sidecar.
        units: The classified, exclude-filtered units.
        rename: The source.rename entries, or None.
        change_delivery: The source config's delivery mode for change-log
            kinds.

    Returns:
        One SourceTableSpec per unit, in unit order.

    Raises:
        SourceRenameUnresolved: A rename entry's (table, sub_type) does not
            match any unit, or one of its columns keys is unresolved.
    """
    rename_by_key: dict[tuple[str, str | None], RenameEntry] = {}
    if rename is not None:
        for entry in rename:
            rename_by_key[(entry.table, entry.sub_type)] = entry

    matched_keys: set[tuple[str, str | None]] = set()
    specs: list[SourceTableSpec] = []
    for unit in units:
        name = _default_table_name(unit)
        columns = _default_columns(sidecar, unit, change_delivery)

        key = (unit.source_table, unit.sub_type)
        matched_entry = rename_by_key.get(key)
        if matched_entry is not None:
            matched_keys.add(key)
            name, columns = _apply_rename_entry(matched_entry, name, columns)

        specs.append(
            SourceTableSpec(
                source_table=unit.source_table,
                sub_type=unit.sub_type,
                genre=unit.genre,
                name=name,
                columns=columns,
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
    incremental bookkeeping names/suffixes, checked at plan build so a full
    export and a later incremental drip on the same target agree.

    Args:
        specs: The resolved output table specs.

    Raises:
        ExportError: A table name is `_export_meta` / `_export_windows` or ends
            in `__rows`, or a column is named `__valid_from_ns`.
    """
    for spec in specs:
        if is_reserved_table_name(spec.name):
            raise ExportError(
                f"table '{spec.name}': name is reserved under incremental export"
            )
        for _, out in spec.columns:
            if is_reserved_column_name(out):
                raise ExportError(
                    f"table '{spec.name}': column '{out}' is reserved under"
                    " incremental export"
                )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_source_plan(
    sidecar: "Sidecar",
    config: "SourceConfig | None",
) -> tuple[SourceTableSpec, ...]:
    """
    Classify the emit and resolve every output table's genre, name, and columns.

    Applies the genre trichotomy and the sub-type split over every records and
    membership table in the sidecar, then exclude, presentation defaults
    (delivery-dependent for a change-log kind — § `change_delivery`), and
    renames, then the collision checks. Deterministic: sidecar table order, with
    split units in enum-domain declaration order.

    Args:
        sidecar: The open emit's sidecar.
        config: The source section, or None for the bare-mode full dump.

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
        SourceRenameUnresolved: A rename entry's table or sub_type does not resolve, or
            a columns key does not name a source column of the table.
        SourceNameCollision: Two output tables share a name, or two columns of one
            output table share a name, after defaults and renames.
        ExportError: A resolved output table name collides with the cross-mode
            bookkeeping names or reserved suffixes (checked at plan build so a
            full export and a later incremental drip on the same target agree).
        SourceUnclassifiedColumn: A records-category column matches no
            records-column taxonomy role.
    """
    units = _classify_units(sidecar)

    exclude = config.exclude if config is not None else None
    units = _apply_exclude(units, exclude)

    rename = config.rename if config is not None else None
    change_delivery = config.change_delivery if config is not None else "changelog"
    specs = _resolve_specs(sidecar, units, rename, change_delivery)

    _check_collisions(specs)
    _check_reserved_names(specs)

    return specs
