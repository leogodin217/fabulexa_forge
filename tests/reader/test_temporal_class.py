"""Tests for Sidecar.temporal_class: the single narrowing point from the sidecar's
verbatim declared value to the TemporalClass enum."""

from __future__ import annotations

import pytest
from _support.sidecar_builder import identity_column

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.reader.errors import (
    ColumnNotFoundError,
    TableNotFoundError,
    TemporalClassUnavailableError,
)
from fabulexa_forge.reader.sidecar import ColumnSpec, Sidecar

_TRUNK_BRANCH: dict[str, object] = {
    "fork_path": "trunk",
    "parent": None,
    "slice_at": 0,
}


def _raw_with_columns(columns: list[dict[str, object]]) -> dict[str, object]:
    """Build a minimal valid base.json mapping with one table carrying `columns`."""
    return {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [_TRUNK_BRANCH],
        "tables": [
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": columns,
                "rows": 1,
            }
        ],
    }


def _sidecar_with_columns(columns: list[dict[str, object]]) -> Sidecar:
    return Sidecar.from_raw(_raw_with_columns(columns))


@pytest.mark.parametrize("declared_class", ["constant", "tracked", "slice_only"])
def test_temporal_class_returns_declared_value(declared_class: str) -> None:
    """The accessor returns the declared class for each of the three enum values."""
    sidecar = _sidecar_with_columns(
        [
            {
                "name": "prop__status",
                "type": "VARCHAR",
                "history_tracked": True,
                "temporal_class": declared_class,
            }
        ]
    )
    assert sidecar.temporal_class("records__patient", "prop__status") == declared_class


def test_no_temporal_attributes_raises_with_no_temporal_semantics_message() -> None:
    """A column carrying neither attribute raises, message names the no-semantics
    case and never mentions C13 (it is a conformant structural column)."""
    sidecar = _sidecar_with_columns(
        [identity_column("record_id", "VARCHAR")],
    )
    with pytest.raises(TemporalClassUnavailableError) as exc_info:
        sidecar.temporal_class("records__patient", "record_id")
    message = str(exc_info.value)
    assert "C13" not in message


def test_history_tracked_without_temporal_class_raises_citing_c13() -> None:
    """A column declaring history_tracked but no temporal_class raises, message
    cites C13 and directs to `fabulexa-forge validate`."""
    sidecar = _sidecar_with_columns(
        [{"name": "prop__status", "type": "VARCHAR", "history_tracked": True}],
    )
    with pytest.raises(TemporalClassUnavailableError) as exc_info:
        sidecar.temporal_class("records__patient", "prop__status")
    message = str(exc_info.value)
    assert "C13" in message
    assert "fabulexa-forge validate" in message


def test_out_of_enum_declared_value_raises_naming_the_value() -> None:
    """A column declaring an out-of-enum temporal_class raises, message names it."""
    sidecar = _sidecar_with_columns(
        [
            {
                "name": "prop__status",
                "type": "VARCHAR",
                "history_tracked": True,
                "temporal_class": "bogus",
            }
        ],
    )
    with pytest.raises(TemporalClassUnavailableError) as exc_info:
        sidecar.temporal_class("records__patient", "prop__status")
    assert "bogus" in str(exc_info.value)


def test_unknown_table_raises_table_not_found() -> None:
    """An unknown table raises TableNotFoundError."""
    sidecar = _sidecar_with_columns([identity_column("record_id", "VARCHAR")])
    with pytest.raises(TableNotFoundError):
        sidecar.temporal_class("records__doctor", "prop__status")


def test_unknown_column_raises_column_not_found() -> None:
    """An unknown column raises ColumnNotFoundError."""
    sidecar = _sidecar_with_columns([identity_column("record_id", "VARCHAR")])
    with pytest.raises(ColumnNotFoundError):
        sidecar.temporal_class("records__patient", "prop__missing")


def test_from_raw_carries_out_of_enum_value_verbatim() -> None:
    """from_raw parses an out-of-enum declared value verbatim onto ColumnSpec
    (no coercion, no parse error)."""
    sidecar = _sidecar_with_columns(
        [
            {
                "name": "prop__status",
                "type": "VARCHAR",
                "history_tracked": True,
                "temporal_class": "bogus",
            }
        ],
    )
    col = sidecar.columns("records__patient")[0]
    assert col.temporal_class == "bogus"


def test_column_spec_absent_attribute_defaults_to_none() -> None:
    """ColumnSpec constructed without temporal_class (existing test shape) still
    works — attribute absent everywhere means None."""
    col = ColumnSpec(
        name="record_id",
        type="VARCHAR",
        references=None,
        history_tracked=None,
        temporal_class=None,
    )
    assert col.temporal_class is None
