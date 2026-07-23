"""Integration test: the corrupt->base composition.

Guards the two invariants the sprint spec's Corrupter composition and
totality section asserts: (1) a base export over a corrupted emit succeeds
and surfaces a declared defect unchanged in the reconstructed value, with no
corrupter-aware branch anywhere in the base mode; (2) totality -- no row is
dropped and no cast error is raised, so the corrupted export's row counts
match the uncorrupted export's row counts exactly. `mode: base` reads
through the same reader/derivations any other consumer of a corrupted emit
would use -- it has no idea the emit was ever corrupted.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from _support.notices import discard_notice_sink
from recipes._recipe_fixture import build_recipe_emit

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    Amount,
    CorruptConfig,
    ExportConfig,
    NullCells,
    Target,
)
from fabulexa_forge.corrupters.engine import corrupt_emit
from fabulexa_forge.exporters.base.engine import export_base
from fabulexa_forge.reader.emit import open_emit


def _null_patient_name_config() -> CorruptConfig:
    """A one-operation corrupt config: null `records__patient`'s `prop__name`
    cell on p001 alone (`where` scopes the operation to exactly the one row
    this test declares and later observes as the defect)."""
    return CorruptConfig(
        seed=1,
        operations=[
            NullCells(
                kind="null_cells",
                name="null_patient_name",
                target=Target(
                    table="records__patient",
                    where={"record_id": "p001"},
                    columns=["prop__name"],
                ),
                amount=Amount(rate=1.0),
            ),
        ],
    )


def _export_base_row_counts(emit_dir: Path, out_dir: Path) -> dict[str, int]:
    """Run a bare mode='base' export of emit_dir into out_dir/csv; return row
    counts per output table."""
    out_dir.mkdir(parents=True, exist_ok=True)
    config = ExportConfig(mode="base")
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        return export_base(
            emit, config, out_dir, "csv", anchor, notice_sink=discard_notice_sink
        )


def test_corrupt_then_base_export_surfaces_declared_defect(tmp_path: Path) -> None:
    """A base export over a corrupted emit succeeds; the declared defect (a
    nulled `records__patient.prop__name` cell) is observable in the flat
    reconstruction."""
    source_dir = tmp_path / "source"
    build_recipe_emit(source_dir)

    corrupt_dir = tmp_path / "corrupted"
    with open_emit(source_dir) as emit:
        report = corrupt_emit(emit, _null_patient_name_config(), corrupt_dir)

    assert report.outcomes[0].units_affected == 1

    manifest = json.loads((corrupt_dir / "defects.json").read_text(encoding="utf-8"))
    (defect,) = manifest["defects"]
    assert defect["location"]["kind"] == "cell"
    assert defect["location"]["table"] == "records__patient"
    assert defect["location"]["column"] == "prop__name"
    record_id = dict(defect["location"]["row"]["keys"])["record_id"]

    out_dir = tmp_path / "dump"
    row_counts = _export_base_row_counts(corrupt_dir, out_dir)

    assert "patient" in row_counts

    with (out_dir / "patient.csv").open(newline="", encoding="utf-8") as fh:
        patient_rows = list(csv.DictReader(fh))
    (target_row,) = [row for row in patient_rows if row["id"] == record_id]
    assert target_row["prop__name"] == ""


def test_corrupt_then_base_export_is_total(tmp_path: Path) -> None:
    """No row is dropped and no cast error is raised: a base export's row
    counts over a corrupted emit exactly match the uncorrupted export's --
    the totality guarantee."""
    source_dir = tmp_path / "source"
    build_recipe_emit(source_dir)

    corrupt_dir = tmp_path / "corrupted"
    with open_emit(source_dir) as emit:
        corrupt_emit(emit, _null_patient_name_config(), corrupt_dir)

    baseline_counts = _export_base_row_counts(source_dir, tmp_path / "baseline_dump")
    corrupted_counts = _export_base_row_counts(corrupt_dir, tmp_path / "corrupted_dump")

    assert corrupted_counts == baseline_counts
