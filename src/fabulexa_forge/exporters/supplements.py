"""Supplementary tables: an author-supplied table carried verbatim into a
dimensional export.

`load_supplements` is the filesystem step the config model does not take:
resolving each declared `SupplementDecl`'s data — a file or inline rows —
into text rows. The file is read exactly once, there; no later step touches
it again. `compile_supplement_specs` turns those text rows into a compiled
`QuerySpec` per supplement — a `VALUES`-and-cast relation over the session,
gated by the reserved-name rule, the TIMESTAMPTZ anchor rule, and a
before-any-write cell probe. `check_supplement_sources_not_outputs` refuses a
file supplement whose resolved source this invocation would overwrite.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from fabulexa_forge.config.models import canonical_supplement_type
from fabulexa_forge.errors import (
    ExportError,
    SupplementFileInvalid,
    SupplementFileMissing,
    SupplementHeaderMismatch,
    SupplementSourceIsOutput,
    SupplementValueInvalid,
    TemporalRenderRequiresAnchor,
)
from fabulexa_forge.exporters.query_spec import QuerySpec
from fabulexa_forge.exporters.reserved_names import is_reserved_table_name

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from fabulexa_forge.anchor import EffectiveAnchor
    from fabulexa_forge.config.models import ExportConfig, SupplementDecl
    from fabulexa_forge.reader.emit import Emit


@dataclass(frozen=True)
class SupplementSource:
    """Manifest-facing provenance of one supplement table: the declared
    (config-relative) file string and its SHA-256, both None for inline."""

    file: str | None
    sha256: str | None


@dataclass(frozen=True)
class ResolvedSupplement:
    """One supplement with its data resolved by the config's loader.

    `path` is the resolved absolute path for a file supplement (None for
    inline); `sha256` the hex digest of the file bytes (None for inline);
    `rows` the text rows — one tuple per data row in file / list order,
    one `str | None` cell per declared column in declared order (an empty
    CSV field or a YAML `null` is None; every inline scalar is rendered to
    text at load). `decl` is the declaration verbatim. Built once per
    invocation; the dimensional entry points, the incremental driver, the
    shaped playback head, and the pack builder all consume this — the file
    is never read again.
    """

    decl: SupplementDecl
    path: Path | None
    sha256: str | None
    rows: tuple[tuple[str | None, ...], ...]


def _render_inline_cell(value: str | int | float | None) -> str | None:
    """Render one inline-row cell to its text form.

    Args:
        value: The parsed YAML scalar (`str` / `int` / `float` / `None`;
            `bool` and temporal instances are refused at parse).

    Returns:
        `None` for `None`; the string itself for `str` (including `''`);
        decimal text for `int`; `repr` (shortest round-trip) for `float`.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    return repr(value)


def _load_inline_supplement(decl: SupplementDecl) -> ResolvedSupplement:
    """Resolve one `rows`-declared supplement to its text rows.

    Args:
        decl: The declaration; `rows` is known non-None (parse-time
            `supplement_source_exactly_one`).

    Returns:
        The resolved supplement — no `path` / `sha256`.
    """
    assert decl.rows is not None
    declared_names = list(decl.columns)
    text_rows = tuple(
        tuple(_render_inline_cell(row[name]) for name in declared_names)
        for row in decl.rows
    )
    return ResolvedSupplement(decl=decl, path=None, sha256=None, rows=text_rows)


def _resolve_supplement_file(
    decl: SupplementDecl, config_dir: Path
) -> tuple[Path, bytes]:
    """Resolve a supplement's `file` against `config_dir` and read its bytes.

    Args:
        decl: The declaration; `file` is known non-None (parse-time
            `supplement_source_exactly_one`).
        config_dir: The directory the config file was loaded from.

    Returns:
        The resolved absolute path and the file's raw bytes.

    Raises:
        SupplementFileMissing: The resolved path does not exist or is not a
            regular file.
    """
    assert decl.file is not None
    path = (config_dir / decl.file).resolve()
    if not path.is_file():
        raise SupplementFileMissing(f"supplement '{decl.name}': file not found: {path}")
    return path, path.read_bytes()


def _decode_supplement_text(decl: SupplementDecl, path: Path, raw_bytes: bytes) -> str:
    """Decode a supplement file's bytes as `utf-8-sig` (BOM stripped).

    Args:
        decl: The declaration, for the error message.
        path: The resolved path, for the error message.
        raw_bytes: The file's raw bytes.

    Returns:
        The decoded text.

    Raises:
        SupplementFileInvalid: The bytes are not valid UTF-8.
    """
    try:
        return raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SupplementFileInvalid(
            f"supplement '{decl.name}': file is not UTF-8: {path}"
        ) from exc


def _parse_supplement_csv(
    decl: SupplementDecl, path: Path, text: str
) -> list[list[str]]:
    """Parse decoded supplement text as strict RFC 4180 CSV.

    Args:
        decl: The declaration, for the error message.
        path: The resolved path, for the error message.
        text: The decoded file text.

    Returns:
        Every row (header included, when present) as a list of fields; empty
        for a zero-byte file.

    Raises:
        SupplementFileInvalid: The text does not parse as CSV under the
            strict dialect (e.g. an unterminated quote).
    """
    try:
        return list(csv.reader(io.StringIO(text), strict=True))
    except csv.Error as exc:
        raise SupplementFileInvalid(
            f"supplement '{decl.name}': file is not valid CSV: {path}: {exc}"
        ) from exc


def _header_mismatch_position(header: list[str], declared: list[str]) -> int | None:
    """The 0-based index of the first position `header` and `declared`
    differ, or None if they are equal. A shorter side reads as `''` at the
    positions it lacks.

    Args:
        header: The CSV header row's fields (empty when the file carries no
            header row at all).
        declared: The declaration's column names, in declared order.

    Returns:
        The first differing 0-based position, or None.
    """
    for i in range(max(len(header), len(declared))):
        h = header[i] if i < len(header) else ""
        d = declared[i] if i < len(declared) else ""
        if h != d:
            return i
    return None


def _check_supplement_header(decl: SupplementDecl, header: list[str]) -> None:
    """The CSV header equals the declared column names, in order, exactly.

    Args:
        decl: The declaration; `header` is checked against `decl.columns`'
            keys in declared order.
        header: The CSV header row's fields (empty when the file carries no
            header row at all — a mismatch at position 1).

    Raises:
        SupplementHeaderMismatch: They differ; names the 1-based first
            differing position and both spellings.
    """
    declared = list(decl.columns)
    position = _header_mismatch_position(header, declared)
    if position is None:
        return
    header_text = header[position] if position < len(header) else ""
    declared_text = declared[position] if position < len(declared) else ""
    raise SupplementHeaderMismatch(
        f"supplement '{decl.name}': CSV header differs from 'columns' at"
        f" position {position + 1}: header '{header_text}', declared '{declared_text}'"
    )


def _supplement_data_rows(
    decl: SupplementDecl, header: list[str], data_rows: list[list[str]]
) -> tuple[tuple[str | None, ...], ...]:
    """Turn a supplement's raw CSV data rows into text-cell rows.

    Args:
        decl: The declaration, for the error message.
        header: The CSV header row's fields — every data row's field count
            must equal this.
        data_rows: The CSV rows following the header, in file order.

    Returns:
        One tuple per data row, one `str | None` cell per field (an empty
        field is None).

    Raises:
        SupplementFileInvalid: A data row's field count differs from the
            header's; names the 1-based data row and both counts.
    """
    rows: list[tuple[str | None, ...]] = []
    for row_index, row in enumerate(data_rows, start=1):
        if len(row) != len(header):
            raise SupplementFileInvalid(
                f"supplement '{decl.name}': data row {row_index} has {len(row)}"
                f" fields, header has {len(header)}"
            )
        rows.append(tuple(cell if cell != "" else None for cell in row))
    return tuple(rows)


def _load_file_supplement(decl: SupplementDecl, config_dir: Path) -> ResolvedSupplement:
    """Resolve one `file`-declared supplement to its text rows.

    Args:
        decl: The declaration; `file` is known non-None (parse-time
            `supplement_source_exactly_one`).
        config_dir: The directory the config file was loaded from.

    Returns:
        The resolved supplement, `path` and `sha256` set.
    """
    path, raw_bytes = _resolve_supplement_file(decl, config_dir)
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    text = _decode_supplement_text(decl, path, raw_bytes)
    parsed_rows = _parse_supplement_csv(decl, path, text)
    header = parsed_rows[0] if parsed_rows else []
    _check_supplement_header(decl, header)
    data_rows = _supplement_data_rows(decl, header, parsed_rows[1:])
    return ResolvedSupplement(decl=decl, path=path, sha256=sha256, rows=data_rows)


def load_supplements(
    config: ExportConfig,
    config_dir: Path,
) -> tuple[ResolvedSupplement, ...]:
    """Resolve every declared supplement's data beside the loaded config.

    The filesystem step the model does not take: resolves each `file`
    against `config_dir`, reads and hashes the bytes, decodes them as
    `utf-8-sig`, parses them with Python `csv` (default dialect,
    `strict=True`, `newline=''` semantics), checks the header against the
    declaration, and yields the text rows (an empty field is None). Inline
    supplements are rendered to text rows with no file facts: `None` ->
    None, `str` -> itself (`''` stays the empty string), `int` -> decimal
    text, `float` -> `repr`.

    Args:
        config: The validated export config.
        config_dir: The directory the config file was loaded from.

    Returns:
        One ResolvedSupplement per declaration, in declaration order; empty
        when the config declares none.

    Raises:
        SupplementFileMissing: A `file` does not exist or is not a regular
            file.
        SupplementFileInvalid: The bytes are not UTF-8, do not parse as
            CSV, or a data row's field count differs from the header's
            (names the 1-based data row).
        SupplementHeaderMismatch: The header row differs from the declared
            column names in count, order, or spelling; names the first
            differing position (a headerless file mismatches at position 1).
    """
    if not config.supplements:
        return ()
    return tuple(
        _load_file_supplement(decl, config_dir)
        if decl.file is not None
        else _load_inline_supplement(decl)
        for decl in config.supplements
    )


def _sql_string_literal(text: str) -> str:
    """A DuckDB single-quoted string literal for one text cell.

    Args:
        text: The cell's text.

    Returns:
        The literal, with embedded single quotes doubled.
    """
    return "'" + text.replace("'", "''") + "'"


def _supplement_columns(decl: "SupplementDecl") -> list[tuple[str, str]]:
    """One supplement's (output name, canonical type) pairs, declared order.

    Args:
        decl: The declaration; every type text is known to canonicalize
            (parse-time `supplement_types_known`).

    Returns:
        (column name, canonical type) pairs, declared order.
    """
    return [
        (name, canonical_supplement_type(type_text))
        for name, type_text in decl.columns.items()
    ]


def _supplement_raw_relation_sql(
    rows: tuple[tuple[str | None, ...], ...], column_count: int
) -> str:
    """The untyped `VALUES` relation over `rows`, a leading position column.

    Args:
        rows: The resolved text rows; non-empty (the caller handles the
            zero-row case separately).
        column_count: The declared column count.

    Returns:
        `(VALUES (1, ...), (2, ...), ...) AS "_raw"("_pos", "_c0", ...)`.
    """
    col_names = ", ".join(f'"_c{i}"' for i in range(column_count))
    values_rows = ", ".join(
        "({}, {})".format(
            position,
            ", ".join(
                "NULL" if cell is None else _sql_string_literal(cell) for cell in row
            ),
        )
        for position, row in enumerate(rows, start=1)
    )
    return f'(VALUES {values_rows}) AS "_raw"("_pos", {col_names})'


def _supplement_relation_sql(
    columns: list[tuple[str, str]], rows: tuple[tuple[str | None, ...], ...]
) -> str:
    """The compiled `SELECT` for one supplement: cast columns, given row order.

    Args:
        columns: (output name, canonical type) pairs, declared order.
        rows: The resolved text rows.

    Returns:
        The `VALUES`-and-cast `SELECT`, ordered by position (position
        projected away); for zero rows the typed empty-relation form
        (`SELECT CAST(NULL AS <type>) AS <col>, … WHERE false`).
    """
    if not rows:
        projections = ", ".join(
            f'CAST(NULL AS {ctype}) AS "{name}"' for name, ctype in columns
        )
        return f"SELECT {projections} WHERE false"
    raw = _supplement_raw_relation_sql(rows, len(columns))
    projections = ", ".join(
        f'CAST("_c{i}" AS {ctype}) AS "{name}"'
        for i, (name, ctype) in enumerate(columns)
    )
    return f'SELECT {projections} FROM {raw} ORDER BY "_pos"'


def _supplement_probe_sql(
    columns: list[tuple[str, str]], rows: tuple[tuple[str | None, ...], ...]
) -> str:
    """One query returning, per column, the first row position (or NULL)
    whose non-NULL cell does not `TRY_CAST` to the column's type.

    Args:
        columns: (output name, canonical type) pairs, declared order.
        rows: The resolved text rows; non-empty (the caller skips a
            zero-row supplement entirely).

    Returns:
        A single-row `SELECT`, one nullable position column per declared
        column, in declared order.
    """
    raw = _supplement_raw_relation_sql(rows, len(columns))
    probes = ", ".join(
        f'MIN(CASE WHEN "_c{i}" IS NOT NULL AND TRY_CAST("_c{i}" AS {ctype})'
        f' IS NULL THEN "_pos" END) AS "_bad{i}"'
        for i, (_, ctype) in enumerate(columns)
    )
    return f"SELECT {probes} FROM {raw}"


def _check_supplement_reserved_names(
    supplements: "Sequence[ResolvedSupplement]",
) -> None:
    """Refuse a supplement named for a reserved incremental bookkeeping table.

    Args:
        supplements: The resolved supplements.

    Raises:
        ExportError: A supplement's `name` is `_export_meta` / `_export_windows`.
    """
    for supplement in supplements:
        name = supplement.decl.name
        if is_reserved_table_name(name):
            raise ExportError(
                f"supplement '{name}' collides with a reserved bookkeeping table name"
            )


def _check_supplement_anchor_requirement(
    supplements: "Sequence[ResolvedSupplement]", anchor: "EffectiveAnchor | None"
) -> None:
    """Refuse a TIMESTAMPTZ supplement column with no resolved anchor.

    Args:
        supplements: The resolved supplements.
        anchor: The resolved effective anchor, or None.

    Raises:
        TemporalRenderRequiresAnchor: A TIMESTAMPTZ column with `anchor` None.
    """
    if anchor is not None:
        return
    for supplement in supplements:
        for col_name, type_text in supplement.decl.columns.items():
            if canonical_supplement_type(type_text) != "TIMESTAMPTZ":
                continue
            raise TemporalRenderRequiresAnchor(
                f"supplement '{supplement.decl.name}': column '{col_name}' is"
                " TIMESTAMPTZ and requires a resolved anchor; supply"
                " rebase.base_date/timezone or rely on the sidecar runtime"
                " anchor"
            )


def _probe_supplement_cells(emit: "Emit", supplement: "ResolvedSupplement") -> None:
    """Probe one supplement's cells for the first cast failure, before any write.

    Args:
        emit: The open emit whose session evaluates the probe.
        supplement: The resolved supplement.

    Raises:
        SupplementValueInvalid: The first non-NULL cell (column-then-row
            declared order) that does not `TRY_CAST` to its column's type;
            names the table, column, 1-based data row, and cell text.
    """
    if not supplement.rows:
        return
    columns = _supplement_columns(supplement.decl)
    sql = _supplement_probe_sql(columns, supplement.rows)
    (result_row,) = emit.query(sql, ())
    for index, (col_name, ctype) in enumerate(columns):
        position = cast("int | None", result_row[index])
        if position is None:
            continue
        cell_text = supplement.rows[position - 1][index]
        raise SupplementValueInvalid(
            f"supplement '{supplement.decl.name}': column '{col_name}', data"
            f" row {position}: value {cell_text!r} is not a {ctype}"
        )


def compile_supplement_specs(
    emit: "Emit",
    supplements: "Sequence[ResolvedSupplement]",
    anchor: "EffectiveAnchor | None",
    write_mode: Literal["create", "replace"],
) -> list[QuerySpec]:
    """Compile every supplement into a QuerySpec over the session.

    Each spec's SQL is the VALUES-and-cast relation over the resolved text
    rows: a leading row-position column, every cell a VARCHAR literal or
    NULL, one CAST per declared column to its canonical type, ordered by
    position with the position projected away; the typed
    `SELECT CAST(NULL AS <type>) AS <col>, … WHERE false` form for zero
    rows. Carries no keys, empty `provenance` / `kind_values`, the author's
    `descriptions` as `author_descriptions`, the author's `description` as
    `author_table_description`, `event_log=False`, and a `SupplementSource`.
    Runs, in order: the reserved-name gate over every supplement name
    (`is_reserved_table_name`, the supplement message); the anchor rule (a
    TIMESTAMPTZ column with `anchor` None); the cell probe (per column in
    declared order, the first row whose non-NULL cell `TRY_CAST`s to NULL).
    Every gate is a pure function of the declaration, the resolved rows, and
    the anchor — no emit table is read — which is what lets the shaped head
    call this at open.

    Args:
        emit: The open emit whose session materializes the relation.
        supplements: The resolved supplements, in declaration order.
        anchor: The resolved effective anchor, or None — consulted by the
            TIMESTAMPTZ rule only.
        write_mode: 'create' for a full export, 'replace' for a windowed
            compile — the caller's delivery regime, never inferred.

    Returns:
        One QuerySpec per supplement, in declaration order; empty for an
        empty input.

    Raises:
        ExportError: A supplement name is a bookkeeping name.
        TemporalRenderRequiresAnchor: A TIMESTAMPTZ column with anchor None;
            names the supplement and the column.
        SupplementValueInvalid: A cell does not cast to its declared type;
            names the table, column, 1-based data row, and cell text.
    """
    if not supplements:
        return []
    _check_supplement_reserved_names(supplements)
    _check_supplement_anchor_requirement(supplements, anchor)
    for supplement in supplements:
        _probe_supplement_cells(emit, supplement)

    specs: list[QuerySpec] = []
    for supplement in supplements:
        decl = supplement.decl
        columns = _supplement_columns(decl)
        specs.append(
            QuerySpec(
                table_name=decl.name,
                sql=_supplement_relation_sql(columns, supplement.rows),
                write_mode=write_mode,
                author_descriptions=decl.descriptions or {},
                author_table_description=decl.description,
                supplement=SupplementSource(file=decl.file, sha256=supplement.sha256),
            )
        )
    return specs


def check_supplement_sources_not_outputs(
    supplements: "Sequence[ResolvedSupplement]",
    output_paths: "Collection[Path]",
    removed_dirs: "Collection[Path]",
) -> None:
    """Refuse a supplement whose resolved source file this invocation would
    overwrite or delete.

    Both tests run over the resolved source path (`Path.resolve()` on both
    sides): equality with any output path; containment under any removed
    directory at any depth.

    Args:
        supplements: The resolved supplements (inline ones have no path
            and are skipped).
        output_paths: Every file the invocation writes, resolved — the
            union of the naming functions for the invocation's fmt and
            regime (plus `out` itself under fmt='duckdb'), assembled by the
            caller from those functions, never enumerated by hand.
        removed_dirs: Every directory the invocation removes wholesale,
            resolved — `csv_removed_dirs` under a CSV `--next` window or
            range; empty for a full export and every DuckDB invocation.

    Raises:
        SupplementSourceIsOutput: A file supplement's `path` equals an
            output path, or lies under a removed directory; names the
            supplement and the path.
    """
    resolved_outputs = {path.resolve() for path in output_paths}
    resolved_removed_dirs = [path.resolve() for path in removed_dirs]
    for supplement in supplements:
        if supplement.path is None:
            continue
        source = supplement.path.resolve()
        is_output = source in resolved_outputs
        is_removed = any(
            source == removed_dir or removed_dir in source.parents
            for removed_dir in resolved_removed_dirs
        )
        if not is_output and not is_removed:
            continue
        raise SupplementSourceIsOutput(
            f"supplement '{supplement.decl.name}': source file {source} is an"
            " output of this export; move the source or change the output"
            " target"
        )
