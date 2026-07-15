#!/usr/bin/env python
"""
Demo: One version authority; gate -> 5.

Shows the version gate's two sides now that the vendored contract is v5:

1. The spanning fixture (a genuinely v5-shaped emit) opens through the reader
   and passes conformance check C1 against the vendored v5 JSON Schema -- the
   first time C1 has passed since the contract was re-vendored to v5.
2. A sidecar stamped with the never-valid sentinel version 99 is refused by
   the same reader with UnsupportedBaseFormatVersionError(found_version=99).

Sprint: base-format-v5-adopt
Phase: 1
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# The spanning fixture builder is test infrastructure (tests/reader/_fixtures_build.py),
# not part of the installed package. Put tests/ on sys.path so this standalone demo can
# reuse the one fixture builder the phase's success criteria refer to by name.
_TESTS_DIR = Path(__file__).resolve().parents[4] / "tests"
sys.path.insert(0, str(_TESTS_DIR))

from reader._fixtures_build import build_spanning  # noqa: E402

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION  # noqa: E402
from fabulexa_forge.reader.conformance import validate  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402
from fabulexa_forge.reader.errors import UnsupportedBaseFormatVersionError  # noqa: E402

_SENTINEL_VERSION = 99


def _spanning_fixture_passes_c1(emit_dir: Path) -> None:
    """Build the spanning fixture and assert C1 passes against the v5 schema."""
    build_spanning(emit_dir)
    with open_emit(emit_dir) as emit:
        if emit.sidecar.base_format_version != SUPPORTED_BASE_FORMAT_VERSION:
            raise SystemExit(
                "FAILURE: opened emit's base_format_version is "
                f"{emit.sidecar.base_format_version}, expected "
                f"{SUPPORTED_BASE_FORMAT_VERSION}"
            )
        report = validate(emit)
    c1 = next(r for r in report.results if r.check == "C1")
    if not c1.passed:
        raise SystemExit(
            f"FAILURE: C1 failed against the vendored schema: {c1.messages}"
        )
    print(
        f"C1 passed: the spanning fixture (base_format_version="
        f"{SUPPORTED_BASE_FORMAT_VERSION}) conforms to the vendored v5 JSON Schema."
    )


def _sentinel_version_is_refused(emit_dir: Path) -> None:
    """Stamp the spanning fixture's sidecar with 99 and assert the gate refuses it."""
    build_spanning(emit_dir)
    base_json_path = emit_dir / "base.json"
    sidecar = json.loads(base_json_path.read_text(encoding="utf-8"))
    sidecar["base_format_version"] = _SENTINEL_VERSION
    base_json_path.write_text(json.dumps(sidecar), encoding="utf-8")

    try:
        open_emit(emit_dir)
    except UnsupportedBaseFormatVersionError as exc:
        if exc.found_version != _SENTINEL_VERSION:
            raise SystemExit(
                f"FAILURE: expected found_version={_SENTINEL_VERSION}, "
                f"got {exc.found_version}"
            ) from None
        print(
            "UnsupportedBaseFormatVersionError raised: "
            f"found_version={exc.found_version} (no auto-upgrade)."
        )
    else:
        raise SystemExit("FAILURE: a base_format_version=99 emit was NOT refused")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        _spanning_fixture_passes_c1(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        _sentinel_version_is_refused(Path(tmp))
    print(
        "SUCCESS: the version gate is single-authority at "
        f"SUPPORTED_BASE_FORMAT_VERSION={SUPPORTED_BASE_FORMAT_VERSION}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
