#!/usr/bin/env python
"""
Demo: C16 domain closure — the check and the corrupter impact oracle
Sprint: base-format-v13
Phase: 2

Builds the spanning fixture (its enum-domained `prop__status` carries only
declared values), runs `validate` (C16 passes), corrupts it with a
`mutate_cells` out-of-domain mutation on `prop__status`, runs `validate` on
the corrupted emit (C16 fails, naming the value), then loads `defects.json`
and asserts the defect declares C16 and the corrupted emit's failing-check
set is contained in the manifest's impact union. Exits non-zero on any miss.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# The fixture builders live in the test tree; make them importable standalone.
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tests"))

from reader._fixtures_build import build_spanning  # noqa: E402

from fabulexa_forge.config.models import (  # noqa: E402
    Amount,
    CorruptConfig,
    MutateCells,
    MutationOutOfDomain,
    Target,
)
from fabulexa_forge.corrupters.engine import corrupt_emit  # noqa: E402
from fabulexa_forge.reader import conformance  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402

_CONFIG = CorruptConfig(
    seed=7,
    operations=[
        MutateCells(
            kind="mutate_cells",
            name="status_out_of_domain",
            target=Target(table="records__actor", columns=["prop__status"]),
            amount=Amount(rate=1.0),
            mutation=MutationOutOfDomain(kind="out_of_domain"),
        )
    ],
)


def _failing_checks(report: conformance.ConformanceReport) -> set[str]:
    return {r.check for r in report.results if not r.passed}


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_dir = root / "source"
        out_dir = root / "out"
        build_spanning(source_dir)

        with open_emit(source_dir) as emit:
            source_report = conformance.validate(emit)
            c16 = conformance.run_check(emit, "C16")
            print(
                f"source: {len(source_report.results)} checks, C16 passed={c16.passed}"
            )
            if not source_report.ok or not c16.passed:
                print("FAILURE: source emit is not fully conformant")
                return 1
            corrupt_emit(emit, _CONFIG, out_dir)

        with open_emit(out_dir) as corrupted:
            report = conformance.validate(corrupted)
        c16_corrupted = next(r for r in report.results if r.check == "C16")
        print(f"corrupted: C16 passed={c16_corrupted.passed}")
        for message in c16_corrupted.messages:
            print(f"  {message}")
        if c16_corrupted.passed or "prop__status" not in c16_corrupted.messages[0]:
            print("FAILURE: C16 did not fail naming prop__status")
            return 1

        manifest = json.loads((out_dir / "defects.json").read_text(encoding="utf-8"))
        impact_union = {
            code
            for defect in manifest["defects"]
            for code in defect["impact"]
            if code != "beyond-c1-c12"
        }
        failing = _failing_checks(report)
        print(f"manifest impact union: {sorted(impact_union)}")
        print(f"validate failing set: {sorted(failing)}")
        if "C16" not in impact_union:
            print("FAILURE: defects.json does not declare C16")
            return 1
        if not failing <= impact_union:
            print("FAILURE: failing checks escape the manifest impact union")
            return 1

    print("SUCCESS: C16 passes on the source, fails on the corrupted emit, declared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
