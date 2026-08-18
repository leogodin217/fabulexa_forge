"""Shared SQL-string utilities used across reader, derivation, exporter, and
corrupter layers."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from fabulexa_forge.errors import ExportError


def quote_identifier(name: str) -> str:
    """Wrap a SQL identifier in double quotes, doubling any internal quotes.

    The one identifier-quoting helper: every CREATE TABLE / SELECT / DESCRIBE
    splice of a name that is not pattern-gated at config load (e.g. a
    bundle-sourced sidecar table name) must go through this, so an embedded
    `"` can never break out of the identifier position.

    Args:
        name: The identifier string (table or column name).

    Returns:
        The DuckDB double-quoted identifier string.
    """
    return '"' + name.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    """Render a Python string as a single-quoted SQL literal (escaped).

    Args:
        value: The string value to quote.

    Returns:
        A SQL single-quoted string literal with internal single-quotes escaped.
    """
    return "'" + value.replace("'", "''") + "'"


# Integer families recognized by DuckDB
_INTEGER_TYPES = {
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "UHUGEINT",
}

# Floating-point families
_FLOAT_TYPES = {"DOUBLE", "FLOAT", "REAL"}

# Strict grammar for the parameterized families. Anchored end-to-end: nothing
# may follow the closing paren, so a payload that closes a CAST and appends
# SQL (e.g. "VARCHAR(10)) FROM read_csv(...) --") can never pass the gate.
_PARAMETERIZED_TYPE_RE = re.compile(
    r"^(?:VARCHAR\(\s*\d+\s*\)|(?:DECIMAL|NUMERIC)\(\s*\d+\s*(?:,\s*\d+\s*)?\))$"
)


def is_recognized_sql_type(sql_type: str) -> bool:
    """Whether `sql_type` is on the recognized DuckDB type allow-list.

    The one type-name gate shared by `render_typed_literal` and every config
    surface that accepts an author-supplied type string (e.g.
    `SchemaDrift.retype_to`) — a type name that fails here must never be
    spliced into SQL. Parameterized forms are matched by an anchored grammar
    (digits-only arguments, nothing after the closing paren), never by prefix.

    Args:
        sql_type: The DuckDB SQL type name (e.g. 'VARCHAR', 'BIGINT').

    Returns:
        True iff the type is VARCHAR, BOOLEAN, an integer family member, a
        float family member, or a strictly-parameterized VARCHAR(n) /
        DECIMAL(p[,s]) / NUMERIC(p[,s]).
    """
    upper = sql_type.upper()
    return (
        upper == "VARCHAR"
        or upper in _INTEGER_TYPES
        or upper in _FLOAT_TYPES
        or upper == "BOOLEAN"
        or _PARAMETERIZED_TYPE_RE.fullmatch(upper) is not None
    )


def render_typed_literal(value: str, sql_type: str) -> str:
    """Render a scalar value as a SQL literal typed to sql_type.

    VARCHAR (or VARCHAR( prefix) → single-quoted with '' escaping.
    Integer family (TINYINT/SMALLINT/INTEGER/BIGINT/HUGEINT + unsigned variants)
    / DOUBLE/FLOAT/REAL / DECIMAL(p,s) / BOOLEAN → CAST('<escaped>' AS <type>).
    Unknown types → raise ExportError.

    Args:
        value: The raw string value to render.
        sql_type: The DuckDB SQL type name (e.g. 'VARCHAR', 'BIGINT', 'BOOLEAN').

    Returns:
        A SQL literal fragment (not a full expression).

    Raises:
        ExportError: sql_type is not a recognized DuckDB type.
    """
    if not is_recognized_sql_type(sql_type):
        raise ExportError(
            f"render_typed_literal: unrecognized SQL type '{sql_type}'"
            " — no silent VARCHAR fallback"
        )

    escaped = value.replace("'", "''")
    upper = sql_type.upper()

    if upper == "VARCHAR" or upper.startswith("VARCHAR("):
        return f"'{escaped}'"

    return f"CAST('{escaped}' AS {sql_type})"


def render_predicate_condition(
    column: str,
    value: str | list[str],
    sql_type: str,
    alias: str | None,
) -> str:
    """Render one config predicate entry as a SQL condition.

    A `str` is a scalar and renders `= <literal>`; a `list` is a set of
    alternatives and renders `IN (<literal>, ...)` preserving element order.
    Discrimination is on `isinstance(value, str)` — never on `Sequence`, which
    a `str` itself satisfies. Every element is typed by `render_typed_literal`
    against `sql_type`, so a list is exactly the scalar rule applied
    element-wise.

    The one predicate-rendering authority: no module outside this one renders
    `=` or `IN` over a config predicate value.

    Args:
        column: The predicate column name, quoted through `quote_identifier`.
        value: The required value — a scalar, or a non-empty list of
            alternatives. Emptiness is a parse-time failure and is not
            re-checked here.
        sql_type: The column's DuckDB type from the sidecar, used to type each
            literal. `VARCHAR` yields the raw single-quoted literal, which is
            how the `history.value` surface gets its untyped comparison — a
            caller's type choice, not a mode of this function.
        alias: A relation alias to qualify the column with, or None for an
            unqualified condition. Only the point-in-time membership foreign
            key needs one; every other caller passes None explicitly.

    Returns:
        One SQL condition, unparenthesized, suitable for AND-joining with
        sibling conditions.

    Raises:
        ExportError: `sql_type` is not a recognized DuckDB type. Raised by
            `render_typed_literal`, which never falls back to VARCHAR.
    """
    quoted_column = quote_identifier(column)
    qualified = quoted_column if alias is None else f"{alias}.{quoted_column}"

    if isinstance(value, str):
        return f"{qualified} = {render_typed_literal(value, sql_type)}"

    literals = ", ".join(render_typed_literal(element, sql_type) for element in value)
    return f"{qualified} IN ({literals})"


# DuckDB's VARCHAR->BOOLEAN literal grammar (case-insensitive), mirrored here so
# `cast_predicate_element` never needs a live connection to decide castability.
_BOOLEAN_TRUE_LITERALS = frozenset({"true", "t", "yes", "y", "1"})
_BOOLEAN_FALSE_LITERALS = frozenset({"false", "f", "no", "n", "0"})


def cast_predicate_element(element: str, sql_type: str) -> object:
    """The plan-time constant evaluation of the CAST `render_typed_literal`
    compiles: one predicate element's typed value under a column's declared
    DuckDB type.

    Returned values are hashable, and `==` / `hash` realize typed-value
    identity under `sql_type` — two spellings of one value ('5' / '05'
    under BIGINT) are one value. Reads no rows.

    Args:
        element: The raw config string element.
        sql_type: The column's DuckDB type from the sidecar.

    Returns:
        The typed value.

    Raises:
        ValueError: `sql_type` cannot cast `element` (the caller wraps this
            into `SourceWhereValueUncastable` with owner context).
        ExportError: `sql_type` is not a recognized DuckDB type — never a
            silent VARCHAR fallback (per `render_typed_literal`).
    """
    if not is_recognized_sql_type(sql_type):
        raise ExportError(
            f"cast_predicate_element: unrecognized SQL type {sql_type!r}"
            " — no silent VARCHAR fallback"
        )

    upper = sql_type.upper()

    if upper == "VARCHAR" or upper.startswith("VARCHAR("):
        return element

    if upper in _INTEGER_TYPES:
        try:
            return int(element)
        except ValueError as exc:
            raise ValueError(f"{element!r} does not cast to {sql_type}") from exc

    if upper in _FLOAT_TYPES:
        try:
            return float(element)
        except ValueError as exc:
            raise ValueError(f"{element!r} does not cast to {sql_type}") from exc

    if upper == "BOOLEAN":
        lowered = element.strip().lower()
        if lowered in _BOOLEAN_TRUE_LITERALS:
            return True
        if lowered in _BOOLEAN_FALSE_LITERALS:
            return False
        raise ValueError(f"{element!r} does not cast to {sql_type}")

    # The remaining recognized family: DECIMAL(p[,s]) / NUMERIC(p[,s]).
    try:
        return Decimal(element)
    except InvalidOperation as exc:
        raise ValueError(f"{element!r} does not cast to {sql_type}") from exc


# ---------------------------------------------------------------------------
# date_parse format anatomy: the closed instant-string directive vocabulary
# ---------------------------------------------------------------------------

_DATE_PARSE_DIRECTIVE_RE = re.compile(r"%.")

# Directives that share one temporal field are mutually exclusive alternative
# forms of that field — at most one of a class's members may appear.
_DATE_PARSE_YEAR_DIRECTIVES = frozenset({"%Y", "%y"})
_DATE_PARSE_MONTH_DIRECTIVES = frozenset({"%m", "%b", "%B"})
_DATE_PARSE_HOUR_DIRECTIVES = frozenset({"%H", "%I"})
_DATE_PARSE_FRACTION_DIRECTIVES = frozenset({"%f", "%g"})

_DATE_PARSE_DATE_FIELD_DIRECTIVES = (
    _DATE_PARSE_YEAR_DIRECTIVES | _DATE_PARSE_MONTH_DIRECTIVES | {"%d"}
)

_DATE_PARSE_ALLOWED_DIRECTIVES = (
    _DATE_PARSE_DATE_FIELD_DIRECTIVES
    | _DATE_PARSE_HOUR_DIRECTIVES
    | _DATE_PARSE_FRACTION_DIRECTIVES
    | {"%p", "%M", "%S", "%%"}
)

# Uniqueness classes: each is one temporal field; a validated format carries
# at most one directive from each class (named in the error message).
_DATE_PARSE_UNIQUENESS_CLASSES: dict[str, frozenset[str]] = {
    "year": _DATE_PARSE_YEAR_DIRECTIVES,
    "month": _DATE_PARSE_MONTH_DIRECTIVES,
    "day": frozenset({"%d"}),
    "hour": _DATE_PARSE_HOUR_DIRECTIVES,
    "AM/PM marker": frozenset({"%p"}),
    "minute": frozenset({"%M"}),
    "second": frozenset({"%S"}),
    "sub-second fraction": _DATE_PARSE_FRACTION_DIRECTIVES,
}


def _date_parse_directives(fmt: str, field_name: str) -> list[str]:
    """Extract a date_parse format's `%`-directives, rejecting malformed or
    out-of-vocabulary ones.

    Well-formedness only (a valid `%`-count, a closed directive set);
    pairing, uniqueness, and completeness are the caller's concern.

    Args:
        fmt: The author-declared format string (already known non-empty).
        field_name: The field's dotted name, for the error message.

    Returns:
        The format's directives in order, `%%` included.

    Raises:
        ValueError: A `%` is not part of a well-formed directive, or a
            directive is outside the closed vocabulary.
    """
    directives = _DATE_PARSE_DIRECTIVE_RE.findall(fmt)
    accounted_percents = sum(2 if d == "%%" else 1 for d in directives)
    if fmt.count("%") != accounted_percents:
        raise ValueError(f"{field_name} {fmt!r} contains a malformed '%' directive")
    unknown = sorted({d for d in directives if d not in _DATE_PARSE_ALLOWED_DIRECTIVES})
    if unknown:
        raise ValueError(
            f"{field_name} {fmt!r} uses unsupported directive(s) {unknown};"
            " date_parse format must denote a complete date, a complete time,"
            " or both, using only %Y/%y/%m/%b/%B/%d (date), %H/%I/%p/%M/%S/%f/%g"
            " (time), %% (literal), and text"
        )
    return directives


def _date_parse_completeness(directives: list[str]) -> tuple[bool, bool]:
    """Whether a format's directives are date-complete and/or time-complete.

    Assumes `directives` already passed the closed-vocabulary check
    (`_date_parse_directives`); does not re-validate pairing or uniqueness.

    Args:
        directives: The format's extracted directives.

    Returns:
        `(date_complete, time_complete)` — date-complete iff a year, a
        month, and `%d` all appear; time-complete iff `%H` appears, or
        `%I` and `%p` both appear.
    """
    has_year = any(d in _DATE_PARSE_YEAR_DIRECTIVES for d in directives)
    has_month = any(d in _DATE_PARSE_MONTH_DIRECTIVES for d in directives)
    has_day = "%d" in directives
    has_hour_24 = "%H" in directives
    has_hour_12 = "%I" in directives
    has_ampm = "%p" in directives
    date_complete = has_year and has_month and has_day
    time_complete = has_hour_24 or (has_hour_12 and has_ampm)
    return date_complete, time_complete


def validate_date_parse_format(fmt: str, field_name: str) -> None:
    """A `date_parse` format string denotes a complete temporal value.

    Closed strptime-directive set — date class `%Y`/`%y` (year),
    `%m`/`%b`/`%B` (month), `%d` (day); time class `%H`/`%I` (hour), `%p`
    (AM/PM), `%M` (minute), `%S` (second), `%f` (µs), `%g` (ms); `%%`
    (literal `%`) plus arbitrary literal text. Pairing: `%I` and `%p` each
    require the other; `%M` requires an hour directive; `%S` requires `%M`;
    `%f`/`%g` require `%S`. Uniqueness: each temporal field at most once —
    no repeated directive, no two alternative forms of one field
    (`%Y`/`%y`, `%m`/`%b`/`%B`, `%H`/`%I`, `%f`/`%g`). Completeness: the
    format must be date-complete (a year directive + a month directive +
    `%d`), time-complete (an hour directive — `%H`, or `%I` with `%p`), or
    both.

    Args:
        fmt: The author-declared format string.
        field_name: The field's dotted name, for the error message.

    Raises:
        ValueError: `fmt` is empty, contains a malformed or unsupported `%`
            directive, violates a pairing or uniqueness rule, or is neither
            date-complete nor time-complete. The message names the format
            and the violated rule.
    """
    if not fmt:
        raise ValueError(f"{field_name} must be non-empty")
    directives = _date_parse_directives(fmt, field_name)

    for label, class_directives in _DATE_PARSE_UNIQUENESS_CLASSES.items():
        if sum(1 for d in directives if d in class_directives) > 1:
            raise ValueError(
                f"{field_name} {fmt!r} names the {label} field more than once"
                " (a temporal field takes at most one directive)"
            )

    has_hour_12 = "%I" in directives
    has_ampm = "%p" in directives
    if has_hour_12 != has_ampm:
        raise ValueError(
            f"{field_name} {fmt!r}: %I and %p must appear together"
            " (the 12-hour clock requires its AM/PM marker)"
        )
    has_hour = has_hour_12 or "%H" in directives
    has_minute = "%M" in directives
    if has_minute and not has_hour:
        raise ValueError(
            f"{field_name} {fmt!r}: %M requires an hour directive (%H or %I)"
        )
    has_second = "%S" in directives
    if has_second and not has_minute:
        raise ValueError(f"{field_name} {fmt!r}: %S requires %M")
    has_fraction = any(d in _DATE_PARSE_FRACTION_DIRECTIVES for d in directives)
    if has_fraction and not has_second:
        raise ValueError(f"{field_name} {fmt!r}: %f/%g require %S")

    date_complete, time_complete = _date_parse_completeness(directives)
    has_any_date_field = any(d in _DATE_PARSE_DATE_FIELD_DIRECTIVES for d in directives)
    if has_any_date_field and not date_complete:
        raise ValueError(
            f"{field_name} {fmt!r} has a partial calendar date"
            " (a year, month, and day directive must appear together)"
        )
    if not date_complete and not time_complete:
        raise ValueError(
            f"{field_name} {fmt!r} must denote a complete date"
            " (a year, month, and day directive), a complete time"
            " (%H, or %I with %p), or both"
        )


def date_parse_denoted_type(fmt: str) -> Literal["DATE", "TIME", "TIMESTAMP"]:
    """The temporal type a validated date_parse format denotes.

    The single derivation authority: complete date only -> DATE; complete
    date + complete time -> TIMESTAMP; complete time only -> TIME. Every
    consumer of a parse's output type (the renderer, any plan-time typing
    read) resolves through this function; none re-inspects the format.

    Args:
        fmt: A format string that has passed validate_date_parse_format.

    Returns:
        The denoted DuckDB type name.
    """
    directives = _DATE_PARSE_DIRECTIVE_RE.findall(fmt)
    date_complete, time_complete = _date_parse_completeness(directives)
    if date_complete and time_complete:
        return "TIMESTAMP"
    if time_complete:
        return "TIME"
    return "DATE"


def render_date_parse_expr(
    qualified_source: str,
    date_format: str,
    out_name: str,
    table_label: str,
) -> str:
    """Render the SQL SELECT fragment reinterpreting a VARCHAR column as its
    format-denoted temporal type under an author-declared format.

    Lives in the shared SQL utilities — every mode renders a declared parse
    through this one function. The output type is the format's denoted type
    (date_parse_denoted_type). NULL source values yield NULL of that type.
    A non-NULL value not matching the format fails the export loudly at
    query time, naming table_label, the source column, and the offending
    value — never a silent NULL. TIMESTAMP and TIME denotations truncate to
    µs (the family-wide presentation rule; `%g` milliseconds widen exactly
    to µs — DuckDB's `%f` already zero-pads a shorter fraction on the
    right, so `%g` is spliced into STRPTIME as `%f`). The format is
    assumed validated (validate_date_parse_format).

    Args:
        qualified_source: The fully table-qualified VARCHAR source column SQL.
        date_format: The author-declared strptime-style format.
        out_name: The output column name (the `AS "<out_name>"` alias).
        table_label: The output table name interpolated into the guard's
            error message.

    Returns:
        A SQL SELECT-list expression fragment ending in `AS "<out_name>"`,
        typed as the format's denoted type.
    """
    denoted_type = date_parse_denoted_type(date_format)
    column_label = qualified_source.rsplit(".", 1)[-1].strip('"')
    strptime_format = date_format.replace("%g", "%f")
    format_literal = _sql_literal(strptime_format)
    message_prefix = _sql_literal(
        f"date_parse on '{table_label}.{column_label}': value '"
    )
    message_suffix = _sql_literal(f"' does not match format '{date_format}'")
    error_expr = (
        f"error({message_prefix}"
        f" || CAST({qualified_source} AS VARCHAR) || {message_suffix})"
    )
    return (
        "CASE"
        f" WHEN {qualified_source} IS NULL THEN CAST(NULL AS {denoted_type})"
        f" WHEN TRY_STRPTIME({qualified_source}, {format_literal}) IS NOT NULL"
        f" THEN CAST(STRPTIME({qualified_source}, {format_literal}) AS {denoted_type})"
        f" ELSE CAST({error_expr} AS {denoted_type})"
        f' END AS "{out_name}"'
    )
