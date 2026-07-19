"""Integration test: the corrupt->source composition.

Guards the invariant the sprint spec calls out: a source export over a
corrupted emit succeeds and a defect declared in `defects.json` is observable
in the resulting dump, with no corrupter-aware branch anywhere in the source
mode. `mode: source` reads through the same reader/derivations any other
consumer of a corrupted emit would use -- it has no idea the emit was ever
corrupted.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from _support.notices import discard_notice_sink
from reader._fixtures_build import build_spanning

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import (
    Amount,
    CorruptConfig,
    ExportConfig,
    NullCells,
    Target,
)
from fabulexa_forge.corrupters.engine import corrupt_emit
from fabulexa_forge.exporters.source.engine import export_source
from fabulexa_forge.reader.emit import open_emit


def _null_doctor_name_config() -> CorruptConfig:
    """A one-operation corrupt config: null `records__doctor`'s `prop__name`
    cell on d001 alone (the spanning fixture now carries three doctor rows —
    `where` scopes the operation to exactly the one row this test declares
    and later observes as the defect).
    """
    return CorruptConfig(
        seed=1,
        operations=[
            NullCells(
                kind="null_cells",
                name="null_doctor_name",
                target=Target(
                    table="records__doctor",
                    where={"record_id": "d001"},
                    columns=["prop__name"],
                ),
                amount=Amount(rate=1.0),
            ),
        ],
    )


def test_corrupt_then_source_export_surfaces_declared_defect(tmp_path: Path) -> None:
    """A source export over a corrupted emit succeeds; the declared defect (a
    nulled `records__doctor.prop__name` cell) is observable in the dump."""
    source_dir = tmp_path / "source"
    build_spanning(source_dir)

    corrupt_dir = tmp_path / "corrupted"
    with open_emit(source_dir) as emit:
        report = corrupt_emit(emit, _null_doctor_name_config(), corrupt_dir)

    assert report.outcomes[0].units_affected == 1

    manifest = json.loads((corrupt_dir / "defects.json").read_text(encoding="utf-8"))
    (defect,) = manifest["defects"]
    assert defect["location"]["kind"] == "cell"
    assert defect["location"]["table"] == "records__doctor"
    assert defect["location"]["column"] == "prop__name"
    record_id = dict(defect["location"]["row"]["keys"])["record_id"]

    config = ExportConfig(mode="source")
    out_dir = tmp_path / "dump"
    out_dir.mkdir()
    with open_emit(corrupt_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        row_counts = export_source(
            emit, config, out_dir, "csv", anchor, notice_sink=discard_notice_sink
        )

    assert "doctor" in row_counts

    with (out_dir / "doctor.csv").open(newline="", encoding="utf-8") as fh:
        doctor_rows = list(csv.DictReader(fh))
    (target_row,) = [row for row in doctor_rows if row["id"] == record_id]
    assert target_row["name"] == ""
