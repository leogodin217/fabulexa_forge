#!/usr/bin/env python
"""
Demo: README overlay grammar — parsing, slot printing, and loud refusals
Sprint: companion-artifacts
Phase: 1

Writes a sample overlay to a temp file, parses it with load_readme_overlay and
prints its slots, then shows two grammar rejections (an uncased '## Overview'
heading, a duplicate 'table:' key) and an unknown-table refusal from
validate_overlay_tables — each naming the offender.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fabulexa_forge.errors import ReadmeOverlayInvalid, ReadmeOverlayUnknownTable
from fabulexa_forge.exporters.companion.overlay import (
    load_readme_overlay,
    validate_overlay_tables,
)

SAMPLE_OVERLAY = """\
## overview
Nightly extract of the clinic's operational database, reshaped for the
data-engineering course. Timestamps are Europe/London wallclock.

## table: patients
One row per registered patient; `status` is the current value at export time.

## table: ward_events
The polymorphic event log. `changes` holds the per-event column diff as JSON.
"""

WRONG_CASE_OVERLAY = "## Overview\nThis heading is wrong-cased.\n"

DUPLICATE_KEY_OVERLAY = """\
## table: patients
First note.

## table: patients
Second note — a duplicate key.
"""


def demo_parse_sample_overlay(tmp_dir: Path) -> None:
    """Parse a well-formed overlay and print its slots."""
    path = tmp_dir / "sample-overlay.md"
    path.write_text(SAMPLE_OVERLAY, encoding="utf-8")

    overlay = load_readme_overlay(path)
    print("--- parsed overlay ---")
    print(f"overview: {overlay.overview!r}")
    for name, body in overlay.table_notes.items():
        print(f"table: {name!r} -> {body!r}")


def demo_grammar_rejections(tmp_dir: Path) -> None:
    """Show two grammar rejections, each naming the offender."""
    print("--- grammar rejections ---")

    wrong_case_path = tmp_dir / "wrong-case-overlay.md"
    wrong_case_path.write_text(WRONG_CASE_OVERLAY, encoding="utf-8")
    try:
        load_readme_overlay(wrong_case_path)
    except ReadmeOverlayInvalid as exc:
        print(f"'## Overview' (wrong case): refused ({exc})")
    else:
        raise AssertionError("expected ReadmeOverlayInvalid for '## Overview'")

    duplicate_key_path = tmp_dir / "duplicate-key-overlay.md"
    duplicate_key_path.write_text(DUPLICATE_KEY_OVERLAY, encoding="utf-8")
    try:
        load_readme_overlay(duplicate_key_path)
    except ReadmeOverlayInvalid as exc:
        print(f"duplicate 'table: patients' slot: refused ({exc})")
    else:
        raise AssertionError("expected ReadmeOverlayInvalid for a duplicate slot")


def demo_unknown_table_refusal(tmp_dir: Path) -> None:
    """Show validate_overlay_tables refusing a table: slot the plan doesn't
    produce."""
    print("--- unknown-table refusal ---")
    path = tmp_dir / "sample-overlay.md"
    overlay = load_readme_overlay(path)
    try:
        validate_overlay_tables(overlay, ["patients"])
    except ReadmeOverlayUnknownTable as exc:
        print(f"'table: ward_events' against a plan without it: refused ({exc})")
    else:
        raise AssertionError("expected ReadmeOverlayUnknownTable")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        demo_parse_sample_overlay(tmp_dir)
        demo_grammar_rejections(tmp_dir)
        demo_unknown_table_refusal(tmp_dir)
    print("SUCCESS: overlay grammar parses, rejects, and validates as specified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
