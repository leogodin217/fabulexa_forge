"""Tests for defect manifest canonicalisation, id assignment, and the
byte-deterministic defects.json serializer."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from fabulexa_export.corrupters.manifest import (
    DefectManifest,
    DefectRecord,
    DefectSource,
)
from fabulexa_export.corrupters.manifest_build import (
    build_defect_manifest,
    derive_defect_id,
    write_defect_manifest,
)
from fabulexa_export.errors import CorruptError

_SOURCE = DefectSource(sidecar_sha256="a" * 64, base_format_version=4)
_CONFIG_FINGERPRINT = "b" * 64
_CODE_VERSION = "0.0.1"


def _column_locator(
    table: str = "records__patient", column: str = "prop__email"
) -> dict[str, object]:
    return {"kind": "column", "table": table, "column": column}


def _row_locator(
    table: str = "records__patient",
    category: str = "records",
    record_id: str = "r1",
) -> dict[str, object]:
    return {
        "kind": "row",
        "table": table,
        "row": {
            "category": category,
            "keys": [["fork_path", "trunk"], ["record_id", record_id]],
        },
    }


def _record(**overrides: object) -> DefectRecord:
    kwargs: dict[str, object] = {
        "class": "missing_value",
        "rule": "null_patient_email",
        "impact": ["C6"],
        "location": _column_locator(),
    }
    kwargs.update(overrides)
    return DefectRecord.model_validate(kwargs)


def _build(records: list[DefectRecord]) -> DefectManifest:
    return build_defect_manifest(_SOURCE, _CONFIG_FINGERPRINT, _CODE_VERSION, records)


# ---------------------------------------------------------------------------
# Canonical order
# ---------------------------------------------------------------------------


def test_canonical_order_by_impact_when_otherwise_identical() -> None:
    """Records differing only in impact order deterministically by impact."""
    a = _record(impact=["C7"])
    b = _record(impact=["C6"])
    manifest = _build([a, b])
    assert [d.impact for d in manifest.defects] == [("C6",), ("C7",)]


def test_identical_records_get_sequential_ordinals_and_distinct_ids() -> None:
    """N identical records get ordinals 0..N-1 and distinct defect_ids."""
    records = [_record() for _ in range(3)]
    manifest = _build(records)
    ids = [d.defect_id for d in manifest.defects]
    assert len(ids) == 3
    assert len(set(ids)) == 3


def test_input_order_of_records_irrelevant_to_output_bytes(tmp_path: Path) -> None:
    """Shuffling the input records list yields byte-identical defects.json."""
    records = [_record(rule=f"rule_{i}") for i in range(5)]
    shuffled = list(records)
    rng = random.Random(7)
    rng.shuffle(shuffled)

    manifest_a = _build(records)
    manifest_b = _build(shuffled)

    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    out_a.mkdir()
    out_b.mkdir()
    path_a = write_defect_manifest(manifest_a, out_a)
    path_b = write_defect_manifest(manifest_b, out_b)
    assert path_a.read_bytes() == path_b.read_bytes()


# ---------------------------------------------------------------------------
# derive_defect_id
# ---------------------------------------------------------------------------


def test_derive_defect_id_omits_impact() -> None:
    """Two records sharing (class, rule, locator) but differing in impact
    still get distinct ids, disambiguated via the occurrence ordinal."""
    a = _record(impact=["C6"])
    b = _record(impact=["C7"])
    manifest = _build([a, b])
    ids = {d.defect_id for d in manifest.defects}
    assert len(ids) == 2


def test_derive_defect_id_deterministic() -> None:
    """derive_defect_id is a pure function of its inputs."""
    record = _record()
    assert derive_defect_id(record, 0) == derive_defect_id(record, 0)
    assert derive_defect_id(record, 0) != derive_defect_id(record, 1)


# ---------------------------------------------------------------------------
# Build invariants
# ---------------------------------------------------------------------------


def test_malformed_table_name_raises_corrupt_error() -> None:
    """A malformed locator table name raises CorruptError, not ValueError."""
    record = _record(location=_column_locator(table="not_a_valid_table!"))
    with pytest.raises(CorruptError):
        _build([record])


def test_rowref_category_table_mismatch_raises_corrupt_error() -> None:
    """A RowRef.category disagreeing with its table's implied category
    raises CorruptError, not ValueError. The RowRef's own keys are
    self-consistent with its declared category (records); it is the
    membership-shaped table name that disagrees."""
    record = _record(
        **{"class": "duplicate_row"},
        impact=["beyond-c1-c12"],
        location=_row_locator(
            table="membership__patient__assigned_ward",
            category="records",
        ),
    )
    with pytest.raises(CorruptError):
        _build([record])


def test_well_formed_records_table_accepted() -> None:
    """A well-formed records__<kind> table with a matching category builds
    cleanly."""
    record = _record(
        **{"class": "duplicate_row"},
        impact=["beyond-c1-c12"],
        location=_row_locator(table="records__patient", category="records"),
    )
    manifest = _build([record])
    assert len(manifest.defects) == 1


# ---------------------------------------------------------------------------
# write_defect_manifest
# ---------------------------------------------------------------------------


def test_write_defect_manifest_is_byte_deterministic(tmp_path: Path) -> None:
    """Writing the same manifest twice produces byte-identical files."""
    manifest = _build([_record()])
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    out_a.mkdir()
    out_b.mkdir()
    path_a = write_defect_manifest(manifest, out_a)
    path_b = write_defect_manifest(manifest, out_b)
    assert path_a.read_bytes() == path_b.read_bytes()


def test_write_defect_manifest_sorted_keys_and_trailing_newline(tmp_path: Path) -> None:
    """The written file has sorted keys, compact separators, and a trailing
    newline."""
    manifest = _build([_record()])
    path = write_defect_manifest(manifest, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    assert ", " not in text
    assert ": " not in text
    keys_in_order = [
        "code_version",
        "config_fingerprint",
        "counts",
        "defect_manifest_version",
    ]
    positions = [text.index(f'"{key}"') for key in keys_in_order]
    assert positions == sorted(positions)
