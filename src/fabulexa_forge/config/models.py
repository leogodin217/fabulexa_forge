"""Pydantic models for the dimensional export configuration.

All models use `extra='forbid'` to surface unknown fields at parse time.
Each model enforces its own structural constraints via `@model_validator`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    model_validator,
)
from typing_extensions import Self

from fabulexa_forge._sql import is_recognized_sql_type, validate_date_parse_format
from fabulexa_forge.anchor import TemporalRender

# ---------------------------------------------------------------------------
# Identifier validation (author-supplied names spliced into SQL / filenames)
# ---------------------------------------------------------------------------

_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Kafka-convention topic name: also safe as a filename stem (no separator,
# no traversal), so a `groups` target can never escape the jsonl output dir.
_TOPIC_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# ---------------------------------------------------------------------------
# Key election
# ---------------------------------------------------------------------------

KeySurface = Literal["record_id", "record_index", "presentation_id"]
"""The three identity surfaces a population or FK edge may elect."""


def _check_keys_well_formed(
    keys: "dict[str, KeySurface | dict[str, KeySurface]] | None",
) -> None:
    """Shared `keys` block shape check for ExportConfig and StreamConfig.

    `keys` (when present) is non-empty; every per-kind map is non-empty.
    Emit-dependent checks (kind/sub-type existence, registry declaration,
    union safety) are deliberately not here — the config is emit-independent.

    Args:
        keys: The config `keys` block, verbatim.

    Raises:
        ValueError: `keys` is an empty map, or a per-kind map value is empty.
    """
    if keys is None:
        return
    if not keys:
        raise ValueError("keys: must not be empty (omit the field instead)")
    for kind, election in keys.items():
        if isinstance(election, dict) and not election:
            raise ValueError(f"keys.{kind}: per-sub-type map must not be empty")


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
# Temporal rendering elections: shared vocabulary + validators
# ---------------------------------------------------------------------------


def _require_render_map_valid(
    value: "Mapping[str, object] | None", field_name: str
) -> None:
    """A `render` map: when present, non-empty, with non-empty keys.

    Value-type-agnostic (`Mapping[str, object]`): shared by every `render`
    field, whether its narrow structural-shorthand-only spelling (the events
    block's own map) or the unified `RenderElection` spelling — the shape
    check is the same regardless of the value form.

    Args:
        value: The field's value, or None when absent.
        field_name: The field's dotted name, for the error message.

    Raises:
        ValueError: `value` is an empty dict, or contains an empty key.
    """
    if value is None:
        return
    if not value:
        raise ValueError(f"{field_name} must be non-empty when present")
    for key in value:
        if not key:
            raise ValueError(f"{field_name} keys must be non-empty")


# ---------------------------------------------------------------------------
# Kafka connection config (streaming sink)
# ---------------------------------------------------------------------------


class StrictBaseModel(BaseModel):
    """Base model rejecting unknown fields (extra='forbid')."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# Value rendering elections: the unified `render:` map's value forms
# ---------------------------------------------------------------------------


def _check_decimal_bounds(precision: int, scale: int, field_name: str) -> None:
    """1 <= precision <= 38; 0 <= scale <= precision — shared by both decimal
    spellings (the `render` map's `DecimalElection` and dimensional's
    `derived: decimal` `DecimalSpec`).

    Args:
        precision: The declared precision.
        scale: The declared scale.
        field_name: The field's dotted name, for the error message.

    Raises:
        ValueError: `precision` outside 1..38, or `scale` outside 0..precision.
    """
    if not (1 <= precision <= 38):
        raise ValueError(f"{field_name}: precision must be 1..38 (got {precision})")
    if not (0 <= scale <= precision):
        raise ValueError(
            f"{field_name}: scale must be 0..precision"
            f" (got scale={scale}, precision={precision})"
        )


def _check_json_precision_shape(leaves: "Mapping[str, int]", field_name: str) -> None:
    """Leaf map non-empty; keys non-empty; 0 <= digits <= 12 — shared by both
    json_precision spellings (the `render` map's `JsonPrecisionElection` and
    dimensional's `derived: json_precision` `JsonPrecisionSpec`).

    Args:
        leaves: The declared top-level key -> fraction-digits map.
        field_name: The field's dotted name, for the error message.

    Raises:
        ValueError: `leaves` is empty, has an empty key, or a digits value
            outside 0..12.
    """
    if not leaves:
        raise ValueError(f"{field_name}: leaf map must not be empty")
    for key, digits in leaves.items():
        if not key:
            raise ValueError(f"{field_name}: keys must be non-empty")
        if not (0 <= digits <= 12):
            raise ValueError(
                f"{field_name}: digits for key {key!r} must be 0..12 (got {digits})"
            )


class DecimalElection(StrictBaseModel):
    """Numeric precision rendering: DOUBLE source -> DECIMAL(p, s)."""

    decimal: tuple[int, int]
    """(precision, scale); 1 <= precision <= 38, 0 <= scale <= precision."""

    @model_validator(mode="after")
    def decimal_bounds(self) -> Self:
        """1 <= precision <= 38; 0 <= scale <= precision.

        Raises:
            ValueError: `precision` outside 1..38, or `scale` outside
                0..precision.
        """
        precision, scale = self.decimal
        _check_decimal_bounds(precision, scale, "decimal")
        return self


class InstantElection(StrictBaseModel):
    """Payload sim-instant declaration: BIGINT ns source, rendered via the anchor through the shared instant-election vocabulary."""  # noqa: E501

    instant: TemporalRender
    """Which instant rendering the declared ns offset receives."""


class JsonPrecisionElection(StrictBaseModel):
    """In-place rounding of named top-level numeric leaves of a JSON payload."""

    json_precision: dict[str, int]
    """Top-level key -> fraction digits (0..12); non-empty."""

    @model_validator(mode="after")
    def json_precision_shape(self) -> Self:
        """Leaf map non-empty; keys non-empty; 0 <= digits <= 12.

        Raises:
            ValueError: `json_precision` is empty, has an empty key, or a
                digits value outside 0..12.
        """
        _check_json_precision_shape(self.json_precision, "json_precision")
        return self


class DateParseElection(StrictBaseModel):
    """The declared parse, relocated into the unified render map; format semantics unchanged (validated by validate_date_parse_format)."""  # noqa: E501

    date_parse: str
    """strptime-style format; shared format rules."""

    @model_validator(mode="after")
    def format_valid(self) -> Self:
        """`date_parse` denotes a complete date, a complete time, or both.

        Raises:
            ValueError: The format is empty, uses a directive outside the
                closed set, violates a pairing rule, duplicates a temporal
                field, or is neither date-complete nor time-complete (see
                `validate_date_parse_format`).
        """
        validate_date_parse_format(self.date_parse, "render.date_parse")
        return self


RenderElection = (
    TemporalRender
    | DateParseElection
    | InstantElection
    | DecimalElection
    | JsonPrecisionElection
)
"""A render-map value: a bare temporal-election literal (structural instant
shorthand) or one typed election object. Source identity -> RenderElection."""


# ---------------------------------------------------------------------------
# Predicate values (dimensional row predicates: scalar or non-empty list)
# ---------------------------------------------------------------------------


def _reject_malformed_predicate(value: str | list[str]) -> str | list[str]:
    """Reject the two malformed list shapes; say nothing about the scalar form.

    Args:
        value: A parsed predicate value.

    Returns:
        The value unchanged.

    Raises:
        ValueError: the value is an empty list, or a list containing a repeated
            element (the message names the repeated element).
    """
    if isinstance(value, str):
        return value
    if not value:
        raise ValueError(
            "predicate value must not be an empty list"
            " (an empty predicate selects nothing; omit the entry or the table)"
        )
    seen: set[str] = set()
    duplicates: list[str] = []
    for element in value:
        if element in seen:
            duplicates.append(element)
        seen.add(element)
    if duplicates:
        raise ValueError(
            f"predicate value list contains duplicate element(s): {duplicates}"
        )
    return value


PredicateValue: TypeAlias = Annotated[
    str | list[str], AfterValidator(_reject_malformed_predicate)
]
"""One predicate's required value: a scalar (compiles to `=`) or a non-empty,
duplicate-free list of alternatives (compiles to `IN`).

The well-formedness rule rides the type, not the models. Every field declared
`PredicateValue` carries it — including as the value type of a
`dict[str, PredicateValue]`, where it applies per entry — so the three
predicate-bearing models need no shared validator, the failure is reported at the
offending field's path rather than at model level, and a future predicate field
on any mode's config inherits the rule without wiring."""


class FkClause(StrictBaseModel):
    """A dimension foreign key resolved by a labeled-edge pathfind."""

    to: str
    """The kind whose dimension row this FK resolves to."""
    via: Literal["reference", "membership"]
    """Which edge to pathfind along — a declared reference, or a membership interval."""
    where: dict[str, PredicateValue] | None = None
    """Membership filter predicates matched against membership-table element columns."""
    member_field: str | None = None
    """Membership-table column holding the member identity to resolve."""
    property: str | None = None
    """The membership property name to join against."""
    path: list[str] | None = None
    """Reference-edge hop chain from the grain kind to the target kind."""
    target_key: KeySurface | None = None
    """Which identity surface to write into the FK column. Absent: inherit
    the destination dim's source population set's election (record_id when
    it carries none). Present: per-edge override, gated identically over the
    same population set."""
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
    """A sim_time source column rendered as an elected wallclock type via the anchor."""

    source: str
    """The base-layer sim_time column to convert."""
    as_: TemporalRender | None = Field(None, alias="as")
    """The instant rendering election. Absent (`None`) means the
    mode-definitional default `timestamp` rendering — absence detection, not
    an invented value. Any set value, `timestamp` included, is an explicit
    election and makes the column anchor-required (business rule)."""


class ScdWindowSpec(StrictBaseModel):
    """An SCD-2 validity bound with an instant-rendering election."""

    bound: Literal["valid_from", "valid_to"]
    """Which validity bound this column carries."""
    as_: TemporalRender = Field(alias="as")
    """The instant rendering election — required. The object form exists to
    elect (a bound-only object would duplicate the bare-literal shorthand),
    so every object form is an explicit election with the same anchor
    semantics as an explicit TimestampSpec election."""


def scd_window_bound(
    scd_window: "Literal['valid_from', 'valid_to'] | ScdWindowSpec | None",
) -> "Literal['valid_from', 'valid_to'] | None":
    """The validity bound of a `DerivedSpec.scd_window` field value.

    The bare-literal shorthand and the object form carry the bound
    differently; every reader of the field's bound goes through this one
    function rather than re-deriving it.

    Args:
        scd_window: A `DerivedSpec.scd_window` field value.

    Returns:
        The bound, or None when `scd_window` is None.
    """
    if scd_window is None:
        return None
    if isinstance(scd_window, ScdWindowSpec):
        return scd_window.bound
    return scd_window


def scd_window_render(
    scd_window: "Literal['valid_from', 'valid_to'] | ScdWindowSpec",
) -> TemporalRender:
    """The instant-rendering election of a set `DerivedSpec.scd_window` value.

    The bare-literal shorthand carries no election — the mode-definitional
    default `timestamp` rendering (absence detection, not an invented value).

    Args:
        scd_window: A set (non-None) `DerivedSpec.scd_window` field value.

    Returns:
        The object form's `as_`, or `timestamp` for the bare literal.
    """
    if isinstance(scd_window, ScdWindowSpec):
        return scd_window.as_
    return "timestamp"


def timestamp_render(spec: TimestampSpec) -> TemporalRender:
    """The instant-rendering election of a `TimestampSpec`.

    Absence (`as_ is None`) means the mode-definitional default `timestamp`
    rendering — absence detection, not an invented value.

    Args:
        spec: A `TimestampSpec` field value.

    Returns:
        `spec.as_`, or `timestamp` when unset.
    """
    return spec.as_ if spec.as_ is not None else "timestamp"


class DateParseSpec(StrictBaseModel):
    """A declared reinterpretation of a VARCHAR source column as its format-denoted temporal type (DATE, TIME, or naive TIMESTAMP)."""  # noqa: E501

    from_: str = Field(alias="from")
    """The VARCHAR source column holding temporal strings (sidecar-validated)."""
    format: str
    """The author-declared parse format (closed strptime-directive set; see
    validate_date_parse_format). Must denote a complete date, a complete
    time, or both; validated at load time, never defaulted. The format is
    the election — the denoted type is derived from it, never declared
    separately."""

    @model_validator(mode="after")
    def format_denotes_a_temporal(self) -> Self:
        """`from_` is non-empty; `format` denotes a complete temporal value.

        Raises:
            ValueError: `from_` is empty, or `format` is empty, uses a
                directive outside the closed set, violates a pairing rule
                (%I⇔%p, %M needs an hour, %S needs %M, %f/%g need %S),
                duplicates a temporal field (a repeated directive, or two
                alternative forms of one field), or is neither
                date-complete nor time-complete.
        """
        _require_nonempty_str(self.from_, "date_parse.from")
        validate_date_parse_format(self.format, "date_parse.format")
        return self


class DecimalSpec(StrictBaseModel):
    """Dimensional derived spelling of the decimal election."""

    from_: str = Field(alias="from")
    """The grain-surface source column (DOUBLE payload column)."""
    as_: tuple[int, int] = Field(alias="as")
    """(precision, scale), same bounds as DecimalElection."""

    @model_validator(mode="after")
    def decimal_bounds(self) -> Self:
        """1 <= precision <= 38; 0 <= scale <= precision.

        Raises:
            ValueError: `precision` outside 1..38, or `scale` outside
                0..precision.
        """
        precision, scale = self.as_
        _check_decimal_bounds(precision, scale, "derived.decimal")
        return self


class JsonPrecisionSpec(StrictBaseModel):
    """Dimensional derived spelling of the json_precision election."""

    from_: str = Field(alias="from")
    """The grain-surface source column (VARCHAR JSON payload)."""
    leaves: dict[str, int]
    """Top-level key -> fraction digits (0..12); non-empty."""

    @model_validator(mode="after")
    def json_precision_shape(self) -> Self:
        """Leaf map non-empty; keys non-empty; 0 <= digits <= 12.

        Raises:
            ValueError: `leaves` is empty, has an empty key, or a digits
                value outside 0..12.
        """
        _check_json_precision_shape(self.leaves, "derived.json_precision")
        return self


class ElapsedSpec(StrictBaseModel):
    """A cross-row elapsed time-delta between two correlated events."""

    correlate_on: str
    """The output column that links this row to its counterpart event row."""
    other_where: dict[str, PredicateValue]
    """Predicate identifying the counterpart event row(s); the earliest
    matching interval start per correlation key is the one correlated."""
    start_source: str
    """The sim_time column on the counterpart row marking the interval start."""
    end_source: str
    """The sim_time column on this row marking the interval end."""
    unit: Literal["minutes", "seconds", "hours"] | None = None
    """Numeric rendering: the delta divided to this unit (DOUBLE). Exclusive
    with `as_`; exactly one of the two is required (exactly_one_rendering)."""
    as_: Literal["interval"] | None = Field(None, alias="as")
    """Typed rendering: the delta as an INTERVAL. Exclusive with `unit`."""

    @model_validator(mode="after")
    def other_where_non_empty(self) -> Self:
        """`other_where` names at least one predicate entry.

        The grammar's one required predicate mapping: an empty mapping renders
        no condition at all — a degenerate correlation the elapsed subquery
        cannot express. (The optional mappings have a meaning when empty —
        select all rows — that `other_where` does not.)

        Raises:
            ValueError: `other_where` is empty.
        """
        if not self.other_where:
            raise ValueError(
                "elapsed.other_where must name at least one predicate entry"
                " (an empty mapping renders no condition at all)"
            )
        return self

    @model_validator(mode="after")
    def exactly_one_rendering(self) -> Self:
        """Exactly one of `unit` / `as_` is set.

        Omitting both is an error (no default rendering is invented);
        setting both is an error (the elections contradict).

        Raises:
            ValueError: Neither or both of `unit` / `as_` is set.
        """
        if (self.unit is None) == (self.as_ is None):
            raise ValueError(
                "elapsed must set exactly one of 'unit' / 'as'"
                f" (got unit={self.unit!r}, as={self.as_!r})"
            )
        return self


class DerivedSpec(StrictBaseModel):
    """A computed column; exactly one of the eight derivation kinds is set."""

    ordinal: OrdinalSpec | None = None
    """Assigns a ROW_NUMBER within a partition ordered by a named column."""
    value_map: ValueMapSpec | None = None
    """Substitutes source values via a lookup table; unmapped values become NULL."""
    timestamp: TimestampSpec | None = None
    """Converts a sim_time source column to an elected wallclock type via the anchor."""
    scd_window: Literal["valid_from", "valid_to"] | ScdWindowSpec | None = None
    """Bare literal (shorthand, default rendering) or the object form
    carrying an instant-rendering election."""
    elapsed: ElapsedSpec | None = None
    """Computes a cross-row time delta between two correlated events."""
    date_parse: DateParseSpec | None = None
    """Declared VARCHAR->DATE reinterpretation of a source column."""
    decimal: DecimalSpec | None = None
    """Numeric precision rendering of a DOUBLE grain-surface column."""
    json_precision: JsonPrecisionSpec | None = None
    """In-place leaf rounding of a VARCHAR JSON grain-surface column."""

    @model_validator(mode="after")
    def exactly_one_derived(self) -> Self:
        """A DerivedSpec sets exactly one derived kind.

        Exactly one of ordinal/value_map/timestamp/scd_window/elapsed/
        date_parse/decimal/json_precision must be set.
        """
        set_fields = [
            f
            for f, v in [
                ("ordinal", self.ordinal),
                ("value_map", self.value_map),
                ("timestamp", self.timestamp),
                ("scd_window", self.scd_window),
                ("elapsed", self.elapsed),
                ("date_parse", self.date_parse),
                ("decimal", self.decimal),
                ("json_precision", self.json_precision),
            ]
            if v is not None
        ]
        if len(set_fields) != 1:
            raise ValueError(
                "DerivedSpec must set exactly one of"
                " ordinal/value_map/timestamp/scd_window/elapsed/date_parse/"
                "decimal/json_precision; "
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
    value: PredicateValue | None = None
    """For history_point grain, the property value(s) to filter on."""
    where: dict[str, PredicateValue] | None = None
    """Membership-only row predicate matched against membership element columns."""
    filter: dict[str, PredicateValue] | None = None
    """Records-only row predicate matched against the kind's records columns."""

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

    @model_validator(mode="after")
    def membership_grain_fk_where_refused(self) -> Self:
        """A membership-grain table's plain via:membership fk may not set
        `where` — the grain row already IS the binding, so there is no
        separate membership relation for the predicate to narrow. Narrowing
        which binding rows the table holds belongs in `source.where`. The
        point-in-time form (`as_of`) correlates its own membership subquery
        and keeps `where`.

        Raises:
            ValueError: source.grain == 'membership' and a column's fk sets
                via='membership' and `where` without `as_of`.
        """
        if self.source.grain != "membership":
            return self
        for col in self.columns:
            fk = col.fk
            if (
                fk is not None
                and fk.via == "membership"
                and fk.as_of is None
                and fk.where is not None
            ):
                raise ValueError(
                    f"table '{self.name}' column '{col.name}': fk.where has no"
                    " meaning on a membership-grain table — the grain row is"
                    " already the binding, so there is no membership relation"
                    " to narrow. Narrow this table's rows with source.where"
                    " instead (the point-in-time membership fk, as_of, still"
                    " accepts where)."
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


# ---------------------------------------------------------------------------
# Source declared-table grammar: population-address decl models
# ---------------------------------------------------------------------------


def _require_nonempty_str(value: str, field_name: str) -> None:
    """Reject an empty author-supplied label field.

    Args:
        value: The field's value.
        field_name: The field's dotted name, for the error message.

    Raises:
        ValueError: `value` is the empty string.
    """
    if not value:
        raise ValueError(f"{field_name} must be non-empty")


def _require_distinct_nonempty_tuple(
    value: tuple[str, ...] | None, field_name: str
) -> None:
    """A tuple field: when present, non-empty, with distinct entries.

    Shared by every declared-table / events-source list-valued field
    (`sub_types`, `columns`, `only`, `ignore`) — the parse-time rule applies
    identically to all of them.

    Args:
        value: The field's value, or None when absent.
        field_name: The field's dotted name, for the error message.

    Raises:
        ValueError: `value` is an empty tuple, or contains a duplicate entry.
    """
    if value is None:
        return
    if not value:
        raise ValueError(f"{field_name} must be non-empty when present")
    seen: set[str] = set()
    duplicates: list[str] = []
    for entry in value:
        if entry in seen:
            duplicates.append(entry)
        seen.add(entry)
    if duplicates:
        raise ValueError(f"{field_name} entries must be distinct: {duplicates}")


def _require_rename_map_valid(value: dict[str, str] | None, field_name: str) -> None:
    """A rename map: when present, non-empty, with distinct output names.

    Keys (source column names) are already distinct by dict construction;
    this additionally enforces that two source columns may not rename to
    the same output name.

    Args:
        value: The field's value, or None when absent.
        field_name: The field's dotted name, for the error message.

    Raises:
        ValueError: `value` is an empty dict, or two keys share a value.
    """
    if value is None:
        return
    if not value:
        raise ValueError(f"{field_name} must be non-empty when present")
    seen: set[str] = set()
    duplicates: list[str] = []
    for target in value.values():
        if target in seen:
            duplicates.append(target)
        seen.add(target)
    if duplicates:
        raise ValueError(f"{field_name} values must be distinct: {duplicates}")


def _require_where_map_valid(
    value: dict[str, PredicateValue] | None, field_name: str
) -> None:
    """A `where` mapping: when present, non-empty, with non-empty keys.

    Per-entry value emptiness / duplication rides the `PredicateValue` type
    itself (`_reject_malformed_predicate`) and is not re-checked here.

    Args:
        value: The field's value, or None when absent.
        field_name: The field's dotted name, for the error message.

    Raises:
        ValueError: `value` is an empty dict, or contains an empty key.
    """
    if value is None:
        return
    if not value:
        raise ValueError(f"{field_name} must be non-empty when present")
    for key in value:
        if not key:
            raise ValueError(f"{field_name} keys must be non-empty")


def _require_dict_entries_nonempty(
    value: dict[str, str] | None, field_name: str
) -> None:
    """A dict field: when present, every key and value is non-empty.

    Shared by every dict-valued field whose grammar refuses empty keys or
    values on top of `_require_rename_map_valid`'s present-but-empty /
    distinct-values checks (`SourceEventSourceDecl.rename`,
    `SourceConfig.kind_labels`).

    Args:
        value: The field's value, or None when absent.
        field_name: The field's dotted name, for the error message.

    Raises:
        ValueError: A key or value in `value` is the empty string.
    """
    if value is None:
        return
    for key, target in value.items():
        if not key:
            raise ValueError(f"{field_name} keys must be non-empty")
        if not target:
            raise ValueError(f"{field_name} values must be non-empty")


def _require_exactly_one_population_source(
    kind: str | None, membership: "MembershipRef | None", label: str
) -> None:
    """Exactly one of `kind` / `membership` addresses the declaration's population.

    Args:
        kind: The declaration's `kind` field.
        membership: The declaration's `membership` field.
        label: The declaring unit's message label.

    Raises:
        ValueError: Both or neither of `kind` / `membership` is set.
    """
    if (kind is None) == (membership is None):
        raise ValueError(f"{label} must set exactly one of 'kind' / 'membership'")


class MembershipRef(StrictBaseModel):
    """Addresses one membership table by its contract identity."""

    kind: str
    """The owning kind `K` of the sidecar `membership__<K>__<property>` table."""
    property: str
    """The membership property `p` of the sidecar `membership__<K>__<p>` table."""


class SourceTableDecl(StrictBaseModel):
    """One declared output table: a name, one population source, optional column selection, renames, and row selection."""  # noqa: E501

    name: str
    """Author-verbatim output table name."""
    kind: str | None = None
    """A records kind, exclusive with `membership` (`table_shape`)."""
    sub_types: tuple[str, ...] | None = None
    """Explicit population subset (with `kind`) or owner sub-type subset
    (with `membership` — the junction renders intervals of owners in these
    sub-types, resolved through the parent lookup). Absent = every declared
    sub-type."""
    membership: MembershipRef | None = None
    """A membership-table address, exclusive with `kind`."""
    columns: tuple[str, ...] | None = None
    """Source-column selection; absent = full classified projection."""
    rename: dict[str, str] | None = None
    """Source column name -> output name overrides."""
    where: dict[str, PredicateValue] | None = None
    """Row predicate; entries AND-joined. Keys name `constant`-class payload
    properties of the subject kind (gated at plan time): source column names
    (`prop__<p>`) with `kind`, bare owner-property names with `membership`.
    Absent = every row of the selected populations."""
    render: "dict[str, RenderElection] | None" = None
    """Per-column rendering election, keyed by source identity (e.g.
    `created_sim_time`, `prop__error_rate`). A bare temporal literal elects a
    structural instant column (shorthand); a typed election object elects a
    payload column (`prop__<p>` on `state`, `elem__<f>` on `junction`). One
    column, one election; keys and shape validated at plan time. Absent =
    default rendering."""

    @model_validator(mode="after")
    def table_shape(self) -> Self:
        """The declaration's structural shape (design doc § Config Models).

        Raises:
            ValueError: `name` is empty; not exactly one of `kind` /
                `membership` is set; `sub_types` / `columns` is
                present-but-empty or carries a duplicate entry; `rename` is
                present-but-empty or two keys share a target value; `where` is
                present-but-empty or has an empty key; `render` is
                present-but-empty or has an empty key. (Value emptiness /
                duplication is carried by `PredicateValue` per entry; each
                `render` entry's own shape is carried by its `RenderElection`
                model.)
        """
        _require_nonempty_str(self.name, "SourceTableDecl.name")
        label = f"table {self.name!r}"
        _require_exactly_one_population_source(self.kind, self.membership, label)
        _require_distinct_nonempty_tuple(self.sub_types, "SourceTableDecl.sub_types")
        _require_distinct_nonempty_tuple(self.columns, "SourceTableDecl.columns")
        _require_rename_map_valid(self.rename, "SourceTableDecl.rename")
        _require_where_map_valid(self.where, "SourceTableDecl.where")
        _require_render_map_valid(self.render, "SourceTableDecl.render")
        return self


class SourceEventSourceDecl(StrictBaseModel):
    """One audited population set for the event log."""

    kind: str | None = None
    """A records kind, exclusive with `membership` (`source_shape`)."""
    sub_types: tuple[str, ...] | None = None
    """Explicit population subset (with `kind`) or owner sub-type subset
    (with `membership` — the source's join/leave stream narrows to owners
    in these sub-types, resolved through the parent lookup). Absent =
    every declared sub-type."""
    membership: MembershipRef | None = None
    """A membership-table address, exclusive with `kind`."""
    only: tuple[str, ...] | None = None
    """Audited-property subset by bare name (element-field name for a
    membership source); mutually exclusive with `ignore`."""
    ignore: tuple[str, ...] | None = None
    """Audited-property exclusion by bare name; mutually exclusive with
    `only`."""
    item_type: str | None = None
    """This source's resolved item-type, verbatim, overriding the
    kind-label / contract-identity default. Optional; non-empty when
    present."""
    rename: dict[str, str] | None = None
    """Audited property (element field) bare name -> `changes` output key.
    Keys are source identities, never output keys, so a default-key
    collision is always resolvable. A membership reference field's entry
    renames its expanded `<f>_kind` / `<f>_id` pair in place."""
    where: dict[str, PredicateValue] | None = None
    """Record predicate over the subject kind (the declared kind, or the
    owner kind for a membership source), keyed by bare property name;
    entries AND-joined; keys must name `constant`-class payload properties
    (gated at plan time). Selects which records' (owners') events feed this
    source's audit stream — orthogonal to `only` / `ignore`, which select
    the audited property set."""

    @model_validator(mode="after")
    def source_shape(self) -> Self:
        """The declaration's structural shape (design doc § Config Models).

        Raises:
            ValueError: Not exactly one of `kind` / `membership` is set;
                `sub_types` / `only` / `ignore` is present-but-empty or
                carries a duplicate entry; both `only` and `ignore` are set;
                `item_type` is empty; `rename` is present-but-empty, has an
                empty key or value, or two keys share a target value;
                `where` is present-but-empty or has an empty key.
        """
        label = "events source"
        _require_exactly_one_population_source(self.kind, self.membership, label)
        _require_distinct_nonempty_tuple(
            self.sub_types, "SourceEventSourceDecl.sub_types"
        )
        _require_distinct_nonempty_tuple(self.only, "SourceEventSourceDecl.only")
        _require_distinct_nonempty_tuple(self.ignore, "SourceEventSourceDecl.ignore")
        if self.only is not None and self.ignore is not None:
            raise ValueError(f"{label}: 'only' and 'ignore' are mutually exclusive")
        if self.item_type is not None:
            _require_nonempty_str(self.item_type, "SourceEventSourceDecl.item_type")
        _require_rename_map_valid(self.rename, "SourceEventSourceDecl.rename")
        _require_dict_entries_nonempty(self.rename, "SourceEventSourceDecl.rename")
        _require_where_map_valid(self.where, "SourceEventSourceDecl.where")
        return self


class SourceEventsDecl(StrictBaseModel):
    """The single polymorphic event log declaration."""

    name: str
    """Author-verbatim output table name for the log."""
    sources: tuple[SourceEventSourceDecl, ...]
    """Audited populations, >= 1 entry, pairwise-disjoint (gated at plan
    time — `SourceEventSourceOverlap`)."""
    render: dict[str, TemporalRender] | None = None
    """Rendering election for the log's instant column, keyed by source
    identity (`event_sim_time`, the log's one legal key)."""

    @model_validator(mode="after")
    def events_shape(self) -> Self:
        """The declaration's structural shape (design doc § Config Models).

        Raises:
            ValueError: `name` is empty, `sources` is empty, or `render` is
                present-but-empty or has an empty key.
        """
        _require_nonempty_str(self.name, "SourceEventsDecl.name")
        if not self.sources:
            raise ValueError("SourceEventsDecl.sources must be non-empty (>= 1 entry)")
        _require_render_map_valid(self.render, "SourceEventsDecl.render")
        return self


class SourceConfig(StrictBaseModel):
    """mode: source section — the declared app-database shape."""

    tables: tuple[SourceTableDecl, ...] = ()
    """The declared output tables: `state` (kind) or `junction` (membership)
    per entry, declaration order. Defaults empty (a log-only config is
    legal); at least one of `tables` / `events` must be declared."""
    events: SourceEventsDecl | None = None
    """The single polymorphic event log declaration; absent = no history
    exported (a Type-1-only app)."""
    declare_keys: bool = False
    """Emit declared key constraints (PK/UNIQUE) for the DuckDB writer,
    resolved from the sidecar's presentation_keys registry. Off by default —
    the design doc's own contract default, not an invented value. Ignored
    under CSV: a keys-not-declarable-csv notice is emitted instead."""
    kind_labels: dict[str, str] | None = None
    """Engine kind name -> domain label, applied wherever a kind name
    renders as a value: item-type defaults, `<f>_kind` entries inside
    `changes`, and junction `member__<f>__kind` values. Never applied to
    identity values, table names, or sub-type discriminator values. Absent =
    verbatim kind names."""

    @model_validator(mode="after")
    def kind_labels_shape(self) -> Self:
        """`kind_labels`, when present: non-empty, non-empty keys and
        values, distinct values.

        Raises:
            ValueError: The map is empty, a key or value is the empty
                string, or two kinds map to one label.
        """
        _require_rename_map_valid(self.kind_labels, "SourceConfig.kind_labels")
        _require_dict_entries_nonempty(self.kind_labels, "SourceConfig.kind_labels")
        return self

    @model_validator(mode="after")
    def source_section_required(self) -> Self:
        """A `source` section declares at least one output: >= 1 entry in
        `tables`, or an `events` block.

        Raises:
            ValueError: `tables` is empty and `events` is None — there is no
                implicit layout to fall back to (`source: {}` is refused).
        """
        if not self.tables and self.events is None:
            raise ValueError(
                "source section must declare at least one output: >= 1 entry"
                " in 'tables', or an 'events' block"
            )
        return self

    @model_validator(mode="after")
    def table_source_exclusive(self) -> Self:
        """Cross-declaration checks the per-declaration validators cannot
        see: `tables[].name` is distinct across the declaration list. Every
        other rule the design doc's `table_source_exclusive` docstring
        describes — exactly one of `kind` / `membership`, non-empty distinct
        collections, `rename` values distinct, `only`/`ignore` mutually
        exclusive — is already enforced per-declaration by
        `SourceTableDecl.table_shape` / `SourceEventSourceDecl.source_shape`;
        "at most one events block" is structural (`events` is a single
        optional field, never a list).

        Raises:
            ValueError: Two `tables` entries share the same `name`.
        """
        seen: set[str] = set()
        duplicates: list[str] = []
        for decl in self.tables:
            if decl.name in seen:
                duplicates.append(decl.name)
            seen.add(decl.name)
        if duplicates:
            raise ValueError(
                f"source.tables contains duplicate table names: {duplicates}"
            )
        return self


def _duplicate_tables(entries: "list[RenameEntry] | list[BaseRenderDecl]") -> list[str]:
    """Return `table` values appearing more than once across `entries`, in order.

    Args:
        entries: A list of entries each carrying a `table` attribute.

    Returns:
        The `table` value of each repeat occurrence past the first.
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    for entry in entries:
        if entry.table in seen:
            duplicates.append(entry.table)
        seen.add(entry.table)
    return duplicates


class BaseRenderDecl(StrictBaseModel):
    """Per-table rendering elections for the base mode."""

    table: str
    """The sidecar `records__<kind>` table this entry targets (the same
    keying as the mode's rename entries; targets disjoint across entries)."""
    render: "dict[str, RenderElection] | None" = None
    """Per-column election, keyed on pre-default identities (e.g.
    `created_sim_time`, `prop__error_rate`). A bare temporal literal elects
    a structural lifecycle column (shorthand); a typed object elects a
    payload column (`prop__<p>`). `last_mutation_sim_time` is outside the
    key domain. One column, one election; keys and shape validated at
    plan time. Absent = default rendering."""

    @model_validator(mode="after")
    def entry_well_formed(self) -> Self:
        """`table` is non-empty; `render` is well-formed.

        Raises:
            ValueError: `table` is empty, or `render` is present-but-empty
                or has an empty key. (Each entry's own shape is carried by
                its `RenderElection` model.)
        """
        _require_nonempty_str(self.table, "BaseRenderDecl.table")
        _require_render_map_valid(self.render, "BaseRenderDecl.render")
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
    render: list[BaseRenderDecl] | None = None
    """Per-table rendering elections; entries' `table` targets disjoint."""
    slice_at: int | None = None
    """Inclusive point-in-time horizon (sim-time ns). Absent -> tape's end.
    Mutually exclusive with `incremental` (enforced on ExportConfig)."""
    declare_keys: bool | None = None
    """Emit declared key constraints (PK/UNIQUE) for the DuckDB writer,
    resolved from the sidecar's presentation_keys registry. Absent or False
    -> off (a semantic default 'off', mirroring `slice_at` — not an invented
    mapping value). Ignored under CSV: a keys-not-declarable-csv notice is
    emitted instead."""

    @model_validator(mode="after")
    def at_least_one_field(self) -> Self:
        """A present `base` section sets at least one of
        exclude/rename/render/slice_at/declare_keys.

        Raises:
            ValueError: An empty `base: {}` block; omit the section instead.
        """
        if not self.model_fields_set:
            raise ValueError(
                "base section must set at least one of exclude / rename /"
                " render / slice_at / declare_keys (an empty base: {} block is"
                " not meaningful; omit the section for a bare current-state dump)"
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
        """No two rename entries, and no two render entries, share a `table` target.

        Raises:
            ValueError: Two rename entries, or two render entries, target the
                same table.
        """
        if self.rename is not None:
            rename_duplicates = _duplicate_tables(self.rename)
            if rename_duplicates:
                raise ValueError(
                    "base.rename contains more than one entry for the same"
                    f" table: {rename_duplicates}"
                )
        if self.render is not None:
            render_duplicates = _duplicate_tables(self.render)
            if render_duplicates:
                raise ValueError(
                    "base.render contains more than one entry for the same"
                    f" table: {render_duplicates}"
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
    """The declared app-database shape for the source mode; required when
    mode='source' (`mode_section_matches`) — there is no implicit bare-dump
    layout."""
    base: BaseConfig | None = None
    """The escape-hatch + slice declaration for the base mode; absent means a bare
    current-state dump with no exclude/rename/slice_at."""
    keys: dict[str, KeySurface | dict[str, KeySurface]] | None = None
    """Per-kind key election. A scalar elects the surface for the whole kind
    (every population, for a sub-typed kind); a map elects per sub-type.
    Absent: no election — every mode keys and renders record identity as
    today. Kind/sub-type existence, registry declaration, and union safety
    are export-time gates against the sidecar, not parse-time checks (the
    config is emit-independent)."""

    @model_validator(mode="after")
    def keys_well_formed(self) -> Self:
        """`keys` (when present) is non-empty; every per-kind map is non-empty.

        Emit-dependent checks (kind/sub-type existence, registry declaration,
        union safety) are deliberately not here — the config is emit-independent.

        Raises:
            ValueError: `keys` is an empty map, or a per-kind map value is empty.
        """
        _check_keys_well_formed(self.keys)
        return self

    @model_validator(mode="after")
    def mode_section_matches(self) -> Self:
        """The section named by `mode` is present; the other modes' sections
        are absent.

        `mode='dimensional'` requires the `dimensional` section (unchanged).
        `mode='source'` now joins that posture — it requires the `source`
        section (the bare-dump allowance is removed; `SourceConfig`'s own
        `source_section_required` validator additionally refuses `source: {}`,
        since a source config declares its output or is refused at load).
        `mode='base'` keeps its escape-hatch posture — a bare `mode: base` (no
        `base` section) stays a valid full dump. Whichever mode is selected,
        the *other* modes' sections must be absent.

        Raises:
            ValueError: `mode='dimensional'` without `dimensional`;
                `mode='source'` without `source`; any mode with another
                mode's section present.
        """
        if self.mode == "dimensional":
            if self.dimensional is None:
                raise ValueError("mode='dimensional' requires a 'dimensional' section")
            if self.source is not None:
                raise ValueError("mode='dimensional' forbids a 'source' section")
            if self.base is not None:
                raise ValueError("mode='dimensional' forbids a 'base' section")
        elif self.mode == "source":
            if self.source is None:
                raise ValueError("mode='source' requires a 'source' section")
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


def _validate_stream_name(name: str) -> None:
    """Validate a declared stream's `name` against the topic-name rule.

    The retired RoutingConfig groups-target rule, carried over verbatim: a
    stream's `name` is the topic — the Kafka topic, the `<name>.jsonl`
    filename, and the `events_per_topic` key — so it must be legal for all
    three sinks up front (the sink is a CLI flag; the config never knows it).

    Raises:
        ValueError: `name` does not match ^[A-Za-z0-9._-]+$, or is "." or "..".
    """
    if not _TOPIC_NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise ValueError(
            f"stream name {name!r} must be a valid topic name"
            " (^[A-Za-z0-9._-]+$ and not '.' or '..')"
        )


def _reject_prefixed_names(
    values: list[str], prefixes: tuple[str, ...], label: str
) -> None:
    """Raise if any of `values` carries one of `prefixes`.

    Shared by KindStream.properties (prop__) and MembershipStream.fields
    (elem__ / member__) so the bare-name rule never drifts between them.

    Raises:
        ValueError: naming the offending values, prefixed with `label`.
    """
    bad = [v for v in values if v.startswith(prefixes)]
    if bad:
        prefix_desc = " or ".join(prefixes)
        raise ValueError(f"{label} must not carry {prefix_desc} prefixes: {bad}")


def _reject_duplicate_names(values: list[str], label: str) -> None:
    """Raise if any of `values` repeats.

    Shared by KindStream.properties, KindStream.sub_types, and
    MembershipStream.fields.

    Raises:
        ValueError: naming the duplicated values, prefixed with `label`.
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    for v in values:
        if v in seen:
            duplicates.append(v)
        seen.add(v)
    if duplicates:
        raise ValueError(f"{label} contains duplicate names: {duplicates}")


class KindStream(StrictBaseModel):
    """A kind-shaped declared stream: an author-named topic fed by one kind's populations (content: state-changes)."""  # noqa: E501

    name: str
    """The topic — author-verbatim, matching the topic-name rule
    (^[A-Za-z0-9._-]+$ and not '.' or '..'). The Kafka topic, the
    <name>.jsonl filename, and the events_per_topic key."""

    kind: str
    """The records kind; resolves to records__<kind>."""

    sub_types: list[str] | None = None
    """Population scope for a sub-typed kind: declared `<kind>_type` values,
    non-empty and duplicate-free when present. Omitted on a sub-typed kind =
    the full discriminator domain. A flat kind refuses it (business pass)."""

    properties: list[str]
    """Bare property names projected into the after-image — required, no
    default: `[]` must be written to declare a notification feed (identity-only
    payload; the event set is payload-independent). Never `prop__`-prefixed;
    duplicate-free."""

    @model_validator(mode="after")
    def kind_stream_well_formed(self) -> Self:
        """name matches the topic-name rule; properties never prop__-prefixed
        and duplicate-free; sub_types non-empty and duplicate-free when present.

        Raises:
            ValueError: Any of the above.
        """
        _validate_stream_name(self.name)
        label = f"stream {self.name!r}: properties"
        _reject_prefixed_names(self.properties, ("prop__",), label)
        _reject_duplicate_names(self.properties, label)
        if self.sub_types is not None:
            if not self.sub_types:
                raise ValueError(
                    f"stream {self.name!r}: sub_types must be non-empty when"
                    " present (omit the field for the full discriminator domain)"
                )
            _reject_duplicate_names(self.sub_types, f"stream {self.name!r}: sub_types")
        return self


class MembershipStream(StrictBaseModel):
    """A membership-shaped declared stream: an author-named topic fed by one membership table (content: membership-events)."""  # noqa: E501

    name: str
    """The topic — same contract as KindStream.name."""

    membership: MembershipRef
    """The membership table."""

    fields: list[str]
    """Bare element-schema field names — required, no default: `[]` must be
    written to declare an owner-identity-only feed. Never `elem__`/`member__`-
    prefixed; duplicate-free."""

    @model_validator(mode="after")
    def membership_stream_well_formed(self) -> Self:
        """name matches the topic-name rule; fields never elem__/member__-
        prefixed and duplicate-free.

        Raises:
            ValueError: Any of the above.
        """
        _validate_stream_name(self.name)
        label = f"stream {self.name!r}: fields"
        _reject_prefixed_names(self.fields, ("elem__", "member__"), label)
        _reject_duplicate_names(self.fields, label)
        return self


def _check_stream_declaration_shape(value: object) -> object:
    """Reject a `streams[]` entry that carries neither or both of `kind` /
    `membership`, before union discrimination runs.

    Args:
        value: The raw entry (a dict from YAML, or an already-built model).

    Returns:
        `value` unchanged when exactly one of `kind` / `membership` is
        present (or `value` is not a dict — an already-built model has
        already been shape-checked by its own constructor).

    Raises:
        ValueError: `value` is a dict carrying neither or both of `kind` /
            `membership`, naming the two shapes.
    """
    if isinstance(value, dict):
        has_kind = "kind" in value
        has_membership = "membership" in value
        if has_kind and has_membership:
            raise ValueError(
                "streams[] entry carries both 'kind' and 'membership';"
                " declare exactly one shape (KindStream or MembershipStream)"
            )
        if not has_kind and not has_membership:
            raise ValueError(
                "streams[] entry carries neither 'kind' nor 'membership';"
                " declare exactly one shape (KindStream or MembershipStream)"
            )
    return value


def _stream_declaration_tag(value: object) -> str | None:
    """Discriminate a `streams[]` entry by which of `kind` / `membership` it
    carries.

    Args:
        value: The raw entry (a dict from YAML, or an already-built model).

    Returns:
        'kind' or 'membership' when exactly one is present; None otherwise
        (unreachable in practice — `_check_stream_declaration_shape` already
        rejected the neither/both cases).
    """
    if isinstance(value, dict):
        has_kind = "kind" in value
    else:
        has_kind = getattr(value, "kind", None) is not None
    return "kind" if has_kind else "membership"


StreamDeclaration = Annotated[
    Annotated[
        Annotated[KindStream, Tag("kind")]
        | Annotated[MembershipStream, Tag("membership")],
        Discriminator(_stream_declaration_tag),
    ],
    BeforeValidator(_check_stream_declaration_shape),
]
"""A declared stream: discriminated by which of `kind` / `membership` the
entry carries — a declaration mixing the two shapes' fields is
unrepresentable, not validated away. An entry with neither or both fails
parse with a message naming the two shapes."""


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

    table_identity: Literal["source_table", "topic"] = "source_table"
    """What source.table (and the value-schema name) reports: the event's
    route_table leaf (canonical Debezium, the default) or the declaring
    stream's name. Moved here from the retired RoutingConfig; meaning
    unchanged."""
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


class StreamConfig(StrictBaseModel):
    """Streaming delivery envelope: content and the declared streams."""

    content: Literal["state-changes", "membership-events"]
    """The event content axis. Selects the fold family and the required
    declaration shape: KindStream for state-changes, MembershipStream for
    membership-events. Closed Literal so a further content type is additive."""
    streams: list[StreamDeclaration]
    """The declared streams — required, non-empty, names unique. Replaces
    `kinds`, `memberships`, and `routing`. Every entry's shape must match
    `content`."""
    keys: dict[str, KeySurface | dict[str, KeySurface]] | None = None
    """Per-kind key election — the ExportConfig.keys grammar and
    keys_well_formed validator, verbatim. Absent: record_id throughout (every
    identity render site renders byte-identically to today)."""
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
    today's behavior. The optional-block `= None` exception, mirroring rebase /
    debezium."""
    kafka: KafkaConfig | None = None
    """Optional Kafka connection block; None ⇒ bootstrap comes from --bootstrap-servers
    or FABEXPORT_KAFKA_BOOTSTRAP. The optional-block `= None` exception, mirroring
    rebase / debezium / clock. Inert unless --sink kafka."""

    @model_validator(mode="after")
    def streams_match_content(self) -> Self:
        """`streams` is non-empty; every entry's shape matches `content`
        (KindStream for state-changes, MembershipStream for
        membership-events). Replaces selection_matches_content.

        Raises:
            ValueError: `streams` is empty, or an entry's shape does not
                match `content`.
        """
        if not self.streams:
            raise ValueError("StreamConfig.streams must be non-empty")
        expected = KindStream if self.content == "state-changes" else MembershipStream
        mismatched = [s.name for s in self.streams if not isinstance(s, expected)]
        if mismatched:
            raise ValueError(
                f"StreamConfig.streams entries {mismatched} do not match"
                f" content={self.content!r} (expected {expected.__name__})"
            )
        return self

    @model_validator(mode="after")
    def stream_names_unique(self) -> Self:
        """No two streams share a `name` (replaces kinds_unique /
        memberships_unique — same-kind and same-table repeats are now legal;
        identity is the name).

        Raises:
            ValueError: A name appears more than once.
        """
        seen: set[str] = set()
        duplicates: list[str] = []
        for s in self.streams:
            if s.name in seen:
                duplicates.append(s.name)
            seen.add(s.name)
        if duplicates:
            raise ValueError(
                f"StreamConfig.streams contains duplicate stream names: {duplicates}"
            )
        return self

    @model_validator(mode="after")
    def keys_well_formed(self) -> Self:
        """`keys` (when present) is non-empty; every per-kind map is non-empty.

        Emit-dependent checks (kind/sub-type existence, registry declaration,
        union safety) are deliberately not here — the config is emit-independent.

        Raises:
            ValueError: `keys` is an empty map, or a per-kind map value is empty.
        """
        _check_keys_well_formed(self.keys)
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
