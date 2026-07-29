"""Tests for derivations.record_index.

Materialized against minimal in-process emits via the reader, reusing the
shared record-fixture scaffold in `_fixtures.py` (record_index already sits in
its contract-declared slot on every fixture records table).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fabulexa_forge.derivations.record_index import (
    RECORD_INDEX_COLUMNS,
    build_record_index_at_end_sql,
    build_record_index_at_sql,
)
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import TableNotFoundError

from ._fixtures import _build_emit

_REC_ID = RECORD_INDEX_COLUMNS.index("record_id")
_REC_IDX = RECORD_INDEX_COLUMNS.index("record_index")


def _run_at(
    emit_dir: Path,
    kind: str,
    horizon_ns: int,
    fork_path: str = "trunk",
) -> list[tuple[Any, ...]]:
    """Open the emit and materialize build_record_index_at_sql at horizon_ns."""
    with open_emit(emit_dir) as emit:
        sql = build_record_index_at_sql(emit.sidecar, fork_path, kind, horizon_ns)
        return emit.query(sql, ())


def _run_at_end(
    emit_dir: Path,
    kind: str,
    fork_path: str = "trunk",
) -> list[tuple[Any, ...]]:
    """Open the emit and materialize build_record_index_at_end_sql."""
    with open_emit(emit_dir) as emit:
        sql = build_record_index_at_end_sql(emit.sidecar, fork_path, kind)
        return emit.query(sql, ())


# A row shape matching _RECORD_COLS: (fork_path, record_id, created_sim_time,
# active, deactivated_at, last_mutation_sim_time, record_index, prop__status,
# prop__score).


class TestHorizonMembership:
    """Only records created strictly before the horizon appear."""

    def test_created_before_horizon_present(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "5")],
            history_rows=[],
        )
        rows = _run_at(emit_dir, "item", horizon_ns=20)
        assert len(rows) == 1
        assert rows[0][_REC_ID] == "r1"
        assert rows[0][_REC_IDX] == 0

    def test_created_at_horizon_absent(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 20, True, None, 20, 0, "a", "5")],
            history_rows=[],
        )
        rows = _run_at(emit_dir, "item", horizon_ns=20)
        assert rows == []

    def test_created_after_horizon_absent(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 30, True, None, 30, 0, "a", "5")],
            history_rows=[],
        )
        rows = _run_at(emit_dir, "item", horizon_ns=20)
        assert rows == []


class TestDeactivatedIgnored:
    """`active` is never a predicate — a deactivated record is still present."""

    def test_deactivated_before_horizon_present(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 5, False, 8, 8, 0, "a", "5")],
            history_rows=[],
        )
        rows = _run_at(emit_dir, "item", horizon_ns=20)
        assert len(rows) == 1
        assert rows[0][_REC_ID] == "r1"


class TestVerbatimProjection:
    """Pairs are projected verbatim; surviving indexes are the creation-order
    prefix 0..n-1."""

    def test_pairs_match_records_table(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r0", 10, True, None, 10, 0, "a", "1"),
                ("trunk", "r1", 20, True, None, 20, 1, "a", "2"),
                ("trunk", "r2", 30, True, None, 30, 2, "a", "3"),
            ],
            history_rows=[],
        )
        rows = _run_at(emit_dir, "item", horizon_ns=100)
        pairs = {(r[_REC_ID], r[_REC_IDX]) for r in rows}
        assert pairs == {("r0", 0), ("r1", 1), ("r2", 2)}
        indexes = sorted(r[_REC_IDX] for r in rows)
        assert indexes == [0, 1, 2]


class TestDistinctCollapse:
    """A duplicated row carrying the identical pair yields one relation row."""

    def test_duplicate_row_collapses(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r1", 10, True, None, 10, 0, "a", "1"),
                ("trunk", "r1", 10, True, None, 10, 0, "a", "1"),
            ],
            history_rows=[],
        )
        rows = _run_at(emit_dir, "item", horizon_ns=20)
        assert len(rows) == 1
        assert rows[0][_REC_ID] == "r1"
        assert rows[0][_REC_IDX] == 0


class TestForkPathFilter:
    """The relation filters to fork_path."""

    def test_other_fork_path_excluded(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r1", 10, True, None, 10, 0, "a", "1"),
                ("other", "r2", 10, True, None, 10, 1, "a", "2"),
            ],
            history_rows=[],
        )
        rows = _run_at(emit_dir, "item", horizon_ns=20, fork_path="trunk")
        assert len(rows) == 1
        assert rows[0][_REC_ID] == "r1"


class TestNoOrderBy:
    """The SQL declares no ORDER BY — a join relation, not an ordered fold."""

    def test_at_sql_has_no_order_by(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "1")],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            sql = build_record_index_at_sql(emit.sidecar, "trunk", "item", 20)
        assert "ORDER BY" not in sql.upper()

    def test_at_end_sql_has_no_order_by(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "1")],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            sql = build_record_index_at_end_sql(emit.sidecar, "trunk", "item")
        assert "ORDER BY" not in sql.upper()


class TestEndOfTape:
    """Every record of the kind; no horizon predicate; equivalent to the
    horizoned builder at a horizon strictly beyond every creation instant."""

    def test_every_record_present(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r0", 10, True, None, 10, 0, "a", "1"),
                ("trunk", "r1", 20, True, None, 20, 1, "a", "2"),
            ],
            history_rows=[],
        )
        rows = _run_at_end(emit_dir, "item")
        pairs = {(r[_REC_ID], r[_REC_IDX]) for r in rows}
        assert pairs == {("r0", 0), ("r1", 1)}

    def test_no_horizon_predicate_in_sql(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "1")],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            sql = build_record_index_at_end_sql(emit.sidecar, "trunk", "item")
        assert "created_sim_time" not in sql

    def test_equivalent_to_horizon_beyond_every_creation(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r0", 10, True, None, 10, 0, "a", "1"),
                ("trunk", "r1", 20, True, None, 20, 1, "a", "2"),
            ],
            history_rows=[],
        )
        end_rows = set(_run_at_end(emit_dir, "item"))
        horizoned_rows = set(_run_at(emit_dir, "item", horizon_ns=1_000_000))
        assert end_rows == horizoned_rows


class TestUnknownKind:
    """Unknown kind raises TableNotFoundError from the sidecar lookup."""

    def test_at_sql_unknown_kind(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "1")],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(TableNotFoundError):
                build_record_index_at_sql(emit.sidecar, "trunk", "ghost", 20)

    def test_at_end_sql_unknown_kind(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "1")],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(TableNotFoundError):
                build_record_index_at_end_sql(emit.sidecar, "trunk", "ghost")
