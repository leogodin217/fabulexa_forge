"""Supplementary tables: an author-supplied table carried verbatim into a
dimensional export.

Phase 1 delivers the loader step the config model does not take: resolving
each declared `SupplementDecl`'s data — a file or inline rows — into text
rows the compile (a later phase) turns into a relation. The file is read
exactly once, here; no later step touches it again.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fabulexa_forge.errors import (
    SupplementFileInvalid,
    SupplementFileMissing,
    SupplementHeaderMismatch,
)

if TYPE_CHECKING:
    from fabulexa_forge.config.models import ExportConfig, SupplementDecl


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
