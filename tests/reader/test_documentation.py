"""Tests for the documentation view: Sidecar.documentation(), ColumnDoc /
EnumOption resolution, and the one-authority-per-column rule (design doc
§ The documentation view)."""

from __future__ import annotations

import pytest
from _support.sidecar_builder import enum_options, identity_column, prop_column

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.reader.documentation import ColumnDoc, EnumOption
from fabulexa_forge.reader.errors import ColumnNotFoundError, TableNotFoundError
from fabulexa_forge.reader.sidecar import Sidecar

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TRUNK_BRANCH: dict[str, object] = {
    "fork_path": "trunk",
    "parent": None,
    "slice_at": 0,
}

_HISTORY_TABLE: dict[str, object] = {
    "name": "history",
    "category": "fixed",
    "columns": [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "kind", "type": "VARCHAR"},
        {"name": "record_id", "type": "VARCHAR"},
        {"name": "property", "type": "VARCHAR"},
        {"name": "sim_time", "type": "BIGINT"},
        {"name": "value", "type": "VARCHAR"},
    ],
    "rows": 0,
}


def _records_table(
    record_kind: str,
    extra_columns: list[dict[str, object]],
    *,
    description: str | None = None,
) -> dict[str, object]:
    """Build a minimal records-category table: structural columns + extras."""
    columns: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        *extra_columns,
    ]
    table: dict[str, object] = {
        "name": f"records__{record_kind}",
        "category": "records",
        "record_kind": record_kind,
        "columns": columns,
        "rows": 0,
    }
    if description is not None:
        table["description"] = description
    return table


def _membership_table(record_kind: str, property_name: str) -> dict[str, object]:
    """Build a minimal membership-category table."""
    return {
        "name": f"membership__{record_kind}__{property_name}",
        "category": "membership",
        "record_kind": record_kind,
        "property": property_name,
        "columns": [
            identity_column("fork_path", "VARCHAR"),
            identity_column("record_id", "VARCHAR"),
            {"name": "joined_sim_time", "type": "BIGINT"},
            {"name": "left_sim_time", "type": "BIGINT"},
        ],
        "rows": 0,
    }


def _build_sidecar(tables: list[dict[str, object]], **extra: object) -> Sidecar:
    """Build a Sidecar over the trunk branch with the given tables plus any
    extra top-level sidecar blocks (scenario_description, enum_domains, ...)."""
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [_TRUNK_BRANCH],
        "tables": tables,
    }
    raw.update(extra)
    return Sidecar.from_raw(raw)


# ---------------------------------------------------------------------------
# column_doc -- resolution table
# ---------------------------------------------------------------------------


def test_history_column_pinned_string_with_unit_verbatim() -> None:
    """A history column resolves to its pinned contract string; the history
    family's placeholders (none present here) stay verbatim, unit carried."""
    sidecar = _build_sidecar([_HISTORY_TABLE])
    doc = sidecar.documentation().column_doc("history", "sim_time")
    assert doc == ColumnDoc(
        description=(
            "Simulation time the change took effect; the value holds until "
            "the series' next row."
        ),
        unit="ns",
        origin="contract",
    )


def test_records_structural_column_created_sim_time_unit_ns() -> None:
    """A records structural column resolves from the pinned block;
    created_sim_time carries unit 'ns'."""
    sidecar = _build_sidecar([_records_table("ticket", [])])
    doc = sidecar.documentation().column_doc("records__ticket", "created_sim_time")
    assert doc == ColumnDoc(
        description=(
            "Simulation time the record was created. Set once; never "
            "changed by later writes or deactivation."
        ),
        unit="ns",
        origin="contract",
    )


def test_ref_index_column_name_placeholder_bound() -> None:
    """ref_index__<name> resolves with <name> bound to the referenced
    property's own name."""
    extra = [
        prop_column(
            "prop__opened_by",
            "VARCHAR",
            history_tracked=False,
            temporal_class="constant",
            references="staff",
        ),
        identity_column("ref_index__opened_by", "BIGINT"),
    ]
    sidecar = _build_sidecar([_records_table("ticket", extra)])
    doc = sidecar.documentation().column_doc("records__ticket", "ref_index__opened_by")
    assert doc == ColumnDoc(
        description=(
            "Creation-order index of the record referenced by the sibling "
            "prop__opened_by column, resolved at the emitted slice; NULL "
            "together with it."
        ),
        unit=None,
        origin="contract",
    )


def test_membership_structural_column_kind_placeholder_bound() -> None:
    """A membership structural column binds <K> from the table's record_kind."""
    sidecar = _build_sidecar([_membership_table("ticket", "tags")])
    doc = sidecar.documentation().column_doc("membership__ticket__tags", "record_id")
    assert doc == ColumnDoc(
        description=(
            "Id of the record that owns the collection; its kind is the "
            "table name's ticket segment."
        ),
        unit=None,
        origin="contract",
    )


def test_prop_column_with_description_and_unit_sidecar_origin() -> None:
    """A prop__ column with both description and unit answers verbatim from
    the sidecar, origin 'sidecar'."""
    extra = [
        prop_column(
            "prop__balance",
            "DOUBLE",
            history_tracked=True,
            temporal_class="tracked",
            description="Current account balance.",
            unit="GBP",
        )
    ]
    sidecar = _build_sidecar([_records_table("customer", extra)])
    doc = sidecar.documentation().column_doc("records__customer", "prop__balance")
    assert doc == ColumnDoc(
        description="Current account balance.", unit="GBP", origin="sidecar"
    )


def test_prop_column_with_neither_description_nor_unit_returns_none() -> None:
    """A prop__ column with neither description nor unit resolves to None."""
    extra = [
        prop_column(
            "prop__notes",
            "VARCHAR",
            history_tracked=False,
            temporal_class="slice_only",
        )
    ]
    sidecar = _build_sidecar([_records_table("customer", extra)])
    doc = sidecar.documentation().column_doc("records__customer", "prop__notes")
    assert doc is None


def test_prop_column_unit_only_yields_description_none() -> None:
    """A prop__ column carrying only a unit yields ColumnDoc(description=None,
    unit=..., origin='sidecar')."""
    extra = [
        prop_column(
            "prop__weight",
            "DOUBLE",
            history_tracked=False,
            temporal_class="constant",
            unit="kg",
        )
    ]
    sidecar = _build_sidecar([_records_table("parcel", extra)])
    doc = sidecar.documentation().column_doc("records__parcel", "prop__weight")
    assert doc == ColumnDoc(description=None, unit="kg", origin="sidecar")


def test_declared_no_role_column_answers_from_sidecar() -> None:
    """A records-category column matching no taxonomy role still answers from
    its sidecar entry -- the pinned block has no entry to answer from."""
    extra = [
        {
            "name": "mystery_field",
            "type": "VARCHAR",
            "description": "An undeclared-role column, still sidecar-documented.",
        }
    ]
    sidecar = _build_sidecar([_records_table("widget", extra)])
    doc = sidecar.documentation().column_doc("records__widget", "mystery_field")
    assert doc == ColumnDoc(
        description="An undeclared-role column, still sidecar-documented.",
        unit=None,
        origin="sidecar",
    )


def test_structural_column_defective_sidecar_description_contract_wins() -> None:
    """A structural column that (defectively) also carries a sidecar
    description still answers from the contract -- one authority, never both."""
    columns: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {
            "name": "created_sim_time",
            "type": "BIGINT",
            "description": "A defective sidecar-authored description.",
        },
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
    ]
    table: dict[str, object] = {
        "name": "records__ticket",
        "category": "records",
        "record_kind": "ticket",
        "columns": columns,
        "rows": 0,
    }
    sidecar = _build_sidecar([table])
    doc = sidecar.documentation().column_doc("records__ticket", "created_sim_time")
    assert doc is not None
    assert doc.origin == "contract"
    assert doc.description != "A defective sidecar-authored description."


# ---------------------------------------------------------------------------
# column_doc -- identifier errors
# ---------------------------------------------------------------------------


def test_column_doc_unknown_table_raises_table_not_found() -> None:
    """column_doc raises TableNotFoundError for an undeclared table."""
    sidecar = _build_sidecar([_HISTORY_TABLE])
    with pytest.raises(TableNotFoundError):
        sidecar.documentation().column_doc("phantom", "sim_time")


def test_column_doc_unknown_column_raises_column_not_found() -> None:
    """column_doc raises ColumnNotFoundError for an undeclared column."""
    sidecar = _build_sidecar([_HISTORY_TABLE])
    with pytest.raises(ColumnNotFoundError):
        sidecar.documentation().column_doc("history", "phantom")


# ---------------------------------------------------------------------------
# table_description
# ---------------------------------------------------------------------------


def test_table_description_present_verbatim() -> None:
    """table_description returns tables[].description verbatim when present."""
    sidecar = _build_sidecar(
        [_records_table("customer", [], description="Customer accounts.")]
    )
    assert (
        sidecar.documentation().table_description("records__customer")
        == "Customer accounts."
    )


def test_table_description_absent_is_none() -> None:
    """table_description returns None when the table carries no description."""
    sidecar = _build_sidecar([_records_table("customer", [])])
    assert sidecar.documentation().table_description("records__customer") is None


def test_table_description_history_is_none() -> None:
    """table_description is always None for the fixed history table."""
    sidecar = _build_sidecar([_HISTORY_TABLE])
    assert sidecar.documentation().table_description("history") is None


def test_table_description_unknown_table_raises_table_not_found() -> None:
    """table_description raises TableNotFoundError for an undeclared table."""
    sidecar = _build_sidecar([_HISTORY_TABLE])
    with pytest.raises(TableNotFoundError):
        sidecar.documentation().table_description("phantom")


# ---------------------------------------------------------------------------
# enum_options
# ---------------------------------------------------------------------------


def test_enum_options_membership_and_order_match_typed_enum_domains() -> None:
    """enum_options' value set and order equal the typed values-only
    enum_domains() surface."""
    sidecar = _build_sidecar(
        [_HISTORY_TABLE],
        enum_domains={
            "customer": {"status": enum_options("active", "closed", "pending")}
        },
    )
    options = sidecar.documentation().enum_options("customer", "status")
    assert (
        tuple(o.value for o in options) == sidecar.enum_domains()["customer"]["status"]
    )


def test_enum_options_glosses_verbatim() -> None:
    """Per-value glosses are carried verbatim, in sidecar order."""
    raw_options = [
        {"value": "active", "description": "Account is open."},
        {"value": "closed", "description": "Account has been closed."},
    ]
    sidecar = _build_sidecar(
        [_HISTORY_TABLE], enum_domains={"customer": {"status": raw_options}}
    )
    options = sidecar.documentation().enum_options("customer", "status")
    assert options == (
        EnumOption(value="active", description="Account is open."),
        EnumOption(value="closed", description="Account has been closed."),
    )


def test_enum_options_gloss_absent_is_none() -> None:
    """An option with no description gloss carries description=None."""
    sidecar = _build_sidecar(
        [_HISTORY_TABLE],
        enum_domains={"customer": {"status": enum_options("active", "closed")}},
    )
    options = sidecar.documentation().enum_options("customer", "status")
    assert all(o.description is None for o in options)


def test_enum_options_malformed_value_object_absent_from_both_views() -> None:
    """A malformed option entry drops whole, from both this view and the
    typed values-only enum_domains() surface."""
    raw_options = [
        {"value": "active"},
        "not-a-dict",
        {"description": "no value key"},
        {"value": "closed"},
    ]
    sidecar = _build_sidecar(
        [_HISTORY_TABLE], enum_domains={"customer": {"status": raw_options}}
    )
    options = sidecar.documentation().enum_options("customer", "status")
    assert tuple(o.value for o in options) == ("active", "closed")
    assert (
        tuple(o.value for o in options) == sidecar.enum_domains()["customer"]["status"]
    )


def test_enum_options_mis_shaped_gloss_becomes_gloss_absent_value_kept() -> None:
    """A mis-shaped (non-string) gloss parses as gloss-absent; the value survives."""
    raw_options = [{"value": "active", "description": 42}]
    sidecar = _build_sidecar(
        [_HISTORY_TABLE], enum_domains={"customer": {"status": raw_options}}
    )
    options = sidecar.documentation().enum_options("customer", "status")
    assert options == (EnumOption(value="active", description=None),)


def test_enum_options_unknown_kind_raises_key_error() -> None:
    """enum_options raises KeyError for a kind with no enum_domains entry."""
    sidecar = _build_sidecar(
        [_HISTORY_TABLE],
        enum_domains={"customer": {"status": enum_options("active")}},
    )
    with pytest.raises(KeyError):
        sidecar.documentation().enum_options("phantom_kind", "status")


def test_enum_options_unknown_prop_raises_key_error() -> None:
    """enum_options raises KeyError for a prop with no entry under a known kind."""
    sidecar = _build_sidecar(
        [_HISTORY_TABLE],
        enum_domains={"customer": {"status": enum_options("active")}},
    )
    with pytest.raises(KeyError):
        sidecar.documentation().enum_options("customer", "phantom_prop")


def test_enum_options_discriminator_via_kind_type() -> None:
    """A sub-typed kind's discriminator domain is read at <kind>_type, same
    as any other declared property."""
    sidecar = _build_sidecar(
        [_HISTORY_TABLE],
        enum_domains={"actor": {"actor_type": enum_options("staff", "trip")}},
    )
    options = sidecar.documentation().enum_options("actor", "actor_type")
    assert tuple(o.value for o in options) == ("staff", "trip")


# ---------------------------------------------------------------------------
# scenario_description
# ---------------------------------------------------------------------------


def test_scenario_description_verbatim() -> None:
    """scenario_description returns the sidecar's narrative verbatim."""
    sidecar = _build_sidecar(
        [_HISTORY_TABLE], scenario_description="A retail loyalty simulation."
    )
    assert (
        sidecar.documentation().scenario_description() == "A retail loyalty simulation."
    )


def test_scenario_description_absent_is_none() -> None:
    """scenario_description returns None when the sidecar declares none."""
    sidecar = _build_sidecar([_HISTORY_TABLE])
    assert sidecar.documentation().scenario_description() is None


# ---------------------------------------------------------------------------
# Laziness
# ---------------------------------------------------------------------------


def test_documentation_is_lazily_constructed_and_cached() -> None:
    """Two documentation() calls on the same Sidecar return the same object."""
    sidecar = _build_sidecar([_HISTORY_TABLE])
    assert sidecar.documentation() is sidecar.documentation()
