"""Tests for supplementary-table declarations: SupplementDecl,
ExportConfig.supplements, and canonical_supplement_type."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from fabulexa_forge.config.models import ExportConfig, canonical_supplement_type


def _yaml_rows(text: str) -> list[dict[str, object]]:
    """Parse a YAML rows list, so an unquoted scalar folds exactly as it
    would from an author's config file (a `date`/`bool` Python object)."""
    return yaml.safe_load(text)


def _dimensional_config(
    supplements: list[dict[str, object]] | None = None,
    extra_table: dict[str, object] | None = None,
) -> dict[str, object]:
    """A minimal mode='dimensional' export config dict, optionally carrying
    a `supplements` list and/or a second declared table."""
    tables: list[dict[str, object]] = [
        {
            "name": "dim_x",
            "role": "dim",
            "scd": "type1",
            "source": {"grain": "records", "kind": "actor"},
            "key": ["id"],
            "columns": [{"name": "id", "from": "record_id"}],
        }
    ]
    if extra_table is not None:
        tables.append(extra_table)
    config: dict[str, object] = {
        "mode": "dimensional",
        "dimensional": {"tables": tables},
    }
    if supplements is not None:
        config["supplements"] = supplements
    return config


def _region_supplement(**overrides: object) -> dict[str, object]:
    """One minimal file-backed supplement declaration."""
    decl: dict[str, object] = {
        "name": "region_code",
        "columns": {"code": "VARCHAR", "label": "VARCHAR"},
        "file": "region_code.csv",
    }
    decl.update(overrides)
    return decl


# ---------------------------------------------------------------------------
# canonical_supplement_type
# ---------------------------------------------------------------------------

_VOCABULARY = (
    "BIGINT",
    "INTEGER",
    "SMALLINT",
    "TINYINT",
    "DOUBLE",
    "FLOAT",
    "BOOLEAN",
    "VARCHAR",
    "TIMESTAMP",
    "DATE",
    "TIME",
    "TIMESTAMPTZ",
)


@pytest.mark.parametrize("type_text", _VOCABULARY)
def test_canonical_supplement_type_round_trips(type_text: str) -> None:
    """Each vocabulary member round-trips upper-cased."""
    assert canonical_supplement_type(type_text) == type_text


def test_canonical_supplement_type_case_and_whitespace_insensitive() -> None:
    """'bigint' / ' Bigint ' both canonicalize to 'BIGINT'."""
    assert canonical_supplement_type("bigint") == "BIGINT"
    assert canonical_supplement_type(" Bigint ") == "BIGINT"


def test_canonical_supplement_type_decimal_canonicalizes() -> None:
    """'decimal(10, 2)' canonicalizes to 'DECIMAL(10,2)'."""
    assert canonical_supplement_type("decimal(10, 2)") == "DECIMAL(10,2)"


@pytest.mark.parametrize("type_text", ["DECIMAL(39,0)", "DECIMAL(5,6)"])
def test_canonical_supplement_type_decimal_bounds_raise(type_text: str) -> None:
    """Out-of-bounds DECIMAL precision/scale raise the bounds message."""
    with pytest.raises(ValueError, match="precision|scale"):
        canonical_supplement_type(type_text)


@pytest.mark.parametrize(
    "type_text", ["INTERVAL", "BLOB", "VARCHAR(10)", "STRUCT(a INT)"]
)
def test_canonical_supplement_type_unrecognized_raises(type_text: str) -> None:
    """A type outside the vocabulary raises."""
    with pytest.raises(ValueError, match="not recognized"):
        canonical_supplement_type(type_text)


# ---------------------------------------------------------------------------
# Parsing: file / inline / empty rows
# ---------------------------------------------------------------------------


def test_file_supplement_parses() -> None:
    """A file supplement parses."""
    config = ExportConfig.model_validate(_dimensional_config([_region_supplement()]))
    assert config.supplements is not None
    assert config.supplements[0].file == "region_code.csv"


def test_inline_supplement_parses() -> None:
    """An inline supplement parses."""
    decl = _region_supplement(
        file=None, rows=[{"code": "US", "label": "United States"}]
    )
    config = ExportConfig.model_validate(_dimensional_config([decl]))
    assert config.supplements is not None
    assert config.supplements[0].rows == [{"code": "US", "label": "United States"}]


def test_inline_supplement_empty_rows_parses() -> None:
    """`rows: []` parses."""
    decl = _region_supplement(file=None, rows=[])
    config = ExportConfig.model_validate(_dimensional_config([decl]))
    assert config.supplements is not None
    assert config.supplements[0].rows == []


# ---------------------------------------------------------------------------
# ExportConfig-level rules
# ---------------------------------------------------------------------------


def test_supplements_under_mode_source_refused() -> None:
    """`supplements` under mode='source' is refused."""
    config: dict[str, object] = {
        "mode": "source",
        "source": {"tables": [{"name": "actors", "kind": "actor"}]},
        "supplements": [_region_supplement()],
    }
    with pytest.raises(ValidationError, match="dimensional"):
        ExportConfig.model_validate(config)


def test_supplements_under_mode_base_refused() -> None:
    """`supplements` under mode='base' is refused."""
    config: dict[str, object] = {"mode": "base", "supplements": [_region_supplement()]}
    with pytest.raises(ValidationError, match="dimensional"):
        ExportConfig.model_validate(config)


def test_supplements_empty_list_refused() -> None:
    """`supplements: []` is refused."""
    with pytest.raises(ValidationError, match="must not be empty"):
        ExportConfig.model_validate(_dimensional_config([]))


def test_supplements_duplicate_name_refused() -> None:
    """Two supplements named alike are refused."""
    decls = [_region_supplement(), _region_supplement()]
    with pytest.raises(ValidationError, match="duplicate name"):
        ExportConfig.model_validate(_dimensional_config(decls))


def test_supplement_name_collides_with_declared_table_refused() -> None:
    """A supplement named like a declared dimensional table is refused with
    the collision message naming both."""
    decl = _region_supplement(name="dim_x")
    with pytest.raises(
        ValidationError, match="dim_x' collides with declared table 'dim_x'"
    ):
        ExportConfig.model_validate(_dimensional_config([decl]))


# ---------------------------------------------------------------------------
# SupplementDecl structural rules
# ---------------------------------------------------------------------------


def test_supplement_name_not_sql_identifier_refused() -> None:
    """`name: "1abc"` is refused."""
    decl = _region_supplement(name="1abc")
    with pytest.raises(ValidationError, match="SQL identifier"):
        ExportConfig.model_validate(_dimensional_config([decl]))


def test_supplement_empty_columns_refused() -> None:
    """`columns: {}` is refused."""
    decl = _region_supplement(columns={})
    with pytest.raises(ValidationError, match="must not be empty"):
        ExportConfig.model_validate(_dimensional_config([decl]))


def test_supplement_column_name_with_hyphen_refused() -> None:
    """A column name with a hyphen is refused."""
    decl = _region_supplement(columns={"region-code": "VARCHAR"})
    with pytest.raises(ValidationError, match="SQL identifier"):
        ExportConfig.model_validate(_dimensional_config([decl]))


def test_supplement_blank_column_type_refused() -> None:
    """A blank type is refused."""
    decl = _region_supplement(columns={"code": "  "})
    with pytest.raises(ValidationError, match="blank type"):
        ExportConfig.model_validate(_dimensional_config([decl]))


def test_supplement_unknown_column_type_refused() -> None:
    """An unknown type is refused with the vocabulary message naming the
    supplement, column, and text."""
    decl = _region_supplement(columns={"code": "INTERVAL"})
    with pytest.raises(
        ValidationError,
        match=r"supplement 'region_code': column 'code' declares type 'INTERVAL'",
    ):
        ExportConfig.model_validate(_dimensional_config([decl]))


def test_supplement_both_sources_refused() -> None:
    """`file` + `rows` is refused."""
    decl = _region_supplement(rows=[{"code": "US", "label": "United States"}])
    with pytest.raises(ValidationError, match="exactly one"):
        ExportConfig.model_validate(_dimensional_config([decl]))


def test_supplement_neither_source_refused() -> None:
    """Neither `file` nor `rows` is refused."""
    decl = _region_supplement(file=None)
    with pytest.raises(ValidationError, match="exactly one"):
        ExportConfig.model_validate(_dimensional_config([decl]))


def test_supplement_blank_file_refused() -> None:
    """`file: "  "` is refused."""
    decl = _region_supplement(file="  ")
    with pytest.raises(ValidationError, match="must not be blank"):
        ExportConfig.model_validate(_dimensional_config([decl]))


def test_supplement_inline_row_missing_key_refused() -> None:
    """An inline row with a missing key is refused, naming the row."""
    decl = _region_supplement(file=None, rows=[{"code": "US"}])
    with pytest.raises(ValidationError, match="row 1"):
        ExportConfig.model_validate(_dimensional_config([decl]))


def test_supplement_inline_row_extra_key_refused() -> None:
    """An inline row with an extra key is refused, naming the row."""
    decl = _region_supplement(
        file=None, rows=[{"code": "US", "label": "United States", "extra": "x"}]
    )
    with pytest.raises(ValidationError, match="row 1"):
        ExportConfig.model_validate(_dimensional_config([decl]))


def test_supplement_unquoted_temporal_row_refused() -> None:
    """Unquoted `2024-01-01` is refused naming the row and column."""
    rows = _yaml_rows("- {code: US, d: 2024-01-01}\n")
    decl = _region_supplement(
        file=None,
        columns={"code": "VARCHAR", "d": "DATE"},
        rows=rows,
    )
    with pytest.raises(
        ValidationError, match=r"quote temporal values \(row 1, column 'd'\)"
    ):
        ExportConfig.model_validate(_dimensional_config([decl]))


@pytest.mark.parametrize("cell", ["yes", "true", "off"])
def test_supplement_unquoted_boolean_row_refused(cell: str) -> None:
    """Unquoted `yes` / `true` / `off` are refused with 'quote boolean values'."""
    rows = _yaml_rows(f"- {{code: US, label: {cell}}}\n")
    decl = _region_supplement(file=None, rows=rows)
    with pytest.raises(ValidationError, match="quote boolean values"):
        ExportConfig.model_validate(_dimensional_config([decl]))


def test_supplement_bool_refused_even_in_integer_column() -> None:
    """A `bool` cell is refused even targeting an INTEGER column."""
    decl = _region_supplement(
        file=None,
        columns={"code": "VARCHAR", "n": "INTEGER"},
        rows=[{"code": "US", "n": True}],
    )
    with pytest.raises(ValidationError, match="quote boolean values"):
        ExportConfig.model_validate(_dimensional_config([decl]))


def test_supplement_blank_description_refused() -> None:
    """A blank `description` is refused."""
    decl = _region_supplement(description="  ")
    with pytest.raises(ValidationError, match="description"):
        ExportConfig.model_validate(_dimensional_config([decl]))


def test_supplement_descriptions_undeclared_key_refused() -> None:
    """A `descriptions` entry naming an undeclared column is refused."""
    decl = _region_supplement(descriptions={"nope": "prose"})
    with pytest.raises(ValidationError, match="not a declared column"):
        ExportConfig.model_validate(_dimensional_config([decl]))


def test_supplement_descriptions_blank_value_refused() -> None:
    """A blank `descriptions` value is refused."""
    decl = _region_supplement(descriptions={"code": "  "})
    with pytest.raises(ValidationError, match="must not be blank"):
        ExportConfig.model_validate(_dimensional_config([decl]))
