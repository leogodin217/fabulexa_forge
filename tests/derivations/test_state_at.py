"""Tests for derivations.state_at.build_state_at_sql.

Materialized against minimal in-process emits via the reader. Tests cover all
conditions from the Phase 2 spec.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fabulexa_export.derivations.state_at import STATE_AT_COLUMNS, build_state_at_sql
from fabulexa_export.errors import ExportError
from fabulexa_export.reader.emit import open_emit
from fabulexa_export.reader.errors import TableNotFoundError

from ._fixtures import (
    _RECORD_COLS_INTERLEAVED,
    _RECORD_COLS_WITH_PID,
    _build_emit,
)


def _run(
    emit_dir: Path,
    kind: str,
    properties: frozenset[str],
    horizon_ns: int,
) -> list[tuple[Any, ...]]:
    """Open the emit and materialize the state-at SQL at horizon_ns."""
    with open_emit(emit_dir) as emit:
        sql = build_state_at_sql(emit.sidecar, "trunk", kind, properties, horizon_ns)
        return emit.query(sql, ())


# ---------------------------------------------------------------------------
# Column index helpers
# ---------------------------------------------------------------------------

_REC_ID = STATE_AT_COLUMNS.index("record_id")
_CREATED = STATE_AT_COLUMNS.index("created_sim_time")
_ACTIVE = STATE_AT_COLUMNS.index("active")
_DEACT = STATE_AT_COLUMNS.index("deactivated_at")
_N_PREFIX = len(STATE_AT_COLUMNS)  # 4


# ---------------------------------------------------------------------------
# Membership tests: created_sim_time < horizon
# ---------------------------------------------------------------------------


class TestMembership:
    """Only records created strictly before the horizon appear."""

    def test_record_created_before_horizon_present(self, tmp_path: Path) -> None:
        """A record created before the horizon is present."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, "a", "5")],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset(), horizon_ns=20)
        assert len(rows) == 1
        assert rows[0][_REC_ID] == "r1"

    def test_record_created_at_horizon_absent(self, tmp_path: Path) -> None:
        """A record created exactly at the horizon is absent (horizon is exclusive)."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 20, True, None, 20, "a", "5")],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset(), horizon_ns=20)
        assert rows == []

    def test_record_created_after_horizon_absent(self, tmp_path: Path) -> None:
        """A record created after the horizon is absent."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 30, True, None, 30, "a", "5")],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset(), horizon_ns=20)
        assert rows == []


# ---------------------------------------------------------------------------
# Horizon exclusivity / window composition
# ---------------------------------------------------------------------------


class TestHorizonExclusivity:
    """The horizon is exclusive; state at window k's end equals windows 0..k."""

    def test_event_exactly_at_horizon_not_reflected(self, tmp_path: Path) -> None:
        """A history event at exactly horizon_ns is not reflected in the as-of value."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 5, True, None, 30, "c", "5")],
            history_rows=[
                ("trunk", "item", "r1", "status", 5, "a"),
                ("trunk", "item", "r1", "status", 30, "b"),
            ],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}), horizon_ns=30)
        assert len(rows) == 1
        # value at sim_time=30 is not reflected since horizon is exclusive
        assert rows[0][_N_PREFIX] == "a"

    def test_state_at_window_end_equals_composed_windows(self, tmp_path: Path) -> None:
        """State at the end of window k equals the state at horizon = window k's end."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, True, None, 40, "c", "5")],
            history_rows=[
                ("trunk", "item", "r1", "status", 0, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
                ("trunk", "item", "r1", "status", 40, "c"),
            ],
        )
        # Two windows [0,20) and [20,40); state at each end reflects only strictly
        # earlier events.
        rows_at_20 = _run(emit_dir, "item", frozenset({"status"}), horizon_ns=20)
        rows_at_40 = _run(emit_dir, "item", frozenset({"status"}), horizon_ns=40)
        assert rows_at_20[0][_N_PREFIX] == "a"
        assert rows_at_40[0][_N_PREFIX] == "b"


# ---------------------------------------------------------------------------
# Property reconstruction
# ---------------------------------------------------------------------------


class TestPropertyReconstruction:
    """Tracked / untracked property as-of reconstruction."""

    def test_tracked_prop_most_recent_value_before_horizon(
        self, tmp_path: Path
    ) -> None:
        """Tracked prop carries the most-recent history value strictly before horizon."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, True, None, 30, "c", "5")],
            history_rows=[
                ("trunk", "item", "r1", "status", 0, "alpha"),
                ("trunk", "item", "r1", "status", 10, "beta"),
                ("trunk", "item", "r1", "status", 30, "gamma"),
            ],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}), horizon_ns=25)
        assert rows[0][_N_PREFIX] == "beta"

    def test_tracked_prop_null_when_no_history_at_or_before(
        self, tmp_path: Path
    ) -> None:
        """Tracked prop is NULL when the kind has no history at or before the horizon."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, True, None, 30, "c", "5")],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "beta"),
            ],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}), horizon_ns=5)
        assert rows[0][_N_PREFIX] is None

    def test_untracked_prop_current_value_temporally_constant(
        self, tmp_path: Path
    ) -> None:
        """Untracked (current-value) prop is the record's current value at every horizon."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, True, None, 30, "c", "99")],
            history_rows=[],
        )
        rows_early = _run(emit_dir, "item", frozenset({"score"}), horizon_ns=5)
        rows_late = _run(emit_dir, "item", frozenset({"score"}), horizon_ns=1000)
        assert rows_early[0][_N_PREFIX] == "99"
        assert rows_late[0][_N_PREFIX] == "99"

    def test_empty_properties_identity_and_lifecycle_only(self, tmp_path: Path) -> None:
        """Empty properties yields identity + lifecycle columns only (no prop__ cols)."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, True, None, 0, "a", "5")],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset(), horizon_ns=10)
        assert all(len(r) == _N_PREFIX for r in rows)


# ---------------------------------------------------------------------------
# Lifecycle horizon-rendering
# ---------------------------------------------------------------------------


class TestLifecycleHorizonRender:
    """active / deactivated_at are horizon-rendered relative to horizon_ns."""

    def test_deactivated_after_horizon_shows_active(self, tmp_path: Path) -> None:
        """A record deactivated after the horizon shows active=true, deactivated_at=NULL."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, False, 50, 50, "a", "5")],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset(), horizon_ns=20)
        assert rows[0][_ACTIVE] is True
        assert rows[0][_DEACT] is None

    def test_deactivated_before_horizon_shows_inactive(self, tmp_path: Path) -> None:
        """A record deactivated before the horizon shows active=false, deactivated_at set."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, False, 50, 50, "a", "5")],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset(), horizon_ns=100)
        assert rows[0][_ACTIVE] is False
        assert rows[0][_DEACT] == 50

    def test_deactivated_exactly_at_horizon_shows_active(self, tmp_path: Path) -> None:
        """A delete event exactly at the horizon is not reflected (horizon is exclusive)."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, False, 50, 50, "a", "5")],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset(), horizon_ns=50)
        assert rows[0][_ACTIVE] is True
        assert rows[0][_DEACT] is None

    def test_record_created_and_deactivated_between_two_horizons(
        self, tmp_path: Path
    ) -> None:
        """A record deactivated between two horizons flips from active to inactive."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, False, 30, 30, "a", "5")],
            history_rows=[],
        )
        rows_early = _run(emit_dir, "item", frozenset(), horizon_ns=10)
        rows_late = _run(emit_dir, "item", frozenset(), horizon_ns=50)
        assert rows_early[0][_ACTIVE] is True
        assert rows_early[0][_DEACT] is None
        assert rows_late[0][_ACTIVE] is False
        assert rows_late[0][_DEACT] == 30

    def test_active_record_no_deactivated_at(self, tmp_path: Path) -> None:
        """An active record (deactivated_at NULL) is always active."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, True, None, 0, "a", "5")],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset(), horizon_ns=1000)
        assert rows[0][_ACTIVE] is True
        assert rows[0][_DEACT] is None


# ---------------------------------------------------------------------------
# Column order
# ---------------------------------------------------------------------------


class TestColumnOrder:
    """Canonical column order: STATE_AT_COLUMNS + presentation_id + props."""

    def test_prefix_columns_are_canonical(self) -> None:
        """STATE_AT_COLUMNS matches the fixed canonical prefix."""
        assert STATE_AT_COLUMNS == (
            "record_id",
            "created_sim_time",
            "active",
            "deactivated_at",
        )

    def test_presentation_id_appended_when_carried(self, tmp_path: Path) -> None:
        """presentation_id follows STATE_AT_COLUMNS when the kind carries it."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 99, 0, True, None, 0, "a")],
            history_rows=[],
            record_cols=_RECORD_COLS_WITH_PID,
        )
        rows = _run(emit_dir, "item", frozenset(), horizon_ns=10)
        assert len(rows[0]) == _N_PREFIX + 1
        assert rows[0][_N_PREFIX] == "99"

    def test_presentation_id_absent_when_not_carried(self, tmp_path: Path) -> None:
        """No presentation_id column when the kind does not carry a surrogate."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, True, None, 0, "a", "5")],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset(), horizon_ns=10)
        assert all(len(r) == _N_PREFIX for r in rows)

    def test_props_in_sidecar_declaration_order(self, tmp_path: Path) -> None:
        """prop__ columns follow sidecar column-declaration order regardless of class."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, True, None, 0, "a1", "b1", "g1")],
            history_rows=[
                ("trunk", "widget", "r1", "alpha", 0, "a1"),
                ("trunk", "widget", "r1", "gamma", 0, "g1"),
            ],
            kind="widget",
            record_cols=_RECORD_COLS_INTERLEAVED,
        )
        rows = _run(
            emit_dir,
            "widget",
            frozenset({"alpha", "beta", "gamma"}),
            horizon_ns=10,
        )
        assert rows[0][_N_PREFIX] == "a1"
        assert rows[0][_N_PREFIX + 1] == "b1"
        assert rows[0][_N_PREFIX + 2] == "g1"


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


class TestOrdering:
    """Output is ordered by (created_sim_time, record_id)."""

    def test_ordered_by_created_sim_time_then_record_id(self, tmp_path: Path) -> None:
        """Rows are in (created_sim_time, record_id) order."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r2", 5, True, None, 5, "b", "2"),
                ("trunk", "r1", 5, True, None, 5, "a", "1"),
                ("trunk", "r0", 0, True, None, 0, "z", "0"),
            ],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset(), horizon_ns=10)
        assert [r[_REC_ID] for r in rows] == ["r0", "r1", "r2"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestErrors:
    """Errors for unknown kind and unknown property."""

    def test_unknown_kind_raises_table_not_found_error(self, tmp_path: Path) -> None:
        """records__<kind> not in sidecar raises TableNotFoundError."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(TableNotFoundError):
                build_state_at_sql(
                    emit.sidecar, "trunk", "nonexistent_kind", frozenset(), 10
                )

    def test_unknown_property_raises_export_error(self, tmp_path: Path) -> None:
        """A selected property missing from the kind raises ExportError."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 0, True, None, 0, "a", "5")],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="has no prop__bogus column"):
                build_state_at_sql(
                    emit.sidecar, "trunk", "item", frozenset({"bogus"}), 10
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
            record_rows=[("trunk", "r1", 0, True, None, 0, "a", "5")],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            sql1 = build_state_at_sql(
                emit.sidecar, "trunk", "item", frozenset({"status"}), 10
            )
            sql2 = build_state_at_sql(
                emit.sidecar, "trunk", "item", frozenset({"status"}), 10
            )
        assert sql1 == sql2

    def test_filtered_to_fork_path(self, tmp_path: Path) -> None:
        """Records on another branch's fork_path are excluded."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r1", 0, True, None, 0, "a", "5"),
                ("other/branch", "r2", 0, True, None, 0, "b", "6"),
            ],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset(), horizon_ns=10)
        assert [r[_REC_ID] for r in rows] == ["r1"]
