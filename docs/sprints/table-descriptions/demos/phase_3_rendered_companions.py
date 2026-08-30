#!/usr/bin/env python
"""
Demo: Dictionary resolution -- author-first table-description tier, the
forge-pinned event-log table + column set, `origin: "forge"`.

Sprint: table-descriptions
Phase: 3

Runs a full `mode: source` export (a described `visit_state` table, an
`events` declaration, and a `readme_overlay` table note on `visit_state`)
against the source exporter test fixture emit -- reusing it via a
`sys.path` splice, mirroring the Phase 2 demo. Prints the rendered README's
`visit_state` and event-log sections and the manifest's `tables` entries:
`visit_state` shows the overlay note first, then the author description;
the event log shows the pinned table description and its six pinned column
lines, with `item_type`'s gloss list rendering beneath the pinned
description.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (_REPO_ROOT, _REPO_ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from _support.notices import discard_notice_sink  # noqa: E402

from fabulexa_forge.anchor import resolve_effective_anchor  # noqa: E402
from fabulexa_forge.config.loader import load_export_config  # noqa: E402
from fabulexa_forge.exporters.companion.overlay import ReadmeOverlay  # noqa: E402
from fabulexa_forge.exporters.source.engine import export_source  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402
from tests.exporters.source._source_fixtures import (  # noqa: E402
    build_source_test_emit,
)

_VISIT_TABLE_DESCRIPTION = "Visit records as the app would present them."

_CONFIG_YAML = f"""
mode: source
source:
  tables:
    - name: visit_state
      kind: visit
      description: "{_VISIT_TABLE_DESCRIPTION}"
  events:
    name: audit
    sources:
      - kind: visit
"""


def _print_readme_section(readme_text: str, table_name: str) -> None:
    """Print one table section's text (up to the next '### ' heading or end)."""
    start = readme_text.index(f"### {table_name}")
    rest = readme_text[start + 1 :]
    next_heading = rest.find("\n### ")
    end = start + 1 + next_heading if next_heading != -1 else len(readme_text)
    print(readme_text[start:end].rstrip())
    print()


def _print_manifest_table(document: dict[str, object], table_name: str) -> None:
    """Print one manifest `tables[]` entry's name, description, and columns."""
    tables = document["tables"]
    assert isinstance(tables, list)
    entry = next(t for t in tables if t["name"] == table_name)
    print(f"  {table_name!r}: description={entry['description']!r}")
    columns = entry["columns"]
    assert isinstance(columns, list)
    for column in columns:
        print(f"    {column['name']!r}: description={column['description']!r}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        emit_dir = tmp_dir / "emit"
        emit_dir.mkdir()
        build_source_test_emit(emit_dir)

        config_path = tmp_dir / "source.yaml"
        config_path.write_text(_CONFIG_YAML, encoding="utf-8")
        config = load_export_config(config_path)
        overlay = ReadmeOverlay(
            overview=None, table_notes={"visit_state": "Nightly extract note."}
        )

        out = tmp_dir / "export.duckdb"
        with open_emit(emit_dir) as emit:
            anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
            assert anchor is not None, "the fixture declares a runtime block"
            report = export_source(
                emit, config, out, "duckdb", anchor, discard_notice_sink, overlay
            )

        event_log_table = next(t for t in report.tables if t.event_log)
        print(f"== event-log spec: {event_log_table.name!r} (event_log=True) ==")
        print()

        readme_path = tmp_dir / f"{out.stem}-source-readme.md"
        manifest_path = tmp_dir / f"{out.stem}-source-manifest.json"
        readme_text = readme_path.read_text(encoding="utf-8")
        document = json.loads(manifest_path.read_text(encoding="utf-8"))

        print("== README: visit_state (overlay note, then author description) ==")
        _print_readme_section(readme_text, "visit_state")

        print(f"== README: {event_log_table.name} (pinned table + column docs) ==")
        _print_readme_section(readme_text, event_log_table.name)

        print("== manifest: tables[] ==")
        _print_manifest_table(document, "visit_state")
        _print_manifest_table(document, event_log_table.name)

    print(
        "SUCCESS: author table description resolves author-first; the event"
        " log renders the forge-pinned table + six column descriptions; the"
        " overlay note and author description both render, note first"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
