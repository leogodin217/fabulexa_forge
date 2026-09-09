"""Tests for compile_supplement_specs and check_supplement_sources_not_outputs:
the VALUES-and-cast relation, its three before-any-write gates (reserved
name, TIMESTAMPTZ anchor, cell probe), and the source-is-output guard."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from exporters._emit_fixtures import build_test_emit
from fabulexa_forge.anchor import EffectiveAnchor
from fabulexa_forge.config.models import SupplementDecl
from fabulexa_forge.errors import (
    ExportError,
    SupplementSourceIsOutput,
    SupplementValueInvalid,
    TemporalRenderRequiresAnchor,
)
from fabulexa_forge.exporters.supplements import (
    ResolvedSupplement,
    check_supplement_sources_not_outputs,
    compile_supplement_specs,
)
from fabulexa_forge.reader.emit import open_emit, pin_session_timezone

if TYPE_CHECKING:
    from pathlib import Path

    from fabulexa_forge.reader.emit import Emit


def _decl(
    name: str = "t",
    columns: dict[str, str] | None = None,
    *,
    file: str | None = None,
    description: str | None = None,
    descriptions: dict[str, str] | None = None,
) -> SupplementDecl:
    """A minimal SupplementDecl; `rows` is only what parse-time shape
    validation needs — compile tests supply their own text rows via
    `_resolved`, never the decl's `rows`."""
    data: dict[str, object] = {
        "name": name,
        "columns": columns or {"a": "VARCHAR"},
        "description": description,
        "descriptions": descriptions,
    }
    if file is not None:
        data["file"] = file
    else:
        data["rows"] = []
    return SupplementDecl.model_validate(data)


def _resolved(
    decl: SupplementDecl,
    rows: tuple[tuple[str | None, ...], ...] = (),
    *,
    path: "Path | None" = None,
    sha256: str | None = None,
) -> ResolvedSupplement:
    return ResolvedSupplement(decl=decl, path=path, sha256=sha256, rows=rows)


def test_empty_input_yields_empty_list(tmp_path: "Path") -> None:
    """No supplements -> []."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        assert compile_supplement_specs(emit, (), None, "create") == []


def test_one_spec_per_supplement_in_declaration_order(tmp_path: "Path") -> None:
    """Two supplements compile to two specs, in declaration order, each
    carrying the declaration's write_mode / provenance / description fields."""
    emit_dir = build_test_emit(tmp_path)
    decl_a = _decl(
        "region_code",
        {"code": "VARCHAR"},
        description="Region lookup.",
        descriptions={"code": "The region code."},
    )
    decl_b = _decl("rate_tier", {"tier": "VARCHAR"})
    resolved = (
        _resolved(decl_a, (("US",),)),
        _resolved(decl_b, (("A",),)),
    )
    with open_emit(emit_dir) as emit:
        specs = compile_supplement_specs(emit, resolved, None, "replace")

    assert [s.table_name for s in specs] == ["region_code", "rate_tier"]
    for spec in specs:
        assert spec.write_mode == "replace"
        assert spec.keys is None
        assert spec.upsert_key is None
        assert spec.provenance == {}
        assert spec.kind_values == {}
        assert spec.event_log is False
    assert specs[0].author_descriptions == {"code": "The region code."}
    assert specs[0].author_table_description == "Region lookup."
    assert specs[1].author_descriptions == {}
    assert specs[1].author_table_description is None


def test_supplement_carries_declared_file_string_and_sha256(tmp_path: "Path") -> None:
    """A file supplement's SupplementSource carries the declared (unresolved)
    file string and the resolved sha256; an inline supplement carries both
    None."""
    emit_dir = build_test_emit(tmp_path)
    file_decl = _decl("region_code", {"code": "VARCHAR"}, file="region_code.csv")
    inline_decl = _decl("rate_tier", {"tier": "VARCHAR"})
    resolved = (
        _resolved(
            file_decl,
            (("US",),),
            path=tmp_path / "resolved" / "region_code.csv",
            sha256="deadbeef",
        ),
        _resolved(inline_decl, (("A",),)),
    )
    with open_emit(emit_dir) as emit:
        specs = compile_supplement_specs(emit, resolved, None, "create")

    assert specs[0].supplement is not None
    assert specs[0].supplement.file == "region_code.csv"
    assert specs[0].supplement.sha256 == "deadbeef"
    assert specs[1].supplement is not None
    assert specs[1].supplement.file is None
    assert specs[1].supplement.sha256 is None


def _describe(emit: "Emit", sql: str) -> list[tuple[object, ...]]:
    return emit.query(f"DESCRIBE ({sql})", ())


def test_materialization_yields_declared_columns_and_rows(tmp_path: "Path") -> None:
    """DESCRIBE gives declared names in declared order with canonical types;
    a SELECT gives rows in given order; a None cell is NULL."""
    emit_dir = build_test_emit(tmp_path)
    decl = _decl(
        "widgets",
        {"n": "BIGINT", "label": "VARCHAR"},
    )
    resolved = (_resolved(decl, (("1", "first"), (None, None), ("3", "third"))),)
    with open_emit(emit_dir) as emit:
        (spec,) = compile_supplement_specs(emit, resolved, None, "create")
        described = _describe(emit, spec.sql)
        rows = emit.query(spec.sql, ())

    assert [(row[0], row[1]) for row in described] == [
        ("n", "BIGINT"),
        ("label", "VARCHAR"),
    ]
    assert rows == [(1, "first"), (None, None), (3, "third")]


def test_zero_rows_yields_typed_empty_relation(tmp_path: "Path") -> None:
    """A supplement with no rows compiles to a typed, empty relation
    carrying the declared columns."""
    emit_dir = build_test_emit(tmp_path)
    decl = _decl("widgets", {"n": "BIGINT", "label": "VARCHAR"})
    resolved = (_resolved(decl, ()),)
    with open_emit(emit_dir) as emit:
        (spec,) = compile_supplement_specs(emit, resolved, None, "create")
        described = _describe(emit, spec.sql)
        rows = emit.query(spec.sql, ())

    assert [(row[0], row[1]) for row in described] == [
        ("n", "BIGINT"),
        ("label", "VARCHAR"),
    ]
    assert rows == []


@pytest.mark.parametrize(
    ("col_type", "cell_text", "expected"),
    [
        ("BIGINT", "1.5", 2),
        ("DECIMAL(5,2)", "12.345", "12.35"),
        ("BOOLEAN", "yes", True),
        ("BIGINT", " 7 ", 7),
    ],
)
def test_cast_leniency_matches_duckdb(
    tmp_path: "Path", col_type: str, cell_text: str, expected: object
) -> None:
    """The cast's leniency is DuckDB's own — an author-surprising round or
    coercion, not forge's own rule."""
    emit_dir = build_test_emit(tmp_path)
    decl = _decl("widgets", {"v": col_type})
    resolved = (_resolved(decl, ((cell_text,),)),)
    with open_emit(emit_dir) as emit:
        (spec,) = compile_supplement_specs(emit, resolved, None, "create")
        (row,) = emit.query(spec.sql, ())

    if col_type == "DECIMAL(5,2)":
        assert str(row[0]) == expected
    else:
        assert row[0] == expected


def test_bad_cell_raises_supplement_value_invalid(tmp_path: "Path") -> None:
    """A cell that does not cast raises SupplementValueInvalid, naming the
    table, the column, the 1-based data row, and the cell text."""
    emit_dir = build_test_emit(tmp_path)
    decl = _decl("widgets", {"n": "BIGINT"})
    resolved = (_resolved(decl, (("1",), ("not-a-number",))),)
    with open_emit(emit_dir) as emit:
        with pytest.raises(SupplementValueInvalid) as exc_info:
            compile_supplement_specs(emit, resolved, None, "create")

    message = str(exc_info.value)
    assert "widgets" in message
    assert "'n'" in message
    assert "row 2" in message
    assert "not-a-number" in message


def test_first_bad_cell_by_column_then_row_order(tmp_path: "Path") -> None:
    """Among several bad cells, the reported one is the first in
    column-then-row declared order — column a's own first bad row (2), even
    though column b's own first bad row (1) precedes it positionally."""
    emit_dir = build_test_emit(tmp_path)
    decl = _decl("widgets", {"a": "BIGINT", "b": "BOOLEAN"})
    resolved = (_resolved(decl, (("1", "not-a-bool"), ("not-a-number", "true"))),)
    with open_emit(emit_dir) as emit:
        with pytest.raises(SupplementValueInvalid) as exc_info:
            compile_supplement_specs(emit, resolved, None, "create")

    message = str(exc_info.value)
    assert "'a'" in message
    assert "row 2" in message


def test_timestamptz_no_anchor_raises(tmp_path: "Path") -> None:
    """A TIMESTAMPTZ column with anchor=None raises
    TemporalRenderRequiresAnchor, naming the supplement and the column."""
    emit_dir = build_test_emit(tmp_path)
    decl = _decl("events", {"occurred_at": "TIMESTAMPTZ"})
    resolved = (_resolved(decl, (("2024-01-15 12:00:00+00:00",),)),)
    with open_emit(emit_dir) as emit:
        with pytest.raises(TemporalRenderRequiresAnchor) as exc_info:
            compile_supplement_specs(emit, resolved, None, "create")

    message = str(exc_info.value)
    assert "events" in message
    assert "occurred_at" in message


def test_timestamptz_with_anchor_compiles_and_renders(tmp_path: "Path") -> None:
    """A TIMESTAMPTZ column with a resolved anchor compiles and renders in
    the anchor zone."""
    emit_dir = build_test_emit(tmp_path)
    decl = _decl("events", {"occurred_at": "TIMESTAMPTZ"})
    resolved = (_resolved(decl, (("2024-01-15 12:00:00+00:00",),)),)
    anchor = EffectiveAnchor(
        start_instant=datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        timezone=ZoneInfo("America/New_York"),
    )
    with open_emit(emit_dir) as emit:
        pin_session_timezone(emit, anchor)
        (spec,) = compile_supplement_specs(emit, resolved, anchor, "create")
        (row,) = emit.query(
            f"SELECT strftime(occurred_at, '%Y-%m-%d %H:%M:%S%z') FROM ({spec.sql})",
            (),
        )

    assert row[0] == "2024-01-15 07:00:00-05"


def test_reserved_name_raises_export_error(tmp_path: "Path") -> None:
    """A supplement named `_export_meta` raises ExportError, naming the
    reserved bookkeeping collision."""
    emit_dir = build_test_emit(tmp_path)
    decl = _decl("_export_meta", {"a": "VARCHAR"})
    resolved = (_resolved(decl, (("x",),)),)
    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportError, match="reserved"):
            compile_supplement_specs(emit, resolved, None, "create")


def test_reserved_name_gate_fires_before_bad_cell(tmp_path: "Path") -> None:
    """The reserved-name gate runs before the cell probe: a reserved name
    with a bad cell raises ExportError (naming the reservation), never
    SupplementValueInvalid."""
    emit_dir = build_test_emit(tmp_path)
    decl = _decl("_export_meta", {"n": "BIGINT"})
    resolved = (_resolved(decl, (("not-a-number",),)),)
    with open_emit(emit_dir) as emit:
        with pytest.raises(ExportError, match="reserved") as exc_info:
            compile_supplement_specs(emit, resolved, None, "create")

    assert not isinstance(exc_info.value, SupplementValueInvalid)


def test_anchor_gate_fires_before_bad_cell(tmp_path: "Path") -> None:
    """The anchor rule runs before the cell probe: a TIMESTAMPTZ column with
    no anchor and a bad cell raises TemporalRenderRequiresAnchor, never
    SupplementValueInvalid."""
    emit_dir = build_test_emit(tmp_path)
    decl = _decl("events", {"occurred_at": "TIMESTAMPTZ"})
    resolved = (_resolved(decl, (("not-a-timestamp",),)),)
    with open_emit(emit_dir) as emit:
        with pytest.raises(TemporalRenderRequiresAnchor):
            compile_supplement_specs(emit, resolved, None, "create")


# ---------------------------------------------------------------------------
# check_supplement_sources_not_outputs
# ---------------------------------------------------------------------------


def test_inline_only_passes_with_any_paths(tmp_path: "Path") -> None:
    """An inline supplement (no path) is skipped — passes regardless of
    output_paths / removed_dirs."""
    decl = _decl("rate_tier", {"tier": "VARCHAR"})
    resolved = (_resolved(decl, (("A",),)),)
    check_supplement_sources_not_outputs(
        resolved, {tmp_path / "anything.csv"}, {tmp_path / "removed"}
    )


def test_path_equal_to_output_is_refused(tmp_path: "Path") -> None:
    """A file supplement's resolved source path equal to an output path is
    refused."""
    source = tmp_path / "region_code.csv"
    source.write_text("code\nUS\n", encoding="utf-8")
    decl = _decl("region_code", {"code": "VARCHAR"}, file="region_code.csv")
    resolved = (_resolved(decl, (("US",),), path=source),)

    with pytest.raises(SupplementSourceIsOutput):
        check_supplement_sources_not_outputs(resolved, {source}, ())


def test_path_under_removed_dir_at_depth_two_is_refused(tmp_path: "Path") -> None:
    """A source path nested two levels under a removed directory is
    refused — containment at any depth, not just direct membership."""
    removed_dir = tmp_path / "window"
    nested = removed_dir / "sub" / "region_code.csv"
    nested.parent.mkdir(parents=True)
    nested.write_text("code\nUS\n", encoding="utf-8")
    decl = _decl("region_code", {"code": "VARCHAR"}, file="window/sub/region_code.csv")
    resolved = (_resolved(decl, (("US",),), path=nested),)

    with pytest.raises(SupplementSourceIsOutput):
        check_supplement_sources_not_outputs(resolved, (), {removed_dir})


def test_path_sharing_parent_with_output_passes(tmp_path: "Path") -> None:
    """A source that merely shares a parent directory with an output file
    (not equal to it, not under a removed directory) passes."""
    source = tmp_path / "region_code.csv"
    source.write_text("code\nUS\n", encoding="utf-8")
    other_output = tmp_path / "dim_widget.csv"
    decl = _decl("region_code", {"code": "VARCHAR"}, file="region_code.csv")
    resolved = (_resolved(decl, (("US",),), path=source),)

    check_supplement_sources_not_outputs(resolved, {other_output}, ())


def test_relative_output_path_is_compared_resolved(
    tmp_path: "Path", monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative output_paths entry is resolved before comparison, so it
    still matches an absolute source path naming the same file."""
    monkeypatch.chdir(tmp_path)
    source = (tmp_path / "region_code.csv").resolve()
    source.write_text("code\nUS\n", encoding="utf-8")
    decl = _decl("region_code", {"code": "VARCHAR"}, file="region_code.csv")
    resolved = (_resolved(decl, (("US",),), path=source),)

    from pathlib import Path as PathCls

    with pytest.raises(SupplementSourceIsOutput):
        check_supplement_sources_not_outputs(resolved, {PathCls("region_code.csv")}, ())
