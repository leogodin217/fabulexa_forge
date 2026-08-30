"""The vendored base-layer contract is present, well-formed, and covers the format
version this package supports."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from _support.sidecar_builder import identity_column

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION

_CONTRACT = Path(__file__).resolve().parent.parent / "contract"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_SIDECAR = {
    "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
    "surface": "published",
    "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
    "tables": [
        {
            "name": "history",
            "category": "fixed",
            "columns": [identity_column("fork_path", "VARCHAR")],
            "rows": 0,
        }
    ],
}

_RECORD_ROLES_BLOCK = {
    "actor": {"trip": "fact", "visit": "fact", "staff": "dimension"},
    "entity": "dimension",
    "asset": "fact",
}

_SUB_TYPE_COLUMNS_BLOCK = {
    "actor": {
        "staff": ["prop__doctor", "ref_index__doctor", "prop__salary"],
        "trip": ["prop__distance"],
        "visit": [],
    },
}


def _get_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads((_CONTRACT / "base-format.schema.json").read_text())
    return jsonschema.Draft202012Validator(schema)


# ---------------------------------------------------------------------------
# File-presence and schema-validity
# ---------------------------------------------------------------------------


def test_vendored_files_present() -> None:
    assert (_CONTRACT / "base-format.schema.json").is_file()
    assert (_CONTRACT / "base-format.md").is_file()


def test_schema_is_a_valid_json_schema() -> None:
    schema = json.loads((_CONTRACT / "base-format.schema.json").read_text())
    # Raises SchemaError if the vendored schema is not itself a valid JSON Schema.
    jsonschema.Draft202012Validator.check_schema(schema)


def test_schema_covers_supported_version() -> None:
    """A minimal sidecar at the supported version validates; an empty object does not.

    Guards that SUPPORTED_BASE_FORMAT_VERSION and the vendored schema agree, without
    duplicating the schema's field list here.
    """
    validator = _get_validator()
    validator.validate(_MINIMAL_SIDECAR)

    with pytest.raises(jsonschema.ValidationError):
        validator.validate({})


# ---------------------------------------------------------------------------
# record_roles: valid cases
# ---------------------------------------------------------------------------


def test_sidecar_without_record_roles_validates() -> None:
    """A sidecar with no record_roles key is valid (field is optional)."""
    validator = _get_validator()
    validator.validate(_MINIMAL_SIDECAR)


def test_sidecar_with_well_formed_record_roles_validates() -> None:
    """A sidecar with a well-formed record_roles block (actor + bare strings) validates."""
    validator = _get_validator()
    sidecar = {**_MINIMAL_SIDECAR, "record_roles": _RECORD_ROLES_BLOCK}
    validator.validate(sidecar)


def test_sidecar_with_bare_string_only_record_roles_validates() -> None:
    """A record_roles block with only bare-string kinds (no actor) validates."""
    validator = _get_validator()
    sidecar = {
        **_MINIMAL_SIDECAR,
        "record_roles": {"entity": "dimension", "asset": "fact"},
    }
    validator.validate(sidecar)


# ---------------------------------------------------------------------------
# record_roles: invalid cases
# ---------------------------------------------------------------------------


def test_record_roles_invalid_role_string_fails() -> None:
    """A record_roles value outside {'dimension', 'fact'} fails schema validation."""
    validator = _get_validator()
    bad_roles = {**_RECORD_ROLES_BLOCK, "entity": "unknown_role"}
    sidecar = {**_MINIMAL_SIDECAR, "record_roles": bad_roles}
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(sidecar)


def test_record_roles_value_as_integer_fails() -> None:
    """A record_roles value that is neither a string nor an object fails validation."""
    validator = _get_validator()
    bad_roles = {"entity": 42}
    sidecar = {**_MINIMAL_SIDECAR, "record_roles": bad_roles}
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(sidecar)


def test_record_roles_actor_sub_type_invalid_role_fails() -> None:
    """An actor sub-type with a role outside {'dimension', 'fact'} fails validation."""
    validator = _get_validator()
    bad_roles = {"actor": {"trip": "bad_role"}, "entity": "dimension"}
    sidecar = {**_MINIMAL_SIDECAR, "record_roles": bad_roles}
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(sidecar)


# ---------------------------------------------------------------------------
# sub_type_columns: valid cases
# ---------------------------------------------------------------------------


def test_sidecar_without_sub_type_columns_validates() -> None:
    """A sidecar with no sub_type_columns key is valid (field is optional)."""
    _get_validator().validate(_MINIMAL_SIDECAR)


def test_sidecar_with_well_formed_sub_type_columns_validates() -> None:
    """A well-formed sub_type_columns block (incl. an empty per-sub-type list) validates."""
    validator = _get_validator()
    sidecar = {**_MINIMAL_SIDECAR, "sub_type_columns": _SUB_TYPE_COLUMNS_BLOCK}
    validator.validate(sidecar)


# ---------------------------------------------------------------------------
# sub_type_columns: invalid cases
# ---------------------------------------------------------------------------


def test_sub_type_columns_non_string_column_fails() -> None:
    """A column entry that is not a string fails schema validation."""
    validator = _get_validator()
    bad = {"actor": {"staff": ["prop__doctor", 42]}}
    sidecar = {**_MINIMAL_SIDECAR, "sub_type_columns": bad}
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(sidecar)


def test_sub_type_columns_empty_sub_type_map_fails() -> None:
    """A kind mapping to an empty sub-type object fails (minProperties: 1)."""
    validator = _get_validator()
    bad = {"actor": {}}
    sidecar = {**_MINIMAL_SIDECAR, "sub_type_columns": bad}
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(sidecar)
