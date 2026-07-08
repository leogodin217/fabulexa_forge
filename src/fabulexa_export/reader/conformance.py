"""Conformance checks C1–C12 for base-layer emits.

Independent reimplementation of the base-format conformance procedure.
Zero imports outside the vendored contract. Reads the vendored v4 JSON Schema and DuckDB
catalog/data via Emit.query; never raises on a conformance failure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import jsonschema

from fabulexa_export.reader._schema import _load_vendored_schema

if TYPE_CHECKING:
    from fabulexa_export.reader.emit import Emit

from fabulexa_export.reader.sidecar import ColumnSpec, RecordRoles

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one conformance check (C1–C12).

    `passed` is the authoritative verdict; `messages` and `skips` are diagnostics
    and never decide pass/fail.
    """

    check: str  # "C1" .. "C12"
    passed: bool
    messages: tuple[str, ...]  # failure detail; empty when passed
    skips: tuple[str, ...]  # parts deliberately not examined. Informational.


@dataclass(frozen=True)
class ConformanceReport:
    """Aggregate outcome of running C1–C12 over one emit."""

    results: tuple[CheckResult, ...]  # one per check, in C1..C12 order

    @property
    def ok(self) -> bool:
        """True iff every check passed."""
        return all(r.passed for r in self.results)


# ---------------------------------------------------------------------------
# Pinned spec (PS) — C4 and C5 fixed column lists
# The single sanctioned restatement; used only to check, never to discover.
# ---------------------------------------------------------------------------

# history: 6 required base columns
_PS_HISTORY_BASE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("fork_path", "VARCHAR"),
    ("kind", "VARCHAR"),
    ("record_id", "VARCHAR"),
    ("property", "VARCHAR"),
    ("sim_time", "BIGINT"),
    ("value", "VARCHAR"),
)

# records__K: 6 fixed prefix columns
_PS_RECORDS_PREFIX_COLUMNS: tuple[tuple[str, str], ...] = (
    ("fork_path", "VARCHAR"),
    ("record_id", "VARCHAR"),
    ("created_sim_time", "BIGINT"),
    ("active", "BOOLEAN"),
    ("deactivated_at", "BIGINT"),
    ("last_mutation_sim_time", "BIGINT"),
)

# Valid warehouse roles for C12
_VALID_ROLES: frozenset[str] = frozenset({"dimension", "fact"})

# ---------------------------------------------------------------------------
# Identifier quoting helpers
# ---------------------------------------------------------------------------

_QUOTE_RE = re.compile('"')


def _quote_identifier(name: str) -> str:
    """Wrap a SQL identifier in double quotes, doubling any internal quotes.

    Args:
        name: The identifier string (table or column name).

    Returns:
        The DuckDB double-quoted identifier string.
    """
    return '"' + _QUOTE_RE.sub('""', name) + '"'


# ---------------------------------------------------------------------------
# Catalog-probe helpers
# ---------------------------------------------------------------------------


def _catalog_tables(emit: "Emit") -> set[str]:
    """Return the set of table names present in the DuckDB catalog.

    Args:
        emit: An open emit.

    Returns:
        The set of table names in the catalog.
    """
    rows = emit.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'",
        (),
    )
    return {str(row[0]) for row in rows}


def _catalog_columns(emit: "Emit", table_name: str) -> list[tuple[str, str]]:
    """Return the (column_name, data_type) list for a table, ordered by ordinal.

    Args:
        emit: An open emit.
        table_name: The DuckDB table name to introspect.

    Returns:
        Ordered list of (name, data_type) pairs.
    """
    rows = emit.query(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'main' AND table_name = ? "
        "ORDER BY ordinal_position",
        (table_name,),
    )
    return [(str(row[0]), str(row[1])) for row in rows]


def _catalog_row_count(emit: "Emit", table_name: str) -> int:
    """Return the row count for a table.

    Args:
        emit: An open emit.
        table_name: The DuckDB table name.

    Returns:
        Row count as integer.
    """
    quoted = _quote_identifier(table_name)
    rows = emit.query(f"SELECT count(*) FROM {quoted}", ())
    val = rows[0][0]
    assert isinstance(val, (int, str))
    return int(val)


# ---------------------------------------------------------------------------
# Type normalization for C2
# ---------------------------------------------------------------------------


def _normalize_type(type_str: str) -> str:
    """Normalize a DuckDB type literal for comparison.

    Uppercases and collapses internal whitespace.

    Args:
        type_str: A DuckDB type literal string.

    Returns:
        Normalized type string.
    """
    return re.sub(r"\s+", " ", type_str.upper().strip())


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_c1(emit: "Emit") -> CheckResult:
    """C1: base.json validates against the vendored v4 JSON Schema.

    Unknown top-level fields are recorded in skips, not failures.
    Unknown nested fields (inside branch/table/column/runtime) fail C1.

    Args:
        emit: An open emit.

    Returns:
        A CheckResult for C1.
    """
    schema = _load_vendored_schema()
    raw = emit.sidecar.raw

    # Step 1: record unknown top-level keys as skips
    schema_properties = schema.get("properties", {})
    assert isinstance(schema_properties, dict)
    known_top_keys = set(schema_properties.keys())
    unknown_top = sorted(set(raw.keys()) - known_top_keys)
    skips = tuple(f"unknown top-level field: {k!r}" for k in unknown_top)

    # Step 2: validate with top-level additionalProperties relaxed to true
    relaxed_schema: dict[str, object] = {**schema, "additionalProperties": True}
    messages: list[str] = []
    try:
        jsonschema.validate(instance=raw, schema=relaxed_schema)
    except jsonschema.ValidationError as exc:
        messages.append(str(exc.message))
    except jsonschema.SchemaError as exc:
        messages.append(f"schema error: {exc.message}")

    return CheckResult(
        check="C1",
        passed=len(messages) == 0,
        messages=tuple(messages),
        skips=skips,
    )


def _check_c2(emit: "Emit") -> CheckResult:
    """C2: DuckDB catalog (table set, column order+types, row counts) matches sidecar.

    Args:
        emit: An open emit.

    Returns:
        A CheckResult for C2.
    """
    messages: list[str] = []

    catalog_table_set = _catalog_tables(emit)
    sidecar_tables = emit.sidecar.tables()
    sidecar_table_names = {t.name for t in sidecar_tables}

    # Tables in catalog but not in sidecar
    for name in sorted(catalog_table_set - sidecar_table_names):
        messages.append(f"catalog table '{name}' not declared in sidecar")

    # Tables in sidecar but not in catalog
    for name in sorted(sidecar_table_names - catalog_table_set):
        messages.append(f"sidecar table '{name}' absent from catalog")

    # For each table present in both: compare columns and row count
    for table_spec in sidecar_tables:
        tname = table_spec.name
        if tname not in catalog_table_set:
            continue  # already reported as missing

        cat_cols = _catalog_columns(emit, tname)
        sc_cols: list[ColumnSpec] = list(table_spec.columns)

        if len(cat_cols) != len(sc_cols):
            # Identify the surplus/missing column
            cat_names = [c[0] for c in cat_cols]
            sc_names = [c.name for c in sc_cols]
            cat_set = set(cat_names)
            sc_set = set(sc_names)
            for surplus in sorted(cat_set - sc_set):
                messages.append(
                    f"table '{tname}': catalog has surplus column '{surplus}'"
                )
            for missing in sorted(sc_set - cat_set):
                messages.append(
                    f"table '{tname}': sidecar column '{missing}' absent from catalog"
                )
            if cat_set == sc_set:
                # Same columns but different cardinality (duplicates) — report count
                messages.append(
                    f"table '{tname}': column count mismatch "
                    f"(catalog={len(cat_cols)}, sidecar={len(sc_cols)})"
                )
        else:
            # Same cardinality — compare element-wise by ordinal position
            for idx, (cat_col, sc_col) in enumerate(zip(cat_cols, sc_cols)):
                cat_name, cat_type = cat_col
                if cat_name != sc_col.name:
                    messages.append(
                        f"table '{tname}' col[{idx}]: name mismatch "
                        f"(catalog={cat_name!r}, sidecar={sc_col.name!r})"
                    )
                elif _normalize_type(cat_type) != _normalize_type(sc_col.type):
                    messages.append(
                        f"table '{tname}' col[{idx}] '{sc_col.name}': type mismatch "
                        f"(catalog={cat_type!r}, sidecar={sc_col.type!r})"
                    )

        # Row count check
        try:
            actual_rows = _catalog_row_count(emit, tname)
        except Exception as exc:
            messages.append(f"table '{tname}': row count query failed: {exc}")
            continue
        if actual_rows != table_spec.rows:
            messages.append(
                f"table '{tname}': row count mismatch "
                f"(catalog={actual_rows}, sidecar={table_spec.rows})"
            )

    return CheckResult(
        check="C2",
        passed=len(messages) == 0,
        messages=tuple(messages),
        skips=(),
    )


def _check_c3(emit: "Emit") -> CheckResult:
    """C3: Required tables present; table names well-formed per category.

    history must exist. records__<kind> and membership__<kind>__<property>
    name composition must match record_kind/property fields.

    Args:
        emit: An open emit.

    Returns:
        A CheckResult for C3.
    """
    messages: list[str] = []
    table_names = {t.name for t in emit.sidecar.tables()}

    # Required fixed table
    if "history" not in table_names:
        messages.append("required table 'history' not declared in sidecar")

    # Name composition for records and membership tables
    for table_spec in emit.sidecar.tables():
        tname = table_spec.name
        cat = table_spec.category

        if cat == "records":
            rk = table_spec.record_kind
            if rk is None:
                messages.append(
                    f"table '{tname}': category 'records' requires record_kind "
                    f"but it is absent/None — name-composition mismatch"
                )
            else:
                expected = f"records__{rk}"
                if tname != expected:
                    messages.append(
                        f"table '{tname}': name does not match "
                        f"records__{{record_kind}} (expected '{expected}')"
                    )

        elif cat == "membership":
            rk = table_spec.record_kind
            prop = table_spec.property
            if rk is None:
                messages.append(
                    f"table '{tname}': category 'membership' requires record_kind "
                    f"but it is absent/None — name-composition mismatch"
                )
            elif prop is None:
                messages.append(
                    f"table '{tname}': category 'membership' requires property "
                    f"but it is absent/None — name-composition mismatch"
                )
            else:
                expected = f"membership__{rk}__{prop}"
                if tname != expected:
                    messages.append(
                        f"table '{tname}': name does not match "
                        f"membership__{{record_kind}}__{{property}} "
                        f"(expected '{expected}')"
                    )

    return CheckResult(
        check="C3",
        passed=len(messages) == 0,
        messages=tuple(messages),
        skips=(),
    )


def _check_c4(emit: "Emit") -> CheckResult:
    """C4: history cols 1-6 match the pinned spec exactly.

    Args:
        emit: An open emit.

    Returns:
        A CheckResult for C4.
    """
    messages: list[str] = []
    sidecar = emit.sidecar
    table_names = {t.name for t in sidecar.tables()}

    # Check history base 6 columns
    if "history" in table_names:
        history_cols = list(sidecar.columns("history"))
        base_ps = _PS_HISTORY_BASE_COLUMNS
        if len(history_cols) < len(base_ps):
            messages.append(
                f"history: expected at least {len(base_ps)} columns, "
                f"got {len(history_cols)}"
            )
        else:
            for idx, (ps_name, ps_type) in enumerate(base_ps):
                sc_col = history_cols[idx]
                if sc_col.name != ps_name:
                    messages.append(
                        f"history col[{idx}]: name mismatch "
                        f"(sidecar={sc_col.name!r}, spec={ps_name!r})"
                    )
                elif _normalize_type(sc_col.type) != _normalize_type(ps_type):
                    messages.append(
                        f"history col[{idx}] '{ps_name}': type mismatch "
                        f"(sidecar={sc_col.type!r}, spec={ps_type!r})"
                    )

    return CheckResult(
        check="C4",
        passed=len(messages) == 0,
        messages=tuple(messages),
        skips=(),
    )


def _check_c5_table(
    tname: str,
    cols: list[ColumnSpec],
    messages: list[str],
) -> None:
    """Check C5 shape for a single records table column list.

    The positional prefix is head -> optional presentation_id -> tail:
    head = (fork_path, record_id) at idx 0-1; an optional projection-minted
    presentation_id at idx 2 (scalar; its type is taken from the sidecar, not
    pinned — C2 guarantees sidecar/catalog agreement); tail = (created_sim_time,
    active, deactivated_at, last_mutation_sim_time) at the next four slots. A
    presentation_id anywhere but idx 2 is not consumed here and so fails: it
    displaces a pinned tail column (name mismatch) or lands in the prop block as a
    non-prop__ column. The contiguous prop__ block follows the (possibly shifted)
    prefix.

    Args:
        tname: Table name (for error messages).
        cols: Ordered column list (from sidecar or catalog).
        messages: Accumulator; failures are appended here.
    """
    head_ps = _PS_RECORDS_PREFIX_COLUMNS[:2]  # fork_path, record_id
    # tail = created_sim_time, active, deactivated_at, last_mutation_sim_time
    tail_ps = _PS_RECORDS_PREFIX_COLUMNS[2:]

    # Optional presentation_id occupies idx 2 (right after record_id) when present.
    has_pid = len(cols) > len(head_ps) and cols[len(head_ps)].name == "presentation_id"
    prefix_len = len(_PS_RECORDS_PREFIX_COLUMNS) + (1 if has_pid else 0)

    # Must have the full positional prefix (head + optional presentation_id + tail)
    if len(cols) < prefix_len:
        messages.append(
            f"table '{tname}': expected at least {prefix_len} columns, got {len(cols)}"
        )
        return

    # Head (fork_path, record_id) at idx 0-1, then the tail at its shifted slots.
    # presentation_id at idx 2 is checked by name + position only; its type is
    # sidecar-declared and verified against the catalog by C2.
    tail_start = len(head_ps) + (1 if has_pid else 0)
    pinned = list(enumerate(head_ps)) + [
        (tail_start + j, pc) for j, pc in enumerate(tail_ps)
    ]
    for idx, (ps_name, ps_type) in pinned:
        sc_col = cols[idx]
        if sc_col.name != ps_name:
            messages.append(
                f"table '{tname}' col[{idx}]: name mismatch "
                f"(got={sc_col.name!r}, spec={ps_name!r})"
            )
        elif _normalize_type(sc_col.type) != _normalize_type(ps_type):
            messages.append(
                f"table '{tname}' col[{idx}] '{ps_name}': type mismatch "
                f"(got={sc_col.type!r}, spec={ps_type!r})"
            )

    # Columns after the (possibly shifted) prefix must all be prop__ columns
    after_prefix = cols[prefix_len:]
    for i, sc_col in enumerate(after_prefix):
        if not sc_col.name.startswith("prop__"):
            messages.append(
                f"table '{tname}' col[{prefix_len + i}] '{sc_col.name}': "
                f"expected a prop__ column in the prop block"
            )


def _extract_prop_block(
    cols: list[ColumnSpec],
) -> list[str]:
    """Extract the prop__ column names from a records table column list.

    Returns the ordered list of prop__ column names. The extraction is
    name-filtered (prop__ prefix), so it is correct whether or not the optional
    presentation_id column is present — any non-prop__ column ahead of the prop
    block (a shifted lifecycle column when presentation_id is present) is simply
    skipped.

    Args:
        cols: Ordered column list.

    Returns:
        Ordered list of prop__ column names in the prop block.
    """
    prefix_len = len(_PS_RECORDS_PREFIX_COLUMNS)
    after_prefix = cols[prefix_len:]
    return [c.name for c in after_prefix if c.name.startswith("prop__")]


def _check_c5(emit: "Emit") -> CheckResult:
    """C5: records__K shape: head + optional presentation_id + tail prefix, then a
    contiguous prop__ block.

    Checks the sidecar ColumnSpec list against the pinned spec (PS). Also verifies
    that the catalog's prop__ columns for each records table match the sidecar's
    declared prop__ columns — a discrepancy means the sidecar and catalog disagree
    on which scalar properties exist, which is a C5 structural failure.

    Args:
        emit: An open emit.

    Returns:
        A CheckResult for C5.
    """
    messages: list[str] = []
    skips: list[str] = []
    sidecar = emit.sidecar
    catalog_table_set = _catalog_tables(emit)

    for table_spec in sidecar.tables():
        if table_spec.category != "records":
            continue

        tname = table_spec.name

        # Check sidecar column list shape against PS
        _check_c5_table(tname, list(table_spec.columns), messages)

        # If the table is present in catalog, verify catalog prop__ block

        if tname in catalog_table_set:
            cat_cols_raw = _catalog_columns(emit, tname)
            cat_col_specs = [
                ColumnSpec(name=n, type=t, references=None, history_tracked=None)
                for n, t in cat_cols_raw
            ]
            sc_props = _extract_prop_block(list(table_spec.columns))
            cat_props = _extract_prop_block(cat_col_specs)
            if sc_props != cat_props:
                for col in sc_props:
                    if col not in cat_props:
                        messages.append(
                            f"table '{tname}': sidecar prop__ column '{col}' "
                            f"absent from catalog"
                        )
                for col in cat_props:
                    if col not in sc_props:
                        messages.append(
                            f"table '{tname}': catalog prop__ column '{col}' "
                            f"not declared in sidecar"
                        )
        else:
            skips.append(
                f"table '{tname}' absent from catalog — C5 catalog check skipped"
            )

    return CheckResult(
        check="C5",
        passed=len(messages) == 0,
        messages=tuple(messages),
        skips=tuple(skips),
    )


# ---------------------------------------------------------------------------
# CSV codec (C6)
# ---------------------------------------------------------------------------

_ROUND_TRIPPABLE_TYPES: frozenset[str] = frozenset(
    {"BIGINT", "DOUBLE", "BOOLEAN", "VARCHAR"}
)


def to_csv_text(value: object, duckdb_type: str) -> str:
    """Re-encode a DuckDB-native Python value to its base-layer CSV-text form.

    Reimplemented independently of the producer's codec; agreement is a conformance
    requirement, not a code dependency. Maps by DuckDB type literal:

    - BIGINT  → str(int)
    - DOUBLE  → repr(float)
    - BOOLEAN → "true" / "false" (lowercase)
    - VARCHAR → identity (value is already text)

    Args:
        value: A value as returned by Emit.query for the column (DuckDB-native type).
        duckdb_type: The column's DuckDB type literal from ColumnSpec.type.

    Returns:
        The text encoding of value for the given type.

    Raises:
        ValueError: duckdb_type is outside {BIGINT, DOUBLE, BOOLEAN, VARCHAR}.
    """
    norm = duckdb_type.upper().strip()
    if norm == "BIGINT":
        assert isinstance(value, (int, str))
        return str(int(value))
    if norm == "DOUBLE":
        assert isinstance(value, (int, float, str))
        return repr(float(value))
    if norm == "BOOLEAN":
        return "true" if value else "false"
    if norm == "VARCHAR":
        return str(value)
    raise ValueError(
        f"type {duckdb_type!r} is not text-round-trippable; "
        f"supported: BIGINT, DOUBLE, BOOLEAN, VARCHAR"
    )


# ---------------------------------------------------------------------------
# C6–C10 helpers
# ---------------------------------------------------------------------------


def _table_and_col_present(
    emit: "Emit",
    catalog_tables: set[str],
    table_name: str,
    col_name: str,
) -> bool:
    """Return True iff table exists in the catalog AND has the named column.

    Args:
        emit: An open emit.
        catalog_tables: Pre-fetched set of table names.
        table_name: Table to check.
        col_name: Column to check within that table.

    Returns:
        True if both are present.
    """
    if table_name not in catalog_tables:
        return False
    cat_cols = {name for name, _ in _catalog_columns(emit, table_name)}
    return col_name in cat_cols


def _catalog_col_type(
    emit: "Emit",
    table_name: str,
    col_name: str,
) -> str | None:
    """Return the DuckDB type for a column, or None if not found.

    Args:
        emit: An open emit.
        table_name: Table to look up.
        col_name: Column within that table.

    Returns:
        The type string, or None.
    """
    for name, ctype in _catalog_columns(emit, table_name):
        if name == col_name:
            return ctype
    return None


def _check_c6(emit: "Emit") -> CheckResult:
    """C6: history-tracked property round-trip.

    For every (fork_path, kind, record_id, property) series in history, the latest
    pre-slice history.value equals to_csv_text(records__<kind>.prop__<property>) at
    the same (fork_path, record_id). "Latest pre-slice" is bounded by
    BranchEntry.slice_at, not MAX(sim_time).

    Resolved set-based: one window+join query per distinct (kind, property)
    series-class (not two queries per series), with the codec applied in Python so
    the encoding is identical to the per-series form. The "latest" row per
    (fork_path, record_id) is selected by ROW_NUMBER ordered by sim_time DESC with a
    deterministic value tie-break, so a series with two rows sharing the maximum
    sim_time resolves to one fixed value (Determinism invariant). The contract marks
    C6 sample-based with exhaustive checking "the consumer's choice"; this runs the
    exhaustive choice, now cheap enough to be the default.

    History rows whose fork_path is not a declared branch are dropped (a C8 failure
    surfaced there), as are series with no history row at or before slice_at.

    Args:
        emit: An open emit.

    Returns:
        A CheckResult for C6.
    """
    messages: list[str] = []
    skips: list[str] = []
    catalog_tables = _catalog_tables(emit)

    if "history" not in catalog_tables:
        skips.append("history table absent from catalog — C6 skipped")
        return CheckResult(check="C6", passed=True, messages=(), skips=tuple(skips))

    branches = list(emit.sidecar.branches())
    if not branches:
        skips.append("no sidecar branches — C6 skipped")
        return CheckResult(check="C6", passed=True, messages=(), skips=tuple(skips))

    # Per-fork slice_at bounds as an inline, parameterized relation. The INNER JOIN
    # of history to bounds drops any history row whose fork_path is not a declared
    # branch (a C8 concern); the bound enforces "latest pre-slice".
    bounds_select = " UNION ALL ".join(
        ["SELECT ? AS fork_path, ? AS slice_at"] * len(branches)
    )
    bounds_params: list[object] = []
    for b in branches:
        bounds_params.append(b.fork_path)
        bounds_params.append(b.slice_at)

    # Distinct (kind, property) series-classes drive one set query each.
    pair_rows = emit.query("SELECT DISTINCT kind, property FROM history", ())
    pairs = sorted((str(r[0]), str(r[1])) for r in pair_rows)

    for kind, prop in pairs:
        records_table = f"records__{kind}"
        prop_col = f"prop__{prop}"

        if records_table not in catalog_tables:
            skips.append(
                f"table {records_table!r} absent from catalog — "
                f"C6 series (kind={kind!r}, property={prop!r}) skipped"
            )
            continue

        prop_col_type = _catalog_col_type(emit, records_table, prop_col)
        if prop_col_type is None:
            skips.append(
                f"column {prop_col!r} absent from {records_table!r} catalog — "
                f"C6 series (kind={kind!r}, property={prop!r}) skipped"
            )
            continue

        if prop_col_type.upper().strip() not in _ROUND_TRIPPABLE_TYPES:
            skips.append(
                f"column {prop_col!r} in {records_table!r} has non-round-trippable "
                f"type {prop_col_type!r} — C6 series (kind={kind!r}, "
                f"property={prop!r}) skipped"
            )
            continue

        tq = _quote_identifier(records_table)
        cq = _quote_identifier(prop_col)
        rows = emit.query(
            f"WITH bounds AS ({bounds_select}), "
            f"latest AS ("
            f"  SELECT h.fork_path AS fp, h.record_id AS rid, h.value AS val, "
            f"         ROW_NUMBER() OVER ("
            f"           PARTITION BY h.fork_path, h.record_id "
            f"           ORDER BY h.sim_time DESC, h.value DESC"
            f"         ) AS rn "
            f"  FROM history h "
            f"  JOIN bounds b ON h.fork_path = b.fork_path "
            f"  WHERE h.kind = ? AND h.property = ? AND h.sim_time <= b.slice_at"
            f") "
            f'SELECT l.fp, l.rid, l.val, r."record_id", r.{cq} '
            f"FROM latest l "
            f'LEFT JOIN {tq} r ON r."fork_path" = l.fp AND r."record_id" = l.rid '
            f"WHERE l.rn = 1",
            tuple(bounds_params) + (kind, prop),
        )

        for row in rows:
            fork_path = str(row[0])
            record_id = str(row[1])
            history_value = str(row[2])
            rec_record_id = row[3]
            cell_value = row[4]

            if rec_record_id is None:
                messages.append(
                    f"C6: no records__{kind} row for "
                    f"(fork_path={fork_path!r}, record_id={record_id!r}) — "
                    f"series ({prop!r}) unresolved"
                )
                continue

            if cell_value is None:
                # A tracked property backed by a history series but whose current
                # records cell is NULL cannot round-trip to the series' (non-NULL)
                # latest value: it is a C6 failure, not a codec error. Report it
                # directly rather than encode NULL — to_csv_text has no NULL form for
                # BIGINT/DOUBLE and would raise. A conformant emit never reaches here
                # (a series implies a non-NULL current value); only an already-broken
                # emit — e.g. a corrupter's missing-value defect — does.
                messages.append(
                    f"C6: round-trip mismatch for "
                    f"(fork_path={fork_path!r}, kind={kind!r}, "
                    f"record_id={record_id!r}, property={prop!r}): "
                    f"history.value={history_value!r} != records cell is NULL"
                )
                continue

            encoded = to_csv_text(cell_value, prop_col_type)
            if encoded != history_value:
                messages.append(
                    f"C6: round-trip mismatch for "
                    f"(fork_path={fork_path!r}, kind={kind!r}, "
                    f"record_id={record_id!r}, property={prop!r}): "
                    f"history.value={history_value!r} != encoded={encoded!r}"
                )

    return CheckResult(
        check="C6",
        passed=len(messages) == 0,
        messages=tuple(messages),
        skips=tuple(skips),
    )


def _check_null_group(
    emit: "Emit",
    table_name: str,
    group_cols: tuple[str, ...],
    messages: list[str],
    skips: list[str],
    catalog_tables: set[str],
) -> None:
    """Check NULL all-or-none for a group of columns in a table.

    All columns in the group must be simultaneously NULL or simultaneously non-NULL
    within each row. Appends failures to messages; appends skips for absent columns.

    Args:
        emit: An open emit.
        table_name: The table to check.
        group_cols: Column names forming the NULL all-or-none group.
        messages: Accumulator for failure messages.
        skips: Accumulator for skip messages.
        catalog_tables: Pre-fetched set of table names in the catalog.
    """
    if table_name not in catalog_tables:
        skips.append(f"table {table_name!r} absent — NULL all-or-none group skipped")
        return

    cat_col_names = {name for name, _ in _catalog_columns(emit, table_name)}
    present = [c for c in group_cols if c in cat_col_names]
    missing = [c for c in group_cols if c not in cat_col_names]

    if missing:
        skips.append(
            f"table {table_name!r}: columns {missing!r} absent from catalog — "
            f"NULL all-or-none group partially skipped"
        )
    if len(present) < 2:
        return

    tq = _quote_identifier(table_name)
    # Build NULL all-or-none predicate: NOT ((all null) OR (all not null))
    all_null = " AND ".join(f"{_quote_identifier(c)} IS NULL" for c in present)
    all_not_null = " AND ".join(f"{_quote_identifier(c)} IS NOT NULL" for c in present)
    bad_rows = emit.query(
        f"SELECT count(*) FROM {tq} WHERE NOT (({all_null}) OR ({all_not_null}))",
        (),
    )
    _bad_val = bad_rows[0][0]
    assert isinstance(_bad_val, (int, str))
    bad_count = int(_bad_val)
    if bad_count > 0:
        messages.append(
            f"C7: table {table_name!r} has {bad_count} row(s) with partial "
            f"NULL in group {list(group_cols)!r}"
        )


def _check_c7_deactivated_at(
    emit: "Emit",
    tname: str,
    catalog_tables: set[str],
    messages: list[str],
) -> None:
    """Check deactivated_at NULL iff active for a single records table.

    Args:
        emit: An open emit.
        tname: Records table name.
        catalog_tables: Pre-fetched set of table names in the catalog.
        messages: Accumulator for failure messages.
    """
    if tname not in catalog_tables:
        return
    cat_col_names = {name for name, _ in _catalog_columns(emit, tname)}
    if "active" not in cat_col_names or "deactivated_at" not in cat_col_names:
        return
    tq = _quote_identifier(tname)
    bad_rows = emit.query(
        f"SELECT count(*) FROM {tq} WHERE NOT ("
        f'("active" IS TRUE AND "deactivated_at" IS NULL) OR '
        f'("active" IS FALSE AND "deactivated_at" IS NOT NULL)'
        f")",
        (),
    )
    _bad_val = bad_rows[0][0]
    assert isinstance(_bad_val, (int, str))
    bad_count = int(_bad_val)
    if bad_count > 0:
        messages.append(
            f"C7: table {tname!r} has {bad_count} row(s) where "
            f"deactivated_at NULL-iff-active is violated"
        )


def _check_c7_membership_ref_pairs(
    emit: "Emit",
    tname: str,
    catalog_tables: set[str],
    messages: list[str],
    skips: list[str],
) -> None:
    """Check NULL all-or-none for member__<f>__kind / member__<f>__id pairs.

    Args:
        emit: An open emit.
        tname: Membership table name.
        catalog_tables: Pre-fetched set of table names in the catalog.
        messages: Accumulator for failure messages.
        skips: Accumulator for skip messages.
    """
    if tname not in catalog_tables:
        return
    cat_cols = {name for name, _ in _catalog_columns(emit, tname)}
    member_kind_cols = [
        c for c in cat_cols if c.startswith("member__") and c.endswith("__kind")
    ]
    for kind_col in member_kind_cols:
        prefix = kind_col[: -len("__kind")]
        id_col = f"{prefix}__id"
        if id_col in cat_cols:
            _check_null_group(
                emit,
                tname,
                (kind_col, id_col),
                messages,
                skips,
                catalog_tables,
            )


def _check_c7(emit: "Emit") -> CheckResult:
    """C7: NULL all-or-none on column groups.

    Groups checked:
    - records__K.deactivated_at NULL iff active
    - membership member__f__kind/id pairs (all-NULL or all-non-NULL)

    Args:
        emit: An open emit.

    Returns:
        A CheckResult for C7.
    """
    messages: list[str] = []
    skips: list[str] = []
    catalog_tables = _catalog_tables(emit)

    for table_spec in emit.sidecar.tables():
        if table_spec.category == "records":
            _check_c7_deactivated_at(emit, table_spec.name, catalog_tables, messages)
        elif table_spec.category == "membership":
            _check_c7_membership_ref_pairs(
                emit, table_spec.name, catalog_tables, messages, skips
            )

    return CheckResult(
        check="C7",
        passed=len(messages) == 0,
        messages=tuple(messages),
        skips=tuple(skips),
    )


def _check_c8(emit: "Emit") -> CheckResult:
    """C8: Exactly one branch; distinct fork_path across all tables equals that branch.

    The sanitised subset mandates exactly one branch in `branches`. This check
    first asserts cardinality, then verifies the set-equality of fork_paths across
    catalog tables and the sidecar.

    Args:
        emit: An open emit.

    Returns:
        A CheckResult for C8.
    """
    messages: list[str] = []
    catalog_tables = _catalog_tables(emit)

    # Assert exactly one branch
    branches = list(emit.sidecar.branches())
    if len(branches) != 1:
        messages.append(f"C8: branches must have exactly 1 entry, got {len(branches)}")

    # Collect distinct fork_path values from all tables that have a fork_path column
    data_fork_paths: set[str] = set()
    for table_spec in emit.sidecar.tables():
        tname = table_spec.name
        if tname not in catalog_tables:
            continue
        cat_col_names = {name for name, _ in _catalog_columns(emit, tname)}
        if "fork_path" not in cat_col_names:
            continue
        tq = _quote_identifier(tname)
        rows = emit.query(
            f'SELECT DISTINCT "fork_path" FROM {tq} WHERE "fork_path" IS NOT NULL',
            (),
        )
        for row in rows:
            data_fork_paths.add(str(row[0]))

    sidecar_fork_paths = {b.fork_path for b in branches}

    extra_in_data = sorted(data_fork_paths - sidecar_fork_paths)
    missing_in_data = sorted(sidecar_fork_paths - data_fork_paths)

    for fp in extra_in_data:
        messages.append(
            f"C8: fork_path {fp!r} found in data but not in sidecar branches"
        )
    for fp in missing_in_data:
        messages.append(
            f"C8: fork_path {fp!r} declared in sidecar branches but absent from data"
        )

    return CheckResult(
        check="C8",
        passed=len(messages) == 0,
        messages=tuple(messages),
        skips=(),
    )


def _check_c9(emit: "Emit") -> CheckResult:
    """C9: Pinned IDs resolve to exactly one row per (id x fork_path) in records.

    For each (kind, label, id) in pinned_ids, records__<kind> must exist and must
    contain exactly one row per (id x fork_path present in that table). An absent
    records__<kind> is a C9 failure (not a skip).

    Args:
        emit: An open emit.

    Returns:
        A CheckResult for C9.
    """
    messages: list[str] = []
    skips: list[str] = []
    pinned_ids = emit.sidecar.pinned_ids()

    if not pinned_ids:
        return CheckResult(check="C9", passed=True, messages=(), skips=())

    catalog_tables = _catalog_tables(emit)

    for kind, labels in pinned_ids.items():
        records_table = f"records__{kind}"
        if records_table not in catalog_tables:
            messages.append(
                f"C9: records__{kind!r} absent from catalog — "
                f"pinned kind {kind!r} cannot resolve"
            )
            continue

        cat_col_names = {name for name, _ in _catalog_columns(emit, records_table)}
        if "record_id" not in cat_col_names or "fork_path" not in cat_col_names:
            skips.append(
                f"C9: table {records_table!r} missing record_id/fork_path columns — "
                f"pin resolution skipped"
            )
            continue

        tq = _quote_identifier(records_table)
        # Get all distinct fork_paths present in this records table
        fp_rows = emit.query(
            f'SELECT DISTINCT "fork_path" FROM {tq} WHERE "fork_path" IS NOT NULL',
            (),
        )
        table_fork_paths = [str(r[0]) for r in fp_rows]

        for label, record_id in labels.items():
            for fork_path in table_fork_paths:
                count_rows = emit.query(
                    f"SELECT count(*) FROM {tq} "
                    f'WHERE "record_id" = ? AND "fork_path" = ?',
                    (record_id, fork_path),
                )
                _count_val = count_rows[0][0]
                assert isinstance(_count_val, (int, str))
                count = int(_count_val)
                if count != 1:
                    messages.append(
                        f"C9: pinned id kind={kind!r} label={label!r} "
                        f"id={record_id!r} fork_path={fork_path!r}: "
                        f"expected 1 row, got {count}"
                    )

    return CheckResult(
        check="C9",
        passed=len(messages) == 0,
        messages=tuple(messages),
        skips=tuple(skips),
    )


def _check_c10(emit: "Emit") -> CheckResult:
    """C10: Membership integrity.

    - left_sim_time IS NULL OR left_sim_time >= joined_sim_time
    - Each non-NULL member reference resolves to some row in records__<member_kind>
      on the same fork_path (regardless of active).

    Args:
        emit: An open emit.

    Returns:
        A CheckResult for C10.
    """
    messages: list[str] = []
    skips: list[str] = []
    catalog_tables = _catalog_tables(emit)

    for table_spec in emit.sidecar.tables():
        if table_spec.category != "membership":
            continue

        tname = table_spec.name
        if tname not in catalog_tables:
            skips.append(
                f"membership table {tname!r} absent from catalog — C10 skipped"
            )
            continue

        cat_col_names = {name for name, _ in _catalog_columns(emit, tname)}
        tq = _quote_identifier(tname)

        # Check left_sim_time >= joined_sim_time
        if "left_sim_time" in cat_col_names and "joined_sim_time" in cat_col_names:
            bad_rows = emit.query(
                f'SELECT count(*) FROM {tq} WHERE "left_sim_time" IS NOT NULL '
                f'AND "left_sim_time" < "joined_sim_time"',
                (),
            )
            _bad_val2 = bad_rows[0][0]
            assert isinstance(_bad_val2, (int, str))
            bad_count = int(_bad_val2)
            if bad_count > 0:
                messages.append(
                    f"C10: table {tname!r} has {bad_count} row(s) where "
                    f"left_sim_time < joined_sim_time"
                )

        # Check member reference integrity. Resolved set-based: for each member
        # reference column, one anti-join per distinct referenced kind surfaces the
        # (fork_path, id) pairs that resolve to no records row — replacing one count
        # query per distinct reference. Resolution is against record *identity*: any
        # row for that (fork_path, record_id) satisfies it regardless of `active`.
        member_kind_cols = [
            c
            for c in cat_col_names
            if c.startswith("member__") and c.endswith("__kind")
        ]
        for kind_col in member_kind_cols:
            prefix = kind_col[: -len("__kind")]
            id_col = f"{prefix}__id"

            if id_col not in cat_col_names:
                skips.append(
                    f"C10: table {tname!r} has {kind_col!r} but no {id_col!r} — "
                    f"member reference check skipped"
                )
                continue

            kq = _quote_identifier(kind_col)
            iq = _quote_identifier(id_col)

            # Distinct member kinds referenced by this column.
            kind_rows = emit.query(
                f"SELECT DISTINCT {kq} FROM {tq} "
                f"WHERE {kq} IS NOT NULL AND {iq} IS NOT NULL",
                (),
            )

            for kind_row in sorted(str(r[0]) for r in kind_rows):
                member_kind = kind_row
                member_table = f"records__{member_kind}"

                if member_table not in catalog_tables:
                    skips.append(
                        f"C10: table {tname!r} references {member_table!r} "
                        f"(kind={member_kind!r}) but that table is absent from the "
                        f"catalog — skipped"
                    )
                    continue

                member_cat_cols = {
                    name for name, _ in _catalog_columns(emit, member_table)
                }
                if (
                    "record_id" not in member_cat_cols
                    or "fork_path" not in member_cat_cols
                ):
                    skips.append(
                        f"C10: table {member_table!r} missing record_id/fork_path — "
                        f"reference resolution skipped"
                    )
                    continue

                mtq = _quote_identifier(member_table)
                dangling_rows = emit.query(
                    f"SELECT ref.fp, ref.rid FROM ("
                    f'  SELECT DISTINCT "fork_path" AS fp, {iq} AS rid FROM {tq} '
                    f"  WHERE {kq} = ? AND {iq} IS NOT NULL"
                    f") ref "
                    f'LEFT JOIN {mtq} r ON r."fork_path" = ref.fp '
                    f'  AND r."record_id" = ref.rid '
                    f'WHERE r."record_id" IS NULL '
                    f"ORDER BY ref.fp, ref.rid",
                    (member_kind,),
                )
                for dangling_row in dangling_rows:
                    member_fork_path = str(dangling_row[0])
                    member_id = str(dangling_row[1])
                    messages.append(
                        f"C10: table {tname!r} member reference "
                        f"(fork_path={member_fork_path!r}, kind={member_kind!r}, "
                        f"id={member_id!r}) resolves to no row in {member_table!r}"
                    )

    return CheckResult(
        check="C10",
        passed=len(messages) == 0,
        messages=tuple(messages),
        skips=tuple(skips),
    )


def _check_c11(emit: "Emit") -> CheckResult:
    """C11: Column SCD class consistency.

    Verbatim rule (contract base-format.md §C11):
    - If no records-category prop__ column in the sidecar carries history_tracked,
      skip (producer predates the attribute).
    - For each distinct (kind, property) in history (columns 2, 4):
      let col = the prop__<property> column on records__<kind>
      require: col is present in the sidecar with history_tracked == true.

    C11 is one-directional: a type-2 property with no history rows is not checked.
    C11 skips when the history table is absent from the catalog.

    Args:
        emit: An open emit.

    Returns:
        A CheckResult for C11.
    """
    messages: list[str] = []
    skips: list[str] = []
    sidecar = emit.sidecar

    # --- Skip guard: computed INLINE over records-category prop__ columns only ---
    # "no records-category prop__ column carries history_tracked" → skip.
    # Do NOT use Sidecar.history_tracked_available() — it returns True if ANY column
    # anywhere carries the flag (too broad; diverges on malformed emits).
    any_records_prop_tracked = False
    for table_spec in sidecar.tables():
        if table_spec.category != "records":
            continue
        for col in table_spec.columns:
            if col.name.startswith("prop__") and col.history_tracked is not None:
                any_records_prop_tracked = True
                break
        if any_records_prop_tracked:
            break

    if not any_records_prop_tracked:
        skips.append(
            "no records-category prop__ column carries history_tracked — "
            "producer predates the attribute; C11 skipped"
        )
        return CheckResult(check="C11", passed=True, messages=(), skips=tuple(skips))

    # --- Light guard: history table absent → skip ---
    catalog_tables = _catalog_tables(emit)
    if "history" not in catalog_tables:
        skips.append("history table absent from catalog — C11 skipped")
        return CheckResult(check="C11", passed=True, messages=(), skips=tuple(skips))

    # --- Check kind/property columns exist in history catalog ---
    history_cat_cols = {name for name, _ in _catalog_columns(emit, "history")}
    if "kind" not in history_cat_cols or "property" not in history_cat_cols:
        skips.append(
            "history table missing kind/property columns (C4 failure) — C11 skipped"
        )
        return CheckResult(check="C11", passed=True, messages=(), skips=tuple(skips))

    # --- Build prop-map: records__<kind> → {prop_name → history_tracked flag} ---
    # key: kind str; value: dict of prop_name (without prop__ prefix) → history_tracked
    prop_map: dict[str, dict[str, bool | None]] = {}
    for table_spec in sidecar.tables():
        if table_spec.category != "records":
            continue
        kind = table_spec.record_kind
        if kind is None:
            # C3 owns this; skip to avoid double-reporting
            continue
        props: dict[str, bool | None] = {}
        for col in table_spec.columns:
            if col.name.startswith("prop__"):
                prop_name = col.name[len("prop__") :]
                props[prop_name] = col.history_tracked
        prop_map[kind] = props

    # --- Query distinct (kind, property) pairs from history ---
    history_pairs_rows = emit.query(
        "SELECT DISTINCT kind, property FROM history",
        (),
    )

    # Collect pairs and iterate in SORTED order for deterministic messages
    seen_pairs: set[tuple[str, str]] = {
        (str(row[0]), str(row[1])) for row in history_pairs_rows
    }

    for kind, prop in sorted(seen_pairs):
        kind_props = prop_map.get(kind)

        if kind_props is None:
            # records__<kind> not in sidecar → not C11's problem (C3/C9 territory)
            skips.append(
                f"C11: records__{kind!r} not declared in sidecar — "
                f"(kind={kind!r}, property={prop!r}) skipped"
            )
            continue

        if prop not in kind_props:
            messages.append(
                f"C11: prop__{prop} absent from sidecar for records__{kind} — "
                f"history (kind={kind!r}, property={prop!r}) "
                f"has no matching prop__ column"
            )
            continue

        tracked = kind_props[prop]
        if tracked is not True:
            messages.append(
                f"C11: prop__{prop} on records__{kind} "
                f"has history_tracked={tracked!r}, "
                f"expected True — history row (kind={kind!r}, property={prop!r}) "
                f"requires history_tracked == true"
            )

    return CheckResult(
        check="C11",
        passed=len(messages) == 0,
        messages=tuple(messages),
        skips=tuple(skips),
    )


def _check_c12_actor_subtypes(
    emit: "Emit",
    record_roles: RecordRoles,
    catalog_tables: set[str],
    messages: list[str],
    skips: list[str],
) -> None:
    """Check actor sub-type coverage for C12.

    Every distinct prop__actor_type value in records__actor must be declared in
    record_roles["actor"]. records__actor absent from the catalog is a skip
    (C2 owns catalog disagreement); absent from the sidecar is handled by the
    kind-coverage loop in _check_c12.

    Args:
        emit: An open emit.
        record_roles: The parsed RecordRoles view.
        catalog_tables: Pre-fetched set of table names in the catalog.
        messages: Accumulator for failure messages.
        skips: Accumulator for skip messages.
    """
    if not record_roles.is_subtyped("actor"):
        return

    actor_table = "records__actor"
    if actor_table not in catalog_tables:
        skips.append(
            "records__actor absent from catalog — actor sub-type coverage check skipped"
        )
        return

    cat_col_names = {name for name, _ in _catalog_columns(emit, actor_table)}
    subtype_col = "prop__actor_type"
    if subtype_col not in cat_col_names:
        skips.append(
            f"column '{subtype_col}' absent from records__actor catalog — "
            "actor sub-type coverage check skipped"
        )
        return

    tq = _quote_identifier(actor_table)
    cq = _quote_identifier(subtype_col)
    rows = emit.query(
        f"SELECT DISTINCT {cq} FROM {tq} WHERE {cq} IS NOT NULL",
        (),
    )
    for row in rows:
        sub_type = str(row[0])
        try:
            record_roles.role_of("actor", sub_type)
        except KeyError:
            messages.append(
                f"C12: records__actor contains sub-type {sub_type!r} "
                f"not declared in record_roles['actor']"
            )


def _check_c12(emit: "Emit") -> CheckResult:
    """C12: Record-role registry consistency.

    Skips when record_roles is absent from the sidecar. When present, asserts:
    - Every emitted records-category kind (from sidecar tables) appears in
      record_roles.
    - Every non-actor kind maps to a value in {"dimension", "fact"}.
    - record_roles["actor"] is an object (is_subtyped) and every value in it
      is in {"dimension", "fact"}.
    - Every distinct prop__actor_type value in records__actor data is declared
      in record_roles["actor"].

    Args:
        emit: An open emit.

    Returns:
        A CheckResult for C12.
    """
    messages: list[str] = []
    skips: list[str] = []

    record_roles = emit.sidecar.record_roles()
    if record_roles is None:
        skips.append("record_roles absent from sidecar — C12 skipped")
        return CheckResult(check="C12", passed=True, messages=(), skips=tuple(skips))

    catalog_tables = _catalog_tables(emit)

    # Collect emitted records-category kinds from the sidecar
    emitted_kinds: list[str] = []
    for table_spec in emit.sidecar.tables():
        if table_spec.category == "records" and table_spec.record_kind is not None:
            emitted_kinds.append(table_spec.record_kind)

    # Every emitted kind must appear in record_roles
    registered_kinds = set(record_roles.kinds())
    for kind in emitted_kinds:
        if kind not in registered_kinds:
            messages.append(
                f"C12: emitted records kind {kind!r} is missing from record_roles"
            )

    # Validate role values for all registered kinds
    for kind in record_roles.kinds():
        if record_roles.is_subtyped(kind):
            # Object-valued: every sub-type role must be in {"dimension", "fact"}
            for sub_type in record_roles.sub_types(kind):
                role = record_roles.role_of(kind, sub_type)
                if role not in _VALID_ROLES:
                    messages.append(
                        f"C12: record_roles[{kind!r}][{sub_type!r}] = {role!r} "
                        f"is not a valid role (must be 'dimension' or 'fact')"
                    )
        else:
            # Bare-string kind: role must be in {"dimension", "fact"}
            role = record_roles.role_of(kind, None)
            if role not in _VALID_ROLES:
                messages.append(
                    f"C12: record_roles[{kind!r}] = {role!r} "
                    f"is not a valid role (must be 'dimension' or 'fact')"
                )

    # Actor sub-type coverage: every prop__actor_type value in data must be declared
    if "actor" in registered_kinds:
        _check_c12_actor_subtypes(emit, record_roles, catalog_tables, messages, skips)

    return CheckResult(
        check="C12",
        passed=len(messages) == 0,
        messages=tuple(messages),
        skips=tuple(skips),
    )


# ---------------------------------------------------------------------------
# Check registry
# ---------------------------------------------------------------------------

_CHECKS: dict[str, Callable[["Emit"], CheckResult]] = {
    "C1": _check_c1,
    "C2": _check_c2,
    "C3": _check_c3,
    "C4": _check_c4,
    "C5": _check_c5,
    "C6": _check_c6,
    "C7": _check_c7,
    "C8": _check_c8,
    "C9": _check_c9,
    "C10": _check_c10,
    "C11": _check_c11,
    "C12": _check_c12,
}

_RECOGNIZED_IDS: tuple[str, ...] = (
    "C1",
    "C2",
    "C3",
    "C4",
    "C5",
    "C6",
    "C7",
    "C8",
    "C9",
    "C10",
    "C11",
    "C12",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate(emit: "Emit") -> ConformanceReport:
    """Run conformance checks C1–C12 against an opened emit.

    Reimplements the base-format conformance procedure independently of the
    producer's emitters.conformance. C1 validates base.json against the vendored
    v4 JSON Schema; C2–C5 check catalog/sidecar agreement, required tables,
    and column shapes; C6–C12 check data-level integrity.

    A conformance failure is reported as a failing CheckResult, never raised:
    callers inspect the report and choose an exit code. Operational failures
    (unreadable DuckDB) raise.

    Args:
        emit: An emit already opened — and therefore version-gated — by open_emit.

    Returns:
        A ConformanceReport with one CheckResult per check, in C1..C12 order.

    Raises:
        RunDatabaseError: An operational failure reading run.duckdb mid-check.
            A sidecar-declared table or column absent from the catalog is a C2
            failure (and a skip on any other check that needed it), not a raised
            error — data-reading checks probe the catalog before querying.
    """
    results = tuple(_CHECKS[cid](emit) for cid in _RECOGNIZED_IDS)
    return ConformanceReport(results=results)


def run_check(emit: "Emit", check_id: str) -> CheckResult:
    """Run a single named conformance check against an emit.

    Enables targeted negative-fixture tests that assert a specific check fails.

    Args:
        emit: An opened emit.
        check_id: One of "C1" .. "C12".

    Returns:
        The CheckResult for that check.

    Raises:
        ValueError: check_id is not a recognized check name.
        RunDatabaseError: An operational failure reading run.duckdb mid-check.
    """
    if check_id not in _CHECKS:
        raise ValueError(
            f"unrecognized check id {check_id!r}; recognized: {_RECOGNIZED_IDS}"
        )
    fn = _CHECKS[check_id]
    return fn(emit)
