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
    DerivedSpec,
    DimensionalConfig,
    ElapsedSpec,
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
    check_slice_only_column_reads,
    check_slice_only_filter_keys,
    check_source_table_exists,
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


def _assert_slice_only_message(message: str) -> None:
    """Assert a SliceOnlyColumnRefused message names the base column, class,
    and slice-fact contract clause (design doc § Error-message shapes)."""
    assert "records__actor.prop__tier" in message
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
    """Build a wait_minutes ColumnDecl with elapsed spec, harmless defaults."""
    return ColumnDecl(
        name="wait",
        derived=DerivedSpec(
            elapsed=ElapsedSpec(
                correlate_on=correlate_on,
                other_where=other_where or {},
                start_source=start_source,
                end_source=end_source,
                unit="minutes",
            )
        ),
    )


def test_derived_elapsed_correlate_on_refuses_slice_only() -> None:
    """derived.elapsed.correlate_on: a non-exempt slice_only column raises."""
    sidecar = _slice_only_actor_sidecar()
    col = _elapsed_col(correlate_on="prop__tier")
    tbl = _make_table_decl(kind="actor", columns=[col], key=["wait"])
    with pytest.raises(ExportError) as exc_info:
        check_slice_only_column_reads(col, tbl, tbl.source, "records__actor", sidecar)
    _assert_slice_only_message(str(exc_info.value))


def test_derived_elapsed_start_source_refuses_slice_only() -> None:
    """derived.elapsed.start_source: a non-exempt slice_only column raises."""
    sidecar = _slice_only_actor_sidecar()
    col = _elapsed_col(start_source="prop__tier")
    tbl = _make_table_decl(kind="actor", columns=[col], key=["wait"])
    with pytest.raises(ExportError) as exc_info:
        check_slice_only_column_reads(col, tbl, tbl.source, "records__actor", sidecar)
    _assert_slice_only_message(str(exc_info.value))


def test_derived_elapsed_end_source_refuses_slice_only() -> None:
    """derived.elapsed.end_source: a non-exempt slice_only column raises."""
    sidecar = _slice_only_actor_sidecar()
    col = _elapsed_col(end_source="prop__tier")
    tbl = _make_table_decl(kind="actor", columns=[col], key=["wait"])
    with pytest.raises(ExportError) as exc_info:
        check_slice_only_column_reads(col, tbl, tbl.source, "records__actor", sidecar)
    _assert_slice_only_message(str(exc_info.value))


def test_derived_elapsed_other_where_key_refuses_slice_only() -> None:
    """derived.elapsed.other_where key: a non-exempt slice_only column raises."""
    sidecar = _slice_only_actor_sidecar()
    col = _elapsed_col(other_where={"prop__tier": "gold"})
    tbl = _make_table_decl(kind="actor", columns=[col], key=["wait"])
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
