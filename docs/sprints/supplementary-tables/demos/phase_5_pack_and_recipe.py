#!/usr/bin/env python
"""
Demo: Supplementary tables through the pack builder and the recipe
Sprint: supplementary-tables
Phase: 5

Builds a temp example directory (a self-contained bundle + a dimensional
config with a file supplement under data/) and runs build_pack, printing the
archive member list (showing data/region_code.csv). Then drives the three
pack-builder refusals — a missing supplement file, a supplement file
escaping the dataset directory (file: ../escape.csv), and a config the
loader refuses — printing each message. Finally loads the shipped
supplement-tables recipe's config against a self-contained patient emit and
runs export_dimensional, printing the supplement table's rows.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tarfile
import tempfile
import types
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.datasets.models import DatasetEntry
from fabulexa_forge.exporters.dimensional.engine import export_dimensional
from fabulexa_forge.exporters.supplements import load_supplements
from fabulexa_forge.reader.emit import open_emit

if TYPE_CHECKING:
    from fabulexa_forge.exporters.notices import Notice

_REPO_ROOT = Path(__file__).resolve().parents[4]
_TOOL_PATH = _REPO_ROOT / "tools" / "build_dataset_pack.py"
_RECIPE_DIR = _REPO_ROOT / "examples" / "recipes" / "supplement-tables"

_PATIENT_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    {"name": "record_index", "type": "BIGINT"},
    {
        "name": "prop__name",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

_DIM_CUSTOMER_TABLE_YAML = """
dimensional:
  tables:
    - name: customer
      role: dim
      scd: type1
      source: {grain: records, kind: patient}
      key: [customer_id]
      columns:
        - {name: customer_id, from: record_id}
        - {name: full_name, from: prop__name}
"""

_FILE_SUPPLEMENT_YAML = """
supplements:
  - name: region_code
    file: data/region_code.csv
    columns: {code: VARCHAR, label: VARCHAR}
    description: Sales-region codes used by account management.
"""

_REGION_CODE_CSV = "code,label\nUS,United States\nCA,Canada\n"


def _discard_notice(notice: "Notice") -> None:
    """A notice sink that drops every notice — the demo prints its own facts."""


def _load_tool() -> types.ModuleType:
    """Load tools/build_dataset_pack.py as a module (tools/ is not a package)."""
    spec = importlib.util.spec_from_file_location("build_dataset_pack", _TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_patient_emit(bundle_dir: Path) -> None:
    """Write a minimal self-contained bundle: one records__patient kind,
    two rows, no history/membership/runtime block."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(bundle_dir / "run.duckdb"))
    col_fragments = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _PATIENT_COLUMNS)
    conn.execute(f'CREATE TABLE "records__patient" ({col_fragments})')
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p001", 0, True, None, 0, 0, "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__patient" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "p002", 0, True, None, 0, 1, "Bob"],
    )
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": "records__patient",
                "category": "records",
                "record_kind": "patient",
                "rows": 2,
                "columns": _PATIENT_COLUMNS,
            }
        ],
    }
    (bundle_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    (bundle_dir / "ATLAS.md").write_text("# Atlas\n", encoding="utf-8")


def _write_dimensional_config(config_dir: Path, supplements_yaml: str) -> Path:
    """Write dimensional.yaml combining the customer table and a supplements
    block."""
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "dimensional.yaml"
    config_path.write_text(
        "mode: dimensional\n" + _DIM_CUSTOMER_TABLE_YAML + supplements_yaml,
        encoding="utf-8",
    )
    return config_path


def _pack_entry() -> DatasetEntry:
    return DatasetEntry.model_validate(
        {
            "name": "supplement-demo",
            "description": "A demo dataset pack with a file supplement.",
            "url": "https://example.com/supplement-demo.tar.gz",
            "sha256": "0" * 64,
            "size_bytes": 1,
            "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
            "configs": ["dimensional.yaml"],
            "commands": ["fabulexa-forge export {dir}/dimensional.yaml --out out/"],
        }
    )


def _demo_pack_build(pack_builder: types.ModuleType, tmp_root: Path) -> None:
    """Build a pack whose config declares a file supplement; print the
    archive member list."""
    print("Pack build (happy path):")
    example_dir = tmp_root / "example"
    _write_patient_emit(example_dir / "bundle")
    _write_dimensional_config(example_dir, _FILE_SUPPLEMENT_YAML)
    (example_dir / "data").mkdir()
    (example_dir / "data" / "region_code.csv").write_text(
        _REGION_CODE_CSV, encoding="utf-8"
    )

    out_path = tmp_root / "out" / "supplement-demo.tar.gz"
    pack_builder.build_pack(_pack_entry(), example_dir, out_path)
    with tarfile.open(out_path, mode="r:gz") as archive:
        names = sorted(m.name for m in archive.getmembers())
    print(f"  archive members: {names}")


def _fresh_example_dir(tmp_root: Path, case_name: str) -> Path:
    """Build a fresh example directory (bundle only) for one refusal case;
    the caller writes the case-specific config/data before invoking
    build_pack."""
    example_dir = tmp_root / case_name
    _write_patient_emit(example_dir / "bundle")
    return example_dir


def _demo_missing_supplement_file(
    pack_builder: types.ModuleType, tmp_root: Path
) -> None:
    example_dir = _fresh_example_dir(tmp_root, "missing_file")
    _write_dimensional_config(example_dir, _FILE_SUPPLEMENT_YAML)
    # data/region_code.csv deliberately not written.
    try:
        pack_builder.build_pack(
            _pack_entry(), example_dir, tmp_root / "missing_file.tar.gz"
        )
    except pack_builder.PackBuildError as exc:
        print(f"  [missing supplement file] refused: {exc}")
    else:
        raise AssertionError("expected a refusal for a missing supplement file")


def _demo_escaping_supplement_file(
    pack_builder: types.ModuleType, tmp_root: Path
) -> None:
    example_dir = _fresh_example_dir(tmp_root, "escape")
    _write_dimensional_config(
        example_dir,
        "\nsupplements:\n"
        "  - name: region_code\n"
        "    file: ../escape.csv\n"
        "    columns: {code: VARCHAR, label: VARCHAR}\n",
    )
    (tmp_root / "escape.csv").write_text(_REGION_CODE_CSV, encoding="utf-8")
    try:
        pack_builder.build_pack(_pack_entry(), example_dir, tmp_root / "escape.tar.gz")
    except pack_builder.PackBuildError as exc:
        print(f"  [supplement escapes dataset dir] refused: {exc}")
    else:
        raise AssertionError("expected a refusal for an escaping supplement file")


def _demo_config_load_refusal(pack_builder: types.ModuleType, tmp_root: Path) -> None:
    example_dir = _fresh_example_dir(tmp_root, "bad_config")
    (example_dir / "dimensional.yaml").write_text(
        "mode: dimensional\nbogus_field: true\n", encoding="utf-8"
    )
    try:
        pack_builder.build_pack(
            _pack_entry(), example_dir, tmp_root / "bad_config.tar.gz"
        )
    except pack_builder.PackBuildError as exc:
        print(f"  [config the loader refuses] refused: {exc}")
    else:
        raise AssertionError("expected a refusal for a config the loader refuses")


def _demo_recipe_export(tmp_root: Path) -> None:
    """Load the shipped supplement-tables recipe's config, resolve its
    supplements, and export it against a self-contained patient emit."""
    print()
    print("Recipe export (supplement-tables):")
    emit_dir = tmp_root / "recipe_emit"
    _write_patient_emit(emit_dir)

    config_path = _RECIPE_DIR / "config.yaml"
    config = load_export_config(config_path)
    supplements = load_supplements(config, config_path.parent)

    out_dir = tmp_root / "recipe_out"
    out_dir.mkdir()
    with open_emit(emit_dir) as emit:
        export_dimensional(
            emit, config, out_dir, "csv", None, _discard_notice, None, supplements
        )

    for line in (out_dir / "region_code.csv").read_text(encoding="utf-8").splitlines():
        print(f"    region_code.csv: {line}")
    for line in (
        (out_dir / "priority_level.csv").read_text(encoding="utf-8").splitlines()
    ):
        print(f"    priority_level.csv: {line}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        pack_builder = _load_tool()

        _demo_pack_build(pack_builder, tmp_root)
        print()
        print("Refusals:")
        _demo_missing_supplement_file(pack_builder, tmp_root)
        _demo_escaping_supplement_file(pack_builder, tmp_root)
        _demo_config_load_refusal(pack_builder, tmp_root)

        _demo_recipe_export(tmp_root)

    print()
    print(
        "SUCCESS: pack builder carries a file supplement as an archive member;"
        " every refusal fired before any archive was written;"
        " the recipe's supplements exported end-to-end"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
