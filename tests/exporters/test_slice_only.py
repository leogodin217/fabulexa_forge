"""Tests for the shared `slice_only` predicates: the discriminator carve-out
(`is_exempt_discriminator`) and the policy-population predicate
(`is_non_exempt_slice_only`) every policing surface consults.
"""

from __future__ import annotations

import pytest
from _support.sidecar_builder import identity_column

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.exporters.slice_only import (
    is_exempt_discriminator,
    is_non_exempt_slice_only,
    slice_only_refusal_message,
)
from fabulexa_forge.reader.errors import TemporalClassUnavailableError
from fabulexa_forge.reader.sidecar import Sidecar

_TRUNK_BRANCH: dict[str, object] = {
    "fork_path": "trunk",
    "parent": None,
    "slice_at": 0,
}


def _sidecar(
    columns: list[dict[str, object]],
    enum_domains: dict[str, dict[str, list[str]]] | None = None,
) -> Sidecar:
    """Build a minimal Sidecar with one records__actor table.

    Args:
        columns: The table's column entries.
        enum_domains: Optional top-level enum_domains block (subtype_values'
            oracle).

    Returns:
        The parsed Sidecar.
    """
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [_TRUNK_BRANCH],
        "tables": [
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": columns,
                "rows": 1,
            }
        ],
    }
    if enum_domains is not None:
        raw["enum_domains"] = enum_domains
    return Sidecar.from_raw(raw)


# ---------------------------------------------------------------------------
# is_exempt_discriminator
# ---------------------------------------------------------------------------


def test_exempt_iff_discriminator_name_and_subtype_values_non_empty() -> None:
    """prop__<kind>_type with a non-empty subtype_values(kind) is exempt."""
    sidecar = _sidecar(
        [identity_column("record_id", "VARCHAR")],
        enum_domains={"actor": {"actor_type": ["patient", "staff"]}},
    )
    assert is_exempt_discriminator(sidecar, "actor", "prop__actor_type") is True


def test_empty_subtype_values_not_exempt() -> None:
    """A discriminator-shaped name with no declared enum_domains is not exempt."""
    sidecar = _sidecar([identity_column("record_id", "VARCHAR")])
    assert is_exempt_discriminator(sidecar, "actor", "prop__actor_type") is False


def test_non_discriminator_name_not_exempt() -> None:
    """A column name that isn't prop__<kind>_type is never exempt, even with
    subtype_values declared."""
    sidecar = _sidecar(
        [identity_column("record_id", "VARCHAR")],
        enum_domains={"actor": {"actor_type": ["patient", "staff"]}},
    )
    assert is_exempt_discriminator(sidecar, "actor", "prop__name") is False


def test_exempt_discriminator_never_reads_class() -> None:
    """Exemption is mechanical: a discriminator with no temporal attributes at
    all is still exempt (is_exempt_discriminator never consults the class)."""
    sidecar = _sidecar(
        [{"name": "prop__actor_type", "type": "VARCHAR"}],
        enum_domains={"actor": {"actor_type": ["patient", "staff"]}},
    )
    assert is_exempt_discriminator(sidecar, "actor", "prop__actor_type") is True


# ---------------------------------------------------------------------------
# is_non_exempt_slice_only
# ---------------------------------------------------------------------------


def test_non_prop_name_returns_false_with_no_class_read() -> None:
    """A non-prop__ name is outside the population; no class read (the table
    doesn't even declare the column, so a class read would raise)."""
    sidecar = _sidecar([identity_column("record_id", "VARCHAR")])
    assert is_non_exempt_slice_only(sidecar, "actor", "record_id") is False


def test_slice_only_non_exempt_is_true() -> None:
    """A non-discriminator prop__ column declaring slice_only is in the population."""
    sidecar = _sidecar(
        [
            {
                "name": "prop__loyalty_tier",
                "type": "VARCHAR",
                "history_tracked": False,
                "temporal_class": "slice_only",
            }
        ]
    )
    assert is_non_exempt_slice_only(sidecar, "actor", "prop__loyalty_tier") is True


@pytest.mark.parametrize("temporal_class", ["constant", "tracked"])
def test_constant_and_tracked_are_false(temporal_class: str) -> None:
    """Only slice_only columns are in the population."""
    sidecar = _sidecar(
        [
            {
                "name": "prop__status",
                "type": "VARCHAR",
                "history_tracked": temporal_class == "tracked",
                "temporal_class": temporal_class,
            }
        ]
    )
    assert is_non_exempt_slice_only(sidecar, "actor", "prop__status") is False


def test_exempt_discriminator_short_circuits_without_class_read() -> None:
    """An exempt discriminator with no temporal attributes at all still returns
    False (exemption short-circuits before any class read)."""
    sidecar = _sidecar(
        [{"name": "prop__actor_type", "type": "VARCHAR"}],
        enum_domains={"actor": {"actor_type": ["patient", "staff"]}},
    )
    assert is_non_exempt_slice_only(sidecar, "actor", "prop__actor_type") is False


def test_non_subtyped_kinds_discriminator_is_not_exempt() -> None:
    """A non-sub-typed kind's prop__<kind>_type marked slice_only is refused like
    any other column — the carve-out requires subtype_values non-empty."""
    sidecar = _sidecar(
        [
            {
                "name": "prop__actor_type",
                "type": "VARCHAR",
                "history_tracked": False,
                "temporal_class": "slice_only",
            }
        ]
    )
    assert is_non_exempt_slice_only(sidecar, "actor", "prop__actor_type") is True


def test_missing_pair_propagates_temporal_class_unavailable() -> None:
    """A prop__ column carrying no temporal attributes at all raises rather than
    being inferred as any class."""
    sidecar = _sidecar([{"name": "prop__loyalty_tier", "type": "VARCHAR"}])
    with pytest.raises(TemporalClassUnavailableError):
        is_non_exempt_slice_only(sidecar, "actor", "prop__loyalty_tier")


# ---------------------------------------------------------------------------
# slice_only_refusal_message
# ---------------------------------------------------------------------------


def test_refusal_message_names_table_column_class_and_contract_fact() -> None:
    """The rendered message names the output table.column, the base
    table.column, the class, and the slice-fact contract clause."""
    message = slice_only_refusal_message(
        "dim_actor", "tier", "column", "actor", "prop__loyalty_tier"
    )
    assert "dim_actor" in message
    assert "tier" in message
    assert "records__actor.prop__loyalty_tier" in message
    assert "temporal_class: slice_only" in message
    assert "known only at the emit's slice" in message
