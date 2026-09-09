#!/usr/bin/env python
"""
Demo: Supplementary tables through the incremental drip
Sprint: supplementary-tables
Phase: 3

Builds a self-contained minimal emit (one records__actor kind, two rows) and
a dimensional config declaring one dim table, a sim_period_ns incremental
block, and one file supplement. Drives a --next CSV drip of three windows,
printing each window drop's listing to show the supplement re-delivered
whole every time — including the later windows, where the dim table carries
no new mutations but the supplement is still written in full; then a --next
DuckDB drip, printing the supplement table's rows after each window to show
it replaced, not duplicated. Then, continuing the CSV drip: edits the
supplement's
`description` (the next window still succeeds — presentation-only), renames
the supplement file with identical bytes (still succeeds — the sha256, not
the path, is the fingerprint's identity), and changes one CSV byte (the next
window raises IncrementalFingerprintMismatch). Finally, a fresh --next CSV
config whose supplement source sits at the path window 0's drop would land —
SupplementSourceIsOutput, before any write.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.errors import ExporterError, IncrementalFingerprintMismatch
from fabulexa_forge.exporters.supplements import load_supplements
from fabulexa_forge.incremental.driver import export_incremental_next
from fabulexa_forge.incremental.windows import derive_window
from fabulexa_forge.reader.emit import open_emit

if TYPE_CHECKING:
    from fabulexa_forge.config.models import ExportConfig
    from fabulexa_forge.exporters.notices import Notice
    from fabulexa_forge.reader.emit import Emit

_ACTOR_COLUMNS: list[dict[str, object]] = [
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

_SLICE_AT = 650
_PERIOD_NS = 100

_REGION_CODE_CSV = "code,label\nUS,United States\nCA,\n"
_REGION_CODE_CSV_EDITED = "code,label\nUS,United States\nCA,Canada\n"


def _discard_notice(notice: "Notice") -> None:
    """A notice sink that drops every notice — the demo prints its own facts."""


def _build_emit(emit_dir: Path) -> Path:
    """Build a minimal self-contained emit: one records__actor kind, two
    rows mutated at sim_time 0, a branch sliced at _SLICE_AT."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    col_fragments = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _ACTOR_COLUMNS)
    conn.execute(f'CREATE TABLE "records__actor" ({col_fragments})')
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a001", 0, True, None, 0, 0, "Alice"],
    )
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        ["trunk", "a002", 0, True, None, 0, 1, "Bob"],
    )
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "surface": "published",
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": _SLICE_AT}],
        "tables": [
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "rows": 2,
                "columns": _ACTOR_COLUMNS,
            }
        ],
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return emit_dir


def _config_yaml(file_name: str, description: str) -> str:
    """The dim_actor + incremental + one-file-supplement config, parameterized
    by the supplement's file name and description."""
    return f"""
mode: dimensional
dimensional:
  tables:
    - name: dim_actor
      role: dim
      scd: type1
      source: {{grain: records, kind: actor}}
      key: [id]
      columns:
        - {{name: id, from: record_id}}
        - {{name: name, from: prop__name}}
incremental:
  sim_period_ns: {_PERIOD_NS}
supplements:
  - name: region_code
    file: {file_name}
    columns: {{code: VARCHAR, label: VARCHAR}}
    description: {description}
"""


def _write_config(config_dir: Path, file_name: str, description: str) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(_config_yaml(file_name, description), encoding="utf-8")
    return config_path


def _next(
    emit: "Emit", config: "ExportConfig", out: Path, fmt: str, config_dir: Path
) -> None:
    """Run one --next window and print its label + row counts."""
    supplements = load_supplements(config, config_dir)
    outcome = export_incremental_next(
        emit, config, out, fmt, None, _discard_notice, None, supplements
    )
    assert outcome.status == "emitted"
    assert outcome.window is not None
    assert outcome.row_counts is not None
    print(f"  window '{outcome.window.label}': {dict(outcome.row_counts)}")


def _demo_csv_drip(emit: "Emit", tmp_root: Path) -> tuple[Path, Path]:
    """Three --next CSV windows; each drop's listing shows the supplement
    re-delivered whole. Returns (out_dir, config_dir) for the continuation
    demo."""
    print("CSV drip (--next):")
    config_dir = tmp_root / "csv_cfg"
    config_dir.mkdir(parents=True)
    (config_dir / "region_code.csv").write_text(_REGION_CODE_CSV, encoding="utf-8")
    config_path = _write_config(config_dir, "region_code.csv", "Sales-region codes.")
    config = load_export_config(config_path)
    out_dir = tmp_root / "csv_drip"

    for _ in range(3):
        _next(emit, config, out_dir, "csv", config_dir)
        window_dirs = sorted(p.name for p in out_dir.iterdir() if p.is_dir())
        latest = out_dir / window_dirs[-1]
        listing = sorted(p.name for p in latest.iterdir())
        region_text = (latest / "region_code.csv").read_text(encoding="utf-8")
        print(f"    drop '{latest.name}' contents: {listing}")
        print("    region_code.csv:\n      " + region_text.replace("\n", "\n      "))

    return out_dir, config_dir


def _demo_duckdb_drip(emit: "Emit", tmp_root: Path) -> None:
    """Two --next DuckDB windows; the supplement table is replaced, not
    duplicated, each time."""
    print("DuckDB drip (--next):")
    config_dir = tmp_root / "duckdb_cfg"
    config_dir.mkdir(parents=True)
    (config_dir / "region_code.csv").write_text(_REGION_CODE_CSV, encoding="utf-8")
    config_path = _write_config(config_dir, "region_code.csv", "Sales-region codes.")
    config = load_export_config(config_path)
    db_path = tmp_root / "duckdb_drip" / "warehouse.duckdb"
    db_path.parent.mkdir(parents=True)

    for _ in range(2):
        _next(emit, config, db_path, "duckdb", config_dir)
        conn = duckdb.connect(str(db_path), read_only=True)
        rows = conn.execute('SELECT * FROM "region_code" ORDER BY code').fetchall()
        conn.close()
        print(f"    region_code rows: {rows}")


def _demo_continuation(
    emit: "Emit", tmp_root: Path, out_dir: Path, config_dir: Path
) -> None:
    """Continue the CSV drip: a description edit and an identical-bytes
    rename both succeed; a content edit raises IncrementalFingerprintMismatch."""
    print("Continuing the CSV drip:")

    # description edit — presentation-only, excluded from the fingerprint
    config_path = _write_config(
        config_dir, "region_code.csv", "Sales-region codes (updated prose)."
    )
    config = load_export_config(config_path)
    _next(emit, config, out_dir, "csv", config_dir)
    print("    description edit: next window succeeded")

    # rename with identical bytes — the sha256, not the path, is the identity
    renamed = config_dir / "region_code_v2.csv"
    renamed.write_bytes((config_dir / "region_code.csv").read_bytes())
    (config_dir / "region_code.csv").unlink()
    config_path = _write_config(
        config_dir, "region_code_v2.csv", "Sales-region codes (updated prose)."
    )
    config = load_export_config(config_path)
    _next(emit, config, out_dir, "csv", config_dir)
    print("    identical-bytes rename: next window succeeded")

    # one changed byte — the sha256 changes, the fingerprint mismatches
    renamed.write_text(_REGION_CODE_CSV_EDITED, encoding="utf-8")
    try:
        _next(emit, config, out_dir, "csv", config_dir)
    except IncrementalFingerprintMismatch as exc:
        print(f"    changed byte: refused — {exc}")
    else:
        raise AssertionError("expected IncrementalFingerprintMismatch")


def _demo_source_is_output(emit: "Emit", tmp_root: Path) -> None:
    """A fresh --next CSV config whose supplement source sits exactly where
    window 0's drop would land — refused before any write."""
    print("Source-is-output under --next:")
    config_dir = tmp_root / "source_is_output_cfg"
    config_dir.mkdir(parents=True)
    config_path = _write_config(config_dir, "region_code.csv", "Sales-region codes.")
    config = load_export_config(config_path)
    assert config.incremental is not None
    window_zero = derive_window(0, config.incremental, None)

    out_dir = tmp_root / "source_is_output_out"
    drop_dir = out_dir / window_zero.label
    drop_dir.mkdir(parents=True)
    (drop_dir / "region_code.csv").write_text(_REGION_CODE_CSV, encoding="utf-8")

    # The supplement's declared file resolves to the very path window 0
    # would write — point the config at it directly via config_dir.
    supplement_config_dir = drop_dir
    supplements = load_supplements(config, supplement_config_dir)
    try:
        export_incremental_next(
            emit, config, out_dir, "csv", None, _discard_notice, None, supplements
        )
    except ExporterError as exc:
        remaining = sorted(p.name for p in out_dir.rglob("*"))
        print(f"    refused: {exc}")
        print(f"    output tree afterward: {remaining}")
    else:
        raise AssertionError("expected SupplementSourceIsOutput")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        emit_dir = _build_emit(tmp_root / "emit")

        with open_emit(emit_dir) as emit:
            out_dir, config_dir = _demo_csv_drip(emit, tmp_root)
            print()
            _demo_duckdb_drip(emit, tmp_root)
            print()
            _demo_continuation(emit, tmp_root, out_dir, config_dir)
            print()
            _demo_source_is_output(emit, tmp_root)

    print()
    print(
        "SUCCESS: the supplement was re-delivered whole every emitting window;"
        " description/path changes left the fingerprint unchanged; a content"
        " change and a source-is-output config were both refused"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
