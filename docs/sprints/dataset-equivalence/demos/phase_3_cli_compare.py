#!/usr/bin/env python
"""
Demo: Renderers and the CLI verb — `fabulexa-forge compare`
Sprint: dataset-equivalence
Phase: 3

Phase 3 wires Phase 2's `compare_datasets` engine into the CLI's `compare`
verb, with `render_comparison_text` / `render_comparison_json` and the
doc's exit-code contract (0 equal, 1 not equal, 2 input error).

Builds the Phase-2-style fixtures (one small `people` table, an equal actual
DuckDB copy, and an unequal one), then invokes `fabulexa_forge.cli.main`
directly, capturing stdout/stderr, for:
  (a) An equal pair -> exit 0, text report on stdout.
  (b) An unequal pair -> exit 1, discrepancy report on stdout.
  (c) `--format json` -> exit 0, byte-stable JSON on stdout.
  (d) A bad input (missing expected file) -> exit 2, message on stderr.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

import duckdb

from fabulexa_forge.cli import main as cli_main


def _fail(message: str) -> SystemExit:
    print(f"FAIL: {message}", file=sys.stderr)
    return SystemExit(1)


def _build_people(path: Path, rows: str) -> None:
    """A one-table `people(id BIGINT, name VARCHAR)` DuckDB file."""
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE people (id BIGINT, name VARCHAR)")
    con.execute(f"INSERT INTO people VALUES {rows}")
    con.close()


def _run_cli(args: list[str]) -> tuple[int, str, str]:
    """Invoke `fabulexa_forge.cli.main`, capturing (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        exit_code = cli_main(args)
    return exit_code, out.getvalue(), err.getvalue()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        expected_path = root / "expected.duckdb"
        _build_people(expected_path, "(1, 'Ada'), (2, 'Grace')")

        print("(a) Equal pair:")
        equal_path = root / "actual_equal.duckdb"
        _build_people(equal_path, "(2, 'Grace'), (1, 'Ada')")
        exit_a, out_a, err_a = _run_cli(
            ["compare", str(expected_path), str(equal_path)]
        )
        print(out_a, end="")
        if exit_a != 0:
            raise _fail(f"expected exit 0 for the equal pair, got {exit_a}")
        if not out_a.startswith("EQUAL"):
            raise _fail(f"expected an EQUAL report, got {out_a!r}")
        if err_a:
            raise _fail(f"expected empty stderr for the equal pair, got {err_a!r}")
        print()

        print("(b) Unequal pair:")
        unequal_path = root / "actual_unequal.duckdb"
        _build_people(unequal_path, "(1, 'Ada')")
        exit_b, out_b, _ = _run_cli(["compare", str(expected_path), str(unequal_path)])
        print(out_b, end="")
        if exit_b != 1:
            raise _fail(f"expected exit 1 for the unequal pair, got {exit_b}")
        if not out_b.startswith("NOT EQUAL"):
            raise _fail(f"expected a NOT EQUAL report, got {out_b!r}")
        print()

        print("(c) --format json:")
        exit_c, out_c, _ = _run_cli(
            [
                "compare",
                str(expected_path),
                str(equal_path),
                "--format",
                "json",
            ]
        )
        print(out_c, end="")
        if exit_c != 0:
            raise _fail(f"expected exit 0 for the json format, got {exit_c}")
        parsed = json.loads(out_c)
        if parsed["equal"] is not True:
            raise _fail(f"expected equal=true in the JSON report, got {parsed}")
        print()

        print("(d) Bad input (missing expected file):")
        missing_path = root / "does-not-exist.duckdb"
        exit_d, out_d, err_d = _run_cli(["compare", str(missing_path), str(equal_path)])
        print(f"  stderr: {err_d.strip()}")
        if exit_d != 2:
            raise _fail(f"expected exit 2 for a bad input, got {exit_d}")
        if out_d:
            raise _fail(f"expected no report on stdout for a bad input, got {out_d!r}")
        if "ERROR" not in err_d:
            raise _fail(f"expected an ERROR message on stderr, got {err_d!r}")
        print()

    print(
        "SUCCESS: `fabulexa-forge compare` renders the text/json report to "
        "stdout and honors the 0/1/2 exit-code contract, routing input "
        "errors to stderr"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
