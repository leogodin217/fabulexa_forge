"""Tests for derivations.row_state_events.build_row_state_events_sql.

Materialized against minimal in-process emits via the reader. Tests cover all
conditions from the Phase 2 spec.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _support.sidecar_builder import identity_column as _identity_column

from fabulexa_forge.derivations.row_state_events import (
    EVENT_CLASS_CREATE,
    EVENT_CLASS_DELETE,
    EVENT_CLASS_UPDATE,
    ROW_STATE_EVENT_COLUMNS,
    build_row_state_events_sql,
    resolve_stream_columns,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import TableNotFoundError

from ._fixtures import (
    _RECORD_COLS_INTERLEAVED,
    _RECORD_COLS_INTERLEAVED_WITH_PID,
    _RECORD_COLS_WITH_PID,
    _build_emit,
)


def _run(
    emit_dir: Path,
    kind: str,
    properties: frozenset[str],
    change_scope: frozenset[str] | None = None,
) -> list[tuple[Any, ...]]:
    """Open the emit and materialize the row-state-events SQL.

    change_scope defaults to `properties` (the shipped single-scope
    invocation) when the caller doesn't split scopes.
    """
    with open_emit(emit_dir) as emit:
        sql = build_row_state_events_sql(
            emit.sidecar,
            "trunk",
            kind,
            properties,
            change_scope=properties if change_scope is None else change_scope,
        )
        return emit.query(sql, ())


# ---------------------------------------------------------------------------
# Column index helpers
# ---------------------------------------------------------------------------

_REC_ID = ROW_STATE_EVENT_COLUMNS.index("record_id")
_EVT = ROW_STATE_EVENT_COLUMNS.index("event_sim_time")
_CLS = ROW_STATE_EVENT_COLUMNS.index("event_class")
_OP = ROW_STATE_EVENT_COLUMNS.index("op")
_N_PREFIX = len(ROW_STATE_EVENT_COLUMNS)  # 4


# ---------------------------------------------------------------------------
# Create event tests
# ---------------------------------------------------------------------------


class TestCreateEvents:
    """Genesis 'c' events are always emitted."""

    def test_every_record_gets_create_event(self, tmp_path: Path) -> None:
        """Every record emits exactly one 'c' event regardless of history."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r1", 10, True, None, 10, 0, "a", "5"),
                ("trunk", "r2", 20, True, None, 20, 1, "b", "3"),
            ],
            history_rows=[("trunk", "item", "r1", "status", 10, "a")],
        )
        rows = _run(emit_dir, "item", frozenset())
        create_rows = [r for r in rows if r[_OP] == "c"]
        record_ids = {r[_REC_ID] for r in create_rows}
        assert "r1" in record_ids
        assert "r2" in record_ids

    def test_create_event_at_created_sim_time(self, tmp_path: Path) -> None:
        """The 'c' event time equals the record's created_sim_time."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 42, True, None, 42, 0, "a", "5")],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset())
        creates = [r for r in rows if r[_OP] == "c" and r[_REC_ID] == "r1"]
        assert len(creates) == 1
        assert creates[0][_EVT] == 42

    def test_record_with_no_tracked_props_still_gets_create(
        self, tmp_path: Path
    ) -> None:
        """A record with no history rows still gets a 'c' event."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, None, "1")],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset())
        creates = [r for r in rows if r[_OP] == "c"]
        assert len(creates) == 1

    def test_create_after_image_uses_creation_seed_history(
        self, tmp_path: Path
    ) -> None:
        """The 'c' after-image for a type-2 prop uses history at or before created_sim_time."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 20, 0, "a", "5")],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "seed_value"),
                ("trunk", "item", "r1", "status", 20, "update_value"),
            ],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        creates = [r for r in rows if r[_OP] == "c" and r[_REC_ID] == "r1"]
        assert len(creates) == 1
        # prop__status is at index _N_PREFIX (4)
        assert creates[0][_N_PREFIX] == "seed_value"

    def test_no_separate_update_at_created_sim_time(self, tmp_path: Path) -> None:
        """No 'u' event is spawned at created_sim_time; only 'c' appears there."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 20, 0, "a", "5")],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "seed_value"),
                ("trunk", "item", "r1", "status", 20, "update_value"),
            ],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        at_creation = [r for r in rows if r[_EVT] == 10 and r[_REC_ID] == "r1"]
        # Only one event at created_sim_time — the 'c'
        assert len(at_creation) == 1
        assert at_creation[0][_OP] == "c"


# ---------------------------------------------------------------------------
# Update event tests
# ---------------------------------------------------------------------------


class TestUpdateEvents:
    """Update events from later history change points."""

    def test_one_update_per_later_history_sim_time(self, tmp_path: Path) -> None:
        """Each distinct history sim_time after creation yields one 'u' event."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 30, 0, "c", "5")],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
                ("trunk", "item", "r1", "status", 30, "c"),
            ],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        updates = [r for r in rows if r[_OP] == "u" and r[_REC_ID] == "r1"]
        assert len(updates) == 2
        times = {r[_EVT] for r in updates}
        assert times == {20, 30}

    def test_type2_prop_as_of_lookback(self, tmp_path: Path) -> None:
        """Type-2 prop value is the most-recent history row at or before event_sim_time."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 30, 0, "c", "5")],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "alpha"),
                ("trunk", "item", "r1", "status", 20, "beta"),
            ],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        update_at_20 = [
            r for r in rows if r[_OP] == "u" and r[_REC_ID] == "r1" and r[_EVT] == 20
        ]
        assert len(update_at_20) == 1
        assert update_at_20[0][_N_PREFIX] == "beta"

    def test_type2_prop_null_before_first_history(self, tmp_path: Path) -> None:
        """Type-2 prop is NULL at the create event when no history row is at or before it."""
        # created_sim_time=5 but first history at 10 => create event has NULL prop__status
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 5, True, None, 10, 0, "a", "5")],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "first"),
            ],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        creates = [r for r in rows if r[_OP] == "c" and r[_REC_ID] == "r1"]
        assert len(creates) == 1
        assert creates[0][_N_PREFIX] is None

    def test_type1_prop_constant_across_events(self, tmp_path: Path) -> None:
        """Type-1 (current-value) prop is carried at the record's current value on every event."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 20, 0, "a", "42")],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
            ],
        )
        rows = _run(emit_dir, "item", frozenset({"score"}))
        # score is type-1; find the prop index
        # No tracked props, so prop__score is at _N_PREFIX
        non_delete = [r for r in rows if r[_OP] != "d"]
        for row in non_delete:
            assert row[_N_PREFIX] == "42"

    def test_empty_properties_no_prop_columns(self, tmp_path: Path) -> None:
        """With properties=frozenset(), no prop__ columns are in the output."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "5")],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset())
        # All rows have exactly the prefix columns
        assert all(len(r) == _N_PREFIX for r in rows)


# ---------------------------------------------------------------------------
# Delete event tests
# ---------------------------------------------------------------------------


class TestDeleteEvents:
    """Deactivated records emit a 'd' event; active records do not."""

    def test_deactivated_record_emits_delete(self, tmp_path: Path) -> None:
        """A deactivated record emits a 'd' event at deactivated_at."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, False, 50, 50, 0, "a", "5")],
            history_rows=[("trunk", "item", "r1", "status", 10, "a")],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        deletes = [r for r in rows if r[_OP] == "d" and r[_REC_ID] == "r1"]
        assert len(deletes) == 1
        assert deletes[0][_EVT] == 50

    def test_active_record_no_delete(self, tmp_path: Path) -> None:
        """An active record (deactivated_at NULL) emits no 'd' event."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "5")],
            history_rows=[("trunk", "item", "r1", "status", 10, "a")],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        deletes = [r for r in rows if r[_OP] == "d"]
        assert len(deletes) == 0

    def test_delete_after_image_all_null(self, tmp_path: Path) -> None:
        """The 'd' event's after-image columns are all NULL."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, False, 50, 50, 0, "a", "5")],
            history_rows=[("trunk", "item", "r1", "status", 10, "a")],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        deletes = [r for r in rows if r[_OP] == "d"]
        assert len(deletes) == 1
        # prop__status at index _N_PREFIX should be NULL
        assert deletes[0][_N_PREFIX] is None

    def test_coincident_update_and_delete_ordering(self, tmp_path: Path) -> None:
        """A 'u' at the same sim_time as 'd' orders before 'd' (event_class 1 < 2)."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, False, 50, 50, 0, "b", "5")],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "status", 50, "b"),
            ],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        at_50 = [r for r in rows if r[_EVT] == 50 and r[_REC_ID] == "r1"]
        assert len(at_50) == 2
        # 'u' (event_class=1) before 'd' (event_class=2)
        assert at_50[0][_OP] == "u"
        assert at_50[1][_OP] == "d"


# ---------------------------------------------------------------------------
# Full lifecycle test
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    """A record with c/u/d events across its full life."""

    def test_full_lifecycle_c_u_d(self, tmp_path: Path) -> None:
        """A record with history emits c, u*, d in correct order."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, False, 40, 40, 0, "c", "5")],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
                ("trunk", "item", "r1", "status", 30, "c"),
            ],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        r1_rows = [r for r in rows if r[_REC_ID] == "r1"]
        ops = [r[_OP] for r in r1_rows]
        assert ops[0] == "c"
        assert ops[-1] == "d"
        assert ops.count("u") == 2
        assert ops == ["c", "u", "u", "d"]


# ---------------------------------------------------------------------------
# Ordering tests
# ---------------------------------------------------------------------------


class TestOrdering:
    """Output is ordered by (event_sim_time, event_class, record_id)."""

    def test_ordered_by_event_sim_time_then_event_class_then_record_id(
        self, tmp_path: Path
    ) -> None:
        """Rows are in (event_sim_time, event_class, record_id) order."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[
                ("trunk", "r1", 10, True, None, 10, 0, "a", "1"),
                ("trunk", "r2", 10, True, None, 10, 1, "b", "2"),
            ],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset())
        # Both creates at t=10; r1 before r2
        assert rows[0][_REC_ID] == "r1"
        assert rows[1][_REC_ID] == "r2"

    def test_event_class_ordering_constants(self) -> None:
        """EVENT_CLASS_CREATE < EVENT_CLASS_UPDATE < EVENT_CLASS_DELETE."""
        assert EVENT_CLASS_CREATE == 0
        assert EVENT_CLASS_UPDATE == 1
        assert EVENT_CLASS_DELETE == 2


# ---------------------------------------------------------------------------
# Op recode tests
# ---------------------------------------------------------------------------


class TestOpRecode:
    """event_class 0/1/2 recodes to 'c'/'u'/'d' in SQL."""

    def test_op_recode_create(self, tmp_path: Path) -> None:
        """event_class=0 recodes to op='c'."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "5")],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset())
        creates = [r for r in rows if r[_CLS] == EVENT_CLASS_CREATE]
        assert all(r[_OP] == "c" for r in creates)

    def test_op_recode_update(self, tmp_path: Path) -> None:
        """event_class=1 recodes to op='u'."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 20, 0, "b", "5")],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
            ],
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        updates = [r for r in rows if r[_CLS] == EVENT_CLASS_UPDATE]
        assert all(r[_OP] == "u" for r in updates)

    def test_op_recode_delete(self, tmp_path: Path) -> None:
        """event_class=2 recodes to op='d'."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, False, 50, 50, 0, "a", "5")],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset())
        deletes = [r for r in rows if r[_CLS] == EVENT_CLASS_DELETE]
        assert all(r[_OP] == "d" for r in deletes)


# ---------------------------------------------------------------------------
# presentation_id tests
# ---------------------------------------------------------------------------


class TestPresentationId:
    """presentation_id is appended when the kind carries it; absent otherwise."""

    def test_kind_with_presentation_id_appends_column(self, tmp_path: Path) -> None:
        """A kind carrying presentation_id has it in the output after record_id."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 99, 10, True, None, 10, 0, "a")],
            history_rows=[],
            record_cols=_RECORD_COLS_WITH_PID,
        )
        rows = _run(emit_dir, "item", frozenset())
        assert len(rows) > 0
        # presentation_id is at _N_PREFIX (index 4) for no-prop case
        row = rows[0]
        assert row[_N_PREFIX] == "99"

    def test_kind_without_presentation_id_has_no_column(self, tmp_path: Path) -> None:
        """A kind without presentation_id has no presentation_id column."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "5")],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset())
        # Only 4 columns (the prefix); no presentation_id
        assert all(len(r) == _N_PREFIX for r in rows)

    def test_presentation_id_cast_to_varchar(self, tmp_path: Path) -> None:
        """BIGINT presentation_id is cast to VARCHAR in the output."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 12345, 10, True, None, 10, 0, "a")],
            history_rows=[],
            record_cols=_RECORD_COLS_WITH_PID,
        )
        rows = _run(emit_dir, "item", frozenset())
        row = rows[0]
        assert isinstance(row[_N_PREFIX], str)
        assert row[_N_PREFIX] == "12345"

    def test_presentation_id_null_on_delete(self, tmp_path: Path) -> None:
        """presentation_id is NULL on a 'd' event."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 99, 10, False, 50, 50, 0, "a")],
            history_rows=[],
            record_cols=_RECORD_COLS_WITH_PID,
        )
        rows = _run(emit_dir, "item", frozenset())
        deletes = [r for r in rows if r[_OP] == "d"]
        assert len(deletes) == 1
        assert deletes[0][_N_PREFIX] is None


# ---------------------------------------------------------------------------
# Combined delete-nulling: presentation_id + tracked + current props on one row
# ---------------------------------------------------------------------------


class TestCombinedDeleteNulling:
    """A 'd' row NULLs presentation_id, tracked props, and current props together."""

    def test_delete_nulls_pid_tracked_and_current_props_simultaneously(
        self, tmp_path: Path
    ) -> None:
        """One deactivated record with a surrogate plus tracked (alpha, gamma)
        and current (beta) props: the single 'd' row NULLs all four after-image
        columns at once, while the 'c'/'u' rows carry them all populated."""
        # Cols: fork_path, record_id, presentation_id, created_sim_time, active,
        # deactivated_at, last_mutation_sim_time, prop__alpha, prop__beta, prop__gamma
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 77, 10, False, 50, 30, 0, "a2", "b0", "g1")],
            history_rows=[
                ("trunk", "widget", "r1", "alpha", 10, "a1"),
                ("trunk", "widget", "r1", "alpha", 30, "a2"),
                ("trunk", "widget", "r1", "gamma", 10, "g1"),
            ],
            kind="widget",
            record_cols=_RECORD_COLS_INTERLEAVED_WITH_PID,
        )
        rows = _run(emit_dir, "widget", frozenset({"alpha", "beta", "gamma"}))

        # After-image layout: prefix(4) + presentation_id, prop__alpha,
        # prop__beta, prop__gamma (sidecar declaration order)
        pid_idx, alpha_idx, beta_idx, gamma_idx = 4, 5, 6, 7
        r1_rows = [r for r in rows if r[_REC_ID] == "r1"]
        assert [r[_OP] for r in r1_rows] == ["c", "u", "d"]
        create, update, delete = r1_rows

        # 'c' at 10: all after-image columns populated
        assert create[_EVT] == 10
        assert (create[pid_idx], create[alpha_idx], create[beta_idx]) == (
            "77",
            "a1",
            "b0",
        )
        assert create[gamma_idx] == "g1"

        # 'u' at 30: still all populated, alpha advanced
        assert update[_EVT] == 30
        assert (update[pid_idx], update[alpha_idx], update[beta_idx]) == (
            "77",
            "a2",
            "b0",
        )
        assert update[gamma_idx] == "g1"

        # 'd' at 50: presentation_id, tracked props, and current prop all NULL
        # on the SAME row — record_id remains
        assert delete[_EVT] == 50
        assert delete[_REC_ID] == "r1"
        assert delete[pid_idx] is None
        assert delete[alpha_idx] is None
        assert delete[beta_idx] is None
        assert delete[gamma_idx] is None


# ---------------------------------------------------------------------------
# Column list tests
# ---------------------------------------------------------------------------


class TestColumnList:
    """Output column list matches spec."""

    def test_prefix_columns_are_canonical(self, tmp_path: Path) -> None:
        """ROW_STATE_EVENT_COLUMNS matches the fixed canonical prefix."""
        assert ROW_STATE_EVENT_COLUMNS == (
            "record_id",
            "event_sim_time",
            "event_class",
            "op",
        )

    def test_prop_columns_in_sidecar_order(self, tmp_path: Path) -> None:
        """prop__ columns follow sidecar column-declaration order."""
        # _RECORD_COLS has prop__status before prop__score
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "5")],
            history_rows=[("trunk", "item", "r1", "status", 10, "a")],
        )
        # Both props selected; status is tracked, score is not
        rows = _run(emit_dir, "item", frozenset({"status", "score"}))
        # Row has: prefix(4) + prop__status + prop__score
        assert len(rows[0]) == 6

    def test_history_tracked_false_treated_as_type1(self, tmp_path: Path) -> None:
        """A prop with history_tracked=False is treated as current-value (type-1)."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "99")],
            history_rows=[],
        )
        rows = _run(emit_dir, "item", frozenset({"score"}))
        non_delete = [r for r in rows if r[_OP] != "d"]
        # score is type-1 (history_tracked=False), value is "99"
        for row in non_delete:
            assert row[_N_PREFIX] == "99"

    def test_history_tracked_none_treated_as_type1(self, tmp_path: Path) -> None:
        """A prop with history_tracked=None (absent) is treated as current-value (type-1)."""
        # Build custom cols with None history_tracked
        cols_none_tracked: list[dict[str, object]] = [
            _identity_column("fork_path", "VARCHAR"),
            _identity_column("record_id", "VARCHAR"),
            {"name": "created_sim_time", "type": "BIGINT"},
            {"name": "active", "type": "BOOLEAN"},
            {"name": "deactivated_at", "type": "BIGINT"},
            {"name": "last_mutation_sim_time", "type": "BIGINT"},
            _identity_column("record_index", "BIGINT"),
            {"name": "prop__value", "type": "VARCHAR"},  # no history_tracked key
        ]
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "static_val")],
            history_rows=[],
            record_cols=cols_none_tracked,
        )
        rows = _run(emit_dir, "item", frozenset({"value"}))
        non_delete = [r for r in rows if r[_OP] != "d"]
        for row in non_delete:
            assert row[_N_PREFIX] == "static_val"


# ---------------------------------------------------------------------------
# Error tests
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
                build_row_state_events_sql(
                    emit.sidecar,
                    "trunk",
                    "nonexistent_kind",
                    frozenset(),
                    change_scope=frozenset(),
                )

    def test_unknown_property_raises_export_error(self, tmp_path: Path) -> None:
        """A selected property missing from the kind raises ExportError."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "5")],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="has no prop__bogus column"):
                build_row_state_events_sql(
                    emit.sidecar,
                    "trunk",
                    "item",
                    frozenset({"bogus"}),
                    change_scope=frozenset({"bogus"}),
                )


# ---------------------------------------------------------------------------
# resolve_stream_columns tests
# ---------------------------------------------------------------------------


class TestResolveStreamColumns:
    """resolve_stream_columns returns sidecar-ordered after-image column names."""

    def test_no_surrogate_empty_properties_returns_record_id_only(
        self, tmp_path: Path
    ) -> None:
        """No surrogate + empty properties => ['record_id']."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            result = resolve_stream_columns(emit.sidecar, "item", frozenset())
        assert result == ["record_id"]

    def test_with_surrogate_inserts_presentation_id_second(
        self, tmp_path: Path
    ) -> None:
        """presentation_id is second when the kind carries a surrogate."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[],
            history_rows=[],
            record_cols=_RECORD_COLS_WITH_PID,
        )
        with open_emit(emit_dir) as emit:
            result = resolve_stream_columns(emit.sidecar, "item", frozenset())
        assert result[0] == "record_id"
        assert result[1] == "presentation_id"

    def test_props_in_sidecar_declaration_order_not_tracked_then_current(
        self, tmp_path: Path
    ) -> None:
        """Props appear in sidecar order: alpha(tracked), beta(current), gamma(tracked)."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[],
            history_rows=[],
            record_cols=_RECORD_COLS_INTERLEAVED,
            kind="widget",
        )
        with open_emit(emit_dir) as emit:
            result = resolve_stream_columns(
                emit.sidecar,
                "widget",
                frozenset({"alpha", "beta", "gamma"}),
            )
        assert result == [
            "record_id",
            "prop__alpha",
            "prop__beta",
            "prop__gamma",
        ]

    def test_unknown_kind_raises_table_not_found_error(self, tmp_path: Path) -> None:
        """resolve_stream_columns raises TableNotFoundError for unknown kind."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(TableNotFoundError):
                resolve_stream_columns(emit.sidecar, "ghost", frozenset())

    def test_unknown_property_raises_export_error(self, tmp_path: Path) -> None:
        """resolve_stream_columns raises ExportError for unknown property."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="has no prop__bogus column"):
                resolve_stream_columns(emit.sidecar, "item", frozenset({"bogus"}))


# ---------------------------------------------------------------------------
# Interleaved-prop after-image order test (latent-bug fix)
# ---------------------------------------------------------------------------


class TestInterleavedPropAfterImageOrder:
    """build_row_state_events_sql emits after-image cols in resolve_stream_columns order."""

    def test_after_image_key_order_matches_resolve_stream_columns(
        self, tmp_path: Path
    ) -> None:
        """c/u after-image dict key order equals resolve_stream_columns(...) order."""
        # Interleaved: alpha(tracked), beta(current), gamma(tracked)
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a1", "b1", "g1")],
            history_rows=[
                ("trunk", "widget", "r1", "alpha", 10, "a1"),
                ("trunk", "widget", "r1", "gamma", 10, "g1"),
            ],
            kind="widget",
            record_cols=_RECORD_COLS_INTERLEAVED,
        )
        with open_emit(emit_dir) as emit:
            expected_order = resolve_stream_columns(
                emit.sidecar,
                "widget",
                frozenset({"alpha", "beta", "gamma"}),
            )
            sql = build_row_state_events_sql(
                emit.sidecar,
                "trunk",
                "widget",
                frozenset({"alpha", "beta", "gamma"}),
                change_scope=frozenset({"alpha", "beta", "gamma"}),
            )
            rows = emit.query(sql, ())

        # Find the create event
        creates = [r for r in rows if r[_OP] == "c"]
        assert len(creates) == 1
        row = creates[0]

        # Build after-image dict from fold row (col_names = ROW_STATE_EVENT_COLUMNS + expected_order[1:])
        col_names = list(ROW_STATE_EVENT_COLUMNS) + expected_order[1:]
        after: dict[str, object] = {"record_id": row[_REC_ID]}
        for i in range(4, len(col_names)):
            after[col_names[i]] = row[i]

        # Key order must match resolve_stream_columns order
        assert list(after.keys()) == expected_order


# ---------------------------------------------------------------------------
# Deep single-record history (quadratic-blowup regression)
# ---------------------------------------------------------------------------


class TestDeepSingleRecordHistory:
    """A single record with deep concentrated history reconstructs linearly.

    Regression for the correlated-as-of quadratic: one record concentrating many
    history rows for a tracked property formerly drove an O(events x history) scan
    per record (one resource changing ~9.7k times OOM'd at ~13 GB). The ASOF-join
    reconstruction is a single linear pass; this asserts the deep case still yields
    the correct as-of after-images.
    """

    def test_deep_history_after_images_are_correct(self, tmp_path: Path) -> None:
        """N changes on one record give N-1 updates with the right as-of value each."""
        n = 1000
        # Record created at sim_time 1; one history row per sim_time 1..n, value=str(t).
        history_rows = [
            ("trunk", "item", "r1", "status", t, str(t)) for t in range(1, n + 1)
        ]
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 1, True, None, n, 0, str(n), "5")],
            history_rows=history_rows,
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        r1_rows = [r for r in rows if r[_REC_ID] == "r1"]

        # One 'c' at t=1 plus one 'u' per later distinct history sim_time (2..n).
        creates = [r for r in r1_rows if r[_OP] == "c"]
        updates = [r for r in r1_rows if r[_OP] == "u"]
        assert len(creates) == 1
        assert len(updates) == n - 1

        # Every event's after-image is the value at its own sim_time (exact ASOF hit).
        for r in r1_rows:
            assert r[_N_PREFIX] == str(r[_EVT])

    def test_deep_history_asof_lookback_between_changes(self, tmp_path: Path) -> None:
        """An event between history points reads the most-recent prior value."""
        # History at even sim_times only; a delete at an odd time looks back one step.
        history_rows = [
            ("trunk", "item", "r1", "status", t, str(t)) for t in range(2, 21, 2)
        ]
        emit_dir = _build_emit(
            tmp_path,
            # Deactivated at t=21 (odd) — no history row there; as-of is value at 20.
            record_rows=[("trunk", "r1", 2, False, 21, 20, 0, "20", "5")],
            history_rows=history_rows,
        )
        rows = _run(emit_dir, "item", frozenset({"status"}))
        deletes = [r for r in rows if r[_OP] == "d" and r[_REC_ID] == "r1"]
        assert len(deletes) == 1
        # 'd' after-image is NULL by contract regardless of lookback.
        assert deletes[0][_N_PREFIX] is None
        # The last 'u' at t=20 carries "20"; the one at t=18 carries "18".
        at_20 = [r for r in rows if r[_OP] == "u" and r[_EVT] == 20]
        at_18 = [r for r in rows if r[_OP] == "u" and r[_EVT] == 18]
        assert at_20[0][_N_PREFIX] == "20"
        assert at_18[0][_N_PREFIX] == "18"


# ---------------------------------------------------------------------------
# Two-scope contract: change_scope drives event membership, properties drives
# the after-image projection — independently.
# ---------------------------------------------------------------------------


class TestChangeScopeSplit:
    """change_scope (event membership) and properties (after-image) split."""

    def test_change_scope_equals_properties_is_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """Equal scopes reproduce the shipped single-scope SQL and rows exactly."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 30, 0, "c", "5")],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
                ("trunk", "item", "r1", "status", 30, "c"),
            ],
        )
        properties = frozenset({"status"})
        with open_emit(emit_dir) as emit:
            single_scope_sql = build_row_state_events_sql(
                emit.sidecar,
                "trunk",
                "item",
                properties,
                change_scope=properties,
            )
            split_call_sql = build_row_state_events_sql(
                emit.sidecar,
                "trunk",
                "item",
                properties,
                change_scope=frozenset({"status"}),
            )
        assert single_scope_sql == split_call_sql
        assert _run(emit_dir, "item", properties) == _run(
            emit_dir, "item", properties, change_scope=frozenset({"status"})
        )

    def test_wider_change_scope_fires_u_at_untracked_after_image_columns(
        self, tmp_path: Path
    ) -> None:
        """A tracked column outside `properties` still drives 'u' membership; its
        value never appears — the after-image carries only `properties`."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 30, 0, "a2", "b0", "g1")],
            history_rows=[
                ("trunk", "widget", "r1", "alpha", 20, "a2"),
                ("trunk", "widget", "r1", "gamma", 30, "g1"),
            ],
            kind="widget",
            record_cols=_RECORD_COLS_INTERLEAVED,
        )
        rows = _run(
            emit_dir,
            "widget",
            properties=frozenset({"beta"}),
            change_scope=frozenset({"alpha", "beta", "gamma"}),
        )
        r1_rows = [r for r in rows if r[_REC_ID] == "r1"]
        assert [r[_EVT] for r in r1_rows] == [10, 20, 30]
        assert [r[_OP] for r in r1_rows] == ["c", "u", "u"]
        # Only prop__beta is carried (properties = {"beta"}); no alpha/gamma columns.
        assert all(len(r) == _N_PREFIX + 1 for r in r1_rows)
        assert all(r[_N_PREFIX] == "b0" for r in r1_rows)

    def test_empty_properties_with_nonempty_change_scope_identity_only(
        self, tmp_path: Path
    ) -> None:
        """properties=frozenset() yields the full c/u/d event set from change_scope,
        with an identity-only after-image (no prop__ columns)."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, False, 30, 30, 0, "b", "5")],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "status", 20, "b"),
            ],
        )
        rows = _run(
            emit_dir,
            "item",
            properties=frozenset(),
            change_scope=frozenset({"status"}),
        )
        r1_rows = [r for r in rows if r[_REC_ID] == "r1"]
        assert [r[_OP] for r in r1_rows] == ["c", "u", "d"]
        assert all(len(r) == _N_PREFIX for r in r1_rows)

    def test_current_value_name_in_change_scope_contributes_no_updates(
        self, tmp_path: Path
    ) -> None:
        """A current-value (non-tracked) name in change_scope has no history rows
        and so drives no 'u' events."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "5")],
            history_rows=[],
        )
        rows = _run(
            emit_dir,
            "item",
            properties=frozenset(),
            change_scope=frozenset({"score"}),
        )
        r1_rows = [r for r in rows if r[_REC_ID] == "r1"]
        assert [r[_OP] for r in r1_rows] == ["c"]

    def test_disjoint_scopes_event_set_follows_change_scope_payload_follows_properties(
        self, tmp_path: Path
    ) -> None:
        """change_scope and properties disjoint: 'u' events follow change_scope's
        tracked changes; the after-image carries only properties' (constant)
        current-value column."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 30, 0, "b", "42")],
            history_rows=[
                ("trunk", "item", "r1", "status", 10, "a"),
                ("trunk", "item", "r1", "status", 20, "a2"),
                ("trunk", "item", "r1", "status", 30, "b"),
            ],
        )
        rows = _run(
            emit_dir,
            "item",
            properties=frozenset({"score"}),
            change_scope=frozenset({"status"}),
        )
        r1_rows = [r for r in rows if r[_REC_ID] == "r1"]
        assert [r[_EVT] for r in r1_rows] == [10, 20, 30]
        assert [r[_OP] for r in r1_rows] == ["c", "u", "u"]
        assert all(r[_N_PREFIX] == "42" for r in r1_rows)

    def test_bad_change_scope_name_raises_export_error(self, tmp_path: Path) -> None:
        """A change_scope name with no prop__<name> column raises ExportError
        naming the column."""
        emit_dir = _build_emit(
            tmp_path,
            record_rows=[("trunk", "r1", 10, True, None, 10, 0, "a", "5")],
            history_rows=[],
        )
        with open_emit(emit_dir) as emit:
            with pytest.raises(ExportError, match="has no prop__bogus column"):
                build_row_state_events_sql(
                    emit.sidecar,
                    "trunk",
                    "item",
                    frozenset(),
                    change_scope=frozenset({"bogus"}),
                )
