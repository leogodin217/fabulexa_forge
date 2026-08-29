#!/usr/bin/env python
"""
Demo: The companion dictionary consults an export config's author
descriptions first, re-voicing a carried column's prose (or giving a
computed column a description-only doc) without touching a single rendered
data value; the incremental fingerprint stays unaffected by a description-only
config change.

Sprint: desc-override
Phase: 3

Synthesizes a single-kind emit (`records__member`, one `prop__tier`
property), then runs a full base-mode export twice from otherwise-identical
configs -- one bare `rename`, one adding a `descriptions` override on the
same column -- and shows: the README column line and the manifest's
per-column `description` both switch to the authored prose identically; the
written `member.csv` dataset is byte-identical across the two runs (the
override never touches a rendered value); and `compute_fingerprint` is
unchanged between the two configs, though it does change on an unrelated
`rename` edit.
"""

from __future__ import annotations

import filecmp
import json
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.exporters.base.engine import build_base_query_specs
from fabulexa_forge.exporters.companion.artifacts import write_companion_artifacts
from fabulexa_forge.exporters.query_spec import write_query_specs
from fabulexa_forge.incremental.fingerprint import compute_fingerprint
from fabulexa_forge.reader.emit import compute_sidecar_sha256, open_emit

_AUTHORED_DESCRIPTION = "Loyalty tier assigned at signup; author-supplied prose."

_BASE_CONFIG_WITHOUT_OVERRIDE = """
mode: base
base:
  rename:
    - table: records__member
      columns:
        prop__tier: loyalty_tier
"""

_BASE_CONFIG_WITH_OVERRIDE = f"""
mode: base
base:
  rename:
    - table: records__member
      columns:
        prop__tier: loyalty_tier
      descriptions:
        prop__tier: "{_AUTHORED_DESCRIPTION}"
"""

_BASE_CONFIG_RENAMED_DIFFERENTLY = """
mode: base
base:
  rename:
    - table: records__member
      columns:
        prop__tier: tier_label
"""

_MEMBER_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__tier",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_HISTORY_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "kind", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "property", "type": "VARCHAR"},
    {"name": "sim_time", "type": "BIGINT"},
    {"name": "value", "type": "VARCHAR"},
]


def _write_config(tmp_dir: Path, name: str, text: str) -> Path:
    """Write one example config's YAML to `tmp_dir/name` and return its path."""
    path = tmp_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def _build_emit(emit_dir: Path) -> None:
    """Write a minimal single-kind emit: records__member plus an empty history."""
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    ddl = ", ".join(f'"{col["name"]}" {col["type"]}' for col in _MEMBER_COLUMNS)
    conn.execute(f'CREATE TABLE "records__member" ({ddl})')
    conn.execute(
        'INSERT INTO "records__member" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "m1", 0, True, 0, 0, "gold"],
    )
    history_ddl = ", ".join(
        f'"{col["name"]}" {col["type"]}' for col in _HISTORY_COLUMNS
    )
    conn.execute(f'CREATE TABLE "history" ({history_ddl})')
    conn.close()

    sidecar: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": "records__member",
                "category": "records",
                "record_kind": "member",
                "rows": 1,
                "columns": _MEMBER_COLUMNS,
            },
            {
                "name": "history",
                "category": "fixed",
                "rows": 0,
                "columns": _HISTORY_COLUMNS,
            },
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _run_export(tmp_dir: Path, emit_dir: Path, config_text: str, run_name: str) -> Path:
    """Compile + write one full base-mode export; return its output directory."""
    config = load_export_config(_write_config(tmp_dir, f"{run_name}.yaml", config_text))
    out_dir = tmp_dir / run_name
    out_dir.mkdir()
    with open_emit(emit_dir) as emit:
        specs = build_base_query_specs(emit, config, None, None, lambda _n: None)
        report = write_query_specs(emit, specs, out_dir, "csv")
        write_companion_artifacts(
            emit, config, "csv", None, report, None, out_dir, None
        )
    return out_dir


def _loyalty_tier_readme_line(readme_text: str) -> str:
    """The `loyalty_tier` column-inventory line from a rendered README."""
    for line in readme_text.splitlines():
        if line.startswith("- `loyalty_tier`"):
            return line
    raise AssertionError(f"no loyalty_tier column line in:\n{readme_text}")


def _loyalty_tier_manifest_description(manifest_bytes: bytes) -> str | None:
    """The `loyalty_tier` column's manifest `description` field."""
    document = json.loads(manifest_bytes)
    for column in document["tables"][0]["columns"]:
        if column["name"] == "loyalty_tier":
            description = column["description"]
            assert description is None or isinstance(description, str)
            return description
    raise AssertionError("no loyalty_tier column in manifest")


def _demo_dictionary_and_dataset_parity(tmp_dir: Path, emit_dir: Path) -> None:
    """Run with/without the override; compare companion artifacts + dataset."""
    without_dir = _run_export(
        tmp_dir, emit_dir, _BASE_CONFIG_WITHOUT_OVERRIDE, "without_override"
    )
    with_dir = _run_export(
        tmp_dir, emit_dir, _BASE_CONFIG_WITH_OVERRIDE, "with_override"
    )

    without_line = _loyalty_tier_readme_line(
        (without_dir / "base-readme.md").read_text(encoding="utf-8")
    )
    with_line = _loyalty_tier_readme_line(
        (with_dir / "base-readme.md").read_text(encoding="utf-8")
    )
    print(f"README without override: {without_line!r}")
    print(f"README with override:    {with_line!r}")
    assert without_line == "- `loyalty_tier` (VARCHAR)"
    assert with_line == f"- `loyalty_tier` (VARCHAR): {_AUTHORED_DESCRIPTION}"

    without_description = _loyalty_tier_manifest_description(
        (without_dir / "base-manifest.json").read_bytes()
    )
    with_description = _loyalty_tier_manifest_description(
        (with_dir / "base-manifest.json").read_bytes()
    )
    print(f"manifest description without override: {without_description!r}")
    print(f"manifest description with override:    {with_description!r}")
    assert without_description is None
    assert with_description == _AUTHORED_DESCRIPTION
    assert with_description in with_line

    datasets_identical = filecmp.cmp(
        without_dir / "member.csv", with_dir / "member.csv", shallow=False
    )
    print(f"member.csv byte-identical across both runs: {datasets_identical}")
    assert datasets_identical


def _demo_fingerprint_exclusion(tmp_dir: Path, emit_dir: Path) -> None:
    """The fingerprint ignores the description but still reacts to a rename."""
    without_config = load_export_config(
        _write_config(tmp_dir, "fp_without.yaml", _BASE_CONFIG_WITHOUT_OVERRIDE)
    )
    with_config = load_export_config(
        _write_config(tmp_dir, "fp_with.yaml", _BASE_CONFIG_WITH_OVERRIDE)
    )
    renamed_config = load_export_config(
        _write_config(tmp_dir, "fp_renamed.yaml", _BASE_CONFIG_RENAMED_DIFFERENTLY)
    )
    with open_emit(emit_dir) as emit:
        sidecar_sha256 = compute_sidecar_sha256(emit)

    fp_without = compute_fingerprint(
        without_config, None, sidecar_sha256, "trunk", "csv", "0.0.0"
    )
    fp_with = compute_fingerprint(
        with_config, None, sidecar_sha256, "trunk", "csv", "0.0.0"
    )
    fp_renamed = compute_fingerprint(
        renamed_config, None, sidecar_sha256, "trunk", "csv", "0.0.0"
    )
    print(f"fingerprint without description: {fp_without}")
    print(f"fingerprint with description:    {fp_with}")
    print(f"fingerprint with unrelated rename edit: {fp_renamed}")
    assert fp_without == fp_with
    assert fp_without != fp_renamed


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        emit_dir = tmp_dir / "emit"
        emit_dir.mkdir()
        _build_emit(emit_dir)

        _demo_dictionary_and_dataset_parity(tmp_dir, emit_dir)
        _demo_fingerprint_exclusion(tmp_dir, emit_dir)

    print(
        "SUCCESS: the author description re-voices the README/manifest"
        " identically without touching the written dataset, and the"
        " fingerprint ignores it while still reacting to other config changes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
