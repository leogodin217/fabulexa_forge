"""Tests for `corrupters.engine.corrupt_emit`."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from reader._fixtures_build import (
    build_all_fixtures,
    build_history_series,
    build_spanning,
)

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION, __version__
from fabulexa_forge.config.models import (
    Amount,
    ClusteredTemporal,
    Correlated,
    CorruptConfig,
    DangleReference,
    DeleteRows,
    DropEvents,
    DuplicateRows,
    EntityScoped,
    FreezeSeries,
    InsertRows,
    MispointReference,
    MutateCells,
    MutationCase,
    MutationSentinel,
    MutationTypo,
    NullCells,
    SchemaDrift,
    Target,
)
from fabulexa_forge.corrupters.engine import corrupt_emit
from fabulexa_forge.corrupters.fingerprint import fingerprint_config
from fabulexa_forge.errors import CorruptValidationError
from fabulexa_forge.reader import conformance
from fabulexa_forge.reader.emit import open_emit


def _four_operation_config(seed: int = 42) -> CorruptConfig:
    """One of each operation kind, over the spanning fixture's tables."""
    return CorruptConfig(
        seed=seed,
        operations=[
            NullCells(
                kind="null_cells",
                name="null_actor_name",
                target=Target(table="records__actor", columns=["prop__name"]),
                amount=Amount(rate=1.0),
            ),
            DuplicateRows(
                kind="duplicate_rows",
                target=Target(table="records__doctor"),
                amount=Amount(count=1),
            ),
            SchemaDrift(
                kind="schema_drift",
                target=Target(table="records__actor"),
                rename_to={"prop__status": "prop__account_status"},
            ),
            DangleReference(
                kind="dangle_reference",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__id"],
                ),
                amount=Amount(rate=1.0),
            ),
        ],
    )


def _spanning_emit(tmp_path: Path) -> Path:
    dest = tmp_path / "spanning"
    build_spanning(dest)
    return dest


def _history_series_emit(tmp_path: Path) -> Path:
    dest = tmp_path / "history_series"
    build_history_series(dest)
    return dest


def test_corrupt_emit_end_to_end(tmp_path: Path) -> None:
    emit_dir = _spanning_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = _four_operation_config()

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    assert [o.kind for o in report.outcomes] == [
        "null_cells",
        "duplicate_rows",
        "schema_drift",
        "dangle_reference",
    ]
    assert (out_dir / "run.duckdb").exists()
    assert (out_dir / "base.json").exists()
    assert (out_dir / "defects.json").exists()

    with open_emit(out_dir) as corrupted:
        result = conformance.validate(corrupted)
    structural = {"C1", "C2", "C3", "C4", "C5", "C8"}
    for check in result.results:
        if check.check in structural:
            assert check.passed, f"{check.check} failed: {check.messages}"

    failing = {r.check for r in result.results if not r.passed}
    manifest = json.loads((out_dir / "defects.json").read_text(encoding="utf-8"))
    impact_union = {
        code
        for defect in manifest["defects"]
        for code in defect["impact"]
        if code != "beyond-c1-c12"
    }
    assert failing <= impact_union  # soundness: containment holds


def test_determinism_byte_identical_base_and_manifest(tmp_path: Path) -> None:
    emit_dir = _spanning_emit(tmp_path)
    config = _four_operation_config()

    with open_emit(emit_dir) as emit:
        corrupt_emit(emit, config, tmp_path / "out1")
    with open_emit(emit_dir) as emit:
        corrupt_emit(emit, config, tmp_path / "out2")

    assert (tmp_path / "out1" / "base.json").read_bytes() == (
        tmp_path / "out2" / "base.json"
    ).read_bytes()
    assert (tmp_path / "out1" / "defects.json").read_bytes() == (
        tmp_path / "out2" / "defects.json"
    ).read_bytes()


def test_manifest_provenance(tmp_path: Path) -> None:
    emit_dir = _spanning_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = _four_operation_config()

    with open_emit(emit_dir) as emit:
        expected_sha256 = hashlib.sha256(
            (emit_dir / "base.json").read_bytes()
        ).hexdigest()
        corrupt_emit(emit, config, out_dir)

    manifest = json.loads((out_dir / "defects.json").read_text(encoding="utf-8"))
    assert manifest["source"]["sidecar_sha256"] == expected_sha256
    assert manifest["source"]["base_format_version"] == SUPPORTED_BASE_FORMAT_VERSION
    assert manifest["config_fingerprint"] == fingerprint_config(config)
    assert manifest["code_version"] == __version__


@pytest.mark.parametrize(
    "fixture_name",
    [
        "c4_wrong_history_type",
        "c5_prop_missing",
        "c7_half_null_member",
        "schema_mismatch",
        "c12_missing_kind",
        "c12_missing_subtype",
    ],
)
def test_refuses_non_conformant_source(tmp_path: Path, fixture_name: str) -> None:
    fixtures = build_all_fixtures(tmp_path / "fixtures")
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            NullCells(
                kind="null_cells",
                target=Target(table="history", columns=["value"]),
                amount=Amount(rate=1.0),
            )
        ],
    )

    with open_emit(fixtures[fixture_name]) as emit:
        with pytest.raises(CorruptValidationError):
            corrupt_emit(emit, config, out_dir)

    assert not out_dir.exists() or not any(out_dir.iterdir())


def test_refuses_populated_out_dir(tmp_path: Path) -> None:
    emit_dir = _spanning_emit(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "run.duckdb").write_bytes(b"")
    config = _four_operation_config()

    with open_emit(emit_dir) as emit:
        with pytest.raises(CorruptValidationError):
            corrupt_emit(emit, config, out_dir)


def test_refuses_out_dir_holding_only_base_json(tmp_path: Path) -> None:
    """The refuses-to-overwrite guard trips on a base.json alone -- the second
    arm of the `or` -- with no run.duckdb present."""
    emit_dir = _spanning_emit(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "base.json").write_text("{}", encoding="utf-8")
    config = _four_operation_config()

    with open_emit(emit_dir) as emit:
        with pytest.raises(CorruptValidationError, match="refuses to overwrite"):
            corrupt_emit(emit, config, out_dir)

    assert not (out_dir / "run.duckdb").exists()


def test_business_rule_failure_writes_nothing(tmp_path: Path) -> None:
    emit_dir = _spanning_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            NullCells(
                kind="null_cells",
                target=Target(table="records__actor", columns=["record_id"]),
                amount=Amount(rate=1.0),
            )
        ],
    )

    with open_emit(emit_dir) as emit:
        with pytest.raises(CorruptValidationError):
            corrupt_emit(emit, config, out_dir)

    assert not out_dir.exists() or not any(out_dir.iterdir())


def test_rule_labels_explicit_and_fallback(tmp_path: Path) -> None:
    emit_dir = _spanning_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            NullCells(
                kind="null_cells",
                name="my_custom_rule",
                target=Target(table="records__actor", columns=["prop__name"]),
                amount=Amount(rate=1.0),
            ),
            DuplicateRows(
                kind="duplicate_rows",
                target=Target(table="records__doctor"),
                amount=Amount(count=1),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        corrupt_emit(emit, config, out_dir)

    manifest = json.loads((out_dir / "defects.json").read_text(encoding="utf-8"))
    rules = {d["rule"] for d in manifest["defects"]}
    assert "my_custom_rule" in rules
    assert "duplicate_rows#1" in rules


def test_units_selected_and_affected(tmp_path: Path) -> None:
    emit_dir = _spanning_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            DuplicateRows(
                kind="duplicate_rows",
                target=Target(table="records__doctor"),
                amount=Amount(count=1),
            )
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    (outcome,) = report.outcomes
    assert outcome.units_selected == 1
    assert outcome.units_affected == 1
    assert len(outcome.defects) == 1


def _class_targeted_config(seed: int = 1) -> CorruptConfig:
    """A `category: records` null_cells, pooled over records__actor + records__doctor."""
    return CorruptConfig(
        seed=seed,
        operations=[
            NullCells(
                kind="null_cells",
                target=Target(category="records", columns=["prop__name"]),
                amount=Amount(rate=1.0),
            )
        ],
    )


def test_class_targeted_config_corrupts_every_resolved_table(tmp_path: Path) -> None:
    emit_dir = _spanning_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = _class_targeted_config()

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    (outcome,) = report.outcomes
    assert outcome.tables == ("records__actor", "records__doctor")
    defect_tables = {d.location.table for d in outcome.defects}
    assert defect_tables == {"records__actor", "records__doctor"}
    assert {d.rule for d in outcome.defects} == {"null_cells#0"}


def test_class_targeted_config_determinism_byte_identical(tmp_path: Path) -> None:
    emit_dir = _spanning_emit(tmp_path)
    config = _class_targeted_config()

    with open_emit(emit_dir) as emit:
        corrupt_emit(emit, config, tmp_path / "out1")
    with open_emit(emit_dir) as emit:
        corrupt_emit(emit, config, tmp_path / "out2")

    assert (tmp_path / "out1" / "defects.json").read_bytes() == (
        tmp_path / "out2" / "defects.json"
    ).read_bytes()


def _mixed_placement_config(seed: int = 5) -> CorruptConfig:
    """One operation of each placement kind, over the spanning fixture."""
    return CorruptConfig(
        seed=seed,
        operations=[
            NullCells(
                kind="null_cells",
                target=Target(table="records__actor", columns=["prop__name"]),
                amount=Amount(rate=1.0),
                placement=Correlated(
                    kind="correlated", column="prop__status", value="active", weight=5.0
                ),
            ),
            DuplicateRows(
                kind="duplicate_rows",
                target=Target(table="records__doctor"),
                amount=Amount(count=1),
                placement=EntityScoped(kind="entity_scoped", entities=Amount(count=1)),
            ),
            DangleReference(
                kind="dangle_reference",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__id"],
                ),
                amount=Amount(rate=1.0),
                placement=ClusteredTemporal(
                    kind="clustered_temporal",
                    column="joined_sim_time",
                    clusters=1,
                    width=5,
                ),
            ),
        ],
    )


def test_mixed_placement_kinds_determinism_byte_identical(tmp_path: Path) -> None:
    emit_dir = _spanning_emit(tmp_path)
    config = _mixed_placement_config()

    with open_emit(emit_dir) as emit:
        corrupt_emit(emit, config, tmp_path / "out1")
    with open_emit(emit_dir) as emit:
        corrupt_emit(emit, config, tmp_path / "out2")

    assert (tmp_path / "out1" / "defects.json").read_bytes() == (
        tmp_path / "out2" / "defects.json"
    ).read_bytes()


def test_break_locality_untouched_table_unchanged(tmp_path: Path) -> None:
    """Corrupting records__actor leaves records__doctor's content identical."""
    emit_dir = _spanning_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            NullCells(
                kind="null_cells",
                target=Target(table="records__actor", columns=["prop__name"]),
                amount=Amount(rate=1.0),
            )
        ],
    )

    import duckdb

    with open_emit(emit_dir) as emit:
        before = emit.query("SELECT * FROM records__doctor", ())
        corrupt_emit(emit, config, out_dir)

    conn = duckdb.connect(str(out_dir / "run.duckdb"), read_only=True)
    try:
        after = conn.execute("SELECT * FROM records__doctor").fetchall()
    finally:
        conn.close()
    assert sorted(before) == sorted(after)


# ---------------------------------------------------------------------------
# drop_events: shrinking-table sidecar `rows` regeneration
# ---------------------------------------------------------------------------


def test_drop_events_shrinks_history_sidecar_rows(tmp_path: Path) -> None:
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            DropEvents(
                kind="drop_events",
                target=Target(table="history"),
                amount=Amount(count=2),
            )
        ],
    )

    with open_emit(emit_dir) as emit:
        source_history_rows = next(
            t.rows for t in emit.sidecar.tables() if t.name == "history"
        )
        report = corrupt_emit(emit, config, out_dir)

    (outcome,) = report.outcomes
    assert outcome.kind == "drop_events"
    assert outcome.units_selected == 2
    assert outcome.units_affected == 2

    sidecar_raw = json.loads((out_dir / "base.json").read_text(encoding="utf-8"))
    history_table = next(t for t in sidecar_raw["tables"] if t["name"] == "history")
    assert history_table["rows"] == source_history_rows - 2

    manifest = json.loads((out_dir / "defects.json").read_text(encoding="utf-8"))
    classes = {d["class"] for d in manifest["defects"]}
    assert classes == {"dropped_event"}

    with open_emit(out_dir) as corrupted:
        result = conformance.validate(corrupted)
    structural = {"C1", "C2", "C3", "C4", "C5", "C8"}
    for check in result.results:
        if check.check in structural:
            assert check.passed, f"{check.check} failed: {check.messages}"


# ---------------------------------------------------------------------------
# mutate_cells: cross-operation composition
# ---------------------------------------------------------------------------


def test_schema_drift_rename_then_mutate_cells_targets_new_name(
    tmp_path: Path,
) -> None:
    """A later mutate_cells addresses a schema_drift-renamed column by its
    new name only -- the operation-order catalog evolution."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            SchemaDrift(
                kind="schema_drift",
                name="rename_status",
                target=Target(table="records__actor"),
                rename_to={"prop__status": "prop__account_status"},
            ),
            MutateCells(
                kind="mutate_cells",
                name="case_drift_status",
                target=Target(table="records__actor", columns=["prop__account_status"]),
                amount=Amount(rate=1.0),
                mutation=MutationCase(kind="case", form="upper"),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    assert [o.kind for o in report.outcomes] == ["schema_drift", "mutate_cells"]
    mutate_outcome = report.outcomes[1]
    assert mutate_outcome.units_affected == 1
    manifest = json.loads((out_dir / "defects.json").read_text(encoding="utf-8"))
    (defect,) = [d for d in manifest["defects"] if d["rule"] == "case_drift_status"]
    assert defect["location"]["column"] == "prop__account_status"


def test_schema_drift_rename_then_mutate_cells_old_name_rejected(
    tmp_path: Path,
) -> None:
    """A mutate_cells that still names the pre-rename column fails validation
    -- the old name no longer exists in the schema as of that position."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            SchemaDrift(
                kind="schema_drift",
                name="rename_status",
                target=Target(table="records__actor"),
                rename_to={"prop__status": "prop__account_status"},
            ),
            MutateCells(
                kind="mutate_cells",
                target=Target(table="records__actor", columns=["prop__status"]),
                amount=Amount(rate=1.0),
                mutation=MutationCase(kind="case", form="upper"),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        with pytest.raises(CorruptValidationError):
            corrupt_emit(emit, config, out_dir)

    assert not out_dir.exists() or not any(out_dir.iterdir())


def test_schema_drift_retype_moves_mutate_cells_type_gate_rejects_old_type(
    tmp_path: Path,
) -> None:
    """schema_drift retypes prop__wait_minutes BIGINT->DOUBLE; a following
    `typo` mutation (BIGINT/VARCHAR-gated) is rejected because the retype has
    already moved the column out of that gate, as of that position."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            SchemaDrift(
                kind="schema_drift",
                name="retype_wait_minutes",
                target=Target(table="records__actor"),
                retype_to={"prop__wait_minutes": "DOUBLE"},
            ),
            MutateCells(
                kind="mutate_cells",
                target=Target(table="records__actor", columns=["prop__wait_minutes"]),
                amount=Amount(rate=1.0),
                mutation=MutationTypo(kind="typo"),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        with pytest.raises(CorruptValidationError):
            corrupt_emit(emit, config, out_dir)

    assert not out_dir.exists() or not any(out_dir.iterdir())


def test_null_cells_then_mutate_cells_no_mutation(tmp_path: Path) -> None:
    """A cell nulled by null_cells is skipped by a following mutate_cells --
    NULL-invariance: the unit is selected but never mutated."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            NullCells(
                kind="null_cells",
                name="null_actor_name",
                target=Target(table="records__actor", columns=["prop__name"]),
                amount=Amount(rate=1.0),
            ),
            MutateCells(
                kind="mutate_cells",
                name="case_drift_name",
                target=Target(table="records__actor", columns=["prop__name"]),
                amount=Amount(rate=1.0),
                mutation=MutationCase(kind="case", form="upper"),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    null_outcome, mutate_outcome = report.outcomes
    assert null_outcome.units_affected == 1
    assert mutate_outcome.units_selected == 1
    assert mutate_outcome.units_affected == 0
    assert mutate_outcome.defects == ()


def test_mutate_cells_then_duplicate_rows_copies_carry_mutated_values(
    tmp_path: Path,
) -> None:
    """duplicate_rows copies a row after mutate_cells has rewritten it -- the
    copy carries the mutated value, not the original."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            MutateCells(
                kind="mutate_cells",
                target=Target(table="records__doctor", columns=["prop__name"]),
                amount=Amount(rate=1.0),
                mutation=MutationSentinel(kind="sentinel", value="N/A"),
            ),
            DuplicateRows(
                kind="duplicate_rows",
                target=Target(table="records__doctor"),
                amount=Amount(count=1),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        corrupt_emit(emit, config, out_dir)

    import duckdb

    conn = duckdb.connect(str(out_dir / "run.duckdb"), read_only=True)
    try:
        rows = conn.execute('SELECT "prop__name" FROM records__doctor').fetchall()
    finally:
        conn.close()
    assert len(rows) == 4
    assert {row[0] for row in rows} == {"N/A"}


def test_mutate_cells_zero_unit_population_is_a_no_op(tmp_path: Path) -> None:
    """An empty cell population (a `where` filter matching no rows) is a
    no-op: units selected and affected are both 0, and no error is raised."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            MutateCells(
                kind="mutate_cells",
                target=Target(
                    table="history",
                    where={"property": "does_not_exist"},
                    columns=["value"],
                ),
                amount=Amount(count=1),
                mutation=MutationCase(kind="case", form="upper"),
            )
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    (outcome,) = report.outcomes
    assert outcome.units_selected == 0
    assert outcome.units_affected == 0
    assert outcome.defects == ()


# ---------------------------------------------------------------------------
# mispoint_reference: cross-operation composition
# ---------------------------------------------------------------------------


def test_dangle_reference_then_mispoint_reference_heals_c10(tmp_path: Path) -> None:
    """A following mispoint_reference on the same cell overwrites the dangling
    id with a real donor -- C10 resolves again, so the dangle's own C10
    declaration becomes a sound over-declaration in the final emit."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            DangleReference(
                kind="dangle_reference",
                name="dangle_appointment_doctor",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__id"],
                ),
                amount=Amount(rate=1.0),
            ),
            MispointReference(
                kind="mispoint_reference",
                name="heal_appointment_doctor",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__id"],
                ),
                amount=Amount(rate=1.0),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    assert [o.kind for o in report.outcomes] == [
        "dangle_reference",
        "mispoint_reference",
    ]
    manifest = json.loads((out_dir / "defects.json").read_text(encoding="utf-8"))
    assert {d["class"] for d in manifest["defects"]} == {
        "dangling_reference",
        "mispointed_reference",
    }

    with open_emit(out_dir) as corrupted:
        final_id = corrupted.query_arrow(
            "SELECT member__doctor__id FROM membership__actor__appointments", ()
        ).to_pylist()[0]["member__doctor__id"]
        result = conformance.validate(corrupted)
    assert final_id in {"d001", "d002", "d003"}
    c10 = next(check for check in result.results if check.check == "C10")
    assert c10.passed


def test_null_cells_then_mispoint_reference_filter1_excludes_nulled_id(
    tmp_path: Path,
) -> None:
    """Population filter 1 (the id itself NULL) excludes a cell null_cells has
    already nulled -- the following mispoint_reference is a clean no-op."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            NullCells(
                kind="null_cells",
                name="null_member_id",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__id"],
                ),
                amount=Amount(rate=1.0),
            ),
            MispointReference(
                kind="mispoint_reference",
                name="mispoint_member_id",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__id"],
                ),
                amount=Amount(rate=1.0),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    null_outcome, mispoint_outcome = report.outcomes
    assert null_outcome.units_affected == 1
    assert mispoint_outcome.units_selected == 0
    assert mispoint_outcome.units_affected == 0
    assert mispoint_outcome.defects == ()


def test_null_cells_then_mispoint_reference_filter2_excludes_nulled_partner_kind(
    tmp_path: Path,
) -> None:
    """Population filter 2 (a membership id's partner __kind NULL) excludes a
    cell whose kind half null_cells has already nulled, even though the id
    half is still populated."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            NullCells(
                kind="null_cells",
                name="null_member_kind",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__kind"],
                ),
                amount=Amount(rate=1.0),
            ),
            MispointReference(
                kind="mispoint_reference",
                name="mispoint_member_id",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__id"],
                ),
                amount=Amount(rate=1.0),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    null_outcome, mispoint_outcome = report.outcomes
    assert null_outcome.units_affected == 1
    assert mispoint_outcome.units_selected == 0
    assert mispoint_outcome.units_affected == 0
    assert mispoint_outcome.defects == ()


def test_duplicate_rows_then_mispoint_reference_donor_universe_unchanged(
    tmp_path: Path,
) -> None:
    """duplicate_rows on the donor target table leaves the distinct-id donor
    universe and creation times unchanged -- the constrained donor pool for
    the fixture's sole qualifying donor (d003) is identical with or without
    the physical duplicate row."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            DuplicateRows(
                kind="duplicate_rows",
                name="duplicate_d001",
                target=Target(table="records__doctor", where={"record_id": "d001"}),
                amount=Amount(count=1),
            ),
            MispointReference(
                kind="mispoint_reference",
                name="late_doctor_on_appointment",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__id"],
                ),
                amount=Amount(count=1),
                constraint="created_after_reference",
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    duplicate_outcome, mispoint_outcome = report.outcomes
    assert duplicate_outcome.units_affected == 1
    assert mispoint_outcome.units_affected == 1

    with open_emit(out_dir) as corrupted:
        doctor_rows = corrupted.query_arrow(
            "SELECT record_id FROM records__doctor", ()
        ).to_pylist()
        final_id = corrupted.query_arrow(
            "SELECT member__doctor__id FROM membership__actor__appointments", ()
        ).to_pylist()[0]["member__doctor__id"]
    assert [row["record_id"] for row in doctor_rows].count("d001") == 2
    # d003 is the sole donor created strictly after the write anchor (10),
    # whether or not d001 carries a duplicate physical row.
    assert final_id == "d003"


def test_mispoint_reference_then_mispoint_reference_excludes_prior_donor(
    tmp_path: Path,
) -> None:
    """Two mispoint_reference operations on the same cell share the working
    set: the second reads the first's donor as the cell's "current" id and
    excludes it from its own donor pool -- the final value never repeats the
    first rewrite."""
    emit_dir = _history_series_emit(tmp_path)
    op = MispointReference(
        kind="mispoint_reference",
        target=Target(
            table="membership__actor__appointments", columns=["member__doctor__id"]
        ),
        amount=Amount(count=1),
    )

    solo_config = CorruptConfig(seed=7, operations=[op])
    with open_emit(emit_dir) as emit:
        corrupt_emit(emit, solo_config, tmp_path / "solo")
    with open_emit(tmp_path / "solo") as solo_out:
        first_donor = solo_out.query_arrow(
            "SELECT member__doctor__id FROM membership__actor__appointments", ()
        ).to_pylist()[0]["member__doctor__id"]

    twice_config = CorruptConfig(seed=7, operations=[op, op])
    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, twice_config, tmp_path / "twice")
    with open_emit(tmp_path / "twice") as twice_out:
        final_id = twice_out.query_arrow(
            "SELECT member__doctor__id FROM membership__actor__appointments", ()
        ).to_pylist()[0]["member__doctor__id"]

    assert [o.kind for o in report.outcomes] == [
        "mispoint_reference",
        "mispoint_reference",
    ]
    assert final_id != first_donor


# ---------------------------------------------------------------------------
# delete_rows: the total-erasure guard, no-op partial erasure, tombstone isolation
# ---------------------------------------------------------------------------


def _total_erasure_config(seed: int = 1) -> CorruptConfig:
    """delete_rows on every records/membership table plus drop_events on
    history -- empties every table in the spanning fixture."""
    return CorruptConfig(
        seed=seed,
        operations=[
            DeleteRows(
                kind="delete_rows",
                target=Target(table="records__actor"),
                amount=Amount(rate=1.0),
            ),
            DeleteRows(
                kind="delete_rows",
                target=Target(table="records__doctor"),
                amount=Amount(rate=1.0),
            ),
            DeleteRows(
                kind="delete_rows",
                target=Target(table="membership__actor__appointments"),
                amount=Amount(rate=1.0),
            ),
            DropEvents(
                kind="drop_events",
                target=Target(table="history"),
                amount=Amount(rate=1.0),
            ),
        ],
    )


def test_total_erasure_guard_raises_and_writes_nothing(tmp_path: Path) -> None:
    emit_dir = _spanning_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = _total_erasure_config()

    with open_emit(emit_dir) as emit:
        with pytest.raises(CorruptValidationError, match="erased every row"):
            corrupt_emit(emit, config, out_dir)

    assert not out_dir.exists() or not any(out_dir.iterdir())


def test_emptying_one_table_is_a_noop_for_a_later_operation(tmp_path: Path) -> None:
    emit_dir = _spanning_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            DeleteRows(
                kind="delete_rows",
                name="empty_doctor",
                target=Target(table="records__doctor"),
                amount=Amount(rate=1.0),
            ),
            DeleteRows(
                kind="delete_rows",
                name="doctor_noop",
                target=Target(table="records__doctor"),
                amount=Amount(count=1),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    first_outcome, second_outcome = report.outcomes
    # 3 doctor rows in the spanning fixture (d001, 1005, 9f2ab1) -- rate:1.0
    # empties the table entirely.
    assert first_outcome.units_selected == 3
    assert second_outcome.units_selected == 0
    assert second_outcome.units_affected == 0
    assert second_outcome.defects == ()
    assert (out_dir / "run.duckdb").exists()


def test_deleted_record_ids_never_reaches_output(tmp_path: Path) -> None:
    """The tombstone set is written by delete_rows and starts empty on every
    fresh CorruptState, but is never a field of any output artifact."""
    from fabulexa_forge.corrupters.state import CorruptState

    assert CorruptState(tables={}).deleted_record_ids == {}

    emit_dir = _spanning_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            DeleteRows(
                kind="delete_rows",
                target=Target(table="records__doctor"),
                amount=Amount(rate=1.0),
            )
        ],
    )

    with open_emit(emit_dir) as emit:
        corrupt_emit(emit, config, out_dir)

    base_text = (out_dir / "base.json").read_text(encoding="utf-8")
    defects_text = (out_dir / "defects.json").read_text(encoding="utf-8")
    assert "deleted_record_ids" not in base_text
    assert "deleted_record_ids" not in defects_text


# ---------------------------------------------------------------------------
# Family-B composition -- design doc § Operations in order, new rows
# ---------------------------------------------------------------------------


def test_delete_rows_then_sampling_excludes_removed_rows(tmp_path: Path) -> None:
    """A later operation's population, over the same table, excludes the
    rows an earlier delete_rows removed."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            DeleteRows(
                kind="delete_rows",
                name="delete_one_doctor",
                target=Target(table="records__doctor", where={"record_id": "d002"}),
                amount=Amount(count=1),
            ),
            NullCells(
                kind="null_cells",
                name="null_remaining_doctors",
                target=Target(table="records__doctor", columns=["prop__name"]),
                amount=Amount(rate=1.0),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    delete_outcome, null_outcome = report.outcomes
    assert delete_outcome.units_affected == 1
    # 3 doctor rows minus the one deleted -- the null population excludes it.
    assert null_outcome.units_selected == 2
    assert null_outcome.units_affected == 2


def test_duplicate_then_delete_all_copies_of_pinned_id_declares_c9_each(
    tmp_path: Path,
) -> None:
    """duplicate_rows raises the pinned actor's row count to two; deleting
    both copies (with a bystander phantom keeping the table non-empty)
    declares C9 on each of the two deletions."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            InsertRows(
                kind="insert_rows",
                name="bystander_actor",
                target=Target(table="records__actor"),
                amount=Amount(count=1),
            ),
            DuplicateRows(
                kind="duplicate_rows",
                name="duplicate_pinned_actor",
                target=Target(table="records__actor", where={"record_id": "a001"}),
                amount=Amount(count=1),
            ),
            DeleteRows(
                kind="delete_rows",
                name="delete_all_pinned_copies",
                target=Target(table="records__actor", where={"record_id": "a001"}),
                amount=Amount(rate=1.0),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        corrupt_emit(emit, config, out_dir)

    manifest = json.loads((out_dir / "defects.json").read_text(encoding="utf-8"))
    delete_defects = [
        d for d in manifest["defects"] if d["rule"] == "delete_all_pinned_copies"
    ]
    assert len(delete_defects) == 2
    assert all(set(d["impact"]) == {"C6", "C9"} for d in delete_defects)


def test_duplicate_then_delete_some_copies_of_pinned_id_declares_nothing(
    tmp_path: Path,
) -> None:
    """duplicate_rows raises the pinned actor's row count to two; deleting
    only one copy restores single-copy survival -- the deletion declares
    beyond-c1-c12 (a sound over-declaration is left on the duplicate)."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            DuplicateRows(
                kind="duplicate_rows",
                name="duplicate_pinned_actor",
                target=Target(table="records__actor"),
                amount=Amount(count=1),
            ),
            DeleteRows(
                kind="delete_rows",
                name="delete_one_of_two_copies",
                target=Target(table="records__actor", where={"record_id": "a001"}),
                amount=Amount(count=1),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    _duplicate_outcome, delete_outcome = report.outcomes
    (defect,) = delete_outcome.defects
    assert defect.impact == ("beyond-c1-c12",)

    with open_emit(out_dir) as corrupted:
        actor_ids = corrupted.query_arrow(
            "SELECT record_id FROM records__actor", ()
        ).to_pylist()
    assert [row["record_id"] for row in actor_ids].count("a001") == 1


def test_delete_rows_removes_row_earlier_mutate_cells_declared_c6_against(
    tmp_path: Path,
) -> None:
    """A row an earlier mutate_cells already declared C6 against (a records-
    surface divergence) is then removed by delete_rows -- the series is now
    unresolved via the missing row rather than the value; both operations'
    C6 declarations are joint and sound."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            MutateCells(
                kind="mutate_cells",
                name="drift_actor_name",
                target=Target(table="records__actor", columns=["prop__name"]),
                amount=Amount(rate=1.0),
                mutation=MutationCase(kind="case", form="upper"),
            ),
            DeleteRows(
                kind="delete_rows",
                name="delete_actor",
                target=Target(table="records__actor", where={"record_id": "a001"}),
                amount=Amount(count=1),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    mutate_outcome, delete_outcome = report.outcomes
    (mutate_defect,) = mutate_outcome.defects
    (delete_defect,) = delete_outcome.defects
    assert mutate_defect.impact == ("C6",)
    assert delete_defect.impact == ("C6",)


def test_delete_rows_removes_membership_row_earlier_dangle_reference_dangled(
    tmp_path: Path,
) -> None:
    """A membership row an earlier dangle_reference dangled (declaring C10)
    is then removed entirely by delete_rows -- the delete's own removal
    always declares beyond-c1-c12 for a membership row, and the earlier C10
    stands as a sound over-declaration once the row is gone."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            DangleReference(
                kind="dangle_reference",
                name="dangle_appointment_doctor",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__id"],
                ),
                amount=Amount(rate=1.0),
            ),
            DeleteRows(
                kind="delete_rows",
                name="delete_dangled_membership_row",
                target=Target(table="membership__actor__appointments"),
                amount=Amount(rate=1.0),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    dangle_outcome, delete_outcome = report.outcomes
    (dangle_defect,) = dangle_outcome.defects
    (delete_defect,) = delete_outcome.defects
    assert dangle_defect.impact == ("C10",)
    assert delete_defect.impact == ("beyond-c1-c12",)

    with open_emit(out_dir) as corrupted:
        result = conformance.validate(corrupted)
    c10 = next(check for check in result.results if check.check == "C10")
    assert c10.passed  # the dangle's C10 is healed -- the row itself is gone


def test_insert_rows_then_delete_rows_phantom_ordinarily_deletable(
    tmp_path: Path,
) -> None:
    """A phantom is an ordinary deletable row -- deleting it declares
    beyond-c1-c12 (no pin, no series, no inbound reference)."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            InsertRows(
                kind="insert_rows",
                name="ghost_doctor",
                target=Target(table="records__doctor"),
                amount=Amount(count=1),
            ),
            DeleteRows(
                kind="delete_rows",
                name="delete_every_doctor",
                target=Target(table="records__doctor"),
                amount=Amount(rate=1.0),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    insert_outcome, delete_outcome = report.outcomes
    (phantom_defect,) = insert_outcome.defects
    phantom_id = dict(phantom_defect.location.row.keys)["record_id"]

    phantom_delete_defects = [
        d
        for d in delete_outcome.defects
        if dict(d.location.row.keys)["record_id"] == phantom_id
    ]
    (phantom_delete_defect,) = phantom_delete_defects
    assert phantom_delete_defect.impact == ("beyond-c1-c12",)


def test_insert_rows_after_delete_rows_same_kind_no_resurrection(
    tmp_path: Path,
) -> None:
    """The id universe insert_rows computes at its start includes every
    tombstoned id an earlier delete_rows removed from the same kind -- even
    one (d002) that left no other trace (no history, no reference, no pin)
    -- so no phantom ever resurrects it."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            DeleteRows(
                kind="delete_rows",
                name="delete_traceless_doctor",
                target=Target(table="records__doctor", where={"record_id": "d002"}),
                amount=Amount(count=1),
            ),
            InsertRows(
                kind="insert_rows",
                name="ghost_doctors",
                target=Target(table="records__doctor"),
                amount=Amount(count=2),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    _delete_outcome, insert_outcome = report.outcomes
    phantom_ids = {
        dict(d.location.row.keys)["record_id"] for d in insert_outcome.defects
    }
    assert "d002" not in phantom_ids


def test_schema_drift_then_insert_rows_resample_eligibility_evolved_schema(
    tmp_path: Path,
) -> None:
    """insert_rows' resample-eligible columns, and the phantom's cloned
    columns, are evaluated against the schema as of its position -- after an
    earlier schema_drift renames the resample target."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            SchemaDrift(
                kind="schema_drift",
                name="rename_doctor_name",
                target=Target(table="records__doctor"),
                rename_to={"prop__name": "prop__full_name"},
            ),
            InsertRows(
                kind="insert_rows",
                name="ghost_doctor",
                target=Target(table="records__doctor", columns=["prop__full_name"]),
                amount=Amount(count=1),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    _drift_outcome, insert_outcome = report.outcomes
    assert insert_outcome.units_affected == 1

    with open_emit(out_dir) as corrupted:
        rows = corrupted.query_arrow(
            "SELECT record_id, prop__full_name FROM records__doctor", ()
        ).to_pylist()
    assert "prop__name" not in {k for row in rows for k in row}
    assert all(row["prop__full_name"] is not None for row in rows)


def test_family_c_after_delete_rows_orphaned_series_declares_joint_c6(
    tmp_path: Path,
) -> None:
    """family C still selects a history series whose owning records row an
    earlier delete_rows removed; the C6-mirror oracle fails the round-trip
    (no records row), so the participating family-C operation declares C6
    beside the delete's own C6 declaration."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            InsertRows(
                kind="insert_rows",
                name="bystander_actor",
                target=Target(table="records__actor"),
                amount=Amount(count=1),
            ),
            DeleteRows(
                kind="delete_rows",
                name="delete_actor",
                target=Target(table="records__actor", where={"record_id": "a001"}),
                amount=Amount(count=1),
            ),
            FreezeSeries(
                kind="freeze_series",
                name="freeze_orphaned_status_series",
                target=Target(table="history", where={"property": "status"}),
                amount=Amount(rate=1.0),
                cut="after_first",
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        report = corrupt_emit(emit, config, out_dir)

    _bystander_outcome, delete_outcome, freeze_outcome = report.outcomes
    (delete_defect,) = delete_outcome.defects
    (freeze_defect,) = freeze_outcome.defects
    assert delete_defect.impact == ("C6", "C9")
    assert freeze_defect.impact == ("C6",)


def test_duplicate_rows_mutation_after_mutate_cells_clones_then_transforms(
    tmp_path: Path,
) -> None:
    """duplicate_rows `mutation` copies clone the working (already-mutated)
    value of a cell an earlier mutate_cells rewrote, then applies its own
    transform on top -- composition, not conflict."""
    emit_dir = _history_series_emit(tmp_path)
    out_dir = tmp_path / "out"
    config = CorruptConfig(
        seed=1,
        operations=[
            MutateCells(
                kind="mutate_cells",
                name="sentinel_doctor_name",
                target=Target(
                    table="records__doctor",
                    where={"record_id": "d001"},
                    columns=["prop__name"],
                ),
                amount=Amount(rate=1.0),
                mutation=MutationSentinel(kind="sentinel", value="N/A"),
            ),
            DuplicateRows(
                kind="duplicate_rows",
                name="conflicting_duplicate_name",
                target=Target(
                    table="records__doctor",
                    where={"record_id": "d001"},
                    columns=["prop__name"],
                ),
                amount=Amount(count=1),
                mutation=MutationCase(kind="case", form="lower"),
            ),
        ],
    )

    with open_emit(emit_dir) as emit:
        corrupt_emit(emit, config, out_dir)

    with open_emit(out_dir) as corrupted:
        rows = corrupted.query_arrow(
            "SELECT prop__name FROM records__doctor WHERE record_id = 'd001'", ()
        ).to_pylist()
    values = sorted(row["prop__name"] for row in rows)
    assert values == ["N/A", "n/a"]
