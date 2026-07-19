#!/usr/bin/env python
"""
Demo: The notice channel + notice_sink threading (dimensional / incremental / CLI)

Sprint: slice-only-policy
Phase: 1

Builds a minimal standalone emit (run.duckdb + base.json) with an `entity`
kind whose `prop__entity_type` discriminator has observed values
{'consultant', 'nurse'}. An export config's records `filter` names an
unobserved value ('admin') — the (unchanged) DiscriminatorValueObserved
check now emits a 'discriminator-value-unobserved' Notice through the new
channel instead of a Python warning.

Demonstrates, via the CLI `export` verb:
  - one `notice: ...` line on stderr; data on stdout/disk unchanged; exit 0
  - running the same export twice produces a byte-identical notice sequence
  - a `--next` drip re-emits its compile's notices on every invocation
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.cli import cmd_export

_RECORDS_COLUMNS: list[dict[str, object]] = [
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
    {
        "name": "prop__entity_type",
        "type": "VARCHAR",
        "history_tracked": False,
        "temporal_class": "constant",
    },
]

# The export config: an unobserved discriminator filter value ('admin') on a
# records-grain dim, plus an incremental block so the demo can also drip.
EXPORT_CONFIG_YAML = """
mode: dimensional
dimensional:
  tables:
    - name: dim_entity
      role: dim
      scd: type1
      source:
        grain: records
        kind: entity
        filter:
          prop__entity_type: admin
      key: [id]
      columns:
        - name: id
          from: record_id
        - name: name
          from: prop__name
incremental:
  sim_period_ns: 100
"""


def _build_emit(emit_dir: Path) -> None:
    """Write a minimal run.duckdb + base.json emit into emit_dir."""
    emit_dir.mkdir(parents=True, exist_ok=True)
    db_path = emit_dir / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    col_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in _RECORDS_COLUMNS)
    conn.execute(f'CREATE TABLE "records__entity" ({col_ddl})')
    for record_index, (entity_id, name, mutation_time) in enumerate(
        [("e001", "Alice", 10), ("e002", "Bob", 110), ("e003", "Carol", 210)]
    ):
        conn.execute(
            'INSERT INTO "records__entity" VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)',
            [
                "trunk",
                entity_id,
                mutation_time,
                True,
                mutation_time,
                record_index,
                name,
                "consultant",
            ],
        )
    conn.close()

    sidecar = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 250}],
        "tables": [
            {
                "name": "records__entity",
                "category": "records",
                "columns": _RECORDS_COLUMNS,
                "rows": 3,
                "record_kind": "entity",
            },
        ],
        "enum_domains": {"entity": {"entity_type": ["consultant", "nurse"]}},
    }
    (emit_dir / "base.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _run_export(
    emit_dir: Path, config_path: Path, out_dir: Path
) -> tuple[int, str, str]:
    """Run cmd_export, capturing (exit_code, stdout, stderr)."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        exit_code = cmd_export(emit_dir, config_path, out_dir, "csv")
    return exit_code, out_buf.getvalue(), err_buf.getvalue()


def _run_export_next(
    emit_dir: Path, config_path: Path, out_db: Path
) -> tuple[int, str, str]:
    """Run cmd_export --next, capturing (exit_code, stdout, stderr)."""
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        exit_code = cmd_export(
            emit_dir, config_path, out_db, "duckdb", next_window=True
        )
    return exit_code, out_buf.getvalue(), err_buf.getvalue()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "emit"
        _build_emit(emit_dir)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(EXPORT_CONFIG_YAML, encoding="utf-8")

        # --- One full export: notice on stderr, data on stdout/disk, exit 0 ---
        out_dir_1 = tmp_path / "out1"
        out_dir_1.mkdir()
        exit_code, stdout, stderr = _run_export(emit_dir, config_path, out_dir_1)
        if exit_code != 0:
            print(f"FAIL: expected exit 0, got {exit_code}", file=sys.stderr)
            return 1
        if stderr != (
            "notice: discriminator value 'admin' not observed for"
            " 'entity.prop__entity_type'; table will be empty\n"
        ):
            print(f"FAIL: unexpected stderr: {stderr!r}", file=sys.stderr)
            return 1
        if "dim_entity: 0 rows" not in stdout:
            print(f"FAIL: unexpected stdout: {stdout!r}", file=sys.stderr)
            return 1
        if not (out_dir_1 / "dim_entity.csv").exists():
            print("FAIL: dim_entity.csv not written", file=sys.stderr)
            return 1

        # --- Run again: byte-identical notice sequence ---
        out_dir_2 = tmp_path / "out2"
        out_dir_2.mkdir()
        _, _, stderr_2 = _run_export(emit_dir, config_path, out_dir_2)
        if stderr_2 != stderr:
            print(
                "FAIL: notice sequence differs between identical runs", file=sys.stderr
            )
            return 1

        # --- --next drip: re-emits its compile's notices each invocation ---
        out_db = tmp_path / "wh.duckdb"
        exit_code, _, drip_stderr_1 = _run_export_next(emit_dir, config_path, out_db)
        if exit_code != 0 or drip_stderr_1 != stderr:
            print(
                "FAIL: first --next drip did not emit the expected notice",
                file=sys.stderr,
            )
            return 1

        exit_code, _, drip_stderr_2 = _run_export_next(emit_dir, config_path, out_db)
        if exit_code != 0 or drip_stderr_2 != stderr:
            print(
                "FAIL: second --next drip did not re-emit the notice", file=sys.stderr
            )
            return 1

        print("SUCCESS: notice channel threaded through CLI export and --next drip")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
