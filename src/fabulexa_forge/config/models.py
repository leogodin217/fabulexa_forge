"""Pydantic models for the dimensional export configuration.

All models use `extra='forbid'` to surface unknown fields at parse time.
Each model enforces its own structural constraints via `@model_validator`.
"""

from __future__ import annotations

import math
import re
import string
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import Self

from fabulexa_forge._sql import is_recognized_sql_type

# ---------------------------------------------------------------------------
# Identifier validation (author-supplied names spliced into SQL / filenames)
# ---------------------------------------------------------------------------

_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Kafka-convention topic name: also safe as a filename stem (no separator,
# no traversal), so a `groups` target can never escape the jsonl output dir.
_TOPIC_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _require_sql_identifier(value: str, context: str) -> None:
    """Reject an author-supplied name that is not a plain SQL identifier.

    Names accepted here are later spliced into SQL identifiers and output
    filenames; the pattern forecloses quote break-out and path traversal at
    config load (Principle #7: invalid config errors at load time).

    Args:
        value: The author-supplied name to check.
        context: Prefix for the error message (e.g. "table 'x': name").

    Raises:
        ValueError: `value` does not match ^[A-Za-z_][A-Za-z0-9_]*$.
    """
    if not _SQL_IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            f"{context} {value!r} must be a valid SQL identifier"
            " (letters, digits, underscores; not starting with a digit:"
            " ^[A-Za-z_][A-Za-z0-9_]*$)"
        )


# ---------------------------------------------------------------------------
# Kafka connection config (streaming sink)
# ---------------------------------------------------------------------------


class StrictBaseModel(BaseModel):
    """Base model rejecting unknown fields (extra='forbid')."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class FkClause(StrictBaseModel):
    """A dimension foreign key resolved by a labeled-edge pathfind."""

    to: str
    """The kind whose dimension row this FK resolves to."""
    via: Literal["reference", "membership"]
    """Which edge to pathfind along — a declared reference, or a membership interval."""
    where: dict[str, str] | None = None
    """Membership filter predicates matched against membership-table element columns."""
    member_field: str | None = None
    """Membership-table column holding the member identity to resolve."""
    property: str | None = None
    """The membership property name to join against."""
    path: list[str] | None = None
    """Reference-edge hop chain from the grain kind to the target kind."""
    target_key: Literal["record_id", "presentation_id"] = "record_id"
    """Which identity to write into the fact FK — the natural record_id,
    or the warehouse surrogate presentation_id."""
    as_of: str | None = None
    """For a point-in-time membership FK, the grain column holding the
    firing time T at which membership is resolved."""
    member_path: list[str] | None = None
    """The reference-hop chain from the grain kind to the member identity,
    resolved as of T."""

    @model_validator(mode="after")
    def membership_fk_shape(self) -> Self:
        """Membership-only fields forbidden on reference fk.

        `path` is forbidden on membership fk.
        `as_of`/`member_path` are forbidden on via='reference'.
        `member_path` requires `as_of`.
        """
        if self.via == "reference":
            membership_fields = []
            if self.where is not None:
                membership_fields.append("where")
            if self.member_field is not None:
                membership_fields.append("member_field")
            if self.property is not None:
                membership_fields.append("property")
            if self.as_of is not None:
                membership_fields.append("as_of")
            if self.member_path is not None:
                membership_fields.append("member_path")
            if membership_fields:
                raise ValueError(
                    "fk with via='reference' may not set membership fields: "
                    f"{membership_fields}"
                )
        if self.via == "membership":
            if self.path is not None:
                raise ValueError(
                    "fk with via='membership' may not set 'path'"
                    " (path is only for via='reference')"
                )
            if self.member_path is not None and self.as_of is None:
                raise ValueError(
                    "fk with 'member_path' requires 'as_of'"
                    " (point-in-time membership FK must name the firing-time column)"
                )
            if self.as_of is not None and self.member_path is None:
                raise ValueError(
                    "fk with 'as_of' requires 'member_path'"
                    " (point-in-time membership FK must name the reference path"
                    " to the member identity)"
                )
        return self


class OrdinalSpec(StrictBaseModel):
    """A ROW_NUMBER ordinal over a partition, with a deterministic tie-break."""

    partition_by: str
    """The output column name used to define the partition window."""
    order_by: str
    """The output column name that determines row order within each partition."""


class ValueMapSpec(StrictBaseModel):
    """A value substitution map over a source column; unmapped values -> NULL."""

    from_: str = Field(alias="from")
    """The base-layer source column whose values are substituted."""
    map: dict[str, int | float | str]
    """Key-to-value substitution table; source values not present here become NULL."""

    @model_validator(mode="after")
    def non_empty_collections(self) -> Self:
        """map in a value_map is non-empty."""
        if not self.map:
            raise ValueError("value_map.map must not be empty")
        return self


class TimestampSpec(StrictBaseModel):
    """A sim_time source column rendered as a wallclock TIMESTAMP via the anchor."""

    source: str
    """The base-layer sim_time column to convert to a wallclock TIMESTAMP."""


class ElapsedSpec(StrictBaseModel):
    """A cross-row elapsed time-delta between two correlated events."""

    correlate_on: str
    """The output column that links this row to its counterpart event row."""
    other_where: dict[str, str]
    """Filter predicates identifying the counterpart event row."""
    start_source: str
    """The sim_time column on the counterpart row marking the interval start."""
    end_source: str
    """The sim_time column on this row marking the interval end."""
    unit: Literal["minutes", "seconds", "hours"]
    """The time unit for the computed delta output."""


class DerivedSpec(StrictBaseModel):
    """A computed column; exactly one of the five derivation kinds is set."""

    ordinal: OrdinalSpec | None = None
    """Assigns a ROW_NUMBER within a partition ordered by a named column."""
    value_map: ValueMapSpec | None = None
    """Substitutes source values via a lookup table; unmapped values become NULL."""
    timestamp: TimestampSpec | None = None
    """Converts a sim_time source column to a wallclock TIMESTAMP via the anchor."""
    scd_window: Literal["valid_from", "valid_to"] | None = None
    """Fills the SCD-2 validity bound — valid_from or valid_to — for this column."""
    elapsed: ElapsedSpec | None = None
    """Computes a cross-row time delta between two correlated events."""

    @model_validator(mode="after")
    def exactly_one_derived(self) -> Self:
        """A DerivedSpec sets exactly one derived kind.

        Exactly one of ordinal/value_map/timestamp/scd_window/elapsed must be set.
        """
        set_fields = [
            f
            for f, v in [
                ("ordinal", self.ordinal),
                ("value_map", self.value_map),
                ("timestamp", self.timestamp),
                ("scd_window", self.scd_window),
                ("elapsed", self.elapsed),
            ]
            if v is not None
        ]
        if len(set_fields) != 1:
            raise ValueError(
                "DerivedSpec must set exactly one of"
                " ordinal/value_map/timestamp/scd_window/elapsed; "
                f"got {len(set_fields)}: {set_fields}"
            )
        return self


class LookupClause(StrictBaseModel):
    """Enriches a row with a type-1 scalar property of a related (or its own) record."""

    property: str
    """The property name to project (resolves to prop__<property> on the
    terminal kind)."""
    to: str | None = None
    """The terminal kind to reach via reference-edge pathfind; omit for a self-join."""
    path: list[str] | None = None
    """Reference-edge hop chain from the grain kind to the terminal kind."""

    @model_validator(mode="after")
    def path_requires_to(self) -> Self:
        """`path` is meaningful only for a multi-hop lookup.

        Raises:
            ValueError: `path` is set while `to` is None (a zero-hop self lookup
                has no hops to hint).
        """
        if self.path is not None and self.to is None:
            raise ValueError(
                "lookup 'path' requires 'to' to be set"
                " (path is only meaningful for a multi-hop lookup)"
            )
        return self


class ColumnDecl(StrictBaseModel):
    """One output column declaration with exactly one source mode."""

    name: str
    from_: str | None = Field(default=None, alias="from")
    """The base-layer column to project directly into this output column."""
    fk: FkClause | None = None
    """Resolves a dimension foreign key via labeled-edge pathfind."""
    correlation: str | None = None
    """A base-layer column whose value links rows across tables (correlation key)."""
    derived: DerivedSpec | None = None
    """A computed column produced by one of the five derivation kinds."""
    null: Literal[True] | None = None
    """Emits a NULL column — a placeholder the author intends to fill externally."""
    lookup: LookupClause | None = None
    """Enriches the row with a type-1 scalar property of a related record."""

    @model_validator(mode="before")
    @classmethod
    def remap_yaml_null_key(cls, data: object) -> object:
        """Remap a Python-None key (from YAML bare `null:`) to the string 'null'."""
        if isinstance(data, dict) and None in data:
            data = {("null" if k is None else k): v for k, v in data.items()}
        return data

    @model_validator(mode="after")
    def exactly_one_column_mode(self) -> Self:
        """A ColumnDecl sets exactly one of
        from / fk / correlation / derived / null / lookup.

        Raises:
            ValueError: zero or more than one mode is set.
        """
        set_fields = [
            f
            for f, v in [
                ("from", self.from_),
                ("fk", self.fk),
                ("correlation", self.correlation),
                ("derived", self.derived),
                ("null", self.null),
                ("lookup", self.lookup),
            ]
            if v is not None
        ]
        if len(set_fields) != 1:
            raise ValueError(
                f"ColumnDecl '{self.name}' must set exactly one of"
                f" from/fk/correlation/derived/null/lookup; "
                f"got {len(set_fields)}: {set_fields}"
            )
        return self

    @model_validator(mode="after")
    def name_is_sql_identifier(self) -> Self:
        """`name` is a plain SQL identifier (it becomes an output column name).

        Raises:
            ValueError: `name` does not match ^[A-Za-z_][A-Za-z0-9_]*$.
        """
        _require_sql_identifier(self.name, "column name")
        return self


class SourceDecl(StrictBaseModel):
    """The grain source binding for one output table."""

    grain: Literal["records", "history_point", "history_interval", "membership"]
    """The base-layer grain that drives this table's row set."""
    kind: str
    """The base-layer kind whose table is the primary source."""
    property: str | None = None
    """The property name scoping the history or membership grain."""
    value: str | None = None
    """For history_point grain, the specific property value to filter on."""
    where: dict[str, str] | None = None
    """Membership-only filter predicates matched against membership element columns."""
    filter: dict[str, str] | None = None
    """Records-only filter predicates matched against the kind's records columns."""

    @model_validator(mode="after")
    def source_fields_match_grain(self) -> Self:
        """`property` required for history/membership grains; other fields grain-gated.

        `filter` records-only; `where` membership-only; `value` history_point-only.
        """
        all_non_records = {"history_point", "history_interval", "membership"}

        if self.grain in all_non_records and self.property is None:
            raise ValueError(f"source with grain='{self.grain}' requires 'property'")
        if self.filter is not None and self.grain != "records":
            raise ValueError(
                f"'filter' is only allowed on grain='records'; got grain='{self.grain}'"
            )
        if self.where is not None and self.grain != "membership":
            raise ValueError(
                f"'where' is only allowed on grain='membership';"
                f" got grain='{self.grain}'"
            )
        if self.value is not None and self.grain != "history_point":
            raise ValueError(
                f"'value' is only allowed on grain='history_point';"
                f" got grain='{self.grain}'"
            )
        return self


class TableDecl(StrictBaseModel):
    """One output table: grain source, role, SCD class, key, and columns."""

    name: str
    role: Literal["dim", "fact"]
    """Whether this table is a dimension (dim) or a fact table."""
    scd: Literal["type1", "type2"] | None = None
    """The SCD tracking class for dim tables; absent means no SCD versioning."""
    source: SourceDecl
    """The grain source that drives this table's row set."""
    key: list[str]
    """The output column names forming the primary key of this table."""
    columns: list[ColumnDecl]
    """The ordered list of output column declarations for this table."""

    @model_validator(mode="after")
    def name_is_sql_identifier(self) -> Self:
        """`name` is a plain SQL identifier (it becomes an output table name
        spliced into SQL and output filenames).

        Raises:
            ValueError: `name` does not match ^[A-Za-z_][A-Za-z0-9_]*$.
        """
        _require_sql_identifier(self.name, "table name")
        return self

    @model_validator(mode="after")
    def scd_only_on_dims(self) -> Self:
        """`scd` is set iff role=='dim'; a fact must not declare an SCD class."""
        if self.role == "fact" and self.scd is not None:
            raise ValueError(
                f"table '{self.name}': 'scd' is not allowed on role='fact'"
            )
        return self

    @model_validator(mode="after")
    def non_empty_collections(self) -> Self:
        """columns and key are non-empty."""
        if not self.columns:
            raise ValueError(f"table '{self.name}': 'columns' must not be empty")
        if not self.key:
            raise ValueError(f"table '{self.name}': 'key' must not be empty")
        return self

    @model_validator(mode="after")
    def column_names_unique(self) -> Self:
        """`columns` names no output column twice.

        Raises:
            ValueError: A column name appears more than once within this table.
        """
        seen: set[str] = set()
        duplicates: list[str] = []
        for col in self.columns:
            if col.name in seen:
                duplicates.append(col.name)
            seen.add(col.name)
        if duplicates:
            raise ValueError(
                f"table '{self.name}': 'columns' contains duplicate column"
                f" names: {duplicates}"
            )
        return self


class ExcludeDecl(StrictBaseModel):
    """Kinds and tables dropped before export."""

    kinds: list[str] | None = None
    """Base-layer kind names to drop entirely from the export."""
    tables: list[str] | None = None
    """Output table names to omit from the final export."""

    @model_validator(mode="after")
    def non_empty_collections(self) -> Self:
        """Any present exclude list is non-empty."""
        if self.kinds is not None and not self.kinds:
            raise ValueError("exclude.kinds must not be empty when present")
        if self.tables is not None and not self.tables:
            raise ValueError("exclude.tables must not be empty when present")
        return self


class RenameEntry(StrictBaseModel):
    """One table's output-name overrides, keyed by sidecar identity."""

    table: str
    """The sidecar base-table name this entry targets (records__<kind> or
    membership__<K>__<p>)."""
    sub_type: str | None = None
    """The split unit this entry targets, for an untracked kind whose role registry
    entry is an object (a kind that splits); exactly the declared discriminator value.
    Absent for a tracked kind — tracked kinds never split."""
    name: str | None = None
    """The output table name replacing the derived default."""
    columns: dict[str, str] | None = None
    """Source column name -> replacement output name. Keyed on source identity — the
    base column name, or, for a change-log kind, the canonical-fold name (`op`,
    `event_sim_time`, `record_id`, `presentation_id`, `prop__<p>`). Never the derived
    default output name, so two source columns that share a default output name stay
    individually addressable."""

    @model_validator(mode="after")
    def entry_well_formed(self) -> Self:
        """At least one of name/columns is set; columns (when present) is non-empty
        with non-empty keys/values and distinct values; name/table/sub_type
        (when set) are non-empty strings.

        Raises:
            ValueError: Neither name nor columns is set; columns is empty or has
                an empty key/value; two columns entries share an output name; or
                name/table/sub_type is an empty string.
        """
        if self.name is None and self.columns is None:
            raise ValueError("RenameEntry must set at least one of name / columns")
        if not self.table:
            raise ValueError("RenameEntry.table must be a non-empty string")
        if self.sub_type is not None and not self.sub_type:
            raise ValueError("RenameEntry.sub_type must be a non-empty string")
        if self.name is not None and not self.name:
            raise ValueError("RenameEntry.name must be a non-empty string")
        if self.columns is not None:
            if not self.columns:
                raise ValueError("RenameEntry.columns must not be empty when present")
            for key, value in self.columns.items():
                if not key:
                    raise ValueError("RenameEntry.columns keys must be non-empty")
                if not value:
                    raise ValueError("RenameEntry.columns values must be non-empty")
            seen_values: set[str] = set()
            duplicate_values: list[str] = []
            for value in self.columns.values():
                if value in seen_values:
                    duplicate_values.append(value)
                seen_values.add(value)
            if duplicate_values:
                raise ValueError(
                    "RenameEntry.columns values must be distinct (two source"
                    f" columns may not rename to one output name): {duplicate_values}"
                )
        return self

    @model_validator(mode="after")
    def rename_targets_are_sql_identifiers(self) -> Self:
        """Every rename *target* — `name` and each `columns` value — is a plain
        SQL identifier (targets become output table/column names spliced into
        SQL and filenames; `table` / `columns` keys are sidecar identities and
        stay unrestricted).

        Raises:
            ValueError: A target does not match ^[A-Za-z_][A-Za-z0-9_]*$.
        """
        if self.name is not None:
            _require_sql_identifier(self.name, "RenameEntry.name")
        if self.columns is not None:
            for key, value in self.columns.items():
                _require_sql_identifier(value, f"RenameEntry.columns[{key!r}] target")
        return self


class SourceConfig(StrictBaseModel):
    """The source-mode section: escape hatches over the full-emit dump."""

    exclude: ExcludeDecl | None = None
    """Kinds and sidecar tables dropped before export."""
    rename: list[RenameEntry] | None = None
    """Per-table output-name overrides."""
    change_delivery: Literal["changelog", "snapshot"] = "changelog"
    """How change-log-genre kinds deliver: the wide CDC table (default), or
    full-table snapshots — one per window horizon under a windowed invocation,
    one at the tape's end in a full export. Participates in the incremental
    fingerprint."""

    @model_validator(mode="after")
    def at_least_one_field(self) -> Self:
        """A present `source` section sets at least one of exclude / rename.

        Raises:
            ValueError: No field was explicitly set (source: {} is not
                meaningful — omit the section entirely for a bare full dump).
        """
        if not self.model_fields_set:
            raise ValueError(
                "source section must set at least one of exclude / rename"
                " (an empty source: {} block is not meaningful;"
                " omit the section for a bare full dump)"
            )
        return self

    @model_validator(mode="after")
    def entries_disjoint(self) -> Self:
        """No two rename entries share the same (table, sub_type) target.

        Raises:
            ValueError: Two rename entries target the same (table, sub_type).
        """
        if self.rename is not None:
            seen: set[tuple[str, str | None]] = set()
            duplicates: list[tuple[str, str | None]] = []
            for entry in self.rename:
                key = (entry.table, entry.sub_type)
                if key in seen:
                    duplicates.append(key)
                seen.add(key)
            if duplicates:
                raise ValueError(
                    "SourceConfig.rename contains more than one entry for the"
                    f" same (table, sub_type): {duplicates}"
                )
        return self


class BaseConfig(StrictBaseModel):
    """The base-mode section: presentation escape hatches plus an optional point-in-time slice."""  # noqa: E501

    exclude: ExcludeDecl | None = None
    """Kinds/output tables dropped before export. `kinds` names records kinds;
    `tables` names base output table names."""
    rename: list[RenameEntry] | None = None
    """Per-table (`name`) / per-column (`columns`) overrides, keyed on the sidecar
    `records__<kind>` name. `columns` keys are state-at column identities
    (`record_id`, `presentation_id`, `created_sim_time`, `active`,
    `deactivated_at`, `prop__<p>`). `sub_type` rejected; `table` targets disjoint."""
    slice_at: int | None = None
    """Inclusive point-in-time horizon (sim-time ns). Absent -> tape's end.
    Mutually exclusive with `incremental` (enforced on ExportConfig)."""

    @model_validator(mode="after")
    def at_least_one_field(self) -> Self:
        """A present `base` section sets at least one of exclude/rename/slice_at.

        Raises:
            ValueError: An empty `base: {}` block; omit the section instead.
        """
        if not self.model_fields_set:
            raise ValueError(
                "base section must set at least one of exclude / rename / slice_at"
                " (an empty base: {} block is not meaningful;"
                " omit the section for a bare current-state dump)"
            )
        return self

    @model_validator(mode="after")
    def slice_at_non_negative(self) -> Self:
        """`slice_at`, when set, is a non-negative sim-time ns.

        Raises:
            ValueError: `slice_at` is negative.
        """
        if self.slice_at is not None and self.slice_at < 0:
            raise ValueError(f"base.slice_at must be non-negative; got {self.slice_at}")
        return self

    @model_validator(mode="after")
    def rename_no_sub_type(self) -> Self:
        """No rename entry sets `sub_type` — base never splits a kind.

        Raises:
            ValueError: A rename entry sets `sub_type`.
        """
        if self.rename is not None:
            offenders = [
                entry.table for entry in self.rename if entry.sub_type is not None
            ]
            if offenders:
                raise ValueError(
                    "base.rename entries must not set 'sub_type'"
                    f" (base never splits a kind): {offenders}"
                )
        return self

    @model_validator(mode="after")
    def entries_disjoint(self) -> Self:
        """No two rename entries share a `table` target.

        Raises:
            ValueError: Two rename entries target the same table.
        """
        if self.rename is not None:
            seen: set[str] = set()
            duplicates: list[str] = []
            for entry in self.rename:
                if entry.table in seen:
                    duplicates.append(entry.table)
                seen.add(entry.table)
            if duplicates:
                raise ValueError(
                    "base.rename contains more than one entry for the same"
                    f" table: {duplicates}"
                )
        return self


class DimensionalConfig(StrictBaseModel):
    """The dimensional-mode section: the star-schema declaration."""

    tables: list[TableDecl]
    """The ordered list of dim and fact table declarations for this star schema."""
    exclude: ExcludeDecl | None = None
    """Optional kinds and tables to drop before export."""

    @model_validator(mode="after")
    def non_empty_collections(self) -> Self:
        """tables must be non-empty."""
        if not self.tables:
            raise ValueError("dimensional.tables must not be empty")
        return self

    @model_validator(mode="after")
    def table_names_unique(self) -> Self:
        """`tables` names no output table twice.

        Two TableDecl entries sharing a name would silently collapse to one
        output downstream (query specs are keyed on table name), so a
        duplicate is a load-time error (Principle #7).

        Raises:
            ValueError: A table name appears more than once.
        """
        seen: set[str] = set()
        duplicates: list[str] = []
        for table in self.tables:
            if table.name in seen:
                duplicates.append(table.name)
            seen.add(table.name)
        if duplicates:
            raise ValueError(
                f"dimensional.tables contains duplicate table names: {duplicates}"
            )
        return self


class RebaseConfig(StrictBaseModel):
    """Author-chosen wallclock origin and/or zone for timestamp rendering."""

    base_date: datetime | None = None
    """The wallclock origin to use when converting sim_time values to timestamps."""
    timezone: str | None = None
    """The display/localization timezone applied to rendered timestamps."""

    @model_validator(mode="after")
    def at_least_one_knob(self) -> Self:
        """A present `rebase` block sets at least one of base_date / timezone.

        Raises:
            ValueError: Both base_date and timezone are absent (empty block).
                Surfaced by `load_export_config` as `ConfigError`.
        """
        if self.base_date is None and self.timezone is None:
            raise ValueError(
                "rebase block must set at least one of base_date / timezone"
            )
        return self


class IncrementalConfig(StrictBaseModel):
    """Author-specified incremental export cadence."""

    period: Literal["day", "week", "month"] | None = None
    """Calendar-regime cadence; requires a resolved wallclock anchor at invocation."""
    sim_period_ns: int | None = None
    """Sim-time-regime cadence in nanoseconds; requires no wallclock anchor."""

    @model_validator(mode="after")
    def exactly_one_cadence(self) -> Self:
        """Exactly one of period / sim_period_ns is set; sim_period_ns >= 1."""
        period_set = self.period is not None
        sim_set = self.sim_period_ns is not None
        if period_set and sim_set:
            raise ValueError(
                "incremental: set exactly one of period / sim_period_ns, not both"
            )
        if not period_set and not sim_set:
            raise ValueError(
                "incremental: exactly one of period / sim_period_ns is required"
            )
        if sim_set and self.sim_period_ns is not None and self.sim_period_ns < 1:
            raise ValueError("incremental.sim_period_ns must be >= 1")
        return self


class ExportConfig(StrictBaseModel):
    """Top-level export configuration block."""

    mode: Literal["dimensional", "source", "base"]
    """The export mode; determines which mode-specific section is required."""
    rebase: RebaseConfig | None = None
    """Optional wallclock origin and timezone for timestamp rendering."""
    incremental: IncrementalConfig | None = None
    """Optional incremental export cadence; absent means full export."""
    dimensional: DimensionalConfig | None = None
    """The star-schema declaration for the dimensional mode."""
    source: SourceConfig | None = None
    """The escape-hatch declaration for the source mode; absent means a bare
    full dump with no exclude/rename."""
    base: BaseConfig | None = None
    """The escape-hatch + slice declaration for the base mode; absent means a bare
    current-state dump with no exclude/rename/slice_at."""

    @model_validator(mode="after")
    def mode_section_matches(self) -> Self:
        """The section named by `mode` matches; the other modes' sections are absent.

        `mode='dimensional'` requires the `dimensional` section (unchanged single-arm
        behavior). `mode='source'` and `mode='base'` have no such requirement — both
        sections are pure escape hatches, so a bare `mode: source` / `mode: base`
        (no mode-specific section at all) is a valid full dump. Whichever mode is
        selected, the *other* modes' sections must be absent.

        Raises:
            ValueError: `mode='dimensional'` with the `dimensional` section absent;
                or any mode with another mode's section present.
        """
        if self.mode == "dimensional":
            if self.dimensional is None:
                raise ValueError("mode='dimensional' requires a 'dimensional' section")
            if self.source is not None:
                raise ValueError("mode='dimensional' forbids a 'source' section")
            if self.base is not None:
                raise ValueError("mode='dimensional' forbids a 'base' section")
        elif self.mode == "source":
            if self.dimensional is not None:
                raise ValueError("mode='source' forbids a 'dimensional' section")
            if self.base is not None:
                raise ValueError("mode='source' forbids a 'base' section")
        else:  # mode == "base"
            if self.dimensional is not None:
                raise ValueError("mode='base' forbids a 'dimensional' section")
            if self.source is not None:
                raise ValueError("mode='base' forbids a 'source' section")
        return self

    @model_validator(mode="after")
    def base_slice_at_excludes_incremental(self) -> Self:
        """Reject `base.slice_at` together with an `incremental` block — a pinned
        instant and a window sequence are contradictory temporal selectors.

        Raises:
            ValueError: Both are set.
        """
        if (
            self.base is not None
            and self.base.slice_at is not None
            and self.incremental is not None
        ):
            raise ValueError(
                "base.slice_at and incremental are mutually exclusive"
                " (a pinned instant and a window sequence are contradictory)"
            )
        return self


def _validate_topic_template(template: str) -> None:
    """Validate a topic_template for well-formedness.

    Checks that the template is non-empty, has balanced braces, and contains
    no format-spec or conversion on any placeholder.

    Raises:
        ValueError: Template is empty, has unbalanced braces, a format-spec, or
            a conversion on a placeholder.
    """
    if not template:
        raise ValueError("topic_template must be non-empty")
    formatter = string.Formatter()
    try:
        parsed = list(formatter.parse(template))
    except (ValueError, KeyError) as exc:
        raise ValueError(f"topic_template has unbalanced braces: {template!r}") from exc
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if format_spec:
            raise ValueError(
                f"topic_template placeholder {{{field_name}:{format_spec}}} must not"
                f" carry a format-spec; use a bare {{name}}"
            )
        if conversion:
            raise ValueError(
                f"topic_template placeholder {{{field_name}!{conversion}}} must not"
                f" carry a conversion; use a bare {{name}}"
            )


class RoutingConfig(StrictBaseModel):
    """Layer-B routing policy: content-agnostic topic naming and regrouping."""

    topic_template: str = "{route_table}"
    """Template rendered via `str.format(**attributes)` to produce each event's base
    topic name. Placeholders: {route_table} (always present), {kind} (state-changes),
    {sub_type} (sub-typed kinds only). Validated by `groups_well_formed`."""

    groups: dict[str, list[str]] = {}
    """Many-to-one regrouping: target topic → list of rendered base-topic names it
    absorbs. A rendered name appears in at most one group."""

    table_identity: Literal["source_table", "topic"] = "source_table"
    """Debezium source.table / value-schema name: 'source_table' = route_table;
    'topic' = resolved topic. Ignored by the jsonl format."""

    @model_validator(mode="after")
    def groups_well_formed(self) -> Self:
        """Validate topic_template and groups.

        topic_template must be non-empty with balanced braces and no format-spec or
        conversion on any placeholder. Every groups target must be a valid topic
        name — Kafka convention ^[A-Za-z0-9._-]+$ and not "." or ".." (a target
        also becomes the jsonl sink's filename stem, so this forecloses path
        traversal). Every member must be a non-empty string; a member may appear
        in at most one group.

        Raises:
            ValueError: An empty template, a malformed template (unbalanced brace,
                format-spec, or conversion on a placeholder), an empty or invalid
                target topic name, an empty member string, or a member shared by
                two groups.
        """
        _validate_topic_template(self.topic_template)
        seen_members: set[str] = set()
        for target, members in self.groups.items():
            if not target:
                raise ValueError("groups target topic must be a non-empty string")
            if not _TOPIC_NAME_RE.fullmatch(target) or target in {".", ".."}:
                raise ValueError(
                    f"groups target topic {target!r} must be a valid topic name"
                    " (^[A-Za-z0-9._-]+$ and not '.' or '..')"
                )
            for member in members:
                if not member:
                    raise ValueError(
                        f"groups[{target!r}] contains an empty member string"
                    )
                if member in seen_members:
                    raise ValueError(
                        f"groups member {member!r} appears in more than one group"
                    )
                seen_members.add(member)
        return self


class StreamKindSelection(StrictBaseModel):
    """One kind's participation in the stream: its sub-type scope and carried properties."""  # noqa: E501

    kind: str
    """The base-layer record kind to stream; resolves to records__<kind>."""
    types: list[str] = []
    """Bare <kind>_type values to stream; empty selects all sub-types. A non-empty
    list on a non-sub-typed kind or an unknown value is a business-pass error."""
    properties: list[str]
    """Bare prop__ names (type-1 and type-2) carried in each event's after-image;
    the sidecar's history_tracked flag classifies each. Empty selects identity +
    lifecycle only (no prop columns)."""

    @model_validator(mode="after")
    def types_are_bare(self) -> Self:
        """No entry in `types` carries the prop__ prefix or names the <kind>_type
        column.

        Raises:
            ValueError: A value begins with 'prop__'.
        """
        prefixed = [t for t in self.types if t.startswith("prop__")]
        if prefixed:
            raise ValueError(
                f"types must be bare discriminator values"
                f" (no 'prop__' prefix): {prefixed}"
            )
        return self

    @model_validator(mode="after")
    def properties_are_bare(self) -> Self:
        """No entry in `properties` carries the prop__ prefix.

        Raises:
            ValueError: A property name begins with 'prop__' (it must be the bare
                name; the prefix is implied).
        """
        prefixed = [p for p in self.properties if p.startswith("prop__")]
        if prefixed:
            raise ValueError(
                f"properties must be bare names (no 'prop__' prefix): {prefixed}"
            )
        return self


class DebeziumSourceIdentity(StrictBaseModel):
    """Source-identity masquerade; no-default fields (P7); `schema_` is `schema`."""

    connector: str
    """source.connector; also names the source schema struct
    (io.debezium.connector.<connector>.Source)."""
    name: str
    """source.name (logical server); the envelope/value schema namespace
    (<name>.<table>.Envelope / <name>.<table>.Value)."""
    db: str
    """source.db."""
    schema_: str = Field(alias="schema")
    """source.schema. Aliased: the YAML/wire key is `schema`."""
    version: str
    """source.version."""

    @model_validator(mode="after")
    def source_fields_non_empty(self) -> Self:
        """All source identity fields must be non-empty strings.

        Raises:
            ValueError: Any field is an empty string.
        """
        for field_name, value in [
            ("connector", self.connector),
            ("name", self.name),
            ("db", self.db),
            ("schema", self.schema_),
            ("version", self.version),
        ]:
            if not value:
                raise ValueError(f"debezium.source.{field_name} must be non-empty")
        return self


class DebeziumConfig(StrictBaseModel):
    """Debezium options on a streaming run; read for 'debezium', ignored for 'jsonl'."""

    schemas_enable: bool = True
    """Wrap each message as {schema, payload} (true) or emit the bare payload
    (false). Global to the run."""
    source: DebeziumSourceIdentity
    """The masquerade source identity — required (no default)."""


class ClockConfig(StrictBaseModel):
    """Streaming pace policy: fast (unpaced) or realtime (paced by sim-time spacing)."""

    mode: Literal["fast", "realtime"]
    """`fast` delivers as-fast-as-possible (unpaced). `realtime` paces delivery."""

    speed: float | None = Field(default=None, gt=0)
    """Sim-to-real playback multiplier under `realtime` (e.g. 60.0 = 60x). Required
    under `realtime`, forbidden under `fast`; must be > 0."""

    idle_cap_seconds: float | None = Field(default=None, gt=0)
    """Real-seconds ceiling on inter-event delay under `realtime`; absent = uncapped.
    Forbidden under `fast`; must be > 0."""

    @model_validator(mode="after")
    def mode_fields_consistent(self) -> Self:
        """Enforce per-mode field presence on ClockConfig.

        Raises:
            ValueError: mode='realtime' with speed unset; or mode='fast' with speed or
                idle_cap_seconds set.
        """
        if self.mode == "realtime" and self.speed is None:
            raise ValueError("ClockConfig: mode='realtime' requires speed to be set")
        if self.mode == "fast" and self.speed is not None:
            raise ValueError("ClockConfig: speed is forbidden under mode='fast'")
        if self.mode == "fast" and self.idle_cap_seconds is not None:
            raise ValueError(
                "ClockConfig: idle_cap_seconds is forbidden under mode='fast'"
            )
        return self


class KafkaConfig(StrictBaseModel):
    """Kafka sink connection block; read only for sink='kafka', ignored otherwise."""

    bootstrap_servers: str
    """Comma-separated host:port bootstrap list. Overridden by --bootstrap-servers;
    falls back to FABEXPORT_KAFKA_BOOTSTRAP when neither CLI nor this block supplies
    one. Must be non-empty."""

    @model_validator(mode="after")
    def bootstrap_servers_non_empty(self) -> Self:
        """KafkaConfig.bootstrap_servers must be a non-empty string.

        Raises:
            ValueError: bootstrap_servers is empty.
        """
        if not self.bootstrap_servers:
            raise ValueError("KafkaConfig.bootstrap_servers must be a non-empty string")
        return self


class MembershipSelection(StrictBaseModel):
    """One membership table's participation: owner kind, property, carried fields."""

    owner_kind: str
    """The kind that owns the collection-struct property; resolves to
    membership__<owner_kind>__<property>."""

    property: str
    """The collection-struct property naming the membership table."""

    fields: list[str]
    """Bare element-schema field names carried in each event's payload. A scalar
    field f maps to elem__<f>; a reference field f maps to member__<f>__kind /
    member__<f>__id. Empty carries owner identity (record_id) only. A non-empty
    list naming a field with no elem__/member__ column on the table is a
    business-pass error."""

    @model_validator(mode="after")
    def fields_are_bare(self) -> Self:
        """No entry in `fields` carries the elem__ or member__ prefix.

        Raises:
            ValueError: A field name begins with 'elem__' or 'member__'.
        """
        bad = [
            f for f in self.fields if f.startswith("elem__") or f.startswith("member__")
        ]
        if bad:
            raise ValueError(
                "MembershipSelection.fields must not carry elem__ or"
                f" member__ prefixes: {bad}"
            )
        return self

    @model_validator(mode="after")
    def fields_unique(self) -> Self:
        """No field name appears twice in `fields`.

        Raises:
            ValueError: A field name appears more than once.
        """
        seen: set[str] = set()
        duplicates: list[str] = []
        for f in self.fields:
            if f in seen:
                duplicates.append(f)
            seen.add(f)
        if duplicates:
            raise ValueError(
                f"MembershipSelection.fields contains duplicate names: {duplicates}"
            )
        return self


class StreamConfig(StrictBaseModel):
    """Streaming delivery envelope: content, routing policy, and per-kind scope."""

    content: Literal["state-changes", "membership-events"]
    """The event content axis. Selects the fold the engine materializes and the
    selection list it reads (kinds for state-changes, memberships for
    membership-events). Closed Literal so a further content type is additive."""
    routing: RoutingConfig | None = None
    """Optional Layer-B routing policy; None applies the default policy (leaf topics, no
    groups, source_table). The optional-block `= None` exception, mirroring rebase /
    debezium."""
    kinds: list[StreamKindSelection] = []
    """Non-empty for content='state-changes'; empty otherwise. (Was required+non-empty;
    now content-conditional.)"""
    memberships: list[MembershipSelection] = []
    """Non-empty for content='membership-events'; empty otherwise."""
    rebase: RebaseConfig | None = None
    """Optional wallclock origin/zone for event timestamps; falls back to the
    sidecar runtime anchor. The `= None` default is the one optional-block exception
    to the no-`Optional[X] = None` rule, mirroring the shipped ExportConfig.rebase."""
    debezium: DebeziumConfig | None = None
    """Optional Debezium-format options; omittable for `jsonl` runs (same optional-block
    exception as `rebase`). Required under `--fmt debezium`; enforced by the
    DebeziumRequiresConfig business rule."""
    clock: ClockConfig | None = None
    """Optional pace policy for delivery; None (absent) ≡ mode: fast (unpaced),
    today's behavior. The optional-block `= None` exception, mirroring routing /
    rebase / debezium."""
    kafka: KafkaConfig | None = None
    """Optional Kafka connection block; None ⇒ bootstrap comes from --bootstrap-servers
    or FABEXPORT_KAFKA_BOOTSTRAP. The optional-block `= None` exception, mirroring
    routing / rebase / debezium / clock. Inert unless --sink kafka."""

    @model_validator(mode="after")
    def selection_matches_content(self) -> Self:
        """Exactly the selected content's selection list is populated; the other empty.

        Raises:
            ValueError: content='state-changes' with empty `kinds` or non-empty
                `memberships`; or content='membership-events' with empty `memberships`
                or non-empty `kinds`.
        """
        if self.content == "state-changes":
            if not self.kinds:
                raise ValueError(
                    "StreamConfig.kinds must be non-empty for content='state-changes'"
                )
            if self.memberships:
                raise ValueError(
                    "StreamConfig.memberships must be empty for content='state-changes'"
                )
        else:  # membership-events
            if not self.memberships:
                raise ValueError(
                    "StreamConfig.memberships must be non-empty"
                    " for content='membership-events'"
                )
            if self.kinds:
                raise ValueError(
                    "StreamConfig.kinds must be empty for content='membership-events'"
                )
        return self

    @model_validator(mode="after")
    def kinds_unique(self) -> Self:
        """`kinds` names no kind twice (uniqueness half of the former
        kinds_non_empty_and_unique). A no-op when `kinds` is empty.

        Raises:
            ValueError: A kind name appears more than once.
        """
        seen: set[str] = set()
        duplicates: list[str] = []
        for ks in self.kinds:
            if ks.kind in seen:
                duplicates.append(ks.kind)
            seen.add(ks.kind)
        if duplicates:
            raise ValueError(
                f"StreamConfig.kinds contains duplicate kind names: {duplicates}"
            )
        return self

    @model_validator(mode="after")
    def memberships_unique(self) -> Self:
        """`memberships` names no (owner_kind, property) pair twice.

        Raises:
            ValueError: A (owner_kind, property) pair appears more than once.
        """
        seen: set[tuple[str, str]] = set()
        duplicates: list[tuple[str, str]] = []
        for ms in self.memberships:
            key = (ms.owner_kind, ms.property)
            if key in seen:
                duplicates.append(key)
            seen.add(key)
        if duplicates:
            raise ValueError(
                "StreamConfig.memberships contains duplicate"
                f" (owner_kind, property) pairs: {duplicates}"
            )
        return self


# ---------------------------------------------------------------------------
# Corrupter config (data-quality injection)
# ---------------------------------------------------------------------------


class Target(StrictBaseModel):
    """The base slice an operation acts on: a table selector, optional rows, optional columns. Exactly one of table / tables / glob / category / record_kind is set."""  # noqa: E501

    table: str | None = None
    """One concrete base table name; must be declared in the emit's sidecar."""
    tables: list[str] | None = None
    """Explicit table-name list; every entry must be declared in the sidecar."""
    glob: str | None = None
    """fnmatch pattern over sidecar table names; must match at least one."""
    category: Literal["fixed", "records", "membership"] | None = None
    """Sidecar table category; selects every table of that category."""
    record_kind: str | None = None
    """Record kind; selects every records/membership table of that kind."""
    where: dict[str, str] | None = None
    """Equality row filter; exact column names, each present in >= 1
    resolved table; literals typed per each resolved table's current column
    type (an unrepresentable literal fails in the shared DuckDB cast at
    apply time). A table lacking a key contributes zero rows. None selects
    every row on the sole fork_path."""
    columns: list[str] | None = None
    """The columns the operation touches — exact names or fnmatch patterns
    over the operation's eligible columns. Required for cell/reference
    operations, per-mode for duplicate_rows, forbidden for schema_drift."""

    @model_validator(mode="after")
    def exactly_one_selector(self) -> Self:
        """Exactly one of table / tables / glob / category / record_kind is set;
        tables, when present, is non-empty and names no table twice."""
        set_fields = [
            f
            for f, v in [
                ("table", self.table),
                ("tables", self.tables),
                ("glob", self.glob),
                ("category", self.category),
                ("record_kind", self.record_kind),
            ]
            if v is not None
        ]
        if len(set_fields) != 1:
            raise ValueError(
                "Target: set exactly one of table / tables / glob / category /"
                f" record_kind; got {len(set_fields)}: {set_fields}"
            )
        if self.tables is not None:
            if not self.tables:
                raise ValueError("Target.tables must not be empty when present")
            seen_tables: set[str] = set()
            duplicate_tables: list[str] = []
            for t in self.tables:
                if t in seen_tables:
                    duplicate_tables.append(t)
                seen_tables.add(t)
            if duplicate_tables:
                raise ValueError(
                    f"Target.tables contains duplicate names: {duplicate_tables}"
                )
        return self

    @model_validator(mode="after")
    def columns_non_empty_and_unique(self) -> Self:
        """`columns`, when present, is non-empty and names no column twice."""
        if self.columns is not None:
            if not self.columns:
                raise ValueError("Target.columns must not be empty when present")
            seen: set[str] = set()
            duplicates: list[str] = []
            for c in self.columns:
                if c in seen:
                    duplicates.append(c)
                seen.add(c)
            if duplicates:
                raise ValueError(
                    f"Target.columns contains duplicate names: {duplicates}"
                )
        return self


class Amount(StrictBaseModel):
    """The seeded quantity of units to corrupt: exactly one of rate / count."""

    rate: float | None = None
    """Fraction of the population; 0 < rate <= 1. floor(rate * N) units are drawn."""
    count: int | None = None
    """Absolute unit count; count >= 1. min(count, N) units are drawn."""

    @model_validator(mode="after")
    def exactly_one_quantity(self) -> Self:
        """Exactly one of rate / count is set; rate in (0, 1]; count >= 1."""
        if (self.rate is None) == (self.count is None):
            raise ValueError(
                "Amount: set exactly one of rate / count, not both or neither"
            )
        if self.rate is not None and not (0 < self.rate <= 1):
            raise ValueError(f"Amount.rate must be in (0, 1]; got {self.rate}")
        if self.count is not None and self.count < 1:
            raise ValueError(f"Amount.count must be >= 1; got {self.count}")
        return self


class EntityScoped(StrictBaseModel):
    """Concentrate the draw on a seeded subset of record_id entities."""

    kind: Literal["entity_scoped"]
    entities: Amount
    """Subset size over the distinct record_ids in the pooled population."""


class ClusteredTemporal(StrictBaseModel):
    """Restrict the draw to windows around seeded sim-time cluster centers."""

    kind: Literal["clustered_temporal"]
    column: str
    """Sim-time-valued column, exact name (never a pattern); must exist in
    >= 1 resolved table and be BIGINT wherever it exists. Rows of a table
    lacking it weigh 0."""
    clusters: int
    """Number of cluster centers; >= 1."""
    width: int
    """Window half-width in the column's own units (ns); > 0."""

    @model_validator(mode="after")
    def clusters_and_width_positive(self) -> Self:
        """clusters >= 1; width > 0."""
        if self.clusters < 1:
            raise ValueError(
                f"ClusteredTemporal.clusters must be >= 1; got {self.clusters}"
            )
        if self.width <= 0:
            raise ValueError(f"ClusteredTemporal.width must be > 0; got {self.width}")
        return self


class Correlated(StrictBaseModel):
    """Weight the draw where another column equals a value (MNAR)."""

    kind: Literal["correlated"]
    column: str
    """Condition column, exact name (never a pattern); must exist in >= 1
    resolved table. Rows of a table lacking it keep weight 1."""
    value: str
    """Condition value; typed per the column's DuckDB type at evaluation."""
    weight: float
    """Weight multiplier where the condition holds; > 0. Non-matching and
    NULL rows keep weight 1."""

    @model_validator(mode="after")
    def weight_positive(self) -> Self:
        """weight > 0."""
        if self.weight <= 0:
            raise ValueError(f"Correlated.weight must be > 0; got {self.weight}")
        return self


Placement = Annotated[
    EntityScoped | ClusteredTemporal | Correlated,
    Field(discriminator="kind"),
]
"""The optional biased-draw axis on null_cells / duplicate_rows /
dangle_reference. Deliberately not named Distribution — that model is the
shared numeric-additive delta shape."""


class Distribution(StrictBaseModel):
    """A shared numeric-additive delta shape (near-duplicate jitter; shift_sim_time's `offset`)."""  # noqa: E501

    shape: Literal["uniform", "normal"]
    """Which distribution the per-cell additive delta is drawn from."""
    low: float | None = None
    """Uniform lower bound (inclusive).
    Required and only allowed for shape='uniform'."""
    high: float | None = None
    """Uniform upper bound (inclusive).
    Required and only allowed for shape='uniform'."""
    mean: float | None = None
    """Normal mean. Required and only allowed for shape='normal'."""
    stddev: float | None = None
    """Normal standard deviation (> 0). Required and only allowed for shape='normal'."""

    @model_validator(mode="after")
    def params_match_shape(self) -> Self:
        """shape='uniform' sets low <= high and no normal params; shape='normal' sets
        stddev > 0 and no uniform params."""
        if self.shape == "uniform":
            if self.mean is not None or self.stddev is not None:
                raise ValueError(
                    "Distribution with shape='uniform' must not set mean / stddev"
                )
            if self.low is None or self.high is None:
                raise ValueError(
                    "Distribution with shape='uniform' requires low and high"
                )
            if self.low > self.high:
                raise ValueError(
                    f"Distribution.low ({self.low}) must be <= Distribution.high"
                    f" ({self.high})"
                )
        else:  # normal
            if self.low is not None or self.high is not None:
                raise ValueError(
                    "Distribution with shape='normal' must not set low / high"
                )
            if self.mean is None or self.stddev is None:
                raise ValueError(
                    "Distribution with shape='normal' requires mean and stddev"
                )
            if self.stddev <= 0:
                raise ValueError(f"Distribution.stddev must be > 0; got {self.stddev}")
        return self


class NullCells(StrictBaseModel):
    """Missing-value injection: null a sampled set of value cells."""

    kind: Literal["null_cells"]
    name: str | None = None
    """Author label; becomes each emitted defect's `rule`.
    Defaults to "null_cells#<index>"."""
    target: Target
    amount: Amount
    placement: Placement | None = None
    """Present ⇒ weighted draw; absent ⇒ the shipped uniform draw."""

    @model_validator(mode="after")
    def requires_columns(self) -> Self:
        """target.columns is present (the cells to null)."""
        if self.target.columns is None:
            raise ValueError("null_cells requires target.columns")
        return self


class DeleteRows(StrictBaseModel):
    """Row removal: delete sampled rows from records / membership tables."""

    kind: Literal["delete_rows"]
    name: str | None = None
    """Author label; becomes each emitted defect's `rule`.
    Defaults to "delete_rows#<index>"."""
    target: Target
    amount: Amount
    placement: Placement | None = None
    """Present ⇒ weighted draw; absent ⇒ the shipped uniform draw."""

    @model_validator(mode="after")
    def no_columns(self) -> Self:
        """target carries no `columns` — a row removal touches no specific column."""
        if self.target.columns is not None:
            raise ValueError(
                "delete_rows forbids target.columns (a row removal touches no"
                " specific column)"
            )
        return self


class InsertRows(StrictBaseModel):
    """Phantom-row injection: clone sampled donor rows under fresh record_ids."""

    kind: Literal["insert_rows"]
    name: str | None = None
    """Author label; becomes each emitted defect's `rule`.
    Defaults to "insert_rows#<index>"."""
    target: Target
    amount: Amount
    placement: Placement | None = None
    """Present ⇒ weighted donor draw; absent ⇒ the shipped uniform draw."""


class SchemaDrift(StrictBaseModel):
    """Catalog-level column drift: rename, retype, and/or drop columns. No sampling."""

    kind: Literal["schema_drift"]
    name: str | None = None
    """Author label; becomes each emitted defect's `rule`.
    Defaults to "schema_drift#<index>"."""
    target: Target
    rename_to: dict[str, str] | None = None
    """Column -> new column name."""
    retype_to: dict[str, str] | None = None
    """Column -> new DuckDB type literal."""
    drop: list[str] | None = None
    """Columns to remove from the table."""

    @model_validator(mode="after")
    def table_only_target_and_one_action(self) -> Self:
        """target uses the concrete `table` selector (no tables / glob /
        category / record_kind) and carries no where / columns; at least one
        of rename_to / retype_to / drop is set; their column keys are
        disjoint and drop names no column that is also renamed or retyped."""
        if self.target.table is None:
            raise ValueError(
                "schema_drift target must use the concrete 'table' selector"
                " (rename_to/retype_to/drop maps name exact columns of one"
                " table and do not generalize to a class)"
            )
        if self.target.where is not None or self.target.columns is not None:
            raise ValueError(
                "schema_drift target must carry only 'table' (no where / columns)"
            )
        rename_keys = set(self.rename_to) if self.rename_to else set()
        retype_keys = set(self.retype_to) if self.retype_to else set()
        drop_keys = set(self.drop) if self.drop else set()
        if not (rename_keys or retype_keys or drop_keys):
            raise ValueError(
                "schema_drift requires at least one of rename_to / retype_to / drop"
            )
        overlaps: dict[str, set[str]] = {
            "rename_to & retype_to": rename_keys & retype_keys,
            "rename_to & drop": rename_keys & drop_keys,
            "retype_to & drop": retype_keys & drop_keys,
        }
        conflicts = {label: cols for label, cols in overlaps.items() if cols}
        if conflicts:
            raise ValueError(
                f"schema_drift column keys must be disjoint across"
                f" rename_to / retype_to / drop; overlaps: {conflicts}"
            )
        return self

    @model_validator(mode="after")
    def rename_targets_and_retype_types_valid(self) -> Self:
        """Every `rename_to` *target* is a plain SQL identifier and every
        `retype_to` type string is a recognized DuckDB type.

        Targets become column names in the written emit's catalog; type
        strings are spliced into a CAST — neither may be free-form. Keys are
        existing-column lookups and stay unrestricted (`ColumnsExist` checks
        them against the table).

        Raises:
            ValueError: A rename target does not match
                ^[A-Za-z_][A-Za-z0-9_]*$, or a retype type is not on the
                recognized DuckDB type allow-list.
        """
        if self.rename_to is not None:
            for key, value in self.rename_to.items():
                _require_sql_identifier(
                    value, f"schema_drift rename_to[{key!r}] target"
                )
        if self.retype_to is not None:
            for key, sql_type in self.retype_to.items():
                if not is_recognized_sql_type(sql_type):
                    raise ValueError(
                        f"schema_drift retype_to[{key!r}]: {sql_type!r} is not"
                        " a recognized DuckDB type (allowed: VARCHAR[(n)],"
                        " integer family, DOUBLE/FLOAT/REAL, BOOLEAN,"
                        " DECIMAL(p,s)/NUMERIC(p,s))"
                    )
        return self


class DangleReference(StrictBaseModel):
    """Referential breakage: rewrite sampled reference ids to non-existent ids."""

    kind: Literal["dangle_reference"]
    name: str | None = None
    """Author label; becomes each emitted defect's `rule`.
    Defaults to "dangle_reference#<index>"."""
    target: Target
    amount: Amount
    placement: Placement | None = None
    """Present ⇒ weighted draw; absent ⇒ the shipped uniform draw."""

    @model_validator(mode="after")
    def requires_columns(self) -> Self:
        """target.columns is present (the reference id columns to break)."""
        if self.target.columns is None:
            raise ValueError("dangle_reference requires target.columns")
        return self


class MispointReference(StrictBaseModel):
    """Referential mis-pointing: rewrite sampled reference ids to wrong-but-real target ids."""  # noqa: E501

    kind: Literal["mispoint_reference"]
    name: str | None = None
    """Author label; becomes each emitted defect's `rule`.
    Defaults to "mispoint_reference#<index>"."""
    target: Target
    amount: Amount
    placement: Placement | None = None
    """Present ⇒ weighted draw; absent ⇒ the shipped uniform draw."""
    constraint: Literal["created_after_reference"] | None = None
    """Absent ⇒ any donor distinct from the current id. Present ⇒ only donors
    created strictly after the reference's write anchor; flips the defect
    class to `point_in_time_dangling_reference`."""

    @model_validator(mode="after")
    def requires_columns(self) -> Self:
        """target.columns is present (the reference id columns to mis-point)."""
        if self.target.columns is None:
            raise ValueError("mispoint_reference requires target.columns")
        return self


class DropEvents(StrictBaseModel):
    """Remove sampled history events — lost CDC messages."""

    kind: Literal["drop_events"]
    name: str | None = None
    """Author label; becomes each emitted defect's `rule`.
    Defaults to "drop_events#<index>"."""
    target: Target
    amount: Amount
    """Pools over event rows."""
    placement: Placement | None = None
    """Present ⇒ weighted draw; absent ⇒ the uniform draw."""

    @model_validator(mode="after")
    def columns_forbidden(self) -> Self:
        """target.columns is absent — a drop removes whole events, never
        named columns."""
        if self.target.columns is not None:
            raise ValueError(
                "drop_events forbids target.columns (removes whole events,"
                " never named columns)"
            )
        return self


class FreezeSeries(StrictBaseModel):
    """Suppress each selected series' timeline tail so its value sticks."""

    kind: Literal["freeze_series"]
    name: str | None = None
    """Author label; becomes each emitted defect's `rule`.
    Defaults to "freeze_series#<index>"."""
    target: Target
    amount: Amount
    """Pools over the series universe (series with >= 2 timeline rows)."""
    placement: Placement | None = None
    """Present ⇒ weighted draw over series; a series takes its terminal row's
    weight. Absent ⇒ the uniform draw."""
    cut: Literal["after_first", "random"]
    """Where the freeze bites: 'after_first' keeps only the first timeline row;
    'random' draws a uniform kept-prefix length in [1, N-1] per selected
    series."""

    @model_validator(mode="after")
    def columns_forbidden(self) -> Self:
        """target.columns is absent — a freeze acts on whole series, never on
        named columns."""
        if self.target.columns is not None:
            raise ValueError(
                "freeze_series forbids target.columns (acts on whole series,"
                " never named columns)"
            )
        return self


class ShiftOffset(StrictBaseModel):
    """Additive clock-skew shift: sim_time += round(delta)."""

    kind: Literal["offset"]
    distribution: Distribution
    """The per-event additive delta shape, in ns; rounded round-half-to-even
    to BIGINT."""


class ShiftCollide(StrictBaseModel):
    """Snap the event onto its predecessor tick — a tick collision."""

    kind: Literal["collide"]


class ShiftSwap(StrictBaseModel):
    """Exchange the event's tick with its predecessor-tick partner's."""

    kind: Literal["swap"]


ShiftSpec = Annotated[
    ShiftOffset | ShiftCollide | ShiftSwap,
    Field(discriminator="kind"),
]
"""How a selected event's sim_time is rewritten."""


class ShiftSimTime(StrictBaseModel):
    """Rewrite sampled events' sim_time: skew, collide, or reorder."""

    kind: Literal["shift_sim_time"]
    name: str | None = None
    """Author label; becomes each emitted defect's `rule`.
    Defaults to "shift_sim_time#<index>"."""
    target: Target
    amount: Amount
    """Pools over event rows (collide/swap: rows with a predecessor tick)."""
    placement: Placement | None = None
    """Present ⇒ weighted draw; absent ⇒ the uniform draw."""
    shift: ShiftSpec

    @model_validator(mode="after")
    def columns_forbidden(self) -> Self:
        """target.columns is absent — a shift rewrites sim_time only, never
        named columns."""
        if self.target.columns is not None:
            raise ValueError(
                "shift_sim_time forbids target.columns (rewrites sim_time"
                " only, never named columns)"
            )
        return self


class DistortIntervals(StrictBaseModel):
    """Distort sampled membership intervals: overlap, gap, or inversion."""

    kind: Literal["distort_intervals"]
    name: str | None = None
    """Author label; becomes each emitted defect's `rule`.
    Defaults to "distort_intervals#<index>"."""
    target: Target
    amount: Amount
    """Pools over the mode's units: adjacent interval pairs (overlap) or
    closed interval rows (gap, left_before_join)."""
    placement: Placement | None = None
    """Present ⇒ weighted draw; absent ⇒ the uniform draw. A pair unit
    takes its earlier row's weight."""
    mode: Literal["overlap", "gap", "left_before_join"]
    """The distortion: extend an interval past its successor's join
    (overlap), end recorded presence early (gap), or swap the timing
    columns (left_before_join)."""

    @model_validator(mode="after")
    def columns_forbidden(self) -> Self:
        """target.columns is absent — the operation rewrites the two timing
        columns only, never author-named columns."""
        if self.target.columns is not None:
            raise ValueError(
                "distort_intervals forbids target.columns (rewrites the two"
                " timing columns only, never named columns)"
            )
        return self


class MutationSentinel(StrictBaseModel):
    """Replace the stored value with an author-specified sentinel literal."""

    kind: Literal["sentinel"]
    value: str | int | float | bool
    """The sentinel, rendered into each resolved column's current type by the
    shared DuckDB cast; an unrepresentable literal fails at apply time.
    Finite when float (parse time) -- NaN never compares equal, so it would
    make the no-mutation equality ill-defined."""

    @model_validator(mode="after")
    def value_finite(self) -> Self:
        """A float value is finite (NaN / inf rejected)."""
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("MutationSentinel.value must be finite when a float")
        return self


class MutationTypo(StrictBaseModel):
    """Exchange two adjacent characters (VARCHAR) or decimal digits (BIGINT) at a seeded position."""  # noqa: E501

    kind: Literal["typo"]


class MutationCase(StrictBaseModel):
    """Apply a case transform to the whole string."""

    kind: Literal["case"]
    form: Literal["upper", "lower", "title", "swap"]


class MutationWhitespace(StrictBaseModel):
    """Insert exactly one space at the chosen end of the string."""

    kind: Literal["whitespace"]
    where: Literal["leading", "trailing"]


class MutationTruncate(StrictBaseModel):
    """Keep the first max_length characters."""

    kind: Literal["truncate"]
    max_length: int = Field(ge=1)
    """Kept prefix length; >= 1."""


class MutationPrecisionDrop(StrictBaseModel):
    """Round to a fixed number of decimal places (round-half-to-even)."""

    kind: Literal["precision_drop"]
    digits: int = Field(ge=0)
    """Decimal places kept; >= 0 (0 is the double->int teaching case)."""


class MutationScale(StrictBaseModel):
    """Multiply by a constant factor (magnitude shift)."""

    kind: Literal["scale"]
    factor: float
    """Finite, not 0 and not 1; BIGINT products store round-half-to-even."""

    @model_validator(mode="after")
    def factor_finite_and_nontrivial(self) -> Self:
        """factor is finite and not in {0, 1}."""
        if not math.isfinite(self.factor):
            raise ValueError("MutationScale.factor must be finite")
        if self.factor in (0, 1):
            raise ValueError("MutationScale.factor must not be 0 or 1")
        return self


class MutationMojibake(StrictBaseModel):
    """Re-decode the value's UTF-8 bytes as latin-1."""

    kind: Literal["mojibake"]


class MutationFormatDirt(StrictBaseModel):
    """Insert comma thousands separators into an all-digit string."""

    kind: Literal["format_dirt"]


class MutationResample(StrictBaseModel):
    """Replace with another real value drawn uniformly from the same column."""

    kind: Literal["resample"]


class MutationOutOfDomain(StrictBaseModel):
    """Mutate an enum-domained category so it leaves the declared domain."""

    kind: Literal["out_of_domain"]


MutationSpec = Annotated[
    MutationSentinel
    | MutationTypo
    | MutationCase
    | MutationWhitespace
    | MutationTruncate
    | MutationPrecisionDrop
    | MutationScale
    | MutationMojibake
    | MutationFormatDirt
    | MutationResample
    | MutationOutOfDomain,
    Field(discriminator="kind"),
]
"""How a selected cell's stored value is rewritten."""


class DuplicateRows(StrictBaseModel):
    """Exact, near-duplicate, or conflicting-duplicate row injection."""

    kind: Literal["duplicate_rows"]
    name: str | None = None
    """Author label; becomes each emitted defect's `rule`.
    Defaults to "duplicate_rows#<index>"."""
    target: Target
    amount: Amount
    jitter: Distribution | None = None
    """Present ⇒ near-duplicate (perturb target.columns numerically)."""
    mutation: MutationSpec | None = None
    """Present ⇒ conflicting duplicate (transform target.columns cells of
    each copy through one MutationSpec kind)."""
    placement: Placement | None = None
    """Present ⇒ weighted draw; absent ⇒ the shipped uniform draw."""

    @model_validator(mode="after")
    def perturbation_governs_columns(self) -> Self:
        """At most one of jitter / mutation is set (the mode is exact when
        neither is). target.columns is required when a perturbation mode is
        set (it names the perturb targets) and forbidden when neither is (an
        exact copy touches no specific column)."""
        if self.jitter is not None and self.mutation is not None:
            raise ValueError(
                "duplicate_rows must not set both jitter and mutation"
                " (at most one perturbation mode)"
            )
        perturbing = self.jitter is not None or self.mutation is not None
        if perturbing and self.target.columns is None:
            raise ValueError(
                "duplicate_rows with jitter or mutation requires target.columns"
            )
        if not perturbing and self.target.columns is not None:
            raise ValueError(
                "duplicate_rows without jitter or mutation forbids"
                " target.columns (exact duplicate touches no specific column)"
            )
        return self


class MutateCells(StrictBaseModel):
    """Wrong-value injection: apply one type-preserving mutation to a sampled set of value cells."""  # noqa: E501

    kind: Literal["mutate_cells"]
    name: str | None = None
    """Author label; becomes each emitted defect's `rule`.
    Defaults to "mutate_cells#<index>"."""
    target: Target
    amount: Amount
    placement: Placement | None = None
    """Present ⇒ weighted draw; absent ⇒ the shipped uniform draw."""
    mutation: MutationSpec

    @model_validator(mode="after")
    def requires_columns(self) -> Self:
        """target.columns is present (the cells to mutate)."""
        if self.target.columns is None:
            raise ValueError("mutate_cells requires target.columns")
        return self


CorruptOperation = Annotated[
    NullCells
    | DuplicateRows
    | DeleteRows
    | InsertRows
    | SchemaDrift
    | DangleReference
    | MispointReference
    | DropEvents
    | FreezeSeries
    | ShiftSimTime
    | MutateCells
    | DistortIntervals,
    Field(discriminator="kind"),
]
"""The discriminated union of corrupter operations, keyed on `kind`."""


class CorruptConfig(StrictBaseModel):
    """Top-level corrupter configuration: a master seed and an ordered operation list."""  # noqa: E501

    seed: int
    """Master seed; each operation derives a stable RNG sub-stream from
    (seed, index)."""
    operations: list[CorruptOperation]
    """The corrupter operations, applied in list order over a shared working set."""

    @model_validator(mode="after")
    def operations_non_empty(self) -> Self:
        """operations is non-empty (an empty corrupter config is an authoring error)."""
        if not self.operations:
            raise ValueError("CorruptConfig.operations must not be empty")
        return self
