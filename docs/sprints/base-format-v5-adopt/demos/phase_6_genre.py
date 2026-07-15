#!/usr/bin/env python
"""
Demo: The genre trichotomy keys on the class.

Three self-contained scenarios, all over a single dimension-role kind
(`venue`) whose sole prop__ column is a presentation value (`prop__name`):

1. The presentation column is class 'tracked' -- the genre trichotomy
   reclassifies the kind from reference to change-log genre, even though
   `record_roles` still declares 'dimension' (the documented reclassification:
   a name that genuinely changes over time *is* a change log).
2. The same kind, presentation column class 'constant' -- no reclassification;
   the kind classifies reference genre by role. The class, not the
   history_tracked bit, decides.
3. The presentation column carries history_tracked with no paired
   temporal_class (the vendored schema does not enforce the pairing) --
   `build_source_plan` raises `TemporalClassUnavailableError` at plan time,
   before any data read, with a message directing to `fabulexa-forge
   validate`.

Sprint: base-format-v5-adopt
Phase: 6
"""

from __future__ import annotations

import sys
from pathlib import Path

# tests/_support is test infrastructure, not part of the installed package. Put
# tests/ on sys.path so this standalone demo can reuse the one fixture-sidecar
# authority the phase's success criteria refer to by name.
_TESTS_DIR = Path(__file__).resolve().parents[4] / "tests"
sys.path.insert(0, str(_TESTS_DIR))

from _support.sidecar_builder import prop_column  # noqa: E402

from fabulexa_forge.exporters.source.plan import build_source_plan  # noqa: E402
from fabulexa_forge.reader.errors import TemporalClassUnavailableError  # noqa: E402
from fabulexa_forge.reader.sidecar import Sidecar  # noqa: E402

_STRUCTURAL_COLUMNS: list[dict[str, object]] = [
    {"name": "fork_path", "type": "VARCHAR"},
    {"name": "record_id", "type": "VARCHAR"},
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
]


def _venue_sidecar(prop_col: dict[str, object]) -> Sidecar:
    """A single dimension-role `records__venue` table carrying `prop_col` as its
    sole value-carrying column."""
    raw: dict[str, object] = {
        "base_format_version": 5,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        "tables": [
            {
                "name": "records__venue",
                "category": "records",
                "record_kind": "venue",
                "columns": [*_STRUCTURAL_COLUMNS, prop_col],
                "rows": 1,
            }
        ],
        "record_roles": {"venue": "dimension"},
    }
    return Sidecar.from_raw(raw)


def _demo_tracked_reclassifies() -> None:
    """A 'tracked' presentation column reclassifies a dimension kind to
    change-log genre."""
    prop_col = prop_column(
        "prop__name", "VARCHAR", history_tracked=True, temporal_class="tracked"
    )
    plan = build_source_plan(_venue_sidecar(prop_col), None)
    if plan[0].genre != "changelog":
        raise SystemExit(f"FAILURE: expected changelog genre, got {plan[0].genre!r}")
    print(f"tracked presentation column -> genre={plan[0].genre!r} (reclassified)")


def _demo_constant_does_not_reclassify() -> None:
    """A 'constant' presentation column does not reclassify; genre stays
    reference, by role."""
    prop_col = prop_column(
        "prop__name", "VARCHAR", history_tracked=True, temporal_class="constant"
    )
    plan = build_source_plan(_venue_sidecar(prop_col), None)
    if plan[0].genre != "reference":
        raise SystemExit(f"FAILURE: expected reference genre, got {plan[0].genre!r}")
    print(f"constant presentation column -> genre={plan[0].genre!r} (unreclassified)")


def _demo_missing_class_refuses_at_plan_time() -> None:
    """A flagged column with no declared class raises TemporalClassUnavailableError
    at plan time, before any data read, directing to `fabulexa-forge validate`."""
    prop_col = prop_column(
        "prop__name", "VARCHAR", history_tracked=True, temporal_class="tracked"
    )
    del prop_col["temporal_class"]
    try:
        build_source_plan(_venue_sidecar(prop_col), None)
    except TemporalClassUnavailableError as exc:
        if "fabulexa-forge validate" not in str(exc):
            raise SystemExit(
                f"FAILURE: message must direct to fabulexa-forge validate: {exc}"
            ) from None
        print(f"flagged column with no class -> refused at plan time: {exc}")
    else:
        raise SystemExit("FAILURE: expected TemporalClassUnavailableError")


def main() -> int:
    _demo_tracked_reclassifies()
    _demo_constant_does_not_reclassify()
    _demo_missing_class_refuses_at_plan_time()
    print("SUCCESS: the genre trichotomy keys on temporal_class, not history_tracked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
