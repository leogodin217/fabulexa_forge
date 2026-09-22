"""Tests for the date-dimension config grammar: `DateDimensionConfig`,
`DateRefSpec`, and the `ColumnDecl` / `ExportConfig` deltas that admit them
(date-dimension sprint Phase 1). Compilation (`date_ref` rendering, the
generated calendar) is inert until Phase 2 — this module tests parsing only.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from fabulexa_forge.config.models import (
    ColumnDecl,
    DateDimensionConfig,
    DateRefSpec,
    ExportConfig,
)

_MINIMAL_DIMENSIONAL = {
    "tables": [
        {
            "name": "dim_x",
            "role": "dim",
            "scd": "type1",
            "source": {"grain": "records", "kind": "actor"},
            "key": ["id"],
            "columns": [{"name": "id", "from": "record_id"}],
        }
    ]
}


def _dimensional_with_date_ref_column() -> dict[str, object]:
    """A minimal dimensional block whose sole table carries a `date_ref` column."""
    return {
        "tables": [
            {
                "name": "fact_visit",
                "role": "fact",
                "source": {"grain": "records", "kind": "visit"},
                "key": ["id"],
                "columns": [
                    {"name": "id", "from": "record_id"},
                    {"name": "visit_date_key", "date_ref": {"source": "sim_time"}},
                ],
            }
        ]
    }


# ---------------------------------------------------------------------------
# DateDimensionConfig
# ---------------------------------------------------------------------------


def test_date_dimension_config_quoted_iso_strings_parse() -> None:
    """Quoted ISO strings parse to `date` on both bounds."""
    cfg = DateDimensionConfig.model_validate({"from": "1985-01-01", "to": "2024-12-31"})
    assert cfg.from_ == date(1985, 1, 1)
    assert cfg.to == date(2024, 12, 31)


def test_date_dimension_config_from_equals_to_accepted() -> None:
    """A single-day range (`from == to`) is accepted."""
    cfg = DateDimensionConfig.model_validate({"from": "2024-01-01", "to": "2024-01-01"})
    assert cfg.from_ == cfg.to


def test_date_dimension_config_unquoted_yaml_date_refused() -> None:
    """A YAML loader-resolved `date` object (unquoted date) is refused, naming
    the string rule rather than failing on a type-union message."""
    with pytest.raises(ValidationError, match="quoted ISO date string"):
        DateDimensionConfig.model_validate(
            {"from": date(2024, 1, 1), "to": "2024-12-31"}
        )


def test_date_dimension_config_integer_bound_refused() -> None:
    """An integer bound (which `date` would otherwise read as a Unix
    timestamp) is refused by the same string rule."""
    with pytest.raises(ValidationError, match="quoted ISO date string"):
        DateDimensionConfig.model_validate({"from": "2024-01-01", "to": 1735689600})


def test_date_dimension_config_to_before_from_refused() -> None:
    """`to` before `from_` is refused."""
    with pytest.raises(ValidationError, match="before"):
        DateDimensionConfig.model_validate({"from": "2024-12-31", "to": "2024-01-01"})


def test_date_dimension_config_model_dump_json_carries_iso_strings() -> None:
    """`model_dump(mode="json")` serializes `from_` as `from_` and both
    bounds as ISO strings (pre-existing `DateParseSpec.from_` posture)."""
    cfg = DateDimensionConfig.model_validate({"from": "1985-01-01", "to": "2024-12-31"})
    dumped = cfg.model_dump(mode="json")
    assert dumped == {"from_": "1985-01-01", "to": "2024-12-31"}


# ---------------------------------------------------------------------------
# DateRefSpec
# ---------------------------------------------------------------------------


def test_date_ref_spec_source_shape_accepted() -> None:
    """`{source}` alone is accepted."""
    spec = DateRefSpec.model_validate({"source": "sim_time"})
    assert spec.source == "sim_time"
    assert spec.from_ is None
    assert spec.format is None


def test_date_ref_spec_parse_shape_date_format_accepted() -> None:
    """`{from, format: "%Y-%m-%d"}` (DATE denotation) is accepted."""
    spec = DateRefSpec.model_validate({"from": "dob_text", "format": "%Y-%m-%d"})
    assert spec.from_ == "dob_text"
    assert spec.format == "%Y-%m-%d"


def test_date_ref_spec_parse_shape_naive_timestamp_format_accepted() -> None:
    """`{from, format: "%d/%m/%Y %H:%M"}` (naive TIMESTAMP denotation) is
    accepted — date_ref only refuses a TIME-only (not date-complete) format."""
    spec = DateRefSpec.model_validate(
        {"from": "logged_at_text", "format": "%d/%m/%Y %H:%M"}
    )
    assert spec.format == "%d/%m/%Y %H:%M"


def test_date_ref_spec_neither_shape_refused() -> None:
    """Neither `source` nor `from` set is refused."""
    with pytest.raises(ValidationError, match="exactly one of"):
        DateRefSpec.model_validate({})


def test_date_ref_spec_both_shapes_refused() -> None:
    """Both `source` and `from` set is refused."""
    with pytest.raises(ValidationError, match="exactly one of"):
        DateRefSpec.model_validate(
            {"source": "sim_time", "from": "dob_text", "format": "%Y-%m-%d"}
        )


def test_date_ref_spec_format_without_from_refused() -> None:
    """`format` set without `from` is refused."""
    with pytest.raises(ValidationError, match="format"):
        DateRefSpec.model_validate({"source": "sim_time", "format": "%Y-%m-%d"})


def test_date_ref_spec_from_without_format_refused() -> None:
    """`from` set without `format` is refused."""
    with pytest.raises(ValidationError, match="format"):
        DateRefSpec.model_validate({"from": "dob_text"})


def test_date_ref_spec_empty_source_refused() -> None:
    """An empty `source` column name is refused."""
    with pytest.raises(ValidationError, match="non-empty"):
        DateRefSpec.model_validate({"source": ""})


def test_date_ref_spec_time_only_format_refused_as_not_date_complete() -> None:
    """A time-only format (`"%H:%M"`) is refused — a TIME denotation is not
    date-complete."""
    with pytest.raises(ValidationError, match="not a date"):
        DateRefSpec.model_validate({"from": "logged_at_text", "format": "%H:%M"})


def test_date_ref_spec_unknown_directive_refused_by_declared_parse_rules() -> None:
    """An unknown strptime directive is refused by the shared declared-parse
    rules (`validate_date_parse_format`), not a date_ref-local check."""
    with pytest.raises(ValidationError, match="unsupported directive"):
        DateRefSpec.model_validate({"from": "dob_text", "format": "%Y-%m-%Z"})


def test_date_ref_spec_neither_shape_message_lists_scd_window() -> None:
    """The neither-shape refusal message names all three shapes, `scd_window`
    included."""
    with pytest.raises(ValidationError, match=r"'source' / 'from' / 'scd_window'"):
        DateRefSpec.model_validate({})


# ---------------------------------------------------------------------------
# DateRefSpec: bound shape (`scd_window`)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bound", ["valid_from", "valid_to"])
def test_date_ref_spec_bound_shape_accepted(bound: str) -> None:
    """`{scd_window: valid_from}` / `{scd_window: valid_to}` alone is
    accepted; `source`, `from_`, `format` all stay None."""
    spec = DateRefSpec.model_validate({"scd_window": bound})
    assert spec.scd_window == bound
    assert spec.source is None
    assert spec.from_ is None
    assert spec.format is None


def test_date_ref_spec_bound_shape_beside_source_refused() -> None:
    """`scd_window` beside `source` is refused."""
    with pytest.raises(ValidationError, match="exactly one of"):
        DateRefSpec.model_validate({"source": "sim_time", "scd_window": "valid_from"})


def test_date_ref_spec_bound_shape_beside_parse_shape_refused() -> None:
    """`scd_window` beside `from` + `format` is refused."""
    with pytest.raises(ValidationError, match="exactly one of"):
        DateRefSpec.model_validate(
            {"from": "dob_text", "format": "%Y-%m-%d", "scd_window": "valid_to"}
        )


def test_date_ref_spec_all_three_shapes_refused() -> None:
    """`source`, `from` + `format`, and `scd_window` all set at once is
    refused, the message naming all three values."""
    with pytest.raises(
        ValidationError,
        match=r"got source='sim_time', from='dob_text', scd_window='valid_from'",
    ):
        DateRefSpec.model_validate(
            {
                "source": "sim_time",
                "from": "dob_text",
                "format": "%Y-%m-%d",
                "scd_window": "valid_from",
            }
        )


def test_date_ref_spec_bound_shape_outside_literal_refused_as_type_error() -> None:
    """`scd_window: "as_of"` (outside the two-literal set) is a Pydantic type
    refusal, not the `exactly_one_shape` validator."""
    with pytest.raises(ValidationError, match="valid_from.*valid_to"):
        DateRefSpec.model_validate({"scd_window": "as_of"})


def test_date_ref_spec_bound_shape_with_format_and_no_from_refused() -> None:
    """`scd_window` with `format` set and no `from` is refused by the
    format-iff-from rule, not the shape-count rule."""
    with pytest.raises(ValidationError, match="format is required iff 'from'"):
        DateRefSpec.model_validate({"scd_window": "valid_from", "format": "%Y-%m-%d"})


# ---------------------------------------------------------------------------
# ColumnDecl.date_ref
# ---------------------------------------------------------------------------


def test_column_decl_date_ref_alone_accepted() -> None:
    """`date_ref` alone is a valid column mode."""
    col = ColumnDecl.model_validate(
        {"name": "visit_date_key", "date_ref": {"source": "sim_time"}}
    )
    assert col.date_ref is not None
    assert col.date_ref.source == "sim_time"


def test_column_decl_date_ref_and_from_refused_with_seven_mode_message() -> None:
    """`date_ref` + `from` is refused; the message lists all seven modes."""
    with pytest.raises(
        ValidationError, match=r"from/fk/correlation/derived/null/lookup/date_ref"
    ):
        ColumnDecl.model_validate(
            {
                "name": "visit_date_key",
                "from": "record_id",
                "date_ref": {"source": "sim_time"},
            }
        )


def test_column_decl_date_ref_and_derived_refused() -> None:
    """`date_ref` + `derived` is refused."""
    with pytest.raises(ValidationError):
        ColumnDecl.model_validate(
            {
                "name": "visit_date_key",
                "derived": {"ordinal": {"partition_by": "id", "order_by": "id"}},
                "date_ref": {"source": "sim_time"},
            }
        )


def test_column_decl_date_ref_bound_shape_alone_accepted() -> None:
    """`date_ref: {scd_window: valid_from}` alone is a valid column mode."""
    col = ColumnDecl.model_validate(
        {"name": "valid_from_date_key", "date_ref": {"scd_window": "valid_from"}}
    )
    assert col.date_ref is not None
    assert col.date_ref.scd_window == "valid_from"


def test_column_decl_date_ref_bound_shape_and_from_refused_with_seven_mode_message() -> (
    None
):
    """`date_ref` (bound shape) + `from` is refused; the one-mode refusal
    fires the same way for the bound shape as for the instant shape."""
    with pytest.raises(
        ValidationError, match=r"from/fk/correlation/derived/null/lookup/date_ref"
    ):
        ColumnDecl.model_validate(
            {
                "name": "valid_from_date_key",
                "from": "record_id",
                "date_ref": {"scd_window": "valid_from"},
            }
        )


# ---------------------------------------------------------------------------
# ExportConfig deltas: date_dimension + date_ref business rules
# ---------------------------------------------------------------------------


def test_export_config_date_dimension_under_mode_source_refused() -> None:
    """`date_dimension` under mode='source' is refused."""
    with pytest.raises(ValidationError, match="date_dimension requires mode"):
        ExportConfig.model_validate(
            {
                "mode": "source",
                "source": {"tables": [{"name": "actors", "kind": "actor"}]},
                "date_dimension": {"from": "2024-01-01", "to": "2024-12-31"},
            }
        )


def test_export_config_date_dimension_under_mode_base_refused() -> None:
    """`date_dimension` under mode='base' is refused."""
    with pytest.raises(ValidationError, match="date_dimension requires mode"):
        ExportConfig.model_validate(
            {
                "mode": "base",
                "date_dimension": {"from": "2024-01-01", "to": "2024-12-31"},
            }
        )


def test_export_config_date_ref_without_block_refused_naming_table_and_column() -> None:
    """A `date_ref` column in a dimensional table without `date_dimension` is
    refused, naming the table and column."""
    with pytest.raises(
        ValidationError, match="table 'fact_visit' column 'visit_date_key'"
    ):
        ExportConfig.model_validate(
            {
                "mode": "dimensional",
                "dimensional": _dimensional_with_date_ref_column(),
            }
        )


def test_export_config_date_dimension_with_dim_date_table_refused() -> None:
    """With `date_dimension` present, a declared table named `dim_date` is
    refused (the generated calendar's reserved name)."""
    dimensional = {
        "tables": [
            {
                "name": "dim_date",
                "role": "dim",
                "source": {"grain": "records", "kind": "actor"},
                "key": ["id"],
                "columns": [{"name": "id", "from": "record_id"}],
            }
        ]
    }
    with pytest.raises(ValidationError, match="dim_date"):
        ExportConfig.model_validate(
            {
                "mode": "dimensional",
                "dimensional": dimensional,
                "date_dimension": {"from": "2024-01-01", "to": "2024-12-31"},
            }
        )


def test_export_config_date_dimension_with_dim_date_supplement_refused() -> None:
    """With `date_dimension` present, a supplement named `dim_date` is refused."""
    with pytest.raises(ValidationError, match="dim_date"):
        ExportConfig.model_validate(
            {
                "mode": "dimensional",
                "dimensional": _MINIMAL_DIMENSIONAL,
                "date_dimension": {"from": "2024-01-01", "to": "2024-12-31"},
                "supplements": [
                    {
                        "name": "dim_date",
                        "columns": {"a": "VARCHAR"},
                        "rows": [{"a": "x"}],
                    }
                ],
            }
        )


def test_export_config_dim_date_table_without_block_accepted() -> None:
    """Without `date_dimension`, a table named `dim_date` is the author's to
    use — accepted."""
    dimensional = {
        "tables": [
            {
                "name": "dim_date",
                "role": "dim",
                "source": {"grain": "records", "kind": "actor"},
                "key": ["id"],
                "columns": [{"name": "id", "from": "record_id"}],
            }
        ]
    }
    cfg = ExportConfig.model_validate(
        {"mode": "dimensional", "dimensional": dimensional}
    )
    assert cfg.dimensional is not None
    assert cfg.dimensional.tables[0].name == "dim_date"


def test_export_config_date_dimension_block_with_no_date_ref_anywhere_accepted() -> (
    None
):
    """`date_dimension` present with no `date_ref` column anywhere is accepted."""
    cfg = ExportConfig.model_validate(
        {
            "mode": "dimensional",
            "dimensional": _MINIMAL_DIMENSIONAL,
            "date_dimension": {"from": "2024-01-01", "to": "2024-12-31"},
        }
    )
    assert cfg.date_dimension is not None
    assert cfg.date_dimension.from_ == date(2024, 1, 1)


def test_export_config_date_ref_bound_shape_without_block_refused() -> None:
    """`date_refs_require_date_dimension` fires for the bound shape exactly
    as it does for the instant shape -- it is shape-blind."""
    dimensional = {
        "tables": [
            {
                "name": "dim_company",
                "role": "dim",
                "scd": "type2",
                "source": {"grain": "records", "kind": "company"},
                "key": ["id"],
                "columns": [
                    {"name": "id", "from": "record_id"},
                    {
                        "name": "valid_from_date_key",
                        "date_ref": {"scd_window": "valid_from"},
                    },
                ],
            }
        ]
    }
    with pytest.raises(
        ValidationError, match="table 'dim_company' column 'valid_from_date_key'"
    ):
        ExportConfig.model_validate({"mode": "dimensional", "dimensional": dimensional})


def test_export_config_date_dimension_and_date_ref_together_accepted() -> None:
    """`date_dimension` present with a `date_ref` column is accepted."""
    cfg = ExportConfig.model_validate(
        {
            "mode": "dimensional",
            "dimensional": _dimensional_with_date_ref_column(),
            "date_dimension": {"from": "2024-01-01", "to": "2024-12-31"},
        }
    )
    assert cfg.date_dimension is not None
    assert cfg.dimensional is not None
    table = cfg.dimensional.tables[0]
    assert table.columns[1].date_ref is not None
