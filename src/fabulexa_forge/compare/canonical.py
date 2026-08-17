"""The compare surface's canonical form: family classification and value
encoding.

Two functions carry the whole canonical-form contract: `family_of` classifies
a DuckDB type name into one of the ten canonical families the comparison
universe recognizes (`None` for a type outside every family — `DECIMAL`
deliberately among them); `encode_value` renders one materialized value to
its canonical text form within a family. Encoding is Python-side on already-
materialized values, never a SQL `CAST(... AS VARCHAR)` — byte-identity with
the C6 conformance codec's encode half (`reader.conformance.to_csv_text`) for
the four overlapping families (integer / float / boolean / text) is the
contract, asserted by test, never by import (`tests/compare/test_canonical.py`).

See `docs/architecture/pending/dataset-equivalence.md` § Canonical value
encoding for the semantic authority (the family table, the interval day-fold
and month-carrying fallback, the timestamptz UTC normalization).
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import pyarrow as pa

CanonicalFamily = Literal[
    "integer",
    "float",
    "boolean",
    "text",
    "timestamp",
    "date",
    "time",
    "timestamptz",
    "interval",
    "blob",
]

_INTEGER_TYPES = frozenset(
    {
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
)
_FLOAT_TYPES = frozenset({"DOUBLE", "FLOAT"})


def family_of(duckdb_type: str) -> CanonicalFamily | None:
    """
    Classify a DuckDB type name into its canonical family.

    Implements the doc's family table: any integer type -> integer;
    DOUBLE/FLOAT -> float; BOOLEAN -> boolean; VARCHAR -> text; TIMESTAMP at
    any precision -> timestamp; DATE -> date; TIME at any precision -> time;
    TIMESTAMPTZ at any precision -> timestamptz; INTERVAL -> interval;
    BLOB -> blob.

    Args:
        duckdb_type: A DuckDB type name as the catalog reports it.

    Returns:
        The canonical family, or None for a type outside every family
        (DECIMAL deliberately among them — the caller decides whether that
        is an error, per the comparison-universe scope rule).
    """
    norm = duckdb_type.upper().strip()
    if norm in _INTEGER_TYPES:
        return "integer"
    if norm in _FLOAT_TYPES:
        return "float"
    if norm == "BOOLEAN":
        return "boolean"
    if norm == "VARCHAR":
        return "text"
    if norm.startswith("TIMESTAMPTZ") or "WITH TIME ZONE" in norm:
        return "timestamptz"
    if norm.startswith("TIMESTAMP"):
        return "timestamp"
    if norm == "DATE":
        return "date"
    if norm.startswith("TIME"):
        return "time"
    if norm == "INTERVAL":
        return "interval"
    if norm == "BLOB":
        return "blob"
    return None


def encode_value(value: object, family: CanonicalFamily) -> str | None:
    """
    Encode one materialized value to its canonical text form.

    Implements the doc's encoding table: str(int) / repr(float) /
    "true"/"false" / identity text / microsecond-precision temporal forms
    (timestamptz normalized to UTC `+00:00`; naive timestamp as stored) /
    the interval `[-]H:MM:SS.ffffff` form with the 24h day-fold and the
    DuckDB-text fallback for month-carrying values / lowercase hex for blob.
    Byte-identical to the C6 codec's `to_csv_text` for the four families it
    covers (integer, float, boolean, text) — asserted by test, never imported.

    Args:
        value: The materialized cell value. NULL arrives as None. Interval
            values arrive as the Arrow month/day/nanosecond triple so
            calendar components are observable.
        family: The expected column's canonical family, directing the encoding.

    Returns:
        Canonical text, or None for a NULL input (None is carried through the
        encoded tuple, distinct by construction from every encoded string).
    """
    if value is None:
        return None
    if family == "integer":
        assert isinstance(value, int)
        return str(value)
    if family == "float":
        assert isinstance(value, (int, float))
        return repr(float(value))
    if family == "boolean":
        assert isinstance(value, bool)
        return "true" if value else "false"
    if family == "text":
        assert isinstance(value, str)
        return value
    if family == "timestamp":
        assert isinstance(value, datetime.datetime)
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")
    if family == "date":
        assert isinstance(value, datetime.date)
        return value.isoformat()
    if family == "time":
        assert isinstance(value, datetime.time)
        return value.strftime("%H:%M:%S.%f")
    if family == "timestamptz":
        assert isinstance(value, datetime.datetime)
        return _encode_timestamptz(value)
    if family == "interval":
        return _encode_interval(value)
    assert family == "blob"
    assert isinstance(value, (bytes, bytearray))
    return value.hex()


def _encode_timestamptz(value: datetime.datetime) -> str:
    """Normalize an aware datetime to its UTC instant, canonical text form.

    Args:
        value: A zone-aware datetime (any offset).

    Returns:
        `YYYY-MM-DD HH:MM:SS.ffffff+00:00` — the instant in UTC.
    """
    utc_value = value.astimezone(datetime.timezone.utc)
    return f"{utc_value.strftime('%Y-%m-%d %H:%M:%S.%f')}+00:00"


def _encode_interval(value: "pa.MonthDayNano") -> str:
    """Encode an interval's month/day/nanosecond triple to canonical text.

    A nonzero months field has no fixed microsecond value, so it encodes as
    DuckDB's own text rendering instead (never the `[-]H:MM:SS.ffffff` form,
    never an error) — no `[-]H:MM:SS.ffffff` encoding can equal that text, so
    a month-carrying actual value surfaces as a row discrepancy carrying the
    real text. Otherwise the days field folds into the microsecond delta at
    exactly 24 hours per day.

    Args:
        value: The materialized month/day/nanosecond interval triple.

    Returns:
        `[-]H:MM:SS.ffffff` for a pure day/microsecond delta, or DuckDB's own
        text rendering for a month-carrying value.
    """
    if value.months != 0:
        return _duckdb_interval_text(
            value.months, value.days, value.nanoseconds // 1000
        )
    total_us = value.days * 24 * 3600 * 1_000_000 + value.nanoseconds // 1000
    sign = "-" if total_us < 0 else ""
    total_us = abs(total_us)
    total_seconds, microseconds = divmod(total_us, 1_000_000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{sign}{hours}:{minutes:02d}:{seconds:02d}.{microseconds:06d}"


def _duckdb_interval_text(months: int, days: int, microseconds: int) -> str:
    """Render a month/day/microsecond triple via DuckDB's own INTERVAL->VARCHAR cast.

    The one Python-side encoding step that delegates to DuckDB: a
    month-carrying interval has no fixed microsecond value, so its canonical
    text is DuckDB's own rendering rather than a value this module could
    compute standalone.

    Args:
        months: The interval's calendar-month component.
        days: The interval's calendar-day component.
        microseconds: The interval's microsecond-of-day component.

    Returns:
        DuckDB's text rendering of the assembled INTERVAL value.
    """
    import duckdb

    con = duckdb.connect()
    row = con.execute(
        "SELECT (to_months(?) + to_days(?) + to_microseconds(?))::VARCHAR",
        [months, days, microseconds],
    ).fetchone()
    assert row is not None
    return str(row[0])
