"""Tests for the `schema_drift` corrupter handler."""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from fabulexa_forge.config.models import SchemaDrift, Target
from fabulexa_forge.corrupters.operations.schema_drift import (
    SchemaDriftCorrupter,
    _cast_column,
)
from fabulexa_forge.corrupters.state import CorruptState
from fabulexa_forge.errors import CorruptValidationError

from .._helpers import column_spec, sidecar, table_spec, working_table

_FORK_PATH = "trunk"
_HANDLER = SchemaDriftCorrupter()


def _patient_spec() -> object:
    return table_spec(
        "records__patient",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("active", "BOOLEAN"),
            column_spec("deactivated_at", "BIGINT"),
            column_spec("prop__name", "VARCHAR", history_tracked=True),
            column_spec("prop__age", "BIGINT", history_tracked=True),
            column_spec("prop__visits", "BIGINT", history_tracked=False),
            column_spec("prop__notes", "VARCHAR", history_tracked=False),
        ),
        record_kind="patient",
    )


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


def _patient_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "fork_path": _FORK_PATH,
        "record_id": "p1",
        "active": True,
        "deactivated_at": None,
        "prop__name": "Alice",
        "prop__age": 42,
        "prop__visits": 3,
        "prop__notes": "hello",
    }
    row.update(overrides)
    return row


def _state(history_rows: list[dict[str, object]] | None = None) -> CorruptState:
    history = working_table(_history_spec(), history_rows or [])
    patients = working_table(_patient_spec(), [_patient_row()])
    return CorruptState(tables={"history": history, "records__patient": patients})


def _apply(state: CorruptState, operation: SchemaDrift, seed: int = 1) -> object:
    return _HANDLER.apply(
        state,
        operation,
        "rule#0",
        random.Random(seed),
        _FORK_PATH,
        sidecar((_patient_spec(),)),
    )


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------


def test_rename_relabels_spec_and_arrow_preserving_type_and_tracked() -> None:
    state = _state()
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__patient"),
        rename_to={"prop__notes": "prop__comments"},
    )
    outcome = _apply(state, op)
    assert outcome.units_selected == 1
    assert outcome.units_affected == 1
    defect = outcome.defects[0]
    assert defect.defect_class == "column_rename"
    assert defect.location.kind == "column"
    assert defect.location.column == "prop__comments"

    new_spec = state.tables["records__patient"].spec
    names = [c.name for c in new_spec.columns]
    assert "prop__comments" in names and "prop__notes" not in names
    renamed = next(c for c in new_spec.columns if c.name == "prop__comments")
    assert renamed.type == "VARCHAR"
    assert renamed.history_tracked is False
    assert "prop__comments" in state.tables["records__patient"].data.schema.names


def test_rename_of_ticked_column_declares_c11() -> None:
    state = _state()
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__patient"),
        rename_to={"prop__name": "prop__full_name"},
    )
    outcome = _apply(state, op)
    assert outcome.defects[0].impact == ("C11",)


def test_rename_of_untracked_column_declares_beyond_c1_c12() -> None:
    state = _state()
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__patient"),
        rename_to={"prop__notes": "prop__comments"},
    )
    outcome = _apply(state, op)
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


# ---------------------------------------------------------------------------
# Drop
# ---------------------------------------------------------------------------


def test_drop_removes_column_from_spec_and_arrow() -> None:
    state = _state()
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__patient"),
        drop=["prop__notes"],
    )
    outcome = _apply(state, op)
    new_spec = state.tables["records__patient"].spec
    assert "prop__notes" not in [c.name for c in new_spec.columns]
    assert "prop__notes" not in state.tables["records__patient"].data.schema.names
    assert outcome.defects[0].location.column == "prop__notes"


def test_drop_of_ticked_column_declares_c11() -> None:
    state = _state()
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__patient"),
        drop=["prop__name"],
    )
    outcome = _apply(state, op)
    assert outcome.defects[0].impact == ("C11",)


def test_drop_of_untracked_column_declares_beyond_c1_c12() -> None:
    state = _state()
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__patient"),
        drop=["prop__visits"],
    )
    outcome = _apply(state, op)
    assert outcome.defects[0].impact == ("beyond-c1-c12",)


# ---------------------------------------------------------------------------
# Retype
# ---------------------------------------------------------------------------


def test_retype_applies_cast_and_updates_spec_type() -> None:
    state = _state()
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__patient"),
        retype_to={"prop__visits": "VARCHAR"},
    )
    _apply(state, op)
    new_spec = state.tables["records__patient"].spec
    retyped = next(c for c in new_spec.columns if c.name == "prop__visits")
    assert retyped.type == "VARCHAR"
    assert state.tables["records__patient"].data.column("prop__visits").to_pylist() == [
        "3"
    ]


def test_retype_tracked_bigint_to_double_breaks_round_trip_declares_c6() -> None:
    state = _state(
        history_rows=[
            {
                "fork_path": _FORK_PATH,
                "kind": "patient",
                "record_id": "p1",
                "property": "age",
                "sim_time": 10,
                "value": "42",
            }
        ]
    )
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__patient"),
        retype_to={"prop__age": "DOUBLE"},
    )
    outcome = _apply(state, op)
    assert outcome.defects[0].impact == ("C6",)


def test_retype_tracked_bigint_to_varchar_round_trips_declares_beyond_c1_c12() -> None:
    state = _state(
        history_rows=[
            {
                "fork_path": _FORK_PATH,
                "kind": "patient",
                "record_id": "p1",
                "property": "age",
                "sim_time": 10,
                "value": "42",
            }
        ]
    )
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__patient"),
        retype_to={"prop__age": "VARCHAR"},
    )
    outcome = _apply(state, op)
    assert outcome.defects[0].impact == ("beyond-c1-c12",)
    assert state.tables["records__patient"].data.column("prop__age").to_pylist() == [
        "42"
    ]


def test_retype_impossible_cast_raises_corrupt_validation_error() -> None:
    state = working_table(
        table_spec(
            "records__patient",
            "records",
            (
                column_spec("fork_path", "VARCHAR"),
                column_spec("record_id", "VARCHAR"),
                column_spec("prop__name", "VARCHAR"),
            ),
            record_kind="patient",
        ),
        [{"fork_path": _FORK_PATH, "record_id": "p1", "prop__name": "not-a-number"}],
    )
    corrupt_state = CorruptState(tables={"records__patient": state})
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__patient"),
        retype_to={"prop__name": "BIGINT"},
    )
    with pytest.raises(CorruptValidationError):
        _apply(corrupt_state, op)


def test_retype_unrecognized_type_rejected_at_config_load() -> None:
    """An unrecognized retype_to type is a config error at model construction
    (the allow-list gate), never reaching the apply-time SQL splice."""
    with pytest.raises(ValidationError):
        SchemaDrift(
            kind="schema_drift",
            target=Target(table="records__patient"),
            retype_to={"prop__notes": "NOTATYPE"},
        )


def test_cast_column_guards_unrecognized_type_before_sql_splice() -> None:
    """_cast_column itself refuses a free-form type string (defense in depth
    behind the config-load allow-list) — an injection payload never reaches
    the SQL text."""
    state = _state()
    data = state.tables["records__patient"].data
    with pytest.raises(CorruptValidationError):
        _cast_column(
            data,
            "prop__notes",
            "INTEGER); ATTACH '/tmp/x.db' AS x; --",
            "records__patient",
        )


def test_cast_column_guards_parameterized_prefix_payload() -> None:
    """A single-statement payload riding the VARCHAR( prefix (closes the CAST
    paren, appends a table function, comments out the rest) is refused by the
    anchored type grammar — it must never reach the SQL text."""
    state = _state()
    data = state.tables["records__patient"].data
    with pytest.raises(CorruptValidationError):
        _cast_column(
            data,
            "prop__notes",
            "VARCHAR(10)) AS x FROM read_csv('/etc/hostname') --",
            "records__patient",
        )


# ---------------------------------------------------------------------------
# Set semantics and collisions
# ---------------------------------------------------------------------------


def test_declaration_order_of_maps_is_irrelevant_to_result() -> None:
    state_a = _state()
    state_b = _state()
    op_a = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__patient"),
        rename_to={"prop__notes": "prop__comments"},
        drop=["prop__visits"],
    )
    op_b = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__patient"),
        drop=["prop__visits"],
        rename_to={"prop__notes": "prop__comments"},
    )
    _apply(state_a, op_a)
    _apply(state_b, op_b)
    names_a = [c.name for c in state_a.tables["records__patient"].spec.columns]
    names_b = [c.name for c in state_b.tables["records__patient"].spec.columns]
    assert names_a == names_b


def test_colliding_rename_targets_raise_at_apply_time() -> None:
    state = _state()
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__patient"),
        rename_to={"prop__notes": "prop__x", "prop__visits": "prop__x"},
    )
    with pytest.raises(CorruptValidationError):
        _apply(state, op)


def test_rename_target_equal_to_surviving_column_raises_at_apply_time() -> None:
    state = _state()
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__patient"),
        rename_to={"prop__notes": "prop__visits"},
    )
    with pytest.raises(CorruptValidationError):
        _apply(state, op)


# ---------------------------------------------------------------------------
# Break locality, determinism
# ---------------------------------------------------------------------------


def test_only_target_table_entry_replaced() -> None:
    state = _state()
    other = state.tables["history"]
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__patient"),
        drop=["prop__notes"],
    )
    _apply(state, op)
    assert state.tables["history"] is other


def test_rerun_with_same_seed_is_identical() -> None:
    state_a = _state()
    state_b = _state()
    op = SchemaDrift(
        kind="schema_drift",
        target=Target(table="records__patient"),
        rename_to={"prop__notes": "prop__comments"},
    )
    outcome_a = _apply(state_a, op, seed=7)
    outcome_b = _apply(state_b, op, seed=7)
    assert outcome_a.defects == outcome_b.defects
