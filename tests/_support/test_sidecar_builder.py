"""Tests for the one sidecar-fixture authority: prop_column, identity_column,
key_space, partition_key, and write_emit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from _support.sidecar_builder import (
    UNSUPPORTED_VERSION_SENTINEL,
    identity_column,
    key_space,
    partition_key,
    prop_column,
    write_emit,
)
from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION

if TYPE_CHECKING:
    from _support.sidecar_builder import KeySpaceClass

_FIRINGS_TABLE: dict[str, object] = {
    "name": "firings",
    "category": "fixed",
    "columns": [
        {"name": "fork_path", "type": "VARCHAR"},
        {"name": "sim_time", "type": "BIGINT"},
    ],
    "rows": 0,
}

# A well-formed records__actor column list: head + lifecycle tail +
# record_index, then a reference-annotated prop__ column immediately followed
# by its ref_index__ sibling (§ Contracts). Shared by the
# write_emit records-shape negatives below, each of which mutates a copy.
_WELL_FORMED_RECORDS_COLUMNS: list[dict[str, object]] = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__doctor_id",
        "VARCHAR",
        history_tracked=False,
        temporal_class="constant",
        references="doctor",
    ),
    identity_column("ref_index__doctor_id", "BIGINT"),
]


def _records_table(name: str, columns: list[dict[str, object]]) -> dict[str, object]:
    """Build a minimal records-category table spec for write_emit's tables list."""
    return {
        "name": name,
        "category": "records",
        "record_kind": "actor",
        "columns": columns,
        "rows": 0,
    }


# ---------------------------------------------------------------------------
# prop_column
# ---------------------------------------------------------------------------


def test_prop_column_rejects_tracked_without_history_tracked() -> None:
    """'tracked' with history_tracked=False raises ValueError."""
    with pytest.raises(ValueError, match="tracked"):
        prop_column(
            "prop__status",
            "VARCHAR",
            history_tracked=False,
            temporal_class="tracked",
        )


def test_prop_column_rejects_slice_only_with_history_tracked() -> None:
    """'slice_only' with history_tracked=True raises ValueError."""
    with pytest.raises(ValueError, match="slice_only"):
        prop_column(
            "prop__insurer",
            "VARCHAR",
            history_tracked=True,
            temporal_class="slice_only",
        )


def test_prop_column_builds_conformant_pair() -> None:
    """A valid pairing builds a column dict carrying both attributes."""
    column = prop_column(
        "prop__patient_id",
        "VARCHAR",
        history_tracked=True,
        temporal_class="constant",
    )
    assert column["history_tracked"] is True
    assert column["temporal_class"] == "constant"
    assert "references" not in column


def test_prop_column_includes_references_when_given() -> None:
    """references is present iff explicitly given."""
    column = prop_column(
        "prop__doctor",
        "VARCHAR",
        history_tracked=True,
        temporal_class="constant",
        references="doctor",
    )
    assert column["references"] == "doctor"


# ---------------------------------------------------------------------------
# identity_column
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "duckdb_type"),
    [
        ("fork_path", "VARCHAR"),
        ("record_id", "VARCHAR"),
        ("record_index", "BIGINT"),
        ("ref_index__doctor_id", "BIGINT"),
    ],
)
def test_identity_column_emits_name_and_type_only(name: str, duckdb_type: str) -> None:
    """A name that classifies as identity emits exactly {"name", "type"}."""
    column = identity_column(name, duckdb_type)
    assert column == {"name": name, "type": duckdb_type}


@pytest.mark.parametrize("name", ["prop__x", "created_sim_time"])
def test_identity_column_rejects_non_identity_name(name: str) -> None:
    """A name that does not classify as identity (payload or lifecycle) raises."""
    with pytest.raises(ValueError, match=name):
        identity_column(name, "VARCHAR")


# ---------------------------------------------------------------------------
# key_space / partition_key
# ---------------------------------------------------------------------------


def test_key_space_digit_rendered_without_gaps() -> None:
    """A digit-rendered class carries prefix/width/start and no gaps member."""
    assert key_space("counter", prefix="WARD_", width=3, start=1) == {
        "class": "counter",
        "prefix": "WARD_",
        "width": 3,
        "start": 1,
    }


def test_key_space_digit_rendered_zero_valued_texture_is_present() -> None:
    """Falsy-but-declared texture (empty prefix, width 0, start 0) is rendered."""
    assert key_space("record_index", prefix="", width=0, start=0) == {
        "class": "record_index",
        "prefix": "",
        "width": 0,
        "start": 0,
    }


def test_key_space_digit_rendered_with_gaps() -> None:
    """gaps renders as {"rate", "max"} from the (rate, max) pair."""
    assert key_space("record_index", prefix="", width=4, start=1, gaps=(0.25, 3)) == {
        "class": "record_index",
        "prefix": "",
        "width": 4,
        "start": 1,
        "gaps": {"rate": 0.25, "max": 3},
    }


@pytest.mark.parametrize("space_class", ["uuid", "record_id"])
def test_key_space_non_digit_rendered_is_class_only(space_class: KeySpaceClass) -> None:
    """A non-digit-rendered class renders as {"class": ...} alone."""
    assert key_space(space_class) == {"class": space_class}


@pytest.mark.parametrize("space_class", ["counter", "record_index"])
@pytest.mark.parametrize("missing", ["prefix", "width", "start"])
def test_key_space_digit_rendered_missing_texture_member_raises(
    space_class: KeySpaceClass, missing: str
) -> None:
    """A digit-rendered class missing any of prefix/width/start raises, naming it."""
    texture: dict[str, Any] = {"prefix": "P_", "width": 3, "start": 1}
    del texture[missing]
    with pytest.raises(ValueError, match=missing):
        key_space(space_class, **texture)


@pytest.mark.parametrize("space_class", ["uuid", "record_id"])
@pytest.mark.parametrize(
    "texture",
    [
        {"prefix": "P_"},
        {"width": 3},
        {"start": 1},
        {"gaps": (0.1, 2)},
    ],
    ids=["prefix", "width", "start", "gaps"],
)
def test_key_space_non_digit_rendered_with_texture_raises(
    space_class: KeySpaceClass, texture: dict[str, Any]
) -> None:
    """A non-digit-rendered class given any texture member raises, naming it."""
    with pytest.raises(ValueError, match=next(iter(texture))):
        key_space(space_class, **texture)


def test_partition_key_counter_derives_emit_unstable_scalars() -> None:
    """'counter' -> emit / branch_stable False / slice_stable False."""
    space = key_space("counter", prefix="", width=0, start=1)
    assert partition_key(space) == {
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
        "key_space": space,
    }


@pytest.mark.parametrize(
    "space",
    [
        key_space("record_index", prefix="", width=0, start=1),
        key_space("uuid"),
        key_space("record_id"),
    ],
    ids=["record_index", "uuid", "record_id"],
)
def test_partition_key_stable_classes_derive_branch_stable_scalars(
    space: dict[str, object],
) -> None:
    """Every non-counter class -> branch / True / True."""
    assert partition_key(space) == {
        "unique_within": "branch",
        "branch_stable": True,
        "slice_stable": True,
        "key_space": space,
    }


@pytest.mark.parametrize("space", [{}, {"class": "bogus"}], ids=["absent", "unknown"])
def test_partition_key_unrecognized_class_raises(space: dict[str, object]) -> None:
    """A key_space with no recognized class raises ValueError."""
    with pytest.raises(ValueError, match="class"):
        partition_key(space)


def test_write_emit_accepts_constructor_built_presentation_keys(
    tmp_path: Path,
) -> None:
    """Constructor output, texture and gaps included, passes the schema net
    inside a presentation_keys block and is carried verbatim."""
    entry = partition_key(
        key_space("record_index", prefix="WARD_", width=3, start=1, gaps=(0.1, 2))
    )
    write_emit(
        tmp_path,
        tables=[_FIRINGS_TABLE],
        extra={"presentation_keys": {"ward": {"key": entry}}},
    )
    sidecar = json.loads((tmp_path / "base.json").read_text(encoding="utf-8"))
    assert sidecar["presentation_keys"]["ward"]["key"] == entry


def test_write_emit_rejects_hand_built_key_space_missing_texture(
    tmp_path: Path,
) -> None:
    """A digit-rendered key_space missing `start` (mutated from the constructor) fails the schema net at
    construction -- the constructors are the only conformant path."""
    space = key_space("counter", prefix="", width=0, start=1)
    del space["start"]
    hand_built = partition_key(space)
    with pytest.raises(ValueError, match="start"):
        write_emit(
            tmp_path,
            tables=[_FIRINGS_TABLE],
            extra={"presentation_keys": {"ward": {"key": hand_built}}},
        )
    assert not (tmp_path / "base.json").exists()


# ---------------------------------------------------------------------------
# write_emit
# ---------------------------------------------------------------------------


def test_write_emit_default_stamps_supported_version(tmp_path: Path) -> None:
    """Omitting base_format_version stamps SUPPORTED_BASE_FORMAT_VERSION."""
    write_emit(tmp_path, tables=[_FIRINGS_TABLE])
    sidecar = json.loads((tmp_path / "base.json").read_text(encoding="utf-8"))
    assert sidecar["base_format_version"] == SUPPORTED_BASE_FORMAT_VERSION


def test_write_emit_schema_valid_rejects_missing_required_field(
    tmp_path: Path,
) -> None:
    """schema_valid=True names the missing required field at construction time."""
    broken_table: dict[str, object] = {
        "name": "firings",
        "category": "fixed",
        "columns": [{"name": "fork_path", "type": "VARCHAR"}],
        # 'rows' omitted -- schema-required.
    }
    with pytest.raises(ValueError, match="rows"):
        write_emit(tmp_path, tables=[broken_table], schema_valid=True)
    assert not (tmp_path / "base.json").exists()


def test_write_emit_schema_valid_false_writes_schema_invalid_sidecar(
    tmp_path: Path,
) -> None:
    """schema_valid=False writes a fixture whose declared defect is schema-level."""
    write_emit(
        tmp_path,
        tables=[_FIRINGS_TABLE],
        base_format_version=UNSUPPORTED_VERSION_SENTINEL,
        schema_valid=False,
    )
    sidecar = json.loads((tmp_path / "base.json").read_text(encoding="utf-8"))
    assert sidecar["base_format_version"] == UNSUPPORTED_VERSION_SENTINEL


def test_write_emit_extra_blocks_carried_verbatim(tmp_path: Path) -> None:
    """extra top-level blocks (e.g. record_roles) are carried verbatim."""
    write_emit(
        tmp_path,
        tables=[_FIRINGS_TABLE],
        extra={"record_roles": {"entity": "dimension"}},
    )
    sidecar = json.loads((tmp_path / "base.json").read_text(encoding="utf-8"))
    assert sidecar["record_roles"] == {"entity": "dimension"}


# ---------------------------------------------------------------------------
# write_emit -- records-shape assertion (records_shape_valid)
# ---------------------------------------------------------------------------


def test_write_emit_rejects_records_table_missing_record_index(
    tmp_path: Path,
) -> None:
    """A records table with no record_index names the table and column."""
    columns = [c for c in _WELL_FORMED_RECORDS_COLUMNS if c["name"] != "record_index"]
    table = _records_table("records__actor", columns)
    with pytest.raises(ValueError, match="records__actor") as excinfo:
        write_emit(tmp_path, tables=[table])
    assert "record_index" in str(excinfo.value)
    assert not (tmp_path / "base.json").exists()


def test_write_emit_rejects_reference_prop_without_ref_index_sibling(
    tmp_path: Path,
) -> None:
    """A reference-annotated prop__ column without its ref_index__ sibling
    names the table and the missing sibling column."""
    columns = [
        c for c in _WELL_FORMED_RECORDS_COLUMNS if c["name"] != "ref_index__doctor_id"
    ]
    table = _records_table("records__actor", columns)
    with pytest.raises(ValueError, match="records__actor") as excinfo:
        write_emit(tmp_path, tables=[table])
    assert "ref_index__doctor_id" in str(excinfo.value)
    assert not (tmp_path / "base.json").exists()


def test_write_emit_rejects_no_role_column(tmp_path: Path) -> None:
    """A column matching no records-column taxonomy role names the table and
    column."""
    columns = [
        *_WELL_FORMED_RECORDS_COLUMNS,
        {"name": "bogus_column", "type": "VARCHAR"},
    ]
    table = _records_table("records__actor", columns)
    with pytest.raises(ValueError, match="records__actor") as excinfo:
        write_emit(tmp_path, tables=[table])
    assert "bogus_column" in str(excinfo.value)
    assert not (tmp_path / "base.json").exists()


def test_write_emit_records_shape_valid_false_bypasses_shape_net(
    tmp_path: Path,
) -> None:
    """records_shape_valid=False writes a shape-defective records table
    without touching schema_valid (still True by default here)."""
    columns = [c for c in _WELL_FORMED_RECORDS_COLUMNS if c["name"] != "record_index"]
    table = _records_table("records__actor", columns)
    write_emit(tmp_path, tables=[table], records_shape_valid=False)
    sidecar = json.loads((tmp_path / "base.json").read_text(encoding="utf-8"))
    written_columns = sidecar["tables"][0]["columns"]
    assert all(c["name"] != "record_index" for c in written_columns)
