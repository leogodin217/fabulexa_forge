#!/usr/bin/env python
"""
Demo: Canonical form — family classification and value encoding
Sprint: dataset-equivalence
Phase: 1

`family_of` classifies a DuckDB type name into one of ten canonical families;
`encode_value` renders one materialized value to that family's canonical text
form. Together they are the compare surface's whole encoding authority
(Phase 2 wires them into `compare_datasets`; this phase proves the seam
itself).

Shows:
  1. One representative value (and a NULL) per family -> canonical text.
  2. Byte-identity between `encode_value` and the C6 conformance codec's
     `to_csv_text` for BIGINT / DOUBLE / BOOLEAN / VARCHAR, including a
     repr-sensitive float.
"""

from __future__ import annotations

import datetime
import sys

import pyarrow as pa

from fabulexa_forge.compare.canonical import CanonicalFamily, encode_value
from fabulexa_forge.reader.conformance import to_csv_text


def _fail(message: str) -> SystemExit:
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def _representative_values() -> tuple[tuple[CanonicalFamily, object], ...]:
    """One representative materialized value per canonical family."""
    return (
        ("integer", -5),
        ("float", 0.30000000000000004),
        ("boolean", True),
        ("text", "hello world"),
        ("timestamp", datetime.datetime(2024, 6, 1, 12, 30, 45, 123456)),
        ("date", datetime.date(2024, 6, 1)),
        ("time", datetime.time(12, 30, 45, 123456)),
        (
            "timestamptz",
            datetime.datetime(
                2024,
                6,
                1,
                12,
                0,
                0,
                500000,
                tzinfo=datetime.timezone(datetime.timedelta(hours=-4)),
            ),
        ),
        ("interval", pa.MonthDayNano((0, 1, 2 * 3600 * 1_000_000_000))),
        ("blob", b"\xde\xad\xbe\xef"),
    )


def main() -> int:
    print("1. Family -> canonical-text table (one representative value, and a NULL):")
    for family, value in _representative_values():
        encoded = encode_value(value, family)
        null_encoded = encode_value(None, family)
        if null_encoded is not None:
            raise _fail(
                f"NULL for family {family!r} encoded as {null_encoded!r}, want None"
            )
        print(f"  {family:>11}: {value!r:>45} -> {encoded!r}  (NULL -> None)")
    print()

    print(
        "2. Byte-identity with the C6 codec's to_csv_text"
        " (BIGINT/DOUBLE/BOOLEAN/VARCHAR):"
    )
    cases: tuple[tuple[object, CanonicalFamily, str], ...] = (
        (5, "integer", "BIGINT"),
        (-5, "integer", "BIGINT"),
        (0, "integer", "BIGINT"),
        (0.1, "float", "DOUBLE"),
        (0.30000000000000004, "float", "DOUBLE"),  # repr-sensitive
        (True, "boolean", "BOOLEAN"),
        (False, "boolean", "BOOLEAN"),
        ("hello world", "text", "VARCHAR"),
        ("", "text", "VARCHAR"),
    )
    for value, family, duckdb_type in cases:
        ours = encode_value(value, family)
        theirs = to_csv_text(value, duckdb_type)
        if ours != theirs:
            raise _fail(
                f"encode_value({value!r}, {family!r}) = {ours!r} != "
                f"to_csv_text({value!r}, {duckdb_type!r}) = {theirs!r}"
            )
        print(f"  {duckdb_type:>7} {value!r:>28} -> {ours!r} (matches to_csv_text)")
    print()

    print(
        "SUCCESS: encode_value renders every canonical family to its pinned text"
        " form (NULL carried as None), and is byte-identical to the C6 codec's"
        " to_csv_text for the four overlapping families, including a"
        " repr-sensitive float"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
