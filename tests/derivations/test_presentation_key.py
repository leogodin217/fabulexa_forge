"""Tests for derivations.presentation_key.

Materialized against minimal in-process emits via the reader, reusing the
shared record-fixture scaffold in `_fixtures.py`. `presentation_id` renders as
a VARCHAR code (`ALPHA_001`-shaped) here, so this module carries its own
record_cols with a VARCHAR presentation_id column rather than reusing
`_fixtures._RECORD_COLS_WITH_PID`'s BIGINT one; the presentation-id-less
`_RECORD_COLS` default exercises the missing-column refusal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fabulexa_forge.derivations.presentation_key import (
    PRESENTATION_KEY_COLUMNS,
    build_presentation_key_at_end_sql,
    build_presentation_key_at_sql,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import TableNotFoundError

from ._fixtures import _build_emit

_REC_ID = PRESENTATION_KEY_COLUMNS.index("record_id")
_PID = PRESENTATION_KEY_COLUMNS.index("presentation_id")

_PID_RECORD_COLS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "presentation_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {"name": "prop__status", "type": "VARCHAR"},
]


def _build_pid_emit(
    tmp_path: Path,
    record_rows: list[tuple[Any, ...]],
    kind: str = "item",
) -> Path:
    """Build a minimal emit whose records__<kind> table carries presentation_id."""
    return _build_emit(
        tmp_path,
        record_rows=record_rows,
        history_rows=[],
        kind=kind,
        record_cols=_PID_RECORD_COLS,
    )


def _run_at(
    emit_dir: Path,
    kind: str,
    horizon_ns: int,
    fork_path: str = "trunk",
) -> list[tuple[Any, ...]]:
    """Open the emit and materialize build_presentation_key_at_sql at horizon_ns."""
    with open_emit(emit_dir) as emit:
        sql = build_presentation_key_at_sql(emit.sidecar, fork_path, kind, horizon_ns)
        return emit.query(sql, ())


def _run_at_end(
    emit_dir: Path,
    kind: str,
    fork_path: str = "trunk",
) -> list[tuple[Any, ...]]:
    """Open the emit and materialize build_presentation_key_at_end_sql."""
    with open_emit(emit_dir) as emit:
        sql = build_presentation_key_at_end_sql(emit.sidecar, fork_path, kind)
        return emit.query(sql, ())


# A row shape matching _RECORD_COLS_WITH_PID: (fork_path, record_id,
# presentation_id, created_sim_time, active, deactivated_at,
# last_mutation_sim_time, record_index, prop__name).


class TestHorizonMembership:
    """Only records created strictly before the horizon appear."""

    def test_created_before_horizon_present(self, tmp_path: Path) -> None:
        emit_dir = _build_pid_emit(
            tmp_path,
            record_rows=[("trunk", "r1", "ALPHA_001", 10, True, None, 10, 0, "a")],
        )
        rows = _run_at(emit_dir, "item", horizon_ns=20)
        assert len(rows) == 1
        assert rows[0][_REC_ID] == "r1"
        assert rows[0][_PID] == "ALPHA_001"

    def test_created_at_horizon_absent(self, tmp_path: Path) -> None:
        emit_dir = _build_pid_emit(
            tmp_path,
            record_rows=[("trunk", "r1", "ALPHA_001", 20, True, None, 20, 0, "a")],
        )
        rows = _run_at(emit_dir, "item", horizon_ns=20)
        assert rows == []

    def test_created_after_horizon_absent(self, tmp_path: Path) -> None:
        emit_dir = _build_pid_emit(
            tmp_path,
            record_rows=[("trunk", "r1", "ALPHA_001", 30, True, None, 30, 0, "a")],
        )
        rows = _run_at(emit_dir, "item", horizon_ns=20)
        assert rows == []


class TestDeactivatedIgnored:
    """`active` is never a predicate — a deactivated record is still present."""

    def test_deactivated_before_horizon_present(self, tmp_path: Path) -> None:
        emit_dir = _build_pid_emit(
            tmp_path,
            record_rows=[("trunk", "r1", "ALPHA_001", 5, False, 8, 8, 0, "a")],
        )
        rows = _run_at(emit_dir, "item", horizon_ns=20)
        assert len(rows) == 1
        assert rows[0][_REC_ID] == "r1"


class TestVerbatimProjection:
    """Pairs are projected verbatim, including a NULL presentation_id for an
    undeclared population's honest surface value."""

    def test_pairs_match_records_table(self, tmp_path: Path) -> None:
        emit_dir = _build_pid_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r0", "ALPHA_001", 10, True, None, 10, 0, "a"),
                ("trunk", "r1", "ALPHA_002", 20, True, None, 20, 1, "a"),
                ("trunk", "r2", "ALPHA_003", 30, True, None, 30, 2, "a"),
            ],
        )
        rows = _run_at(emit_dir, "item", horizon_ns=100)
        pairs = {(r[_REC_ID], r[_PID]) for r in rows}
        assert pairs == {
            ("r0", "ALPHA_001"),
            ("r1", "ALPHA_002"),
            ("r2", "ALPHA_003"),
        }

    def test_null_presentation_id_projects_verbatim(self, tmp_path: Path) -> None:
        emit_dir = _build_pid_emit(
            tmp_path,
            record_rows=[("trunk", "r1", None, 10, True, None, 10, 0, "a")],
        )
        rows = _run_at(emit_dir, "item", horizon_ns=20)
        assert len(rows) == 1
        assert rows[0][_REC_ID] == "r1"
        assert rows[0][_PID] is None


class TestDistinctCollapse:
    """A duplicated row carrying the identical pair yields one relation row;
    two relation rows with distinct values for one record_id are both kept
    — the guard's problem, not the relation's."""

    def test_duplicate_row_collapses(self, tmp_path: Path) -> None:
        emit_dir = _build_pid_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r1", "ALPHA_001", 10, True, None, 10, 0, "a"),
                ("trunk", "r1", "ALPHA_001", 10, True, None, 10, 0, "a"),
            ],
        )
        rows = _run_at(emit_dir, "item", horizon_ns=20)
        assert len(rows) == 1
        assert rows[0][_REC_ID] == "r1"
        assert rows[0][_PID] == "ALPHA_001"

    def test_mutated_duplicate_row_kept_both(self, tmp_path: Path) -> None:
        emit_dir = _build_pid_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r1", "ALPHA_001", 10, True, None, 10, 0, "a"),
                ("trunk", "r1", "ALPHA_999", 10, True, None, 10, 0, "a"),
            ],
        )
        rows = _run_at(emit_dir, "item", horizon_ns=20)
        assert len(rows) == 2
        pids = {r[_PID] for r in rows}
        assert pids == {"ALPHA_001", "ALPHA_999"}


class TestForkPathFilter:
    """The relation filters to fork_path."""

    def test_other_fork_path_excluded(self, tmp_path: Path) -> None:
        emit_dir = _build_pid_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r1", "ALPHA_001", 10, True, None, 10, 0, "a"),
                ("other", "r2", "ALPHA_002", 10, True, None, 10, 1, "a"),
            ],
        )
        rows = _run_at(emit_dir, "item", horizon_ns=20, fork_path="trunk")
        assert len(rows) == 1
        assert rows[0][_REC_ID] == "r1"


class TestNoOrderBy:
    """The SQL declares no ORDER BY — a join relation, not an ordered fold."""

    def test_at_sql_has_no_order_by(self, tmp_path: Path) -> None:
        emit_dir = _build_pid_emit(
            tmp_path,
            record_rows=[("trunk", "r1", "ALPHA_001", 10, True, None, 10, 0, "a")],
        )
        with open_emit(emit_dir) as emit:
            sql = build_presentation_key_at_sql(emit.sidecar, "trunk", "item", 20)
        assert "ORDER BY" not in sql.upper()

    def test_at_end_sql_has_no_order_by(self, tmp_path: Path) -> None:
        emit_dir = _build_pid_emit(
            tmp_path,
            record_rows=[("trunk", "r1", "ALPHA_001", 10, True, None, 10, 0, "a")],
        )
        with open_emit(emit_dir) as emit:
            sql = build_presentation_key_at_end_sql(emit.sidecar, "trunk", "item")
        assert "ORDER BY" not in sql.upper()


class TestEndOfTape:
    """Every record of the kind; no horizon predicate; equivalent to the
    horizoned builder at a horizon strictly beyond every creation instant."""

    def test_every_record_present(self, tmp_path: Path) -> None:
        emit_dir = _build_pid_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r0", "ALPHA_001", 10, True, None, 10, 0, "a"),
                ("trunk", "r1", "ALPHA_002", 20, True, None, 20, 1, "a"),
            ],
        )
        rows = _run_at_end(emit_dir, "item")
        pairs = {(r[_REC_ID], r[_PID]) for r in rows}
        assert pairs == {("r0", "ALPHA_001"), ("r1", "ALPHA_002")}

    def test_no_horizon_predicate_in_sql(self, tmp_path: Path) -> None:
        emit_dir = _build_pid_emit(
            tmp_path,
            record_rows=[("trunk", "r1", "ALPHA_001", 10, True, None, 10, 0, "a")],
        )
        with open_emit(emit_dir) as emit:
            sql = build_presentation_key_at_end_sql(emit.sidecar, "trunk", "item")
        assert "created_sim_time" not in sql

    def test_equivalent_to_horizon_beyond_every_creation(self, tmp_path: Path) -> None:
        emit_dir = _build_pid_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r0", "ALPHA_001", 10, True, None, 10, 0, "a"),
                ("trunk", "r1", "ALPHA_002", 20, True, None, 20, 1, "a"),
            ],
        )
        end_rows = set(_run_at_end(emit_dir, "item"))
        horizoned_rows = set(_run_at(emit_dir, "item", horizon_ns=1_000_000))
        assert end_rows == horizoned_rows


class TestUnknownKind:
    """Unknown kind raises TableNotFoundError from the sidecar lookup."""

    def test_at_sql_unknown_kind(self, tmp_path: Path) -> None:
        emit_dir = _build_pid_emit(
            tmp_path,
            record_rows=[("trunk", "r1", "ALPHA_001", 10, True, None, 10, 0, "a")],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(TableNotFoundError):
                build_presentation_key_at_sql(emit.sidecar, "trunk", "ghost", 20)

    def test_at_end_sql_unknown_kind(self, tmp_path: Path) -> None:
        emit_dir = _build_pid_emit(
            tmp_path,
            record_rows=[("trunk", "r1", "ALPHA_001", 10, True, None, 10, 0, "a")],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(TableNotFoundError):
                build_presentation_key_at_end_sql(emit.sidecar, "trunk", "ghost")


class TestMissingPresentationIdColumn:
    """A records table without a presentation_id column refuses with ExportError."""

    def test_at_sql_missing_column(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "5")],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError):
                build_presentation_key_at_sql(emit.sidecar, "trunk", "item", 20)

    def test_at_end_sql_missing_column(self, tmp_path: Path) -> None:
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "5")],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError):
                build_presentation_key_at_end_sql(emit.sidecar, "trunk", "item")
