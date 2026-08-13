"""Tests for `SourceTableDecl.where` plan-time resolution: the
constant-column gate (`_resolve_where_selection`), castability
(`SourceWhereValueUncastable`), and the out-of-domain notice
(`_check_where_values_observed`) — `exporters/source/plan.py`, source-row-
selection sprint § Phase 1.

Every fixture is a real (DuckDB-backed) emit: the gate-matrix and domain-
notice cases reuse `_source_fixtures.py`'s spanning fixtures (the deep
plan/fixture surface the `source` step's `_build_state_table_plan` delta
already reshapes); the castability cases need a `constant`-class BIGINT
payload property no spanning fixture declares, so they use a small bespoke
bare emit (0 rows — no gate here consults row data), mirroring
`test_election_plan.py`'s own "gate-only" bespoke-emit convention.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pytest
from _support.notices import RecordingNoticeSink, discard_notice_sink
from _support.sidecar_builder import identity_column, prop_column, write_emit

from fabulexa_forge.anchor import resolve_effective_anchor
from fabulexa_forge.config.models import ExportConfig, SourceConfig, SourceTableDecl
from fabulexa_forge.errors import (
    SourceWhereColumnUnresolved,
    SourceWhereNotConstant,
    SourceWhereOnDiscriminator,
    SourceWhereValueUncastable,
)
from fabulexa_forge.exporters.election import resolve_election
from fabulexa_forge.exporters.source.plan import SourceStateTablePlan, build_source_plan
from fabulexa_forge.reader.emit import open_emit

from ._source_fixtures import build_slice_only_source_emit, build_source_test_emit

if TYPE_CHECKING:
    from fabulexa_forge.exporters.notices import NoticeSink
    from fabulexa_forge.exporters.source.plan import SourcePlan

# ---------------------------------------------------------------------------
# Config + plan-build helpers
# ---------------------------------------------------------------------------


def _config(tables: "tuple[SourceTableDecl, ...]") -> ExportConfig:
    """Build a `mode: source` ExportConfig from a declared table set."""
    return ExportConfig(mode="source", source=SourceConfig(tables=tables))


def _open_plan(
    emit_dir: Path,
    config: ExportConfig,
    *,
    notice_sink: "NoticeSink" = discard_notice_sink,
) -> "SourcePlan":
    """Open `emit_dir` and build a SourcePlan against it, resolving the
    anchor and election the way the engine does."""
    with open_emit(emit_dir) as emit:
        anchor = resolve_effective_anchor(emit.sidecar.runtime(), None, None, None)
        assert anchor is not None, "every fixture here declares a runtime block"
        election = resolve_election(emit.sidecar, None)
        return build_source_plan(emit, config, anchor, election, False, notice_sink)


def _state(plan: "SourcePlan", name: str) -> SourceStateTablePlan:
    """The sole `state` unit named `name`."""
    table = next(t for t in plan.tables if t.name == name)
    assert isinstance(table, SourceStateTablePlan)
    return table


# ---------------------------------------------------------------------------
# Gate matrix (doc § The constant-column gate, all seven rows)
# ---------------------------------------------------------------------------


def test_where_constant_property_accepted(tmp_path: Path) -> None:
    """A `constant`-class, non-discriminator payload property resolves: one
    `SourceWhereEntry` carrying the source column, sidecar type, verbatim
    value, and its typed cast."""
    tables = (
        SourceTableDecl(name="loc", kind="location", where={"prop__name": "Ward A"}),
    )
    plan = _open_plan(build_source_test_emit(tmp_path), _config(tables))
    table = _state(plan, "loc")
    assert len(table.where) == 1
    entry = table.where[0]
    assert entry.key == "prop__name"
    assert entry.source_column == "prop__name"
    assert entry.sql_type == "VARCHAR"
    assert entry.value == "Ward A"
    assert entry.typed_values == ("Ward A",)


def test_where_tracked_column_refused(tmp_path: Path) -> None:
    """A `tracked`-class column is refused with the tracked message variant."""
    tables = (SourceTableDecl(name="v", kind="visit", where={"prop__status": "open"}),)
    with pytest.raises(SourceWhereNotConstant, match="temporal_class: tracked"):
        _open_plan(build_source_test_emit(tmp_path), _config(tables))


def test_where_slice_only_column_refused(tmp_path: Path) -> None:
    """A `slice_only`-class column is refused with the slice_only message
    variant — `SourceSliceOnlyRead`'s population never extends to `where`."""
    tables = (
        SourceTableDecl(name="p", kind="patient", where={"prop__loyalty_tier": "gold"}),
    )
    with pytest.raises(SourceWhereNotConstant, match="temporal_class: slice_only"):
        _open_plan(build_slice_only_source_emit(tmp_path), _config(tables))


def test_where_on_discriminator_refused(tmp_path: Path) -> None:
    """A `where` key naming the subject kind's discriminator is refused,
    pointing at `sub_types`."""
    tables = (
        SourceTableDecl(name="s", kind="shift", where={"prop__shift_type": "day"}),
    )
    with pytest.raises(SourceWhereOnDiscriminator, match="sub_types, not where"):
        _open_plan(build_source_test_emit(tmp_path), _config(tables))


def test_where_structural_column_unresolved(tmp_path: Path) -> None:
    """A structural column (`record_id`) is not a payload property and is
    refused — `SourceWhereColumnUnresolved`, not any other class."""
    tables = (SourceTableDecl(name="v", kind="visit", where={"record_id": "v001"}),)
    with pytest.raises(SourceWhereColumnUnresolved, match="not a payload property"):
        _open_plan(build_source_test_emit(tmp_path), _config(tables))


def test_where_unknown_column_unresolved(tmp_path: Path) -> None:
    """A key naming no column of the subject kind is refused."""
    tables = (
        SourceTableDecl(name="loc", kind="location", where={"prop__nonexistent": "x"}),
    )
    with pytest.raises(SourceWhereColumnUnresolved, match="not a payload property"):
        _open_plan(build_source_test_emit(tmp_path), _config(tables))


def test_where_bare_name_missing_prefix_unresolved(tmp_path: Path) -> None:
    """A `kind:` table's `where` key must carry the `prop__` prefix; a bare
    name is refused as unresolved, never silently prefixed."""
    tables = (SourceTableDecl(name="loc", kind="location", where={"name": "Ward A"}),)
    with pytest.raises(SourceWhereColumnUnresolved, match="not a payload property"):
        _open_plan(build_source_test_emit(tmp_path), _config(tables))


# ---------------------------------------------------------------------------
# Bespoke bare emit: a `constant`-class BIGINT property no spanning fixture
# declares (castability), plus a non-discriminator enum_domains entry no
# spanning fixture declares (the out-of-domain notice).
# ---------------------------------------------------------------------------

_SENSOR_COLUMNS: "list[dict[str, object]]" = [
    identity_column("fork_path", "VARCHAR"),
    identity_column("record_id", "VARCHAR"),
    {"name": "created_sim_time", "type": "BIGINT"},
    {"name": "active", "type": "BOOLEAN"},
    {"name": "deactivated_at", "type": "BIGINT"},
    {"name": "last_mutation_sim_time", "type": "BIGINT"},
    identity_column("record_index", "BIGINT"),
    prop_column(
        "prop__reading", "BIGINT", history_tracked=False, temporal_class="constant"
    ),
    prop_column(
        "prop__category", "VARCHAR", history_tracked=False, temporal_class="constant"
    ),
]


def _col_ddl(col: "dict[str, object]") -> str:
    """Build a single column DDL fragment (name + type only)."""
    return f'"{col["name"]}" {col["type"]}'


def _write_sensor_emit(tmp_path: Path) -> Path:
    """A flat `sensor` kind (0 rows): `prop__reading` (BIGINT, constant) for
    castability, `prop__category` (VARCHAR, constant) with a declared
    enum_domains entry for the out-of-domain notice."""
    db_path = tmp_path / "run.duckdb"
    conn = duckdb.connect(str(db_path))
    col_fragments = ", ".join(_col_ddl(c) for c in _SENSOR_COLUMNS)
    conn.execute(f'CREATE TABLE "records__sensor" ({col_fragments})')
    conn.close()
    write_emit(
        tmp_path,
        tables=[
            {
                "name": "records__sensor",
                "category": "records",
                "record_kind": "sensor",
                "columns": _SENSOR_COLUMNS,
                "rows": 0,
            }
        ],
        extra={
            "runtime": {
                "timezone": "UTC",
                "start_datetime": "2024-01-01T00:00:00+00:00",
            },
            "enum_domains": {"sensor": {"category": ["indoor", "outdoor"]}},
        },
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Castability
# ---------------------------------------------------------------------------


def test_where_uncastable_element_refused(tmp_path: Path) -> None:
    """A non-numeric element on a BIGINT column is refused, naming the
    element — before any write."""
    tables = (SourceTableDecl(name="s", kind="sensor", where={"prop__reading": "abc"}),)
    with pytest.raises(
        SourceWhereValueUncastable, match="'abc'.*does not cast to BIGINT"
    ):
        _open_plan(_write_sensor_emit(tmp_path), _config(tables))


def test_where_castable_element_resolves_typed_value(tmp_path: Path) -> None:
    """A castable BIGINT element resolves to its typed (int) value."""
    tables = (SourceTableDecl(name="s", kind="sensor", where={"prop__reading": "42"}),)
    plan = _open_plan(_write_sensor_emit(tmp_path), _config(tables))
    table = _state(plan, "s")
    assert table.where[0].typed_values == (42,)


# ---------------------------------------------------------------------------
# Domain notice (discriminator-value-unobserved, never an error)
# ---------------------------------------------------------------------------


def test_where_scalar_observed_emits_no_notice(tmp_path: Path) -> None:
    """A scalar value inside the declared enum_domains entry emits nothing."""
    tables = (
        SourceTableDecl(name="s", kind="sensor", where={"prop__category": "indoor"}),
    )
    sink = RecordingNoticeSink()
    _open_plan(_write_sensor_emit(tmp_path), _config(tables), notice_sink=sink)
    assert sink.notices == []


def test_where_scalar_unobserved_emits_one_notice_no_rows(tmp_path: Path) -> None:
    """A scalar value outside the domain emits one notice stating the unit
    renders no rows — never an error."""
    tables = (
        SourceTableDecl(
            name="s", kind="sensor", where={"prop__category": "underground"}
        ),
    )
    sink = RecordingNoticeSink()
    plan = _open_plan(_write_sensor_emit(tmp_path), _config(tables), notice_sink=sink)
    assert _state(plan, "s").where  # the gate passed; only the value notices
    assert len(sink.notices) == 1
    assert sink.notices[0].code == "discriminator-value-unobserved"
    assert sink.notices[0].message == (
        "table 's': where value 'underground' for 'prop__category' not"
        " observed; the unit renders no rows"
    )


def test_where_list_wholly_unobserved_emits_one_notice_per_element(
    tmp_path: Path,
) -> None:
    """A list with no element observed emits one notice per element, config
    element order, each keeping the "renders no rows" wording."""
    tables = (
        SourceTableDecl(
            name="s",
            kind="sensor",
            where={"prop__category": ["underground", "space"]},
        ),
    )
    sink = RecordingNoticeSink()
    _open_plan(_write_sensor_emit(tmp_path), _config(tables), notice_sink=sink)
    assert [n.code for n in sink.notices] == ["discriminator-value-unobserved"] * 2
    assert "underground" in sink.notices[0].message
    assert "renders no rows" in sink.notices[0].message
    assert "space" in sink.notices[1].message
    assert "renders no rows" in sink.notices[1].message


def test_where_list_partially_observed_emits_only_unobserved_elements(
    tmp_path: Path,
) -> None:
    """A list with some elements observed emits one notice per unobserved
    element only, config order, with the weaker "contributes no rows"
    wording — the unit is not, in fact, empty."""
    tables = (
        SourceTableDecl(
            name="s",
            kind="sensor",
            where={"prop__category": ["indoor", "underground", "outdoor", "space"]},
        ),
    )
    sink = RecordingNoticeSink()
    _open_plan(_write_sensor_emit(tmp_path), _config(tables), notice_sink=sink)
    assert len(sink.notices) == 2
    assert "underground" in sink.notices[0].message
    assert "contributes no rows" in sink.notices[0].message
    assert "space" in sink.notices[1].message
    assert "contributes no rows" in sink.notices[1].message


def test_where_column_absent_from_registry_emits_no_notice(tmp_path: Path) -> None:
    """A `where` column with no `enum_domains` entry is unchecked, whatever
    value it carries."""
    tables = (SourceTableDecl(name="s", kind="sensor", where={"prop__reading": "5"}),)
    sink = RecordingNoticeSink()
    _open_plan(_write_sensor_emit(tmp_path), _config(tables), notice_sink=sink)
    assert sink.notices == []
