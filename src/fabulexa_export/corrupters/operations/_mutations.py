"""Shared transform machinery for `mutate_cells` and `duplicate_rows`'
`mutation` mode.

Pure per-kind transforms, seeded-position machinery, the `resample`
donor-pool contract, the per-kind transform dispatcher, and the sentinel
apply-time cast oracle -- the `operations/_impact.py` precedent (underscore
module, underscore-free member names, package-internal). See
`docs/architecture/corrupters.md` § What mutate_cells does and
§ `duplicate_rows` -- the `mutation` mode.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from fabulexa_export._sql import render_typed_literal
from fabulexa_export.config.models import (
    MutationCase,
    MutationFormatDirt,
    MutationMojibake,
    MutationOutOfDomain,
    MutationPrecisionDrop,
    MutationResample,
    MutationScale,
    MutationSentinel,
    MutationTruncate,
    MutationTypo,
    MutationWhitespace,
)
from fabulexa_export.corrupters.selection import working_connection
from fabulexa_export.errors import CorruptValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fabulexa_export.config.models import MutationSpec
    from fabulexa_export.corrupters.operations._impact import TablePopulation
    from fabulexa_export.corrupters.state import WorkingTable

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

FORMAT_DIRT_SHAPE = re.compile(r"-?\d{4,}")
"""format_dirt's shape gate: an optional-minus all-digit string of >= 4 digits."""


def cast_sentinel(
    value: str | int | float | bool,
    target_type: str,
    table_name: str,
    column: str,
    rule: str,
    operation_label: str,
) -> object:
    """Cast `value` into `target_type` via DuckDB `CAST` -- the sentinel
    apply-time cast oracle (the `schema_drift`-retype cast-oracle stance).

    Args:
        value: The author's sentinel literal.
        target_type: The column's current DuckDB type.
        table_name: The resolved table, for the error message.
        column: The resolved column, for the error message.
        rule: The operation's rule label, for the error message.
        operation_label: The operation kind, for the error message prefix
            (`mutate_cells` or `duplicate_rows`).

    Returns:
        The cast value, in the column's Python-native representation.

    Raises:
        CorruptValidationError: `value` does not cast into `target_type`.
    """
    import duckdb

    conn = duckdb.connect(":memory:")
    try:
        try:
            result = conn.execute(
                f"SELECT CAST(? AS {target_type})", [value]
            ).fetchone()
        except duckdb.Error as exc:
            raise CorruptValidationError(
                f"{operation_label} ({rule}): sentinel value {value!r} does not"
                f" cast into {table_name}.{column} ({target_type}): {exc}"
            ) from exc
    finally:
        conn.close()
    assert result is not None
    return result[0]


def sentinel_cast_cache(
    mutation: MutationSentinel,
    populations: "Sequence[TablePopulation]",
    per_table_columns: "Sequence[Sequence[str]]",
    rule: str,
    operation_label: str,
) -> dict[tuple[str, str], object]:
    """The sentinel literal cast into every matched (table, column) pair's
    current type, once per pair, before any write (data-independent).

    Args:
        mutation: The operation's sentinel mutation spec.
        populations: The operation's resolved-table populations.
        per_table_columns: Each population's matched columns, parallel order.
        rule: The operation's rule label, for the error message.
        operation_label: The operation kind, for the error message prefix.

    Returns:
        The cast sentinel value, keyed by (table_name, column).

    Raises:
        CorruptValidationError: The literal does not cast into some pair's type.
    """
    cache: dict[tuple[str, str], object] = {}
    for population, columns in zip(populations, per_table_columns):
        columns_by_name = {
            col.name: col for col in population.working_table.spec.columns
        }
        for column in columns:
            target_type = columns_by_name[column].type
            cache[(population.table_name, column)] = cast_sentinel(
                mutation.value,
                target_type,
                population.table_name,
                column,
                rule,
                operation_label,
            )
    return cache


def apply_case(value: str, form: str) -> str:
    """Apply `form` to `value` -- `case`'s transform."""
    if form == "upper":
        return value.upper()
    if form == "lower":
        return value.lower()
    if form == "title":
        return value.title()
    return value.swapcase()


def apply_whitespace(value: str, where: str) -> str:
    """Insert one space at `where`'s end -- `whitespace`'s transform."""
    return f" {value}" if where == "leading" else f"{value} "


def apply_truncate(value: str, max_length: int) -> str:
    """Keep the first `max_length` characters -- `truncate`'s transform."""
    return value[:max_length]


def apply_precision_drop(value: float, digits: int) -> float:
    """Round to `digits` decimal places, round-half-to-even --
    `precision_drop`'s transform."""
    return round(value, digits)


def apply_scale(value: object, factor: float) -> object:
    """Multiply by `factor` -- `scale`'s transform. A BIGINT (int) value
    stores round-half-to-even of the product; a DOUBLE (float) value stores
    the product as-is."""
    if isinstance(value, int):
        return round(value * factor)
    assert isinstance(value, float)
    return value * factor


def apply_mojibake(value: str) -> str:
    """Re-decode the value's UTF-8 bytes as latin-1 -- `mojibake`'s transform.
    Identity for pure ASCII."""
    return value.encode("utf-8").decode("latin-1")


def group_thousands(digits: str) -> str:
    """Comma-group an all-digit string into thousands, right to left."""
    groups: list[str] = []
    while len(digits) > 3:
        groups.append(digits[-3:])
        digits = digits[:-3]
    groups.append(digits)
    return ",".join(reversed(groups))


def apply_format_dirt(value: str) -> str:
    """Insert comma thousands separators into an optional-minus all-digit
    string of >= 4 digits -- `format_dirt`'s transform. Identity when the
    value's shape doesn't match."""
    if not FORMAT_DIRT_SHAPE.fullmatch(value):
        return value
    sign, digits = ("-", value[1:]) if value.startswith("-") else ("", value)
    return sign + group_thousands(digits)


def swap_adjacent(text: str, pos: int) -> str:
    """Exchange the characters at `pos` and `pos + 1` of `text`."""
    chars = list(text)
    chars[pos], chars[pos + 1] = chars[pos + 1], chars[pos]
    return "".join(chars)


def seeded_position(n: int, seed: float) -> int:
    """A 0-based index in `[0, n)`, deterministic from one `seed` in `[0, 1)`."""
    return min(int(seed * n), n - 1)


def apply_typo_str(value: str, seed: float) -> str:
    """VARCHAR `typo`: exchange two adjacent characters at a seeded position.
    Cannot-apply (returns `value` unchanged) when fewer than two characters."""
    if len(value) < 2:
        return value
    return swap_adjacent(value, seeded_position(len(value) - 1, seed))


def apply_typo_int(value: int, seed: float) -> int:
    """BIGINT `typo`: exchange two adjacent decimal digits of the absolute
    value, sign preserved. Cannot-apply (returns `value` unchanged) when
    fewer than two digits, or the exchange overflows the BIGINT domain."""
    sign = -1 if value < 0 else 1
    digits = str(abs(value))
    if len(digits) < 2:
        return value
    pos = seeded_position(len(digits) - 1, seed)
    swapped = sign * int(swap_adjacent(digits, pos))
    if not (INT64_MIN <= swapped <= INT64_MAX):
        return value
    return swapped


def apply_resample(
    current: object, seed: float, donor_pool: "Sequence[object]"
) -> object:
    """`resample`: a uniform draw over `donor_pool`, excluding `current`.
    No-mutation (returns `current`) when the pool, minus `current`, is empty."""
    effective = [v for v in donor_pool if v != current]
    if not effective:
        return current
    return effective[seeded_position(len(effective), seed)]


def rotation(n: int, seed: float) -> list[int]:
    """All positions `[0, n)`, in seeded-rotation order starting at a seeded
    position."""
    if n <= 0:
        return []
    start = seeded_position(n, seed)
    return [(start + i) % n for i in range(n)]


def apply_out_of_domain(value: str, seed: float, domain: "frozenset[str]") -> str:
    """`out_of_domain`: the first adjacent-transposition candidate, scanned in
    seeded rotation, that is outside `domain` and != `value`; falling back to
    repeated final-character append (guaranteed to leave any finite domain).
    No-mutation (returns `value` unchanged) when `value` is empty."""
    if value == "":
        return value
    for pos in rotation(len(value) - 1, seed):
        candidate = swap_adjacent(value, pos)
        if candidate != value and candidate not in domain:
            return candidate
    last = value[-1]
    candidate = value
    while True:
        candidate += last
        if candidate not in domain:
            return candidate


def resample_donor_pool(
    working_table: "WorkingTable",
    fork_path: str,
    column: str,
    series: tuple[str, str] | None,
) -> list[object]:
    """`resample`'s donor pool for one (table, column): distinct non-NULL
    values on `fork_path`, ascending in DuckDB's total order. Read from the
    state the operation began with; never narrowed by `target.where` (the
    family-C whole-timeline stance -- § Donor pool). For `history.value`,
    additionally narrowed to `series`' `(kind, property)` pair -- the
    per-property value population is the meaningful "column" there, so a
    name is never drawn into a weight series.

    Args:
        working_table: The resolved table's working state as of this
            operation's start.
        fork_path: The sole branch's fork_path.
        column: The mutated column.
        series: The `(kind, property)` pair narrowing a `history.value` pool;
            None for every other column.

    Returns:
        The donor pool, ascending in DuckDB's total order for the column's
        type; possibly empty.
    """
    column_types = {col.name: col.type for col in working_table.spec.columns}
    fork_literal = render_typed_literal(fork_path, column_types["fork_path"])
    predicate = f'"fork_path" = {fork_literal} AND "{column}" IS NOT NULL'
    if series is not None:
        kind, property_name = series
        kind_literal = render_typed_literal(kind, column_types["kind"])
        property_literal = render_typed_literal(property_name, column_types["property"])
        predicate += f' AND "kind" = {kind_literal} AND "property" = {property_literal}'
    with working_connection(working_table.data) as conn:
        sql = (
            f'SELECT DISTINCT "{column}" AS v FROM working'
            f" WHERE {predicate}"
            " ORDER BY v ASC"
        )
        rows = conn.execute(sql).fetchall()
    return [row[0] for row in rows]


def transform(
    mutation: "MutationSpec",
    current: object,
    table_name: str,
    column: str,
    sentinel_cache: dict[tuple[str, str], object],
    seed: float | None,
    domain: "frozenset[str] | None",
    donor_pool: "Sequence[object] | None",
) -> object:
    """The mutated value for one selected, present cell (§ What each mutation does).

    Args:
        mutation: The operation's mutation spec.
        current: The cell's stored (pre-mutation) value; never None (NULL
            cells are filtered before this call).
        table_name: The cell's resolved table.
        column: The cell's resolved column.
        sentinel_cache: `sentinel`'s precomputed cast, keyed by (table, column).
        seed: The seeded kinds' per-unit draw (`typo` / `resample` /
            `out_of_domain`); None for every other kind.
        domain: `out_of_domain`'s declared domain; None for every other kind.
        donor_pool: `resample`'s donor pool; None for every other kind.

    Returns:
        The value to store; equal to `current` is the no-mutation signal.
    """
    if isinstance(mutation, MutationSentinel):
        return sentinel_cache[(table_name, column)]
    if isinstance(mutation, MutationPrecisionDrop):
        assert isinstance(current, float)
        return apply_precision_drop(current, mutation.digits)
    if isinstance(mutation, MutationScale):
        return apply_scale(current, mutation.factor)
    if isinstance(mutation, MutationTypo):
        assert seed is not None
        if isinstance(current, str):
            return apply_typo_str(current, seed)
        assert isinstance(current, int)
        return apply_typo_int(current, seed)
    if isinstance(mutation, MutationResample):
        assert seed is not None
        assert donor_pool is not None
        return apply_resample(current, seed, donor_pool)
    if isinstance(mutation, MutationOutOfDomain):
        assert seed is not None
        assert domain is not None
        assert isinstance(current, str)
        return apply_out_of_domain(current, seed, domain)
    assert isinstance(current, str)
    if isinstance(mutation, MutationCase):
        return apply_case(current, mutation.form)
    if isinstance(mutation, MutationWhitespace):
        return apply_whitespace(current, mutation.where)
    if isinstance(mutation, MutationTruncate):
        return apply_truncate(current, mutation.max_length)
    if isinstance(mutation, MutationMojibake):
        return apply_mojibake(current)
    assert isinstance(mutation, MutationFormatDirt)
    return apply_format_dirt(current)
