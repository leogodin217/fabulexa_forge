#!/usr/bin/env python
"""
Demo: The three table-level `description` config surfaces (`TableDecl.description`,
`SourceTableDecl.description`, `RenameEntry.description`) parse and load-validate;
`RenameEntry`'s widened at-least-one-field rule accepts a description-only entry;
a `description` key on the source events declaration is a parse error.

Sprint: table-descriptions
Phase: 1

Parses one example export config per mode (dimensional, source, base), each
setting the mode's table-level `description`, and prints the parsed value.
Separately parses a base rename entry carrying only `description` (no name,
no columns) to show it is a legal entry, then shows a `description` key on
the source events declaration refused at load (`SourceEventsDecl` is a
strict model with no such field). Stamping these values onto the compiled
plan (`QuerySpec.author_table_description` / `TableReport`) is Phase 2's
job -- this phase only carries the config surface.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.errors import ConfigError

_DIMENSIONAL_CONFIG = """
mode: dimensional
dimensional:
  tables:
    - name: dim_entity
      role: dim
      key: [id]
      source:
        grain: records
        kind: entity
      description: "One row per entity, current state only."
      columns:
        - name: id
          from: record_id
"""

_SOURCE_CONFIG = """
mode: source
source:
  tables:
    - name: entity_state
      kind: entity
      description: "The entity table as the app database would present it."
"""

_BASE_CONFIG = """
mode: base
base:
  rename:
    - table: records__entity
      description: "The raw entity records table, renamed for the export."
"""

_DESCRIPTION_ONLY_RENAME_CONFIG = """
mode: base
base:
  rename:
    - table: records__entity
      description: "Only a description override -- no name, no columns."
"""

_EVENTS_DESCRIPTION_CONFIG = """
mode: source
source:
  events:
    name: versions
    description: "This key does not exist on SourceEventsDecl."
    sources:
      - kind: entity
"""


def _write_config(tmp_dir: Path, name: str, text: str) -> Path:
    """Write one example config's YAML to `tmp_dir/name` and return its path."""
    path = tmp_dir / name
    path.write_text(text, encoding="utf-8")
    return path


def _parse_table_description_surfaces(tmp_dir: Path) -> None:
    """Parse the three per-mode table-description surfaces and print each
    parsed value."""
    dimensional = load_export_config(
        _write_config(tmp_dir, "dimensional.yaml", _DIMENSIONAL_CONFIG)
    )
    assert dimensional.dimensional is not None
    table = dimensional.dimensional.tables[0]
    print(f"dimensional: TableDecl.description = {table.description!r}")

    source = load_export_config(_write_config(tmp_dir, "source.yaml", _SOURCE_CONFIG))
    assert source.source is not None
    print(
        f"source: SourceTableDecl.description = {source.source.tables[0].description!r}"
    )

    base = load_export_config(_write_config(tmp_dir, "base.yaml", _BASE_CONFIG))
    assert base.base is not None
    assert base.base.rename is not None
    print(f"base: RenameEntry.description = {base.base.rename[0].description!r}")


def _parse_description_only_rename_entry(tmp_dir: Path) -> None:
    """Parse a base rename entry carrying only `description` (no `name`, no
    `columns`) and show it is a legal entry."""
    config = load_export_config(
        _write_config(
            tmp_dir, "rename_description_only.yaml", _DESCRIPTION_ONLY_RENAME_CONFIG
        )
    )
    assert config.base is not None
    assert config.base.rename is not None
    entry = config.base.rename[0]
    print(
        "base: description-only rename entry -- name="
        f"{entry.name!r} columns={entry.columns!r} description={entry.description!r}"
    )


def _reject_events_description_key(tmp_dir: Path) -> None:
    """Show a `description` key on the source events declaration refused at
    load -- no such config surface exists (the event log's documentation is
    entirely forge-pinned)."""
    try:
        load_export_config(
            _write_config(
                tmp_dir, "events_description.yaml", _EVENTS_DESCRIPTION_CONFIG
            )
        )
        raise AssertionError(
            "a description key on the events declaration should have been refused"
        )
    except ConfigError as exc:
        print(f"events declaration 'description' key refused at load: {exc}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _parse_table_description_surfaces(tmp_dir)
        _parse_description_only_rename_entry(tmp_dir)
        _reject_events_description_key(tmp_dir)

    print(
        "SUCCESS: all three table-description config surfaces parse and validate;"
        " a description-only rename entry is legal; the events declaration"
        " refuses a description key"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
