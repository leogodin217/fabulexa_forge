"""Tests for dimensional exporter business rules.

Each test verifies one business rule raises ExportError with the documented
message, or (DiscriminatorValueObserved) emits a Notice.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink

from exporters._emit_fixtures import build_test_emit
from fabulexa_forge.config.models import (
    ColumnDecl,
    DerivedSpec,
    DimensionalConfig,
    OrdinalSpec,
    SourceDecl,
    TableDecl,
    TimestampSpec,
)
from fabulexa_forge.errors import ExportError
from fabulexa_forge.exporters.dimensional.validation import (
    check_excluded_kind_not_sourced,
    check_excluded_table_not_sourced,
    check_key_columns_declared,
    check_ordinal_refs_siblings,
    check_projection_column_exists,
    check_scd2_needs_history,
    check_source_table_exists,
    check_timestamp_source_available,
    validate_table,
)
from fabulexa_forge.reader.emit import open_emit


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
        from fabulexa_forge.exporters.dimensional.validation import (
            check_discriminator_value_observed,
        )

        sink = RecordingNoticeSink()
        check_discriminator_value_observed(src, emit.sidecar, sink)
        assert len(sink.notices) == 1
        assert sink.notices[0].code == "discriminator-value-unobserved"
        assert "nonexistent_type" in sink.notices[0].message


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
