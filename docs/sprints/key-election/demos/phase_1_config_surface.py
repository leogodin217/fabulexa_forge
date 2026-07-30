#!/usr/bin/env python
"""
Demo: `keys` config surface + election error hierarchy
Sprint: key-election
Phase: 1

Parses election-bearing configs (scalar, per-sub-type map, dimensional
`target_key: record_index`), then shows each parse-time refusal (empty
`keys`, empty per-kind map, non-surface value). Also shows that kind/
sub-type existence against an emit is deliberately NOT a parse-time
check — the config is emit-independent (a `keys` entry naming a kind
that doesn't exist in any given emit still parses cleanly here; that
gate runs later, against the sidecar, at export time).

No emit is opened — Phase 1 delivers config parsing + the election error
classes only.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import ValidationError

from fabulexa_forge.config.loader import load_export_config
from fabulexa_forge.config.models import ExportConfig, FkClause
from fabulexa_forge.errors import (
    ElectedKeyDuplicate,
    ElectionDimKeyDisagrees,
    ElectionInheritanceAmbiguous,
    ElectionKindUnknown,
    ElectionMixedIdentity,
    ElectionPresentationUndeclared,
    ElectionSubTypeUnknown,
    ElectionUnionUnsafe,
    ExportError,
)

SCALAR_ELECTION_CONFIG = """
mode: source
keys:
  actor: presentation_id
  entity: presentation_id
  booking: record_index
source:
  change_delivery: changelog
"""

PER_SUB_TYPE_ELECTION_CONFIG = """
mode: source
keys:
  entity:
    alpha: presentation_id
    beta: presentation_id
    gamma: record_index
"""

DIMENSIONAL_RECORD_INDEX_CONFIG = """
mode: dimensional
keys:
  entity:
    alpha: presentation_id
dimensional:
  tables:
    - name: dim_alpha
      role: dim
      scd: type1
      source: {grain: records, kind: entity, filter: {prop__entity_type: alpha}}
      key: [alpha_id]
      columns:
        - {name: alpha_id, from: presentation_id}
    - name: fact_transfer
      role: fact
      source: {grain: history_point, kind: booking, property: location}
      key: [booking_id, transferred_at]
      columns:
        - {name: booking_id, from: record_id}
        - name: alpha_id
          fk: {to: dim_alpha, via: reference, target_key: record_index}
        - {name: transferred_at, derived: {timestamp: {source: sim_time}}}
"""

# `keys` names a kind no emit needs to declare for this to parse — kind/
# sub-type existence is an export-time gate against the sidecar, never a
# parse-time check (the config is emit-independent).
EMIT_INDEPENDENT_CONFIG = """
mode: source
keys:
  no_such_kind: presentation_id
"""

EMPTY_KEYS_BLOCK_CONFIG = """
mode: source
keys: {}
"""

EMPTY_PER_KIND_MAP_CONFIG = """
mode: source
keys:
  entity: {}
"""

NON_SURFACE_VALUE_CONFIG = """
mode: source
keys:
  entity: uuid
"""


def _load(yaml_text: str) -> ExportConfig:
    """Write yaml_text to a temp file and load it as an ExportConfig."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "export.yaml"
        path.write_text(yaml_text, encoding="utf-8")
        return load_export_config(path)


def demo_parses_cleanly() -> None:
    """Election-bearing configs (scalar, per-sub-type map, dimensional
    target_key: record_index) parse cleanly."""
    scalar_cfg = _load(SCALAR_ELECTION_CONFIG)
    assert scalar_cfg.keys == {
        "actor": "presentation_id",
        "entity": "presentation_id",
        "booking": "record_index",
    }
    print(f"SCALAR: keys = {scalar_cfg.keys}")

    per_sub_type_cfg = _load(PER_SUB_TYPE_ELECTION_CONFIG)
    assert per_sub_type_cfg.keys == {
        "entity": {
            "alpha": "presentation_id",
            "beta": "presentation_id",
            "gamma": "record_index",
        }
    }
    print(f"PER-SUB-TYPE: keys = {per_sub_type_cfg.keys}")

    dim_cfg = _load(DIMENSIONAL_RECORD_INDEX_CONFIG)
    assert dim_cfg.dimensional is not None
    fact = dim_cfg.dimensional.tables[1]
    fk_col = fact.columns[1]
    assert fk_col.fk is not None
    assert fk_col.fk.target_key == "record_index"
    print(f"DIMENSIONAL: fact_transfer.alpha_id.fk.target_key = {fk_col.fk.target_key}")

    absent_cfg = _load("mode: source\n")
    assert absent_cfg.keys is None
    print(f"NO KEYS BLOCK: keys = {absent_cfg.keys}")


def demo_emit_independent() -> None:
    """`keys` accepts a kind Pydantic can't check against any emit — kind/
    sub-type existence is deliberately NOT a parse-time error."""
    cfg = _load(EMIT_INDEPENDENT_CONFIG)
    assert cfg.keys == {"no_such_kind": "presentation_id"}
    print(f"EMIT-INDEPENDENT: parses cleanly with keys = {cfg.keys}")
    print("  (kind/sub-type existence is an export-time gate, not checked here)")


def demo_parse_time_refusals() -> None:
    """Each parse-time refusal fires: empty keys, empty per-kind map,
    non-surface value."""
    for label, yaml_text in (
        ("empty `keys: {}`", EMPTY_KEYS_BLOCK_CONFIG),
        ("empty per-kind map `entity: {}`", EMPTY_PER_KIND_MAP_CONFIG),
        ("non-surface value `entity: uuid`", NON_SURFACE_VALUE_CONFIG),
    ):
        try:
            _load(yaml_text)
        except Exception as exc:  # ConfigError wraps the underlying ValidationError
            print(f"REFUSED ({label}): {type(exc).__name__}: {exc}".splitlines()[0])
        else:
            raise AssertionError(f"expected a refusal for {label}")


def demo_fk_target_key() -> None:
    """fk.target_key: absent -> None (inherit); record_index parses;
    invalid literal is refused."""
    absent = FkClause.model_validate({"to": "dim_x", "via": "reference"})
    assert absent.target_key is None
    print(f"FK target_key absent -> {absent.target_key!r} (inherit, not 'record_id')")

    explicit = FkClause.model_validate(
        {"to": "dim_x", "via": "reference", "target_key": "record_index"}
    )
    assert explicit.target_key == "record_index"
    print(f"FK target_key: record_index -> {explicit.target_key!r}")

    try:
        FkClause.model_validate(
            {"to": "dim_x", "via": "reference", "target_key": "uuid"}
        )
    except ValidationError as exc:
        print(f"FK target_key: uuid -> refused: {type(exc).__name__}")
    else:
        raise AssertionError("expected a refusal for target_key: uuid")


def demo_election_error_hierarchy() -> None:
    """Every election error class subclasses ExportError and is catchable
    through the ExporterError funnel."""
    election_errors = [
        ElectionKindUnknown,
        ElectionSubTypeUnknown,
        ElectionPresentationUndeclared,
        ElectionMixedIdentity,
        ElectionUnionUnsafe,
        ElectionInheritanceAmbiguous,
        ElectionDimKeyDisagrees,
        ElectedKeyDuplicate,
    ]
    for cls in election_errors:
        assert issubclass(cls, ExportError)
    names = ", ".join(cls.__name__ for cls in election_errors)
    print(
        f"ERROR HIERARCHY: {len(election_errors)} classes subclass ExportError: {names}"
    )


def main() -> int:
    print("=== keys config surface ===")
    demo_parses_cleanly()
    print()
    print("=== emit-independence ===")
    demo_emit_independent()
    print()
    print("=== parse-time refusals ===")
    demo_parse_time_refusals()
    print()
    print("=== fk.target_key ===")
    demo_fk_target_key()
    print()
    print("=== election error hierarchy ===")
    demo_election_error_hierarchy()
    print()
    print("SUCCESS: config surface + election errors behave per spec")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
