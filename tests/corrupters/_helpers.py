"""Shared test fixture builders for the corrupters test package.

Builds `TableSpec` / `ColumnSpec` / `WorkingTable` instances directly (no
DuckDB source emit involved), for tests that exercise the selector and
business-rule surfaces in isolation. Module-level, non-test (`_`-prefixed) so
pytest never collects it.
"""

from __future__ import annotations

import random
from typing import Mapping, Sequence

import pyarrow as pa

from fabulexa_forge.corrupters.state import WorkingTable
from fabulexa_forge.reader.sidecar import BranchEntry, ColumnSpec, Sidecar, TableSpec

_PA_TYPES: dict[str, pa.DataType] = {
    "BIGINT": pa.int64(),
    "DOUBLE": pa.float64(),
    "BOOLEAN": pa.bool_(),
    "VARCHAR": pa.string(),
}


def column_spec(
    name: str,
    duckdb_type: str,
    *,
    references: str | None = None,
    history_tracked: bool | None = None,
) -> ColumnSpec:
    """Build one ColumnSpec."""
    return ColumnSpec(
        name=name,
        type=duckdb_type,
        references=references,
        history_tracked=history_tracked,
    )


def table_spec(
    name: str,
    category: str,
    columns: tuple[ColumnSpec, ...],
    *,
    record_kind: str | None = None,
    property_: str | None = None,
    rows: int = 0,
) -> TableSpec:
    """Build one TableSpec."""
    return TableSpec(
        name=name,
        category=category,
        record_kind=record_kind,
        property=property_,
        columns=columns,
        rows=rows,
    )


def working_table(
    spec: TableSpec, rows: Sequence[Mapping[str, object]]
) -> WorkingTable:
    """Build a WorkingTable whose Arrow content is `rows`, typed per `spec`.

    Args:
        spec: The table's descriptor; also fixes the Arrow column order/types.
        rows: Row dicts keyed by column name; a missing key means NULL.

    Returns:
        A WorkingTable with an Arrow table matching `spec`'s columns.

    A DuckDB type outside `_PA_TYPES` (e.g. a non-round-trippable type used
    only to exercise a business rule, never queried for its actual content)
    falls back to `pa.string()` — a test-fixture convenience, not a scenario
    value.
    """
    arrow_types = {
        col.name: _PA_TYPES.get(col.type.upper(), pa.string()) for col in spec.columns
    }
    arrays = {
        col.name: pa.array(
            [row.get(col.name) for row in rows], type=arrow_types[col.name]
        )
        for col in spec.columns
    }
    schema = pa.schema([(col.name, arrow_types[col.name]) for col in spec.columns])
    data = pa.table(arrays, schema=schema)
    return WorkingTable(spec=spec, data=data)


def sidecar(
    tables: tuple[TableSpec, ...],
    *,
    enum_domains: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None,
    pinned_ids: Mapping[str, Mapping[str, str]] | None = None,
    branches: tuple[BranchEntry, ...] = (),
) -> Sidecar:
    """Build a Sidecar directly from TableSpecs (no base.json round-trip).

    Args:
        tables: The tables this sidecar declares.
        enum_domains: The closed-domain registry, for enum_domains discriminator
            tests; empty when omitted.
        pinned_ids: The pin surface {kind: {label: id}}, for C9 tests; empty
            when omitted.
        branches: The declared branches; empty when omitted (tests that never
            resolve a branch's slice_at don't need one).

    Returns:
        A Sidecar exposing exactly `tables`, `branches`, `enum_domains`, and
        `pinned_ids`.
    """
    return Sidecar(
        raw={},
        base_format_version=4,
        branches=branches,
        tables=tables,
        runtime=None,
        pinned_ids=pinned_ids or {},
        enum_domains=enum_domains or {},
        record_roles=None,
    )


class FixedSampleRandom(random.Random):
    """A `random.Random` whose `.sample()` deterministically returns a fixed
    subsequence; every other draw (`.random()`, `.uniform()`, ...) uses the
    real stream seeded from `seed`.

    For placement handler tests: pins the placement setup draw (the
    `entity_scoped` subset / `clustered_temporal` centers) so the test's
    `amount` can be chosen to make the following weighted unit draw's outcome
    forced (e.g. `amount.count` equal to the positive-weight unit count)
    rather than dependent on the seed's actual `u ** (1/w)` keys.
    """

    def __init__(self, chosen: Sequence[object], seed: int = 0) -> None:
        super().__init__(seed)
        self._chosen = list(chosen)

    def sample(
        self, population: Sequence[object], k: int, *, counts: object = None
    ) -> list[object]:
        return list(self._chosen)


class CallOrderRandom(random.Random):
    """A `random.Random` subclass recording each `.sample()` / `.random()` /
    `.randrange()` call's kind, in call order — for asserting RNG draw order
    (placement setup before the unit draw before mode draws) without
    depending on the numeric stream. `randrange_calls` additionally records
    each `.randrange()` call's `(start, stop)` arguments, in call order — for
    asserting per-unit mode-draw ranges (e.g. `freeze_series`' `cut: random`
    `rng.randrange(1, N)`).
    """

    def __init__(self, seed: int = 0) -> None:
        super().__init__(seed)
        self.calls: list[str] = []
        self.randrange_calls: list[tuple[int, int]] = []

    def sample(
        self, population: Sequence[object], k: int, *, counts: object = None
    ) -> list[object]:
        self.calls.append("sample")
        return super().sample(population, k, counts=counts)

    def random(self) -> float:
        self.calls.append("random")
        return super().random()

    def randrange(self, start: int, stop: int | None = None, step: int = 1) -> int:
        self.calls.append("randrange")
        if stop is not None:
            self.randrange_calls.append((start, stop))
        return super().randrange(start, stop, step)
