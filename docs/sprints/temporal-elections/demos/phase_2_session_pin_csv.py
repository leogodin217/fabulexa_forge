#!/usr/bin/env python
"""
Demo: Session-zone pin + CSV temporal text forms
Sprint: temporal-elections
Phase: 2

`pin_session_timezone` pins an open emit's materialization session to the
resolved anchor's IANA zone; the CSV writer's `_format_value` grows pinned
per-type text forms for DATE / TIME / TIMESTAMPTZ / INTERVAL. Together these
guarantee CSV output is byte-identical regardless of the *host machine's*
zone — only the *anchor* zone governs the text form.

Shows:
  1. Opens a fixture emit carrying a sidecar `runtime` anchor.
  2. Resolves the effective anchor and pins the session to its zone.
  3. Materializes literals of the four new types through `query_arrow` and
     writes them via the CSV writer, printing the exact bytes.
  4. Re-runs the identical pipeline in a subprocess under a *different*
     process `TZ`, proving the CSV bytes are machine-independent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import duckdb

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.reader.emit import open_emit, pin_session_timezone
from fabulexa_forge.writers.csv import write_csv

_ANCHOR_ZONE = "America/New_York"
_ANCHOR_START_DATETIME = "2024-06-01T12:00:00+00:00"

_LITERALS_SQL = textwrap.dedent(
    """\
    SELECT
        DATE '2024-06-01' AS admission_date,
        TIME '14:30:00.500000' AS check_in_time,
        TIMESTAMPTZ '2024-06-01 14:30:00.500000-04:00' AS event_instant,
        INTERVAL '5400000000 microseconds' AS wait_duration
    """
)


def _fail(message: str) -> SystemExit:
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def build_fixture_emit(emit_dir: Path) -> None:
    """Write a minimal single-branch emit with a sidecar `runtime` anchor.

    Args:
        emit_dir: Directory to write base.json + run.duckdb into.
    """
    emit_dir.mkdir(parents=True, exist_ok=True)

    base_json = {
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
        "runtime": {
            "timezone": _ANCHOR_ZONE,
            "start_datetime": _ANCHOR_START_DATETIME,
        },
    }
    (emit_dir / "base.json").write_text(json.dumps(base_json), encoding="utf-8")

    conn = duckdb.connect(str(emit_dir / "run.duckdb"))
    conn.execute("CREATE TABLE firings (fork_path VARCHAR, sim_time BIGINT)")
    conn.close()


def run_pipeline(emit_dir: Path, out_dir: Path) -> bytes:
    """Open the fixture emit, pin the session to its resolved anchor zone,
    materialize the four literals, write CSV, and return the file's bytes.

    Args:
        emit_dir: A fixture emit built by `build_fixture_emit`.
        out_dir: Directory to write the CSV into.

    Returns:
        The written CSV file's raw bytes.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None, "the fixture sidecar always carries a runtime anchor"
        pin_session_timezone(emit, anchor)
        write_csv(emit, "literals", _LITERALS_SQL, out_dir)
    return (out_dir / "literals.csv").read_bytes()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        emit_dir = tmp_path / "emit"
        build_fixture_emit(emit_dir)

        print(f"1. Fixture emit's sidecar runtime anchor: zone={_ANCHOR_ZONE}")
        print()

        print("2-3. Pin the session, materialize the four literals, write CSV:")
        out_dir = tmp_path / "out_parent"
        parent_bytes = run_pipeline(emit_dir, out_dir)
        print(parent_bytes.decode("utf-8"))

        print("4. Re-running under a different process TZ (subprocess):")
        sub_out_dir = tmp_path / "out_subprocess"
        sub_out_dir.mkdir()
        script = (
            "import sys; sys.path.insert(0, %r);"
            "from phase_2_session_pin_csv import run_pipeline;"
            "from pathlib import Path;"
            "sys.stdout.buffer.write(run_pipeline(Path(%r), Path(%r)))"
        ) % (str(Path(__file__).parent), str(emit_dir), str(sub_out_dir))
        result = subprocess.run(
            [sys.executable, "-c", script],
            env={"TZ": "Asia/Tokyo", "PATH": os.environ.get("PATH", "")},
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise _fail(f"subprocess failed: {result.stderr.decode('utf-8')}")
        subprocess_bytes = result.stdout

        if subprocess_bytes != parent_bytes:
            raise _fail(
                "CSV bytes differ under a different process TZ:"
                f" parent={parent_bytes!r} subprocess={subprocess_bytes!r}"
            )
        print("  OK: byte-identical under TZ=Asia/Tokyo")
        print()

    print(
        "SUCCESS: pin_session_timezone + the CSV writer's pinned text forms make"
        " DATE/TIME/TIMESTAMPTZ/INTERVAL serialization a pure function of the"
        " resolved anchor zone — never the host machine's zone"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
