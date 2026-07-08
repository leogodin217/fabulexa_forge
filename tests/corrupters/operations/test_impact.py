"""Tests for the family-C impact oracle and series enumeration (`_impact.py`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pyarrow as pa
import pytest

from fabulexa_export.corrupters.operations._impact import (
    enumerate_series_units,
    membership_kind_id_pairs,
    resolve_c6_anchor,
    series_round_trip_fails,
)
from fabulexa_export.corrupters.state import CorruptState, WorkingTable
from fabulexa_export.errors import CorruptError

from .._helpers import column_spec, table_spec, working_table

_FORK_PATH = "trunk"
_SLICE_AT = 100


def _history_spec() -> object:
    return table_spec(
        "history",
        "fixed",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("kind", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("property", "VARCHAR"),
            column_spec("sim_time", "BIGINT"),
            column_spec("value", "VARCHAR"),
        ),
    )


def _history_table(rows: Sequence[Mapping[str, object]]) -> pa.Table:
    return working_table(_history_spec(), rows).data


def _series_row(
    sim_time: int,
    value: str,
    *,
    fork_path: str = _FORK_PATH,
    kind: str = "actor",
    record_id: str = "a001",
    property_: str = "status",
) -> dict[str, object]:
    return {
        "fork_path": fork_path,
        "kind": kind,
        "record_id": record_id,
        "property": property_,
        "sim_time": sim_time,
        "value": value,
    }


# ---------------------------------------------------------------------------
# resolve_c6_anchor
# ---------------------------------------------------------------------------


class TestResolveC6Anchor:
    def test_rank_one_under_sim_time_desc_value_desc(self) -> None:
        history = _history_table(
            [_series_row(10, "a"), _series_row(30, "b"), _series_row(20, "c")]
        )
        anchor = resolve_c6_anchor(
            history, _FORK_PATH, _SLICE_AT, "actor", "status", "a001"
        )
        assert anchor == (30, "b")

    def test_differing_value_duplicate_tick_resolves_via_value_desc(self) -> None:
        history = _history_table([_series_row(30, "a"), _series_row(30, "z")])
        anchor = resolve_c6_anchor(
            history, _FORK_PATH, _SLICE_AT, "actor", "status", "a001"
        )
        assert anchor == (30, "z")

    def test_byte_identical_duplicates_resolve_to_one_pair(self) -> None:
        history = _history_table([_series_row(30, "b"), _series_row(30, "b")])
        anchor = resolve_c6_anchor(
            history, _FORK_PATH, _SLICE_AT, "actor", "status", "a001"
        )
        assert anchor == (30, "b")

    def test_rows_past_slice_at_excluded(self) -> None:
        history = _history_table([_series_row(30, "b"), _series_row(150, "future")])
        anchor = resolve_c6_anchor(
            history, _FORK_PATH, _SLICE_AT, "actor", "status", "a001"
        )
        assert anchor == (30, "b")

    def test_all_rows_past_slice_at_is_empty_view(self) -> None:
        history = _history_table([_series_row(150, "future")])
        anchor = resolve_c6_anchor(
            history, _FORK_PATH, _SLICE_AT, "actor", "status", "a001"
        )
        assert anchor is None

    def test_no_series_rows_is_empty_view(self) -> None:
        history = _history_table([])
        anchor = resolve_c6_anchor(
            history, _FORK_PATH, _SLICE_AT, "actor", "status", "a001"
        )
        assert anchor is None

    def test_other_series_and_fork_path_ignored(self) -> None:
        history = _history_table(
            [
                _series_row(30, "b"),
                _series_row(90, "other-property", property_="name"),
                _series_row(90, "other-record", record_id="a002"),
                _series_row(90, "other-kind", kind="doctor"),
                _series_row(90, "other-fork", fork_path="other"),
            ]
        )
        anchor = resolve_c6_anchor(
            history, _FORK_PATH, _SLICE_AT, "actor", "status", "a001"
        )
        assert anchor == (30, "b")

    def test_missing_contract_pinned_column_raises(self) -> None:
        history = pa.table(
            {
                "fork_path": pa.array(["trunk"], type=pa.string()),
                "kind": pa.array(["actor"], type=pa.string()),
                "record_id": pa.array(["a001"], type=pa.string()),
                "property": pa.array(["status"], type=pa.string()),
                "sim_time": pa.array([10], type=pa.int64()),
                # "value" column deliberately omitted
            }
        )
        with pytest.raises(CorruptError):
            resolve_c6_anchor(history, _FORK_PATH, _SLICE_AT, "actor", "status", "a001")


# ---------------------------------------------------------------------------
# series_round_trip_fails
# ---------------------------------------------------------------------------


def _records_actor_spec(
    *, prop_status_present: bool = True, prop_status_type: str = "VARCHAR"
) -> object:
    columns = [
        column_spec("fork_path", "VARCHAR"),
        column_spec("record_id", "VARCHAR"),
    ]
    if prop_status_present:
        columns.append(column_spec("prop__status", prop_status_type))
    return table_spec("records__actor", "records", tuple(columns), record_kind="actor")


def _state(
    history_rows: Sequence[Mapping[str, object]],
    *,
    records_spec: object | None = _records_actor_spec(),
    records_rows: Sequence[Mapping[str, object]] | None = None,
    include_history: bool = True,
) -> CorruptState:
    tables: dict[str, WorkingTable] = {}
    if include_history:
        tables["history"] = working_table(_history_spec(), history_rows)
    if records_spec is not None:
        tables["records__actor"] = working_table(records_spec, records_rows or [])
    return CorruptState(tables=tables)


class TestSeriesRoundTripFails:
    def test_empty_c6_view_cannot_fail(self) -> None:
        state = _state([], records_rows=[{"fork_path": "trunk", "record_id": "a001"}])
        assert (
            series_round_trip_fails(
                state, _FORK_PATH, _SLICE_AT, "actor", "status", "a001"
            )
            is False
        )

    def test_records_table_absent_cannot_fail(self) -> None:
        state = _state([_series_row(10, "active")], records_spec=None)
        assert (
            series_round_trip_fails(
                state, _FORK_PATH, _SLICE_AT, "actor", "status", "a001"
            )
            is False
        )

    def test_prop_column_absent_from_working_spec_cannot_fail(self) -> None:
        state = _state(
            [_series_row(10, "active")],
            records_spec=_records_actor_spec(prop_status_present=False),
            records_rows=[{"fork_path": "trunk", "record_id": "a001"}],
        )
        assert (
            series_round_trip_fails(
                state, _FORK_PATH, _SLICE_AT, "actor", "status", "a001"
            )
            is False
        )

    def test_non_round_trippable_type_cannot_fail(self) -> None:
        state = _state(
            [_series_row(10, "active")],
            records_spec=_records_actor_spec(prop_status_type="TIMESTAMP"),
            records_rows=[
                {"fork_path": "trunk", "record_id": "a001", "prop__status": "active"}
            ],
        )
        assert (
            series_round_trip_fails(
                state, _FORK_PATH, _SLICE_AT, "actor", "status", "a001"
            )
            is False
        )

    def test_missing_records_row_fails(self) -> None:
        state = _state([_series_row(10, "active")], records_rows=[])
        assert (
            series_round_trip_fails(
                state, _FORK_PATH, _SLICE_AT, "actor", "status", "a001"
            )
            is True
        )

    def test_null_records_cell_fails(self) -> None:
        state = _state(
            [_series_row(10, "active")],
            records_rows=[
                {"fork_path": "trunk", "record_id": "a001", "prop__status": None}
            ],
        )
        assert (
            series_round_trip_fails(
                state, _FORK_PATH, _SLICE_AT, "actor", "status", "a001"
            )
            is True
        )

    def test_mismatched_value_fails(self) -> None:
        state = _state(
            [_series_row(10, "active")],
            records_rows=[
                {"fork_path": "trunk", "record_id": "a001", "prop__status": "pending"}
            ],
        )
        assert (
            series_round_trip_fails(
                state, _FORK_PATH, _SLICE_AT, "actor", "status", "a001"
            )
            is True
        )

    def test_matching_value_passes(self) -> None:
        state = _state(
            [_series_row(10, "active")],
            records_rows=[
                {"fork_path": "trunk", "record_id": "a001", "prop__status": "active"}
            ],
        )
        assert (
            series_round_trip_fails(
                state, _FORK_PATH, _SLICE_AT, "actor", "status", "a001"
            )
            is False
        )

    def test_bigint_cell_encodes_through_same_codec(self) -> None:
        spec = table_spec(
            "records__actor",
            "records",
            (
                column_spec("fork_path", "VARCHAR"),
                column_spec("record_id", "VARCHAR"),
                column_spec("prop__count", "BIGINT"),
            ),
            record_kind="actor",
        )
        state = _state(
            [_series_row(10, "42", property_="count")],
            records_spec=spec,
            records_rows=[
                {"fork_path": "trunk", "record_id": "a001", "prop__count": 42}
            ],
        )
        assert (
            series_round_trip_fails(
                state, _FORK_PATH, _SLICE_AT, "actor", "count", "a001"
            )
            is False
        )

    def test_double_cell_encodes_via_repr_float(self) -> None:
        spec = table_spec(
            "records__actor",
            "records",
            (
                column_spec("fork_path", "VARCHAR"),
                column_spec("record_id", "VARCHAR"),
                column_spec("prop__score", "DOUBLE"),
            ),
            record_kind="actor",
        )
        state = _state(
            [_series_row(10, repr(3.14), property_="score")],
            records_spec=spec,
            records_rows=[
                {"fork_path": "trunk", "record_id": "a001", "prop__score": 3.14}
            ],
        )
        assert (
            series_round_trip_fails(
                state, _FORK_PATH, _SLICE_AT, "actor", "score", "a001"
            )
            is False
        )

    def test_boolean_cell_encodes_lowercase(self) -> None:
        spec = table_spec(
            "records__actor",
            "records",
            (
                column_spec("fork_path", "VARCHAR"),
                column_spec("record_id", "VARCHAR"),
                column_spec("prop__flag", "BOOLEAN"),
            ),
            record_kind="actor",
        )
        state = _state(
            [_series_row(10, "true", property_="flag")],
            records_spec=spec,
            records_rows=[
                {"fork_path": "trunk", "record_id": "a001", "prop__flag": True}
            ],
        )
        assert (
            series_round_trip_fails(
                state, _FORK_PATH, _SLICE_AT, "actor", "flag", "a001"
            )
            is False
        )

    def test_working_history_table_absent_raises(self) -> None:
        state = _state([], include_history=False)
        with pytest.raises(CorruptError):
            series_round_trip_fails(
                state, _FORK_PATH, _SLICE_AT, "actor", "status", "a001"
            )


# ---------------------------------------------------------------------------
# enumerate_series_units
# ---------------------------------------------------------------------------


class TestEnumerateSeriesUnits:
    def test_lexicographic_order(self) -> None:
        timeline = _history_table(
            [
                _series_row(10, "a", record_id="a002", property_="status"),
                _series_row(20, "b", record_id="a002", property_="status"),
                _series_row(10, "a", record_id="a001", property_="name"),
                _series_row(20, "b", record_id="a001", property_="name"),
                _series_row(10, "a", record_id="a001", property_="status"),
                _series_row(20, "b", record_id="a001", property_="status"),
            ]
        )
        units = enumerate_series_units(timeline, timeline, _FORK_PATH)
        assert units == (
            ("actor", "a001", "name"),
            ("actor", "a001", "status"),
            ("actor", "a002", "status"),
        )

    def test_single_event_series_excluded(self) -> None:
        timeline = _history_table(
            [
                _series_row(10, "a", record_id="a001", property_="name"),
                _series_row(10, "a", record_id="a002", property_="status"),
                _series_row(20, "b", record_id="a002", property_="status"),
            ]
        )
        units = enumerate_series_units(timeline, timeline, _FORK_PATH)
        assert units == (("actor", "a002", "status"),)

    def test_population_narrowing_membership_timeline_full(self) -> None:
        timeline = _history_table(
            [
                _series_row(10, "a", record_id="a001", property_="status"),
                _series_row(20, "b", record_id="a001", property_="status"),
            ]
        )
        # `where` narrows the population to just one row of the series;
        # membership still holds because the timeline (not the population)
        # decides event count.
        population = _history_table(
            [_series_row(10, "a", record_id="a001", property_="status")]
        )
        units = enumerate_series_units(population, timeline, _FORK_PATH)
        assert units == (("actor", "a001", "status"),)

    def test_timeline_source_other_fork_path_rows_not_counted(self) -> None:
        """A same-triple row on a different fork_path must not count toward
        the series' timeline-count qualification threshold -- without the
        fork_path skip, this two-row (one per fork_path) series would
        wrongly qualify despite carrying only one event on `_FORK_PATH`."""
        timeline = _history_table(
            [
                _series_row(10, "a", record_id="a001", property_="status"),
                _series_row(
                    20, "b", record_id="a001", property_="status", fork_path="other"
                ),
            ]
        )
        population = _history_table(
            [_series_row(10, "a", record_id="a001", property_="status")]
        )
        units = enumerate_series_units(population, timeline, _FORK_PATH)
        assert units == ()

    def test_empty_when_nothing_qualifies(self) -> None:
        timeline = _history_table([])
        units = enumerate_series_units(timeline, timeline, _FORK_PATH)
        assert units == ()

    def test_missing_history_column_raises(self) -> None:
        malformed = pa.table(
            {
                "fork_path": pa.array(["trunk"], type=pa.string()),
                "kind": pa.array(["actor"], type=pa.string()),
                "record_id": pa.array(["a001"], type=pa.string()),
                "sim_time": pa.array([10], type=pa.int64()),
                "value": pa.array(["a"], type=pa.string()),
                # "property" column deliberately omitted
            }
        )
        with pytest.raises(CorruptError):
            enumerate_series_units(malformed, malformed, _FORK_PATH)


# ---------------------------------------------------------------------------
# membership_kind_id_pairs
# ---------------------------------------------------------------------------


def _ward_spec() -> object:
    return table_spec(
        "membership__patient__ward",
        "membership",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("joined_sim_time", "BIGINT"),
            column_spec("member__consultant__kind", "VARCHAR"),
            column_spec("member__consultant__id", "VARCHAR"),
        ),
        record_kind="patient",
        property_="ward",
    )


class TestMembershipKindIdPairs:
    def test_collects_non_null_pairs_excludes_null_pairs(self) -> None:
        state = CorruptState(
            tables={
                "membership__patient__ward": working_table(
                    _ward_spec(),
                    [
                        {
                            "fork_path": "trunk",
                            "record_id": "p1",
                            "joined_sim_time": 1,
                            "member__consultant__kind": "doctor",
                            "member__consultant__id": "d1",
                        },
                        {
                            "fork_path": "trunk",
                            "record_id": "p2",
                            "joined_sim_time": 2,
                            "member__consultant__kind": None,
                            "member__consultant__id": None,
                        },
                    ],
                )
            }
        )
        assert membership_kind_id_pairs(state) == frozenset({("doctor", "d1")})

    def test_non_membership_tables_ignored(self) -> None:
        records_spec = table_spec(
            "records__doctor",
            "records",
            (
                column_spec("fork_path", "VARCHAR"),
                column_spec("record_id", "VARCHAR"),
            ),
            record_kind="doctor",
        )
        state = CorruptState(
            tables={
                "records__doctor": working_table(
                    records_spec, [{"fork_path": "trunk", "record_id": "d1"}]
                )
            }
        )
        assert membership_kind_id_pairs(state) == frozenset()

    def test_kind_column_without_partner_id_column_is_skipped(self) -> None:
        """A `member__<f>__kind` column whose partner `__id` column is absent
        from the table's current schema -- an engine-invariant edge no
        contract-conformant emit produces -- must not raise; the traversal
        simply contributes no pair for it."""
        malformed_spec = table_spec(
            "membership__patient__ward",
            "membership",
            (
                column_spec("fork_path", "VARCHAR"),
                column_spec("record_id", "VARCHAR"),
                column_spec("member__consultant__kind", "VARCHAR"),
                # member__consultant__id deliberately omitted
            ),
            record_kind="patient",
            property_="ward",
        )
        state = CorruptState(
            tables={
                "membership__patient__ward": working_table(
                    malformed_spec,
                    [
                        {
                            "fork_path": "trunk",
                            "record_id": "p1",
                            "member__consultant__kind": "doctor",
                        }
                    ],
                )
            }
        )
        assert membership_kind_id_pairs(state) == frozenset()
