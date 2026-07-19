"""Runtime working-set types for the corrupter engine.

`WorkingTable` / `CorruptState` hold the in-flight working set threaded through
the operations, in place; `OperationOutcome` / `CorruptReport` are the
reportable result of applying one operation, and a full corrupt run. See
`docs/architecture/pending/corrupter-engine-and-manifest.md` § Runtime Types
(normative).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fabulexa_forge.errors import CorruptError

if TYPE_CHECKING:
    import pyarrow

    from fabulexa_forge.corrupters.manifest import DefectRecord
    from fabulexa_forge.reader.sidecar import TableSpec


@dataclass
class WorkingTable:
    """One table's in-flight state as operations mutate it."""

    spec: "TableSpec"
    """The table's evolving descriptor (columns change under schema_drift; rows
    under duplicate_rows)."""
    data: "pyarrow.Table"
    """The table's evolving row content."""


def _table_record_index_mark(working_table: "WorkingTable") -> int | None:
    """The `record_index` high-water mark captured for one records-category
    working table, at the instant it is captured.

    Args:
        working_table: One records-category WorkingTable, at capture time.

    Returns:
        The table's maximum `record_index` value, `-1` when it carries zero
        rows (the first mint then yields `0`), or `None` when the table
        carries no `record_index` column at all -- unreachable for a
        records-category table on a conformant source (C5 requires
        `record_index` on every one), kept so the accessor stays total rather
        than assuming the invariant.
    """
    if "record_index" not in working_table.data.column_names:
        return None
    if working_table.data.num_rows == 0:
        return -1
    values: list[int] = working_table.data.column("record_index").to_pylist()
    return max(values)


@dataclass
class CorruptState:
    """The full working set threaded through the ordered operations."""

    tables: dict[str, "WorkingTable"]
    """Every source table by name; operations replace the entries they touch."""
    deleted_record_ids: dict[str, set[str]] = field(default_factory=dict)
    """Tombstones: kind -> the `record_id`s earlier `delete_rows` operations
    removed from `records__<K>`. Starts empty; written only by the
    `delete_rows` handler, read only by `insert_rows`' id universe. The
    engine, writer, and manifest builder ignore it -- it never reaches any
    output artifact."""
    _record_index_marks: dict[str, int] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    """Per-`records__<K>` table `record_index` high-water mark, captured at
    working-set load (`__post_init__`, before any operation can mutate a
    table) and advanced only forward by `mint_record_index` -- never
    recomputed from a later (possibly deletion-shrunk) table state, so a
    tombstoned ordinal (a deleted suffix row's index included) is never
    reused. Absent for a table carrying no `record_index` column."""

    def __post_init__(self) -> None:
        for name, working_table in self.tables.items():
            if working_table.spec.category != "records":
                continue
            mark = _table_record_index_mark(working_table)
            if mark is not None:
                self._record_index_marks[name] = mark

    def mint_record_index(self, table_name: str) -> int:
        """Mint the next fresh `record_index` for `table_name`, advancing its
        working high-water mark past the minted value.

        Design doc § Semantics, `insert_rows` -- the minting rule: per-table
        ordinal high-water mark `+ 1` per phantom, in assignment order; the
        mark is never lowered, so a deletion gap (a tombstoned suffix
        included) is never reused.

        Args:
            table_name: The records-category table receiving one phantom row.

        Returns:
            The freshly minted `record_index`.

        Raises:
            CorruptError: `table_name` carries no captured high-water mark --
                an engine invariant breach (a records-category `insert_rows`
                target with no `record_index` column), not a config error.
        """
        mark = self._record_index_marks.get(table_name)
        if mark is None:
            raise CorruptError(
                f"mint_record_index: no record_index high-water mark captured"
                f" for table {table_name!r}"
            )
        mark += 1
        self._record_index_marks[table_name] = mark
        return mark


@dataclass(frozen=True)
class OperationOutcome:
    """The reportable result of one applied operation, and the defects it declared.

    units_selected is the size of the sampled set draw_sample returned — the units
    the operation *intended* to corrupt, pooled across every resolved table.
    units_affected is how many selected units actually changed stored state — the
    same unit amount pooled over. The `== len(defects)` equality holds only where
    unit and act coincide: a selected unit already in the target state does not
    count and emits no defect (nulling an already-NULL cell; schema_drift is
    counted per column changed). For duplicate_rows and dangle_reference the two
    are equal by construction (every duplicate adds a row; the dangle population
    excludes already-NULL ids, rows whose partner member__<f>__kind is NULL, and
    rows whose target records table is absent from the working set — every
    unresolvable reference). schema_drift samples nothing (it carries no amount):
    both counts equal the number of columns it names across rename_to / retype_to
    / drop, each of which changes. drop_events keeps the equality (one defect per
    removed row). freeze_series counts one series unit while emitting one defect
    per removed row, breaking the equality — every selected series removes at
    least one timeline row, so units_affected always equals units_selected for
    this operation even though len(defects) does not. shift_sim_time honors
    the family-wide no-mutation rule from the other direction: a selected unit
    whose offset delta rounds to zero, whose swap pairs with a
    predecessor-tick partner sharing its value (an equal-value swap), or whose
    swap is skipped because either row was already rewritten this operation (a
    chained swap) changed nothing in stored state, so it is not counted in
    units_affected and emits no defect. A performed swap counts as one unit
    while emitting two defects, one per rewritten row. defects are the
    label-grade DefectRecords this operation declares — the single seam:
    corrupt_emit collects them across all operations and passes them to
    build_defect_manifest. The driver never infers defects by diffing;
    operations are the sole source of truth.
    """

    kind: str
    tables: tuple[str, ...]
    """The operation's resolved table set, canonical (lexicographic) order; a
    single-table operation carries a 1-tuple."""
    units_selected: int
    units_affected: int
    defects: tuple["DefectRecord", ...]


@dataclass(frozen=True)
class CorruptReport:
    """The per-operation outcomes of a corrupt run, in application order."""

    outcomes: tuple[OperationOutcome, ...]
