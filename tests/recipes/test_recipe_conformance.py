"""Conformance gate for the shared recipe fixture.

Asserts that the fixture emit passes every conformance check C1–C12 so that
recipe tests always run against a base layer that meets the contract.

Exporters are specified to receive conformant emits; only corrupters are
permitted to break conformance (C6/C7). A failing conformance gate here means
the fixture itself has drifted out of contract and must be fixed before any
recipe result can be trusted.
"""

from __future__ import annotations

from pathlib import Path

from fabulexa_forge.reader.conformance import validate
from fabulexa_forge.reader.emit import open_emit


def test_recipe_fixture_passes_all_conformance_checks(
    recipe_emit_dir: Path,
) -> None:
    """The shared recipe fixture emit passes C1–C12 with no failures."""
    with open_emit(recipe_emit_dir) as emit:
        report = validate(emit)

    failures = [r for r in report.results if not r.passed]
    assert not failures, (
        "Recipe fixture failed conformance checks — the fixture is out of contract.\n"
        + "\n".join(f"  {r.check}: {'; '.join(r.messages)}" for r in failures)
    )
