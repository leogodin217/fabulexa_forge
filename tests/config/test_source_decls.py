"""Tests for the source declared-table decl models: MembershipRef,
SourceTableDecl, SourceEventSourceDecl, SourceEventsDecl.

Phase 1: these models are standalone (not yet wired into SourceConfig), so
tests construct them directly rather than through ExportConfig / the loader.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fabulexa_forge.config.models import (
    MembershipRef,
    SourceEventsDecl,
    SourceEventSourceDecl,
    SourceTableDecl,
)

# ---------------------------------------------------------------------------
# MembershipRef
# ---------------------------------------------------------------------------


def test_membership_ref_requires_both_fields() -> None:
    """MembershipRef requires both `kind` and `property`."""
    ref = MembershipRef(kind="trip", property="drivers")
    assert ref.kind == "trip"
    assert ref.property == "drivers"

    with pytest.raises(ValidationError):
        MembershipRef.model_validate({"kind": "trip"})
    with pytest.raises(ValidationError):
        MembershipRef.model_validate({"property": "drivers"})
    with pytest.raises(ValidationError):
        MembershipRef.model_validate({})


def test_membership_ref_extra_field_forbidden() -> None:
    """MembershipRef rejects unknown fields (StrictBaseModel)."""
    with pytest.raises(ValidationError):
        MembershipRef.model_validate({"kind": "trip", "property": "drivers", "x": 1})


# ---------------------------------------------------------------------------
# SourceTableDecl: exactly one of kind / membership
# ---------------------------------------------------------------------------


def test_table_decl_with_kind_only() -> None:
    """kind-only table decl parses."""
    decl = SourceTableDecl(name="trips", kind="trip")
    assert decl.kind == "trip"
    assert decl.membership is None


def test_table_decl_with_membership_only() -> None:
    """membership-only table decl parses."""
    decl = SourceTableDecl(
        name="trip_drivers", membership=MembershipRef(kind="trip", property="drivers")
    )
    assert decl.membership is not None
    assert decl.kind is None


def test_table_decl_neither_kind_nor_membership_rejected() -> None:
    """Neither kind nor membership set -> rejected."""
    with pytest.raises(ValidationError, match="exactly one"):
        SourceTableDecl(name="trips")


def test_table_decl_both_kind_and_membership_rejected() -> None:
    """Both kind and membership set -> rejected."""
    with pytest.raises(ValidationError, match="exactly one"):
        SourceTableDecl(
            name="trips",
            kind="trip",
            membership=MembershipRef(kind="trip", property="drivers"),
        )


def test_table_decl_sub_types_only_with_kind() -> None:
    """sub_types on a membership-source table decl -> rejected."""
    with pytest.raises(ValidationError, match="sub_types"):
        SourceTableDecl(
            name="trip_drivers",
            membership=MembershipRef(kind="trip", property="drivers"),
            sub_types=("standard",),
        )


def test_table_decl_sub_types_with_kind_parses() -> None:
    """sub_types alongside kind parses."""
    decl = SourceTableDecl(name="customers", kind="customer", sub_types=("vip",))
    assert decl.sub_types == ("vip",)


# ---------------------------------------------------------------------------
# SourceTableDecl: non-empty / distinct entries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, match",
    [
        pytest.param({"name": "", "kind": "trip"}, "non-empty", id="name_empty"),
        pytest.param(
            {"name": "customers", "kind": "customer", "sub_types": ()},
            "non-empty",
            id="sub_types_empty",
        ),
        pytest.param(
            {"name": "trips", "kind": "trip", "columns": ()},
            "non-empty",
            id="columns_empty",
        ),
        pytest.param(
            {"name": "trips", "kind": "trip", "rename": {}},
            "non-empty",
            id="rename_empty",
        ),
    ],
)
def test_table_decl_field_empty_rejected(kwargs: dict[str, object], match: str) -> None:
    """An empty (but present) collection/name field -> rejected."""
    with pytest.raises(ValidationError, match=match):
        SourceTableDecl(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs, match",
    [
        pytest.param(
            {"name": "customers", "kind": "customer", "sub_types": ("vip", "vip")},
            "distinct",
            id="sub_types_duplicate",
        ),
        pytest.param(
            {"name": "trips", "kind": "trip", "columns": ("prop__fare", "prop__fare")},
            "distinct",
            id="columns_duplicate",
        ),
        pytest.param(
            {
                "name": "trips",
                "kind": "trip",
                "rename": {"prop__fare": "amount", "prop__tip": "amount"},
            },
            "distinct",
            id="rename_values_not_distinct",
        ),
    ],
)
def test_table_decl_field_duplicate_rejected(
    kwargs: dict[str, object], match: str
) -> None:
    """A duplicate entry (or non-distinct rename target) -> rejected."""
    with pytest.raises(ValidationError, match=match):
        SourceTableDecl(**kwargs)  # type: ignore[arg-type]


def test_table_decl_rename_distinct_values_parses() -> None:
    """Distinct rename targets parse."""
    decl = SourceTableDecl(name="trips", kind="trip", rename={"prop__fare": "fare_usd"})
    assert decl.rename == {"prop__fare": "fare_usd"}


def test_table_decl_extra_field_forbidden() -> None:
    """SourceTableDecl rejects unknown fields."""
    with pytest.raises(ValidationError):
        SourceTableDecl.model_validate({"name": "trips", "kind": "trip", "bogus": 1})


# ---------------------------------------------------------------------------
# SourceEventSourceDecl
# ---------------------------------------------------------------------------


def test_event_source_decl_with_kind_only() -> None:
    """kind-only events source parses."""
    decl = SourceEventSourceDecl(kind="trip")
    assert decl.kind == "trip"


def test_event_source_decl_with_membership_only() -> None:
    """membership-only events source parses."""
    decl = SourceEventSourceDecl(
        membership=MembershipRef(kind="trip", property="drivers")
    )
    assert decl.membership is not None


def test_event_source_decl_neither_kind_nor_membership_rejected() -> None:
    """Neither kind nor membership set -> rejected."""
    with pytest.raises(ValidationError, match="exactly one"):
        SourceEventSourceDecl()


def test_event_source_decl_both_kind_and_membership_rejected() -> None:
    """Both kind and membership set -> rejected."""
    with pytest.raises(ValidationError, match="exactly one"):
        SourceEventSourceDecl(
            kind="trip", membership=MembershipRef(kind="trip", property="drivers")
        )


def test_event_source_decl_sub_types_only_with_kind() -> None:
    """sub_types on a membership-source events source -> rejected."""
    with pytest.raises(ValidationError, match="sub_types"):
        SourceEventSourceDecl(
            membership=MembershipRef(kind="trip", property="drivers"),
            sub_types=("standard",),
        )


def test_event_source_decl_only_ignore_mutually_exclusive() -> None:
    """`only` and `ignore` set together -> rejected."""
    with pytest.raises(ValidationError, match="mutually exclusive"):
        SourceEventSourceDecl(kind="trip", only=("status",), ignore=("fare",))


def test_event_source_decl_only_empty_rejected() -> None:
    """`only` present but empty -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceEventSourceDecl(kind="trip", only=())


@pytest.mark.parametrize(
    "kwargs, match",
    [
        pytest.param(
            {"kind": "trip", "only": ("status", "status")},
            "distinct",
            id="only_duplicate",
        ),
        pytest.param(
            {"kind": "trip", "ignore": ("fare", "fare")},
            "distinct",
            id="ignore_duplicate",
        ),
    ],
)
def test_event_source_decl_field_duplicate_rejected(
    kwargs: dict[str, object], match: str
) -> None:
    """A duplicate entry in `only`/`ignore` -> rejected."""
    with pytest.raises(ValidationError, match=match):
        SourceEventSourceDecl(**kwargs)  # type: ignore[arg-type]


def test_event_source_decl_only_parses() -> None:
    """A valid `only` filter parses."""
    decl = SourceEventSourceDecl(kind="trip", only=("status", "fare"))
    assert decl.only == ("status", "fare")


def test_event_source_decl_extra_field_forbidden() -> None:
    """SourceEventSourceDecl rejects unknown fields."""
    with pytest.raises(ValidationError):
        SourceEventSourceDecl.model_validate({"kind": "trip", "bogus": 1})


# ---------------------------------------------------------------------------
# SourceEventsDecl
# ---------------------------------------------------------------------------


def test_events_decl_parses() -> None:
    """A well-formed events decl parses."""
    decl = SourceEventsDecl(
        name="versions",
        sources=(
            SourceEventSourceDecl(kind="trip"),
            SourceEventSourceDecl(kind="customer"),
        ),
    )
    assert decl.name == "versions"
    assert len(decl.sources) == 2


def test_events_decl_name_empty_rejected() -> None:
    """Empty name -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceEventsDecl(name="", sources=(SourceEventSourceDecl(kind="trip"),))


def test_events_decl_sources_required_nonempty() -> None:
    """sources must carry >= 1 entry."""
    with pytest.raises(ValidationError):
        SourceEventsDecl(name="versions", sources=())


def test_events_decl_extra_field_forbidden() -> None:
    """SourceEventsDecl rejects unknown fields."""
    with pytest.raises(ValidationError):
        SourceEventsDecl.model_validate(
            {"name": "versions", "sources": [{"kind": "trip"}], "bogus": 1}
        )
