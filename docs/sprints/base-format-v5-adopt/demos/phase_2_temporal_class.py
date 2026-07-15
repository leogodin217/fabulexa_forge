#!/usr/bin/env python
"""
Demo: The reader models the class.

Builds a sidecar declaring one column of each of the three temporal_class values,
plus a column carrying history_tracked with no paired temporal_class (unpaired), a
column declaring an out-of-enum temporal_class value, and a bare structural column
carrying neither temporal attribute. Shows Sidecar.temporal_class returning each
declared class and raising each of the three TemporalClassUnavailableError cases
(distinct messages), plus ColumnNotFoundError and TableNotFoundError.

Sprint: base-format-v5-adopt
Phase: 2
"""

from __future__ import annotations

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.reader.errors import (
    ColumnNotFoundError,
    TableNotFoundError,
    TemporalClassUnavailableError,
)
from fabulexa_forge.reader.sidecar import Sidecar

_TABLE = "records__patient"

_RAW: dict[str, object] = {
    "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
    "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
    "tables": [
        {
            "name": _TABLE,
            "category": "records",
            "record_kind": "patient",
            "columns": [
                {"name": "record_id", "type": "VARCHAR"},
                {
                    "name": "prop__patient_id",
                    "type": "VARCHAR",
                    "history_tracked": True,
                    "temporal_class": "constant",
                },
                {
                    "name": "prop__status",
                    "type": "VARCHAR",
                    "history_tracked": True,
                    "temporal_class": "tracked",
                },
                {
                    "name": "prop__insurer",
                    "type": "VARCHAR",
                    "history_tracked": False,
                    "temporal_class": "slice_only",
                },
                {
                    "name": "prop__triage_band",
                    "type": "VARCHAR",
                    "history_tracked": True,
                },
                {
                    "name": "prop__bogus",
                    "type": "VARCHAR",
                    "history_tracked": True,
                    "temporal_class": "bogus",
                },
            ],
            "rows": 1,
        }
    ],
}


def _show_declared_classes(sidecar: Sidecar) -> None:
    """The accessor returns each column's declared class verbatim-narrowed."""
    for column_name, expected in (
        ("prop__patient_id", "constant"),
        ("prop__status", "tracked"),
        ("prop__insurer", "slice_only"),
    ):
        found = sidecar.temporal_class(_TABLE, column_name)
        if found != expected:
            raise SystemExit(f"FAILURE: {column_name} expected {expected}, got {found}")
        print(f"{_TABLE}.{column_name} -> temporal_class={found!r}")


def _show_no_temporal_semantics_case(sidecar: Sidecar) -> None:
    """A bare structural column has no temporal semantics to ask about."""
    try:
        sidecar.temporal_class(_TABLE, "record_id")
    except TemporalClassUnavailableError as exc:
        if "C13" in str(exc):
            raise SystemExit(
                "FAILURE: no-temporal-semantics message must not mention C13"
            ) from None
        print(f"record_id -> TemporalClassUnavailableError: {exc}")
    else:
        raise SystemExit("FAILURE: record_id should have raised")


def _show_unpaired_case(sidecar: Sidecar) -> None:
    """A history_tracked column with no temporal_class is C13 non-conformant."""
    try:
        sidecar.temporal_class(_TABLE, "prop__triage_band")
    except TemporalClassUnavailableError as exc:
        message = str(exc)
        if "C13" not in message or "fabulexa-forge validate" not in message:
            raise SystemExit(
                f"FAILURE: unpaired message must cite C13 and validate: {message}"
            ) from None
        print(f"prop__triage_band -> TemporalClassUnavailableError: {exc}")
    else:
        raise SystemExit("FAILURE: prop__triage_band should have raised")


def _show_out_of_enum_case(sidecar: Sidecar) -> None:
    """A column declaring an out-of-enum value names it in the error message."""
    try:
        sidecar.temporal_class(_TABLE, "prop__bogus")
    except TemporalClassUnavailableError as exc:
        if "bogus" not in str(exc):
            raise SystemExit(
                f"FAILURE: out-of-enum message must name 'bogus': {exc}"
            ) from None
        print(f"prop__bogus -> TemporalClassUnavailableError: {exc}")
    else:
        raise SystemExit("FAILURE: prop__bogus should have raised")


def _show_column_and_table_not_found(sidecar: Sidecar) -> None:
    """Unknown column/table raise ColumnNotFoundError/TableNotFoundError."""
    try:
        sidecar.temporal_class(_TABLE, "prop__missing")
    except ColumnNotFoundError as exc:
        print(f"prop__missing -> ColumnNotFoundError: {exc}")
    else:
        raise SystemExit(
            "FAILURE: prop__missing should have raised ColumnNotFoundError"
        )

    try:
        sidecar.temporal_class("records__doctor", "prop__status")
    except TableNotFoundError as exc:
        print(f"records__doctor -> TableNotFoundError: {exc}")
    else:
        raise SystemExit(
            "FAILURE: records__doctor should have raised TableNotFoundError"
        )


def main() -> int:
    sidecar = Sidecar.from_raw(_RAW)
    _show_declared_classes(sidecar)
    _show_no_temporal_semantics_case(sidecar)
    _show_unpaired_case(sidecar)
    _show_out_of_enum_case(sidecar)
    _show_column_and_table_not_found(sidecar)
    print("SUCCESS: Sidecar.temporal_class is the single narrowing point.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
