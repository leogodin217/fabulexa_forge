"""Tests for the sidecar core: Sidecar.from_raw, descriptors, error hierarchy,
and RecordRoles accessor."""

from __future__ import annotations

import pytest
from _support.sidecar_builder import identity_column

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.reader.errors import (
    SidecarStructureError,
    TableNotFoundError,
    UnsupportedBaseFormatVersionError,
)
from fabulexa_forge.reader.sidecar import (
    BranchEntry,
    ColumnSpec,
    RecordRoles,
    RuntimeAnchor,
    Sidecar,
    SubTypeColumns,
)

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
    "columns": [{"name": "fork_path", "type": "VARCHAR"}],
    "rows": 0,
}


def _minimal_raw(**overrides: object) -> dict[str, object]:
    """Build a minimal valid base.json mapping."""
    base: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [_TRUNK_BRANCH],
        "tables": [_HISTORY_TABLE],
    }
    base.update(overrides)
    return base


def _full_raw() -> dict[str, object]:
    """Build a fully-populated base.json mapping exercising all optional blocks."""
    return {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [
            {"fork_path": "trunk", "parent": None, "slice_at": 0},
            {"fork_path": "trunk@branch_a", "parent": "trunk", "slice_at": 100},
        ],
        "tables": [
            {
                "name": "history",
                "category": "fixed",
                "columns": [
                    {"name": "fork_path", "type": "VARCHAR"},
                    {"name": "sim_time", "type": "BIGINT"},
                ],
                "rows": 42,
            },
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "columns": [
                    identity_column("record_id", "VARCHAR"),
                    identity_column("record_index", "BIGINT"),
                    {
                        "name": "prop__doctor",
                        "type": "VARCHAR",
                        "references": "doctor",
                    },
                    identity_column("ref_index__doctor", "BIGINT"),
                ],
                "rows": 10,
            },
            {
                "name": "membership__patient__tags",
                "category": "membership",
                "record_kind": "patient",
                "property": "tags",
                "columns": [identity_column("record_id", "VARCHAR")],
                "rows": 5,
            },
        ],
        "runtime": {
            "timezone": "America/New_York",
            "start_datetime": "2024-01-01T00:00:00-05:00",
        },
        "pinned_ids": {
            "patient": {"alice": "uuid-alice", "bob": "uuid-bob"},
        },
        "enum_domains": {
            "patient": {"status": ["active", "discharged", "deceased"]},
        },
    }


def _record_roles_raw() -> dict[str, object]:
    """A record_roles block with an actor (object-valued) and bare-string kinds."""
    return {
        "actor": {"trip": "fact", "visit": "fact", "staff": "dimension"},
        "entity": "dimension",
        "asset": "fact",
    }


def _sidecar_with_record_roles() -> Sidecar:
    """Build a Sidecar with a record_roles block."""
    raw = _minimal_raw(record_roles=_record_roles_raw())
    return Sidecar.from_raw(raw)


# ---------------------------------------------------------------------------
# Happy-path: fully-populated raw builds expected descriptors
# ---------------------------------------------------------------------------


def test_from_raw_fully_populated_branches() -> None:
    """from_raw on a fully-populated dict builds BranchEntry tuples in order."""
    sidecar = Sidecar.from_raw(_full_raw())
    branches = sidecar.branches()
    assert len(branches) == 2
    assert branches[0] == BranchEntry(fork_path="trunk", parent=None, slice_at=0)
    assert branches[1] == BranchEntry(
        fork_path="trunk@branch_a", parent="trunk", slice_at=100
    )


def test_from_raw_fully_populated_tables() -> None:
    """from_raw builds TableSpec tuples with columns in sidecar order."""
    sidecar = Sidecar.from_raw(_full_raw())
    tables = sidecar.tables()
    assert len(tables) == 3

    history = tables[0]
    assert history.name == "history"
    assert history.category == "fixed"
    assert history.record_kind is None
    assert history.property is None
    assert history.rows == 42
    assert len(history.columns) == 2
    assert history.columns[0] == ColumnSpec(
        name="fork_path",
        type="VARCHAR",
        references=None,
        history_tracked=None,
        temporal_class=None,
    )

    records_table = tables[1]
    assert records_table.name == "records__patient"
    assert records_table.category == "records"
    assert records_table.record_kind == "patient"
    assert records_table.property is None
    ref_col = records_table.columns[2]
    assert ref_col.references == "doctor"

    membership_table = tables[2]
    assert membership_table.category == "membership"
    assert membership_table.record_kind == "patient"
    assert membership_table.property == "tags"


def test_from_raw_runtime_present() -> None:
    """runtime() returns a RuntimeAnchor when the block is present."""
    sidecar = Sidecar.from_raw(_full_raw())
    rt = sidecar.runtime()
    assert rt == RuntimeAnchor(
        timezone="America/New_York",
        start_datetime="2024-01-01T00:00:00-05:00",
    )


def test_from_raw_runtime_absent() -> None:
    """runtime() returns None when no runtime block."""
    sidecar = Sidecar.from_raw(_minimal_raw())
    assert sidecar.runtime() is None


def test_from_raw_pinned_ids_present() -> None:
    """pinned_ids() returns the nested mapping when present."""
    sidecar = Sidecar.from_raw(_full_raw())
    pins = sidecar.pinned_ids()
    assert pins == {"patient": {"alice": "uuid-alice", "bob": "uuid-bob"}}


def test_from_raw_pinned_ids_absent() -> None:
    """pinned_ids() returns empty mapping when absent."""
    sidecar = Sidecar.from_raw(_minimal_raw())
    assert sidecar.pinned_ids() == {}


def test_from_raw_enum_domains_present() -> None:
    """enum_domains() returns the nested mapping when present."""
    sidecar = Sidecar.from_raw(_full_raw())
    domains = sidecar.enum_domains()
    assert domains == {"patient": {"status": ("active", "discharged", "deceased")}}


def test_from_raw_enum_domains_absent() -> None:
    """enum_domains() returns empty mapping when absent."""
    sidecar = Sidecar.from_raw(_minimal_raw())
    assert sidecar.enum_domains() == {}


# ---------------------------------------------------------------------------
# pinned_ids / enum_domains lenient parse — malformed nested entries dropped
# ---------------------------------------------------------------------------


def test_pinned_ids_non_string_id_dropped_valid_siblings_kept() -> None:
    """A non-string id under a pinned label is dropped; valid siblings survive."""
    raw = _minimal_raw(
        pinned_ids={"patient": {"alice": "uuid-alice", "bob": 42, "carol": None}}
    )
    sidecar = Sidecar.from_raw(raw)
    assert sidecar.pinned_ids() == {"patient": {"alice": "uuid-alice"}}


def test_pinned_ids_non_dict_labels_drops_kind() -> None:
    """A kind whose labels value is not a dict is dropped entirely."""
    raw = _minimal_raw(
        pinned_ids={
            "patient": "not-a-dict",
            "doctor": {"dana": "uuid-dana"},
        }
    )
    sidecar = Sidecar.from_raw(raw)
    assert sidecar.pinned_ids() == {"doctor": {"dana": "uuid-dana"}}


def test_pinned_ids_all_ids_invalid_keeps_kind_with_empty_mapping() -> None:
    """A kind whose ids are all non-string keeps the kind key with an empty inner map."""
    raw = _minimal_raw(pinned_ids={"patient": {"alice": 1, "bob": True}})
    sidecar = Sidecar.from_raw(raw)
    assert sidecar.pinned_ids() == {"patient": {}}


def test_pinned_ids_non_dict_block_returns_empty() -> None:
    """A pinned_ids block that is not a dict parses to an empty mapping."""
    raw = _minimal_raw(pinned_ids="not-a-dict")
    sidecar = Sidecar.from_raw(raw)
    assert sidecar.pinned_ids() == {}


def test_enum_domains_non_list_options_drops_property() -> None:
    """A property whose options value is not a list is dropped; siblings survive."""
    raw = _minimal_raw(
        enum_domains={
            "patient": {
                "status": ["active", "discharged"],
                "tier": "not-a-list",
            }
        }
    )
    sidecar = Sidecar.from_raw(raw)
    assert sidecar.enum_domains() == {"patient": {"status": ("active", "discharged")}}


def test_enum_domains_non_string_option_dropped_from_list() -> None:
    """Non-string entries within an options list are dropped, keeping the rest."""
    raw = _minimal_raw(
        enum_domains={"patient": {"status": ["active", 42, None, "discharged"]}}
    )
    sidecar = Sidecar.from_raw(raw)
    assert sidecar.enum_domains() == {"patient": {"status": ("active", "discharged")}}


def test_enum_domains_non_dict_props_drops_kind() -> None:
    """A kind whose properties value is not a dict is dropped entirely."""
    raw = _minimal_raw(
        enum_domains={
            "patient": "not-a-dict",
            "doctor": {"specialty": ["surgery"]},
        }
    )
    sidecar = Sidecar.from_raw(raw)
    assert sidecar.enum_domains() == {"doctor": {"specialty": ("surgery",)}}


def test_enum_domains_non_dict_block_returns_empty() -> None:
    """An enum_domains block that is not a dict parses to an empty mapping."""
    raw = _minimal_raw(enum_domains=["not", "a", "dict"])
    sidecar = Sidecar.from_raw(raw)
    assert sidecar.enum_domains() == {}


def test_from_raw_references_carried_through() -> None:
    """references field is carried through to ColumnSpec."""
    sidecar = Sidecar.from_raw(_full_raw())
    cols = sidecar.columns("records__patient")
    ref_col = next(c for c in cols if c.name == "prop__doctor")
    assert ref_col.references == "doctor"
    non_ref_col = cols[0]
    assert non_ref_col.references is None


def test_sidecar_raw_is_original_mapping() -> None:
    """Sidecar.raw returns the original parsed mapping."""
    raw = _minimal_raw()
    sidecar = Sidecar.from_raw(raw)
    assert sidecar.raw is raw


def test_sidecar_base_format_version() -> None:
    """base_format_version returns SUPPORTED_BASE_FORMAT_VERSION."""
    sidecar = Sidecar.from_raw(_minimal_raw())
    assert sidecar.base_format_version == SUPPORTED_BASE_FORMAT_VERSION


# ---------------------------------------------------------------------------
# Root vs child branch parent field
# ---------------------------------------------------------------------------


def test_root_branch_parent_is_none() -> None:
    """A root branch carries parent: null -> BranchEntry.parent is None."""
    sidecar = Sidecar.from_raw(_minimal_raw())
    assert sidecar.branches()[0].parent is None


def test_child_branch_parent_is_string() -> None:
    """A child branch carries the parent string."""
    sidecar = Sidecar.from_raw(_full_raw())
    child = sidecar.branches()[1]
    assert child.parent == "trunk"


# ---------------------------------------------------------------------------
# table() and columns() accessors
# ---------------------------------------------------------------------------


def test_table_returns_matching_spec() -> None:
    """table(name) returns the matching TableSpec."""
    sidecar = Sidecar.from_raw(_full_raw())
    spec = sidecar.table("history")
    assert spec.name == "history"


def test_table_unknown_raises_table_not_found() -> None:
    """table(name) raises TableNotFoundError for unknown name."""
    sidecar = Sidecar.from_raw(_minimal_raw())
    with pytest.raises(TableNotFoundError):
        sidecar.table("does_not_exist")


def test_columns_returns_columns_for_known_table() -> None:
    """columns(name) mirrors table(name).columns."""
    sidecar = Sidecar.from_raw(_full_raw())
    cols = sidecar.columns("history")
    assert len(cols) == 2
    assert cols[0].name == "fork_path"


def test_columns_unknown_raises_table_not_found() -> None:
    """columns(name) raises TableNotFoundError for unknown table."""
    sidecar = Sidecar.from_raw(_minimal_raw())
    with pytest.raises(TableNotFoundError):
        sidecar.columns("phantom_table")


def test_duplicate_table_name_resolves_to_first() -> None:
    """Duplicate-named tables resolve to the first match."""
    raw = _minimal_raw()
    raw["tables"] = [
        {
            "name": "history",
            "category": "fixed",
            "columns": [{"name": "col_a", "type": "VARCHAR"}],
            "rows": 10,
        },
        {
            "name": "history",
            "category": "fixed",
            "columns": [{"name": "col_b", "type": "BIGINT"}],
            "rows": 0,
        },
    ]
    sidecar = Sidecar.from_raw(raw)
    spec = sidecar.table("history")
    assert spec.columns[0].name == "col_a"


def test_tables_order_preserved() -> None:
    """tables() preserves sidecar order."""
    sidecar = Sidecar.from_raw(_full_raw())
    names = [t.name for t in sidecar.tables()]
    assert names == ["history", "records__patient", "membership__patient__tags"]


def test_branches_order_preserved() -> None:
    """branches() preserves sidecar order."""
    sidecar = Sidecar.from_raw(_full_raw())
    paths = [b.fork_path for b in sidecar.branches()]
    assert paths == ["trunk", "trunk@branch_a"]


# ---------------------------------------------------------------------------
# Version errors
# ---------------------------------------------------------------------------


def test_unsupported_version_raises_with_found_version() -> None:
    """from_raw raises UnsupportedBaseFormatVersionError(found_version=99) for version 99."""
    raw = _minimal_raw(base_format_version=99)
    with pytest.raises(UnsupportedBaseFormatVersionError) as exc_info:
        Sidecar.from_raw(raw)
    assert exc_info.value.found_version == 99


def test_unsupported_version_found_version_is_int() -> None:
    """The carried found_version is the int 99."""
    raw = _minimal_raw(base_format_version=99)
    with pytest.raises(UnsupportedBaseFormatVersionError) as exc_info:
        Sidecar.from_raw(raw)
    assert isinstance(exc_info.value.found_version, int)
    assert exc_info.value.found_version == 99


# ---------------------------------------------------------------------------
# SidecarStructureError: non-version-5 values of base_format_version
# ---------------------------------------------------------------------------


def test_structure_error_absent_version() -> None:
    """from_raw raises SidecarStructureError when base_format_version is absent."""
    raw: dict[str, object] = {
        "branches": [_TRUNK_BRANCH],
        "tables": [_HISTORY_TABLE],
    }
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_float_version() -> None:
    """from_raw raises SidecarStructureError for float 3.0 (not a strict int)."""
    raw = _minimal_raw(base_format_version=3.0)
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_bool_version_true() -> None:
    """from_raw raises SidecarStructureError for True (bool must NOT pass as version 1)."""
    raw = _minimal_raw(base_format_version=True)
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_bool_version_false() -> None:
    """from_raw raises SidecarStructureError for False (bool is not a strict int here)."""
    raw = _minimal_raw(base_format_version=False)
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_string_version() -> None:
    """from_raw raises SidecarStructureError for string '3'."""
    raw = _minimal_raw(base_format_version="3")
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_null_version() -> None:
    """from_raw raises SidecarStructureError for null/None."""
    raw = _minimal_raw(base_format_version=None)
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


# ---------------------------------------------------------------------------
# SidecarStructureError: structural floor failures
# ---------------------------------------------------------------------------


def test_structure_error_branches_empty_list() -> None:
    """from_raw raises SidecarStructureError when branches is an empty list."""
    raw = _minimal_raw(branches=[])
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_branches_not_a_list() -> None:
    """from_raw raises SidecarStructureError when branches is not a list."""
    raw = _minimal_raw(branches="not-a-list")
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_tables_not_a_list() -> None:
    """from_raw raises SidecarStructureError when tables is not a list."""
    raw = _minimal_raw(tables="not-a-list")
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_table_missing_columns() -> None:
    """from_raw raises SidecarStructureError when a table is missing columns."""
    raw = _minimal_raw()
    raw["tables"] = [{"name": "history", "category": "fixed", "rows": 0}]
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_table_missing_name() -> None:
    """from_raw raises SidecarStructureError when a table is missing name."""
    raw = _minimal_raw()
    raw["tables"] = [
        {"category": "fixed", "columns": [{"name": "x", "type": "VARCHAR"}], "rows": 0}
    ]
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_table_missing_category() -> None:
    """from_raw raises SidecarStructureError when a table is missing category."""
    raw = _minimal_raw()
    raw["tables"] = [
        {"name": "history", "columns": [{"name": "x", "type": "VARCHAR"}], "rows": 0}
    ]
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_table_non_string_category() -> None:
    """from_raw raises SidecarStructureError when category is not a string."""
    raw = _minimal_raw()
    raw["tables"] = [
        {
            "name": "history",
            "category": 42,
            "columns": [{"name": "x", "type": "VARCHAR"}],
            "rows": 0,
        }
    ]
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_table_missing_rows() -> None:
    """from_raw raises SidecarStructureError when a table is missing rows."""
    raw = _minimal_raw()
    raw["tables"] = [
        {
            "name": "history",
            "category": "fixed",
            "columns": [{"name": "x", "type": "VARCHAR"}],
        }
    ]
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_column_not_object() -> None:
    """from_raw raises SidecarStructureError when a columns element is not an object."""
    raw = _minimal_raw()
    raw["tables"] = [
        {
            "name": "history",
            "category": "fixed",
            "columns": ["not-an-object"],
            "rows": 0,
        }
    ]
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_column_missing_name() -> None:
    """from_raw raises SidecarStructureError when a column is missing name."""
    raw = _minimal_raw()
    raw["tables"] = [
        {
            "name": "history",
            "category": "fixed",
            "columns": [{"type": "VARCHAR"}],
            "rows": 0,
        }
    ]
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_column_missing_type() -> None:
    """from_raw raises SidecarStructureError when a column is missing type."""
    raw = _minimal_raw()
    raw["tables"] = [
        {"name": "history", "category": "fixed", "columns": [{"name": "x"}], "rows": 0}
    ]
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_branch_missing_fork_path() -> None:
    """from_raw raises SidecarStructureError when a branch is missing fork_path."""
    raw = _minimal_raw()
    raw["branches"] = [{"parent": None, "slice_at": 0}]
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_branch_missing_slice_at() -> None:
    """from_raw raises SidecarStructureError when a branch is missing slice_at."""
    raw = _minimal_raw()
    raw["branches"] = [{"fork_path": "trunk", "parent": None}]
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


def test_structure_error_branch_absent_parent_key() -> None:
    """from_raw raises SidecarStructureError when a branch has no parent key (absent, not null)."""
    raw = _minimal_raw()
    raw["branches"] = [{"fork_path": "trunk", "slice_at": 0}]
    with pytest.raises(SidecarStructureError):
        Sidecar.from_raw(raw)


# ---------------------------------------------------------------------------
# Successful opens despite schema-invalid input (C1/C3's job, not the floor's)
# ---------------------------------------------------------------------------


def test_succeeds_with_empty_columns_array() -> None:
    """from_raw SUCCEEDS with columns: [] (schema-invalid, but floor allows it)."""
    raw = _minimal_raw()
    raw["tables"] = [{"name": "history", "category": "fixed", "columns": [], "rows": 0}]
    sidecar = Sidecar.from_raw(raw)
    assert sidecar.columns("history") == ()


def test_bogus_category_raises_structure_error() -> None:
    """from_raw raises SidecarStructureError for an out-of-set category, naming
    the table and the value (structural-temporal sprint: reclassified from a
    C1 conformance failure to a parse-time refusal)."""
    raw = _minimal_raw()
    raw["tables"] = [
        {
            "name": "history",
            "category": "bogus",
            "columns": [{"name": "x", "type": "VARCHAR"}],
            "rows": 0,
        }
    ]
    with pytest.raises(SidecarStructureError, match="history.*bogus"):
        Sidecar.from_raw(raw)


@pytest.mark.parametrize("category", ["fixed", "records", "membership"])
def test_recognised_categories_parse(category: str) -> None:
    """Each of the three contract table categories still parses successfully."""
    raw = _minimal_raw()
    raw["tables"] = [
        {
            "name": "t",
            "category": category,
            "columns": [{"name": "x", "type": "VARCHAR"}],
            "rows": 0,
        }
    ]
    sidecar = Sidecar.from_raw(raw)
    assert sidecar.table("t").category == category


def test_succeeds_with_missing_record_kind_on_records_table() -> None:
    """from_raw SUCCEEDS with record_kind absent on a records table (C1/C3 enforces)."""
    raw = _minimal_raw()
    raw["tables"] = [
        {
            "name": "records__patient",
            "category": "records",
            "columns": [{"name": "record_id", "type": "VARCHAR"}],
            "rows": 0,
        }
    ]
    sidecar = Sidecar.from_raw(raw)
    assert sidecar.table("records__patient").record_kind is None


def test_succeeds_with_missing_property_on_membership_table() -> None:
    """from_raw SUCCEEDS with property absent on a membership table (C1/C3 enforces)."""
    raw = _minimal_raw()
    raw["tables"] = [
        {
            "name": "membership__patient__tags",
            "category": "membership",
            "record_kind": "patient",
            "columns": [{"name": "record_id", "type": "VARCHAR"}],
            "rows": 0,
        }
    ]
    sidecar = Sidecar.from_raw(raw)
    assert sidecar.table("membership__patient__tags").property is None


# ---------------------------------------------------------------------------
# record_roles() accessor — present and absent
# ---------------------------------------------------------------------------


def test_record_roles_absent_returns_none() -> None:
    """record_roles() returns None when the sidecar has no record_roles block."""
    sidecar = Sidecar.from_raw(_minimal_raw())
    assert sidecar.record_roles() is None


def test_record_roles_present_returns_record_roles_instance() -> None:
    """record_roles() returns a RecordRoles instance when the block is present."""
    sidecar = _sidecar_with_record_roles()
    result = sidecar.record_roles()
    assert isinstance(result, RecordRoles)


# ---------------------------------------------------------------------------
# RecordRoles.kinds()
# ---------------------------------------------------------------------------


def test_record_roles_kinds_returns_all_registered_keys() -> None:
    """kinds() returns all registered kind names."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    assert set(rr.kinds()) == {"actor", "entity", "asset"}


def test_record_roles_kinds_preserves_sidecar_order() -> None:
    """kinds() preserves the order from the sidecar dict."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    assert rr.kinds() == ("actor", "entity", "asset")


def test_record_roles_kinds_returns_tuple() -> None:
    """kinds() returns a tuple, not a list."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    assert isinstance(rr.kinds(), tuple)


# ---------------------------------------------------------------------------
# RecordRoles.is_subtyped()
# ---------------------------------------------------------------------------


def test_is_subtyped_true_for_object_valued_kind() -> None:
    """is_subtyped('actor') is True when actor is object-valued."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    assert rr.is_subtyped("actor") is True


def test_is_subtyped_false_for_bare_string_kind() -> None:
    """is_subtyped('entity') is False when entity is a bare string."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    assert rr.is_subtyped("entity") is False


def test_is_subtyped_false_for_another_bare_string_kind() -> None:
    """is_subtyped('asset') is False when asset is a bare string."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    assert rr.is_subtyped("asset") is False


def test_is_subtyped_raises_key_error_for_unregistered_kind() -> None:
    """is_subtyped raises KeyError for a kind not in the registry."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    with pytest.raises(KeyError):
        rr.is_subtyped("no_such_kind")


# ---------------------------------------------------------------------------
# RecordRoles.role_of() — bare-string kinds
# ---------------------------------------------------------------------------


def test_role_of_bare_string_kind_with_none_sub_type() -> None:
    """role_of('entity', None) returns 'dimension' for a bare-string kind."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    assert rr.role_of("entity", None) == "dimension"


def test_role_of_bare_string_kind_ignores_sub_type() -> None:
    """role_of on a bare-string kind ignores sub_type — any value returns the role."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    assert rr.role_of("entity", "anything") == "dimension"


def test_role_of_bare_string_kind_fact() -> None:
    """role_of('asset', None) returns 'fact' for a bare-string fact kind."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    assert rr.role_of("asset", None) == "fact"


# ---------------------------------------------------------------------------
# RecordRoles.role_of() — object-valued kinds
# ---------------------------------------------------------------------------


def test_role_of_object_kind_with_declared_sub_type() -> None:
    """role_of('actor', 'trip') returns 'fact' for a declared sub-type."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    assert rr.role_of("actor", "trip") == "fact"


def test_role_of_object_kind_dimension_sub_type() -> None:
    """role_of('actor', 'staff') returns 'dimension' for a dimension sub-type."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    assert rr.role_of("actor", "staff") == "dimension"


def test_role_of_object_kind_with_none_sub_type_raises_value_error() -> None:
    """role_of on an object-valued kind with sub_type=None raises ValueError."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    with pytest.raises(ValueError, match="sub_type"):
        rr.role_of("actor", None)


def test_role_of_object_kind_with_undeclared_sub_type_raises_key_error() -> None:
    """role_of on an object-valued kind with an undeclared sub_type raises KeyError."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    with pytest.raises(KeyError):
        rr.role_of("actor", "no_such_sub_type")


# ---------------------------------------------------------------------------
# RecordRoles.role_of() / is_subtyped() — unregistered kind
# ---------------------------------------------------------------------------


def test_role_of_unregistered_kind_raises_key_error() -> None:
    """role_of raises KeyError when the kind is not in the registry."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    with pytest.raises(KeyError):
        rr.role_of("phantom", None)


# ---------------------------------------------------------------------------
# RecordRoles — extra sub-types declared beyond data coverage
# ---------------------------------------------------------------------------


def test_role_of_resolves_for_every_declared_sub_type() -> None:
    """An actor object with more sub-types than appear in data still resolves for all."""
    raw = _minimal_raw(
        record_roles={
            "actor": {
                "trip": "fact",
                "visit": "fact",
                "staff": "dimension",
                "extra_subtype": "dimension",
            }
        }
    )
    sidecar = Sidecar.from_raw(raw)
    rr = sidecar.record_roles()
    assert rr is not None
    assert rr.role_of("actor", "extra_subtype") == "dimension"


# ---------------------------------------------------------------------------
# RecordRoles.sub_types()
# ---------------------------------------------------------------------------


def test_sub_types_returns_declared_sub_types_in_order() -> None:
    """sub_types('actor') returns declared sub-type names in declaration order."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    assert rr.sub_types("actor") == ("trip", "visit", "staff")


def test_sub_types_returns_tuple() -> None:
    """sub_types returns a tuple, not a list."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    assert isinstance(rr.sub_types("actor"), tuple)


def test_sub_types_raises_key_error_for_unknown_kind() -> None:
    """sub_types raises KeyError for a kind not in the registry."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    with pytest.raises(KeyError):
        rr.sub_types("no_such_kind")


def test_sub_types_raises_value_error_for_bare_string_kind() -> None:
    """sub_types raises ValueError for a bare-string (non-subtyped) kind."""
    rr = _sidecar_with_record_roles().record_roles()
    assert rr is not None
    with pytest.raises(ValueError):
        rr.sub_types("entity")


# ---------------------------------------------------------------------------
# RecordRoles — malformed/absent record_roles block parses cleanly
# ---------------------------------------------------------------------------


def test_record_roles_absent_key_parses_cleanly() -> None:
    """A sidecar without record_roles parses without error; accessor returns None."""
    sidecar = Sidecar.from_raw(_minimal_raw())
    assert sidecar.record_roles() is None


def test_record_roles_non_dict_value_parses_cleanly() -> None:
    """A sidecar with record_roles as a non-dict parses cleanly; accessor returns None."""
    raw = _minimal_raw(record_roles="not-a-dict")
    sidecar = Sidecar.from_raw(raw)
    assert sidecar.record_roles() is None


# ---------------------------------------------------------------------------
# Sidecar.subtype_values() — the discriminator oracle
# ---------------------------------------------------------------------------


def _sidecar_with_enum_domains() -> Sidecar:
    """Build a Sidecar with a populated enum_domains block.

    Contains:
    - actor: has actor_type (sub-typed, object-valued role)
    - entity: has entity_type (sub-typed, bare-string role)
    - resource: has only a 'status' domain (no resource_type — not sub-typed)
    """
    raw = _minimal_raw(
        enum_domains={
            "actor": {
                "actor_type": ["trip", "visit", "staff"],
                "actor_status": ["active", "inactive"],
            },
            "entity": {
                "entity_type": ["dimension_a", "dimension_b"],
            },
            "resource": {
                "status": ["available", "in_use"],
            },
        },
        record_roles={
            "actor": {"trip": "fact", "visit": "fact", "staff": "dimension"},
            "entity": "dimension",
            "resource": "fact",
        },
    )
    return Sidecar.from_raw(raw)


def test_subtype_values_returns_declared_values_for_subtyped_kind() -> None:
    """subtype_values returns declared actor_type values for actor."""
    sidecar = _sidecar_with_enum_domains()
    assert sidecar.subtype_values("actor") == ("trip", "visit", "staff")


def test_subtype_values_returns_values_in_declaration_order() -> None:
    """subtype_values preserves enum_domains declaration order, not lexicographic."""
    sidecar = _sidecar_with_enum_domains()
    result = sidecar.subtype_values("actor")
    assert result != tuple(sorted(result))


def test_subtype_values_bare_role_kind_with_type_domain_returns_values() -> None:
    """A bare-string-role kind (entity) with entity_type domain returns its values."""
    sidecar = _sidecar_with_enum_domains()
    assert sidecar.subtype_values("entity") == ("dimension_a", "dimension_b")


def test_subtype_values_kind_with_no_type_key_returns_empty() -> None:
    """A kind in enum_domains but without a <kind>_type key returns ()."""
    sidecar = _sidecar_with_enum_domains()
    assert sidecar.subtype_values("resource") == ()


def test_subtype_values_unknown_kind_returns_empty() -> None:
    """An unknown kind (absent from enum_domains) returns () without raising."""
    sidecar = _sidecar_with_enum_domains()
    assert sidecar.subtype_values("no_such_kind") == ()


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("actor", id="known-kind"),
        pytest.param("phantom", id="unknown-kind"),
    ],
)
def test_subtype_values_absent_enum_domains_returns_empty(kind: str) -> None:
    """A sidecar with no enum_domains block returns () for any kind."""
    sidecar = Sidecar.from_raw(_minimal_raw())
    assert sidecar.subtype_values(kind) == ()


# ---------------------------------------------------------------------------
# Sidecar.sub_type_columns() accessor + SubTypeColumns view
# ---------------------------------------------------------------------------


def _sub_type_columns_raw() -> dict[str, object]:
    """A sub_type_columns block: one sub-typed kind (actor), three sub-types.

    'staff' owns two value columns (one of them a reference, so its
    ref_index sibling is listed too); 'trip' owns one; 'visit' owns none
    (all-collection-struct sub-type -> empty list, key retained).
    """
    return {
        "actor": {
            "staff": ["prop__doctor", "ref_index__doctor", "prop__salary"],
            "trip": ["prop__distance"],
            "visit": [],
        },
    }


def _sidecar_with_sub_type_columns() -> Sidecar:
    """Build a Sidecar carrying a sub_type_columns block."""
    return Sidecar.from_raw(_minimal_raw(sub_type_columns=_sub_type_columns_raw()))


def test_sub_type_columns_absent_returns_none() -> None:
    """sub_type_columns() is None when the sidecar omits the block."""
    sidecar = Sidecar.from_raw(_minimal_raw())
    assert sidecar.sub_type_columns() is None


def test_sub_type_columns_non_dict_value_returns_none() -> None:
    """A non-dict sub_type_columns parses cleanly; accessor returns None."""
    sidecar = Sidecar.from_raw(_minimal_raw(sub_type_columns="not-a-dict"))
    assert sidecar.sub_type_columns() is None


def test_sub_type_columns_present_returns_instance() -> None:
    """sub_type_columns() returns a SubTypeColumns view when the block is present."""
    assert isinstance(
        _sidecar_with_sub_type_columns().sub_type_columns(), SubTypeColumns
    )


def test_sub_type_columns_kinds_returns_partitioned_kinds() -> None:
    """kinds() returns the sub-typed kinds carried by the partition, as a tuple."""
    stc = _sidecar_with_sub_type_columns().sub_type_columns()
    assert stc is not None
    assert stc.kinds() == ("actor",)


def test_sub_type_columns_sub_types_returns_declared_sub_types() -> None:
    """sub_types(kind) returns every declared sub-type, empty-list ones included."""
    stc = _sidecar_with_sub_type_columns().sub_type_columns()
    assert stc is not None
    assert stc.sub_types("actor") == ("staff", "trip", "visit")


def test_sub_type_columns_columns_for_returns_owned_columns() -> None:
    """columns_for returns a sub-type's owned columns in declared order."""
    stc = _sidecar_with_sub_type_columns().sub_type_columns()
    assert stc is not None
    assert stc.columns_for("actor", "staff") == (
        "prop__doctor",
        "ref_index__doctor",
        "prop__salary",
    )


def test_sub_type_columns_columns_for_empty_list_is_preserved() -> None:
    """An all-collection-struct sub-type keeps its key with an empty tuple."""
    stc = _sidecar_with_sub_type_columns().sub_type_columns()
    assert stc is not None
    assert stc.columns_for("actor", "visit") == ()


def test_sub_type_columns_unknown_kind_raises_keyerror() -> None:
    """columns_for on an unregistered kind raises KeyError."""
    stc = _sidecar_with_sub_type_columns().sub_type_columns()
    assert stc is not None
    with pytest.raises(KeyError):
        stc.columns_for("no_such_kind", "staff")


def test_sub_type_columns_unknown_sub_type_raises_keyerror() -> None:
    """columns_for on an undeclared sub-type raises KeyError."""
    stc = _sidecar_with_sub_type_columns().sub_type_columns()
    assert stc is not None
    with pytest.raises(KeyError):
        stc.columns_for("actor", "no_such_sub_type")


def test_sub_type_columns_present_but_empty_partition_is_not_none() -> None:
    """A present-but-empty block yields a view (not None), so absence stays distinct."""
    sidecar = Sidecar.from_raw(_minimal_raw(sub_type_columns={}))
    stc = sidecar.sub_type_columns()
    assert isinstance(stc, SubTypeColumns)
    assert stc.kinds() == ()
