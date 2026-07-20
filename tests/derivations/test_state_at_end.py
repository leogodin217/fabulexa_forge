"""Tests for derivations.state_at.build_state_at_end_sql.

Materialized against minimal in-process emits via the reader. Tests cover all
conditions from the Phase 2 spec.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fabulexa_forge.derivations.state_at import (
    STATE_AT_COLUMNS,
    build_state_at_end_sql,
    build_state_at_sql,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import TableNotFoundError

from ._fixtures import (
    _RECORD_COLS_WITH_PID,
    _build_emit,
)

_REC_ID = STATE_AT_COLUMNS.index("record_id")
_ACTIVE = STATE_AT_COLUMNS.index("active")
_DEACT = STATE_AT_COLUMNS.index("deactivated_at")
_N_PREFIX = len(STATE_AT_COLUMNS)  # 4

#: An horizon strictly beyond every history and lifecycle instant these fixtures use.
_BEYOND_EVERYTHING = 10_000_000


def _run_end(
    emit_dir: Path,
    kind: str,
    properties: frozenset[str],
) -> list[tuple[Any, ...]]:
    """Open the emit and materialize the end-of-tape state SQL."""
    with open_emit(emit_dir) as emit:
        sql = build_state_at_end_sql(emit.sidecar, "trunk", kind, properties)
        return emit.query(sql, ())


def _run_at(
    emit_dir: Path,
    kind: str,
    properties: frozenset[str],
    horizon_ns: int,
) -> list[tuple[Any, ...]]:
    """Open the emit and materialize the horizoned state-at SQL."""
    with open_emit(emit_dir) as emit:
        sql = build_state_at_sql(emit.sidecar, "trunk", kind, properties, horizon_ns)
        return emit.query(sql, ())


# ---------------------------------------------------------------------------
# Equivalence contract
# ---------------------------------------------------------------------------


class TestEquivalence:
    """Equal to build_state_at_sql at a horizon beyond every instant."""

    def test_equals_horizoned_builder_beyond_everything(self, tmp_path: Path) -> None:
        """End-of-tape state matches the horizoned state at a far-future horizon."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r1", 0, True, None, 40, 0, "c", "5"),
                ("trunk", "r2", 10, False, 30, 30, 1, "z", "9"),
            ],
            history_rows=[
                ("trunk", "item", "r1", "status", 0, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
                ("trunk", "item", "r1", "status", 40, "c"),
            ],
        )
        end_rows = _run_end(emit_dir, "item", frozenset({"status", "score"}))
        at_rows = _run_at(
            emit_dir, "item", frozenset({"status", "score"}), _BEYOND_EVERYTHING
        )
        assert end_rows == at_rows


# ---------------------------------------------------------------------------
# Deactivated-after-last-history-event case
# ---------------------------------------------------------------------------


class TestSpineVerbatim:
    """active / deactivated_at come from the spine, not a history-derived bound."""

    def test_deactivated_after_last_history_event_is_inactive(
        self, tmp_path: Path
    ) -> None:
        """A record deactivated after its last history event is inactive at end."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, False, 100, 100, 0, "final", "5")],
            history_rows=[
                ("trunk", "item", "r1", "status", 0, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
            ],
        )
        rows = _run_end(emit_dir, "item", frozenset())
        assert rows[0][_ACTIVE] is False
        assert rows[0][_DEACT] == 100

        # A history-only horizon (the last history instant) would get this wrong:
        # the record still reads active there, since deactivation is after it.
        wrong_rows = _run_at(emit_dir, "item", frozenset(), horizon_ns=21)
        assert wrong_rows[0][_ACTIVE] is True
        assert wrong_rows[0][_DEACT] is None

    def test_active_record_no_deactivated_at(self, tmp_path: Path) -> None:
        """An active record (deactivated_at NULL) reads active end-of-tape."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, True, None, 0, 0, "a", "5")],
            history_rows=[],
        )
        rows = _run_end(emit_dir, "item", frozenset())
        assert rows[0][_ACTIVE] is True
        assert rows[0][_DEACT] is None


# ---------------------------------------------------------------------------
# No horizon predicate: every record, regardless of created_sim_time
# ---------------------------------------------------------------------------


class TestNoHorizonPredicate:
    """No created-time filter: every record of the kind appears."""

    def test_every_record_present_regardless_of_created_time(
        self, tmp_path: Path
    ) -> None:
        """Records created at any sim_time all appear (no membership filter)."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r1", 0, True, None, 0, 0, "a", "5"),
                ("trunk", "r2", 1_000_000, True, None, 1_000_000, 1, "b", "6"),
            ],
            history_rows=[],
        )
        rows = _run_end(emit_dir, "item", frozenset())
        assert sorted(r[_REC_ID] for r in rows) == ["r1", "r2"]


# ---------------------------------------------------------------------------
# Property reconstruction
# ---------------------------------------------------------------------------


class TestPropertyReconstruction:
    """Tracked / untracked property values at the tape's end."""

    def test_tracked_prop_latest_recorded_value(self, tmp_path: Path) -> None:
        """Tracked prop carries the latest recorded history value, no bound."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, True, None, 30, 0, "c", "5")],
            history_rows=[
                ("trunk", "item", "r1", "status", 0, "alpha"),
                ("trunk", "item", "r1", "status", 10, "beta"),
                ("trunk", "item", "r1", "status", 30, "gamma"),
            ],
        )
        rows = _run_end(emit_dir, "item", frozenset({"status"}))
        assert rows[0][_N_PREFIX] == "gamma"

    def test_untracked_prop_current_records_value(self, tmp_path: Path) -> None:
        """Untracked (current-value) prop is the record's current value."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, True, None, 30, 0, "c", "99")],
            history_rows=[],
        )
        rows = _run_end(emit_dir, "item", frozenset({"score"}))
        assert rows[0][_N_PREFIX] == "99"


# ---------------------------------------------------------------------------
# Columns and ordering — exactly as the horizoned builder
# ---------------------------------------------------------------------------


class TestColumnsAndOrdering:
    """Columns + ORDER BY match the horizoned builder exactly."""

    def test_presentation_id_appended_when_carried(self, tmp_path: Path) -> None:
        """presentation_id follows STATE_AT_COLUMNS when the kind carries it."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 99, 0, True, None, 0, 0, "a")],
            history_rows=[],
            record_cols=_RECORD_COLS_WITH_PID,
        )
        rows = _run_end(emit_dir, "item", frozenset())
        assert len(rows[0]) == _N_PREFIX + 1
        assert rows[0][_N_PREFIX] == "99"

    def test_ordered_by_created_sim_time_then_record_id(self, tmp_path: Path) -> None:
        """Rows are in (created_sim_time, record_id) order."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r2", 5, True, None, 5, 0, "b", "2"),
                ("trunk", "r1", 5, True, None, 5, 1, "a", "1"),
                ("trunk", "r0", 0, True, None, 0, 2, "z", "0"),
            ],
            history_rows=[],
        )
        rows = _run_end(emit_dir, "item", frozenset())
        assert [r[_REC_ID] for r in rows] == ["r0", "r1", "r2"]

    def test_empty_properties_identity_and_lifecycle_only(self, tmp_path: Path) -> None:
        """Empty properties yields identity + lifecycle columns only."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, True, None, 0, 0, "a", "5")],
            history_rows=[],
        )
        rows = _run_end(emit_dir, "item", frozenset())
        assert all(len(r) == _N_PREFIX for r in rows)


# ---------------------------------------------------------------------------
# No horizon predicate emitted in the SQL text
# ---------------------------------------------------------------------------


class TestNoHorizonInSql:
    """The emitted SQL carries no horizon literal / predicate at all."""

    def test_sql_has_no_created_sim_time_predicate(self, tmp_path: Path) -> None:
        """The compiled SELECT filters only on fork_path, never created_sim_time."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, True, None, 0, 0, "a", "5")],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            sql = build_state_at_end_sql(emit.sidecar, "trunk", "item", frozenset())
        assert (
            "created_sim_time" not in sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestErrors:
    """Errors for unknown kind and unknown property."""

    def test_unknown_kind_raises_table_not_found_error(self, tmp_path: Path) -> None:
        """records__<kind> not in sidecar raises TableNotFoundError."""
        emit_dir = _build_emit(tmp_path, record_rows=[], history_rows=[])
        with open_emit(emit_dir) as emit:
            with pytest.raises(TableNotFoundError):
                build_state_at_end_sql(
                    emit.sidecar, "trunk", "nonexistent_kind", frozenset()
                )

    def test_unknown_property_raises_export_error(self, tmp_path: Path) -> None:
        """A selected property missing from the kind raises ExportError."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, True, None, 0, 0, "a", "5")],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="has no prop__bogus column"):
                build_state_at_end_sql(
                    emit.sidecar, "trunk", "item", frozenset({"bogus"})
                )


# ---------------------------------------------------------------------------
# Determinism / branch filtering
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Identical SQL across builds; filtered to the single fork_path."""

    def test_identical_sql_for_same_arguments(self, tmp_path: Path) -> None:
        """Two builds with identical arguments produce identical SQL."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, True, None, 0, 0, "a", "5")],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            sql1 = build_state_at_end_sql(
                emit.sidecar, "trunk", "item", frozenset({"status"})
            )
            sql2 = build_state_at_end_sql(
                emit.sidecar, "trunk", "item", frozenset({"status"})
            )
        assert sql1 == sql2

    def test_filtered_to_fork_path(self, tmp_path: Path) -> None:
        """Records on another branch's fork_path are excluded."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r1", 0, True, None, 0, 0, "a", "5"),
                ("other/branch", "r2", 0, True, None, 0, 1, "b", "6"),
            ],
            history_rows=[],
        )
        rows = _run_end(emit_dir, "item", frozenset())
        assert [r[_REC_ID] for r in rows] == ["r1"]
