"""Tests for the `CORRUPTER_REGISTRY` dispatch table."""

from __future__ import annotations

from typing import get_args

from fabulexa_export.config.models import CorruptOperation
from fabulexa_export.corrupters.operations import CORRUPTER_REGISTRY


def _union_kinds() -> set[str]:
    """Every `kind` literal in the `CorruptOperation` discriminated union."""
    # CorruptOperation is Annotated[Union[...], Field(discriminator="kind")];
    # get_args unwraps Annotated to (Union[...], FieldInfo), then Union to its
    # member models.
    union_type = get_args(CorruptOperation)[0]
    kinds: set[str] = set()
    for model in get_args(union_type):
        (literal_value,) = get_args(model.model_fields["kind"].annotation)
        kinds.add(literal_value)
    return kinds


def test_registry_keys_equal_corrupt_operation_kinds() -> None:
    assert set(CORRUPTER_REGISTRY) == _union_kinds()


def test_registry_values_are_corrupter_implementations() -> None:
    for handler in CORRUPTER_REGISTRY.values():
        assert callable(handler.apply)


def test_delete_rows_is_registered() -> None:
    assert "delete_rows" in CORRUPTER_REGISTRY


def test_insert_rows_is_registered() -> None:
    assert "insert_rows" in CORRUPTER_REGISTRY


def test_distort_intervals_is_registered() -> None:
    assert "distort_intervals" in CORRUPTER_REGISTRY
