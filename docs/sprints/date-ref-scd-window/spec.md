# Sprint: date-ref-scd-window

Design doc: [`docs/architecture/pending/date-ref-scd-window-bound.md`](../../architecture/pending/date-ref-scd-window-bound.md)
— the WHY, every semantic table, every error message. This spec carries the
contracts, phases, and test cases; it does not restate the design's prose.
Where a contract below says "§ X", that is a section of the design doc.

## Purpose

An author keys an `scd: type2` dim's version rows into `dim_date` by the local
date a version took effect or ended, with a third `date_ref` shape —
`{scd_window: valid_from | valid_to}` — so the calendar is reachable by key from
every table family the dimensional mode produces.

```yaml
- name: dim_company
  scd: type2
  key: [company_id, valid_from]
  columns:
    - name: valid_from
      derived: {scd_window: {bound: valid_from, as: date}}
    - name: valid_from_date_key
      date_ref: {scd_window: valid_from}        # 20260314
    - name: valid_to_date_key
      date_ref: {scd_window: valid_to}          # NULL on the current version
```

The key equals `date_key_expr` over the sibling `scd_window: {…, as: date}`
column by construction; the README names the bound the key addresses; the
manifest's `columns[].references` reads `dim_date` as for every `date_ref`.

## Scope

**Capabilities touched:**
- date-dimension (`date_ref` column mode): the bound shape on `DateRefSpec`, `resolve_date_ref_source` nullable, `render_date_ref_expr` over the version relation's bound, the new always-on rule `DateRefWindowBoundRequiresScd2`, `TemporalRenderRequiresAnchor` covering the shape, the slice-only read collector contributing nothing for it (not: the range guard, the manifest field set, `dim_date` itself, or the instant / parse shapes — all unchanged)
- dimensional-scd2: `build_scd2_column_expr_flag` hands the renderer `version_start` / `version_end`; the records-grain builder asserts a source column (not: the reconstruction, `derived: scd_window`, `Scd2NeedsHistory`)
- incremental-windowing: the bound-shape branch of the one per-column variance reading, picked up by `window_delivery_class` and `KeyColumnsStable` (not: the delivery classes, `WindowKeyDuplicate`)
- companion-artifacts / documentation-channel: `QuerySpec.window_bounds` / `TableReport.window_bounds` stamped at dimensional plan compile and forwarded by both report-assembly sites; the bound-shape pinned dictionary prose (not: the manifest schema or `manifest_format_version`)
- recipes: the `date-dimension` recipe gains a type-2 dim carrying both bound keys

**Not included:** `init` proposing any `date_ref`; `date_ref` on `mode: source` /
`mode: base` / `StreamConfig`; an object form of `scd_window` inside `date_ref`
(bare literal only — § Rationale); a bound-shape key satisfying
`Scd2NeedsHistory`; a provenance entry (or virtual source column) for the bound
shape; any change to `derived: scd_window`, the SCD-2 reconstruction, the range
guard, or the manifest. Architecture-doc updates (`date-dimension.md`,
`dimensional.md`, `documentation-channel.md`, `incremental.md`, promoting the
pending doc) ship separately after archival.

## Breaking Changes

Internal, no shims (CLAUDE.md Principle #9). The base-format contract is untouched.

| What changes | Effect on existing callers |
|---|---|
| `resolve_date_ref_source` returns `str \| None` | Its four `src` readers take a no-column path on `None` (§ Affected Subsystems); no test calls it directly |
| `TableReport` gains required `window_bounds: Mapping[str, Literal["valid_from", "valid_to"]]` | Both report-assembly sites and every test constructor state it (`{}` when the table carries no bound-shape key) — the `references` / `supplement` precedent |
| `QuerySpec` gains `window_bounds: … = field(default_factory=dict)` | Internal runtime type with defaulted siblings; no caller changes. The windowed compile's `dataclasses.replace` carries it unchanged |
| `render_date_ref_expr`'s `qualified_source` may now be a version-relation bound column | Existing callers pass what they passed; only `build_scd2_column_expr_flag` passes the new input |

Every existing config keeps loading and exporting byte-identically: the bound
shape is a new, mutually exclusive alternative, and `window_bounds` is empty
wherever no bound-shape column is declared.

## Success Criteria
- [ ] `date_ref: {scd_window: valid_from}` / `{scd_window: valid_to}` parses; any second shape beside it, or a value outside the two bounds, is refused at parse
- [ ] On a type-2 dim, the bound key equals `date_key_expr` over the sibling `derived: scd_window: {bound: <same>, as: date}` on every version row, across anchor zones (§ Invariants 2)
- [ ] `valid_to` key is `NULL` on the open version; `valid_from` key never `NULL`; same-day versions carry the same `valid_from` key with the version rows intact
- [ ] A bound shape on any non-type-2 table is refused at plan time by `DateRefWindowBoundRequiresScd2` with the pinned message, whether or not an anchor resolves; a bound shape with no anchor on a type-2 table is refused by `TemporalRenderRequiresAnchor` rendering `date`
- [ ] A bound-shape column stamps no `ColumnProvenance` entry, stamps `references[<name>] = "dim_date"`, and stamps `window_bounds[<name>] = <bound>`; both `write_query_specs` arms and the driver's windowed report forward `window_bounds` verbatim
- [ ] `valid_from` bound key is horizon-invariant (admitted in `key`; a dim carrying only `valid_from` bounds/keys can still be `append`); `valid_to` bound key varies (refused in `key` by `KeyColumnsStable`; makes its dim `upsert`)
- [ ] The README / manifest dictionary describes a bound-shape key with the pinned bound-shape prose naming the bound, or the author's `description`; `unit` is `None`; the manifest's `columns[].references` is `dim_date` and no shape-specific field is added
- [ ] The `date-dimension` recipe exports a type-2 dim with both bound keys and its `expect.yaml` pins the rows
- [ ] Every existing test passes; existing recipes are byte-identical

## Contracts

Signatures and docstrings only. Design-doc contracts are reproduced where the
implementer edits them; internal helpers the design leaves to the sprint are
added here.

### Config models (`src/fabulexa_forge/config/models.py`)

```python
class DateRefSpec(StrictBaseModel):
    """A `yyyymmdd` key into `dim_date`, from an instant, a parsed date
    string, or an SCD-2 version-window bound."""

    source: str | None = None
    """Instant shape: the sim_time source column, rendered to its local
    date in the anchor zone — the same availability as `TimestampSpec.source`."""
    from_: str | None = Field(default=None, alias="from")
    """Parse shape: the VARCHAR source column holding date strings."""
    format: str | None = None
    """Parse shape: the declared parse format (closed strptime-directive set,
    see validate_date_parse_format); must be date-complete. Present iff `from_`."""
    scd_window: Literal["valid_from", "valid_to"] | None = None
    """Bound shape: the SCD-2 version-window bound, rendered to its local
    date in the anchor zone — exactly `derived: scd_window: {bound, as: date}`.
    Bare literal only: the `date` election is implied. Legal only on an
    `scd: type2` table (business rule DateRefWindowBoundRequiresScd2)."""

    @model_validator(mode="after")
    def exactly_one_shape(self) -> Self:
        """Exactly one of `source` / `from_` / `scd_window` is set; `format`
        iff `from_`; a set `source` / `from_` is non-empty; `format` denotes
        a date (the existing denotation reading, unchanged).

        Raises:
            ValueError: Zero or more than one shape set (message: "date_ref:
                set exactly one of 'source' / 'from' / 'scd_window'" followed
                by the three values); `format` without `from_` or `from_`
                without `format`; an empty column name; a `format` that is
                not date-complete (time-only) or that fails the
                declared-parse directive rules.
        """
```

A `scd_window` value outside the two literals is a Pydantic type refusal — no
validator code.

### Column helpers (`src/fabulexa_forge/exporters/dimensional/columns.py`)

```python
def resolve_date_ref_source(spec: "DateRefSpec") -> str | None:
    """The one base column a `date_ref` spec reads, or None.

    The instant shape's `source`, the parse shape's `from_`, or None for
    the bound shape, which reads no base column — its input is the SCD-2
    version relation's bound. Shared by every reader of a `date_ref`
    column's source identity (provenance, the slice-only read surface,
    window-variance classification, the records-grain builder); each takes
    its no-column path on None, and a reader that needs the bound reads
    `spec.scd_window`.

    Args:
        spec: The column's `date_ref` spec.

    Returns:
        The source column name as declared, or None for the bound shape.
    """


def render_date_ref_expr(
    spec: "DateRefSpec",
    qualified_source: str,
    out_name: str,
    table_label: str,
    anchor: "EffectiveAnchor | None",
) -> str:
    """Render the SQL SELECT fragment for one `date_ref` column.

    Instant and bound shapes: `anchor_temporal_expr` with the `date`
    election over `qualified_source` (the caller has already enforced the
    anchor rule, so `anchor` is non-None here — asserted), then
    `date_key_expr`. Parse shape: over `date_parse_expr` (its loud
    non-matching-cell failure naming `table_label`, unchanged) cast to
    DATE, then `date_key_expr`. Either way the fragment ends in
    `AS "<out_name>"` and is NULL-propagating; every shape composes the
    bare forms of the shared renderers, never a re-spelling of their SQL.

    Args:
        spec: The column's `date_ref` spec.
        qualified_source: The fully table-qualified SQL for whichever
            input the spec's shape reads — the caller qualifies
            `spec.source` or `spec.from_` (grain alias, or the SCD-2
            tracked cast), or for the bound shape the version relation's
            `version_start` / `version_end` column.
        out_name: The output column name.
        table_label: The output table name, interpolated into the parse
            shape's mismatch error; unused by the other shapes.
        anchor: The resolved anchor; consulted by the instant and bound
            shapes only.

    Returns:
        A SQL SELECT-list expression fragment ending in `AS "<out_name>"`.
    """


def build_column_expr(
    col_decl: "ColumnDecl",
    anchor: "EffectiveAnchor | None",
    table_decl: "TableDecl | None" = None,
    source_grain: str | None = None,
    anchor_kind: str | None = None,
    config: "DimensionalConfig | None" = None,
    sidecar: "Sidecar | None" = None,
    election: "Election | None" = None,
    grain_alias: str = "_grain",
    source_table_name: str | None = None,
) -> tuple[str, list[str]]:
    """(existing contract unchanged, plus:)

    The `date_ref` branch asserts that `resolve_date_ref_source` returns a
    column — the records-grain builder never sees the bound shape
    (`DateRefWindowBoundRequiresScd2` refuses it on every non-type-2 table
    before compile) — as it asserts `table_decl`; it never renders a
    reference to a `None` column. Message: "date_ref on the records-grain
    builder reads a source column".

    Args / Returns / Raises: unchanged.
    """


def resolve_carried_source_column(col_decl: "ColumnDecl") -> str | None:
    """(existing contract unchanged, plus:)

    A bound-shape `date_ref` returns None — it reads no single source
    column, exactly as `derived: scd_window` does — so
    `build_column_provenance` stamps no entry for it.

    Args / Returns: unchanged.
    """
```

### SCD-2 builder (`src/fabulexa_forge/exporters/dimensional/scd.py`)

```python
def build_scd2_column_expr_flag(
    col_decl: "ColumnDecl",
    version_alias: str,
    records_alias: str,
    tracked_props: frozenset[str],
    anchor: "EffectiveAnchor | None",
    sidecar: "Sidecar",
    source_table_name: str,
    table_label: str,
) -> str:
    """(existing contract unchanged, plus:)

    A bound-shape `date_ref` renders through render_date_ref_expr handed
    `"<version_alias>"."version_start"` (`valid_from`) / `"version_end"`
    (`valid_to`) — the same input `derived: scd_window` reads — before any
    source-column resolution, beside the `derived: scd_window` and `null`
    branches; an instant- or parse-shape `date_ref` is handed the
    tracked/untracked source_expr as today.

    Args / Returns / Raises: unchanged.
    """
```

### Validation (`src/fabulexa_forge/exporters/dimensional/validation.py`)

```python
def check_date_ref_window_bound_on_scd2(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
) -> None:
    """Enforce DateRefWindowBoundRequiresScd2: a bound-shape `date_ref`
    declares on an `scd: type2` table.

    Always-on, full export included; a pure function of the declaration.
    The version window the shape addresses exists only on a versioned dim.
    Run by `validate_table`'s per-column loop ahead of
    `check_temporal_render_requires_anchor` (§ Validation Rules — rule
    ordering).

    Args:
        col_decl: The column declaration.
        table_decl: The output table declaration.

    Raises:
        ExportError: `col_decl.date_ref.scd_window` is set and
            `table_decl.scd != "type2"`. Message: "column '{name}' on table
            '{table}': date_ref scd_window: {bound} addresses the SCD-2
            version window, and the table is not scd: type2".
    """


def check_temporal_render_requires_anchor(
    col_decl: "ColumnDecl",
    anchor: "EffectiveAnchor | None",
) -> None:
    """Enforce TemporalRenderRequiresAnchor: an explicit election needs an anchor.

    Covers `derived: timestamp` with `as` set, the `scd_window` object form,
    and a `date_ref` instant or bound shape (each always an explicit `date`
    election). `elapsed: interval`, `date_parse`, and a `date_ref` parse
    shape are exempt.

    Args / Raises: unchanged (the bound shape renders `date` in the message).
    """


def _collect_value_read_sources(col_decl: "ColumnDecl") -> list[str]:
    """(existing contract unchanged, plus:)

    A bound-shape `date_ref` contributes no name — it reads no base column
    — so `check_slice_only_column_reads` sees nothing to classify for it.
    `None` is never appended.

    Args / Returns: unchanged.
    """


def validate_table(...) -> str:
    """(existing contract unchanged, plus:)

    The per-column rule loop runs `check_date_ref_window_bound_on_scd2`
    immediately before `check_temporal_render_requires_anchor` — after
    `check_scd2_column_mode_supported`, `check_projection_column_exists`,
    `check_ordinal_refs_siblings`, and `check_timestamp_source_available`,
    none of which read the bound shape.

    Args / Returns / Raises: unchanged, plus ExportError from the new rule.
    """
```

### Windowing (`src/fabulexa_forge/exporters/dimensional/windowing.py`)

```python
def _channel_variance(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
    config: "DimensionalConfig",
    sidecar: "Sidecar",
    source_table_name: str,
) -> str | None:
    """(existing contract unchanged, plus:)

    A bound-shape `date_ref` answers from its own branch, evaluated before
    any source-column lookup and mirroring the `derived: scd_window`
    branch: `valid_from` → None (invariant); `valid_to` → "is the SCD-2
    valid_to bound, closed by the next version". The branch is
    load-bearing: the reading's fall-through for a column with no source
    is "invariant", which would misclassify `valid_to`. An instant- or
    parse-shape `date_ref` classifies through `_source_variance` as today.
    `window_delivery_class` and `check_key_columns_stable` pick the answer
    up with no edit of their own.

    Args / Returns: unchanged.
    """
```

### Query spec + report (`src/fabulexa_forge/exporters/query_spec.py`)

```python
@dataclass(frozen=True)
class TableReport:
    """(existing fields unchanged; docstring adds:)

    `window_bounds` is forwarded verbatim from the compiled `QuerySpec` —
    no default, so every report-assembly call site states it explicitly.
    It maps each bound-shape `date_ref` output column to the bound it
    addresses; every key is also a `references` key mapped to `dim_date`.
    """

    # ... existing fields ...
    references: "Mapping[str, str]"
    window_bounds: "Mapping[str, Literal['valid_from', 'valid_to']]"


@dataclass(frozen=True)
class QuerySpec:
    """(existing fields unchanged; docstring adds:)

    `window_bounds` maps each bound-shape `date_ref` output column to the
    bound it addresses. Stamped at dimensional plan compile beside
    `references`, in declaration order (`_table_window_bounds`); every
    other mode and the calendar / supplement compilers leave it empty.
    Forwarded to `TableReport` by both report-assembly sites.
    """

    # ... existing fields ...
    calendar: "CalendarSource | None" = None
    window_bounds: "Mapping[str, Literal['valid_from', 'valid_to']]" = field(
        default_factory=dict
    )


def write_query_specs(
    emit: "Emit",
    specs: list[QuerySpec],
    out: "Path",
    fmt: Literal["csv", "duckdb"],
) -> ExportReport:
    """(existing contract unchanged, plus:) both arms forward
    `spec.window_bounds` onto the matching `TableReport`.
    """
```

### Incremental driver (`src/fabulexa_forge/incremental/driver.py`)

```python
def _build_windowed_report(
    specs: "Sequence[QuerySpec]",
    written: "Mapping[str, WrittenRelation]",
    include_keys: bool,
) -> WindowedExport:
    """(existing contract unchanged, plus:) forwards `spec.window_bounds`
    verbatim — windowed and full stamping are identical for the same table.
    """
```

### Dimensional plan compile (`src/fabulexa_forge/exporters/dimensional/engine.py`)

```python
def _table_window_bounds(
    table_decl: "TableDecl",
) -> "Mapping[str, Literal['valid_from', 'valid_to']]":
    """The table's bound-shape `date_ref` column -> bound map, declaration order.

    One entry per column whose `date_ref.scd_window` is set, valued with
    that bound; no entry otherwise. Pure. The sibling of `_table_references`
    — stamped onto the same `QuerySpec` at the same site, so every key here
    is a `references` key mapped to `dim_date` (§ Invariants 3).

    Args:
        table_decl: The output table declaration.

    Returns:
        Output column name -> "valid_from" | "valid_to"; empty when the
        table declares no bound-shape `date_ref`.
    """


def build_query_specs(...) -> list[QuerySpec]:
    """(existing contract unchanged, plus:) each full-compile spec's
    `window_bounds` is stamped from `_table_window_bounds(table_decl)`
    beside `references`. The windowed compile derives its specs from the
    full compile through `dataclasses.replace` and so carries it unchanged.
    """
```

### Dictionary (`src/fabulexa_forge/exporters/companion/dictionary.py`)

```python
_DATE_REF_WINDOW_BOUND_DESCRIPTION_TEMPLATE = (
    "Calendar key (`yyyymmdd`) into `dim_date`, derived from this version's"
    " `{bound}` bound"
)
"""A bound-shape `date_ref` column's pinned description, absent an author
override — `{bound}` the report's `window_bounds` entry for the column."""


def resolve_column_doc(
    doc: "Documentation", table: "TableReport", column_name: str, output_type: str
) -> "ColumnDoc | None":
    """(existing contract unchanged, except on a column whose `references`
    entry is `dim_date`:) with an `author_descriptions` entry, the author's
    description at origin "author"; without one, at origin "forge", the
    pinned bound-shape prose naming `table.window_bounds[column_name]` when
    the column has a `window_bounds` entry, else the pinned `date_ref`
    prose naming the column's provenance source column. Either way `unit`
    is None and no source doc is inherited. The branch reads whichever of
    `window_bounds` / `provenance` is present and never both.

    Args / Returns: unchanged.
    """
```

## Phases

### Phase 1: The bound shape — grammar, rendering, rules, windowing

**Delivers:** `DateRefSpec.scd_window`; `resolve_date_ref_source` nullable with
every reader on its no-column path; `render_date_ref_expr` over a version bound;
`build_scd2_column_expr_flag`'s bound branch; the records-grain builder's
assertion; `DateRefWindowBoundRequiresScd2`; `TemporalRenderRequiresAnchor`
covering the shape; the slice-only collector skipping it; the variance
reading's bound branch. A bound-shape key compiles, executes, agrees with its
sibling by construction, and is governed by every plan-time rule — below the
companion writer (the documentation channel lands in Phase 2, so this phase's
demo and tests stay at `build_query_specs` + DuckDB execution and never write
a README).

**Demo:** `phase_1_bound_shape_render.py` builds a self-contained SCD-2 emit
(one records kind with a tracked `prop__status`, a `history` table giving one
record three versions — two starting on the same local date under
`America/New_York`, the third open — and a second record with one version;
a `runtime` block so an anchor resolves), compiles a type-2 dim declaring
`valid_from` / `valid_to` as `scd_window {…, as: date}` beside
`valid_from_date_key` / `valid_to_date_key` bound keys, executes the SQL, and
prints the version rows showing: the key equals the sibling's `yyyymmdd`
on every row; `valid_to_date_key` is `NULL` on the open version; the two
same-day versions share a `valid_from` key and both rows survive. Then prints
`window_delivery_class` for the dim with and without the `valid_to` key
(`upsert` / `append`) and drives three refusals, each printed with its
message: `DateRefWindowBoundRequiresScd2` (the same column on a type-1 dim,
anchor present), `TemporalRenderRequiresAnchor` (the type-2 dim compiled with
`anchor=None`), and `KeyColumnsStable` (`valid_to_date_key` named in `key`).

**Contracts:** `DateRefSpec`, `resolve_date_ref_source`, `render_date_ref_expr`,
`build_column_expr`, `resolve_carried_source_column`,
`build_scd2_column_expr_flag`, `check_date_ref_window_bound_on_scd2`,
`check_temporal_render_requires_anchor`, `_collect_value_read_sources`,
`validate_table`, `_channel_variance`.

**Steps:** `source → author (2 files) → author (3 files)` — the source reshape
is small but spans five modules; the two author groups each read one slice of
the surface (grammar + rendering; rules + windowing + provenance) so neither
re-reads the whole of it.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/columns.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/scd.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/validation.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/windowing.py` |
| Modify | `tests/config/test_date_dimension.py` |
| Modify | `tests/exporters/dimensional/test_date_ref.py` |
| Modify | `tests/exporters/dimensional/test_validation.py` |
| Modify | `tests/exporters/dimensional/test_windowing.py` |
| Modify | `tests/exporters/dimensional/test_provenance.py` |
| Create | `docs/sprints/date-ref-scd-window/demos/phase_1_bound_shape_render.py` |

**Tests:**
- `tests/config/test_date_dimension.py` (grammar):
  - `DateRefSpec(scd_window="valid_from")` and `"valid_to"` accepted; `source`, `from_`, `format` all None.
  - `scd_window` beside `source` refused; beside `from` + `format` refused; all three refused — each message names the three shapes.
  - `scd_window: "as_of"` (outside the literal) refused as a type error.
  - `scd_window` with `format` and no `from` refused ("format is required iff 'from'").
  - `ColumnDecl(date_ref={scd_window: valid_from})` alone accepted; the `ColumnDecl` one-mode refusal and `ExportConfig.date_refs_require_date_dimension` fire for the bound shape exactly as for the instant shape (one case each).
  - Existing `DateRefSpec` cases still pass; the neither-shape message now lists `scd_window`.
- `tests/exporters/dimensional/test_date_ref.py` (rendering + execution, extending the existing `_scd_sidecar` / `_flag_expr` / `_build_scd2_emit`-style fixtures — add a versioned emit builder to this file or reuse `test_scd.py`'s pattern):
  - `resolve_date_ref_source` returns None for the bound shape, and the declared name for the other two.
  - `render_date_ref_expr` bound shape: composes `date_key_expr(anchor_temporal_expr(anchor, qualified_source, "date"))` byte-for-byte with `AS "<name>"`; asserts on `anchor=None`.
  - `build_scd2_column_expr_flag` bound shape: `valid_from` reads `"_versions"."version_start"`, `valid_to` reads `"_versions"."version_end"`; never `"_records"`; with a tracked-props set that would otherwise matter, still the version alias (the branch runs before source resolution).
  - End-to-end (compile + DuckDB): on every version row `valid_from_date_key == yyyymmdd(valid_from)` and `valid_to_date_key == yyyymmdd(valid_to)` where the siblings are `scd_window {…, as: date}`, under `UTC` and under `America/New_York` with a version starting 2h after midnight UTC (the key follows the local date).
  - Open version: `valid_to_date_key` is None; `valid_from_date_key` is never None across rows.
  - Two versions starting on one local date: both rows present, equal `valid_from` keys, distinct raw `version_start` (query the reconstruction's own bound or assert row count).
  - A bound key with no `scd_window` sibling compiles and executes (the date needn't travel beside the key).
  - Determinism: two compiles of the bound-shape config are byte-identical SQL.
  - Records-grain builder: `build_column_expr` with a bound-shape `date_ref` raises `AssertionError` naming the source-column requirement.
- `tests/exporters/dimensional/test_validation.py` (rules):
  - `check_date_ref_window_bound_on_scd2`: type-2 table passes for both bounds; `scd: type1` dim and a fact refuse with the pinned message naming column, table, and bound; instant / parse shapes pass on any table.
  - `check_temporal_render_requires_anchor`: bound shape with `anchor=None` raises naming the column and `'date'`; with an anchor passes.
  - `check_scd2_column_mode_supported` admits the bound shape (no raise).
  - `_collect_value_read_sources` returns `[]` for a bound-shape column (and `check_slice_only_column_reads` passes on a kind whose every `prop__` is `slice_only`).
  - `check_timestamp_source_available` and `check_date_parse_source_column` return without raising for the bound shape.
  - `validate_table` end-to-end: bound shape on a type-1 dim with `anchor=None` raises `DateRefWindowBoundRequiresScd2` (not the anchor rule) — the ordering pin; on a type-2 dim with an anchor passes; on a type-2 dim with `anchor=None` raises `TemporalRenderRequiresAnchor`.
- `tests/exporters/dimensional/test_windowing.py` (variance):
  - Type-2 dim with a `valid_from` bound key (and no `valid_to` of either form) classifies `append`; adding a `valid_to` bound key classifies `upsert`; adding `derived: scd_window: valid_to` alone also `upsert` (existing reading, unchanged).
  - `check_key_columns_stable`: `valid_from` bound key in `key` beside the `scd_window` column accepted; `valid_to` bound key in `key` refused with the message containing "is the SCD-2 valid_to bound, closed by the next version".
- `tests/exporters/dimensional/test_provenance.py`:
  - A bound-shape column has no `provenance` entry on the compiled spec while its `scd_window` sibling and carried columns are stamped as before.
  - `references` maps the bound-shape column to `dim_date`, in declaration order beside an `fk`.
- Existing: the whole suite passes (no existing test constructs a bound shape or reads `resolve_date_ref_source` directly).

### Phase 2: The report carriage, the documentation channel, and the recipe

**Delivers:** `QuerySpec.window_bounds` / `TableReport.window_bounds` stamped by
`_table_window_bounds` at dimensional plan compile and forwarded by
`write_query_specs` (both arms) and `_build_windowed_report`; the bound-shape
pinned dictionary prose; every existing `TableReport` constructor migrated; the
`date-dimension` recipe gains a type-2 dim carrying both bound keys. A
bound-shape key now exports end-to-end with a README and manifest.

**Demo:** `phase_2_documented_export.py` builds the same kind of SCD-2 emit
as Phase 1 with a `runtime` block, writes a `config.yaml` (`date_dimension`
block, the type-2 dim with `valid_from` / `valid_to` `scd_window` siblings, a
`valid_from_date_key` with an author `description`, and a `valid_to_date_key`
without one), runs `export_dimensional` to CSV, and prints: the dim's rows
(keys beside dates); the README's dictionary lines for the two key columns
(the author's prose vs. the pinned "derived from this version's `valid_to`
bound"); the manifest's `columns[]` entries for both keys (`references:
"dim_date"`, `description` per the above, no `unit`, and no other key present
that the instant shape lacks) and the `TableReport.window_bounds` map. Then
runs one `--from/--to` window through `export_window` and prints the windowed
report's `window_bounds` for the dim (identical to the full export's) and its
delivery class.

**Contracts:** `TableReport`, `QuerySpec`, `write_query_specs`,
`_build_windowed_report`, `_table_window_bounds`, `build_query_specs`,
`_DATE_REF_WINDOW_BOUND_DESCRIPTION_TEMPLATE`, `resolve_column_doc`.

**Steps:** `source → migrate (fan-out, 5 files) → author (5 files) → author (recipe, 2 files)`
— atomic: `TableReport.window_bounds` is required, so every existing
constructor is red until migrated. The recipe step is separate so the author
of the code tests never opens the recipe corpus.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/query_spec.py` |
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/engine.py` |
| Modify | `src/fabulexa_forge/exporters/companion/dictionary.py` |
| Modify | `tests/exporters/companion/_fixtures.py` |
| Modify | `tests/exporters/companion/test_manifest.py` |
| Modify | `tests/exporters/companion/test_readme.py` |
| Modify | `tests/exporters/companion/test_paths.py` |
| Modify | `tests/exporters/companion/test_artifacts.py` |
| Modify | `tests/exporters/companion/test_date_dimension.py` |
| Modify | `tests/exporters/test_query_spec.py` |
| Modify | `tests/exporters/dimensional/test_provenance.py` |
| Modify | `tests/exporters/dimensional/test_export_date_dimension.py` |
| Modify | `tests/incremental/test_date_dimension.py` |
| Modify | `examples/recipes/date-dimension/config.yaml` |
| Modify | `examples/recipes/date-dimension/expect.yaml` |
| Create | `docs/sprints/date-ref-scd-window/demos/phase_2_documented_export.py` |

**Tests:**
- Migration (mechanical, intent preserved): every `TableReport(...)` constructor in the five migrate files gains `window_bounds={}`.
- `tests/exporters/companion/test_date_dimension.py` (migrate its three constructors, then):
  - A report fixture with a bound-shape column: `references={<col>: "dim_date"}`, `window_bounds={<col>: "valid_to"}`, no `provenance` entry.
  - `resolve_column_doc` on it: description `"Calendar key (`yyyymmdd`) into `dim_date`, derived from this version's `valid_to` bound"`, origin `"forge"`, unit None; the `valid_from` variant names `valid_from`.
  - With an `author_descriptions` entry: the author's prose, origin `"author"`, unit None.
  - The instant / parse shape fixtures still resolve the source-naming prose (the branch reads `provenance` only when `window_bounds` has no entry).
  - Manifest: the bound-shape column's entry carries `references: "dim_date"` and the pinned description; the entry's key set equals an instant-shape column's key set (no shape-specific field); `manifest_format_version` is unchanged; bytes deterministic.
  - README: the dim's column line for the bound key renders the pinned prose with no unit.
- `tests/exporters/test_query_spec.py`: both `write_query_specs` arms forward `window_bounds` verbatim; a spec stamping nothing forwards `{}` (extend the empty-maps test).
- `tests/exporters/dimensional/test_provenance.py`: `build_query_specs` stamps `window_bounds` in declaration order for two bound-shape columns (`valid_from`, `valid_to`) and `{}` on a table without one; every `window_bounds` key is a `references` key mapped to `dim_date`; base and source mode specs have empty `window_bounds`.
- `tests/exporters/dimensional/test_export_date_dimension.py`: full export (CSV and DuckDB) of a type-2 dim with both bound keys and a `date_dimension` block succeeds, writes the README and manifest, reports `window_bounds` on the dim's `TableReport` and `{}` on `dim_date`; the range guard refuses a bound key outside the declared range before any write (`DateRefOutOfRange`, shape-blind).
- `tests/incremental/test_date_dimension.py`: a `--from/--to` window over the recipe emit with the type-2 bound-key dim: the windowed `TableReport.window_bounds` equals the full export's; the dim delivers `upsert` with the `valid_to` key and `append` with only `valid_from` bounds/keys (through `export_window`, not just the classifier).
- Recipe (`tests/recipes` gates, unchanged code): `examples/recipes/date-dimension` adds `dim_patient_status` — `scd: type2` over `patient`, `key: [patient_id, valid_from]`, columns `patient_id` / `status` / `valid_from` + `valid_to` (`scd_window {…, as: date}`) / `valid_from_date_key` + `valid_to_date_key` (bound keys), with header comments in the recipe's voice; `expect.yaml` pins `row_count: 4` and rows p001 pending (`20240102` → `20240103`), p001 discharged (`20240104` → null), p002 pending (`20240103` → null). Every other recipe's output is unchanged (determinism gate).
- Existing: the whole suite passes after migration.

## What Doesn't Change

- `dim_date` — name, thirteen columns, `compile_date_dimension_spec`, delivery class, pack membership (§ What Doesn't Change).
- The instant and parse shapes — grammar, availability rules, rendering SQL, provenance stamping, variance, pinned prose. Every existing `date_ref` test is an unchanged oracle.
- `check_date_refs_in_range` — probes by `references`, shape-blind; no edit.
- `_table_references` — no edit; it already maps every `date_ref` column to `dim_date`.
- The manifest builder (`companion/manifest.py`) and `_MANIFEST_FORMAT_VERSION` — no field is added; the bound is on record in the embedded config.
- `derived: scd_window` (`scd_window_bound` / `scd_window_render`, the `ScdWindowSpec` object form), `build_scd2_sql`, `build_versioned_intervals_sql`, and `check_scd2_needs_history` — untouched; a bound-shape key in `key` does not satisfy `Scd2NeedsHistory`.
- `check_scd2_column_mode_supported` — already admits `date_ref` shape-blind; no edit.
- `_build_windowed_query_specs` — carries `window_bounds` through `dataclasses.replace`; no edit.
- `ColumnProvenance`, `build_column_provenance` — no virtual source column; the bound shape's None falls out of `resolve_carried_source_column`.
- `init` (all three proposal engines), `StreamConfig`, `mode: source`, `mode: base` — no `date_ref`.
- `compute_fingerprint` — the config dump already carries the shape; no edit.

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/config/models.py` | `DateRefSpec.scd_window` + three-way `exactly_one_shape` |
| `src/fabulexa_forge/exporters/dimensional/columns.py` | `resolve_date_ref_source` → `str \| None`; `render_date_ref_expr` bound shape; `build_column_expr` source assertion; `resolve_carried_source_column` docstring |
| `src/fabulexa_forge/exporters/dimensional/scd.py` | Bound-shape branch before source resolution |
| `src/fabulexa_forge/exporters/dimensional/validation.py` | `check_date_ref_window_bound_on_scd2`; anchor rule covers the bound shape; `_collect_value_read_sources` skips it; `validate_table` loop order |
| `src/fabulexa_forge/exporters/dimensional/windowing.py` | `_channel_variance` bound-shape branch |
| `src/fabulexa_forge/exporters/query_spec.py` | `QuerySpec.window_bounds`, `TableReport.window_bounds`, both `write_query_specs` arms forward |
| `src/fabulexa_forge/incremental/driver.py` | `_build_windowed_report` forwards `window_bounds` |
| `src/fabulexa_forge/exporters/dimensional/engine.py` | `_table_window_bounds`; `build_query_specs` stamps it |
| `src/fabulexa_forge/exporters/companion/dictionary.py` | `_DATE_REF_WINDOW_BOUND_DESCRIPTION_TEMPLATE`; `resolve_column_doc` bound-shape branch |
| `tests/config/test_date_dimension.py` | Bound-shape grammar cases |
| `tests/exporters/dimensional/test_date_ref.py` | Bound-shape rendering, SCD-2 builder, execution, agreement, NULL, same-day, determinism |
| `tests/exporters/dimensional/test_validation.py` | New rule, anchor rule, slice-only no-op, rule order |
| `tests/exporters/dimensional/test_windowing.py` | Bound-shape variance and `KeyColumnsStable` |
| `tests/exporters/dimensional/test_provenance.py` | No provenance entry; `references`; `window_bounds` stamping |
| `tests/exporters/companion/_fixtures.py` | `TableReport` constructors gain `window_bounds={}` |
| `tests/exporters/companion/test_manifest.py` | Same |
| `tests/exporters/companion/test_readme.py` | Same |
| `tests/exporters/companion/test_paths.py` | Same |
| `tests/exporters/companion/test_artifacts.py` | Same |
| `tests/exporters/companion/test_date_dimension.py` | Migration + bound-shape dictionary / manifest / README cases |
| `tests/exporters/test_query_spec.py` | `window_bounds` forwarding, both arms |
| `tests/exporters/dimensional/test_export_date_dimension.py` | End-to-end full export with bound keys; range guard shape-blind |
| `tests/incremental/test_date_dimension.py` | Windowed forwarding and delivery through `export_window` |
| `examples/recipes/date-dimension/config.yaml` | `dim_patient_status` type-2 dim with both bound keys |
| `examples/recipes/date-dimension/expect.yaml` | Pinned rows for the new dim |
| `docs/sprints/date-ref-scd-window/demos/phase_1_bound_shape_render.py` | Phase 1 demo |
| `docs/sprints/date-ref-scd-window/demos/phase_2_documented_export.py` | Phase 2 demo |
