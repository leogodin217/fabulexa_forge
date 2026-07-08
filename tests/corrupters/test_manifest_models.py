"""Tests for the defect manifest value/model types and their validators."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from fabulexa_export.corrupters.manifest import (
    DefectCounts,
    DefectManifest,
    DefectRecord,
    DefectSource,
    ManifestDefect,
    RowRef,
    _normalize_impact,
)

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "fabulexa_export"
    / "corrupters"
    / "defect_manifest.schema.json"
)


def _column_locator(
    table: str = "records__patient", column: str = "prop__email"
) -> dict[str, object]:
    return {"kind": "column", "table": table, "column": column}


def _defect_record_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "class": "missing_value",
        "rule": "null_patient_email",
        "impact": ["C6"],
        "location": _column_locator(),
    }
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# normalize_impact
# ---------------------------------------------------------------------------


def test_normalize_impact_sorts_and_dedups() -> None:
    """["C7", "C6", "C6"] -> ("C6", "C7")."""
    assert _normalize_impact(["C7", "C6", "C6"]) == ("C6", "C7")


def test_normalize_impact_empty_rejected() -> None:
    """An empty impact list raises."""
    with pytest.raises(ValueError, match="non-empty"):
        _normalize_impact([])


def test_normalize_impact_mixes_sentinel_and_real_code_rejected() -> None:
    """["beyond-c1-c12", "C6"] raises (the mix is self-contradictory)."""
    with pytest.raises(ValueError, match="cannot mix"):
        _normalize_impact(["beyond-c1-c12", "C6"])


def test_defect_record_impact_normalizes_at_construction() -> None:
    """DefectRecord.impact is sorted/deduped through the field validator."""
    record = DefectRecord.model_validate(
        _defect_record_kwargs(impact=["C7", "C6", "C6"])
    )
    assert record.impact == ("C6", "C7")


def test_defect_record_invalid_impact_code_rejected() -> None:
    """An impact code outside the ImpactCode literal is rejected."""
    with pytest.raises(ValidationError):
        DefectRecord.model_validate(_defect_record_kwargs(impact=["C1"]))


# ---------------------------------------------------------------------------
# defect_class shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tag", ["dup_row", "missing_value", "column_rename"])
def test_defect_class_valid_shapes_accepted(tag: str) -> None:
    """Well-shaped lower_snake_case tags are accepted."""
    record = DefectRecord.model_validate(_defect_record_kwargs(**{"class": tag}))
    assert record.defect_class == tag


@pytest.mark.parametrize("tag", ["Dup", "_x", "a__b", "a_"])
def test_defect_class_invalid_shapes_rejected(tag: str) -> None:
    """Dup (uppercase), _x (leading underscore), a__b (double underscore), and
    a_ (trailing underscore) are all rejected."""
    with pytest.raises(ValidationError):
        DefectRecord.model_validate(_defect_record_kwargs(**{"class": tag}))


# ---------------------------------------------------------------------------
# RowRef prefix-vs-category
# ---------------------------------------------------------------------------


def test_rowref_records_correct_prefix_valid() -> None:
    """records prefix is (fork_path, record_id)."""
    ref = RowRef(
        category="records",
        keys=(("fork_path", "trunk"), ("record_id", "r1")),
    )
    assert ref.category == "records"


def test_rowref_history_correct_prefix_valid() -> None:
    """history prefix is (fork_path, kind, record_id, property, sim_time)."""
    ref = RowRef(
        category="history",
        keys=(
            ("fork_path", "trunk"),
            ("kind", "patient"),
            ("record_id", "r1"),
            ("property", "prop__email"),
            ("sim_time", "1"),
        ),
    )
    assert ref.category == "history"


def test_rowref_membership_correct_prefix_valid() -> None:
    """membership prefix is (fork_path, record_id, joined_sim_time)."""
    ref = RowRef(
        category="membership",
        keys=(
            ("fork_path", "trunk"),
            ("record_id", "r1"),
            ("joined_sim_time", "1"),
        ),
    )
    assert ref.category == "membership"


def test_rowref_wrong_key_names_rejected() -> None:
    """Wrong key names for the declared category raise."""
    with pytest.raises(ValidationError):
        RowRef(category="records", keys=(("fork_path", "trunk"), ("bogus", "r1")))


def test_rowref_wrong_key_order_rejected() -> None:
    """Correct key names in the wrong order raise."""
    with pytest.raises(ValidationError):
        RowRef(category="records", keys=(("record_id", "r1"), ("fork_path", "trunk")))


def test_rowref_history_wrong_length_rejected() -> None:
    """A history RowRef missing keys raises."""
    with pytest.raises(ValidationError):
        RowRef(category="history", keys=(("fork_path", "trunk"), ("kind", "patient")))


def test_rowref_membership_records_prefix_rejected() -> None:
    """A membership category with the records prefix raises (category mismatch)."""
    with pytest.raises(ValidationError):
        RowRef(
            category="membership", keys=(("fork_path", "trunk"), ("record_id", "r1"))
        )


# ---------------------------------------------------------------------------
# `class` alias
# ---------------------------------------------------------------------------


def test_defect_record_dumps_class_key_by_alias() -> None:
    """model_dump(by_alias=True) emits `class`, not `defect_class`."""
    record = DefectRecord.model_validate(_defect_record_kwargs())
    dumped = record.model_dump(by_alias=True)
    assert "class" in dumped
    assert "defect_class" not in dumped


def test_defect_record_reads_back_via_class_key() -> None:
    """A record round-trips reading the `class` on-disk key."""
    record = DefectRecord.model_validate(_defect_record_kwargs(**{"class": "dup_row"}))
    assert record.defect_class == "dup_row"


def test_defect_record_reads_back_via_field_name() -> None:
    """A record also validates when given the field name `defect_class`
    (populate_by_name=True)."""
    kwargs = _defect_record_kwargs()
    del kwargs["class"]
    kwargs["defect_class"] = "dup_row"
    record = DefectRecord.model_validate(kwargs)
    assert record.defect_class == "dup_row"


def test_defect_record_rejects_unknown_field() -> None:
    """extra='forbid' still rejects an unrecognized key alongside `class`."""
    with pytest.raises(ValidationError):
        DefectRecord.model_validate(_defect_record_kwargs(bogus="nope"))


def test_generated_schema_keys_manifest_defect_on_class() -> None:
    """The generated JSON Schema keys the manifest defect on `class`."""
    schema = DefectManifest.model_json_schema(by_alias=True)
    properties = schema["$defs"]["ManifestDefect"]["properties"]
    assert "class" in properties
    assert "defect_class" not in properties


# ---------------------------------------------------------------------------
# DefectManifest.check_counts_match
# ---------------------------------------------------------------------------


def _manifest_defect(**overrides: object) -> ManifestDefect:
    kwargs = _defect_record_kwargs()
    kwargs.update(overrides)
    kwargs["defect_id"] = kwargs.pop("defect_id", "id-0")
    return ManifestDefect.model_validate(kwargs)


def _base_manifest_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "defect_manifest_version": 1,
        "source": DefectSource(sidecar_sha256="a" * 64, base_format_version=4),
        "config_fingerprint": "b" * 64,
        "code_version": "0.0.1",
        "counts": DefectCounts(by_class={"missing_value": 1}, by_impact={"C6": 1}),
        "defects": (_manifest_defect(),),
    }
    kwargs.update(overrides)
    return kwargs


def test_manifest_counts_match_valid() -> None:
    """A manifest whose counts equal the defects' aggregation is valid."""
    manifest = DefectManifest.model_validate(_base_manifest_kwargs())
    assert manifest.counts.by_class == {"missing_value": 1}


def test_manifest_stale_by_class_rejected() -> None:
    """A stale by_class count is rejected."""
    with pytest.raises(ValidationError):
        DefectManifest.model_validate(
            _base_manifest_kwargs(
                counts=DefectCounts(by_class={"missing_value": 2}, by_impact={"C6": 1})
            )
        )


def test_manifest_stale_by_impact_rejected() -> None:
    """A stale by_impact count is rejected."""
    with pytest.raises(ValidationError):
        DefectManifest.model_validate(
            _base_manifest_kwargs(
                counts=DefectCounts(by_class={"missing_value": 1}, by_impact={"C6": 2})
            )
        )


def test_manifest_multi_code_impact_adds_one_per_code() -> None:
    """A multi-code impact adds +1 to by_impact per code (sum >= len(defects))."""
    defect = _manifest_defect(impact=["C6", "C7"], defect_id="id-1")
    manifest = DefectManifest.model_validate(
        _base_manifest_kwargs(
            counts=DefectCounts(
                by_class={"missing_value": 1}, by_impact={"C6": 1, "C7": 1}
            ),
            defects=(defect,),
        )
    )
    assert sum(manifest.counts.by_impact.values()) >= len(manifest.defects)


# ---------------------------------------------------------------------------
# Schema drift guard
# ---------------------------------------------------------------------------


def test_schema_drift_guard() -> None:
    """The checked-in defect_manifest.schema.json equals a fresh regeneration
    from the models. If this fails, regenerate it: run

        uv run python -c "
        import json
        from fabulexa_export.corrupters.manifest import DefectManifest
        schema = DefectManifest.model_json_schema(by_alias=True)
        open('src/fabulexa_export/corrupters/defect_manifest.schema.json', 'w').write(
            json.dumps(schema, indent=2, sort_keys=True) + chr(10)
        )
        "
    """
    regenerated = (
        json.dumps(
            DefectManifest.model_json_schema(by_alias=True), indent=2, sort_keys=True
        )
        + "\n"
    )
    checked_in = _SCHEMA_PATH.read_text(encoding="utf-8")
    assert regenerated == checked_in, (
        "defect_manifest.schema.json is stale; regenerate it (see this test's "
        "docstring)"
    )
