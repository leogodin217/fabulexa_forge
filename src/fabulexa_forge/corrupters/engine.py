"""The corrupter engine: `corrupt_emit`.

Guards single-branch, enforces the conformant-source precondition, checks the
emit-dependent business rules, materializes the working set, threads the
operations over it with per-operation seeded RNG sub-streams, hands the result
to the base-emit writer, and assembles + writes the defect manifest. See
`docs/architecture/pending/corrupter-engine-and-manifest.md` § Solution, §
Determinism and canonical ordering (normative).
"""

from __future__ import annotations

import hashlib
import random
from typing import TYPE_CHECKING

from fabulexa_forge import __version__
from fabulexa_forge._sql import quote_identifier
from fabulexa_forge.corrupters.base_writer import write_base_emit
from fabulexa_forge.corrupters.fingerprint import fingerprint_config
from fabulexa_forge.corrupters.manifest import DefectSource
from fabulexa_forge.corrupters.manifest_build import (
    build_defect_manifest,
    write_defect_manifest,
)
from fabulexa_forge.corrupters.operations import CORRUPTER_REGISTRY
from fabulexa_forge.corrupters.state import CorruptReport, CorruptState, WorkingTable
from fabulexa_forge.corrupters.validate import validate_corrupt_config
from fabulexa_forge.derivations.guard import require_single_branch
from fabulexa_forge.errors import CorruptValidationError
from fabulexa_forge.reader.conformance import validate as run_conformance

if TYPE_CHECKING:
    from pathlib import Path

    from fabulexa_forge.config.models import CorruptConfig, CorruptOperation
    from fabulexa_forge.corrupters.manifest import DefectRecord
    from fabulexa_forge.corrupters.state import OperationOutcome
    from fabulexa_forge.reader.emit import Emit


def _sidecar_sha256(emit: "Emit") -> str:
    """The SHA-256 hex digest of the source emit's base.json bytes.

    The corrupter family's own small helper following the sidecar-fingerprint
    convention `incremental/driver.py`'s private `_compute_sidecar_sha256`
    uses (hex digest of the emit's base.json bytes on disk) -- corrupters
    carry their own copy rather than importing that private helper.

    Args:
        emit: The open source emit.

    Returns:
        64-character lowercase hex digest.
    """
    base_json_path = emit.emit_dir / "base.json"
    return hashlib.sha256(base_json_path.read_bytes()).hexdigest()


def _check_source_conformant(emit: "Emit") -> None:
    """Refuse a source emit that fails any C1-C12 check.

    Args:
        emit: The open source emit.

    Raises:
        CorruptValidationError: Naming every failing check id.
    """
    report = run_conformance(emit)
    failing = [result.check for result in report.results if not result.passed]
    if failing:
        raise CorruptValidationError(
            f"source emit is not C1-C12 conformant; failing checks: {failing}"
        )


def _check_out_dir_available(out_dir: "Path") -> None:
    """Refuse an out_dir that already holds a run.duckdb or base.json.

    Args:
        out_dir: The destination directory.

    Raises:
        CorruptValidationError: `out_dir` already holds a `run.duckdb` or
            `base.json`.
    """
    if (out_dir / "run.duckdb").exists() or (out_dir / "base.json").exists():
        raise CorruptValidationError(
            f"out_dir {out_dir} already holds a run.duckdb or base.json;"
            " corrupt_emit refuses to overwrite an existing emit"
        )


def _materialize_state(emit: "Emit") -> CorruptState:
    """Materialize every source table into a `CorruptState`, verbatim.

    The corrupter's one faithful read: `Emit.query_arrow` over each sidecar
    table, in the sidecar's own (DuckDB-catalog) order. `CorruptState.tables`
    is a dict, whose insertion order therefore *is* source table order --
    `write_base_emit` reuses that order directly.

    Args:
        emit: The open source emit.

    Returns:
        A `CorruptState` with one `WorkingTable` per sidecar table.

    Raises:
        RunDatabaseError: Reading a source table fails.
    """
    tables: dict[str, WorkingTable] = {}
    for table_spec in emit.sidecar.tables():
        data = emit.query_arrow(
            f"SELECT * FROM {quote_identifier(table_spec.name)}", ()
        )
        tables[table_spec.name] = WorkingTable(spec=table_spec, data=data)
    return CorruptState(tables=tables)


def _check_not_totally_erased(state: CorruptState) -> None:
    """Refuse a working set left with zero rows across every table.

    A working set with no row anywhere carries no row bearing the branch's
    fork_path, so C8's data/sidecar set-equality would fail -- the one
    apply-time guard total erasure (row-set operations composed with
    family-C erasure) requires (design doc § Validation Rules).

    Args:
        state: The working set after the last operation.

    Raises:
        CorruptValidationError: Every table's row count is 0.
    """
    if all(table.data.num_rows == 0 for table in state.tables.values()):
        raise CorruptValidationError(
            "corrupt run erased every row across every table; the output"
            " would carry no row bearing the branch's fork_path (C8 would"
            " fail)"
        )


def _resolve_rule(operation: "CorruptOperation", index: int) -> str:
    """The `rule` label for one operation: its `name`, or the fallback.

    Args:
        operation: The parsed operation.
        index: The operation's 0-based position in `config.operations`.

    Returns:
        `operation.name` when set, else `"{kind}#{index}"`.
    """
    return operation.name if operation.name is not None else f"{operation.kind}#{index}"


def _operation_rng(seed: int, index: int) -> "random.Random":
    """The operation's deterministic RNG sub-stream, seeded from `(seed, index)`.

    Uses a stable string combiner fed to `random.Random` -- never Python's
    per-process-salted builtin `hash()`.

    Args:
        seed: The config's master seed.
        index: The operation's 0-based position.

    Returns:
        A `random.Random` seeded deterministically for this operation.
    """
    return random.Random(f"{seed}:{index}")


def corrupt_emit(
    emit: "Emit", config: "CorruptConfig", out_dir: "Path"
) -> CorruptReport:
    """Apply a corrupter config to an open emit and write the broken emit + defect
    manifest.

    Guards single-branch, verifies the source emit is C1-C12 conformant
    (conformance.validate; a corrupter refuses to interpret a non-conformant input --
    the agreement invariant's precondition), runs the emit-dependent business rules,
    materializes every source table into a CorruptState, threads the operations in
    list order (each with its own seeded RNG sub-stream and resolved `rule` label),
    hands the result to write_base_emit, then collects every operation's declared
    DefectRecords, computes the manifest provenance (source sidecar SHA-256 +
    base_format_version, the config fingerprint via fingerprint_config, and the code
    version), calls build_defect_manifest, and writes defects.json beside run.duckdb
    + base.json via write_defect_manifest. Every corrupt run writes the manifest. The
    output is a structurally-conformant (C1-C5, C8) v4 base emit that intentionally
    fails the semantic checks its operations targeted.

    out_dir is created if absent; if it already holds a run.duckdb or base.json the
    run refuses rather than overwrite (a corrupt run never clobbers an existing emit,
    as the incremental range path refuses a populated target). The CLI verb
    `fabulexa-forge corrupt <emit_dir> --config <corrupt.yaml> --out <out_dir>` wraps
    this; its handler catches (ReaderError, ExporterError) and exits 1, exactly as
    cmd_export does.

    Args:
        emit: The open, read-only source emit.
        config: The validated corrupter config.
        out_dir: Destination directory for run.duckdb + base.json + defects.json.

    Returns:
        The per-operation report, in application order.

    Raises:
        ExportError: The emit is not single-branch (trunk-only stage; from
            require_single_branch).
        CorruptValidationError: The source emit fails a C1-C12 check, a business
            rule fails (checked against the per-operation evolved schema), a
            retype cast is impossible, out_dir already holds an emit, or the
            working set is left with zero rows across every table after the
            last operation (the total-erasure guard).
        RunDatabaseError: Reading a source table via Emit.query_arrow fails (the
            reader failure domain).
        ExportRuntimeError: Writing the output run.duckdb / base.json /
            defects.json fails (the writer failure domain -- never the reader's
            RunDatabaseError).
    """
    fork_path = require_single_branch(emit.sidecar)
    _check_source_conformant(emit)
    validate_corrupt_config(config, emit.sidecar)
    _check_out_dir_available(out_dir)

    state = _materialize_state(emit)

    outcomes: list[OperationOutcome] = []
    records: list[DefectRecord] = []
    for index, operation in enumerate(config.operations):
        rule = _resolve_rule(operation, index)
        rng = _operation_rng(config.seed, index)
        handler = CORRUPTER_REGISTRY[operation.kind]
        outcome = handler.apply(state, operation, rule, rng, fork_path, emit.sidecar)
        outcomes.append(outcome)
        records.extend(outcome.defects)

    _check_not_totally_erased(state)
    write_base_emit(state, emit.sidecar.raw, out_dir)

    source = DefectSource(
        sidecar_sha256=_sidecar_sha256(emit),
        base_format_version=emit.sidecar.base_format_version,
    )
    manifest = build_defect_manifest(
        source=source,
        config_fingerprint=fingerprint_config(config),
        code_version=__version__,
        records=records,
    )
    write_defect_manifest(manifest, out_dir)

    return CorruptReport(outcomes=tuple(outcomes))
