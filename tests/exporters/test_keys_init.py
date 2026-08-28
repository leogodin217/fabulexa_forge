"""Tests for `exporters.keys_init`: the shared `init` election-menu module.

Covers the two public contracts (docs/architecture/key-election.md § `init`
proposals):
- `propose_key_election`: the active election is uniformly `record_index` for
  every population of every kind; alternatives follow resolvability alone
  (`record_id` always, `presentation_id` only where the presentation-key
  registry declares the population); shape (scalar vs. per-sub-type map)
  follows whether >= 1 declared sub-type exists, never the active values.
  An incoherent `presentation_keys` block propagates
  `PresentationKeysInvalidError` unconditionally (the function always
  consults the registry).
- `render_keys_block`: the single renderer -- swap-not-uncomment header first,
  alternatives before the active line, `keys:` first and one trailing blank
  line, declaration order preserved.
- `population_declared`: presence alone, independent of a rollup claim or
  union-safety.
- Cross-mode: `propose_key_election` + `render_keys_block` produce the exact
  `keys:` block every mode's `init` engine splices -- verified end-to-end
  against all three (dimensional / source / streaming).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from _support.notices import discard_notice_sink
from _support.sidecar_builder import identity_column, prop_column, write_emit

from fabulexa_forge import SUPPORTED_BASE_FORMAT_VERSION
from fabulexa_forge.exporters.dimensional.init import generate_init_config
from fabulexa_forge.exporters.keys_init import (
    KeyElectionProposal,
    population_declared,
    propose_key_election,
    render_keys_block,
)
from fabulexa_forge.exporters.source.init import generate_source_init_config
from fabulexa_forge.exporters.streaming.init import generate_stream_init_config
from fabulexa_forge.reader.emit import open_emit
from fabulexa_forge.reader.errors import PresentationKeysInvalidError
from fabulexa_forge.reader.sidecar import Sidecar

# ---------------------------------------------------------------------------
# Sidecar-building helpers (raw Sidecar.from_raw, no DuckDB -- mirrors
# tests/exporters/test_election.py's convention for plan-time-only tests)
# ---------------------------------------------------------------------------


def _col(name: str, type_: str = "VARCHAR") -> dict[str, object]:
    return {"name": name, "type": type_}


def _records_table(
    kind: str, discriminator: bool = False, presentation_id: bool = True
) -> dict[str, object]:
    """Build a raw `records__<kind>` table entry.

    `discriminator=True` adds a `prop__<kind>_type` column (the domain itself
    lives in `enum_domains`, consulted independently by `subtype_values`).
    """
    cols = [_col("fork_path"), _col("record_id")]
    if presentation_id:
        cols.append(_col("presentation_id"))
    cols += [
        _col("created_sim_time", "BIGINT"),
        _col("active", "BOOLEAN"),
        _col("deactivated_at", "BIGINT"),
        _col("last_mutation_sim_time", "BIGINT"),
    ]
    if discriminator:
        cols.append(_col(f"prop__{kind}_type"))
    return {
        "name": f"records__{kind}",
        "category": "records",
        "record_kind": kind,
        "columns": cols,
        "rows": 1,
    }


def _sidecar(
    tables: list[dict[str, object]],
    enum_domains: dict[str, object] | None = None,
    presentation_keys: dict[str, object] | None = None,
) -> Sidecar:
    raw: dict[str, object] = {
        "base_format_version": SUPPORTED_BASE_FORMAT_VERSION,
        "branches": [{"fork_path": "trunk", "parent": None, "slice_at": 1000}],
        "tables": tables,
    }
    if enum_domains is not None:
        raw["enum_domains"] = enum_domains
    if presentation_keys is not None:
        raw["presentation_keys"] = presentation_keys
    return Sidecar.from_raw(raw)


def _raw_counter_key(prefix: str, width: int = 3) -> dict[str, object]:
    """A conformant counter-class raw partition_key (emit/false/false)."""
    return {
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
        "key_space": {"class": "counter", "prefix": prefix, "width": width},
    }


#: `entity`'s driver/bus declared with bare (comparable) counter prefixes --
#: pairwise-unsafe if union-safety were consulted; `propose_key_election`
#: never consults it, so this fixture doubles as the no-gate regression case.
_UNSAFE_PRESENTATION_KEYS: dict[str, object] = {
    "entity": {
        "sub_types": {
            "driver": _raw_counter_key(""),
            "bus": _raw_counter_key(""),
        },
        "branch_stable": False,
        "slice_stable": False,
    }
}

_PARTIAL_PRESENTATION_KEYS: dict[str, object] = {
    "entity": {
        "sub_types": {
            "driver": _raw_counter_key("DRV_"),
        },
        "unique_within": "emit",
        "branch_stable": False,
        "slice_stable": False,
    }
}

_FLAT_PRESENTATION_KEYS: dict[str, object] = {
    "booking": {
        "key": {
            "unique_within": "branch",
            "branch_stable": True,
            "slice_stable": True,
            "key_space": {"class": "record_index", "prefix": "", "width": 4},
        }
    }
}

_ENTITY_DOMAIN: dict[str, object] = {"entity": {"entity_type": ["driver", "bus"]}}


# ---------------------------------------------------------------------------
# propose_key_election
# ---------------------------------------------------------------------------


def test_active_is_uniformly_record_index() -> None:
    """Every population of every kind elects `record_index` as active,
    regardless of registry declaration."""
    sidecar = _sidecar(
        [_records_table("booking", presentation_id=False)],
        presentation_keys=None,
    )
    proposal = propose_key_election(sidecar)
    assert proposal.active == {"booking": "record_index"}


def test_flat_kind_undeclared_offers_only_record_id_alternative() -> None:
    """A flat kind absent from the registry offers only `record_id`."""
    sidecar = _sidecar(
        [_records_table("booking", presentation_id=False)],
        presentation_keys=None,
    )
    proposal = propose_key_election(sidecar)
    assert proposal.alternatives["booking"] == ["record_id"]


def test_flat_kind_declared_appends_presentation_id_alternative() -> None:
    """A flat kind with its own `key` entry additionally offers
    `presentation_id`, appended after `record_id`."""
    sidecar = _sidecar(
        [_records_table("booking")],
        presentation_keys=_FLAT_PRESENTATION_KEYS,
    )
    proposal = propose_key_election(sidecar)
    assert proposal.active == {"booking": "record_index"}
    assert proposal.alternatives["booking"] == ["record_id", "presentation_id"]


def test_partitioned_kind_no_subtype_declared_collapses_to_scalar() -> None:
    """A partitioned kind with a declared domain but no sub-type carrying a
    registry entry still collapses the active election to the scalar, and
    the scalar's alternatives are addressable by the bare kind name."""
    sidecar = _sidecar(
        [_records_table("entity", discriminator=True, presentation_id=False)],
        enum_domains=_ENTITY_DOMAIN,
        presentation_keys=None,
    )
    proposal = propose_key_election(sidecar)
    assert proposal.active == {"entity": "record_index"}
    assert proposal.alternatives["entity"] == ["record_id"]


def test_partitioned_kind_one_subtype_declared_proposes_per_subtype_map() -> None:
    """>= 1 declared sub-type proposes the per-sub-type map -- shape follows
    the alternatives, not the (uniformly record_index) active values."""
    sidecar = _sidecar(
        [_records_table("entity", discriminator=True)],
        enum_domains=_ENTITY_DOMAIN,
        presentation_keys=_PARTIAL_PRESENTATION_KEYS,
    )
    proposal = propose_key_election(sidecar)
    assert proposal.active == {
        "entity": {"driver": "record_index", "bus": "record_index"}
    }
    assert proposal.alternatives["entity.driver"] == ["record_id", "presentation_id"]
    assert proposal.alternatives["entity.bus"] == ["record_id"]


def test_pairwise_unsafe_declaration_proposes_map_no_gate_consulted() -> None:
    """A pairwise-unsafe declared pair (both bare counter prefixes) still
    proposes the per-sub-type map with no exception and no degradation --
    `propose_key_election` never consults union-safety."""
    sidecar = _sidecar(
        [_records_table("entity", discriminator=True)],
        enum_domains=_ENTITY_DOMAIN,
        presentation_keys=_UNSAFE_PRESENTATION_KEYS,
    )
    proposal = propose_key_election(sidecar)
    assert proposal.active == {
        "entity": {"driver": "record_index", "bus": "record_index"}
    }
    assert proposal.alternatives["entity.driver"] == ["record_id", "presentation_id"]
    assert proposal.alternatives["entity.bus"] == ["record_id", "presentation_id"]


def test_multiple_kinds_each_get_their_own_entry() -> None:
    """One active entry and one alternatives entry per known kind, sidecar
    table order."""
    sidecar = _sidecar(
        [
            _records_table("entity", discriminator=True),
            _records_table("booking"),
        ],
        enum_domains=_ENTITY_DOMAIN,
        presentation_keys={**_PARTIAL_PRESENTATION_KEYS, **_FLAT_PRESENTATION_KEYS},
    )
    proposal = propose_key_election(sidecar)
    assert list(proposal.active.keys()) == ["entity", "booking"]


def test_incoherent_registry_block_propagates_unconditionally() -> None:
    """An incoherent `presentation_keys` block raises `PresentationKeysInvalidError`
    even though nothing in the proposal would elect `presentation_id`
    -- `propose_key_election` always consults the registry."""
    incoherent: dict[str, object] = {
        "booking": {
            "key": {
                "unique_within": "branch",
                "branch_stable": True,
                "slice_stable": True,
                "key_space": {"class": "counter", "prefix": "X_", "width": 3},
            }
        }
    }
    sidecar = _sidecar([_records_table("booking")], presentation_keys=incoherent)
    with pytest.raises(PresentationKeysInvalidError):
        propose_key_election(sidecar)


def test_proposal_is_frozen() -> None:
    """`KeyElectionProposal` is an immutable dataclass."""
    proposal = KeyElectionProposal(active={}, alternatives={})
    with pytest.raises(AttributeError):
        proposal.active = {}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# population_declared
# ---------------------------------------------------------------------------


def test_population_declared_true_for_flat_key_entry() -> None:
    sidecar = _sidecar(
        [_records_table("booking")], presentation_keys=_FLAT_PRESENTATION_KEYS
    )
    assert population_declared(sidecar.presentation_keys(), "booking", None) is True


def test_population_declared_false_when_kind_absent() -> None:
    sidecar = _sidecar([_records_table("booking", presentation_id=False)])
    assert population_declared(sidecar.presentation_keys(), "booking", None) is False


def test_population_declared_true_for_declared_subtype() -> None:
    sidecar = _sidecar(
        [_records_table("entity", discriminator=True)],
        enum_domains=_ENTITY_DOMAIN,
        presentation_keys=_PARTIAL_PRESENTATION_KEYS,
    )
    keys = sidecar.presentation_keys()
    assert population_declared(keys, "entity", "driver") is True
    assert population_declared(keys, "entity", "bus") is False


def test_population_declared_false_when_registry_absent() -> None:
    assert population_declared(None, "booking", None) is False


# ---------------------------------------------------------------------------
# render_keys_block
# ---------------------------------------------------------------------------


def test_render_scalar_leads_with_swap_header_then_alternatives_then_active() -> None:
    proposal = KeyElectionProposal(
        active={"booking": "record_index"},
        alternatives={"booking": ["record_id", "presentation_id"]},
    )
    lines = render_keys_block(proposal)
    assert lines[0] == "keys:"
    assert lines[1] == (
        "  # NOTE: an uncommented alternative below SWAPS the active line for"
        " this population -- delete the active line, don't just uncomment"
    )
    assert lines[2] == "  # booking: record_id"
    assert lines[3] == "  # booking: presentation_id"
    assert lines[4] == "  booking: record_index"
    assert lines[-1] == ""


def test_render_no_alternatives_omits_alternative_comments() -> None:
    proposal = KeyElectionProposal(
        active={"booking": "record_index"},
        alternatives={"booking": ["record_id"]},
    )
    lines = render_keys_block(proposal)
    assert "# booking: presentation_id" not in lines
    assert lines[-2] == "  booking: record_index"


def test_render_map_indents_per_subtype_alternatives_before_active() -> None:
    proposal = KeyElectionProposal(
        active={"entity": {"driver": "record_index", "bus": "record_index"}},
        alternatives={
            "entity.driver": ["record_id", "presentation_id"],
            "entity.bus": ["record_id"],
        },
    )
    lines = render_keys_block(proposal)
    assert lines[0] == "keys:"
    assert lines[1] == "  entity:"
    assert lines[2].startswith("    # NOTE:")
    assert lines[3] == "    # driver: record_id"
    assert lines[4] == "    # driver: presentation_id"
    assert lines[5] == "    driver: record_index"
    assert lines[6].startswith("    # NOTE:")
    assert lines[7] == "    # bus: record_id"
    assert lines[8] == "    bus: record_index"
    assert lines[-1] == ""


def test_render_preserves_active_declaration_order() -> None:
    proposal = KeyElectionProposal(
        active={"zebra": "record_index", "alpha": "record_index"},
        alternatives={"zebra": ["record_id"], "alpha": ["record_id"]},
    )
    lines = render_keys_block(proposal)
    names_in_order = [
        line.split(":")[0].strip()
        for line in lines
        if line.strip().endswith("record_index") and not line.strip().startswith("#")
    ]
    assert names_in_order == ["zebra", "alpha"]


# ---------------------------------------------------------------------------
# Cross-mode: propose_key_election + render_keys_block == every engine's
# spliced `keys:` block
# ---------------------------------------------------------------------------

_PARTIAL_PRESENTATION_KEYS_FOR_ACTOR: dict[str, object] = {
    "actor": {
        "sub_types": {
            "driver": {
                "unique_within": "branch",
                "branch_stable": True,
                "slice_stable": True,
                "key_space": {"class": "record_index", "prefix": "DRV_", "width": 4},
            }
        },
        "unique_within": "branch",
        "branch_stable": True,
        "slice_stable": True,
    }
}


def _create_table_ddl(table_name: str, columns: list[dict[str, object]]) -> str:
    """Build a `CREATE TABLE` statement from a sidecar-shaped column list."""
    col_ddl = ", ".join(f'"{c["name"]}" {c["type"]}' for c in columns)
    return f'CREATE TABLE "{table_name}" ({col_ddl})'


def _build_cross_mode_emit(tmp_path: Path) -> Path:
    """A flat undeclared kind + a partitioned kind with one declared
    sub-type -- exercises both proposal shapes for the byte-identical
    cross-mode splice check."""
    widget_columns: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        prop_column(
            "prop__status", "VARCHAR", history_tracked=False, temporal_class="constant"
        ),
    ]
    actor_columns: list[dict[str, object]] = [
        identity_column("fork_path", "VARCHAR"),
        identity_column("record_id", "VARCHAR"),
        {"name": "presentation_id", "type": "BIGINT"},
        {"name": "created_sim_time", "type": "BIGINT"},
        {"name": "active", "type": "BOOLEAN"},
        {"name": "deactivated_at", "type": "BIGINT"},
        {"name": "last_mutation_sim_time", "type": "BIGINT"},
        identity_column("record_index", "BIGINT"),
        prop_column(
            "prop__actor_type",
            "VARCHAR",
            history_tracked=False,
            temporal_class="constant",
        ),
    ]
    conn = duckdb.connect(str(tmp_path / "run.duckdb"))
    conn.execute(_create_table_ddl("records__widget", widget_columns))
    conn.execute(
        'INSERT INTO "records__widget" VALUES (?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "w1", 10, True, 10, 0, "active"],
    )
    conn.execute(_create_table_ddl("records__actor", actor_columns))
    conn.execute(
        'INSERT INTO "records__actor" VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)',
        ["trunk", "a1", 1, 10, True, 10, 0, "driver"],
    )
    conn.close()
    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__widget",
                "category": "records",
                "record_kind": "widget",
                "columns": widget_columns,
                "rows": 1,
            },
            {
                "name": "records__actor",
                "category": "records",
                "record_kind": "actor",
                "columns": actor_columns,
                "rows": 1,
            },
        ],
        branches=[{"fork_path": "trunk", "parent": None, "slice_at": 100}],
        extra={
            "enum_domains": {"actor": {"actor_type": ["driver", "bus"]}},
            "presentation_keys": _PARTIAL_PRESENTATION_KEYS_FOR_ACTOR,
            "record_roles": {"widget": "dimension", "actor": "dimension"},
        },
    )
    return tmp_path


def _extract_keys_block(content: str) -> str:
    """The `keys:` block substring, from `keys:` through its trailing blank line."""
    start = content.index("keys:")
    end = content.index("\n\n", start) + 1
    return content[start:end]


def test_propose_and_render_matches_all_three_mode_engines(tmp_path: Path) -> None:
    """`propose_key_election` + `render_keys_block`, called directly against
    the emit's sidecar, produce the exact `keys:` block every mode's `init`
    engine splices -- and all three splice byte-identical blocks."""
    emit_dir = _build_cross_mode_emit(tmp_path)
    with open_emit(emit_dir) as emit:
        expected = "\n".join(render_keys_block(propose_key_election(emit.sidecar)))
        dimensional_content = generate_init_config(emit, discard_notice_sink)
        source_content = generate_source_init_config(emit, discard_notice_sink)
        streaming_content = generate_stream_init_config(emit, discard_notice_sink)

    dim_block = _extract_keys_block(dimensional_content)
    source_block = _extract_keys_block(source_content)
    stream_block = _extract_keys_block(streaming_content)

    assert dim_block == source_block == stream_block
    assert dim_block.rstrip("\n") == expected.rstrip("\n")
