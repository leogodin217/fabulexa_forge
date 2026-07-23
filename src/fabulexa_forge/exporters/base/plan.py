"""Base-mode planning: kind enumeration, presentation, and exclude/rename
resolution.

`build_base_plan` is a pure function of `(sidecar, config)` — no SQL, no emit
read beyond the sidecar. It applies, in order: (1) enumeration of every
records-category kind in the sidecar (base classifies nothing — no genre
trichotomy, no sub-type split); (2) `exclude`; (3) operational presentation
defaults (prefix-stripped table name, `record_id -> id`); (4) `rename`; (5) the
collision and reserved-name checks. See
`docs/architecture/base.md` for the semantics this module implements
(no horizon here — render.py's concern).

Layer-direction invariant: imports only the reader, the derivations layer
(the state-at derivation's column order / presentation-id helpers),
fabulexa_forge.errors, the mode-neutral reserved_names, notices (for
`Notice`, and `NoticeSink` TYPE_CHECKING-only), and slice_only modules,
config.models (TYPE_CHECKING only), and stdlib. Never imports
exporters.dimensional.*, exporters.source.*, or exporters.streaming.*.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fabulexa_forge.config.models import BaseConfig, ExcludeDecl, RenameEntry
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.reader.sidecar import Sidecar

from fabulexa_forge.derivations.properties import has_presentation_id
from fabulexa_forge.derivations.state_at import STATE_AT_COLUMNS
from fabulexa_forge.errors import (
    BaseExcludeUnresolved,
    BaseNameCollision,
    BaseRenameSliceOnly,
    BaseRenameUnresolved,
    ExportError,
)
from fabulexa_forge.exporters.notices import Notice
from fabulexa_forge.exporters.reserved_names import (
    RESERVED_PRESENTATION_COLUMN_NAME,
    is_reserved_column_name,
    is_reserved_table_name,
)
from fabulexa_forge.exporters.slice_only import is_non_exempt_slice_only

#: Prefix marking a records-category column as a reconstructable property.
_PROP_PREFIX = "prop__"

#: The `records__<kind>` name prefix stripped for base's default table name.
_RECORDS_PREFIX = "records__"


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
    column_renames: "Mapping[str, str]"
    """State-at column identity -> output name; includes the `record_id -> id`
    default unless a `rename` entry overrides it."""


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


def _state_at_identities(properties: frozenset[str], has_pid: bool) -> tuple[str, ...]:
    """The full set of state-at column identities a kind's table carries.

    The domain a `rename.columns` key must belong to, and the set collision
    and reserved-name checks walk.

    Args:
        properties: The kind's surviving bare property names.
        has_pid: Whether the kind carries `presentation_id`.

    Returns:
        `STATE_AT_COLUMNS`, then `presentation_id` when carried, then one
        `prop__<p>` per property (sorted for determinism — order carries no
        meaning here; emission order is derived at render).
    """
    identities: list[str] = list(STATE_AT_COLUMNS)
    if has_pid:
        identities.append("presentation_id")
    identities.extend(f"{_PROP_PREFIX}{p}" for p in sorted(properties))
    return tuple(identities)


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


def _resolve_naming(
    kind: str,
    matched_entry: "RenameEntry | None",
    valid_identities: frozenset[str],
    omitted: frozenset[str],
) -> tuple[str, dict[str, str]]:
    """Resolve one kind's output table name and column-rename map.

    Args:
        kind: The record kind (its default table name).
        matched_entry: The rename entry targeting this kind's `records__<kind>`
            table, or None when unrenamed.
        valid_identities: The kind's full state-at column identity set (§
            `_state_at_identities`), against which a `columns` key is checked.
        omitted: The kind's `slice_only`-omitted prop__ column names.

    Returns:
        The resolved output table name and the column-rename map (always
        carrying `record_id -> id`, overridable).

    Raises:
        BaseRenameSliceOnly: A `columns` key names an omitted `slice_only` column.
        BaseRenameUnresolved: A `columns` key is not a state-at column identity.
    """
    column_renames: dict[str, str] = {"record_id": "id"}
    if matched_entry is None:
        return kind, column_renames

    name = matched_entry.name if matched_entry.name is not None else kind
    if matched_entry.columns is not None:
        for src_key, out_val in matched_entry.columns.items():
            if src_key in omitted:
                raise BaseRenameSliceOnly(
                    f"rename targets column {src_key!r} on table"
                    f" {matched_entry.table!r}, which is omitted by the"
                    " slice_only policy"
                )
            if src_key not in valid_identities:
                raise BaseRenameUnresolved(
                    f"rename targets column {src_key!r} on table"
                    f" {matched_entry.table!r}, which is not a state-at column"
                    " of this kind"
                )
            column_renames[src_key] = out_val
    return name, column_renames


def _resolve_specs(
    sidecar: "Sidecar",
    kinds: tuple[str, ...],
    rename: "list[RenameEntry] | None",
    notice_sink: "NoticeSink",
) -> tuple[BaseTableSpec, ...]:
    """Resolve every surviving kind's default naming, then apply matching
    rename entries.

    The emission point for `slice-only-column-omitted`: per kind, computes the
    omitted set and emits one notice per kind x column, kind order then
    sidecar column order, before rename resolution and spec assembly.

    Args:
        sidecar: The open emit's sidecar.
        kinds: The classified, exclude-filtered kinds.
        rename: The base.rename entries, or None.
        notice_sink: Receiver for slice-only-column-omitted notices.

    Returns:
        One BaseTableSpec per kind, in kind order.

    Raises:
        BaseRenameSliceOnly: A rename entry's columns key names a
            policy-omitted slice_only column.
        BaseRenameUnresolved: A rename entry's table does not match any
            surviving kind, or one of its columns keys is unresolved.
        TemporalClassUnavailableError: Propagated from the omitted-column scan.
    """
    rename_by_table: dict[str, RenameEntry] = {}
    if rename is not None:
        for entry in rename:
            rename_by_table[entry.table] = entry

    matched_tables: set[str] = set()
    specs: list[BaseTableSpec] = []
    for kind in kinds:
        table = f"{_RECORDS_PREFIX}{kind}"
        omitted_names = _omitted_slice_only_columns(sidecar, kind)
        for column_name in omitted_names:
            notice_sink(_slice_only_omission_notice(kind, column_name))
        omitted = frozenset(omitted_names)

        properties = _surviving_properties(sidecar, kind)
        has_pid = has_presentation_id(sidecar, kind)
        valid_identities = frozenset(_state_at_identities(properties, has_pid))

        matched_entry = rename_by_table.get(table)
        if matched_entry is not None:
            matched_tables.add(table)
        name, column_renames = _resolve_naming(
            kind, matched_entry, valid_identities, omitted
        )

        specs.append(
            BaseTableSpec(
                kind=kind,
                table_name=name,
                properties=properties,
                has_presentation_id=has_pid,
                column_renames=column_renames,
            )
        )

    if rename is not None:
        for entry in rename:
            if entry.table not in matched_tables:
                raise BaseRenameUnresolved(
                    f"rename targets table {entry.table!r}, which is not a"
                    " records kind base emits"
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
        One output name per state-at identity the kind carries, with
        `spec.column_renames` applied (falling back to the raw identity name
        when unrenamed).
    """
    identities = _state_at_identities(spec.properties, spec.has_presentation_id)
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
# Entry point
# ---------------------------------------------------------------------------


def build_base_plan(
    sidecar: "Sidecar",
    config: "BaseConfig | None",
    notice_sink: "NoticeSink",
) -> BasePlan:
    """
    Resolve the time-agnostic plan for a base export: one flat table per surviving
    records kind, its column set, presentation names, and the `slice_only`
    omissions.

    Classifies nothing and reshapes nothing — every non-excluded records kind
    yields exactly one table whose columns are the kind's STATE_AT_COLUMNS with
    `slice_only` properties omitted (one `slice-only-column-omitted` notice each,
    the discriminator carved out) and presentation names applied. Time selection
    (end-of-tape vs a horizon) is supplied at render, not here, so the plan is
    identical for a full, a sliced, and a windowed export.

    Args:
        sidecar: The reader's narrowing view of `base.json`; source of kinds,
            declared property order, `temporal_class`, and `subtype_values`.
        config: The `base` section, or None for a bare current-state dump.
        notice_sink: Required caller-supplied sink for omission notices.

    Returns:
        A `BasePlan`: one `BaseTableSpec` per surviving kind (output name, bare
        property set, presentation_id flag, column-rename map), ready for
        `build_base_render_sql` to render at a caller-chosen horizon. Column
        emission order is fixed (STATE_AT_COLUMNS prefix, presentation_id, then
        `prop__<p>` in sidecar declaration order), so it is derived, not stored.

    Raises:
        BaseRenameSliceOnly: A `rename` entry names an omitted `slice_only` column.
        BaseRenameUnresolved: A `rename` entry's `table` is not a surviving
            `records__<kind>`, or a `columns` key is not a state-at column identity.
        BaseExcludeUnresolved: An `exclude.kinds`/`exclude.tables` entry matches
            nothing base emits.
        BaseNameCollision: Two output tables, or two columns of one output table,
            share a name after presentation defaults and `rename`.
        ExportError: A resolved output name is reserved under incremental export
            (`_export_meta`/`_export_windows`/`*__rows`, `__valid_from_ns`,
            `last_mutation_sim_time`) — checked always-on via
            `exporters.reserved_names`, as source's `_check_reserved_names` does,
            so a full export and a later incremental drip on the same target agree.
        TableNotFoundError: A declared `records__<kind>` table is absent.
    """
    kinds = _classify_kinds(sidecar)

    exclude = config.exclude if config is not None else None
    kinds = _apply_exclude(kinds, exclude)

    rename = config.rename if config is not None else None
    specs = _resolve_specs(sidecar, kinds, rename, notice_sink)

    _check_collisions(specs)
    _check_reserved_names(specs)

    return BasePlan(tables=specs)
