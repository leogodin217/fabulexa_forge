"""Corrupter operation handlers: one implementation per `CorruptOperation` kind.

Houses the `Corrupter` protocol every handler implements, and the total
`CORRUPTER_REGISTRY` dispatch table `corrupt_emit` looks operations up in. See
`docs/architecture/pending/corrupter-engine-and-manifest.md` § Runtime Protocol
(normative).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from fabulexa_forge.corrupters.operations.dangle_reference import (
    DangleReferenceCorrupter,
)
from fabulexa_forge.corrupters.operations.delete_rows import DeleteRowsCorrupter
from fabulexa_forge.corrupters.operations.distort_intervals import (
    DistortIntervalsCorrupter,
)
from fabulexa_forge.corrupters.operations.drop_events import DropEventsCorrupter
from fabulexa_forge.corrupters.operations.duplicate_rows import (
    DuplicateRowsCorrupter,
)
from fabulexa_forge.corrupters.operations.freeze_series import FreezeSeriesCorrupter
from fabulexa_forge.corrupters.operations.insert_rows import InsertRowsCorrupter
from fabulexa_forge.corrupters.operations.mispoint_reference import (
    MispointReferenceCorrupter,
)
from fabulexa_forge.corrupters.operations.mutate_cells import MutateCellsCorrupter
from fabulexa_forge.corrupters.operations.null_cells import NullCellsCorrupter
from fabulexa_forge.corrupters.operations.schema_drift import SchemaDriftCorrupter
from fabulexa_forge.corrupters.operations.shift_sim_time import ShiftSimTimeCorrupter

if TYPE_CHECKING:
    import random

    from fabulexa_forge.config.models import CorruptOperation
    from fabulexa_forge.corrupters.state import CorruptState, OperationOutcome
    from fabulexa_forge.reader.sidecar import Sidecar


class Corrupter(Protocol):
    """One corrupter operation handler. One implementation per operation kind."""

    def apply(
        self,
        state: "CorruptState",
        operation: "CorruptOperation",
        rule: str,
        rng: "random.Random",
        fork_path: str,
        sidecar: "Sidecar",
    ) -> "OperationOutcome":
        """Apply this operation to the working set, in place, and declare its defects.

        Resolves the operation's population over the **current** `WorkingTable` in
        `state.tables` (its evolving Arrow and `WorkingTable.spec`, never the immutable
        source) narrowed to `fork_path` and `operation.target.where` (the `where`
        predicate evaluated via DuckDB over the registered working Arrow, reusing
        `render_typed_literal`'s coercion — see § *Selection is faithful*), imposes the
        canonical content order, draws the sampled units from `rng`, replaces the
        touched `WorkingTable` with the mutated table, and returns the
        OperationOutcome carrying one DefectRecord per atomic injection — each tagged
        with `rule`, its class, and its complete declared impact (the set of semantic
        codes it trips, derived per-defect from target metadata, config, and the
        working state the handler reads as of this operation; see § *The impact
        rule*, § What each operation breaks). Changes only cells/rows/columns named
        by the operation's target; every other table and column keeps identical
        content.

        Args:
            state: The shared working set; the handler replaces the entry it touches.
            operation: The config model for this operation (its own kind).
            rule: The label to stamp on each emitted DefectRecord (operation.name or
                fallback).
            rng: The operation's deterministic RNG sub-stream.
            fork_path: The sole branch's fork_path from the single-branch guard.
            sidecar: The **source** sidecar, for immutable metadata only — a column's
                original category/type, history_tracked flag, and a reference
                column's target kind. Current schema (post-drift) is read from
                `WorkingTable.spec`, not from here.

        Returns:
            The outcome (units selected vs affected, plus the declared DefectRecords).

        Raises:
            CorruptValidationError: A `retype_to` cast is impossible or names an
                unrecognized DuckDB type — the one business rule only the data can
                decide; every name and eligibility rule was already settled up front
                by validate_corrupt_config's evolved-schema simulation.
        """
        ...


CORRUPTER_REGISTRY: dict[str, Corrupter] = {
    "null_cells": NullCellsCorrupter(),
    "duplicate_rows": DuplicateRowsCorrupter(),
    "delete_rows": DeleteRowsCorrupter(),
    "insert_rows": InsertRowsCorrupter(),
    "schema_drift": SchemaDriftCorrupter(),
    "dangle_reference": DangleReferenceCorrupter(),
    "mispoint_reference": MispointReferenceCorrupter(),
    "drop_events": DropEventsCorrupter(),
    "freeze_series": FreezeSeriesCorrupter(),
    "shift_sim_time": ShiftSimTimeCorrupter(),
    "mutate_cells": MutateCellsCorrupter(),
    "distort_intervals": DistortIntervalsCorrupter(),
}
"""The total dispatch table `corrupt_emit` looks operations up in, keyed on
`CorruptOperation`'s `kind` discriminator. One entry per union member — the
discriminated union already rejects an unknown `kind` at parse time, so any
operation reaching the engine has a registered handler here."""
