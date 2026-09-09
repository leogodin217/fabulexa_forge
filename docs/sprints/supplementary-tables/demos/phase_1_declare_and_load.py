#!/usr/bin/env python
"""
Demo: Declaring and loading supplementary tables (file + inline)
Sprint: supplementary-tables
Phase: 1

Writes a config with one file supplement (a UTF-8-BOM CSV with an empty
cell and a quoted newline) and one inline supplement into a temp dir, loads
it, and prints each ResolvedSupplement's path / sha256 / text rows. Then
drives one refusal of each kind and prints the message: unknown type, both
sources, unquoted `yes`, duplicate name, collision with a declared table,
header mismatch, ragged row, missing file, and `supplements` under
`mode: source`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.errors import ExporterError
from fabulexa_forge.exporters.supplements import ResolvedSupplement, load_supplements

if TYPE_CHECKING:
    from collections.abc import Callable

_HAPPY_CONFIG = """
mode: dimensional
dimensional:
  tables:
    - name: dim_actor
      role: dim
      scd: type1
      source: {grain: records, kind: actor}
      key: [id]
      columns:
        - {name: id, from: record_id}
supplements:
  - name: region_code
    file: region_code.csv
    columns: {code: VARCHAR, label: VARCHAR}
    description: Sales-region codes used by account management.
  - name: rate_tier
    columns: {tier: VARCHAR, rate: "DECIMAL(5,2)"}
    rows:
      - {tier: standard, rate: 1.5}
      - {tier: premium, rate: 2.25}
"""

# UTF-8-BOM, CRLF row terminators, a quoted newline inside a cell, and an
# empty trailing cell (-> None).
_REGION_CODE_CSV_BYTES = b'\xef\xbb\xbfcode,label\r\nUS,"United\nStates"\r\nCA,\r\n'

_FILE_SUPPLEMENT_CONFIG = """
mode: dimensional
dimensional:
  tables:
    - name: dim_actor
      role: dim
      source: {grain: records, kind: actor}
      key: [id]
      columns: [{name: id, from: record_id}]
supplements:
  - name: region_code
    file: region_code.csv
    columns: {code: VARCHAR, label: VARCHAR}
"""

_UNKNOWN_TYPE_CONFIG = """
mode: dimensional
dimensional:
  tables:
    - name: dim_actor
      role: dim
      source: {grain: records, kind: actor}
      key: [id]
      columns: [{name: id, from: record_id}]
supplements:
  - name: region_code
    columns: {code: INTERVAL}
    rows: [{code: "1"}]
"""

_BOTH_SOURCES_CONFIG = """
mode: dimensional
dimensional:
  tables:
    - name: dim_actor
      role: dim
      source: {grain: records, kind: actor}
      key: [id]
      columns: [{name: id, from: record_id}]
supplements:
  - name: region_code
    file: region_code.csv
    columns: {code: VARCHAR}
    rows: [{code: US}]
"""

_UNQUOTED_YES_CONFIG = """
mode: dimensional
dimensional:
  tables:
    - name: dim_actor
      role: dim
      source: {grain: records, kind: actor}
      key: [id]
      columns: [{name: id, from: record_id}]
supplements:
  - name: region_code
    columns: {code: VARCHAR, active: BOOLEAN}
    rows:
      - {code: US, active: yes}
"""

_DUPLICATE_NAME_CONFIG = """
mode: dimensional
dimensional:
  tables:
    - name: dim_actor
      role: dim
      source: {grain: records, kind: actor}
      key: [id]
      columns: [{name: id, from: record_id}]
supplements:
  - name: region_code
    columns: {code: VARCHAR}
    rows: [{code: US}]
  - name: region_code
    columns: {code: VARCHAR}
    rows: [{code: CA}]
"""

_COLLISION_CONFIG = """
mode: dimensional
dimensional:
  tables:
    - name: dim_actor
      role: dim
      source: {grain: records, kind: actor}
      key: [id]
      columns: [{name: id, from: record_id}]
supplements:
  - name: dim_actor
    columns: {code: VARCHAR}
    rows: [{code: US}]
"""

_SOURCE_MODE_CONFIG = """
mode: source
source:
  tables:
    - {name: actors, kind: actor}
supplements:
  - name: region_code
    columns: {code: VARCHAR}
    rows: [{code: US}]
"""


def _print_resolved(resolved: ResolvedSupplement) -> None:
    """Print one ResolvedSupplement's provenance and text rows."""
    print(f"  supplement '{resolved.decl.name}':")
    print(f"    path:   {resolved.path}")
    print(f"    sha256: {resolved.sha256}")
    print(f"    rows:   {resolved.rows}")


def _demo_happy_path(tmp_root: Path) -> None:
    """Load a config with one file supplement and one inline supplement;
    print each ResolvedSupplement."""
    config_dir = tmp_root / "happy"
    config_dir.mkdir()
    (config_dir / "region_code.csv").write_bytes(_REGION_CODE_CSV_BYTES)
    config_path = config_dir / "config.yaml"
    config_path.write_text(_HAPPY_CONFIG, encoding="utf-8")

    config = load_export_config(config_path)
    resolved = load_supplements(config, config_dir)

    print("Loaded supplements:")
    for one in resolved:
        _print_resolved(one)


def _demo_refusal(label: str, action: "Callable[[], None]") -> None:
    """Run `action`, expecting it to raise an ExporterError; print the
    message. Fails loudly (AssertionError) if it does not raise."""
    try:
        action()
    except ExporterError as exc:
        print(f"  [{label}] refused: {exc}")
    else:
        raise AssertionError(f"expected a refusal for {label!r}, none raised")


def _load_config_text(tmp_root: Path, case_name: str, text: str) -> None:
    """Write `text` as a config.yaml in its own subdirectory and load it —
    the config-level refusals (parse-time)."""
    case_dir = tmp_root / case_name
    case_dir.mkdir()
    config_path = case_dir / "config.yaml"
    config_path.write_text(text, encoding="utf-8")
    load_export_config(config_path)


def _header_mismatch_refusal(tmp_root: Path) -> None:
    """A valid config whose CSV header does not match the declaration."""
    case_dir = tmp_root / "header_mismatch"
    case_dir.mkdir()
    (case_dir / "region_code.csv").write_text("code,name\nUS,x\n", encoding="utf-8")
    config_path = case_dir / "config.yaml"
    config_path.write_text(_FILE_SUPPLEMENT_CONFIG, encoding="utf-8")
    config = load_export_config(config_path)
    load_supplements(config, case_dir)


def _ragged_row_refusal(tmp_root: Path) -> None:
    """A valid config whose CSV carries a data row with the wrong field
    count."""
    case_dir = tmp_root / "ragged_row"
    case_dir.mkdir()
    (case_dir / "region_code.csv").write_text(
        "code,label\nUS,United States\nCA\n", encoding="utf-8"
    )
    config_path = case_dir / "config.yaml"
    config_path.write_text(_FILE_SUPPLEMENT_CONFIG, encoding="utf-8")
    config = load_export_config(config_path)
    load_supplements(config, case_dir)


def _missing_file_refusal(tmp_root: Path) -> None:
    """A valid config whose declared CSV file does not exist."""
    case_dir = tmp_root / "missing_file"
    case_dir.mkdir()
    config_path = case_dir / "config.yaml"
    config_path.write_text(_FILE_SUPPLEMENT_CONFIG, encoding="utf-8")
    config = load_export_config(config_path)
    load_supplements(config, case_dir)


def _demo_refusals(tmp_root: Path) -> None:
    """Drive one refusal of each kind and print its message."""
    print("Refusals:")
    _demo_refusal(
        "unknown type",
        lambda: _load_config_text(tmp_root, "unknown_type", _UNKNOWN_TYPE_CONFIG),
    )
    _demo_refusal(
        "both sources",
        lambda: _load_config_text(tmp_root, "both_sources", _BOTH_SOURCES_CONFIG),
    )
    _demo_refusal(
        "unquoted yes",
        lambda: _load_config_text(tmp_root, "unquoted_yes", _UNQUOTED_YES_CONFIG),
    )
    _demo_refusal(
        "duplicate name",
        lambda: _load_config_text(tmp_root, "duplicate_name", _DUPLICATE_NAME_CONFIG),
    )
    _demo_refusal(
        "collision with declared table",
        lambda: _load_config_text(tmp_root, "collision", _COLLISION_CONFIG),
    )
    _demo_refusal("header mismatch", lambda: _header_mismatch_refusal(tmp_root))
    _demo_refusal("ragged row", lambda: _ragged_row_refusal(tmp_root))
    _demo_refusal("missing file", lambda: _missing_file_refusal(tmp_root))
    _demo_refusal(
        "supplements under mode: source",
        lambda: _load_config_text(tmp_root, "source_mode", _SOURCE_MODE_CONFIG),
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        _demo_happy_path(tmp_root)
        print()
        _demo_refusals(tmp_root)

    print()
    print("SUCCESS: declared and loaded supplements; every refusal fired as documented")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
