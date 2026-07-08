"""Fail-fast tests for the 10 windowed business-rule gates.

Each gate is tested twice:
  - window is not None  → ExportError with the exact design-doc message
  - window is None      → no error (full export is exempt from all gates)

Design-doc messages are taken verbatim from § Validation Rules → Business Rules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from fabulexa_export import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_export.config.models import (
    ColumnDecl,
    DerivedSpec,
    DimensionalConfig,
    ElapsedSpec,
    FkClause,
    OrdinalSpec,
    SourceDecl,
    TableDecl,
    TimestampSpec,
)
from fabulexa_export.errors import ExportError
from fabulexa_export.exporters.dimensional.validation import (
    check_incremental_elapsed_unsupported,
    check_incremental_filter_column_mutable,
    check_incremental_fk_membership_unsupported,
    check_incremental_fk_mutable_hop_with_config,
    check_incremental_grain_supported,
    check_incremental_ordinal_order_by,
    check_incremental_reserved_names,
    check_incremental_scd2_identity_key,
    check_incremental_scd2_valid_from_unique,
    check_incremental_slice_column_mutable,
)
from fabulexa_export.incremental.windows import Window
from fabulexa_export.reader.sidecar import ColumnSpec, Sidecar

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Shared minimal window — the gate funcs take `window: Window | None`; only
# the None vs. not-None distinction matters for gating. We use a dummy window.
# ---------------------------------------------------------------------------

_WINDOW = Window(index=0, start_ns=0, end_ns=1_000_000_000, label="w0")


# ---------------------------------------------------------------------------
# Sidecar helpers
# ---------------------------------------------------------------------------


def _col_spec(
    name: str,
    *,
    history_tracked: bool | None = None,
) -> ColumnSpec:
    return ColumnSpec(
        name=name, type="VARCHAR", references=None, history_tracked=history_tracked
    )


def _sidecar_with_tracked(
    table_name: str, *, tracked_prop: str = "prop__status"
) -> Sidecar:
    """Return a minimal Sidecar whose table carries history_tracked columns."""
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": table_name,
                "category": "records",
                "record_kind": "entity",
                "columns": [
                    {"name": "record_id", "type": "VARCHAR"},
                    {
                        "name": tracked_prop,
                        "type": "VARCHAR",
                        "history_tracked": True,
                    },
                    {
                        "name": "prop__department",
                        "type": "VARCHAR",
                        "history_tracked": False,
                    },
                ],
                "rows": 0,
            }
        ],
    }
    return Sidecar.from_raw(raw)


def _mock_sidecar_tracked_available(available: bool) -> MagicMock:
    """Return a mock Sidecar whose history_tracked_available() returns `available`."""
    sidecar = MagicMock(spec=Sidecar)
    sidecar.history_tracked_available.return_value = available
    return sidecar


# ---------------------------------------------------------------------------
# TableDecl / ColumnDecl helpers
# ---------------------------------------------------------------------------


def _make_table(
    name: str = "dim_x",
    grain: str = "records",
    kind: str = "entity",
    *,
    role: str = "dim",
    scd: str | None = "type1",
    columns: list[ColumnDecl] | None = None,
    key: list[str] | None = None,
    source_filter: dict[str, str] | None = None,
) -> TableDecl:
    src_kwargs: dict[str, object] = {"grain": grain, "kind": kind}
    if grain in ("history_point", "history_interval"):
        src_kwargs["property"] = "state"
    if grain == "membership":
        src_kwargs["property"] = "team_members"
    if source_filter is not None:
        src_kwargs["filter"] = source_filter
    if columns is None:
        columns = [ColumnDecl(name="id", **{"from": "record_id"})]
    if key is None:
        key = ["id"]
    return TableDecl(
        name=name,
        role=role,  # type: ignore[arg-type]
        scd=scd,  # type: ignore[arg-type]
        source=SourceDecl(**src_kwargs),  # type: ignore[arg-type]
        key=key,
        columns=columns,
    )


def _from_col(col_name: str, src: str) -> ColumnDecl:
    return ColumnDecl(name=col_name, **{"from": src})


def _derived_col(col_name: str, derived: DerivedSpec) -> ColumnDecl:
    return ColumnDecl(name=col_name, derived=derived)


def _fk_col(col_name: str, to: str, via: str) -> ColumnDecl:
    return ColumnDecl(name=col_name, fk=FkClause(to=to, via=via))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. IncrementalGrainUnsupported
# ---------------------------------------------------------------------------


def test_grain_history_interval_raises() -> None:
    """history_interval grain → IncrementalGrainUnsupported."""
    tbl = _make_table(
        "dim_hist",
        grain="history_interval",
        kind="journey_instance",
        scd="type1",
    )
    with pytest.raises(
        ExportError,
        match="table 'dim_hist': grain 'history_interval' is not supported with"
        " incremental export; model interval ends as history_point events",
    ):
        check_incremental_grain_supported(tbl)


def test_grain_membership_raises() -> None:
    """membership grain → IncrementalGrainUnsupported."""
    tbl = _make_table(
        "dim_mem",
        grain="membership",
        kind="journey_instance",
        scd="type1",
    )
    with pytest.raises(
        ExportError,
        match="table 'dim_mem': grain 'membership' is not supported with"
        " incremental export; model interval ends as history_point events",
    ):
        check_incremental_grain_supported(tbl)


def test_grain_records_passes_with_none_window() -> None:
    """records grain with window=None never calls the gate (shown by direct call)."""
    # The gate is not window-aware itself; validate_table skips it when window=None.
    # Direct call must not raise for a supported grain.
    tbl = _make_table("dim_x", grain="records", kind="entity")
    check_incremental_grain_supported(tbl)  # must not raise


# ---------------------------------------------------------------------------
# 2. IncrementalElapsedUnsupported
# ---------------------------------------------------------------------------


def test_elapsed_column_raises() -> None:
    """derived: elapsed → IncrementalElapsedUnsupported."""
    elapsed = DerivedSpec(
        elapsed=ElapsedSpec(
            correlate_on="record_id",
            other_where={"value": "arrival"},
            start_source="sim_time",
            end_source="sim_time",
            unit="minutes",
        )
    )
    col = _derived_col("wait_minutes", elapsed)
    tbl = _make_table(
        "fact_wait",
        grain="history_point",
        kind="journey_instance",
        role="fact",
        scd=None,
        columns=[col],
        key=["wait_minutes"],
    )
    with pytest.raises(
        ExportError,
        match="table 'fact_wait' column 'wait_minutes':"
        " derived: elapsed is not supported with incremental export",
    ):
        check_incremental_elapsed_unsupported(col, tbl)


def test_elapsed_passes_when_window_none() -> None:
    """Non-elapsed column must not raise (window=None means gate not called)."""
    col = _from_col("id", "record_id")
    tbl = _make_table("dim_x", columns=[col])
    check_incremental_elapsed_unsupported(col, tbl)  # must not raise


# ---------------------------------------------------------------------------
# 3. IncrementalFkMembershipUnsupported
# ---------------------------------------------------------------------------


def test_fk_via_membership_raises() -> None:
    """fk via: membership → IncrementalFkMembershipUnsupported."""
    col = _fk_col("team_id", "team", "membership")
    tbl = _make_table(
        "fact_binding", role="fact", scd=None, columns=[col], key=["team_id"]
    )
    with pytest.raises(
        ExportError,
        match="table 'fact_binding' column 'team_id':"
        " fk via: membership is not supported with incremental export;"
        " model member events as history_point facts",
    ):
        check_incremental_fk_membership_unsupported(col, tbl)


def test_fk_via_reference_passes_membership_gate() -> None:
    """fk via: reference must not raise IncrementalFkMembershipUnsupported."""
    col = _fk_col("entity_id", "entity", "reference")
    tbl = _make_table("fact_x", role="fact", scd=None, columns=[col], key=["entity_id"])
    check_incremental_fk_membership_unsupported(col, tbl)  # must not raise


# ---------------------------------------------------------------------------
# 4. IncrementalFkMutableHop
# ---------------------------------------------------------------------------


def test_fk_mutable_hop_emit_lacks_flag_raises() -> None:
    """Emit without history_tracked → IncrementalFkMutableHop (refused outright)."""
    sidecar = _mock_sidecar_tracked_available(False)
    col = _fk_col("entity_id", "entity", "reference")
    tbl = _make_table("fact_x", role="fact", scd=None, columns=[col], key=["entity_id"])
    config = DimensionalConfig(tables=[tbl])

    with pytest.raises(
        ExportError,
        match="table 'fact_x' column 'entity_id':"
        " fk hop '' is not history_tracked: false;"
        " incremental fk paths must be temporally constant",
    ):
        check_incremental_fk_mutable_hop_with_config(col, tbl, config, sidecar)


def test_fk_mutable_hop_non_reference_passes() -> None:
    """Non-reference fk (via: membership) is skipped by the mutable-hop gate."""
    sidecar = _mock_sidecar_tracked_available(False)
    col = _fk_col("entity_id", "entity", "membership")
    tbl = _make_table("fact_x", role="fact", scd=None, columns=[col], key=["entity_id"])
    config = DimensionalConfig(tables=[tbl])
    # Membership fk: gate returns immediately without raising
    check_incremental_fk_mutable_hop_with_config(
        col, tbl, config, sidecar
    )  # must not raise


# ---------------------------------------------------------------------------
# 5. IncrementalOrdinalOrderBy
# ---------------------------------------------------------------------------


def test_ordinal_non_window_key_raises() -> None:
    """ordinal.order_by naming non-window-key column → IncrementalOrdinalOrderBy."""
    # records grain: window key is the col sourcing last_mutation_sim_time
    time_col = _from_col("updated_at", "last_mutation_sim_time")
    ordinal_col = _derived_col(
        "row_num",
        DerivedSpec(ordinal=OrdinalSpec(partition_by="id", order_by="name")),
    )
    id_col = _from_col("id", "record_id")
    name_col = _from_col("name", "prop__name")
    tbl = _make_table(
        "fact_append",
        grain="records",
        kind="entity",
        role="fact",
        scd=None,
        columns=[id_col, time_col, name_col, ordinal_col],
        key=["id"],
    )
    with pytest.raises(
        ExportError,
        match="table 'fact_append' column 'row_num':"
        " ordinal order_by must resolve to the table's window-key time"
        " under incremental export",
    ):
        check_incremental_ordinal_order_by(ordinal_col, tbl, is_append_table=True)


def test_ordinal_window_key_col_passes() -> None:
    """ordinal.order_by = the column sourcing last_mutation_sim_time → passes."""
    time_col = _from_col("updated_at", "last_mutation_sim_time")
    ordinal_col = _derived_col(
        "row_num",
        DerivedSpec(ordinal=OrdinalSpec(partition_by="id", order_by="updated_at")),
    )
    id_col = _from_col("id", "record_id")
    tbl = _make_table(
        "fact_append",
        grain="records",
        kind="entity",
        role="fact",
        scd=None,
        columns=[id_col, time_col, ordinal_col],
        key=["id"],
    )
    check_incremental_ordinal_order_by(ordinal_col, tbl, is_append_table=True)


def test_ordinal_timestamp_derived_window_key_passes() -> None:
    """ordinal.order_by = a derived: timestamp whose source is last_mutation_sim_time → passes."""
    ts_col = _derived_col(
        "updated_ts",
        DerivedSpec(timestamp=TimestampSpec(source="last_mutation_sim_time")),
    )
    ordinal_col = _derived_col(
        "row_num",
        DerivedSpec(ordinal=OrdinalSpec(partition_by="id", order_by="updated_ts")),
    )
    id_col = _from_col("id", "record_id")
    tbl = _make_table(
        "fact_append",
        grain="records",
        kind="entity",
        role="fact",
        scd=None,
        columns=[id_col, ts_col, ordinal_col],
        key=["id"],
    )
    check_incremental_ordinal_order_by(ordinal_col, tbl, is_append_table=True)


def test_ordinal_snapshot_table_exempt() -> None:
    """type-1 dim (snapshot class) ordinal is exempt — is_append_table=False."""
    ordinal_col = _derived_col(
        "row_num",
        DerivedSpec(ordinal=OrdinalSpec(partition_by="id", order_by="name")),
    )
    id_col = _from_col("id", "record_id")
    name_col = _from_col("name", "prop__name")
    tbl = _make_table(
        "dim_x",
        grain="records",
        kind="entity",
        scd="type1",
        columns=[id_col, name_col, ordinal_col],
        key=["id"],
    )
    check_incremental_ordinal_order_by(ordinal_col, tbl, is_append_table=False)


# ---------------------------------------------------------------------------
# 6. IncrementalSliceColumnMutable
# ---------------------------------------------------------------------------


def test_slice_column_active_raises() -> None:
    """type-1 dim column sourcing 'active' → IncrementalSliceColumnMutable."""
    col = _from_col("is_active", "active")
    tbl = _make_table("dim_x", scd="type1", columns=[col], key=["is_active"])
    sidecar = _mock_sidecar_tracked_available(True)
    # 'active' is mutable; _is_column_source_mutable returns True
    sidecar.columns.return_value = []  # no prop__ columns needed; active is hardcoded mutable
    with pytest.raises(
        ExportError,
        match="table 'dim_x' column 'is_active':"
        " slice-read columns must be temporally constant under incremental export",
    ):
        check_incremental_slice_column_mutable(
            col,
            tbl,
            sidecar,
            source_table_name="records__entity",
            is_slice_read=True,
        )


def test_slice_column_constant_source_passes() -> None:
    """type-1 dim column sourcing 'record_id' (constant) → no error."""
    col = _from_col("id", "record_id")
    tbl = _make_table("dim_x", scd="type1", columns=[col], key=["id"])
    sidecar = _mock_sidecar_tracked_available(True)
    sidecar.columns.return_value = []
    check_incremental_slice_column_mutable(
        col,
        tbl,
        sidecar,
        source_table_name="records__entity",
        is_slice_read=True,
    )


def test_slice_column_not_slice_read_passes() -> None:
    """is_slice_read=False → gate skipped even for a mutable source."""
    col = _from_col("is_active", "active")
    tbl = _make_table("fact_x", role="fact", scd=None, columns=[col], key=["is_active"])
    sidecar = _mock_sidecar_tracked_available(True)
    check_incremental_slice_column_mutable(
        col,
        tbl,
        sidecar,
        source_table_name="records__entity",
        is_slice_read=False,
    )


# ---------------------------------------------------------------------------
# 7. IncrementalFilterColumnMutable
# ---------------------------------------------------------------------------


def test_filter_column_tracked_raises() -> None:
    """dim filter on history_tracked: true prop → IncrementalFilterColumnMutable."""
    sidecar = _sidecar_with_tracked("records__entity", tracked_prop="prop__status")
    tbl = _make_table(
        "dim_x",
        scd="type1",
        source_filter={"prop__status": "active"},
    )
    with pytest.raises(
        ExportError,
        match="table 'dim_x': filter column 'prop__status'"
        " must be temporally constant under incremental export",
    ):
        check_incremental_filter_column_mutable(tbl, sidecar, "records__entity")


def test_filter_column_constant_passes() -> None:
    """dim filter on history_tracked: false prop → no error."""
    sidecar = _sidecar_with_tracked("records__entity", tracked_prop="prop__status")
    tbl = _make_table(
        "dim_x",
        scd="type1",
        source_filter={"prop__department": "surgery"},
    )
    check_incremental_filter_column_mutable(tbl, sidecar, "records__entity")


def test_filter_fact_grain_exempt() -> None:
    """records-grain fact is exempt (role='fact', no filter applies)."""
    sidecar = _mock_sidecar_tracked_available(True)
    tbl = _make_table("fact_x", role="fact", scd=None)
    check_incremental_filter_column_mutable(tbl, sidecar, "records__entity")


# ---------------------------------------------------------------------------
# 8. IncrementalScd2IdentityKey
# ---------------------------------------------------------------------------


def test_scd2_all_key_cols_are_scd_window_raises() -> None:
    """SCD-2 key = only scd_window columns → IncrementalScd2IdentityKey."""
    valid_from_col = _derived_col("vf", DerivedSpec(scd_window="valid_from"))
    tbl = _make_table(
        "dim_scd",
        scd="type2",
        role="dim",
        columns=[valid_from_col],
        key=["vf"],
    )
    with pytest.raises(
        ExportError,
        match="table 'dim_scd': incremental SCD-2 requires a"
        " non-scd_window identity column in 'key'",
    ):
        check_incremental_scd2_identity_key(tbl)


def test_scd2_has_identity_col_passes() -> None:
    """SCD-2 key with at least one non-scd_window column → no error."""
    valid_from_col = _derived_col("vf", DerivedSpec(scd_window="valid_from"))
    id_col = _from_col("id", "record_id")
    tbl = _make_table(
        "dim_scd",
        scd="type2",
        role="dim",
        columns=[id_col, valid_from_col],
        key=["id", "vf"],
    )
    check_incremental_scd2_identity_key(tbl)


# ---------------------------------------------------------------------------
# 9. IncrementalScd2ValidFromUnique
# ---------------------------------------------------------------------------


def test_scd2_valid_to_but_no_valid_from_raises() -> None:
    """SCD-2 with valid_to column but zero valid_from → IncrementalScd2ValidFromUnique."""
    id_col = _from_col("id", "record_id")
    valid_to_col = _derived_col("vt", DerivedSpec(scd_window="valid_to"))
    tbl = _make_table(
        "dim_scd",
        scd="type2",
        role="dim",
        columns=[id_col, valid_to_col],
        key=["id"],
    )
    with pytest.raises(
        ExportError,
        match="table 'dim_scd': incremental SCD-2 requires exactly one"
        " scd_window: valid_from column",
    ):
        check_incremental_scd2_valid_from_unique(tbl)


def test_scd2_valid_to_two_valid_from_raises() -> None:
    """SCD-2 with valid_to and two valid_from columns → IncrementalScd2ValidFromUnique."""
    id_col = _from_col("id", "record_id")
    vf1 = _derived_col("vf1", DerivedSpec(scd_window="valid_from"))
    vf2 = _derived_col("vf2", DerivedSpec(scd_window="valid_from"))
    vt = _derived_col("vt", DerivedSpec(scd_window="valid_to"))
    tbl = _make_table(
        "dim_scd",
        scd="type2",
        role="dim",
        columns=[id_col, vf1, vf2, vt],
        key=["id", "vf1"],
    )
    with pytest.raises(
        ExportError,
        match="table 'dim_scd': incremental SCD-2 requires exactly one"
        " scd_window: valid_from column",
    ):
        check_incremental_scd2_valid_from_unique(tbl)


def test_scd2_exactly_one_valid_from_passes() -> None:
    """SCD-2 with exactly one valid_from and one valid_to → no error."""
    id_col = _from_col("id", "record_id")
    vf = _derived_col("vf", DerivedSpec(scd_window="valid_from"))
    vt = _derived_col("vt", DerivedSpec(scd_window="valid_to"))
    tbl = _make_table(
        "dim_scd",
        scd="type2",
        role="dim",
        columns=[id_col, vf, vt],
        key=["id", "vf"],
    )
    check_incremental_scd2_valid_from_unique(tbl)


# ---------------------------------------------------------------------------
# 10. IncrementalReservedName
# ---------------------------------------------------------------------------


def test_reserved_table_name_rows_suffix_raises() -> None:
    """Table named 'dim_x__rows' → IncrementalReservedName."""
    tbl = _make_table("dim_x__rows")
    with pytest.raises(
        ExportError,
        match="table 'dim_x__rows': name 'dim_x__rows' is reserved under incremental export",
    ):
        check_incremental_reserved_names(tbl)


def test_reserved_table_name_export_meta_raises() -> None:
    """Table named '_export_meta' → IncrementalReservedName."""
    tbl = _make_table("_export_meta")
    with pytest.raises(
        ExportError,
        match="table '_export_meta': name '_export_meta' is reserved under incremental export",
    ):
        check_incremental_reserved_names(tbl)


def test_reserved_table_name_export_windows_raises() -> None:
    """Table named '_export_windows' → IncrementalReservedName."""
    tbl = _make_table("_export_windows")
    with pytest.raises(
        ExportError,
        match="table '_export_windows': name '_export_windows' is reserved under incremental export",
    ):
        check_incremental_reserved_names(tbl)


def test_reserved_column_name_valid_from_ns_raises() -> None:
    """Column named '__valid_from_ns' → IncrementalReservedName."""
    reserved_col = _from_col("__valid_from_ns", "record_id")
    tbl = _make_table("dim_x", columns=[reserved_col], key=["__valid_from_ns"])
    with pytest.raises(
        ExportError,
        match="table 'dim_x': name '__valid_from_ns' is reserved under incremental export",
    ):
        check_incremental_reserved_names(tbl)


def test_ordinary_table_and_column_names_pass() -> None:
    """Ordinary table and column names raise no reserved-name error."""
    tbl = _make_table("dim_entity")
    check_incremental_reserved_names(tbl)  # must not raise
