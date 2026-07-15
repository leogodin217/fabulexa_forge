#!/usr/bin/env python
"""
Demo: The corrupter writer round-trips the class.

Builds a v5 emit through the Phase-3 fixture builder
(tests/reader/_fixtures_build.py's `build_spanning`, itself built on
`prop_column` / `write_emit`), corrupts it with a simple `null_cells` config,
then diffs the input and output sidecars column-by-column:

1. Every column declaring `temporal_class` in the source carries it verbatim
   in the corrupted output.
2. A column carrying neither temporal attribute (a structural column) stays
   bare in the output too -- no invented `temporal_class`, no `null`.
3. All three declared class values (constant / tracked / slice_only) survive
   the write.

Sprint: base-format-v5-adopt
Phase: 4
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# tests/reader is test infrastructure, not part of the installed package. Put
# tests/ on sys.path so this standalone demo can reuse the Phase-3 fixture
# builder the phase's success criteria refer to by name.
_TESTS_DIR = Path(__file__).resolve().parents[4] / "tests"
sys.path.insert(0, str(_TESTS_DIR))

import yaml  # noqa: E402
from reader._fixtures_build import build_spanning  # noqa: E402

from fabulexa_forge.config.models import CorruptConfig  # noqa: E402
from fabulexa_forge.corrupters.engine import corrupt_emit  # noqa: E402
from fabulexa_forge.reader.emit import open_emit  # noqa: E402

_TABLE = "records__actor"

_CORRUPT_CONFIG_YAML = """
seed: 7
operations:
  - kind: null_cells
    name: null_all_actor_status
    target:
      category: records
      columns: [prop__status]
    amount: { rate: 1.0 }
"""


def _column_attrs(columns: list[dict[str, object]]) -> dict[str, tuple[object, object]]:
    """Map column name -> (history_tracked, temporal_class), each `None` when absent."""
    return {
        col["name"]: (col.get("history_tracked"), col.get("temporal_class"))
        for col in columns
    }


def _run_corrupt(source_dir: Path, out_dir: Path) -> None:
    """Build the source emit and corrupt it with the embedded null_cells config."""
    build_spanning(source_dir)
    config = CorruptConfig.model_validate(yaml.safe_load(_CORRUPT_CONFIG_YAML))
    with open_emit(source_dir) as emit:
        corrupt_emit(emit, config, out_dir)


def _diff_sidecars(source_dir: Path, out_dir: Path) -> None:
    """Diff input/output sidecars column-by-column over `_TABLE`, printing the
    (history_tracked, temporal_class) pair carried on each side."""
    source_sidecar = json.loads((source_dir / "base.json").read_text(encoding="utf-8"))
    output_sidecar = json.loads((out_dir / "base.json").read_text(encoding="utf-8"))

    source_table = next(t for t in source_sidecar["tables"] if t["name"] == _TABLE)
    output_table = next(t for t in output_sidecar["tables"] if t["name"] == _TABLE)
    source_attrs = _column_attrs(source_table["columns"])
    output_attrs = _column_attrs(output_table["columns"])

    seen_classes: set[object] = set()
    for name, source_pair in source_attrs.items():
        output_pair = output_attrs[name]
        print(f"{_TABLE}.{name}: source={source_pair} output={output_pair}")
        if output_pair != source_pair:
            raise SystemExit(
                f"FAILURE: {name} did not round-trip verbatim: "
                f"source={source_pair} output={output_pair}"
            )
        seen_classes.add(source_pair[1])

    if source_attrs["record_id"] != (None, None):
        raise SystemExit("FAILURE: record_id fixture is expected to carry neither attr")
    if output_attrs["record_id"] != (None, None):
        raise SystemExit(
            "FAILURE: bare structural column record_id gained an invented "
            f"attribute in the output: {output_attrs['record_id']}"
        )

    if not {"constant", "tracked", "slice_only"}.issubset(seen_classes):
        raise SystemExit(
            "FAILURE: expected constant/tracked/slice_only all represented, "
            f"saw {seen_classes}"
        )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source_dir = tmp_path / "source"
        out_dir = tmp_path / "corrupted"
        _run_corrupt(source_dir, out_dir)
        _diff_sidecars(source_dir, out_dir)
    print(
        "SUCCESS: the corrupter writer carries temporal_class verbatim through "
        "write_base_emit -- declared -> declared, absent -> absent, never null."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
