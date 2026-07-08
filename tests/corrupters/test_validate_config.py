"""Tests for validate_corrupt_config: the emit-dependent business-rule table
and the evolved-schema simulation across operations."""

from __future__ import annotations

import pytest

from fabulexa_export.config.models import CorruptConfig
from fabulexa_export.corrupters.validate import validate_corrupt_config
from fabulexa_export.errors import CorruptValidationError
from fabulexa_export.reader.sidecar import Sidecar, TableSpec

from ._helpers import column_spec, sidecar, table_spec

# ---------------------------------------------------------------------------
# Sidecar fixture
# ---------------------------------------------------------------------------


def _records_patient() -> TableSpec:
    return table_spec(
        "records__patient",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("presentation_id", "VARCHAR"),
            column_spec("created_sim_time", "BIGINT"),
            column_spec("active", "BOOLEAN"),
            column_spec("deactivated_at", "BIGINT"),
            column_spec("last_mutation_sim_time", "BIGINT"),
            column_spec("prop__name", "VARCHAR"),
            column_spec("prop__age", "BIGINT"),
            column_spec("prop__doctor_id", "VARCHAR", references="doctor"),
            column_spec("prop__patient_type", "VARCHAR"),
            column_spec("prop__weight_kg", "DOUBLE"),
            column_spec("prop__legacy_score", "INTEGER"),
        ),
        record_kind="patient",
    )


def _records_doctor() -> TableSpec:
    return table_spec(
        "records__doctor",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("prop__specialty", "VARCHAR"),
        ),
        record_kind="doctor",
    )


def _membership_ward() -> TableSpec:
    return table_spec(
        "membership__patient__ward",
        "membership",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("joined_sim_time", "BIGINT"),
            column_spec("left_sim_time", "BIGINT"),
            column_spec("elem__slot", "VARCHAR"),
            column_spec("elem__weight", "BIGINT"),
            column_spec("member__consultant__kind", "VARCHAR"),
            column_spec("member__consultant__id", "VARCHAR"),
        ),
        record_kind="patient",
        property_="ward",
    )


def _history() -> TableSpec:
    return table_spec(
        "history",
        "fixed",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("kind", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("property", "VARCHAR"),
            column_spec("sim_time", "BIGINT"),
            column_spec("value", "VARCHAR"),
        ),
    )


def _sidecar() -> Sidecar:
    return sidecar(
        (_records_patient(), _records_doctor(), _membership_ward(), _history()),
        enum_domains={"patient": {"patient_type": ("adult", "minor")}},
    )


# ---------------------------------------------------------------------------
# Operation-dict builders
# ---------------------------------------------------------------------------


def _null_cells(
    table: str, columns: list[str], where: dict[str, str] | None = None
) -> dict[str, object]:
    target: dict[str, object] = {"table": table, "columns": columns}
    if where is not None:
        target["where"] = where
    return {"kind": "null_cells", "target": target, "amount": {"rate": 0.5}}


def _null_cells_target(
    target: dict[str, object], columns: list[str], where: dict[str, str] | None = None
) -> dict[str, object]:
    t = dict(target)
    t["columns"] = columns
    if where is not None:
        t["where"] = where
    return {"kind": "null_cells", "target": t, "amount": {"rate": 0.5}}


def _dangle_reference(table: str, columns: list[str]) -> dict[str, object]:
    return {
        "kind": "dangle_reference",
        "target": {"table": table, "columns": columns},
        "amount": {"rate": 0.5},
    }


def _dangle_reference_target(
    target: dict[str, object], columns: list[str]
) -> dict[str, object]:
    t = dict(target)
    t["columns"] = columns
    return {"kind": "dangle_reference", "target": t, "amount": {"rate": 0.5}}


def _mispoint_reference(table: str, columns: list[str]) -> dict[str, object]:
    return {
        "kind": "mispoint_reference",
        "target": {"table": table, "columns": columns},
        "amount": {"rate": 0.5},
    }


def _mispoint_reference_target(
    target: dict[str, object], columns: list[str]
) -> dict[str, object]:
    t = dict(target)
    t["columns"] = columns
    return {"kind": "mispoint_reference", "target": t, "amount": {"rate": 0.5}}


def _mispoint_reference_placement(
    target: dict[str, object], columns: list[str], placement: dict[str, object]
) -> dict[str, object]:
    t = dict(target)
    t["columns"] = columns
    return {
        "kind": "mispoint_reference",
        "target": t,
        "amount": {"rate": 0.5},
        "placement": placement,
    }


def _duplicate_rows_jitter(table: str, columns: list[str]) -> dict[str, object]:
    return {
        "kind": "duplicate_rows",
        "target": {"table": table, "columns": columns},
        "amount": {"count": 1},
        "jitter": {"shape": "uniform", "low": -1.0, "high": 1.0},
    }


def _duplicate_rows_exact(table: str) -> dict[str, object]:
    return {
        "kind": "duplicate_rows",
        "target": {"table": table},
        "amount": {"count": 1},
    }


def _delete_rows(target: dict[str, object]) -> dict[str, object]:
    return {"kind": "delete_rows", "target": target, "amount": {"rate": 0.5}}


def _delete_rows_placement(
    target: dict[str, object], placement: dict[str, object]
) -> dict[str, object]:
    return {
        "kind": "delete_rows",
        "target": target,
        "amount": {"rate": 0.5},
        "placement": placement,
    }


def _insert_rows(table: str, columns: list[str] | None = None) -> dict[str, object]:
    target: dict[str, object] = {"table": table}
    if columns is not None:
        target["columns"] = columns
    return {"kind": "insert_rows", "target": target, "amount": {"rate": 0.5}}


def _insert_rows_target(
    target: dict[str, object], columns: list[str] | None = None
) -> dict[str, object]:
    t = dict(target)
    if columns is not None:
        t["columns"] = columns
    return {"kind": "insert_rows", "target": t, "amount": {"rate": 0.5}}


def _drop_events(target: dict[str, object]) -> dict[str, object]:
    return {"kind": "drop_events", "target": target, "amount": {"rate": 0.5}}


def _drop_events_placement(
    target: dict[str, object], placement: dict[str, object]
) -> dict[str, object]:
    return {
        "kind": "drop_events",
        "target": target,
        "amount": {"rate": 0.5},
        "placement": placement,
    }


def _freeze_series(target: dict[str, object]) -> dict[str, object]:
    return {
        "kind": "freeze_series",
        "target": target,
        "amount": {"rate": 0.5},
        "cut": "random",
    }


def _shift_sim_time(
    target: dict[str, object], shift: dict[str, object] | None = None
) -> dict[str, object]:
    return {
        "kind": "shift_sim_time",
        "target": target,
        "amount": {"rate": 0.5},
        "shift": shift if shift is not None else {"kind": "collide"},
    }


def _distort_intervals(
    target: dict[str, object], mode: str = "gap"
) -> dict[str, object]:
    return {
        "kind": "distort_intervals",
        "target": target,
        "amount": {"rate": 0.5},
        "mode": mode,
    }


def _null_cells_placement(
    target: dict[str, object], columns: list[str], placement: dict[str, object]
) -> dict[str, object]:
    t = dict(target)
    t["columns"] = columns
    return {
        "kind": "null_cells",
        "target": t,
        "amount": {"rate": 0.5},
        "placement": placement,
    }


def _schema_drift(
    table: str,
    *,
    rename_to: dict[str, str] | None = None,
    retype_to: dict[str, str] | None = None,
    drop: list[str] | None = None,
) -> dict[str, object]:
    op: dict[str, object] = {"kind": "schema_drift", "target": {"table": table}}
    if rename_to is not None:
        op["rename_to"] = rename_to
    if retype_to is not None:
        op["retype_to"] = retype_to
    if drop is not None:
        op["drop"] = drop
    return op


def _config(*operations: dict[str, object]) -> CorruptConfig:
    return CorruptConfig.model_validate({"seed": 1, "operations": list(operations)})


# ---------------------------------------------------------------------------
# TableExists / ColumnsExist / WhereColumnsExist
# ---------------------------------------------------------------------------


def test_table_exists_rejects_unknown_table() -> None:
    config = _config(_null_cells("records__nope", ["prop__name"]))
    with pytest.raises(
        CorruptValidationError, match=r"operation\[0\].*not in this emit"
    ):
        validate_corrupt_config(config, _sidecar())


def test_column_entries_match_rejects_unknown_column() -> None:
    config = _config(_null_cells("records__patient", ["prop__nope"]))
    with pytest.raises(
        CorruptValidationError, match=r"operation\[0\].*matches no eligible column"
    ):
        validate_corrupt_config(config, _sidecar())


def test_where_columns_exist_rejects_unknown_column() -> None:
    config = _config(
        _null_cells("records__patient", ["prop__name"], where={"prop__nope": "x"})
    )
    with pytest.raises(
        CorruptValidationError, match=r"operation\[0\].*where names unknown column"
    ):
        validate_corrupt_config(config, _sidecar())


# ---------------------------------------------------------------------------
# SelectorResolves (generalizes TableExists)
# ---------------------------------------------------------------------------


def test_selector_resolves_rejects_tables_entry_absent_from_emit() -> None:
    config = _config(
        _null_cells_target(
            {"tables": ["records__patient", "records__nope"]}, ["prop__name"]
        )
    )
    with pytest.raises(
        CorruptValidationError,
        match=r"operation\[0\].*'records__nope'.*not in this emit",
    ):
        validate_corrupt_config(config, _sidecar())


def test_selector_resolves_rejects_zero_match_glob() -> None:
    config = _config(_null_cells_target({"glob": "no_such__*"}, ["prop__name"]))
    with pytest.raises(
        CorruptValidationError, match=r"operation\[0\].*matches no table in this emit"
    ):
        validate_corrupt_config(config, _sidecar())


def test_selector_resolves_rejects_zero_match_record_kind() -> None:
    config = _config(
        _null_cells_target({"record_kind": "no_such_kind"}, ["prop__name"])
    )
    with pytest.raises(
        CorruptValidationError,
        match=r"operation\[0\].*matches no table in this emit",
    ):
        validate_corrupt_config(config, _sidecar())


# ---------------------------------------------------------------------------
# ColumnEntriesMatch: multi-table match domain
# ---------------------------------------------------------------------------


def test_column_entries_match_passes_when_entry_matches_in_one_of_several_tables() -> (
    None
):
    """prop__name exists only on records__patient, not records__doctor; the
    category selector resolves both, and the entry still passes."""
    config = _config(_null_cells_target({"category": "records"}, ["prop__name"]))
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_column_entries_match_rejects_when_entry_matches_in_no_resolved_table() -> None:
    config = _config(_null_cells_target({"category": "records"}, ["prop__nonexistent"]))
    with pytest.raises(
        CorruptValidationError,
        match=r"operation\[0\].*'prop__nonexistent'.*matches no eligible column",
    ):
        validate_corrupt_config(config, _sidecar())


# ---------------------------------------------------------------------------
# WhereColumnsExist: multi-table match domain
# ---------------------------------------------------------------------------


def test_where_columns_exist_generalized_passes_when_key_in_one_of_several_tables() -> (
    None
):
    """prop__specialty exists only on records__doctor, not records__patient."""
    config = _config(
        _null_cells_target(
            {"category": "records"}, ["prop__name"], where={"prop__specialty": "x"}
        )
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_where_columns_exist_generalized_rejects_when_key_in_no_table() -> None:
    config = _config(
        _null_cells_target(
            {"category": "records"}, ["prop__name"], where={"prop__nope": "x"}
        )
    )
    with pytest.raises(
        CorruptValidationError, match=r"operation\[0\].*where names unknown column"
    ):
        validate_corrupt_config(config, _sidecar())


# ---------------------------------------------------------------------------
# NullableColumns
# ---------------------------------------------------------------------------


def test_nullable_columns_rejects_structural_column() -> None:
    config = _config(_null_cells("records__patient", ["record_id"]))
    with pytest.raises(CorruptValidationError, match=r"matches no eligible column"):
        validate_corrupt_config(config, _sidecar())


def test_nullable_columns_accepts_value_columns() -> None:
    config = _config(_null_cells("records__patient", ["prop__name", "deactivated_at"]))
    validate_corrupt_config(config, _sidecar())  # does not raise


# ---------------------------------------------------------------------------
# ReferenceColumns
# ---------------------------------------------------------------------------


def test_reference_columns_rejects_non_reference_column() -> None:
    config = _config(_dangle_reference("records__patient", ["prop__name"]))
    with pytest.raises(CorruptValidationError, match=r"matches no eligible column"):
        validate_corrupt_config(config, _sidecar())


def test_reference_columns_glob_excludes_non_reference_columns() -> None:
    """A glob matching only existing-but-ineligible columns is still a dead entry."""
    config = _config(_dangle_reference("membership__patient__ward", ["elem__*"]))
    with pytest.raises(CorruptValidationError, match=r"matches no eligible column"):
        validate_corrupt_config(config, _sidecar())


def test_reference_columns_accepts_membership_member_id() -> None:
    config = _config(
        _dangle_reference("membership__patient__ward", ["member__consultant__id"])
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_column_entries_match_domain_is_reference_columns_for_dangle_reference() -> (
    None
):
    """The match domain for dangle_reference is reference columns only, even
    when the selector resolves a whole category."""
    config = _config(
        _dangle_reference_target({"category": "membership"}, ["member__*"])
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_reference_columns_rejects_non_reference_column_for_mispoint_reference() -> (
    None
):
    """The ReferenceColumns rule extends unchanged to mispoint_reference."""
    config = _config(_mispoint_reference("records__patient", ["prop__name"]))
    with pytest.raises(CorruptValidationError, match=r"matches no eligible column"):
        validate_corrupt_config(config, _sidecar())


def test_reference_columns_accepts_membership_member_id_for_mispoint_reference() -> (
    None
):
    config = _config(
        _mispoint_reference("membership__patient__ward", ["member__consultant__id"])
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_reference_columns_accepts_records_prop_reference_for_mispoint_reference() -> (
    None
):
    """A records prop__ column with references set is eligible."""
    config = _config(_mispoint_reference("records__patient", ["prop__doctor_id"]))
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_column_entries_match_domain_is_reference_columns_for_mispoint_reference() -> (
    None
):
    """A glob entry matches only reference-eligible columns, even when the
    selector resolves a whole category."""
    config = _config(
        _mispoint_reference_target({"category": "membership"}, ["member__*"])
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_mispoint_reference_where_extends_unchanged() -> None:
    """WhereColumnsExist extends unchanged over mispoint_reference."""
    config = _config(
        _mispoint_reference_target(
            {"table": "records__patient", "where": {"prop__patient_type": "x"}},
            ["prop__doctor_id"],
        )
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_mispoint_reference_placement_extends_unchanged() -> None:
    """PlacementColumnExists extends unchanged over mispoint_reference."""
    config = _config(
        _mispoint_reference_placement(
            {"table": "records__patient"},
            ["prop__doctor_id"],
            {"kind": "correlated", "column": "prop__age", "value": "10", "weight": 2.0},
        )
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


# ---------------------------------------------------------------------------
# DriftColumnsNonStructural
# ---------------------------------------------------------------------------


def test_drift_columns_non_structural_rejects_structural_prefix() -> None:
    config = _config(_schema_drift("records__patient", drop=["record_id"]))
    with pytest.raises(CorruptValidationError, match=r"not a drift-eligible payload"):
        validate_corrupt_config(config, _sidecar())


def test_drift_columns_non_structural_rejects_history_column() -> None:
    config = _config(_schema_drift("history", drop=["value"]))
    with pytest.raises(CorruptValidationError, match=r"not a drift-eligible payload"):
        validate_corrupt_config(config, _sidecar())


def test_drift_columns_non_structural_rejects_reference_column() -> None:
    config = _config(_schema_drift("records__patient", drop=["prop__doctor_id"]))
    with pytest.raises(CorruptValidationError, match=r"not a drift-eligible payload"):
        validate_corrupt_config(config, _sidecar())


def test_drift_columns_non_structural_rejects_enum_domains_discriminator() -> None:
    config = _config(_schema_drift("records__patient", drop=["prop__patient_type"]))
    with pytest.raises(CorruptValidationError, match=r"not a drift-eligible payload"):
        validate_corrupt_config(config, _sidecar())


def test_drift_columns_non_structural_accepts_payload_columns() -> None:
    config = _config(
        _schema_drift("records__patient", rename_to={"prop__name": "prop__full_name"})
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


# ---------------------------------------------------------------------------
# DriftRenamePreservesCategory
# ---------------------------------------------------------------------------


def test_drift_rename_preserves_category_rejects_cross_category_rename() -> None:
    config = _config(
        _schema_drift("records__patient", rename_to={"prop__name": "elem__name"})
    )
    with pytest.raises(CorruptValidationError, match=r"changes .* column category"):
        validate_corrupt_config(config, _sidecar())


# ---------------------------------------------------------------------------
# JitterColumnsNumeric
# ---------------------------------------------------------------------------


def test_jitter_columns_numeric_rejects_varchar_payload() -> None:
    config = _config(_duplicate_rows_jitter("records__patient", ["prop__name"]))
    with pytest.raises(CorruptValidationError, match=r"matches no eligible column"):
        validate_corrupt_config(config, _sidecar())


def test_jitter_columns_numeric_rejects_sim_time_column() -> None:
    config = _config(
        _duplicate_rows_jitter("membership__patient__ward", ["left_sim_time"])
    )
    with pytest.raises(CorruptValidationError, match=r"matches no eligible column"):
        validate_corrupt_config(config, _sidecar())


def test_jitter_columns_numeric_accepts_numeric_payload() -> None:
    config = _config(_duplicate_rows_jitter("records__patient", ["prop__age"]))
    validate_corrupt_config(config, _sidecar())  # does not raise


# ---------------------------------------------------------------------------
# MutableColumns
# ---------------------------------------------------------------------------

_SENTINEL_MUTATION: dict[str, object] = {"kind": "sentinel", "value": "SENTINEL"}
_TYPO_MUTATION: dict[str, object] = {"kind": "typo"}
_CASE_MUTATION: dict[str, object] = {"kind": "case", "form": "upper"}
_PRECISION_DROP_MUTATION: dict[str, object] = {"kind": "precision_drop", "digits": 2}
_SCALE_MUTATION: dict[str, object] = {"kind": "scale", "factor": 1000.0}
_RESAMPLE_MUTATION: dict[str, object] = {"kind": "resample"}
_OUT_OF_DOMAIN_MUTATION: dict[str, object] = {"kind": "out_of_domain"}


def _mutate_cells(
    table: str, columns: list[str], mutation: dict[str, object]
) -> dict[str, object]:
    return {
        "kind": "mutate_cells",
        "target": {"table": table, "columns": columns},
        "amount": {"rate": 0.5},
        "mutation": mutation,
    }


def _mutate_cells_placement(
    target: dict[str, object],
    columns: list[str],
    mutation: dict[str, object],
    placement: dict[str, object],
) -> dict[str, object]:
    t = dict(target)
    t["columns"] = columns
    return {
        "kind": "mutate_cells",
        "target": t,
        "amount": {"rate": 0.5},
        "mutation": mutation,
        "placement": placement,
    }


def test_mutable_columns_rejects_reference_prop_column() -> None:
    """sentinel (any-type gate) still excludes prop__doctor_id: is_mutable_column
    requires references unset."""
    config = _config(
        _mutate_cells("records__patient", ["prop__doctor_id"], _SENTINEL_MUTATION)
    )
    with pytest.raises(
        CorruptValidationError,
        match=(
            r"operation 0 \(mutate_cells\): columns entry 'prop__doctor_id' "
            r"matches no sentinel-eligible column"
        ),
    ):
        validate_corrupt_config(config, _sidecar())


def test_mutable_columns_case_rejects_bigint_column_type_gate() -> None:
    """prop__age is BIGINT; case's type gate is VARCHAR-only."""
    config = _config(_mutate_cells("records__patient", ["prop__age"], _CASE_MUTATION))
    with pytest.raises(
        CorruptValidationError,
        match=(
            r"operation 0 \(mutate_cells\): columns entry 'prop__age' "
            r"matches no case-eligible column"
        ),
    ):
        validate_corrupt_config(config, _sidecar())


def test_mutable_columns_accepts_history_value_for_varchar_gated_kind() -> None:
    """history.value is VARCHAR: sentinel (any-type gate) admits it -- the
    fixed-category branch of MutableColumns."""
    config = _config(_mutate_cells("history", ["value"], _SENTINEL_MUTATION))
    validate_corrupt_config(config, _sidecar())  # does not raise

    config_case = _config(_mutate_cells("history", ["value"], _CASE_MUTATION))
    validate_corrupt_config(config_case, _sidecar())  # does not raise


def test_mutable_columns_out_of_domain_rejects_history_value() -> None:
    """out_of_domain requires a records prop__<p> enum-domain pair; history.value
    is ineligible per the matrix regardless of its VARCHAR type."""
    config = _config(_mutate_cells("history", ["value"], _OUT_OF_DOMAIN_MUTATION))
    with pytest.raises(
        CorruptValidationError,
        match=(
            r"operation 0 \(mutate_cells\): columns entry 'value' "
            r"matches no out_of_domain-eligible column"
        ),
    ):
        validate_corrupt_config(config, _sidecar())


@pytest.mark.parametrize(
    "column", ["sim_time", "property", "kind", "record_id", "fork_path"]
)
def test_mutable_columns_rejects_structural_history_columns(column: str) -> None:
    """Only history.value is name-class-eligible; every other structural
    history column stays ineligible even under sentinel's any-type gate."""
    config = _config(_mutate_cells("history", [column], _SENTINEL_MUTATION))
    with pytest.raises(
        CorruptValidationError,
        match=(
            rf"operation 0 \(mutate_cells\): columns entry '{column}' "
            r"matches no sentinel-eligible column"
        ),
    ):
        validate_corrupt_config(config, _sidecar())


def test_mutable_columns_pattern_entry_matches_eligible_set() -> None:
    config = _config(_mutate_cells("records__patient", ["prop__na*"], _CASE_MUTATION))
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_mutable_columns_accepts_presentation_id() -> None:
    config = _config(
        _mutate_cells("records__patient", ["presentation_id"], _SENTINEL_MUTATION)
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_mutable_columns_accepts_membership_elem() -> None:
    config = _config(
        _mutate_cells("membership__patient__ward", ["elem__slot"], _CASE_MUTATION)
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_mutable_columns_precision_drop_rejects_bigint_column_type_gate() -> None:
    """prop__age is BIGINT; precision_drop's type gate is DOUBLE-only."""
    config = _config(
        _mutate_cells("records__patient", ["prop__age"], _PRECISION_DROP_MUTATION)
    )
    with pytest.raises(
        CorruptValidationError,
        match=(
            r"operation 0 \(mutate_cells\): columns entry 'prop__age' "
            r"matches no precision_drop-eligible column"
        ),
    ):
        validate_corrupt_config(config, _sidecar())


def test_mutable_columns_scale_accepts_bigint_and_double() -> None:
    config_bigint = _config(
        _mutate_cells("records__patient", ["prop__age"], _SCALE_MUTATION)
    )
    validate_corrupt_config(config_bigint, _sidecar())  # does not raise

    config_double = _config(
        _mutate_cells("records__patient", ["prop__weight_kg"], _SCALE_MUTATION)
    )
    validate_corrupt_config(config_double, _sidecar())  # does not raise


def test_mutable_columns_scale_rejects_varchar_column_type_gate() -> None:
    config = _config(_mutate_cells("records__patient", ["prop__name"], _SCALE_MUTATION))
    with pytest.raises(
        CorruptValidationError,
        match=(
            r"operation 0 \(mutate_cells\): columns entry 'prop__name' "
            r"matches no scale-eligible column"
        ),
    ):
        validate_corrupt_config(config, _sidecar())


def test_mutable_columns_deviant_integer_type_ineligible_for_typed_kinds() -> None:
    """prop__legacy_score is INTEGER (not BIGINT): a deviant type is a dead
    entry for every typed kind, never a silent skip."""
    config = _config(
        _mutate_cells("records__patient", ["prop__legacy_score"], _SCALE_MUTATION)
    )
    with pytest.raises(
        CorruptValidationError,
        match=(
            r"operation 0 \(mutate_cells\): columns entry 'prop__legacy_score' "
            r"matches no scale-eligible column"
        ),
    ):
        validate_corrupt_config(config, _sidecar())


def test_mutable_columns_typo_accepts_varchar_and_bigint() -> None:
    config_varchar = _config(
        _mutate_cells("records__patient", ["prop__name"], _TYPO_MUTATION)
    )
    validate_corrupt_config(config_varchar, _sidecar())  # does not raise

    config_bigint = _config(
        _mutate_cells("records__patient", ["prop__age"], _TYPO_MUTATION)
    )
    validate_corrupt_config(config_bigint, _sidecar())  # does not raise


def test_mutable_columns_typo_rejects_double_column_type_gate() -> None:
    config = _config(
        _mutate_cells("records__patient", ["prop__weight_kg"], _TYPO_MUTATION)
    )
    with pytest.raises(
        CorruptValidationError,
        match=(
            r"operation 0 \(mutate_cells\): columns entry 'prop__weight_kg' "
            r"matches no typo-eligible column"
        ),
    ):
        validate_corrupt_config(config, _sidecar())


def test_mutable_columns_resample_accepts_any_type() -> None:
    for column in ("prop__name", "prop__age", "prop__weight_kg"):
        config = _config(
            _mutate_cells("records__patient", [column], _RESAMPLE_MUTATION)
        )
        validate_corrupt_config(config, _sidecar())  # does not raise


def test_mutable_columns_out_of_domain_accepts_declared_enum_domain() -> None:
    """prop__patient_type is VARCHAR and the sidecar declares
    enum_domains['patient']['patient_type']."""
    config = _config(
        _mutate_cells(
            "records__patient", ["prop__patient_type"], _OUT_OF_DOMAIN_MUTATION
        )
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_mutable_columns_out_of_domain_rejects_varchar_without_enum_domain() -> None:
    """prop__name is VARCHAR but the sidecar declares no enum_domains entry
    for it: the enum-domain gate rejects it as a dead entry."""
    config = _config(
        _mutate_cells("records__patient", ["prop__name"], _OUT_OF_DOMAIN_MUTATION)
    )
    with pytest.raises(
        CorruptValidationError,
        match=(
            r"operation 0 \(mutate_cells\): columns entry 'prop__name' "
            r"matches no out_of_domain-eligible column"
        ),
    ):
        validate_corrupt_config(config, _sidecar())


def test_mutable_columns_out_of_domain_rejects_presentation_id() -> None:
    config = _config(
        _mutate_cells("records__patient", ["presentation_id"], _OUT_OF_DOMAIN_MUTATION)
    )
    with pytest.raises(
        CorruptValidationError,
        match=(
            r"operation 0 \(mutate_cells\): columns entry 'presentation_id' "
            r"matches no out_of_domain-eligible column"
        ),
    ):
        validate_corrupt_config(config, _sidecar())


def test_mutable_columns_out_of_domain_rejects_membership_elem() -> None:
    config = _config(
        _mutate_cells(
            "membership__patient__ward", ["elem__slot"], _OUT_OF_DOMAIN_MUTATION
        )
    )
    with pytest.raises(
        CorruptValidationError,
        match=(
            r"operation 0 \(mutate_cells\): columns entry 'elem__slot' "
            r"matches no out_of_domain-eligible column"
        ),
    ):
        validate_corrupt_config(config, _sidecar())


def test_mutable_columns_retyped_column_moves_across_type_gates() -> None:
    """An earlier schema_drift retype moves a column across mutate_cells type
    gates, exactly as it does for JitterColumnsNumeric."""
    config = _config(
        _schema_drift("records__patient", retype_to={"prop__age": "DOUBLE"}),
        _mutate_cells("records__patient", ["prop__age"], _PRECISION_DROP_MUTATION),
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_mutate_cells_placement_extends_unchanged_and_passes() -> None:
    """PlacementColumnExists extends over mutate_cells unchanged."""
    config = _config(
        _mutate_cells_placement(
            {"table": "records__patient"},
            ["prop__name"],
            _CASE_MUTATION,
            {"kind": "correlated", "column": "prop__age", "value": "10", "weight": 2.0},
        )
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_mutate_cells_placement_extends_unchanged_and_rejects_missing_column() -> None:
    config = _config(
        _mutate_cells_placement(
            {"table": "records__patient"},
            ["prop__name"],
            _CASE_MUTATION,
            {"kind": "correlated", "column": "prop__nope", "value": "x", "weight": 2.0},
        )
    )
    with pytest.raises(
        CorruptValidationError,
        match=r"operation\[0\].*placement column 'prop__nope'",
    ):
        validate_corrupt_config(config, _sidecar())


# ---------------------------------------------------------------------------
# ConflictMutableColumns
# ---------------------------------------------------------------------------


def _duplicate_rows_mutation(
    table: str, columns: list[str], mutation: dict[str, object]
) -> dict[str, object]:
    return {
        "kind": "duplicate_rows",
        "target": {"table": table, "columns": columns},
        "amount": {"count": 1},
        "mutation": mutation,
    }


def test_conflict_mutable_columns_rejects_history_only_target() -> None:
    """history.value is deliberately excluded from conflict-eligible columns
    (design doc § Semantics, the mutation mode): a history-only target is a
    dead entry."""
    config = _config(_duplicate_rows_mutation("history", ["value"], _SENTINEL_MUTATION))
    with pytest.raises(
        CorruptValidationError,
        match=(
            r"operation 0 \(duplicate_rows\): columns entry 'value' "
            r"matches no sentinel-eligible column"
        ),
    ):
        validate_corrupt_config(config, _sidecar())


def test_conflict_mutable_columns_rejects_type_gated_mismatch() -> None:
    """prop__age is BIGINT; case's type gate is VARCHAR-only."""
    config = _config(
        _duplicate_rows_mutation("records__patient", ["prop__age"], _CASE_MUTATION)
    )
    with pytest.raises(
        CorruptValidationError,
        match=(
            r"operation 0 \(duplicate_rows\): columns entry 'prop__age' "
            r"matches no case-eligible column"
        ),
    ):
        validate_corrupt_config(config, _sidecar())


def test_conflict_mutable_columns_rejects_out_of_domain_without_enum_domain() -> None:
    """prop__name is VARCHAR but the sidecar declares no enum_domains entry
    for it: the enum-domain gate rejects it as a dead entry."""
    config = _config(
        _duplicate_rows_mutation(
            "records__patient", ["prop__name"], _OUT_OF_DOMAIN_MUTATION
        )
    )
    with pytest.raises(
        CorruptValidationError,
        match=(
            r"operation 0 \(duplicate_rows\): columns entry 'prop__name' "
            r"matches no out_of_domain-eligible column"
        ),
    ):
        validate_corrupt_config(config, _sidecar())


def test_conflict_mutable_columns_accepts_membership_elem() -> None:
    config = _config(
        _duplicate_rows_mutation(
            "membership__patient__ward", ["elem__slot"], _CASE_MUTATION
        )
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


# ---------------------------------------------------------------------------
# Evolved-schema simulation
# ---------------------------------------------------------------------------


def test_evolved_schema_rename_hides_old_name_exposes_new_name() -> None:
    old_name_config = _config(
        _schema_drift("records__patient", rename_to={"prop__name": "prop__full_name"}),
        _null_cells("records__patient", ["prop__name"]),
    )
    with pytest.raises(
        CorruptValidationError, match=r"operation\[1\].*matches no eligible column"
    ):
        validate_corrupt_config(old_name_config, _sidecar())

    new_name_config = _config(
        _schema_drift("records__patient", rename_to={"prop__name": "prop__full_name"}),
        _null_cells("records__patient", ["prop__full_name"]),
    )
    validate_corrupt_config(new_name_config, _sidecar())  # does not raise


def test_evolved_schema_drop_hides_dropped_column() -> None:
    config = _config(
        _schema_drift("records__patient", drop=["prop__name"]),
        _null_cells("records__patient", ["prop__name"]),
    )
    with pytest.raises(
        CorruptValidationError, match=r"operation\[1\].*matches no eligible column"
    ):
        validate_corrupt_config(config, _sidecar())


def test_evolved_schema_retype_changes_jitter_eligibility() -> None:
    config = _config(
        _schema_drift("records__patient", retype_to={"prop__age": "VARCHAR"}),
        _duplicate_rows_jitter("records__patient", ["prop__age"]),
    )
    with pytest.raises(
        CorruptValidationError,
        match=r"operation\[1\].*matches no eligible column",
    ):
        validate_corrupt_config(config, _sidecar())


def test_evolved_schema_pattern_no_longer_matches_renamed_away_column() -> None:
    """A columns pattern loses its match when an earlier schema_drift renames
    the matching column away."""
    config = _config(
        _schema_drift("records__patient", rename_to={"prop__name": "prop__full_name"}),
        _null_cells_target({"table": "records__patient"}, ["prop__na*"]),
    )
    with pytest.raises(
        CorruptValidationError, match=r"operation\[1\].*matches no eligible column"
    ):
        validate_corrupt_config(config, _sidecar())


def test_evolved_schema_pattern_matches_column_renamed_into_range() -> None:
    """A columns pattern gains a match when an earlier schema_drift renames a
    column into its range."""
    config = _config(
        _schema_drift("records__patient", rename_to={"prop__age": "prop__full_name"}),
        _null_cells_target({"table": "records__patient"}, ["prop__full*"]),
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


# ---------------------------------------------------------------------------
# PlacementColumnExists
# ---------------------------------------------------------------------------


def _placement_sidecar_bigint_mismatch() -> Sidecar:
    table_a = table_spec(
        "records__sensor_a",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("ts", "BIGINT"),
            column_spec("prop__val", "VARCHAR"),
        ),
        record_kind="sensor_a",
    )
    table_b = table_spec(
        "records__sensor_b",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("record_id", "VARCHAR"),
            column_spec("ts", "VARCHAR"),
            column_spec("prop__val", "VARCHAR"),
        ),
        record_kind="sensor_b",
    )
    return sidecar((table_a, table_b))


def test_placement_column_exists_passes_when_in_one_of_several_tables() -> None:
    """prop__age exists only on records__patient, not records__doctor; the
    category selector resolves both, and the placement column still passes."""
    config = _config(
        _null_cells_placement(
            {"category": "records"},
            ["prop__name"],
            {"kind": "correlated", "column": "prop__age", "value": "10", "weight": 2.0},
        )
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_placement_column_exists_rejects_when_in_no_resolved_table() -> None:
    config = _config(
        _null_cells_placement(
            {"category": "records"},
            ["prop__name"],
            {
                "kind": "correlated",
                "column": "prop__nonexistent",
                "value": "x",
                "weight": 2.0,
            },
        )
    )
    with pytest.raises(
        CorruptValidationError,
        match=r"operation\[0\].*placement column 'prop__nonexistent'",
    ):
        validate_corrupt_config(config, _sidecar())


def test_placement_column_exists_clustered_temporal_bigint_mismatch_raises() -> None:
    """`ts` is BIGINT on records__sensor_a but VARCHAR on records__sensor_b;
    the category selector resolves both and the mismatch fails."""
    config = _config(
        _null_cells_placement(
            {"category": "records"},
            ["prop__val"],
            {"kind": "clustered_temporal", "column": "ts", "clusters": 1, "width": 10},
        )
    )
    with pytest.raises(CorruptValidationError, match=r"operation\[0\].*must be BIGINT"):
        validate_corrupt_config(config, _placement_sidecar_bigint_mismatch())


def test_placement_column_checked_against_evolved_schema() -> None:
    """A column dropped by an earlier schema_drift no longer satisfies
    PlacementColumnExists."""
    config = _config(
        _schema_drift("records__patient", drop=["prop__age"]),
        _null_cells_placement(
            {"table": "records__patient"},
            ["prop__name"],
            {"kind": "correlated", "column": "prop__age", "value": "10", "weight": 2.0},
        ),
    )
    with pytest.raises(
        CorruptValidationError, match=r"operation\[1\].*placement column 'prop__age'"
    ):
        validate_corrupt_config(config, _sidecar())


# ---------------------------------------------------------------------------
# EntityScopedRecordId
# ---------------------------------------------------------------------------


def test_entity_scoped_record_id_passes_on_records_and_membership_targets() -> None:
    config = _config(
        _null_cells_placement(
            {"table": "records__patient"},
            ["prop__name"],
            {"kind": "entity_scoped", "entities": {"count": 1}},
        )
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_entity_scoped_record_id_rejects_table_lacking_record_id() -> None:
    table_without_record_id = table_spec(
        "records__widget",
        "records",
        (
            column_spec("fork_path", "VARCHAR"),
            column_spec("prop__val", "VARCHAR"),
        ),
        record_kind="widget",
    )
    local_sidecar = sidecar((table_without_record_id,))
    config = _config(
        _null_cells_placement(
            {"table": "records__widget"},
            ["prop__val"],
            {"kind": "entity_scoped", "entities": {"count": 1}},
        )
    )
    with pytest.raises(
        CorruptValidationError,
        match=r"operation\[0\].*entity_scoped requires record_id",
    ):
        validate_corrupt_config(config, local_sidecar)


# ---------------------------------------------------------------------------
# HistoryOnlyTarget
# ---------------------------------------------------------------------------


_FAMILY_C_BUILDERS = {
    "drop_events": _drop_events,
    "freeze_series": _freeze_series,
    "shift_sim_time": _shift_sim_time,
}


@pytest.mark.parametrize("kind", ["drop_events", "freeze_series", "shift_sim_time"])
def test_history_only_target_rejects_records_table(kind: str) -> None:
    config = _config(_FAMILY_C_BUILDERS[kind]({"table": "records__patient"}))
    with pytest.raises(
        CorruptValidationError,
        match=rf"operation 0 \({kind}\).*history table only.*records__patient",
    ):
        validate_corrupt_config(config, _sidecar())


def test_history_only_target_rejects_glob_matching_history_plus_another_table() -> None:
    history_extra = table_spec(
        "history_extra", "fixed", (column_spec("fork_path", "VARCHAR"),)
    )
    local_sidecar = sidecar((_history(), history_extra))
    config = _config(_drop_events({"glob": "h*"}))
    with pytest.raises(
        CorruptValidationError,
        match=r"operation 0 \(drop_events\).*history table only",
    ):
        validate_corrupt_config(config, local_sidecar)


def test_history_only_target_passes_for_concrete_history_table() -> None:
    config = _config(_drop_events({"table": "history"}))
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_history_only_target_passes_for_glob_matching_only_history() -> None:
    config = _config(_drop_events({"glob": "hist*"}))
    validate_corrupt_config(config, _sidecar())  # does not raise


@pytest.mark.parametrize("kind", ["drop_events", "freeze_series", "shift_sim_time"])
def test_history_only_target_passes_for_family_c_on_history(kind: str) -> None:
    config = _config(_FAMILY_C_BUILDERS[kind]({"table": "history"}))
    validate_corrupt_config(config, _sidecar())  # does not raise


@pytest.mark.parametrize("kind", ["drop_events", "freeze_series", "shift_sim_time"])
def test_family_c_where_columns_exist_over_history_columns(kind: str) -> None:
    config = _config(
        _FAMILY_C_BUILDERS[kind]({"table": "history", "where": {"nope": "x"}})
    )
    with pytest.raises(
        CorruptValidationError, match=r"operation\[0\].*where names unknown column"
    ):
        validate_corrupt_config(config, _sidecar())


def test_drop_events_clustered_temporal_on_sim_time_passes() -> None:
    placement = {
        "kind": "clustered_temporal",
        "column": "sim_time",
        "clusters": 1,
        "width": 10,
    }
    config = _config(_drop_events_placement({"table": "history"}, placement))
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_drop_events_entity_scoped_legal_over_history() -> None:
    config = _config(
        _drop_events_placement(
            {"table": "history"}, {"kind": "entity_scoped", "entities": {"count": 1}}
        )
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_shift_sim_time_offset_passes_with_history_target() -> None:
    config = _config(
        _shift_sim_time(
            {"table": "history"},
            {
                "kind": "offset",
                "distribution": {"shape": "normal", "mean": 0.0, "stddev": 1.0},
            },
        )
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


# ---------------------------------------------------------------------------
# NonHistoryTarget
# ---------------------------------------------------------------------------


def test_non_history_target_rejects_direct_history_table() -> None:
    config = _config(_delete_rows({"table": "history"}))
    with pytest.raises(
        CorruptValidationError,
        match=r"operation\[0\].*delete_rows target resolves to fixed-category"
        r" table 'history'",
    ):
        validate_corrupt_config(config, _sidecar())


def test_non_history_target_rejects_category_fixed() -> None:
    config = _config(_delete_rows({"category": "fixed"}))
    with pytest.raises(
        CorruptValidationError,
        match=r"operation\[0\].*delete_rows target resolves to fixed-category",
    ):
        validate_corrupt_config(config, _sidecar())


def test_non_history_target_rejects_glob_that_includes_history() -> None:
    history_extra = table_spec(
        "history_extra", "fixed", (column_spec("fork_path", "VARCHAR"),)
    )
    local_sidecar = sidecar((_history(), history_extra))
    config = _config(_delete_rows({"glob": "h*"}))
    with pytest.raises(
        CorruptValidationError,
        match=r"operation\[0\].*delete_rows target resolves to fixed-category",
    ):
        validate_corrupt_config(config, local_sidecar)


def test_non_history_target_passes_for_records_only() -> None:
    config = _config(_delete_rows({"table": "records__patient"}))
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_non_history_target_passes_for_membership_only() -> None:
    config = _config(_delete_rows({"table": "membership__patient__ward"}))
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_non_history_target_passes_for_mixed_records_and_membership() -> None:
    config = _config(
        _delete_rows({"tables": ["records__patient", "membership__patient__ward"]})
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_delete_rows_selector_resolves_rejects_unknown_table() -> None:
    config = _config(_delete_rows({"table": "records__nope"}))
    with pytest.raises(
        CorruptValidationError, match=r"operation\[0\].*not in this emit"
    ):
        validate_corrupt_config(config, _sidecar())


def test_delete_rows_where_columns_exist_rejects_unknown_column() -> None:
    config = _config(
        _delete_rows({"table": "records__patient", "where": {"prop__nope": "x"}})
    )
    with pytest.raises(
        CorruptValidationError, match=r"operation\[0\].*where names unknown column"
    ):
        validate_corrupt_config(config, _sidecar())


def test_delete_rows_placement_column_exists_extends_unchanged() -> None:
    config = _config(
        _delete_rows_placement(
            {"table": "records__patient"},
            {"kind": "correlated", "column": "prop__age", "value": "10", "weight": 2.0},
        )
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_delete_rows_entity_scoped_placement_extends_unchanged() -> None:
    config = _config(
        _delete_rows_placement(
            {"table": "records__patient"},
            {"kind": "entity_scoped", "entities": {"count": 1}},
        )
    )
    validate_corrupt_config(config, _sidecar())  # does not raise


# ---------------------------------------------------------------------------
# RecordsCategoryTarget
# ---------------------------------------------------------------------------


def test_records_category_target_rejects_history_table() -> None:
    config = _config(_insert_rows("history"))
    with pytest.raises(
        CorruptValidationError,
        match=r"operation\[0\].*insert_rows target resolves to"
        r" non-records-category table 'history'",
    ):
        validate_corrupt_config(config, _sidecar())


def test_records_category_target_rejects_membership_table() -> None:
    config = _config(_insert_rows("membership__patient__ward"))
    with pytest.raises(
        CorruptValidationError,
        match=r"operation\[0\].*insert_rows target resolves to"
        r" non-records-category table 'membership__patient__ward'",
    ):
        validate_corrupt_config(config, _sidecar())


def test_records_category_target_passes_for_records_only() -> None:
    config = _config(_insert_rows("records__patient"))
    validate_corrupt_config(config, _sidecar())  # does not raise


# ---------------------------------------------------------------------------
# PhantomResampleColumns
# ---------------------------------------------------------------------------


def test_phantom_resample_columns_rejects_reference_prop_column() -> None:
    config = _config(_insert_rows("records__patient", ["prop__doctor_id"]))
    with pytest.raises(
        CorruptValidationError, match=r"matches no insert-eligible column"
    ):
        validate_corrupt_config(config, _sidecar())


def test_phantom_resample_columns_rejects_structural_column() -> None:
    config = _config(_insert_rows("records__patient", ["record_id"]))
    with pytest.raises(
        CorruptValidationError, match=r"matches no insert-eligible column"
    ):
        validate_corrupt_config(config, _sidecar())


def test_phantom_resample_columns_passes_when_entry_matches_in_one_of_several_tables() -> (
    None
):
    """prop__specialty exists only on records__doctor, not records__patient;
    the category selector resolves both, and the entry still passes."""
    config = _config(_insert_rows_target({"category": "records"}, ["prop__specialty"]))
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_phantom_resample_columns_accepts_presentation_id() -> None:
    config = _config(_insert_rows("records__patient", ["presentation_id"]))
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_phantom_resample_columns_absent_target_columns_skips_check() -> None:
    """No target.columns at all: PhantomResampleColumns is never invoked."""
    config = _config(_insert_rows("records__patient"))
    validate_corrupt_config(config, _sidecar())  # does not raise


# ---------------------------------------------------------------------------
# MembershipOnlyTarget
# ---------------------------------------------------------------------------


def test_membership_only_target_rejects_history_table() -> None:
    config = _config(_distort_intervals({"table": "history"}))
    with pytest.raises(
        CorruptValidationError,
        match=r"operation 0 \(distort_intervals\): target must resolve to"
        r" membership-category tables only; got 'history'",
    ):
        validate_corrupt_config(config, _sidecar())


def test_membership_only_target_rejects_records_category_table() -> None:
    config = _config(_distort_intervals({"table": "records__patient"}))
    with pytest.raises(
        CorruptValidationError,
        match=r"operation 0 \(distort_intervals\): target must resolve to"
        r" membership-category tables only; got 'records__patient'",
    ):
        validate_corrupt_config(config, _sidecar())


def test_membership_only_target_rejects_glob_resolving_to_membership_plus_records() -> (
    None
):
    """A glob matching both the membership table and a records table sharing
    its name prefix -- the mixed-category resolution rejects on the
    records-category member."""
    membership_shadow = table_spec(
        "membership_shadow", "records", (column_spec("fork_path", "VARCHAR"),)
    )
    local_sidecar = sidecar((_membership_ward(), membership_shadow))
    config = _config(_distort_intervals({"glob": "membership*"}))
    with pytest.raises(
        CorruptValidationError,
        match=r"operation 0 \(distort_intervals\): target must resolve to"
        r" membership-category tables only",
    ):
        validate_corrupt_config(config, local_sidecar)


def test_membership_only_target_passes_for_category_membership() -> None:
    config = _config(_distort_intervals({"category": "membership"}))
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_membership_only_target_passes_for_membership_glob() -> None:
    config = _config(_distort_intervals({"glob": "membership__*"}))
    validate_corrupt_config(config, _sidecar())  # does not raise


def test_membership_only_target_passes_for_exact_membership_table() -> None:
    config = _config(_distort_intervals({"table": "membership__patient__ward"}))
    validate_corrupt_config(config, _sidecar())  # does not raise


# ---------------------------------------------------------------------------
# A fully-valid multi-operation config
# ---------------------------------------------------------------------------


def test_fully_valid_multi_operation_config_returns_none() -> None:
    config = _config(
        _null_cells("records__patient", ["prop__name"]),
        _duplicate_rows_exact("membership__patient__ward"),
        _schema_drift("records__patient", rename_to={"prop__name": "prop__full_name"}),
        _dangle_reference("membership__patient__ward", ["member__consultant__id"]),
        _drop_events({"table": "history"}),
        _freeze_series({"table": "history"}),
        _shift_sim_time({"table": "history"}),
    )
    validate_corrupt_config(config, _sidecar())  # does not raise
