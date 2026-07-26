"""Tests for the records-column taxonomy (`records_column_role`,
`ref_index_sibling`) and the structural-temporal surface
(`structural_instant_columns`, `records_structural_column_is_mutable`).

Pure name classification — no sidecar, no DuckDB, no emit fixtures.
"""

from __future__ import annotations

import pytest

from fabulexa_forge.reader.records_columns import (
    REF_INDEX_PREFIX,
    StructuralInstant,
    records_column_role,
    records_structural_column_is_mutable,
    ref_index_sibling,
    structural_instant_columns,
)

# ---------------------------------------------------------------------------
# records_column_role: positive classification per role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["fork_path", "record_id", "record_index", "ref_index__group", "ref_index__x"],
)
def test_identity_names_classify_identity(name: str) -> None:
    """fork_path, record_id, record_index, and any ref_index__<name> -> identity."""
    assert records_column_role(name) == "identity"


def test_presentation_id_classifies_presentation() -> None:
    """presentation_id -> presentation."""
    assert records_column_role("presentation_id") == "presentation"


@pytest.mark.parametrize(
    "name",
    ["created_sim_time", "active", "deactivated_at", "last_mutation_sim_time"],
)
def test_lifecycle_names_classify_lifecycle(name: str) -> None:
    """All four lifecycle column names -> lifecycle."""
    assert records_column_role(name) == "lifecycle"


@pytest.mark.parametrize("name", ["prop__name", "prop__status", "prop__x"])
def test_prop_prefixed_names_classify_payload(name: str) -> None:
    """prop__<name> -> payload."""
    assert records_column_role(name) == "payload"


# ---------------------------------------------------------------------------
# records_column_role: no-role names -- total, no fuzzy matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "member__entity__id",
        "elem__role_name",
        "kind",
        "property",
        "sim_time",
        "value",
        "",
        "props__x",
        "ref_index_x",
        "ref_index__",
        "prop__",
    ],
)
def test_unclassifiable_names_return_none(name: str) -> None:
    """member__*, history-style names, '', and near-miss prefixes -> None.

    No fuzzy matching: a single-underscore near-miss of `ref_index__` and an
    empty-suffix prefix hit both fail to classify.
    """
    assert records_column_role(name) is None


# ---------------------------------------------------------------------------
# ref_index_sibling
# ---------------------------------------------------------------------------


def test_ref_index_sibling_pairs_with_prop_name() -> None:
    """ref_index_sibling('prop__group') == 'ref_index__group'."""
    assert ref_index_sibling("prop__group") == "ref_index__group"


def test_ref_index_sibling_uses_the_shared_prefix_constant() -> None:
    """The minted sibling name always starts with REF_INDEX_PREFIX."""
    assert ref_index_sibling("prop__anything").startswith(REF_INDEX_PREFIX)


@pytest.mark.parametrize("name", ["record_id", "presentation_id", "props__x", ""])
def test_ref_index_sibling_raises_for_non_prop_name(name: str) -> None:
    """A non-prop__-prefixed name raises ValueError."""
    with pytest.raises(ValueError, match="prop__"):
        ref_index_sibling(name)


# ---------------------------------------------------------------------------
# structural_instant_columns
# ---------------------------------------------------------------------------


def test_structural_instant_columns_records() -> None:
    """records category maps its three lifecycle columns to their instants."""
    assert structural_instant_columns("records") == {
        "created_sim_time": "created",
        "deactivated_at": "closed",
        "last_mutation_sim_time": "last_touched",
    }


def test_structural_instant_columns_fixed() -> None:
    """fixed category maps sim_time to 'changed'."""
    assert structural_instant_columns("fixed") == {"sim_time": "changed"}


def test_structural_instant_columns_membership() -> None:
    """membership category maps joined_sim_time / left_sim_time."""
    assert structural_instant_columns("membership") == {
        "joined_sim_time": "joined",
        "left_sim_time": "left",
    }


def test_structural_instant_columns_unrecognised_category_raises() -> None:
    """An unrecognised category raises ValueError naming the value."""
    with pytest.raises(ValueError, match="bogus"):
        structural_instant_columns("bogus")


def test_structural_instant_columns_vocabulary_totality() -> None:
    """The union of returned instants across the three categories is exactly
    the six-member StructuralInstant vocabulary, each appearing once."""
    import typing

    all_instants = [
        instant
        for category in ("records", "fixed", "membership")
        for instant in structural_instant_columns(category).values()
    ]
    assert sorted(all_instants) == sorted(typing.get_args(StructuralInstant))
    assert len(all_instants) == len(set(all_instants))


# ---------------------------------------------------------------------------
# records_structural_column_is_mutable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["active", "deactivated_at", "last_mutation_sim_time"])
def test_records_structural_column_is_mutable_true(name: str) -> None:
    """active, deactivated_at, last_mutation_sim_time may change after creation."""
    assert records_structural_column_is_mutable(name) is True


@pytest.mark.parametrize(
    "name",
    ["created_sim_time", "fork_path", "record_id", "record_index", "presentation_id"],
)
def test_records_structural_column_is_mutable_false(name: str) -> None:
    """created_sim_time, fork_path, record_id, record_index, presentation_id
    are set once."""
    assert records_structural_column_is_mutable(name) is False


@pytest.mark.parametrize("name", ["prop__status", "ref_index__owner", "sim_time"])
def test_records_structural_column_is_mutable_raises_outside_domain(
    name: str,
) -> None:
    """A prop__, ref_index__, or non-records-structural name raises ValueError."""
    with pytest.raises(ValueError, match=name):
        records_structural_column_is_mutable(name)
