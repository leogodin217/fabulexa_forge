#!/usr/bin/env python
"""
Demo: BaseConfig + the widened ExportConfig for mode: base
Sprint: base-mode
Phase: 1

Loads three valid `mode: base` configs (bare, sliced, excluding/renaming) and
shows each of the five rejections the Phase 1 validators enforce, printing the
error message surfaced to the author in every case.
"""

from __future__ import annotations

import yaml
from pydantic import ValidationError

from fabulexa_forge.config.models import ExportConfig

BARE_BASE_YAML = """
mode: base
"""

SLICED_BASE_YAML = """
mode: base
base:
  slice_at: 172800000000000
"""

EXCLUDE_RENAME_BASE_YAML = """
mode: base
base:
  exclude:
    kinds: [scheduler]
  rename:
    - table: records__actor
      name: actors
"""

EMPTY_BASE_BLOCK_YAML = """
mode: base
base: {}
"""

NEGATIVE_SLICE_AT_YAML = """
mode: base
base:
  slice_at: -1
"""

RENAME_SUB_TYPE_YAML = """
mode: base
base:
  rename:
    - table: records__entity
      sub_type: consultant
      name: consultants
"""

DUPLICATE_RENAME_TABLE_YAML = """
mode: base
base:
  rename:
    - table: records__actor
      name: actors_a
    - table: records__actor
      name: actors_b
"""

SLICE_AT_WITH_INCREMENTAL_YAML = """
mode: base
base:
  slice_at: 100
incremental:
  sim_period_ns: 1
"""


def load(yaml_text: str) -> ExportConfig:
    """Parse a YAML string into an ExportConfig."""
    return ExportConfig.model_validate(yaml.safe_load(yaml_text))


def show_valid(label: str, yaml_text: str) -> None:
    """Load a valid config and print a summary of the resulting BaseConfig."""
    config = load(yaml_text)
    print(f"OK   {label}: base={config.base!r}")


def show_rejected(label: str, yaml_text: str) -> None:
    """Load an invalid config and print the ValidationError message."""
    try:
        load(yaml_text)
    except ValidationError as exc:
        print(f"FAIL {label}: {exc.errors()[0]['msg']}")
    else:
        raise AssertionError(f"{label}: expected ValidationError, none raised")


def main() -> int:
    print("--- Valid configs ---")
    show_valid("bare mode: base", BARE_BASE_YAML)
    show_valid("sliced mode: base", SLICED_BASE_YAML)
    show_valid("excluding/renaming mode: base", EXCLUDE_RENAME_BASE_YAML)

    print("\n--- Rejections ---")
    show_rejected("empty base: {} block", EMPTY_BASE_BLOCK_YAML)
    show_rejected("negative slice_at", NEGATIVE_SLICE_AT_YAML)
    show_rejected("rename with sub_type", RENAME_SUB_TYPE_YAML)
    show_rejected("duplicate rename table target", DUPLICATE_RENAME_TABLE_YAML)
    show_rejected("slice_at with incremental", SLICE_AT_WITH_INCREMENTAL_YAML)

    print("\nSUCCESS: BaseConfig + ExportConfig base arm validated as spec'd")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
