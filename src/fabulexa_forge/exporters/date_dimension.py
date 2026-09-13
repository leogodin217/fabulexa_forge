"""The generated calendar: `dim_date` compile + the `date_ref` range guard.

Beside `exporters/supplements.py` — the mode-neutral position the supplement
compile and cell probe occupy, importable by `query_spec.py` and
`companion/dictionary.py` without reaching into `exporters/dimensional/`.
`compile_date_dimension_spec` is a pure function of the declared range: no
emit, no anchor, no session, so every caller compiles it before any horizon
opens. `check_date_refs_in_range` is the range guard: the last pre-write gate
every `date_ref`-bearing invocation runs before the first byte lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, cast

from fabulexa_forge._sql import date_key_expr
from fabulexa_forge.errors import DateRefOutOfRange
from fabulexa_forge.exporters.query_spec import QuerySpec

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from fabulexa_forge.config.models import DateDimensionConfig
    from fabulexa_forge.reader.emit import Emit

DATE_DIMENSION_TABLE_NAME: Final = "dim_date"
"""The generated calendar's published output-table name — mode-definitional."""

DATE_DIMENSION_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("date_key", "INTEGER"),
    ("date", "DATE"),
    ("year", "INTEGER"),
    ("quarter", "INTEGER"),
    ("month", "INTEGER"),
    ("day", "INTEGER"),
    ("day_of_week", "INTEGER"),
    ("day_of_year", "INTEGER"),
    ("iso_year", "INTEGER"),
    ("iso_week", "INTEGER"),
    ("month_name", "VARCHAR"),
    ("day_name", "VARCHAR"),
    ("is_weekend", "BOOLEAN"),
)
"""The pinned (name, type-text) pairs of `dim_date`, in output order — the
one authority the relation, the documentation dictionary, and the tests read."""


@dataclass(frozen=True)
class CalendarSource:
    """Manifest-facing provenance of the generated calendar: the declared
    inclusive range."""

    from_: "date"
    to: "date"


def _dim_date_relation_sql(from_: "date", to: "date") -> str:
    """The `dim_date` SELECT: one calendar day per row, pinned columns, ordered.

    Args:
        from_: First calendar day, inclusive.
        to: Last calendar day, inclusive.

    Returns:
        The full SELECT — a `generate_series` day spine projected to
        `DATE_DIMENSION_COLUMNS` in order, ordered by `date_key`.
    """
    day = '"_days"."day"'
    return (
        'WITH "_days" AS ('
        f'SELECT CAST(generate_series AS DATE) AS "day" FROM'
        f" generate_series(DATE '{from_.isoformat()}', DATE '{to.isoformat()}',"
        " INTERVAL 1 DAY)"
        ") SELECT"
        f' {date_key_expr(day)} AS "date_key",'
        f' {day} AS "date",'
        f' CAST(year({day}) AS INTEGER) AS "year",'
        f' CAST(quarter({day}) AS INTEGER) AS "quarter",'
        f' CAST(month({day}) AS INTEGER) AS "month",'
        f' CAST(day({day}) AS INTEGER) AS "day",'
        f' CAST(isodow({day}) AS INTEGER) AS "day_of_week",'
        f' CAST(dayofyear({day}) AS INTEGER) AS "day_of_year",'
        f' CAST(isoyear({day}) AS INTEGER) AS "iso_year",'
        f' CAST(week({day}) AS INTEGER) AS "iso_week",'
        f' monthname({day}) AS "month_name",'
        f' dayname({day}) AS "day_name",'
        f' (isodow({day}) >= 6) AS "is_weekend"'
        ' FROM "_days" ORDER BY "date_key"'
    )


def compile_date_dimension_spec(
    config: "DateDimensionConfig",
    write_mode: Literal["create", "replace"],
) -> QuerySpec:
    """Compile the generated calendar into a QuerySpec.

    The SQL is one `generate_series(DATE from, DATE to, INTERVAL 1 DAY)`
    relation, each element cast to DATE, projected to the pinned
    `DATE_DIMENSION_COLUMNS` in order (every integer part cast to INTEGER,
    `date_key` through `date_key_expr`, `day_of_week` ISO 1 = Monday,
    `is_weekend` = `day_of_week >= 6`, English month / day names), ordered
    by `date_key`. Carries `table_name=DATE_DIMENSION_TABLE_NAME`, no keys,
    empty provenance / kind_values / author_descriptions / references, no
    table description, `event_log=False`, `supplement=None`, and
    `CalendarSource(from, to)`. A pure function of the config block — no
    emit, no anchor, no session — so every caller compiles it before any
    horizon opens.

    Args:
        config: The validated `date_dimension` block.
        write_mode: 'create' for a full export, 'replace' for a windowed
            compile — the caller's delivery regime, never inferred.

    Returns:
        The `dim_date` QuerySpec.
    """
    return QuerySpec(
        table_name=DATE_DIMENSION_TABLE_NAME,
        sql=_dim_date_relation_sql(config.from_, config.to),
        write_mode=write_mode,
        calendar=CalendarSource(from_=config.from_, to=config.to),
    )


def _date_ref_columns(spec: QuerySpec) -> list[str]:
    """The spec's output columns that reference `dim_date`, output order.

    Args:
        spec: A compiled QuerySpec.

    Returns:
        Every output column name whose `references` entry targets
        `DATE_DIMENSION_TABLE_NAME`, in `references`' insertion order.
    """
    return [
        column
        for column, target in spec.references.items()
        if target == DATE_DIMENSION_TABLE_NAME
    ]


def _range_guard_sql(
    columns: "Sequence[str]", relation_sql: str, from_: "date", to: "date"
) -> str:
    """The one aggregate query probing every named column against the bounds.

    Args:
        columns: The spec's `date_ref` output columns, in output order.
        relation_sql: The spec's compiled relation.
        from_: The declared range's lower bound.
        to: The declared range's upper bound.

    Returns:
        A single-row SELECT: MIN/MAX per column (in order), then the lower
        and upper bound keys.
    """
    aggs = ", ".join(
        f'MIN("{column}") AS "_min{index}", MAX("{column}") AS "_max{index}"'
        for index, column in enumerate(columns)
    )
    lower = date_key_expr(f"DATE '{from_.isoformat()}'")
    upper = date_key_expr(f"DATE '{to.isoformat()}'")
    return (
        f'SELECT {aggs}, {lower} AS "_lower", {upper} AS "_upper"'
        f' FROM ({relation_sql}) AS "_rel"'
    )


def check_date_refs_in_range(
    emit: "Emit",
    specs: "Sequence[QuerySpec]",
    config: "DateDimensionConfig",
) -> None:
    """The range guard: every non-NULL `date_ref` value lies in the calendar.

    For each spec whose `references` maps at least one column to
    `DATE_DIMENSION_TABLE_NAME`, evaluates one aggregate query over the
    spec's relation via `emit.query` — `MIN` and `MAX` of each such column,
    plus `date_key_expr` over `DATE '<from>'` and `DATE '<to>'` as the two
    bound keys, in the same statement — and compares each column's minimum
    against the lower bound and maximum against the upper. Runs before the
    first write of an invocation (full export), before each window's write
    (incremental), and before each ask returns (shaped playback). Columns
    are checked in the spec's output order (`references` insertion order —
    stamped by the plan compile in declaration order); the first violating
    column refuses, its minimum reported when that is below the lower
    bound, else its maximum. NULL aggregates (an empty relation, or an
    all-NULL column) never violate. A spec whose `references` names no
    `dim_date` column is not probed. Reads only `spec.sql`,
    `spec.references`, and the block — never a sidecar, never a tape of
    its own: the compiled SQL is self-contained (the truncated-tape CTEs
    are inlined), so it runs on the entry point's own `emit`.

    Args:
        emit: The open emit whose session evaluates each relation.
        specs: The compiled specs of the invocation / window / ask.
        config: The validated `date_dimension` block.

    Raises:
        DateRefOutOfRange: A non-NULL value lies outside `[from, to]`; names
            the table, the column, the offending key as it appears in the
            column, and the declared range as ISO dates.
    """
    for spec in specs:
        columns = _date_ref_columns(spec)
        if not columns:
            continue
        sql = _range_guard_sql(columns, spec.sql, config.from_, config.to)
        (row,) = emit.query(sql, ())
        lower = cast(int, row[-2])
        upper = cast(int, row[-1])
        for index, column in enumerate(columns):
            minimum = cast("int | None", row[2 * index])
            if minimum is None:
                continue
            maximum = cast(int, row[2 * index + 1])
            if minimum < lower:
                _raise_out_of_range(spec.table_name, column, minimum, config)
            if maximum > upper:
                _raise_out_of_range(spec.table_name, column, maximum, config)


def _raise_out_of_range(
    table_name: str, column: str, key: int, config: "DateDimensionConfig"
) -> None:
    """Raise `DateRefOutOfRange` for one violating column.

    Args:
        table_name: The spec's output table name.
        column: The violating output column name.
        key: The offending date key.
        config: The validated `date_dimension` block, for the range in the
            message.

    Raises:
        DateRefOutOfRange: Always.
    """
    raise DateRefOutOfRange(
        f"table '{table_name}' column '{column}': date key {key} lies"
        f" outside date_dimension {config.from_.isoformat()}..{config.to.isoformat()}"
    )
