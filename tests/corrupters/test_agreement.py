"""Manifest / validate agreement invariant -- containment (universal).

For a matrix of configs over the spanning fixture -- including a deliberate C7
heal (null both halves of a membership pair across two operations) -- validate's
failing-check set is contained in the manifest's impact union. Set *equality* is
asserted only on the curated corrupt recipes
(tests/recipes/test_corrupt_recipes.py); containment holds universally, per
docs/architecture/pending/corrupter-engine-and-manifest.md § Manifest / validate
agreement invariant.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from reader._fixtures_build import (
    build_history_series,
    build_membership_intervals,
    build_spanning,
)
from recipes._harness import failing_checks_in_manifest_vocabulary

from fabulexa_forge.config.models import (
    Amount,
    ClusteredTemporal,
    CorruptConfig,
    DangleReference,
    DeleteRows,
    DistortIntervals,
    Distribution,
    DropEvents,
    DuplicateRows,
    FreezeSeries,
    InsertRows,
    MispointReference,
    MutateCells,
    MutationCase,
    MutationOutOfDomain,
    MutationSentinel,
    MutationTypo,
    NullCells,
    SchemaDrift,
    ShiftOffset,
    ShiftSimTime,
    Target,
)
from fabulexa_forge.corrupters.engine import corrupt_emit
from fabulexa_forge.reader import conformance
from fabulexa_forge.reader.emit import open_emit

if TYPE_CHECKING:
    from collections.abc import Callable


def _config_null_name() -> CorruptConfig:
    """null_cells on records__actor.prop__name (history-tracked, VARCHAR) -> C6."""
    return CorruptConfig(
        seed=1,
        operations=[
            NullCells(
                kind="null_cells",
                target=Target(table="records__actor", columns=["prop__name"]),
                amount=Amount(rate=1.0),
            )
        ],
    )


def _config_duplicate_pinned() -> CorruptConfig:
    """duplicate_rows on records__actor, whose sole row is pinned -> C9."""
    return CorruptConfig(
        seed=2,
        operations=[
            DuplicateRows(
                kind="duplicate_rows",
                target=Target(table="records__actor"),
                amount=Amount(count=1),
            )
        ],
    )


def _config_schema_drift_rename() -> CorruptConfig:
    """schema_drift rename of the sole ticked column -> C11."""
    return CorruptConfig(
        seed=3,
        operations=[
            SchemaDrift(
                kind="schema_drift",
                target=Target(table="records__actor"),
                rename_to={"prop__name": "prop__full_name"},
            )
        ],
    )


def _config_dangle_membership() -> CorruptConfig:
    """dangle_reference on the membership reference -> C10."""
    return CorruptConfig(
        seed=4,
        operations=[
            DangleReference(
                kind="dangle_reference",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__id"],
                ),
                amount=Amount(rate=1.0),
            )
        ],
    )


def _config_four_operations() -> CorruptConfig:
    """One of each operation kind, over the spanning fixture's tables."""
    return CorruptConfig(
        seed=42,
        operations=[
            NullCells(
                kind="null_cells",
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


def _config_c7_heal() -> CorruptConfig:
    """Null both halves of the membership pair, across two operations.

    The first null (member__doctor__kind) leaves a half-null pair -- declares
    C7. The second null (member__doctor__id) completes an all-NULL pair --
    C7-conformant in the final emit -- so it declares beyond-c1-c12 and HEALS
    the first null's C7: validate no longer reports C7 failing, yet the
    manifest still declares it (a sound over-approximation). Containment still
    holds; set equality would not.
    """
    return CorruptConfig(
        seed=5,
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
            NullCells(
                kind="null_cells",
                name="null_member_id",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__id"],
                ),
                amount=Amount(rate=1.0),
            ),
        ],
    )


def _config_class_targeted_null() -> CorruptConfig:
    """null_cells over `category: records` -- pooled across records__actor +
    records__doctor, one op, one rule, per-table locators."""
    return CorruptConfig(
        seed=6,
        operations=[
            NullCells(
                kind="null_cells",
                target=Target(category="records", columns=["prop__name"]),
                amount=Amount(rate=1.0),
            )
        ],
    )


_CONFIG_FACTORIES: dict[str, "Callable[[], CorruptConfig]"] = {
    "null_only": _config_null_name,
    "duplicate_pinned": _config_duplicate_pinned,
    "schema_drift_rename": _config_schema_drift_rename,
    "dangle_membership": _config_dangle_membership,
    "four_operations": _config_four_operations,
    "c7_heal": _config_c7_heal,
    "class_targeted_null": _config_class_targeted_null,
}


# ---------------------------------------------------------------------------
# Family-C matrix entries -- over the history-series fixture
# ---------------------------------------------------------------------------


def _config_freeze_flips_c6() -> CorruptConfig:
    """freeze_series suppresses the status series' anchor tail -> C6."""
    return CorruptConfig(
        seed=101,
        operations=[
            FreezeSeries(
                kind="freeze_series",
                name="freeze_status_series",
                target=Target(table="history", where={"property": "status"}),
                amount=Amount(rate=1.0),
                cut="after_first",
            )
        ],
    )


def _config_drop_pure_beyond_c1_c12() -> CorruptConfig:
    """drop_events, clustered_temporal-placed to hit only a non-anchor
    mid-series `name` event -> impact is pure beyond-c1-c12."""
    return CorruptConfig(
        seed=4,
        operations=[
            DropEvents(
                kind="drop_events",
                name="drop_name_events",
                target=Target(table="history", where={"property": "name"}),
                amount=Amount(count=1),
                placement=ClusteredTemporal(
                    kind="clustered_temporal", column="sim_time", clusters=1, width=1
                ),
            )
        ],
    )


def _config_family_c_heal() -> CorruptConfig:
    """A shift promotes an event to the `name` series' anchor (declares C6);
    a later drop_events removes that very event -- the shift's C6 label
    becomes a sound over-declaration once the promoted event is gone.
    """
    return CorruptConfig(
        seed=8,
        operations=[
            ShiftSimTime(
                kind="shift_sim_time",
                name="promote_v2_to_anchor",
                target=Target(
                    table="history", where={"property": "name", "sim_time": "60"}
                ),
                amount=Amount(count=1),
                shift=ShiftOffset(
                    kind="offset",
                    distribution=Distribution(shape="uniform", low=35, high=35),
                ),
            ),
            DropEvents(
                kind="drop_events",
                name="drop_promoted_event",
                target=Target(
                    table="history", where={"property": "name", "sim_time": "95"}
                ),
                amount=Amount(count=1),
            ),
        ],
    )


def _config_schema_drift_then_freeze() -> CorruptConfig:
    """schema_drift drops records__actor.prop__status; the following
    freeze_series on the status series is then skip-gated (no round-trippable
    prop__ column) -- every one of its defects declares beyond-c1-c12.
    """
    return CorruptConfig(
        seed=9,
        operations=[
            SchemaDrift(
                kind="schema_drift",
                name="drop_status_column",
                target=Target(table="records__actor"),
                drop=["prop__status"],
            ),
            FreezeSeries(
                kind="freeze_series",
                name="freeze_status_series_skip_gated",
                target=Target(table="history", where={"property": "status"}),
                amount=Amount(rate=1.0),
                cut="after_first",
            ),
        ],
    )


_FAMILY_C_CONFIG_FACTORIES: dict[str, "Callable[[], CorruptConfig]"] = {
    "freeze_flips_c6": _config_freeze_flips_c6,
    "drop_pure_beyond_c1_c12": _config_drop_pure_beyond_c1_c12,
    "family_c_heal": _config_family_c_heal,
    "schema_drift_then_freeze_skip_gated": _config_schema_drift_then_freeze,
}


def _run_and_agreement_sets(
    config: CorruptConfig,
    tmp_path: Path,
    *,
    build_fixture: "Callable[[Path], None]" = build_spanning,
) -> tuple[set[str], set[str]]:
    """Run `config` against a fresh fixture; return (failing, impact_union).

    Args:
        config: The corrupter config to apply.
        tmp_path: A per-test scratch directory.
        build_fixture: The fixture builder; defaults to the spanning fixture
            (`build_history_series` for the family-C matrix entries, whose
            operations require multi-event history series).

    Returns:
        A tuple of (validate's failing-check set on the corrupted emit, scoped to
        the manifest's C1-C12 impact vocabulary, and the manifest's non-sentinel
        impact-code union). C13 is excluded from the failing set: no corrupter
        operation can declare it (see `failing_checks_in_manifest_vocabulary`).
    """
    emit_dir = tmp_path / "source"
    build_fixture(emit_dir)
    out_dir = tmp_path / "out"

    with open_emit(emit_dir) as emit:
        corrupt_emit(emit, config, out_dir)

    with open_emit(out_dir) as corrupted:
        report = conformance.validate(corrupted)
    failing = failing_checks_in_manifest_vocabulary(report)

    manifest = json.loads((out_dir / "defects.json").read_text(encoding="utf-8"))
    impact_union = {
        code
        for defect in manifest["defects"]
        for code in defect["impact"]
        if code != "beyond-c1-c12"
    }
    return failing, impact_union


@pytest.mark.parametrize("name", sorted(_CONFIG_FACTORIES), ids=lambda n: n)
def test_containment_holds(name: str, tmp_path: Path) -> None:
    """validate's failing-check set is a subset of the manifest impact union."""
    config = _CONFIG_FACTORIES[name]()
    failing, impact_union = _run_and_agreement_sets(config, tmp_path)
    assert failing <= impact_union


def test_c7_heal_demonstrates_sound_overapproximation(tmp_path: Path) -> None:
    """The C7-heal config: C7 is declared but no longer fails validate.

    A concrete witness that containment (not set equality) is what universally
    holds -- the manifest may over-declare relative to the final emit's
    validate verdict, never under-declare.
    """
    failing, impact_union = _run_and_agreement_sets(_config_c7_heal(), tmp_path)
    assert "C7" in impact_union
    assert "C7" not in failing
    assert failing <= impact_union


# ---------------------------------------------------------------------------
# Family-C matrix -- over the history-series fixture
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_FAMILY_C_CONFIG_FACTORIES), ids=lambda n: n)
def test_family_c_containment_holds(name: str, tmp_path: Path) -> None:
    """validate's failing-check set is a subset of the manifest impact union,
    over the family-C matrix (history-series fixture)."""
    config = _FAMILY_C_CONFIG_FACTORIES[name]()
    failing, impact_union = _run_and_agreement_sets(
        config, tmp_path, build_fixture=build_history_series
    )
    assert failing <= impact_union


def test_freeze_flips_c6_exactly(tmp_path: Path) -> None:
    """freeze_series suppresses the status series' anchor tail: the manifest
    declares exactly C6, and validate fails exactly C6."""
    failing, impact_union = _run_and_agreement_sets(
        _config_freeze_flips_c6(), tmp_path, build_fixture=build_history_series
    )
    assert impact_union == {"C6"}
    assert failing == {"C6"}


def test_drop_pure_beyond_c1_c12_validate_fully_passes(tmp_path: Path) -> None:
    """The subconformance drop: impact is pure beyond-c1-c12 and validate
    reports no failing check at all."""
    failing, impact_union = _run_and_agreement_sets(
        _config_drop_pure_beyond_c1_c12(), tmp_path, build_fixture=build_history_series
    )
    assert impact_union == set()
    assert failing == set()


def test_family_c_heal_demonstrates_sound_overapproximation(tmp_path: Path) -> None:
    """A shift promotes an event to anchor (declares C6); a later drop_events
    removes that very event, healing the round-trip -- C6 no longer fails
    validate, yet the manifest still declares it.
    """
    failing, impact_union = _run_and_agreement_sets(
        _config_family_c_heal(), tmp_path, build_fixture=build_history_series
    )
    assert "C6" in impact_union
    assert "C6" not in failing
    assert failing <= impact_union


def test_schema_drift_then_freeze_skip_gates_freeze_defects(tmp_path: Path) -> None:
    """schema_drift drops the tracked prop__ column before freeze_series runs:
    the freeze's own defects are all skip-gated to beyond-c1-c12; only the
    drop's own C11 declaration fires."""
    failing, impact_union = _run_and_agreement_sets(
        _config_schema_drift_then_freeze(),
        tmp_path,
        build_fixture=build_history_series,
    )
    assert impact_union == {"C11"}
    assert failing == {"C11"}


# ---------------------------------------------------------------------------
# mutate_cells matrix -- over the history-series fixture
# ---------------------------------------------------------------------------


def _config_mutate_records_c6() -> CorruptConfig:
    """mutate_cells case-drift on a tracked records prop__ cell -- the
    records surface of C6 (the cell itself now disagrees with its series'
    anchor)."""
    return CorruptConfig(
        seed=201,
        operations=[
            MutateCells(
                kind="mutate_cells",
                name="drift_status_cell",
                target=Target(table="records__actor", columns=["prop__status"]),
                amount=Amount(rate=1.0),
                mutation=MutationCase(kind="case", form="upper"),
            )
        ],
    )


def _config_mutate_history_c6() -> CorruptConfig:
    """mutate_cells case-drift on the `name` series' anchor row in
    `history.value` -- the changelog surface of C6."""
    return CorruptConfig(
        seed=202,
        operations=[
            MutateCells(
                kind="mutate_cells",
                name="drift_name_anchor",
                target=Target(
                    table="history",
                    where={"property": "name", "sim_time": "90"},
                    columns=["value"],
                ),
                amount=Amount(rate=1.0),
                mutation=MutationCase(kind="case", form="upper"),
            )
        ],
    )


def _config_mutate_out_of_domain_c12() -> CorruptConfig:
    """mutate_cells out_of_domain on the actor sub-type discriminator -- C12."""
    return CorruptConfig(
        seed=203,
        operations=[
            MutateCells(
                kind="mutate_cells",
                name="drift_actor_type",
                target=Target(table="records__actor", columns=["prop__actor_type"]),
                amount=Amount(rate=1.0),
                mutation=MutationOutOfDomain(kind="out_of_domain"),
            )
        ],
    )


def _config_mutate_pure_beyond_c1_c12() -> CorruptConfig:
    """mutate_cells sentinel on an untracked records prop__ column -- pure
    subconformance, no C6/C12 anywhere."""
    return CorruptConfig(
        seed=204,
        operations=[
            MutateCells(
                kind="mutate_cells",
                name="sentinel_doctor_name",
                target=Target(table="records__doctor", columns=["prop__name"]),
                amount=Amount(rate=1.0),
                mutation=MutationSentinel(kind="sentinel", value="N/A"),
            )
        ],
    )


def _config_mutate_cells_with_every_family() -> CorruptConfig:
    """mutate_cells (records C6, history C6, and C12) mixed with one
    operation from every other shipped family: null_cells (family A's other
    member), duplicate_rows, schema_drift, dangle_reference, and
    drop_events (family C)."""
    return CorruptConfig(
        seed=205,
        operations=[
            MutateCells(
                kind="mutate_cells",
                name="drift_status_cell",
                target=Target(table="records__actor", columns=["prop__status"]),
                amount=Amount(rate=1.0),
                mutation=MutationCase(kind="case", form="upper"),
            ),
            MutateCells(
                kind="mutate_cells",
                name="drift_wait_minutes_anchor",
                target=Target(
                    table="history",
                    where={"property": "wait_minutes", "sim_time": "50"},
                    columns=["value"],
                ),
                amount=Amount(rate=1.0),
                mutation=MutationCase(kind="case", form="upper"),
            ),
            MutateCells(
                kind="mutate_cells",
                name="drift_actor_type",
                target=Target(table="records__actor", columns=["prop__actor_type"]),
                amount=Amount(rate=1.0),
                mutation=MutationOutOfDomain(kind="out_of_domain"),
            ),
            NullCells(
                kind="null_cells",
                name="null_slot",
                target=Target(
                    table="membership__actor__appointments", columns=["elem__slot"]
                ),
                amount=Amount(rate=1.0),
            ),
            DuplicateRows(
                kind="duplicate_rows",
                target=Target(table="records__doctor"),
                amount=Amount(count=1),
            ),
            SchemaDrift(
                kind="schema_drift",
                name="rename_doctor_name",
                target=Target(table="records__doctor"),
                rename_to={"prop__name": "prop__full_name"},
            ),
            DangleReference(
                kind="dangle_reference",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__id"],
                ),
                amount=Amount(rate=1.0),
            ),
            DropEvents(
                kind="drop_events",
                name="drop_name_v0",
                target=Target(
                    table="history", where={"property": "name", "sim_time": "10"}
                ),
                amount=Amount(count=1),
            ),
        ],
    )


_MUTATE_CONFIG_FACTORIES: dict[str, "Callable[[], CorruptConfig]"] = {
    "mutate_records_c6": _config_mutate_records_c6,
    "mutate_history_c6": _config_mutate_history_c6,
    "mutate_out_of_domain_c12": _config_mutate_out_of_domain_c12,
    "mutate_pure_beyond_c1_c12": _config_mutate_pure_beyond_c1_c12,
    "mutate_cells_with_every_family": _config_mutate_cells_with_every_family,
}


@pytest.mark.parametrize("name", sorted(_MUTATE_CONFIG_FACTORIES), ids=lambda n: n)
def test_mutate_cells_containment_holds(name: str, tmp_path: Path) -> None:
    """validate's failing-check set is a subset of the manifest impact union,
    over the mutate_cells matrix (history-series fixture)."""
    config = _MUTATE_CONFIG_FACTORIES[name]()
    failing, impact_union = _run_and_agreement_sets(
        config, tmp_path, build_fixture=build_history_series
    )
    assert failing <= impact_union


def test_mutate_records_c6_exactly(tmp_path: Path) -> None:
    """A records-surface mutate_cells C6 declaration: the manifest declares
    exactly C6, and validate fails exactly C6."""
    failing, impact_union = _run_and_agreement_sets(
        _config_mutate_records_c6(), tmp_path, build_fixture=build_history_series
    )
    assert impact_union == {"C6"}
    assert failing == {"C6"}


def test_mutate_history_c6_exactly(tmp_path: Path) -> None:
    """A history-surface mutate_cells C6 declaration: the manifest declares
    exactly C6, and validate fails exactly C6."""
    failing, impact_union = _run_and_agreement_sets(
        _config_mutate_history_c6(), tmp_path, build_fixture=build_history_series
    )
    assert impact_union == {"C6"}
    assert failing == {"C6"}


def test_mutate_out_of_domain_c12_exactly(tmp_path: Path) -> None:
    """The out_of_domain mutate_cells C12 declaration: the manifest declares
    exactly C12, and validate fails exactly C12."""
    failing, impact_union = _run_and_agreement_sets(
        _config_mutate_out_of_domain_c12(), tmp_path, build_fixture=build_history_series
    )
    assert impact_union == {"C12"}
    assert failing == {"C12"}


def test_mutate_pure_beyond_c1_c12_validate_fully_passes(tmp_path: Path) -> None:
    """The mutate_cells subconformance case: impact is pure beyond-c1-c12 and
    validate reports no failing check at all."""
    failing, impact_union = _run_and_agreement_sets(
        _config_mutate_pure_beyond_c1_c12(),
        tmp_path,
        build_fixture=build_history_series,
    )
    assert impact_union == set()
    assert failing == set()


# ---------------------------------------------------------------------------
# mispoint_reference matrix -- over the history-series fixture
# ---------------------------------------------------------------------------


def _config_mispoint_unconstrained() -> CorruptConfig:
    """Unconstrained mispoint_reference on the membership id -- the donor
    resolves by construction, so impact is pure beyond-c1-c12."""
    return CorruptConfig(
        seed=301,
        operations=[
            MispointReference(
                kind="mispoint_reference",
                name="mispoint_appointment_doctor",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__id"],
                ),
                amount=Amount(rate=1.0),
            )
        ],
    )


def _config_mispoint_constrained() -> CorruptConfig:
    """`constraint: created_after_reference` mispoint_reference on the same
    cell -- still a valid FK (beyond-c1-c12), but the point-in-time class it
    declares is recoverable only via the manifest."""
    return CorruptConfig(
        seed=302,
        operations=[
            MispointReference(
                kind="mispoint_reference",
                name="late_doctor_on_appointment",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__id"],
                ),
                amount=Amount(rate=1.0),
                constraint="created_after_reference",
            )
        ],
    )


def _config_mispoint_mixed_with_every_family() -> CorruptConfig:
    """Both mispoint_reference variants (unconstrained on a records prop__
    reference, constrained on the membership reference) mixed with one
    operation from every other shipped family: null_cells, duplicate_rows,
    schema_drift, dangle_reference, and drop_events."""
    return CorruptConfig(
        seed=303,
        operations=[
            MispointReference(
                kind="mispoint_reference",
                name="mispoint_prop_doctor_id",
                target=Target(table="records__actor", columns=["prop__doctor_id"]),
                amount=Amount(rate=1.0),
            ),
            MispointReference(
                kind="mispoint_reference",
                name="late_doctor_on_appointment",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__id"],
                ),
                amount=Amount(rate=1.0),
                constraint="created_after_reference",
            ),
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
                name="rename_doctor_name",
                target=Target(table="records__doctor"),
                rename_to={"prop__name": "prop__full_name"},
            ),
            DropEvents(
                kind="drop_events",
                name="drop_name_v0",
                target=Target(
                    table="history", where={"property": "name", "sim_time": "10"}
                ),
                amount=Amount(count=1),
            ),
        ],
    )


_MISPOINT_CONFIG_FACTORIES: dict[str, "Callable[[], CorruptConfig]"] = {
    "mispoint_unconstrained": _config_mispoint_unconstrained,
    "mispoint_constrained": _config_mispoint_constrained,
    "mispoint_mixed_with_every_family": _config_mispoint_mixed_with_every_family,
}


@pytest.mark.parametrize("name", sorted(_MISPOINT_CONFIG_FACTORIES), ids=lambda n: n)
def test_mispoint_reference_containment_holds(name: str, tmp_path: Path) -> None:
    """validate's failing-check set is a subset of the manifest impact union,
    over the mispoint_reference matrix (history-series fixture) -- including
    constrained and unconstrained mispoint_reference alongside every other
    shipped family."""
    config = _MISPOINT_CONFIG_FACTORIES[name]()
    failing, impact_union = _run_and_agreement_sets(
        config, tmp_path, build_fixture=build_history_series
    )
    assert failing <= impact_union


def test_mispoint_unconstrained_validate_fully_passes(tmp_path: Path) -> None:
    """The unconstrained mis-point: impact is pure beyond-c1-c12 and validate
    reports no failing check at all -- green RI over a wrong-but-real donor."""
    failing, impact_union = _run_and_agreement_sets(
        _config_mispoint_unconstrained(), tmp_path, build_fixture=build_history_series
    )
    assert impact_union == set()
    assert failing == set()


def test_mispoint_constrained_validate_fully_passes(tmp_path: Path) -> None:
    """The constrained mis-point (point-in-time dangle): impact is pure
    beyond-c1-c12 and validate reports no failing check at all -- the
    late-arriving-dimension defect is invisible to C1-C12 by construction."""
    failing, impact_union = _run_and_agreement_sets(
        _config_mispoint_constrained(), tmp_path, build_fixture=build_history_series
    )
    assert impact_union == set()
    assert failing == set()


# ---------------------------------------------------------------------------
# Row-set operations matrix -- over the history-series fixture
# ---------------------------------------------------------------------------


def _config_delete_referenced_doctor_c10() -> CorruptConfig:
    """delete_rows on the referenced doctor -- the membership row survives,
    now resolving to a dangling id -- C10 (a delete_rows wake arm)."""
    return CorruptConfig(
        seed=401,
        operations=[
            DeleteRows(
                kind="delete_rows",
                name="delete_referenced_doctor",
                target=Target(table="records__doctor", where={"record_id": "d001"}),
                amount=Amount(count=1),
            )
        ],
    )


def _config_delete_pinned_actor_c6_c9() -> CorruptConfig:
    """delete_rows on the pinned actor, with a bystander phantom keeping the
    table non-empty -- C6 (orphaned series) + C9 (broken pin), a
    delete_rows wake arm."""
    return CorruptConfig(
        seed=402,
        operations=[
            InsertRows(
                kind="insert_rows",
                name="bystander_actor",
                target=Target(table="records__actor"),
                amount=Amount(count=1),
            ),
            DeleteRows(
                kind="delete_rows",
                name="delete_pinned_actor",
                target=Target(table="records__actor", where={"record_id": "a001"}),
                amount=Amount(count=1),
            ),
        ],
    )


def _config_delete_membership_beyond() -> CorruptConfig:
    """delete_rows on a membership row -- always beyond-c1-c12 (a delete_rows
    wake arm; no C1-C12 check quantifies over interval existence)."""
    return CorruptConfig(
        seed=403,
        operations=[
            DeleteRows(
                kind="delete_rows",
                name="delete_membership_row",
                target=Target(table="membership__actor__appointments"),
                amount=Amount(rate=1.0),
            )
        ],
    )


def _config_insert_rows_c13() -> CorruptConfig:
    """insert_rows into a kind with a tracked property -- the phantom carries no
    history, so it lacks its genesis row for `prop__specialty` (C13)."""
    return CorruptConfig(
        seed=404,
        operations=[
            InsertRows(
                kind="insert_rows",
                name="ghost_doctors",
                target=Target(table="records__doctor", columns=["prop__name"]),
                amount=Amount(count=2),
            )
        ],
    )


def _config_duplicate_mutation_c6_c9() -> CorruptConfig:
    """duplicate_rows `mutation: typo` on the pinned actor's tracked name --
    C6 (round-trip failure) + C9 (pinned id), a duplicate_rows mutation arm."""
    return CorruptConfig(
        seed=405,
        operations=[
            DuplicateRows(
                kind="duplicate_rows",
                name="split_brain_actor_name",
                target=Target(table="records__actor", columns=["prop__name"]),
                amount=Amount(count=1),
                mutation=MutationTypo(kind="typo"),
            )
        ],
    )


def _config_duplicate_mutation_c12() -> CorruptConfig:
    """duplicate_rows `mutation: out_of_domain` on the actor sub-type
    discriminator -- C9 (pinned id) + C12 (undeclared sub-type), a
    duplicate_rows mutation arm."""
    return CorruptConfig(
        seed=406,
        operations=[
            DuplicateRows(
                kind="duplicate_rows",
                name="split_brain_actor_type",
                target=Target(table="records__actor", columns=["prop__actor_type"]),
                amount=Amount(count=1),
                mutation=MutationOutOfDomain(kind="out_of_domain"),
            )
        ],
    )


def _config_heal_dangle_then_delete_membership() -> CorruptConfig:
    """A dangle_reference declares C10 on a membership reference; a later
    delete_rows removes that very membership row -- the row itself is gone,
    healing C10 in the final emit while the manifest still declares it (a
    sound over-approximation)."""
    return CorruptConfig(
        seed=407,
        operations=[
            DangleReference(
                kind="dangle_reference",
                name="dangle_membership",
                target=Target(
                    table="membership__actor__appointments",
                    columns=["member__doctor__id"],
                ),
                amount=Amount(rate=1.0),
            ),
            DeleteRows(
                kind="delete_rows",
                name="delete_membership_row",
                target=Target(table="membership__actor__appointments"),
                amount=Amount(rate=1.0),
            ),
        ],
    )


def _config_heal_duplicate_then_delete_some_copies() -> CorruptConfig:
    """duplicate_rows raises the pinned actor's row count to two; deleting
    only one copy restores single-copy survival, healing the duplicate's own
    C9 in the final emit while the manifest still declares it."""
    return CorruptConfig(
        seed=408,
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


_ROWSET_CONFIG_FACTORIES: dict[str, "Callable[[], CorruptConfig]"] = {
    "delete_referenced_doctor_c10": _config_delete_referenced_doctor_c10,
    "delete_pinned_actor_c6_c9": _config_delete_pinned_actor_c6_c9,
    "delete_membership_beyond": _config_delete_membership_beyond,
    "insert_rows_c13": _config_insert_rows_c13,
    "duplicate_mutation_c6_c9": _config_duplicate_mutation_c6_c9,
    "duplicate_mutation_c12": _config_duplicate_mutation_c12,
    "heal_dangle_then_delete_membership": _config_heal_dangle_then_delete_membership,
    "heal_duplicate_then_delete_some_copies": (
        _config_heal_duplicate_then_delete_some_copies
    ),
}


@pytest.mark.parametrize("name", sorted(_ROWSET_CONFIG_FACTORIES), ids=lambda n: n)
def test_rowset_containment_holds(name: str, tmp_path: Path) -> None:
    """validate's failing-check set is a subset of the manifest impact union,
    over the row-set operations matrix (history-series fixture) -- delete_rows
    wake arms, the insert_rows C13 genesis gap, duplicate_rows mutation arms,
    and the healing compositions those operations introduce."""
    config = _ROWSET_CONFIG_FACTORIES[name]()
    failing, impact_union = _run_and_agreement_sets(
        config, tmp_path, build_fixture=build_history_series
    )
    assert failing <= impact_union


def test_insert_rows_declares_c13_genesis_gap(tmp_path: Path) -> None:
    """insert_rows into a tracked kind: the phantom has no genesis history row,
    so the manifest declares C13 and validate agrees (C13 is the only break --
    the phantom is otherwise isolated by construction)."""
    failing, impact_union = _run_and_agreement_sets(
        _config_insert_rows_c13(), tmp_path, build_fixture=build_history_series
    )
    assert impact_union == {"C13"}
    assert failing == {"C13"}


def test_heal_dangle_then_delete_membership_demonstrates_sound_overapproximation(
    tmp_path: Path,
) -> None:
    """The dangle_reference-then-delete_rows heal: C10 is declared but no
    longer fails validate once the dangled row itself is gone."""
    failing, impact_union = _run_and_agreement_sets(
        _config_heal_dangle_then_delete_membership(),
        tmp_path,
        build_fixture=build_history_series,
    )
    assert "C10" in impact_union
    assert "C10" not in failing
    assert failing <= impact_union


def test_heal_duplicate_then_delete_some_copies_demonstrates_sound_overapproximation(
    tmp_path: Path,
) -> None:
    """The duplicate_rows-then-delete_rows heal: C9 is declared by the
    duplicate but no longer fails validate once single-copy survival is
    restored."""
    failing, impact_union = _run_and_agreement_sets(
        _config_heal_duplicate_then_delete_some_copies(),
        tmp_path,
        build_fixture=build_history_series,
    )
    assert "C9" in impact_union
    assert "C9" not in failing
    assert failing <= impact_union


# ---------------------------------------------------------------------------
# Family-E matrix -- over the membership-intervals fixture
# ---------------------------------------------------------------------------


def _config_distort_overlap_and_gap() -> CorruptConfig:
    """overlap + gap over membership__actor__oncall -- pure beyond-c1-c12;
    validate stays fully green."""
    return CorruptConfig(
        seed=601,
        operations=[
            DistortIntervals(
                kind="distort_intervals",
                name="oncall_overlap",
                mode="overlap",
                target=Target(table="membership__actor__oncall"),
                amount=Amount(rate=1.0),
            ),
            DistortIntervals(
                kind="distort_intervals",
                name="oncall_gap",
                mode="gap",
                target=Target(table="membership__actor__oncall"),
                amount=Amount(rate=1.0),
            ),
        ],
    )


def _config_distort_left_before_join() -> CorruptConfig:
    """left_before_join over membership__actor__oncall -- declares C10, and
    validate fails it."""
    return CorruptConfig(
        seed=602,
        operations=[
            DistortIntervals(
                kind="distort_intervals",
                name="oncall_invert",
                mode="left_before_join",
                target=Target(table="membership__actor__oncall"),
                amount=Amount(rate=1.0),
            )
        ],
    )


def _config_distort_mixed_three_modes() -> CorruptConfig:
    """One of each distort_intervals mode, where-scoped to distinct member
    timelines so the three rewrites never touch the same physical row."""
    return CorruptConfig(
        seed=603,
        operations=[
            DistortIntervals(
                kind="distort_intervals",
                name="oncall_overlap",
                mode="overlap",
                target=Target(
                    table="membership__actor__oncall", where={"record_id": "a003"}
                ),
                amount=Amount(rate=1.0),
            ),
            DistortIntervals(
                kind="distort_intervals",
                name="oncall_gap",
                mode="gap",
                target=Target(
                    table="membership__actor__oncall", where={"record_id": "a002"}
                ),
                amount=Amount(rate=1.0),
            ),
            DistortIntervals(
                kind="distort_intervals",
                name="oncall_invert",
                mode="left_before_join",
                target=Target(
                    table="membership__actor__oncall", where={"record_id": "a004"}
                ),
                amount=Amount(rate=1.0),
            ),
        ],
    )


_FAMILY_E_CONFIG_FACTORIES: dict[str, "Callable[[], CorruptConfig]"] = {
    "distort_overlap_and_gap": _config_distort_overlap_and_gap,
    "distort_left_before_join": _config_distort_left_before_join,
    "distort_mixed_three_modes": _config_distort_mixed_three_modes,
}


@pytest.mark.parametrize("name", sorted(_FAMILY_E_CONFIG_FACTORIES), ids=lambda n: n)
def test_family_e_containment_holds(name: str, tmp_path: Path) -> None:
    """validate's failing-check set is a subset of the manifest impact union,
    over the family-E matrix (membership-intervals fixture)."""
    config = _FAMILY_E_CONFIG_FACTORIES[name]()
    failing, impact_union = _run_and_agreement_sets(
        config, tmp_path, build_fixture=build_membership_intervals
    )
    assert failing <= impact_union


def test_distort_overlap_and_gap_validate_fully_passes(tmp_path: Path) -> None:
    """overlap + gap: impact is pure beyond-c1-c12 and validate reports no
    failing check at all."""
    failing, impact_union = _run_and_agreement_sets(
        _config_distort_overlap_and_gap(),
        tmp_path,
        build_fixture=build_membership_intervals,
    )
    assert impact_union == set()
    assert failing == set()


def test_distort_left_before_join_fails_c10(tmp_path: Path) -> None:
    """left_before_join: C10 is in the manifest impact union and in
    validate's failing set."""
    failing, impact_union = _run_and_agreement_sets(
        _config_distort_left_before_join(),
        tmp_path,
        build_fixture=build_membership_intervals,
    )
    assert "C10" in impact_union
    assert "C10" in failing
    assert failing <= impact_union
