"""The author README overlay: an H2-slot markdown grammar parsed into
export-level and per-table prose, plus the post-compile check that every
`table:` slot names a table the compiled plan actually produces.

Heading matching is exact and case-sensitive (design doc § The overlay
grammar) — never silently normalized. Two slot forms:

- `## overview` — export-level prose.
- `## table: <name>` — one output table's prose, `<name>` taken verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fabulexa_forge.errors import ReadmeOverlayInvalid, ReadmeOverlayUnknownTable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_H2_PREFIX = "## "
_OVERVIEW_HEADING = "overview"
_TABLE_HEADING_RE = re.compile(r"^table: (\S.*)$")


@dataclass(frozen=True)
class ReadmeOverlay:
    """Parsed author overlay.

    `table_notes` keys are author-facing output-table names; values are
    verbatim markdown bodies. Constructed only by `load_readme_overlay`.
    """

    overview: str | None
    table_notes: Mapping[str, str]


def load_readme_overlay(path: Path) -> ReadmeOverlay:
    """Parse an overlay markdown file per the design's slot grammar.

    Args:
        path: Absolute path to the overlay file.

    Returns:
        The parsed ReadmeOverlay.

    Raises:
        ReadmeOverlayInvalid: unreadable / not UTF-8; content before the
            first H2; a heading matching neither slot form (exact,
            case-sensitive); a duplicate slot key.
    """
    text = _read_overlay_text(path)
    lines = text.splitlines()
    headings = _find_h2_headings(lines)
    _check_no_content_before_first_heading(lines, headings, path)

    overview: str | None = None
    table_notes: dict[str, str] = {}
    for position, (line_index, heading_text) in enumerate(headings):
        slot = _parse_slot_heading(heading_text)
        if slot is None:
            raise ReadmeOverlayInvalid(
                f"{path}: heading '{_H2_PREFIX}{heading_text}' matches neither"
                " 'overview' nor 'table: <name>'"
            )
        kind, name = slot
        body_end = (
            headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        )
        body = _trim_blank_lines(lines[line_index + 1 : body_end])
        if kind == "overview":
            if overview is not None:
                raise ReadmeOverlayInvalid(f"{path}: duplicate slot 'overview'")
            overview = body
        else:
            if name in table_notes:
                raise ReadmeOverlayInvalid(f"{path}: duplicate slot 'table: {name}'")
            table_notes[name] = body

    return ReadmeOverlay(overview=overview, table_notes=table_notes)


def validate_overlay_tables(
    overlay: ReadmeOverlay,
    output_table_names: Sequence[str],
) -> None:
    """Refuse table notes referencing tables the plan won't produce.

    Args:
        overlay: The parsed overlay.
        output_table_names: Author-facing output-table names of the compiled
            plan, in plan iteration order.

    Raises:
        ReadmeOverlayUnknownTable: names the slot and lists the plan's tables.
    """
    known = set(output_table_names)
    for name in overlay.table_notes:
        if name not in known:
            raise ReadmeOverlayUnknownTable(
                f"readme_overlay: table slot 'table: {name}' is not an output"
                f" table of this export (tables: {', '.join(output_table_names)})"
            )


def _read_overlay_text(path: Path) -> str:
    """Read `path` as UTF-8 text.

    Raises:
        ReadmeOverlayInvalid: the file cannot be read, or is not valid UTF-8.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReadmeOverlayInvalid(
            f"readme_overlay: cannot read {path}: {exc}"
        ) from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReadmeOverlayInvalid(
            f"readme_overlay: {path} is not valid UTF-8"
        ) from exc


def _find_h2_headings(lines: list[str]) -> list[tuple[int, str]]:
    """Locate every H2 heading line.

    Returns:
        `(line_index, heading_text)` pairs, in file order, where
        `heading_text` is the text following `## ` with trailing whitespace
        stripped (leading whitespace preserved, per the grammar's exact
        matching).
    """
    return [
        (index, line[len(_H2_PREFIX) :].rstrip())
        for index, line in enumerate(lines)
        if line.startswith(_H2_PREFIX)
    ]


def _check_no_content_before_first_heading(
    lines: list[str], headings: list[tuple[int, str]], path: Path
) -> None:
    """Reject non-blank content preceding the first H2 slot heading.

    Raises:
        ReadmeOverlayInvalid: any line before the first heading (or, absent
            any heading, anywhere in the file) is non-blank.
    """
    first_heading_index = headings[0][0] if headings else len(lines)
    if any(line.strip() for line in lines[:first_heading_index]):
        raise ReadmeOverlayInvalid(
            f"{path}: content before the first '{_H2_PREFIX.strip()}' slot heading"
        )


def _parse_slot_heading(heading_text: str) -> tuple[str, str] | None:
    """Classify one H2 heading's text against the two legal slot forms.

    Returns:
        `("overview", "")` for `overview`; `("table", name)` for
        `table: <name>`; None when the heading matches neither form.
    """
    if heading_text == _OVERVIEW_HEADING:
        return ("overview", "")
    match = _TABLE_HEADING_RE.match(heading_text)
    if match is not None:
        return ("table", match.group(1))
    return None


def _trim_blank_lines(lines: list[str]) -> str:
    """Join `lines`, trimming leading/trailing blank lines; interior blank
    lines are kept verbatim."""
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])
