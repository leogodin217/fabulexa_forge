"""Tests for the generated calendar's companion surface: manifest format
version 5's `tables[].calendar` / `columns[].references`, and the
forge-pinned `dim_date` / `date_ref` dictionary and README prose."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from exporters.companion._fixtures import write_documented_emit, write_minimal_emit
from fabulexa_forge.config.models import ExportConfig
from fabulexa_forge.exporters.companion import dictionary as dictionary_module
from fabulexa_forge.exporters.companion.dictionary import (
    resolve_column_doc,
    resolve_column_enum_options,
    resolve_table_description,
)
from fabulexa_forge.exporters.companion.manifest import (
    build_manifest_document,
    render_manifest_bytes,
)
from fabulexa_forge.exporters.companion.readme import render_readme
from fabulexa_forge.exporters.date_dimension import (
    DATE_DIMENSION_COLUMNS,
    DATE_DIMENSION_TABLE_NAME,
    CalendarSource,
)
from fabulexa_forge.exporters.query_spec import (
    ColumnProvenance,
    ExportReport,
    TableReport,
)
from fabulexa_forge.reader.emit import open_emit

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fabulexa_forge.reader.documentation import Documentation

_CALENDAR_FROM = date(1985, 1, 1)
_CALENDAR_TO = date(2024, 12, 31)
_DATE_REF_SOURCE_TABLE = "records__actor"


def _dim_date_table_report(*, row_count: int | None = 14610) -> TableReport:
    """The generated calendar's own `TableReport`, over the pinned columns
    and a declared range."""
    return TableReport(
        name=DATE_DIMENSION_TABLE_NAME,
        columns=DATE_DIMENSION_COLUMNS,
        row_count=row_count,
        keys=None,
        provenance={},
        kind_values={},
        author_descriptions={},
        author_table_description=None,
        event_log=False,
        supplement=None,
        calendar=CalendarSource(from_=_CALENDAR_FROM, to=_CALENDAR_TO),
        references={},
        window_bounds={},
    )


def _fk_and_date_ref_table_report(
    *, author_descriptions: "Mapping[str, str] | None" = None
) -> TableReport:
    """One dimensional table carrying a plain carried column, an instant-shape
    `date_ref` column (sourced from `created_sim_time`), and an `fk` column
    (`doctor_fk` -> `dim_doctor`)."""
    return TableReport(
        name="dim_patient",
        columns=(
            ("full_name", "VARCHAR"),
            ("event_date_key", "INTEGER"),
            ("doctor_fk", "VARCHAR"),
        ),
        row_count=1,
        keys=None,
        provenance={
            "full_name": ColumnProvenance(_DATE_REF_SOURCE_TABLE, "prop__full_name"),
            "event_date_key": ColumnProvenance(
                _DATE_REF_SOURCE_TABLE, "created_sim_time"
            ),
            "doctor_fk": ColumnProvenance(_DATE_REF_SOURCE_TABLE, "prop__team_id"),
        },
        kind_values={},
        author_descriptions=author_descriptions or {},
        author_table_description=None,
        event_log=False,
        supplement=None,
        calendar=None,
        references={
            "event_date_key": DATE_DIMENSION_TABLE_NAME,
            "doctor_fk": "dim_doctor",
        },
        window_bounds={},
    )


def _parse_shape_date_ref_table_report() -> TableReport:
    """One table with a parse-shape `date_ref` column sourced from a
    `prop__` string column -- the source-naming prose fixture."""
    return TableReport(
        name="dim_patient",
        columns=(("birth_date_key", "INTEGER"),),
        row_count=1,
        keys=None,
        provenance={
            "birth_date_key": ColumnProvenance(_DATE_REF_SOURCE_TABLE, "prop__dob")
        },
        kind_values={},
        author_descriptions={},
        author_table_description=None,
        event_log=False,
        supplement=None,
        calendar=None,
        references={"birth_date_key": DATE_DIMENSION_TABLE_NAME},
        window_bounds={},
    )


def _bound_shape_date_ref_table_report(
    *,
    bound: "Literal['valid_from', 'valid_to']",
    author_descriptions: "Mapping[str, str] | None" = None,
) -> TableReport:
    """One type-2 dim's bound-shape `date_ref` column report: the column
    carries no provenance entry (it reads no base column) and a
    `window_bounds` entry naming `bound`."""
    column_name = f"{bound}_key"
    return TableReport(
        name="dim_patient_scd",
        columns=((column_name, "INTEGER"),),
        row_count=1,
        keys=None,
        provenance={},
        kind_values={},
        author_descriptions=author_descriptions or {},
        author_table_description=None,
        event_log=False,
        supplement=None,
        calendar=None,
        references={column_name: DATE_DIMENSION_TABLE_NAME},
        window_bounds={column_name: bound},
    )


def _minimal_documentation(tmp_path: Path) -> "Documentation":
    """A bare `Documentation` view over a minimal one-table emit -- no
    dictionary rule under test here reads any of its declared content."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)
    with open_emit(emit_dir) as emit:
        return emit.sidecar.documentation()


# ---------------------------------------------------------------------------
# Dictionary: the pinned `dim_date` table + column prose
# ---------------------------------------------------------------------------


def test_dim_date_table_description_is_pinned(tmp_path: Path) -> None:
    """`dim_date`'s table description resolves to the forge-pinned prose."""
    doc = _minimal_documentation(tmp_path)

    description = resolve_table_description(doc, _dim_date_table_report())

    assert description == dictionary_module._DATE_DIMENSION_TABLE_DESCRIPTION


def test_dim_date_columns_resolve_pinned_forge_prose_with_no_unit(
    tmp_path: Path,
) -> None:
    """Every one of `dim_date`'s 13 columns resolves to its pinned prose,
    origin "forge", no unit, no enum options."""
    doc = _minimal_documentation(tmp_path)
    table = _dim_date_table_report()

    for name, type_text in DATE_DIMENSION_COLUMNS:
        column_doc = resolve_column_doc(doc, table, name, type_text)
        assert column_doc is not None
        assert (
            column_doc.description
            == (dictionary_module._DATE_DIMENSION_COLUMN_DESCRIPTIONS[name])
        )
        assert column_doc.origin == "forge"
        assert column_doc.unit is None
        assert resolve_column_enum_options(doc, table, name) == ()


def test_dim_date_pinned_column_key_set_matches_date_dimension_columns() -> None:
    """The pinned per-column prose names exactly `DATE_DIMENSION_COLUMNS`'
    column names -- no more, no fewer."""
    pinned_names = set(dictionary_module._DATE_DIMENSION_COLUMN_DESCRIPTIONS)
    declared_names = {name for name, _type_text in DATE_DIMENSION_COLUMNS}

    assert pinned_names == declared_names


# ---------------------------------------------------------------------------
# Dictionary: `date_ref` column prose, origin, and the unit-None rule
# ---------------------------------------------------------------------------


def test_date_ref_without_author_description_uses_pinned_forge_prose(
    tmp_path: Path,
) -> None:
    """A `date_ref` column with no author override resolves to the pinned
    template naming its provenance source, origin "forge", no unit."""
    doc = _minimal_documentation(tmp_path)
    table = _fk_and_date_ref_table_report()

    column_doc = resolve_column_doc(doc, table, "event_date_key", "INTEGER")

    assert column_doc is not None
    assert column_doc.description == (
        "Calendar key (`yyyymmdd`) into `dim_date`, derived from `created_sim_time`"
    )
    assert column_doc.origin == "forge"
    assert column_doc.unit is None


def test_date_ref_with_author_description_drops_unit_despite_ns_source(
    tmp_path: Path,
) -> None:
    """An author override on a `date_ref` column renders the author's prose
    at origin "author" with no unit -- even though the source is a raw-`ns`
    column (`created_sim_time`) and the output type is `INTEGER`, an
    unremarkable integer render that would otherwise keep a carried "ns"
    unit."""
    doc = _minimal_documentation(tmp_path)
    table = _fk_and_date_ref_table_report(
        author_descriptions={"event_date_key": "When the status change happened."}
    )

    column_doc = resolve_column_doc(doc, table, "event_date_key", "INTEGER")

    assert column_doc is not None
    assert column_doc.description == "When the status change happened."
    assert column_doc.origin == "author"
    assert column_doc.unit is None


def test_date_ref_parse_shape_prose_names_source_column_only(
    tmp_path: Path,
) -> None:
    """The parse shape's pinned prose names the `prop__` source column and
    nothing else -- no shape or format vocabulary."""
    doc = _minimal_documentation(tmp_path)
    table = _parse_shape_date_ref_table_report()

    column_doc = resolve_column_doc(doc, table, "birth_date_key", "INTEGER")

    assert column_doc is not None
    assert column_doc.description == (
        "Calendar key (`yyyymmdd`) into `dim_date`, derived from `prop__dob`"
    )
    assert "format" not in column_doc.description
    assert "source" not in column_doc.description
    assert column_doc.origin == "forge"
    assert column_doc.unit is None


# ---------------------------------------------------------------------------
# Dictionary: the bound-shape `date_ref` column's pinned prose
# ---------------------------------------------------------------------------


def test_bound_shape_valid_from_uses_pinned_bound_prose(tmp_path: Path) -> None:
    """A bound-shape column with no author override resolves to the pinned
    template naming its `window_bounds` bound, origin "forge", no unit."""
    doc = _minimal_documentation(tmp_path)
    table = _bound_shape_date_ref_table_report(bound="valid_from")

    column_doc = resolve_column_doc(doc, table, "valid_from_key", "INTEGER")

    assert column_doc is not None
    assert column_doc.description == (
        "Calendar key (`yyyymmdd`) into `dim_date`, derived from this"
        " version's `valid_from` bound"
    )
    assert column_doc.origin == "forge"
    assert column_doc.unit is None


def test_bound_shape_valid_to_uses_pinned_bound_prose(tmp_path: Path) -> None:
    """The `valid_to` variant names `valid_to`, not `valid_from`."""
    doc = _minimal_documentation(tmp_path)
    table = _bound_shape_date_ref_table_report(bound="valid_to")

    column_doc = resolve_column_doc(doc, table, "valid_to_key", "INTEGER")

    assert column_doc is not None
    assert column_doc.description == (
        "Calendar key (`yyyymmdd`) into `dim_date`, derived from this"
        " version's `valid_to` bound"
    )
    assert column_doc.origin == "forge"
    assert column_doc.unit is None


def test_bound_shape_with_author_description_wins_over_pinned_prose(
    tmp_path: Path,
) -> None:
    """An author override on a bound-shape column renders the author's
    prose at origin "author" with no unit -- the pinned bound prose never
    renders beside it."""
    doc = _minimal_documentation(tmp_path)
    table = _bound_shape_date_ref_table_report(
        bound="valid_to",
        author_descriptions={"valid_to_key": "When this version stopped applying."},
    )

    column_doc = resolve_column_doc(doc, table, "valid_to_key", "INTEGER")

    assert column_doc is not None
    assert column_doc.description == "When this version stopped applying."
    assert column_doc.origin == "author"
    assert column_doc.unit is None


# ---------------------------------------------------------------------------
# Manifest: tables[].calendar / columns[].references
# ---------------------------------------------------------------------------


def _build_document(emit_dir: Path, report: ExportReport) -> "dict[str, object]":
    with open_emit(emit_dir) as emit:
        return build_manifest_document(
            emit=emit,
            config=ExportConfig(mode="base"),
            fmt="duckdb",
            anchor=None,
            report=report,
            windowed=None,
        )


def test_manifest_format_version_is_5(tmp_path: Path) -> None:
    """`manifest_format_version` reads 5, the format the calendar/references
    fields land in."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)

    document = _build_document(
        emit_dir, ExportReport(tables=(_dim_date_table_report(),))
    )

    assert document["manifest_format_version"] == 5


def test_calendar_present_on_dim_date_and_null_elsewhere(tmp_path: Path) -> None:
    """`tables[].calendar` carries the declared range on `dim_date`; the key
    is present and null on every other table."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_documented_emit(emit_dir)

    document = _build_document(
        emit_dir,
        ExportReport(
            tables=(_fk_and_date_ref_table_report(), _dim_date_table_report())
        ),
    )

    tables = document["tables"]
    assert isinstance(tables, list)
    by_name = {table["name"]: table for table in tables}
    assert by_name["dim_patient"]["calendar"] is None
    assert by_name[DATE_DIMENSION_TABLE_NAME]["calendar"] == {
        "from": "1985-01-01",
        "to": "2024-12-31",
    }


def test_references_on_date_ref_and_fk_columns_null_elsewhere(
    tmp_path: Path,
) -> None:
    """`columns[].references` names `dim_date` on a `date_ref` column, the
    resolved dim on an `fk` column, and null on every other column -- the
    key present on all three."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_documented_emit(emit_dir)

    document = _build_document(
        emit_dir, ExportReport(tables=(_fk_and_date_ref_table_report(),))
    )

    tables = document["tables"]
    assert isinstance(tables, list)
    columns = {column["name"]: column for column in tables[0]["columns"]}
    assert columns["full_name"]["references"] is None
    assert columns["event_date_key"]["references"] == DATE_DIMENSION_TABLE_NAME
    assert columns["doctor_fk"]["references"] == "dim_doctor"


def test_dim_date_supplement_primary_key_unique_are_null(tmp_path: Path) -> None:
    """`dim_date` carries no supplement provenance and no declared keys."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)

    document = _build_document(
        emit_dir, ExportReport(tables=(_dim_date_table_report(),))
    )

    tables = document["tables"]
    assert isinstance(tables, list)
    dim_date_table = tables[0]
    assert dim_date_table["supplement"] is None
    assert dim_date_table["primary_key"] is None
    assert dim_date_table["unique"] is None


def test_bound_shape_column_entry_carries_references_and_pinned_description(
    tmp_path: Path,
) -> None:
    """The bound-shape column's manifest entry carries `references:
    "dim_date"` and the pinned bound-shape description -- the same key set
    as any instant-shape column entry (no shape-specific field)."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_documented_emit(emit_dir)

    document = _build_document(
        emit_dir,
        ExportReport(
            tables=(
                _fk_and_date_ref_table_report(),
                _bound_shape_date_ref_table_report(bound="valid_to"),
            )
        ),
    )

    tables = document["tables"]
    assert isinstance(tables, list)
    by_name = {table["name"]: table for table in tables}
    instant_column = by_name["dim_patient"]["columns"][1]
    bound_column = by_name["dim_patient_scd"]["columns"][0]
    assert bound_column["references"] == DATE_DIMENSION_TABLE_NAME
    assert bound_column["description"] == (
        "Calendar key (`yyyymmdd`) into `dim_date`, derived from this"
        " version's `valid_to` bound"
    )
    assert set(bound_column) == set(instant_column)


def test_manifest_bytes_deterministic_with_calendar_and_references(
    tmp_path: Path,
) -> None:
    """Two renders of a document carrying `calendar`, `references`, and a
    bound-shape `window_bounds` entry are byte-identical."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_documented_emit(emit_dir)
    report = ExportReport(
        tables=(
            _fk_and_date_ref_table_report(),
            _bound_shape_date_ref_table_report(bound="valid_from"),
            _dim_date_table_report(),
        )
    )

    first = render_manifest_bytes(_build_document(emit_dir, report))
    second = render_manifest_bytes(_build_document(emit_dir, report))

    assert first == second


# ---------------------------------------------------------------------------
# README: the `dim_date` section, and unit-free `date_ref` column lines
# ---------------------------------------------------------------------------


def test_readme_renders_dim_date_section_with_pinned_columns(
    tmp_path: Path,
) -> None:
    """A `dim_date` table renders its own section: the pinned table
    description, then its 13 pinned columns in order."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_minimal_emit(emit_dir)

    with open_emit(emit_dir) as emit:
        text = render_readme(
            "dimensional",
            emit,
            ExportReport(tables=(_dim_date_table_report(),)),
            None,
            None,
            "manifest.json",
        )

    assert f"### {DATE_DIMENSION_TABLE_NAME}" in text
    assert dictionary_module._DATE_DIMENSION_TABLE_DESCRIPTION in text
    for name, _type_text in DATE_DIMENSION_COLUMNS:
        assert f"- `{name}` (" in text


def test_readme_date_ref_column_lines_carry_no_unit(tmp_path: Path) -> None:
    """The `date_ref` column line -- pinned or author-described -- renders
    with no `[unit]` suffix."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_documented_emit(emit_dir)

    with open_emit(emit_dir) as emit:
        text = render_readme(
            "dimensional",
            emit,
            ExportReport(
                tables=(
                    _fk_and_date_ref_table_report(
                        author_descriptions={
                            "event_date_key": "When the status change happened."
                        }
                    ),
                )
            ),
            None,
            None,
            "manifest.json",
        )

    expected_line = "- `event_date_key` (INTEGER): When the status change happened."
    assert expected_line in text
    assert "event_date_key` (INTEGER) [" not in text


def test_readme_bound_shape_column_line_renders_pinned_bound_prose(
    tmp_path: Path,
) -> None:
    """A bound-shape column's line renders the pinned bound prose, with no
    `[unit]` suffix."""
    emit_dir = tmp_path / "emit"
    emit_dir.mkdir()
    write_documented_emit(emit_dir)

    with open_emit(emit_dir) as emit:
        text = render_readme(
            "dimensional",
            emit,
            ExportReport(
                tables=(_bound_shape_date_ref_table_report(bound="valid_to"),)
            ),
            None,
            None,
            "manifest.json",
        )

    expected_line = (
        "- `valid_to_key` (INTEGER): Calendar key (`yyyymmdd`) into `dim_date`,"
        " derived from this version's `valid_to` bound"
    )
    assert expected_line in text
    assert "valid_to_key` (INTEGER) [" not in text
