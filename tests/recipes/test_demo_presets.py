"""Guard test: demo.yaml joins resolve against each preset's routed topic set.

Covers every committed preset under docs/examples/ that has both a stream.yaml
and a demo.yaml.  A preset whose bundle/ directory is absent (gitignored in CI)
is skipped rather than failed.

Validated constraints per preset:
- Every joins[].fact and joins[].dim is in build_topic_set(config).
- Every windows[] entry is a positive int.
- consumer_offset (when present) is "earliest" or "latest".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from fabulexa_forge.config.loader import load_stream_config
from fabulexa_forge.exporters.streaming.engine import build_topic_set

_EXAMPLES_ROOT = Path(__file__).parent.parent.parent / "docs" / "examples"

# Presets that have both stream.yaml and demo.yaml.
_PRESET_NAMES = ("nhs", "retail", "ride-sharing", "ride-sharing-marketplace")

# Declared join structure per preset.  Pure read-back presets have no fact→dim
# edges; a future edit that silently adds or removes joins must fail here.
_EXPECTED_JOINS: dict[str, list[dict[str, str]]] = {
    "nhs": [],
    "retail": [],
    "ride-sharing": [],
    "ride-sharing-marketplace": [
        {"fact": "driver", "dim": "zone"},
        {"fact": "rider", "dim": "zone"},
        {"fact": "match", "dim": "driver"},
        {"fact": "match", "dim": "rider"},
    ],
}


def _preset_bundle(name: str) -> Path:
    return _EXAMPLES_ROOT / name / "bundle"


def _preset_config(name: str) -> Path:
    return _EXAMPLES_ROOT / name / "stream.yaml"


def _preset_demo(name: str) -> Path:
    return _EXAMPLES_ROOT / name / "demo.yaml"


def _load_demo(name: str) -> dict[str, Any]:
    with _preset_demo(name).open() as fh:
        return yaml.safe_load(fh) or {}


@pytest.mark.parametrize("name", _PRESET_NAMES)
def test_demo_joins_resolve_against_topic_set(name: str) -> None:
    """Every join fact/dim in demo.yaml is in the preset's routed topic set.

    Skips when the bundle is absent (gitignored; not every CI checkout has it).
    """
    bundle = _preset_bundle(name)
    run_db = bundle / "run.duckdb"
    base_js = bundle / "base.json"
    if not run_db.exists() or not base_js.exists():
        pytest.skip(f"bundle absent for preset '{name}' — skipping")

    demo = _load_demo(name)
    config = load_stream_config(_preset_config(name))
    topic_set = set(build_topic_set(config))

    for entry in demo.get("joins", []):
        fact = entry["fact"]
        dim = entry["dim"]
        assert fact in topic_set, (
            f"preset '{name}': join fact '{fact}' not in topic set {sorted(topic_set)}"
        )
        assert dim in topic_set, (
            f"preset '{name}': join dim '{dim}' not in topic set {sorted(topic_set)}"
        )


@pytest.mark.parametrize("name", _PRESET_NAMES)
def test_demo_join_structure_pinned(name: str) -> None:
    """Pin the exact join list per preset.

    Pure read-back presets (retail, ride-sharing) must have no joins; the
    marketplace preset must carry exactly the four declared fact->dim pairs.
    A future edit that silently empties marketplace's joins, or that adds an
    unresolved join to a read-back preset, must fail this test.
    """
    demo = _load_demo(name)
    actual = demo.get("joins") or []
    expected = _EXPECTED_JOINS[name]
    assert actual == expected, (
        f"preset '{name}': join list {actual!r} != expected {expected!r}"
    )


@pytest.mark.parametrize("name", _PRESET_NAMES)
def test_demo_windows_are_positive_ints(name: str) -> None:
    """Every windows[] entry in demo.yaml is a positive int."""
    demo = _load_demo(name)
    for i, entry in enumerate(demo.get("windows", [])):
        assert isinstance(entry, int) and entry > 0, (
            f"preset '{name}': windows[{i}] = {entry!r} is not a positive int"
        )


@pytest.mark.parametrize("name", _PRESET_NAMES)
def test_demo_consumer_offset_valid(name: str) -> None:
    """consumer_offset (when present) is 'earliest' or 'latest'."""
    demo = _load_demo(name)
    offset = demo.get("consumer_offset")
    if offset is not None:
        assert offset in ("earliest", "latest"), (
            f"preset '{name}': consumer_offset={offset!r} is not 'earliest' or 'latest'"
        )


def test_entity_not_in_marketplace_topic_set() -> None:
    """'entity' is not in the ride-sharing-marketplace topic set.

    The entity kind streams under the declared domain topic 'zone'
    (source.yaml's table name for entity/zone_market) — never the bare kind
    'entity'.  This proves the guard tests against the declared topic set,
    not the raw kind list.
    """
    bundle = _preset_bundle("ride-sharing-marketplace")
    run_db = bundle / "run.duckdb"
    base_js = bundle / "base.json"
    if not run_db.exists() or not base_js.exists():
        pytest.skip("bundle absent for ride-sharing-marketplace — skipping")

    config = load_stream_config(_preset_config("ride-sharing-marketplace"))
    topic_set = set(build_topic_set(config))

    assert "entity" not in topic_set, (
        f"'entity' should not be in topic set {sorted(topic_set)}; "
        "expected the declared domain topic 'zone'"
    )
    assert "zone" in topic_set, f"'zone' should be in topic set {sorted(topic_set)}"
