"""Tests for dimensional exporter business rules.

Each test verifies one business rule raises ExportError with the documented
message, or (DiscriminatorValueObserved) emits a Notice.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink
from _support.sidecar_builder import identity_column, prop_column

from exporters._emit_fixtures import build_test_emit
from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.models import (
    ColumnDecl,
    DateParseSpec,
    DecimalSpec,
    DerivedSpec,
    DimensionalConfig,
    ElapsedSpec,
    JsonPrecisionSpec,
    OrdinalSpec,
    ScdWindowSpec,
    SourceDecl,
    TableDecl,
    TimestampSpec,
)
from fabulexa_forge.errors import (
    DateParseSourceColumn,
    DecimalSourceIsDouble,
    ExportError,
    JsonPrecisionSourceIsVarchar,
    TemporalRenderRequiresAnchor,
)
from fabulexa_forge.exporters.dimensional.validation import (
    check_date_parse_source_column,
    check_decimal_source_column,
    check_discriminator_value_observed,
    check_excluded_kind_not_sourced,
    check_excluded_table_not_sourced,
    check_json_precision_source_column,
    check_key_columns_declared,
    check_ordinal_refs_siblings,
    check_projection_column_exists,
    check_scd2_needs_history,
    check_slice_only_column_reads,
    check_slice_only_filter_keys,
    check_source_table_exists,
    check_temporal_render_requires_anchor,
    check_timestamp_source_available,
    validate_table,
)
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.sidecar import Sidecar


def _make_table_decl(
    name: str = "t",
    grain: str = "records",
    kind: str = "entity",
    *,
    columns: list[ColumnDecl] | None = None,
    key: list[str] | None = None,
    source_kwargs: dict[str, object] | None = None,
) -> TableDecl:
    """Build a minimal TableDecl for testing."""
    if columns is None:
        columns = [ColumnDecl(name="id", **{"from": "record_id"})]
    if key is None:
        key = ["id"]
    src_kwargs: dict[str, object] = {"grain": grain, "kind": kind}
    if grain in ("history_point", "history_interval"):
        src_kwargs["property"] = "state"
    if grain == "membership":
        src_kwargs["property"] = "team_members"
    if source_kwargs:
        src_kwargs.update(source_kwargs)
    return TableDecl(
        name=name,
        role="dim",
        scd="type1",
        source=SourceDecl(**src_kwargs),  # type: ignore[arg-type]
        key=key,
        columns=columns,
    )


# ---------------------------------------------------------------------------
# SourceTableExists
# ---------------------------------------------------------------------------


def test_source_table_exists_records(tmp_path: Path) -> None:
    """Known records kind resolves without error."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        src = SourceDecl(grain="records", kind="entity")
        name = check_source_table_exists(src, emit.sidecar)
    assert name == "records__entity"


def test_source_table_exists_history(tmp_path: Path) -> None:
    """history_point grain resolves to 'history'."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        src = SourceDecl(
            grain="history_point", kind="journey_instance", property="state"
        )
        name = check_source_table_exists(src, emit.sidecar)
    assert name == "history"


def test_source_table_not_found(tmp_path: Path) -> None:
    """Unknown kind raises ExportError with kind and grain in message."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        src = SourceDecl(grain="records", kind="unknown_kind")
        with pytest.raises(ExportError, match="unknown_kind"):
            check_source_table_exists(src, emit.sidecar)


# ---------------------------------------------------------------------------
# KeyColumnsDeclared
# ---------------------------------------------------------------------------


def test_key_columns_declared_passes() -> None:
    """All key columns declared in columns list passes."""
    decl = _make_table_decl(key=["id"])
    check_key_columns_declared(decl)  # must not raise


def test_key_columns_undeclared_raises() -> None:
    """Key naming undeclared column raises ExportError."""
    decl = _make_table_decl(key=["id", "missing_col"])
    with pytest.raises(ExportError, match="missing_col"):
        check_key_columns_declared(decl)


# ---------------------------------------------------------------------------
# ProjectionColumnExists
# ---------------------------------------------------------------------------


def test_projection_from_valid(tmp_path: Path) -> None:
    """from: naming a valid surface column does not raise."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        col = ColumnDecl(name="id", **{"from": "record_id"})
        tbl = _make_table_decl(columns=[col])
        from fabulexa_forge.exporters.dimensional.validation import (
            _grain_projectable_surface,
            _resolve_source_table_name,
        )

        src_name = _resolve_source_table_name(tbl.source)
        surface = _grain_projectable_surface(tbl.source, emit.sidecar, src_name)
        check_projection_column_exists(col, tbl, surface)  # must not raise


def test_projection_from_missing_raises(tmp_path: Path) -> None:
    """from: naming absent column raises ExportError."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        col = ColumnDecl(name="x", **{"from": "nonexistent_column"})
        tbl = _make_table_decl(columns=[col], key=["x"])
        from fabulexa_forge.exporters.dimensional.validation import (
            _grain_projectable_surface,
            _resolve_source_table_name,
        )

        src_name = _resolve_source_table_name(tbl.source)
        surface = _grain_projectable_surface(tbl.source, emit.sidecar, src_name)
        with pytest.raises(ExportError, match="nonexistent_column"):
            check_projection_column_exists(col, tbl, surface)


def test_value_map_from_missing_raises(tmp_path: Path) -> None:
    """derived.value_map.from naming absent column raises ExportError."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        col = ColumnDecl(
            name="x",
            derived=DerivedSpec(value_map={"from": "ghost_col", "map": {"a": 1}}),
        )
        tbl = _make_table_decl(columns=[col], key=["x"])
        from fabulexa_forge.exporters.dimensional.validation import (
            _grain_projectable_surface,
            _resolve_source_table_name,
        )

        src_name = _resolve_source_table_name(tbl.source)
        surface = _grain_projectable_surface(tbl.source, emit.sidecar, src_name)
        with pytest.raises(ExportError, match="ghost_col"):
            check_projection_column_exists(col, tbl, surface)


def test_decimal_from_missing_raises(tmp_path: Path) -> None:
    """derived.decimal.from naming absent column raises ExportError."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        col = ColumnDecl(
            name="x",
            derived=DerivedSpec(
                decimal=DecimalSpec(**{"from": "ghost_col", "as": [4, 3]})
            ),
        )
        tbl = _make_table_decl(columns=[col], key=["x"])
        from fabulexa_forge.exporters.dimensional.validation import (
            _grain_projectable_surface,
            _resolve_source_table_name,
        )

        src_name = _resolve_source_table_name(tbl.source)
        surface = _grain_projectable_surface(tbl.source, emit.sidecar, src_name)
        with pytest.raises(ExportError, match="ghost_col"):
            check_projection_column_exists(col, tbl, surface)


def test_json_precision_from_missing_raises(tmp_path: Path) -> None:
    """derived.json_precision.from naming absent column raises ExportError."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        col = ColumnDecl(
            name="x",
            derived=DerivedSpec(
                json_precision=JsonPrecisionSpec(
                    **{"from": "ghost_col", "leaves": {"discount": 2}}
                )
            ),
        )
        tbl = _make_table_decl(columns=[col], key=["x"])
        from fabulexa_forge.exporters.dimensional.validation import (
            _grain_projectable_surface,
            _resolve_source_table_name,
        )

        src_name = _resolve_source_table_name(tbl.source)
        surface = _grain_projectable_surface(tbl.source, emit.sidecar, src_name)
        with pytest.raises(ExportError, match="ghost_col"):
            check_projection_column_exists(col, tbl, surface)


# ---------------------------------------------------------------------------
# OrdinalRefsSiblings
# ---------------------------------------------------------------------------


def test_ordinal_refs_siblings_passes() -> None:
    """ordinal referencing declared siblings does not raise."""
    id_col = ColumnDecl(name="id", **{"from": "record_id"})
    ts_col = ColumnDecl(name="ts", **{"from": "last_mutation_sim_time"})
    ordinal_col = ColumnDecl(
        name="rank",
        derived=DerivedSpec(ordinal=OrdinalSpec(partition_by="id", order_by="ts")),
    )
    tbl = _make_table_decl(columns=[id_col, ts_col, ordinal_col], key=["id"])
    check_ordinal_refs_siblings(ordinal_col, tbl)  # must not raise


def test_ordinal_refs_undeclared_raises() -> None:
    """ordinal referencing an undeclared column raises ExportError."""
    id_col = ColumnDecl(name="id", **{"from": "record_id"})
    ordinal_col = ColumnDecl(
        name="rank",
        derived=DerivedSpec(ordinal=OrdinalSpec(partition_by="id", order_by="missing")),
    )
    tbl = _make_table_decl(columns=[id_col, ordinal_col], key=["id"])
    with pytest.raises(ExportError, match="missing"):
        check_ordinal_refs_siblings(ordinal_col, tbl)


# ---------------------------------------------------------------------------
# TimestampSourceAvailable
# ---------------------------------------------------------------------------


def test_timestamp_source_sim_time_on_history(tmp_path: Path) -> None:
    """sim_time is available on history_point grain."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        col = ColumnDecl(
            name="ts",
            derived=DerivedSpec(timestamp=TimestampSpec(source="sim_time")),
        )
        tbl = _make_table_decl(
            grain="history_point",
            kind="journey_instance",
            columns=[col],
            key=["ts"],
        )
        from fabulexa_forge.exporters.dimensional.validation import (
            _grain_projectable_surface,
            _resolve_source_table_name,
        )

        src_name = _resolve_source_table_name(tbl.source)
        surface = _grain_projectable_surface(tbl.source, emit.sidecar, src_name)
        check_timestamp_source_available(col, tbl, tbl.source, surface)


def test_created_by_sim_time_on_records_raises(tmp_path: Path) -> None:
    """created_by_sim_time on records grain raises ExportError.

    created_by_sim_time cannot occur in a sanitised emit and is no longer
    accepted as a timestamp source on any grain.
    """
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        col = ColumnDecl(
            name="ts",
            derived=DerivedSpec(timestamp=TimestampSpec(source="created_by_sim_time")),
        )
        tbl = _make_table_decl(
            grain="records",
            kind="entity",
            columns=[col],
            key=["ts"],
        )
        from fabulexa_forge.exporters.dimensional.validation import (
            _grain_projectable_surface,
            _resolve_source_table_name,
        )

        src_name = _resolve_source_table_name(tbl.source)
        surface = _grain_projectable_surface(tbl.source, emit.sidecar, src_name)
        with pytest.raises(ExportError, match="created_by_sim_time"):
            check_timestamp_source_available(col, tbl, tbl.source, surface)


def test_lead_sim_time_on_records_raises(tmp_path: Path) -> None:
    """lead_sim_time on records grain raises ExportError."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        col = ColumnDecl(
            name="ts",
            derived=DerivedSpec(timestamp=TimestampSpec(source="lead_sim_time")),
        )
        tbl = _make_table_decl(
            grain="records",
            kind="entity",
            columns=[col],
            key=["ts"],
        )
        from fabulexa_forge.exporters.dimensional.validation import (
            _grain_projectable_surface,
            _resolve_source_table_name,
        )

        src_name = _resolve_source_table_name(tbl.source)
        surface = _grain_projectable_surface(tbl.source, emit.sidecar, src_name)
        with pytest.raises(ExportError, match="lead_sim_time"):
            check_timestamp_source_available(col, tbl, tbl.source, surface)


def test_joined_sim_time_off_membership_raises(tmp_path: Path) -> None:
    """joined_sim_time on records grain raises ExportError."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        col = ColumnDecl(
            name="ts",
            derived=DerivedSpec(timestamp=TimestampSpec(source="joined_sim_time")),
        )
        tbl = _make_table_decl(
            grain="records",
            kind="entity",
            columns=[col],
            key=["ts"],
        )
        from fabulexa_forge.exporters.dimensional.validation import (
            _grain_projectable_surface,
            _resolve_source_table_name,
        )

        src_name = _resolve_source_table_name(tbl.source)
        surface = _grain_projectable_surface(tbl.source, emit.sidecar, src_name)
        with pytest.raises(ExportError, match="joined_sim_time"):
            check_timestamp_source_available(col, tbl, tbl.source, surface)


@pytest.mark.parametrize(
    "source_name", ["created_sim_time", "deactivated_at", "last_mutation_sim_time"]
)
def test_records_grain_instant_sources_accepted(
    tmp_path: Path, source_name: str
) -> None:
    """Each of the three records structural instants passes on records grain.

    The allowlist is the reader's structural-temporal surface, so every
    instant-carrying records structural column is a legal timestamp source —
    a record's birth and close instants as much as its last-touched one.
    """
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        col = ColumnDecl(
            name="ts",
            derived=DerivedSpec(timestamp=TimestampSpec(source=source_name)),
        )
        tbl = _make_table_decl(
            grain="records",
            kind="entity",
            columns=[col],
            key=["ts"],
        )
        from fabulexa_forge.exporters.dimensional.validation import (
            _grain_projectable_surface,
            _resolve_source_table_name,
        )

        src_name = _resolve_source_table_name(tbl.source)
        surface = _grain_projectable_surface(tbl.source, emit.sidecar, src_name)
        check_timestamp_source_available(
            col, tbl, tbl.source, surface
        )  # must not raise


def test_record_index_on_records_raises(tmp_path: Path) -> None:
    """A non-instant records structural column (record_index) is still refused."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        col = ColumnDecl(
            name="ts",
            derived=DerivedSpec(timestamp=TimestampSpec(source="record_index")),
        )
        tbl = _make_table_decl(
            grain="records",
            kind="entity",
            columns=[col],
            key=["ts"],
        )
        from fabulexa_forge.exporters.dimensional.validation import (
            _grain_projectable_surface,
            _resolve_source_table_name,
        )

        src_name = _resolve_source_table_name(tbl.source)
        surface = _grain_projectable_surface(tbl.source, emit.sidecar, src_name)
        with pytest.raises(
            ExportError,
            match="timestamp source 'record_index' is not available on grain 'records'",
        ):
            check_timestamp_source_available(col, tbl, tbl.source, surface)


# ---------------------------------------------------------------------------
# TemporalRenderRequiresAnchor
# ---------------------------------------------------------------------------


def test_temporal_render_requires_anchor_explicit_timestamp_no_anchor_raises() -> None:
    """Explicit `as: timestamp` with no resolved anchor is refused at plan
    time, naming the column."""
    col = ColumnDecl(
        name="admitted_at",
        derived=DerivedSpec(
            timestamp=TimestampSpec(source="created_sim_time", **{"as": "timestamp"})
        ),
    )
    with pytest.raises(TemporalRenderRequiresAnchor, match="admitted_at"):
        check_temporal_render_requires_anchor(col, None)


def test_temporal_render_requires_anchor_unelected_timestamp_no_anchor_passes() -> None:
    """An unelected derived: timestamp (no `as`) with no anchor keeps today's
    raw-ns rendering — absence detection, not an election, so no raise."""
    col = ColumnDecl(
        name="raw_ts",
        derived=DerivedSpec(timestamp=TimestampSpec(source="created_sim_time")),
    )
    check_temporal_render_requires_anchor(col, None)  # must not raise


def test_temporal_render_requires_anchor_scd_window_object_form_no_anchor_raises() -> (
    None
):
    """The scd_window object form is always an explicit election — no anchor
    is refused, naming the column."""
    col = ColumnDecl(
        name="valid_from",
        derived=DerivedSpec(
            scd_window=ScdWindowSpec(bound="valid_from", **{"as": "date"})
        ),
    )
    with pytest.raises(TemporalRenderRequiresAnchor, match="valid_from"):
        check_temporal_render_requires_anchor(col, None)


def test_temporal_render_requires_anchor_scd_window_bare_literal_no_anchor_passes() -> (
    None
):
    """The scd_window bare-literal shorthand carries no election — no anchor
    does not raise."""
    col = ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from"))
    check_temporal_render_requires_anchor(col, None)  # must not raise


def test_temporal_render_requires_anchor_with_anchor_never_raises() -> None:
    """Any explicit election, with a resolved anchor present, never raises."""
    col = ColumnDecl(
        name="admission_date",
        derived=DerivedSpec(
            timestamp=TimestampSpec(source="created_sim_time", **{"as": "date"})
        ),
    )
    check_temporal_render_requires_anchor(col, MagicMock())  # must not raise


# ---------------------------------------------------------------------------
# DateParseSourceColumn
# ---------------------------------------------------------------------------


def _date_parse_sidecar(columns: list[dict[str, object]]) -> Sidecar:
    """A bare records__actor sidecar carrying the given column list — the
    DateParseSourceColumn unit tests' fixture."""
    return _bare_sidecar(
        [
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": columns,
                "rows": 0,
            }
        ]
    )


def test_date_parse_source_column_declared_varchar_passes() -> None:
    """A declared VARCHAR date_parse source passes."""
    sidecar = _date_parse_sidecar(
        [
            identity_column("record_id", "VARCHAR"),
            {"name": "prop__dob", "type": "VARCHAR"},
        ]
    )
    col = ColumnDecl(
        name="birth_date",
        derived=DerivedSpec(
            date_parse=DateParseSpec(**{"from": "prop__dob", "format": "%Y-%m-%d"})
        ),
    )
    tbl = _make_table_decl(kind="actor", columns=[col], key=["birth_date"])
    check_date_parse_source_column(
        col, tbl, "records__actor", sidecar
    )  # must not raise


def test_date_parse_source_column_non_varchar_raises() -> None:
    """A declared non-VARCHAR date_parse source raises DateParseSourceColumn,
    naming the column and the actual type."""
    sidecar = _date_parse_sidecar(
        [
            identity_column("record_id", "VARCHAR"),
            {"name": "prop__dob", "type": "BIGINT"},
        ]
    )
    col = ColumnDecl(
        name="birth_date",
        derived=DerivedSpec(
            date_parse=DateParseSpec(**{"from": "prop__dob", "format": "%Y-%m-%d"})
        ),
    )
    tbl = _make_table_decl(kind="actor", columns=[col], key=["birth_date"])
    with pytest.raises(DateParseSourceColumn, match="birth_date") as exc_info:
        check_date_parse_source_column(col, tbl, "records__actor", sidecar)
    assert "BIGINT" in str(exc_info.value)


def test_date_parse_source_column_structural_source_raises() -> None:
    """A structural source with no declared prop__ type behind it (no
    matching sidecar column) raises, naming 'no declared type'."""
    sidecar = _date_parse_sidecar([identity_column("record_id", "VARCHAR")])
    col = ColumnDecl(
        name="parsed",
        derived=DerivedSpec(
            date_parse=DateParseSpec(
                **{"from": "created_sim_time", "format": "%Y-%m-%d"}
            )
        ),
    )
    tbl = _make_table_decl(kind="actor", columns=[col], key=["parsed"])
    with pytest.raises(DateParseSourceColumn, match="no declared type"):
        check_date_parse_source_column(col, tbl, "records__actor", sidecar)


# ---------------------------------------------------------------------------
# DecimalSourceIsDouble / JsonPrecisionSourceIsVarchar
# (value-rendering-elections Phase 5 — the dimensional derived spellings'
# source-type gates, mirroring DateParseSourceColumn's shape.)
# ---------------------------------------------------------------------------


def test_decimal_source_column_declared_double_passes() -> None:
    """A declared DOUBLE decimal source passes."""
    sidecar = _date_parse_sidecar(
        [
            identity_column("record_id", "VARCHAR"),
            {"name": "prop__amount", "type": "DOUBLE"},
        ]
    )
    col = ColumnDecl(
        name="amount",
        derived=DerivedSpec(
            decimal=DecimalSpec(**{"from": "prop__amount", "as": [4, 3]})
        ),
    )
    tbl = _make_table_decl(kind="actor", columns=[col], key=["amount"])
    check_decimal_source_column(col, tbl, "records__actor", sidecar)  # must not raise


def test_decimal_source_column_non_double_raises() -> None:
    """A declared non-DOUBLE decimal source raises DecimalSourceIsDouble,
    naming the column and the actual type."""
    sidecar = _date_parse_sidecar(
        [
            identity_column("record_id", "VARCHAR"),
            {"name": "prop__amount", "type": "BIGINT"},
        ]
    )
    col = ColumnDecl(
        name="amount",
        derived=DerivedSpec(
            decimal=DecimalSpec(**{"from": "prop__amount", "as": [4, 3]})
        ),
    )
    tbl = _make_table_decl(kind="actor", columns=[col], key=["amount"])
    with pytest.raises(DecimalSourceIsDouble, match="amount") as exc_info:
        check_decimal_source_column(col, tbl, "records__actor", sidecar)
    assert "BIGINT" in str(exc_info.value)


def test_decimal_source_column_structural_source_raises() -> None:
    """A structural source with no declared prop__ type behind it raises,
    naming 'no declared type'."""
    sidecar = _date_parse_sidecar([identity_column("record_id", "VARCHAR")])
    col = ColumnDecl(
        name="amount",
        derived=DerivedSpec(
            decimal=DecimalSpec(**{"from": "last_mutation_sim_time", "as": [4, 3]})
        ),
    )
    tbl = _make_table_decl(kind="actor", columns=[col], key=["amount"])
    with pytest.raises(DecimalSourceIsDouble, match="no declared type"):
        check_decimal_source_column(col, tbl, "records__actor", sidecar)


def test_json_precision_source_column_declared_varchar_passes() -> None:
    """A declared VARCHAR json_precision source passes."""
    sidecar = _date_parse_sidecar(
        [
            identity_column("record_id", "VARCHAR"),
            {"name": "prop__payload", "type": "VARCHAR"},
        ]
    )
    col = ColumnDecl(
        name="payload",
        derived=DerivedSpec(
            json_precision=JsonPrecisionSpec(
                **{"from": "prop__payload", "leaves": {"discount": 2}}
            )
        ),
    )
    tbl = _make_table_decl(kind="actor", columns=[col], key=["payload"])
    check_json_precision_source_column(
        col, tbl, "records__actor", sidecar
    )  # must not raise


def test_json_precision_source_column_non_varchar_raises() -> None:
    """A declared non-VARCHAR json_precision source raises
    JsonPrecisionSourceIsVarchar, naming the column and the actual type."""
    sidecar = _date_parse_sidecar(
        [
            identity_column("record_id", "VARCHAR"),
            {"name": "prop__payload", "type": "DOUBLE"},
        ]
    )
    col = ColumnDecl(
        name="payload",
        derived=DerivedSpec(
            json_precision=JsonPrecisionSpec(
                **{"from": "prop__payload", "leaves": {"discount": 2}}
            )
        ),
    )
    tbl = _make_table_decl(kind="actor", columns=[col], key=["payload"])
    with pytest.raises(JsonPrecisionSourceIsVarchar, match="payload") as exc_info:
        check_json_precision_source_column(col, tbl, "records__actor", sidecar)
    assert "DOUBLE" in str(exc_info.value)


# ---------------------------------------------------------------------------
# DiscriminatorValueObserved (notice, not error)
# ---------------------------------------------------------------------------


def test_discriminator_value_observed_emits_notice(tmp_path: Path) -> None:
    """Declared-but-unobserved filter value emits one Notice, not an error."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        src = SourceDecl(
            grain="records",
            kind="entity",
            filter={"prop__entity_type": "nonexistent_type"},
        )
        sink = RecordingNoticeSink()
        check_discriminator_value_observed(src, emit.sidecar, sink)
        assert len(sink.notices) == 1
        assert sink.notices[0].code == "discriminator-value-unobserved"
        assert "nonexistent_type" in sink.notices[0].message


# ---------------------------------------------------------------------------
# DiscriminatorValueObserved — the five-row notice matrix
# (§ The unobserved-value notice). Each row uses a bare enum_domains-only
# sidecar, since check_discriminator_value_observed reads only source.kind /
# source.filter and sidecar.enum_domains() — no table data is consulted.
# ---------------------------------------------------------------------------


def _enum_domains_sidecar(
    enum_domains: dict[str, dict[str, list[str]]] | None,
) -> Sidecar:
    """A bare sidecar carrying only (optionally) an enum_domains registry."""
    return _bare_sidecar(tables=[], enum_domains=enum_domains)


_OBSERVED_ENTITY_TYPES = {"entity": {"entity_type": ["consultant", "nurse"]}}


def test_discriminator_scalar_observed_emits_no_notice() -> None:
    """Row 1: a scalar filter value that is observed emits zero notices."""
    sidecar = _enum_domains_sidecar(_OBSERVED_ENTITY_TYPES)
    src = SourceDecl(
        grain="records", kind="entity", filter={"prop__entity_type": "consultant"}
    )
    sink = RecordingNoticeSink()
    check_discriminator_value_observed(src, sidecar, sink)
    assert sink.notices == []


def test_discriminator_scalar_unobserved_emits_one_notice_table_empty() -> None:
    """Row 2: a scalar filter value that is unobserved emits one notice, the
    'table will be empty' wording verbatim."""
    sidecar = _enum_domains_sidecar(_OBSERVED_ENTITY_TYPES)
    src = SourceDecl(
        grain="records", kind="entity", filter={"prop__entity_type": "admin"}
    )
    sink = RecordingNoticeSink()
    check_discriminator_value_observed(src, sidecar, sink)
    assert len(sink.notices) == 1
    assert sink.notices[0].code == "discriminator-value-unobserved"
    assert sink.notices[0].message == (
        "discriminator value 'admin' not observed for 'entity.prop__entity_type';"
        " table will be empty"
    )


def test_discriminator_list_wholly_unobserved_emits_one_notice_per_element() -> None:
    """Row 3: a list with no element observed emits one notice per element,
    each keeping the 'table will be empty' wording — the table really is
    empty."""
    sidecar = _enum_domains_sidecar(_OBSERVED_ENTITY_TYPES)
    src = SourceDecl(
        grain="records",
        kind="entity",
        filter={"prop__entity_type": ["admin", "guest"]},
    )
    sink = RecordingNoticeSink()
    check_discriminator_value_observed(src, sidecar, sink)
    assert [n.code for n in sink.notices] == ["discriminator-value-unobserved"] * 2
    assert [n.message for n in sink.notices] == [
        "discriminator value 'admin' not observed for 'entity.prop__entity_type';"
        " table will be empty",
        "discriminator value 'guest' not observed for 'entity.prop__entity_type';"
        " table will be empty",
    ]


def test_discriminator_list_partially_observed_emits_one_notice_per_unobserved() -> (
    None
):
    """Row 4: a list with some elements observed emits one notice per
    unobserved element only, in config element order, with the weaker
    'it contributes no rows' wording — the table is not, in fact, empty."""
    sidecar = _enum_domains_sidecar(_OBSERVED_ENTITY_TYPES)
    src = SourceDecl(
        grain="records",
        kind="entity",
        filter={"prop__entity_type": ["consultant", "admin", "nurse", "guest"]},
    )
    sink = RecordingNoticeSink()
    check_discriminator_value_observed(src, sidecar, sink)
    assert all(n.code == "discriminator-value-unobserved" for n in sink.notices)
    assert [n.message for n in sink.notices] == [
        "discriminator value 'admin' not observed for 'entity.prop__entity_type';"
        " it contributes no rows",
        "discriminator value 'guest' not observed for 'entity.prop__entity_type';"
        " it contributes no rows",
    ]


@pytest.mark.parametrize("value", ["admin", ["admin", "guest"]], ids=["scalar", "list"])
def test_discriminator_column_absent_from_registry_emits_no_notice(
    value: str | list[str],
) -> None:
    """Row 5: the filtered column carries no observed-value set in
    enum_domains -> zero notices, for either the scalar or list form."""
    sidecar = _enum_domains_sidecar(None)
    src = SourceDecl(
        grain="records", kind="entity", filter={"prop__entity_type": value}
    )
    sink = RecordingNoticeSink()
    check_discriminator_value_observed(src, sidecar, sink)
    assert sink.notices == []


# ---------------------------------------------------------------------------
# ExcludedKindNotSourced
# ---------------------------------------------------------------------------


def test_excluded_kind_not_sourced_raises() -> None:
    """Table sourcing an excluded kind raises ExportError."""
    tbl = _make_table_decl(kind="entity")
    config = DimensionalConfig(
        tables=[tbl],
        exclude={"kinds": ["entity"]},
    )
    with pytest.raises(ExportError, match="excluded kind 'entity'"):
        check_excluded_kind_not_sourced(tbl, config)


def test_excluded_kind_not_present_passes() -> None:
    """Table sourcing a non-excluded kind does not raise."""
    tbl = _make_table_decl(kind="entity")
    config = DimensionalConfig(
        tables=[tbl],
        exclude={"kinds": ["scheduler"]},
    )
    check_excluded_kind_not_sourced(tbl, config)  # must not raise


# ---------------------------------------------------------------------------
# ExcludedTableNotSourced
# ---------------------------------------------------------------------------


def test_excluded_table_not_sourced_raises() -> None:
    """Table whose source resolves to an excluded sidecar table raises ExportError."""
    tbl = _make_table_decl(kind="entity")
    config = DimensionalConfig(
        tables=[tbl],
        exclude={"tables": ["records__entity"]},
    )
    with pytest.raises(ExportError, match="excluded sidecar table 'records__entity'"):
        check_excluded_table_not_sourced(tbl, "records__entity", config)


def test_excluded_table_not_matching_passes() -> None:
    """Table whose source resolves to a different table does not raise."""
    tbl = _make_table_decl(kind="entity")
    config = DimensionalConfig(
        tables=[tbl],
        exclude={"tables": ["records__other"]},
    )
    check_excluded_table_not_sourced(tbl, "records__entity", config)  # must not raise


# ---------------------------------------------------------------------------
# Scd2NeedsHistory
# ---------------------------------------------------------------------------


def _make_scd2_table_decl(name: str = "dim_entity", kind: str = "entity") -> TableDecl:
    """Build a minimal scd: type2 TableDecl for Scd2NeedsHistory tests."""
    return TableDecl(
        name=name,
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind=kind),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
        ],
    )


def test_scd2_needs_history_refuses_flag_absent_emit() -> None:
    """check_scd2_needs_history refuses a flag-absent emit with a clear error.

    When history_tracked_available() is False the function raises ExportError
    immediately with a "re-emit with history_tracked" message — no inference.
    """
    table_decl = _make_scd2_table_decl()
    sidecar = MagicMock()
    sidecar.history_tracked_available.return_value = False

    with pytest.raises(ExportError, match="re-emit with history_tracked"):
        check_scd2_needs_history(table_decl, "records__entity", sidecar)


# ---------------------------------------------------------------------------
# Scd2ColumnModeSupported admits per-version renderings over tracked sources
# ---------------------------------------------------------------------------


def _scd2_derived_source_sidecar() -> Sidecar:
    """A records__actor sidecar spanning the source surfaces the type2 mode
    gate and records-grain column gates distinguish: a tracked prop__status,
    an untracked-and-constant prop__birth_date, a presentation-shaped
    prop__minted_on (history_tracked with temporal_class constant — the
    combination the contract mints for a presentation property bound to a
    constant source), a tracked non-VARCHAR prop__score, a slice_only
    prop__last_seen, and a structural created_sim_time."""
    return _bare_sidecar(
        [
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": [
                    identity_column("record_id", "VARCHAR"),
                    {"name": "created_sim_time", "type": "BIGINT"},
                    prop_column(
                        "prop__status",
                        "VARCHAR",
                        history_tracked=True,
                        temporal_class="tracked",
                    ),
                    prop_column(
                        "prop__birth_date",
                        "VARCHAR",
                        history_tracked=False,
                        temporal_class="constant",
                    ),
                    prop_column(
                        "prop__minted_on",
                        "VARCHAR",
                        history_tracked=True,
                        temporal_class="constant",
                    ),
                    prop_column(
                        "prop__score",
                        "BIGINT",
                        history_tracked=True,
                        temporal_class="tracked",
                    ),
                    prop_column(
                        "prop__last_seen",
                        "VARCHAR",
                        history_tracked=False,
                        temporal_class="slice_only",
                    ),
                ],
                "rows": 0,
            }
        ]
    )


def test_validate_table_type2_derived_date_parse_tracked_source_passes() -> None:
    """A derived: date_parse over a tracked prop__ source now passes
    validate_table on a type2 table — the deleted Scd2DerivedSourceConstant
    gate no longer restricts type2 renderings to constant sources."""
    sidecar = _scd2_derived_source_sidecar()
    col = ColumnDecl(
        name="status_date",
        derived=DerivedSpec(
            date_parse=DateParseSpec(**{"from": "prop__status", "format": "%Y-%m-%d"})
        ),
    )
    tbl = _scd2_derived_validate_table_decl(col)
    config = DimensionalConfig(tables=[tbl])
    validate_table(tbl, config, sidecar, None, discard_notice_sink)  # must not raise


def test_validate_table_type2_derived_slice_only_source_still_refused() -> None:
    """A non-exempt slice_only derived source on a type2 table is still
    refused — by the slice-only surface (check_slice_only_column_reads), not
    a type2-specific gate."""
    sidecar = _scd2_derived_source_sidecar()
    col = ColumnDecl(
        name="last_seen_date",
        derived=DerivedSpec(
            date_parse=DateParseSpec(
                **{"from": "prop__last_seen", "format": "%Y-%m-%d"}
            )
        ),
    )
    tbl = _scd2_derived_validate_table_decl(col)
    config = DimensionalConfig(tables=[tbl])
    with pytest.raises(ExportError) as exc_info:
        validate_table(tbl, config, sidecar, None, discard_notice_sink)
    _assert_slice_only_message(str(exc_info.value), column="prop__last_seen")


# ---------------------------------------------------------------------------
# validate_table: type2 derived columns run the records-grain column gates
# ---------------------------------------------------------------------------


def _scd2_derived_validate_table_decl(extra_col: ColumnDecl) -> TableDecl:
    """A tracked-and-keyed scd: type2 dim_patient decl (Scd2NeedsHistory-safe)
    carrying one extra derived column."""
    return TableDecl(
        name="dim_patient",
        role="dim",
        scd="type2",
        source=SourceDecl(grain="records", kind="actor"),
        key=["id", "valid_from"],
        columns=[
            ColumnDecl(name="id", **{"from": "record_id"}),
            ColumnDecl(name="status", **{"from": "prop__status"}),
            ColumnDecl(name="valid_from", derived=DerivedSpec(scd_window="valid_from")),
            extra_col,
        ],
    )


def test_validate_table_type2_derived_timestamp_election_no_anchor_raises() -> None:
    """An explicit timestamp election on a type2 derived: timestamp column
    with no anchor raises TemporalRenderRequiresAnchor — now reachable
    through the admitted derived: timestamp mode."""
    sidecar = _scd2_derived_source_sidecar()
    col = ColumnDecl(
        name="admitted_at",
        derived=DerivedSpec(
            timestamp=TimestampSpec(source="created_sim_time", **{"as": "date"})
        ),
    )
    tbl = _scd2_derived_validate_table_decl(col)
    config = DimensionalConfig(tables=[tbl])
    with pytest.raises(TemporalRenderRequiresAnchor, match="admitted_at"):
        validate_table(tbl, config, sidecar, None, discard_notice_sink)


def test_validate_table_type2_derived_timestamp_unavailable_source_raises() -> None:
    """TimestampSourceAvailable fires on a type2 derived: timestamp column
    exactly as on the records grain."""
    sidecar = _scd2_derived_source_sidecar()
    col = ColumnDecl(
        name="bad_ts",
        derived=DerivedSpec(timestamp=TimestampSpec(source="prop__nonexistent")),
    )
    tbl = _scd2_derived_validate_table_decl(col)
    config = DimensionalConfig(tables=[tbl])
    with pytest.raises(ExportError, match="timestamp source 'prop__nonexistent'"):
        validate_table(tbl, config, sidecar, None, discard_notice_sink)


def test_validate_table_type2_derived_date_parse_non_varchar_raises() -> None:
    """DateParseSourceColumn fires on a type2 derived: date_parse column
    exactly as on the records grain."""
    sidecar = _scd2_derived_source_sidecar()
    col = ColumnDecl(
        name="created_date",
        derived=DerivedSpec(
            date_parse=DateParseSpec(
                **{"from": "created_sim_time", "format": "%Y-%m-%d"}
            )
        ),
    )
    tbl = _scd2_derived_validate_table_decl(col)
    config = DimensionalConfig(tables=[tbl])
    with pytest.raises(DateParseSourceColumn, match="got BIGINT"):
        validate_table(tbl, config, sidecar, None, discard_notice_sink)


def test_validate_table_type2_derived_date_parse_untracked_source_passes() -> None:
    """A derived: date_parse column with a valid untracked VARCHAR source
    passes validate_table on a type2 dim."""
    sidecar = _scd2_derived_source_sidecar()
    col = ColumnDecl(
        name="birth_date",
        derived=DerivedSpec(
            date_parse=DateParseSpec(
                **{"from": "prop__birth_date", "format": "%Y-%m-%d"}
            )
        ),
    )
    tbl = _scd2_derived_validate_table_decl(col)
    config = DimensionalConfig(tables=[tbl])
    validate_table(tbl, config, sidecar, None, discard_notice_sink)  # must not raise


def test_validate_table_type2_derived_decimal_non_double_tracked_source_raises() -> (
    None
):
    """DecimalSourceIsDouble fires on a type2 derived: decimal column reading
    a non-DOUBLE tracked source, exactly as on the records grain."""
    sidecar = _scd2_derived_source_sidecar()
    col = ColumnDecl(
        name="status_amount",
        derived=DerivedSpec(
            decimal=DecimalSpec(**{"from": "prop__status"}, **{"as": (10, 2)})
        ),
    )
    tbl = _scd2_derived_validate_table_decl(col)
    config = DimensionalConfig(tables=[tbl])
    with pytest.raises(DecimalSourceIsDouble, match="got VARCHAR"):
        validate_table(tbl, config, sidecar, None, discard_notice_sink)


def test_validate_table_type2_derived_json_precision_non_varchar_raises() -> None:
    """JsonPrecisionSourceIsVarchar fires on a type2 derived: json_precision
    column reading a non-VARCHAR tracked source, exactly as on the records
    grain."""
    sidecar = _scd2_derived_source_sidecar()
    col = ColumnDecl(
        name="score_precision",
        derived=DerivedSpec(
            json_precision=JsonPrecisionSpec(
                **{"from": "prop__score"}, leaves={"amount": 2}
            )
        ),
    )
    tbl = _scd2_derived_validate_table_decl(col)
    config = DimensionalConfig(tables=[tbl])
    with pytest.raises(JsonPrecisionSourceIsVarchar, match="got BIGINT"):
        validate_table(tbl, config, sidecar, None, discard_notice_sink)


# ---------------------------------------------------------------------------
# validate_table integration
# ---------------------------------------------------------------------------


def test_validate_table_passes(tmp_path: Path) -> None:
    """Valid table declaration against real emit passes validation."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        col = ColumnDecl(name="id", **{"from": "record_id"})
        tbl = _make_table_decl(
            kind="entity",
            columns=[col],
            key=["id"],
        )
        config = DimensionalConfig(tables=[tbl])
        src_name = validate_table(tbl, config, emit.sidecar, None, discard_notice_sink)
    assert src_name == "records__entity"


# ---------------------------------------------------------------------------
# SliceOnlyColumnRefused — shared fixtures
#
# check_slice_only_filter_keys / check_slice_only_column_reads / validate_table
# consult only the sidecar, never run.duckdb, so these tests skip
# build_test_emit/open_emit and parse a bare sidecar dict directly — mirroring
# tests/exporters/test_slice_only.py's own helper.
# ---------------------------------------------------------------------------


def _bare_sidecar(
    tables: list[dict[str, object]],
    enum_domains: dict[str, dict[str, list[str]]] | None = None,
) -> Sidecar:
    """Build a minimal Sidecar (no DuckDB) for the slice_only-check unit tests."""
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": tables,
    }
    if enum_domains is not None:
        raw["enum_domains"] = enum_domains
    return Sidecar.from_raw(raw)


_SLICE_ONLY_ACTOR_COLUMNS: list[dict[str, object]] = [
    identity_column("record_id", "VARCHAR"),
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    prop_column(
        "prop__tier", "VARCHAR", history_tracked=False, temporal_class="slice_only"
    ),
]


def _slice_only_actor_sidecar(
    extra_columns: list[dict[str, object]] | None = None,
    enum_domains: dict[str, dict[str, list[str]]] | None = None,
) -> Sidecar:
    """Sidecar with one records__actor table carrying a slice_only prop__tier."""
    columns = list(_SLICE_ONLY_ACTOR_COLUMNS)
    if extra_columns:
        columns.extend(extra_columns)
    return _bare_sidecar(
        [
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": columns,
                "rows": 0,
            }
        ],
        enum_domains=enum_domains,
    )


def _assert_slice_only_message(message: str, column: str = "prop__tier") -> None:
    """Assert a SliceOnlyColumnRefused message names the base column, class,
    and slice-fact contract clause (design doc § Error-message shapes)."""
    assert f"records__actor.{column}" in message
    assert "temporal_class: slice_only" in message
    assert "known only at the emit's slice" in message


# ---------------------------------------------------------------------------
# SliceOnlyColumnRefused — from / correlation / value_map.from
# ---------------------------------------------------------------------------


def test_from_refuses_slice_only() -> None:
    """from: reading a non-exempt slice_only column raises SliceOnlyColumnRefused."""
    sidecar = _slice_only_actor_sidecar()
    col = ColumnDecl(name="tier", **{"from": "prop__tier"})
    tbl = _make_table_decl(kind="actor", columns=[col], key=["tier"])
    with pytest.raises(ExportError) as exc_info:
        check_slice_only_column_reads(col, tbl, tbl.source, "records__actor", sidecar)
    _assert_slice_only_message(str(exc_info.value))


def test_correlation_refuses_slice_only() -> None:
    """correlation: reading a non-exempt slice_only column raises."""
    sidecar = _slice_only_actor_sidecar()
    col = ColumnDecl(name="tier", correlation="prop__tier")
    tbl = _make_table_decl(kind="actor", columns=[col], key=["tier"])
    with pytest.raises(ExportError) as exc_info:
        check_slice_only_column_reads(col, tbl, tbl.source, "records__actor", sidecar)
    _assert_slice_only_message(str(exc_info.value))


def test_value_map_from_refuses_slice_only() -> None:
    """derived.value_map.from: reading a non-exempt slice_only column raises."""
    sidecar = _slice_only_actor_sidecar()
    col = ColumnDecl(
        name="tier",
        derived=DerivedSpec(value_map={"from": "prop__tier", "map": {"gold": 1}}),
    )
    tbl = _make_table_decl(kind="actor", columns=[col], key=["tier"])
    with pytest.raises(ExportError) as exc_info:
        check_slice_only_column_reads(col, tbl, tbl.source, "records__actor", sidecar)
    _assert_slice_only_message(str(exc_info.value))


# ---------------------------------------------------------------------------
# SliceOnlyColumnRefused — derived: timestamp
# ---------------------------------------------------------------------------


def test_derived_timestamp_source_refuses_slice_only() -> None:
    """derived.timestamp.source: reading a non-exempt slice_only column raises."""
    sidecar = _slice_only_actor_sidecar()
    col = ColumnDecl(
        name="tier_ts",
        derived=DerivedSpec(timestamp=TimestampSpec(source="prop__tier")),
    )
    tbl = _make_table_decl(kind="actor", columns=[col], key=["tier_ts"])
    with pytest.raises(ExportError) as exc_info:
        check_slice_only_column_reads(col, tbl, tbl.source, "records__actor", sidecar)
    _assert_slice_only_message(str(exc_info.value))


# ---------------------------------------------------------------------------
# SliceOnlyColumnRefused — derived: elapsed
# ---------------------------------------------------------------------------


def _elapsed_col(
    correlate_on: str = "last_mutation_sim_time",
    start_source: str = "last_mutation_sim_time",
    end_source: str = "last_mutation_sim_time",
    other_where: dict[str, str] | None = None,
) -> ColumnDecl:
    """Build a wait_minutes ColumnDecl with elapsed spec, harmless defaults.

    `other_where` defaults to a harmless non-empty entry — ElapsedSpec now
    requires `other_where` non-empty (Breaking Changes)."""
    return ColumnDecl(
        name="wait",
        derived=DerivedSpec(
            elapsed=ElapsedSpec(
                correlate_on=correlate_on,
                other_where=other_where or {"last_mutation_sim_time": "0"},
                start_source=start_source,
                end_source=end_source,
                unit="minutes",
            )
        ),
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"correlate_on": "prop__tier"}, id="correlate_on"),
        pytest.param({"start_source": "prop__tier"}, id="start_source"),
        pytest.param({"end_source": "prop__tier"}, id="end_source"),
        pytest.param({"other_where": {"prop__tier": "gold"}}, id="other_where_key"),
    ],
)
def test_derived_elapsed_refuses_slice_only(kwargs: dict[str, object]) -> None:
    """Each derived.elapsed surface refuses a non-exempt slice_only column."""
    sidecar = _slice_only_actor_sidecar()
    col = _elapsed_col(**kwargs)  # type: ignore[arg-type]
    tbl = _make_table_decl(kind="actor", columns=[col], key=["wait"])
    with pytest.raises(ExportError) as exc_info:
        check_slice_only_column_reads(col, tbl, tbl.source, "records__actor", sidecar)
    _assert_slice_only_message(str(exc_info.value))


# ---------------------------------------------------------------------------
# SliceOnlyColumnRefused — derived: date_parse
# ---------------------------------------------------------------------------


def test_derived_date_parse_from_refuses_slice_only() -> None:
    """derived.date_parse.from: reading a non-exempt slice_only column
    raises — a date parse source joins the value-read surface list exactly
    as from/correlation/value_map.from do."""
    sidecar = _slice_only_actor_sidecar()
    col = ColumnDecl(
        name="tier_date",
        derived=DerivedSpec(
            date_parse=DateParseSpec(**{"from": "prop__tier", "format": "%Y-%m-%d"})
        ),
    )
    tbl = _make_table_decl(kind="actor", columns=[col], key=["tier_date"])
    with pytest.raises(ExportError) as exc_info:
        check_slice_only_column_reads(col, tbl, tbl.source, "records__actor", sidecar)
    _assert_slice_only_message(str(exc_info.value))


# ---------------------------------------------------------------------------
# SliceOnlyColumnRefused — derived: decimal / derived: json_precision
# (value-rendering-elections Phase 5)
# ---------------------------------------------------------------------------


def test_derived_decimal_from_refuses_slice_only() -> None:
    """derived.decimal.from: reading a non-exempt slice_only column raises —
    a decimal source joins the value-read surface list exactly as
    from/correlation/value_map.from do."""
    sidecar = _slice_only_actor_sidecar()
    col = ColumnDecl(
        name="tier_amount",
        derived=DerivedSpec(
            decimal=DecimalSpec(**{"from": "prop__tier", "as": [4, 3]})
        ),
    )
    tbl = _make_table_decl(kind="actor", columns=[col], key=["tier_amount"])
    with pytest.raises(ExportError) as exc_info:
        check_slice_only_column_reads(col, tbl, tbl.source, "records__actor", sidecar)
    _assert_slice_only_message(str(exc_info.value))


def test_derived_json_precision_from_refuses_slice_only() -> None:
    """derived.json_precision.from: reading a non-exempt slice_only column
    raises."""
    sidecar = _slice_only_actor_sidecar()
    col = ColumnDecl(
        name="tier_payload",
        derived=DerivedSpec(
            json_precision=JsonPrecisionSpec(
                **{"from": "prop__tier", "leaves": {"discount": 2}}
            )
        ),
    )
    tbl = _make_table_decl(kind="actor", columns=[col], key=["tier_payload"])
    with pytest.raises(ExportError) as exc_info:
        check_slice_only_column_reads(col, tbl, tbl.source, "records__actor", sidecar)
    _assert_slice_only_message(str(exc_info.value))


# ---------------------------------------------------------------------------
# SliceOnlyColumnRefused — records filter key
# ---------------------------------------------------------------------------


def test_filter_key_refuses_slice_only() -> None:
    """A records filter key resolving to a non-exempt slice_only column raises."""
    sidecar = _slice_only_actor_sidecar()
    tbl = _make_table_decl(
        kind="actor",
        source_kwargs={"filter": {"prop__tier": "gold"}},
    )
    with pytest.raises(ExportError) as exc_info:
        check_slice_only_filter_keys(tbl.source, tbl, "records__actor", sidecar)
    _assert_slice_only_message(str(exc_info.value))


# ---------------------------------------------------------------------------
# Discriminator carve-out — exempt at any class, non-sub-typed kind refused
# ---------------------------------------------------------------------------


def test_exempt_discriminator_projectable_via_from() -> None:
    """The exempt discriminator projects via `from` at any class, incl. slice_only."""
    sidecar = _slice_only_actor_sidecar(
        extra_columns=[
            prop_column(
                "prop__actor_type",
                "VARCHAR",
                history_tracked=False,
                temporal_class="slice_only",
            )
        ],
        enum_domains={"actor": {"actor_type": ["consultant", "nurse"]}},
    )
    col = ColumnDecl(name="actor_type", **{"from": "prop__actor_type"})
    tbl = _make_table_decl(kind="actor", columns=[col], key=["actor_type"])
    check_slice_only_column_reads(
        col, tbl, tbl.source, "records__actor", sidecar
    )  # must not raise


def test_exempt_discriminator_filterable() -> None:
    """The exempt discriminator is filterable at any class, incl. slice_only —
    init's classification pre-fill relies on filtering on it."""
    sidecar = _slice_only_actor_sidecar(
        extra_columns=[
            prop_column(
                "prop__actor_type",
                "VARCHAR",
                history_tracked=False,
                temporal_class="slice_only",
            )
        ],
        enum_domains={"actor": {"actor_type": ["consultant", "nurse"]}},
    )
    tbl = _make_table_decl(
        kind="actor",
        source_kwargs={"filter": {"prop__actor_type": "consultant"}},
    )
    check_slice_only_filter_keys(
        tbl.source, tbl, "records__actor", sidecar
    )  # must not raise


def test_non_subtyped_kinds_discriminator_refused() -> None:
    """A non-sub-typed kind's prop__<kind>_type marked slice_only is refused
    like any other column — the carve-out requires subtype_values non-empty."""
    sidecar = _slice_only_actor_sidecar(
        extra_columns=[
            prop_column(
                "prop__actor_type",
                "VARCHAR",
                history_tracked=False,
                temporal_class="slice_only",
            )
        ]
    )  # no enum_domains -> subtype_values(actor) is empty
    col = ColumnDecl(name="actor_type", **{"from": "prop__actor_type"})
    tbl = _make_table_decl(kind="actor", columns=[col], key=["actor_type"])
    with pytest.raises(ExportError, match="temporal_class: slice_only"):
        check_slice_only_column_reads(col, tbl, tbl.source, "records__actor", sidecar)


# ---------------------------------------------------------------------------
# Population scoping — membership/history grain surfaces are classless
# ---------------------------------------------------------------------------


def _membership_and_history_sidecar() -> Sidecar:
    """Sidecar carrying a slice_only records__actor plus a membership table and
    a history table, so membership/history grain surfaces can be exercised
    against a kind whose records columns are (outside their population) all
    slice_only."""
    membership_table = {
        "name": "membership__actor__team",
        "category": "membership",
        "record_kind": "actor",
        "property": "team",
        "columns": [
            identity_column("record_id", "VARCHAR"),
            {"name": "joined_sim_time", "type": "BIGINT"},
            {"name": "left_sim_time", "type": "BIGINT"},
            {"name": "elem__role", "type": "VARCHAR"},
            {"name": "member__actor__kind", "type": "VARCHAR"},
            {"name": "member__actor__id", "type": "VARCHAR"},
        ],
        "rows": 0,
    }
    history_table = {
        "name": "history",
        "category": "fixed",
        "columns": [
            {"name": "kind", "type": "VARCHAR"},
            {"name": "record_id", "type": "VARCHAR"},
            {"name": "property", "type": "VARCHAR"},
            {"name": "sim_time", "type": "BIGINT"},
            {"name": "value", "type": "VARCHAR"},
        ],
        "rows": 0,
    }
    actor_table = {
        "name": "records__actor",
        "category": "records",
        "record_kind": "actor",
        "columns": _SLICE_ONLY_ACTOR_COLUMNS,
        "rows": 0,
    }
    return _bare_sidecar([actor_table, membership_table, history_table])


def test_membership_source_scoping_untouched_by_slice_only_records() -> None:
    """A membership grain's source.where / member surface columns validate
    untouched against a kind whose records columns are slice_only — grain
    surface columns are classless, outside the population."""
    sidecar = _membership_and_history_sidecar()
    col = ColumnDecl(name="role", **{"from": "elem__role"})
    tbl = TableDecl(
        name="fact_team",
        role="fact",
        key=["record_id"],
        source=SourceDecl(
            grain="membership",
            kind="actor",
            property="team",
            where={"elem__role": "surgeon"},
        ),
        columns=[ColumnDecl(name="record_id", **{"from": "record_id"}), col],
    )
    config = DimensionalConfig(tables=[tbl])
    validate_table(tbl, config, sidecar, None, discard_notice_sink)  # must not raise


def test_history_grain_scoping_untouched_by_slice_only_records() -> None:
    """A history grain's source.property / value scoping validates untouched
    against a kind whose records columns are slice_only — grain surface
    columns are classless, outside the population."""
    sidecar = _membership_and_history_sidecar()
    tbl = TableDecl(
        name="fact_state",
        role="fact",
        key=["record_id"],
        source=SourceDecl(
            grain="history_point", kind="actor", property="status", value="active"
        ),
        columns=[
            ColumnDecl(name="record_id", **{"from": "record_id"}),
            ColumnDecl(name="value", **{"from": "value"}),
        ],
    )
    config = DimensionalConfig(tables=[tbl])
    validate_table(tbl, config, sidecar, None, discard_notice_sink)  # must not raise


# ---------------------------------------------------------------------------
# SliceOnlyColumnRefused fires on a full (window=None) compile — always-on
# ---------------------------------------------------------------------------


def test_validate_table_refuses_slice_only_on_full_compile() -> None:
    """SliceOnlyColumnRefused fires on a full (window=None) validate_table
    compile — the check is always-on, not incremental-only."""
    sidecar = _slice_only_actor_sidecar()
    col = ColumnDecl(name="tier", **{"from": "prop__tier"})
    tbl = _make_table_decl(kind="actor", columns=[col], key=["tier"])
    config = DimensionalConfig(tables=[tbl])
    with pytest.raises(ExportError) as exc_info:
        validate_table(tbl, config, sidecar, None, discard_notice_sink)
    _assert_slice_only_message(str(exc_info.value))


# ---------------------------------------------------------------------------
# ReservedPresentationName fires on a full (window=None) compile — always-on
# (the presentation-name posture, Phase 9)
# ---------------------------------------------------------------------------


def test_validate_table_refuses_last_mutation_sim_time_on_full_compile(
    tmp_path: Path,
) -> None:
    """An author-named output column last_mutation_sim_time raises at
    load-time naming the fix, on a full (window=None) compile — the
    presentation-name posture is always-on, not incremental-only."""
    emit_dir = build_test_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        id_col = ColumnDecl(name="id", **{"from": "record_id"})
        bad_col = ColumnDecl(name="last_mutation_sim_time", **{"from": "record_id"})
        tbl = _make_table_decl(columns=[id_col, bad_col], key=["id"])
        config = DimensionalConfig(tables=[tbl])
        with pytest.raises(ExportError, match="last_mutation_sim_time"):
            validate_table(tbl, config, emit.sidecar, None, discard_notice_sink)
