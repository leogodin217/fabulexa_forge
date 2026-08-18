"""Tests for the source declared-table decl models: MembershipRef,
SourceTableDecl, SourceEventSourceDecl, SourceEventsDecl.

These models are exercised standalone here (`ExportConfig` / plan-time wiring
is `test_plan.py` / `test_where_plan.py`'s), since every shape rule they carry
(`table_shape` / `source_shape`, including `SourceTableDecl.where`'s
present-but-empty / empty-key clause) is decidable from the decl alone.
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


def test_table_decl_sub_types_with_membership_parses() -> None:
    """`sub_types` alongside `membership` parses (owner sub-type subset,
    doc § The parent lookup) — no longer a parse error; validated against
    the owner kind's discriminator domain at plan time
    (`tests/exporters/source/test_where_plan.py`)."""
    decl = SourceTableDecl(
        name="trip_drivers",
        membership=MembershipRef(kind="trip", property="drivers"),
        sub_types=("standard",),
    )
    assert decl.sub_types == ("standard",)


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


# ---------------------------------------------------------------------------
# SourceTableDecl.where — shape clause (doc § Config Models; PredicateValue
# per-entry emptiness/duplication rides the type, not this validator)
# ---------------------------------------------------------------------------


def test_table_decl_where_empty_dict_rejected() -> None:
    """`where: {}` (present but empty) -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceTableDecl(name="trips", kind="trip", where={})


def test_table_decl_where_empty_key_rejected() -> None:
    """A `where` entry with an empty key -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceTableDecl(name="trips", kind="trip", where={"": "standard"})


def test_table_decl_where_empty_list_value_rejected() -> None:
    """A `where` value that is an empty list -> rejected by `PredicateValue`
    at the offending entry's path, not by `table_shape`."""
    with pytest.raises(ValidationError, match="empty list"):
        SourceTableDecl(name="trips", kind="trip", where={"prop__fare": []})


def test_table_decl_where_duplicate_list_element_rejected() -> None:
    """A `where` list value carrying a duplicate element -> rejected by
    `PredicateValue` at the offending entry's path."""
    with pytest.raises(ValidationError, match="duplicate element"):
        SourceTableDecl(name="trips", kind="trip", where={"prop__fare": ["5", "5"]})


def test_table_decl_where_scalar_parses() -> None:
    """A scalar `where` value parses."""
    decl = SourceTableDecl(name="trips", kind="trip", where={"prop__fare": "5"})
    assert decl.where == {"prop__fare": "5"}


def test_table_decl_where_list_parses() -> None:
    """A non-empty, duplicate-free list `where` value parses."""
    decl = SourceTableDecl(name="trips", kind="trip", where={"prop__fare": ["5", "10"]})
    assert decl.where == {"prop__fare": ["5", "10"]}


def test_table_decl_where_absent_defaults_none() -> None:
    """A table decl declaring no `where` parses exactly as today: None."""
    decl = SourceTableDecl(name="trips", kind="trip")
    assert decl.where is None


def test_table_decl_extra_field_forbidden() -> None:
    """SourceTableDecl rejects unknown fields."""
    with pytest.raises(ValidationError):
        SourceTableDecl.model_validate({"name": "trips", "kind": "trip", "bogus": 1})


# ---------------------------------------------------------------------------
# SourceTableDecl.render / date_parse — structural-instant elections and
# declared date parses (doc § Config Models; render_maps_valid)
# ---------------------------------------------------------------------------


def test_table_decl_render_parses() -> None:
    """A well-formed `render` map parses."""
    decl = SourceTableDecl(
        name="trips", kind="trip", render={"created_sim_time": "date"}
    )
    assert decl.render == {"created_sim_time": "date"}


def test_table_decl_render_empty_map_rejected() -> None:
    """`render: {}` (present but empty) -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceTableDecl(name="trips", kind="trip", render={})


def test_table_decl_render_empty_key_rejected() -> None:
    """A `render` entry with an empty key -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceTableDecl(name="trips", kind="trip", render={"": "date"})


def test_table_decl_date_parse_parses() -> None:
    """A well-formed `date_parse` map parses."""
    decl = SourceTableDecl(
        name="trips", kind="trip", date_parse={"prop__dob": "%Y-%m-%d"}
    )
    assert decl.date_parse == {"prop__dob": "%Y-%m-%d"}


def test_table_decl_date_parse_empty_map_rejected() -> None:
    """`date_parse: {}` (present but empty) -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceTableDecl(name="trips", kind="trip", date_parse={})


def test_table_decl_date_parse_empty_key_rejected() -> None:
    """A `date_parse` entry with an empty key -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceTableDecl(name="trips", kind="trip", date_parse={"": "%Y-%m-%d"})


def test_table_decl_date_parse_invalid_format_rejected() -> None:
    """A `date_parse` format missing a required directive -> rejected."""
    with pytest.raises(ValidationError, match="year"):
        SourceTableDecl(name="trips", kind="trip", date_parse={"prop__dob": "%m-%d"})


def test_table_decl_date_parse_datetime_format_parses() -> None:
    """A `date_parse` format carrying both date and time directives (the
    widened parse family) parses."""
    decl = SourceTableDecl(
        name="trips",
        kind="trip",
        date_parse={"prop__registered_at": "%Y-%m-%d %H:%M:%S"},
    )
    assert decl.date_parse == {"prop__registered_at": "%Y-%m-%d %H:%M:%S"}


def test_table_decl_date_parse_family_violation_rejected_entry_keyed() -> None:
    """A `date_parse` entry violating a family pairing rule -> rejected,
    the error naming the entry-keyed field name."""
    with pytest.raises(
        ValidationError, match=r"SourceTableDecl\.date_parse\['prop__dob'\]"
    ):
        SourceTableDecl(name="trips", kind="trip", date_parse={"prop__dob": "%I:%M"})


def test_table_decl_render_and_date_parse_column_overlap_rejected() -> None:
    """A column named in both `render` and `date_parse` -> rejected (a
    column names at most one)."""
    with pytest.raises(ValidationError, match="both"):
        SourceTableDecl(
            name="trips",
            kind="trip",
            render={"prop__dob": "date"},
            date_parse={"prop__dob": "%Y-%m-%d"},
        )


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


def test_event_source_decl_sub_types_with_membership_parses() -> None:
    """`sub_types` alongside `membership` parses (owner sub-type subset,
    doc § The parent lookup) — no longer a parse error; validated against
    the owner kind's discriminator domain at plan time
    (`tests/exporters/source/test_where_plan.py`)."""
    decl = SourceEventSourceDecl(
        membership=MembershipRef(kind="trip", property="drivers"),
        sub_types=("standard",),
    )
    assert decl.sub_types == ("standard",)


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
# SourceEventSourceDecl: item_type / rename (source-domain-vocabulary)
# ---------------------------------------------------------------------------


def test_event_source_decl_item_type_parses_on_records_source() -> None:
    """`item_type` parses on a records (kind) source."""
    decl = SourceEventSourceDecl(kind="trip", item_type="clinician")
    assert decl.item_type == "clinician"


def test_event_source_decl_item_type_parses_on_membership_source() -> None:
    """`item_type` parses on a membership source."""
    decl = SourceEventSourceDecl(
        membership=MembershipRef(kind="trip", property="drivers"),
        item_type="consultant_allocation",
    )
    assert decl.item_type == "consultant_allocation"


def test_event_source_decl_item_type_empty_rejected() -> None:
    """`item_type: ""` -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceEventSourceDecl(kind="trip", item_type="")


def test_event_source_decl_rename_parses_on_records_source() -> None:
    """`rename` parses on a records (kind) source."""
    decl = SourceEventSourceDecl(kind="trip", rename={"full_name": "name"})
    assert decl.rename == {"full_name": "name"}


def test_event_source_decl_rename_parses_on_membership_source() -> None:
    """`rename` parses on a membership source."""
    decl = SourceEventSourceDecl(
        membership=MembershipRef(kind="trip", property="drivers"),
        rename={"full_name": "name"},
    )
    assert decl.rename == {"full_name": "name"}


def test_event_source_decl_rename_empty_rejected() -> None:
    """`rename: {}` -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceEventSourceDecl(kind="trip", rename={})


def test_event_source_decl_rename_empty_key_rejected() -> None:
    """`rename` with an empty key -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceEventSourceDecl(kind="trip", rename={"": "name"})


def test_event_source_decl_rename_empty_value_rejected() -> None:
    """`rename` with an empty value -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceEventSourceDecl(kind="trip", rename={"full_name": ""})


def test_event_source_decl_rename_duplicate_targets_rejected() -> None:
    """Two `rename` keys sharing a target value -> rejected."""
    with pytest.raises(ValidationError, match="distinct"):
        SourceEventSourceDecl(
            kind="trip", rename={"full_name": "name", "nickname": "name"}
        )


def test_event_source_decl_item_type_and_rename_default_none() -> None:
    """A source declaring neither field parses exactly as today: both None."""
    decl = SourceEventSourceDecl(kind="trip")
    assert decl.item_type is None
    assert decl.rename is None


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


# ---------------------------------------------------------------------------
# SourceEventsDecl.render — the log's instant-column rendering election
# ---------------------------------------------------------------------------


def test_events_decl_render_parses() -> None:
    """A well-formed `render` map parses."""
    decl = SourceEventsDecl(
        name="versions",
        sources=(SourceEventSourceDecl(kind="trip"),),
        render={"event_sim_time": "timestamptz"},
    )
    assert decl.render == {"event_sim_time": "timestamptz"}


def test_events_decl_render_empty_map_rejected() -> None:
    """`render: {}` (present but empty) -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceEventsDecl(
            name="versions", sources=(SourceEventSourceDecl(kind="trip"),), render={}
        )


def test_events_decl_render_empty_key_rejected() -> None:
    """A `render` entry with an empty key -> rejected."""
    with pytest.raises(ValidationError, match="non-empty"):
        SourceEventsDecl(
            name="versions",
            sources=(SourceEventSourceDecl(kind="trip"),),
            render={"": "date"},
        )
