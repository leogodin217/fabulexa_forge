#!/usr/bin/env python
"""
Demo: Pack builder — deterministic release archives
Sprint: dataset-distribution
Phase: 4

Synthesizes a minimal valid emit inline, lays out an example-shaped tree in
a temp dir (bundle/{run.duckdb,base.json,ATLAS.md} + a config at the example
root), builds the pack twice and shows byte-identical sha256, extracts and
re-opens the packed bundle through open_emit, then shows the
missing-bundle-file and missing-config refusals. tools/build_dataset_pack.py
is loaded via spec_from_file_location — it is a repo-side tool, not a
package module.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tarfile
import tempfile
import types
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.datasets.models import DatasetEntry
from fabulexa_forge.reader import open_emit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_TOOL_PATH = _REPO_ROOT / "tools" / "build_dataset_pack.py"


def load_pack_builder() -> types.ModuleType:
    """Load tools/build_dataset_pack.py as a module."""
    spec = importlib.util.spec_from_file_location("build_dataset_pack", _TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_minimal_bundle(bundle_dir: Path) -> None:
    """Synthesize a minimal valid emit (run.duckdb + base.json) plus ATLAS.md."""
    bundle_dir.mkdir(parents=True)
    duckdb.connect(str(bundle_dir / "run.duckdb")).close()
    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 0}],
        "tables": [
            {
                "name": "firings",
                "category": "fixed",
                "columns": [
                    {"name": "fork_path", "type": "VARCHAR"},
                    {"name": "sim_time", "type": "BIGINT"},
                ],
                "rows": 0,
            }
        ],
    }
    (bundle_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    (bundle_dir / "ATLAS.md").write_text("# Atlas\n\nDemo emit.\n", encoding="utf-8")


def build_example_dir(example_dir: Path) -> None:
    """Lay out an example-shaped tree: bundle triple + one config."""
    build_minimal_bundle(example_dir / "bundle")
    (example_dir / "dimensional.yaml").write_text("grain: event\n", encoding="utf-8")


def build_demo_entry() -> DatasetEntry:
    """A well-formed entry whose stamped fields are deliberately garbage —
    build_pack ignores and recomputes them."""
    return DatasetEntry.model_validate(
        {
            "name": "demo-pack",
            "description": "A demo dataset pack.",
            "url": "https://example.com/demo-pack.tar.gz",
            "sha256": "0" * 64,
            "size_bytes": 1,
            "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
            "configs": ["dimensional.yaml"],
            "commands": ["fabulexa-forge export {dir}/dimensional.yaml --out out/"],
        }
    )


def demo_deterministic_build_and_reopen(
    pack_builder: types.ModuleType, tmp_root: Path
) -> None:
    """Build the pack twice (byte-identical sha256), then extract and re-open
    the packed bundle through open_emit."""
    example_dir = tmp_root / "example"
    build_example_dir(example_dir)
    entry = build_demo_entry()

    first_path = tmp_root / "first" / "demo-pack.tar.gz"
    second_path = tmp_root / "second" / "demo-pack.tar.gz"
    stamp_one = pack_builder.build_pack(entry, example_dir, first_path)
    stamp_two = pack_builder.build_pack(entry, example_dir, second_path)
    assert stamp_one.sha256 == stamp_two.sha256, "rebuild produced different bytes"

    print("--- deterministic build ---")
    print(pack_builder.render_stamp_fragment(stamp_one))

    extract_dir = tmp_root / "extracted"
    with tarfile.open(first_path, mode="r:gz") as archive:
        archive.extractall(extract_dir, filter="data")

    with open_emit(extract_dir / "bundle") as emit:
        reopened_version = emit.sidecar.base_format_version
    assert reopened_version == stamp_one.base_format_version
    print(f"re-opened extracted bundle: base_format_version={reopened_version}")


def demo_missing_bundle_file_refusal(
    pack_builder: types.ModuleType, tmp_root: Path
) -> None:
    """A bundle triple missing run.duckdb refuses, naming the file."""
    example_dir = tmp_root / "missing-bundle-file"
    build_example_dir(example_dir)
    (example_dir / "bundle" / "run.duckdb").unlink()

    try:
        pack_builder.build_pack(
            build_demo_entry(), example_dir, tmp_root / "unused.tar.gz"
        )
    except pack_builder.PackBuildError as exc:
        print("--- missing bundle file refusal ---")
        print(f"refused: {exc}")
    else:
        raise AssertionError("expected PackBuildError for a missing run.duckdb")


def demo_missing_config_refusal(pack_builder: types.ModuleType, tmp_root: Path) -> None:
    """A configs entry absent from the example directory refuses, naming it."""
    example_dir = tmp_root / "missing-config"
    build_example_dir(example_dir)
    (example_dir / "dimensional.yaml").unlink()

    try:
        pack_builder.build_pack(
            build_demo_entry(), example_dir, tmp_root / "unused.tar.gz"
        )
    except pack_builder.PackBuildError as exc:
        print("--- missing configs file refusal ---")
        print(f"refused: {exc}")
    else:
        raise AssertionError("expected PackBuildError for a missing config file")


def main() -> int:
    pack_builder = load_pack_builder()

    with tempfile.TemporaryDirectory() as tmp_root_str:
        tmp_root = Path(tmp_root_str)
        demo_deterministic_build_and_reopen(pack_builder, tmp_root)
        demo_missing_bundle_file_refusal(pack_builder, tmp_root)
        demo_missing_config_refusal(pack_builder, tmp_root)

    print(
        "SUCCESS: build_pack produces a deterministic, re-openable archive and "
        "refuses an incomplete bundle or example directory, naming the missing file"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
