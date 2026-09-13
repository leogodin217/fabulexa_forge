# Sprint: date-dimension

## Purpose

Ship the generated calendar dimension and the `date_ref` column mode for the
dimensional exporter: a top-level `date_dimension: {from, to}` block
materializes a forge-defined `dim_date` (pinned 13-column shape, `INTEGER`
`yyyymmdd` key), and `date_ref: {source}` / `date_ref: {from, format}` columns
render keys into it from a structural instant or a parsed date string, guarded
against the declared range before any write.

An author adds one block and one column to an existing star-schema config —

```yaml
date_dimension: {from: "1985-01-01", to: "2024-12-31"}
...
        - {name: event_date_key, date_ref: {source: sim_time}}
```

— runs `fabulexa-forge export`, and gets a `dim_date` table every fact and dim
can join for by-month / by-weekday / by-quarter analysis, with the manifest
naming the calendar's range and every column that references it.

Design and semantics: `docs/architecture/pending/date-dimension.md` (the WHY —
three rounds of `/arch-review`). This spec carries the contracts, phases, and
tests (the WHAT). Where the two disagree on *placement*, this spec wins; the
three placements it adjusts are listed under *Placement notes* in § Contracts.

## Scope

**Capabilities touched:**
- Dimensional exporter: the `date_ref` column mode on every grain (records /
  interval / SCD-2), the six extended business rules, the `dim_date` compile in
  the supplement delivery slot, the `DateRefOutOfRange` range guard as the last
  pre-write gate.
- Export config: `DateDimensionConfig`, `DateRefSpec`, `ColumnDecl.date_ref`,
  three new `ExportConfig` validators.
- Temporal elections: the two shared renderers split into bare-expression
  authorities (`anchor_temporal_expr`, `date_parse_expr`) that the existing
  aliased fragments wrap — rendered SQL byte-identical.
- Compiled-table representation: `QuerySpec.calendar` / `.references`,
  `TableReport.calendar` / `.references`.
- Incremental driver: `dim_date` as `snapshot` per window, `date_ref` under the
  delivery classifier, per-window range guard.
- Shaped playback: `dim_date` in `tables()` after the declared tables and
  before the supplements; per-ask range guard.
- Companion artifacts: manifest `tables[].calendar` + `columns[].references`
  (`date_ref` *and* `fk`), `manifest_format_version` 4 → 5, forge-pinned
  `dim_date` and `date_ref` dictionary prose.
- Recipes: `examples/recipes/date-dimension/` (both shapes + the calendar).

**Not included:** everything the design doc's *What Doesn't Change* excludes —
no `date_dimension` / `date_ref` on source, base, or streaming; no `init`
proposal; no `scd_window` source shape; no fiscal calendar / holidays / locale;
no `declare_keys` on `dim_date`; no change to `fk`'s grammar or resolution.
Architecture-doc promotion (`pending/` → live, `dimensional.md`,
`companion-artifacts.md`, `incremental.md`, `playback.md`, `CAPABILITIES.md`,
README status) ships in a separate commit after ACCEPT.

## Breaking Changes

- **`TableReport` gains two required fields** (`calendar`, `references`), no
  default by design so every report-assembly site states them. The three source
  sites (`write_query_specs` × 2, `_build_windowed_report`) and every test
  fixture constructing `TableReport(...)` directly migrate in Phase 6, atomically
  with the type change.
- **`manifest_format_version` 4 → 5.** Every table object gains `calendar`
  (`null` except on `dim_date`); every column object gains `references` (`null`
  unless the column is a `date_ref` or `fk`). No top-level key changes. The two
  tests pinning `== 4` migrate in Phase 6.
- **`ShapedPlayback.__init__` gains `calendar_spec`** (internal constructor;
  `open_shaped_playback` is the only caller).
- `render_anchor_temporal_expr` and `render_date_parse_expr` keep their
  signatures and their rendered SQL byte-for-byte; they become wrappers over the
  new bare authorities. Existing configs and outputs are unaffected.
- `ColumnDecl` gains a seventh mode. Purely additive for existing configs; the
  `exactly_one_column_mode` message's mode list grows.

## Success Criteria

- [ ] `date_dimension: {from, to}` on a dimensional config writes `dim_date`
      with exactly the 13 pinned columns, in order, `n` rows for an `n`-day
      range, byte-identical for the same range regardless of emit / anchor /
      format / window.
- [ ] `date_ref {source}` and `date_ref {from, format}` render `yyyymmdd`
      `INTEGER` keys on records, interval, and SCD-2 grains; a `date_ref
      {source: c}` equals `date_key_expr` over `derived: timestamp {source: c,
      as: date}` on every row (family agreement); `NULL` sources yield `NULL`.
- [ ] Every non-`NULL` `date_ref` value in a written or returned table names a
      `dim_date` row, or the invocation refused with `DateRefOutOfRange` before
      writing anything — on full export, per window, and per shaped ask.
- [ ] The six existing business rules refuse the new mode's failure cases with
      their existing messages; `date_dimension` under `mode: source` / `base`,
      `date_ref` without the block, and a `dim_date`-named table or supplement
      beside the block are refused at load.
- [ ] Incremental: `dim_date` is `snapshot` in every emitting window; a
      `date_ref` over a varying source makes its table `upsert`; a changed range
      mid-drip is a fingerprint mismatch.
- [ ] Shaped playback: `tables()` lists `dim_date` after the declared tables,
      before the supplements; a `dim_date`-only ask opens no horizon.
- [ ] Manifest v5 carries `tables[].calendar` and `columns[].references`; the
      dictionary answers `dim_date` and `date_ref` prose from the forge-pinned
      set with `origin: "forge"` and `unit: None`, and an author-described
      `date_ref` over a `ns` source still carries `unit: None`.
- [ ] `examples/recipes/date-dimension/` loads, exports, and asserts under the
      recipe gate.
- [ ] `make test` green; `pre-commit` green on every touched file.

## Contracts

Signatures and docstrings only. Semantics are the design doc's; this section
adds module homes and the deltas to existing functions.

### Placement notes (where this spec adjusts the design doc)

1. `render_date_ref_expr` takes a `DateRefSpec` (a config model), so it cannot
   live in `_sql.py` (`config/models.py` imports `_sql`; a cycle). It lives in
   `exporters/dimensional/columns.py` beside `build_timestamp_expr` /
   `build_date_parse_expr`, which already take config models and import both
   shared renderers. Name unchanged.
2. `fk`'s reference stamp lives where `QuerySpec` is assembled
   (`engine.build_query_specs`, via a new `_table_references`), reusing
   `check_fk_target_is_dim` for the resolved dim name. `fk.py` is not modified.
3. The doc's single "extended `DateParseSourceColumn`" row is two existing
   rules in code — existence is `check_projection_column_exists`, type is
   `check_date_parse_source_column` — exactly as `derived: date_parse` splits
   today. Both extend; each keeps its own message.

### `src/fabulexa_forge/config/models.py`

`DateDimensionConfig` goes after `IncrementalConfig`; `DateRefSpec` after
`DateParseSpec`. Both satisfy `tests/config/test_docstring_convention.py`
(one-line class docstring, attribute docstrings ≤ 400 chars, no
`Field(description=...)`).

```python
class DateDimensionConfig(StrictBaseModel):
    """The inclusive calendar range the generated `dim_date` covers."""

    from_: date = Field(alias="from")
    """First calendar day, inclusive. Loaded from an ISO `YYYY-MM-DD` string."""
    to: date
    """Last calendar day, inclusive; not before `from_` (`range_ordered`)."""

    @model_validator(mode="before")
    @classmethod
    def bounds_are_strings(cls, data: object) -> object:
        """Refuse any non-string value for either bound.

        Both bounds must arrive as `str` and be parsed here, so the author
        sees this rule rather than a type-union failure — and so no other
        input Pydantic's `date` would accept (an unquoted YAML date, or an
        integer read as a Unix timestamp) can parse silently to a wrong day.
        Reads the alias key `from` (and `from_` under populate_by_name).

        Raises:
            ValueError: `from` or `to` is present and not a `str`.
        """

    @model_validator(mode="after")
    def range_ordered(self) -> Self:
        """`to` is not before `from_`.

        Raises:
            ValueError: `to < from_`.
        """


class DateRefSpec(StrictBaseModel):
    """A `yyyymmdd` key into `dim_date`, from an instant or a parsed date string."""

    source: str | None = None
    """Instant shape: the sim_time source column, rendered to its local
    date in the anchor zone — the same availability as `TimestampSpec.source`."""
    from_: str | None = Field(default=None, alias="from")
    """Parse shape: the VARCHAR source column holding date strings."""
    format: str | None = None
    """Parse shape: the declared parse format (closed strptime-directive set,
    see validate_date_parse_format); must be date-complete. Present iff `from_`."""

    @model_validator(mode="after")
    def exactly_one_shape(self) -> Self:
        """Exactly one of `source` / `from_` is set; `format` iff `from_`;
        a set `source` / `from_` is non-empty; `format` denotes a date.

        Date-completeness is read through the one denotation authority:
        `validate_date_parse_format(format, "date_ref.format")` then
        `date_parse_denoted_type(format) in ("DATE", "TIMESTAMP")` — a
        `TIME` denotation (time-only format) is refused.

        Raises:
            ValueError: Neither or both shapes set; `format` without `from_`
                or `from_` without `format`; an empty column name; a
                `format` that is not date-complete (time-only) or that
                fails the declared-parse directive rules.
        """


class ColumnDecl(StrictBaseModel):
    """One output column declaration with exactly one source mode."""

    date_ref: DateRefSpec | None = None
    """Renders a `yyyymmdd` key into the generated `dim_date`."""

    @model_validator(mode="after")
    def exactly_one_column_mode(self) -> Self:
        """A ColumnDecl sets exactly one of
        from / fk / correlation / derived / null / lookup / date_ref.

        Delta: `date_ref` joins the set-fields list; the message's mode list
        becomes `from/fk/correlation/derived/null/lookup/date_ref`.

        Raises:
            ValueError: zero or more than one mode is set.
        """


class ExportConfig(StrictBaseModel):
    """Top-level export configuration block."""

    date_dimension: DateDimensionConfig | None = None
    """Present → the generated calendar `dim_date` is materialized over the
    declared range and `date_ref` columns may reference it. Legal only with
    mode='dimensional' (`date_dimension_requires_dimensional`). Absent → no
    calendar, and any `date_ref` is refused (`date_refs_require_date_dimension`)."""

    @model_validator(mode="after")
    def date_dimension_requires_dimensional(self) -> Self:
        """A present `date_dimension` requires mode='dimensional'.

        Raises:
            ValueError: `date_dimension` present under mode='source' / 'base'.
        """

    @model_validator(mode="after")
    def date_refs_require_date_dimension(self) -> Self:
        """Any `date_ref` column in the dimensional tables requires
        `date_dimension` (message names the table and column).

        Raises:
            ValueError: A `date_ref` column exists and `date_dimension` is None.
        """

    @model_validator(mode="after")
    def dim_date_name_reserved(self) -> Self:
        """With `date_dimension` present, no declared dimensional table and
        no supplement is named `dim_date` — the generated calendar's
        published name (the literal is spelled here; config.models does not
        import exporters). Without the block the name is the author's to use.

        Raises:
            ValueError: A declared table or supplement is named `dim_date`
                while `date_dimension` is present.
        """
```

`ExportConfig.model_dump(mode="json")` serializes the block's `from_` as
`from_`, exactly as `DateParseSpec.from_` does today — pre-existing posture, and
what the fingerprint and the manifest's embedded config carry.

### `src/fabulexa_forge/_sql.py`

```python
def date_key_expr(date_sql: str) -> str:
    """The one `yyyymmdd` key expression, over a DATE-typed SQL expression.

    Renders `CAST(year(d) * 10000 + month(d) * 100 + day(d) AS INTEGER)`
    with `d` the given expression, NULL-propagating. Read by the `dim_date`
    relation and by every `date_ref` compile so the calendar's key and every
    reference into it agree by construction.

    Args:
        date_sql: A SQL expression of type DATE.

    Returns:
        A SQL expression of type INTEGER (no alias).
    """


def date_parse_expr(
    qualified_source: str,
    date_format: str,
    table_label: str,
) -> str:
    """The bare (unaliased) expression of the declared-parse renderer.

    Byte-identical to the `CASE` expression `render_date_parse_expr` aliases
    today — the loud non-matching-cell guard naming `table_label` included;
    that function becomes this expression wrapped in `AS "<out_name>"`.

    Args:
        qualified_source: The fully table-qualified VARCHAR source SQL.
        date_format: The validated declared parse format.
        table_label: The output table name interpolated into the guard.

    Returns:
        A SQL expression of the format's denoted type (no alias).
    """


def render_date_parse_expr(
    qualified_source: str,
    date_format: str,
    out_name: str,
    table_label: str,
) -> str:
    """Delta: the body becomes `date_parse_expr(...)` wrapped in
    `AS "<out_name>"`. Rendered SQL is byte-identical to today; the existing
    tests in tests/test_sql.py pin this without change."""
```

### `src/fabulexa_forge/anchor.py`

```python
def anchor_temporal_expr(
    anchor: EffectiveAnchor,
    qualified_source: str,
    render: TemporalRender,
) -> str:
    """The bare (unaliased) expression of the shared temporal renderer.

    Byte-identical to the expression `render_anchor_temporal_expr` aliases
    today; that function becomes this expression wrapped in `AS
    "<out_name>"` (with its no-anchor pass-through unchanged), so the one
    renderer every wallclock mode shares stays one. The decimal authority's
    posture: bare expression, caller aliases.

    Args:
        anchor: The resolved anchor (the no-anchor path never reaches here).
        qualified_source: The fully table-qualified BIGINT-ns source SQL.
        render: The elected temporal rendering.

    Returns:
        A SQL expression of the elected type (no alias).
    """


def render_anchor_temporal_expr(
    anchor: EffectiveAnchor | None,
    qualified_source: str,
    out_name: str,
    render: TemporalRender,
) -> str:
    """Delta: the `anchor is None` pass-through is unchanged; every other
    branch becomes `anchor_temporal_expr(...)` wrapped in `AS "<out_name>"`.
    Rendered SQL is byte-identical to today."""
```

### `src/fabulexa_forge/errors.py`

Directly after `DateParseSourceColumn` (the dimensional column-mode block).

```python
class DateRefOutOfRange(ExportError):
    """A `date_ref` column carries a non-NULL date outside the declared
    `date_dimension` range — a reference no `dim_date` row answers. Raised
    before any write. Message: `"table '{table}' column '{column}': date key
    {key} lies outside date_dimension {from}..{to}"` — `{key}` the integer
    as it appears in the column, `{from}` / `{to}` ISO dates."""
```

### Create `src/fabulexa_forge/exporters/date_dimension.py`

Beside `exporters/supplements.py` — the mode-neutral position the supplement
compile and cell probe occupy, importable by `query_spec.py` and
`companion/dictionary.py` without reaching into `exporters/dimensional/`.
Imports `_sql.date_key_expr`, `errors.DateRefOutOfRange`,
`exporters.query_spec.QuerySpec`; `config.models.DateDimensionConfig` and
`reader.emit.Emit` under `TYPE_CHECKING`.

```python
DATE_DIMENSION_TABLE_NAME: Final = "dim_date"
"""The generated calendar's published output-table name — mode-definitional."""

DATE_DIMENSION_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("date_key", "INTEGER"), ("date", "DATE"), ("year", "INTEGER"),
    ("quarter", "INTEGER"), ("month", "INTEGER"), ("day", "INTEGER"),
    ("day_of_week", "INTEGER"), ("day_of_year", "INTEGER"),
    ("iso_year", "INTEGER"), ("iso_week", "INTEGER"),
    ("month_name", "VARCHAR"), ("day_name", "VARCHAR"),
    ("is_weekend", "BOOLEAN"),
)
"""The pinned (name, type-text) pairs of `dim_date`, in output order — the
one authority the relation, the documentation dictionary, and the tests read."""


@dataclass(frozen=True)
class CalendarSource:
    """Manifest-facing provenance of the generated calendar: the declared
    inclusive range."""

    from_: date
    to: date


def compile_date_dimension_spec(
    config: DateDimensionConfig,
    write_mode: Literal["create", "replace"],
) -> QuerySpec:
    """Compile the generated calendar into a QuerySpec.

    The SQL is one `generate_series(DATE from, DATE to, INTERVAL 1 DAY)`
    relation, each element cast to DATE, projected to the pinned
    `DATE_DIMENSION_COLUMNS` in order (every integer part cast to INTEGER,
    `date_key` through `date_key_expr`, `day_of_week` ISO 1 = Monday,
    `is_weekend` = `day_of_week >= 6`, English month / day names), ordered
    by `date_key`. Carries `table_name=DATE_DIMENSION_TABLE_NAME`, no keys,
    empty provenance / kind_values / author_descriptions / references, no
    table description, `event_log=False`, `supplement=None`, and
    `CalendarSource(from, to)`. A pure function of the config block — no
    emit, no anchor, no session — so every caller compiles it before any
    horizon opens.

    Args:
        config: The validated `date_dimension` block.
        write_mode: 'create' for a full export, 'replace' for a windowed
            compile — the caller's delivery regime, never inferred.

    Returns:
        The `dim_date` QuerySpec.
    """


def check_date_refs_in_range(
    emit: Emit,
    specs: Sequence[QuerySpec],
    config: DateDimensionConfig,
) -> None:
    """The range guard: every non-NULL `date_ref` value lies in the calendar.

    For each spec whose `references` maps at least one column to
    `DATE_DIMENSION_TABLE_NAME`, evaluates one aggregate query over the
    spec's relation via `emit.query` — `MIN` and `MAX` of each such column,
    plus `date_key_expr` over `DATE '<from>'` and `DATE '<to>'` as the two
    bound keys, in the same statement — and compares each column's minimum
    against the lower bound and maximum against the upper. Runs before the
    first write of an invocation (full export), before each window's write
    (incremental), and before each ask returns (shaped playback). Columns
    are checked in the spec's output order (`references` insertion order —
    stamped by the plan compile in declaration order); the first violating
    column refuses, its minimum reported when that is below the lower
    bound, else its maximum. NULL aggregates (an empty relation, or an
    all-NULL column) never violate. A spec whose `references` names no
    `dim_date` column is not probed. Reads only `spec.sql`,
    `spec.references`, and the block — never a sidecar, never a tape of
    its own: the compiled SQL is self-contained (the truncated-tape CTEs
    are inlined), so it runs on the entry point's own `emit`.

    Args:
        emit: The open emit whose session evaluates each relation.
        specs: The compiled specs of the invocation / window / ask.
        config: The validated `date_dimension` block.

    Raises:
        DateRefOutOfRange: A non-NULL value lies outside `[from, to]`; names
            the table, the column, the offending key as it appears in the
            column, and the declared range as ISO dates.
    """
```

### `src/fabulexa_forge/exporters/query_spec.py`

```python
@dataclass(frozen=True)
class QuerySpec:
    """Delta: two fields appended after `supplement`."""

    calendar: CalendarSource | None = None
    """Set iff this spec is the generated `dim_date`; forwarded to
    `TableReport` by both report-assembly sites."""
    references: Mapping[str, str] = field(default_factory=dict)
    """Output column name -> referenced output table name, for every
    `date_ref` column (-> `dim_date`) and every `fk` column (-> the resolved
    dim table). Empty when the table references nothing. Stamped at plan
    compile (dimensional only; every other mode leaves it empty); forwarded
    to `TableReport`."""


@dataclass(frozen=True)
class TableReport:
    """Delta: two no-default fields appended after `supplement`, forwarded
    verbatim from the compiled QuerySpec — no default, so every
    report-assembly call site states them explicitly."""

    calendar: CalendarSource | None
    references: Mapping[str, str]


def write_query_specs(
    emit: Emit,
    specs: list[QuerySpec],
    out: Path,
    fmt: Literal["csv", "duckdb"],
) -> ExportReport:
    """Delta: both `TableReport(...)` constructions (DuckDB arm, CSV arm) add
    `calendar=spec.calendar, references=spec.references`. The range guard
    does NOT run here — it is the caller's last pre-write gate."""
```

`CalendarSource` is imported under `TYPE_CHECKING`, as `SupplementSource` is.
`QuerySpec.references` lands in Phase 2 and `QuerySpec.calendar` in Phase 3
(each with its first reader); the `TableReport` fields land in Phase 6 with
their first reader (the manifest).

### `src/fabulexa_forge/exporters/dimensional/columns.py`

```python
def render_date_ref_expr(
    spec: DateRefSpec,
    qualified_source: str,
    out_name: str,
    table_label: str,
    anchor: EffectiveAnchor | None,
) -> str:
    """Render the SQL SELECT fragment for one `date_ref` column.

    Instant shape: `anchor_temporal_expr` with the `date` election over
    `qualified_source` (the caller has already enforced the anchor rule, so
    `anchor` is non-None here — asserted), then `date_key_expr`. Parse
    shape: `date_parse_expr` over `qualified_source` (its loud
    non-matching-cell failure naming `table_label`, unchanged) cast to
    DATE, then `date_key_expr`. Either way the fragment ends in
    `AS "<out_name>"` and is NULL-propagating; both shapes compose the bare
    forms of the shared renderers, never a re-spelling of their SQL.

    Args:
        spec: The column's `date_ref` spec.
        qualified_source: The fully table-qualified source column SQL for
            whichever shape the spec carries (the caller qualifies
            `spec.source` or `spec.from_` — grain alias, or the SCD-2
            tracked cast).
        out_name: The output column name.
        table_label: The output table name, interpolated into the parse
            shape's mismatch error; unused by the instant shape.
        anchor: The resolved anchor; consulted by the instant shape only.

    Returns:
        A SQL SELECT-list expression fragment ending in `AS "<out_name>"`.
    """


def build_column_expr(...) -> tuple[str, list[str]]:
    """Delta (signature unchanged): a seventh dispatch arm, after `null` and
    before `fk`. `date_ref` asserts `table_decl` (the parse shape's
    `table_label`), qualifies `spec.source` / `spec.from_` under the grain
    alias, and returns `(render_date_ref_expr(...), [])`. The trailing
    "no column mode set" assertion is unchanged."""


def resolve_carried_source_column(col_decl: ColumnDecl) -> str | None:
    """Delta: `date_ref` joins the source-bearing spellings — `spec.source`
    for the instant shape, `spec.from_` for the parse shape. This is what
    makes `build_column_provenance` stamp `ColumnProvenance(source_table=
    <the grain's resolved source table>, source_column=<source or from>)`
    for a `date_ref` column with no change of its own, and what lets
    `build_scd2_column_expr_flag` resolve a `date_ref` source's
    tracked/untracked class. Docstring's mode list updates."""
```

### `src/fabulexa_forge/exporters/dimensional/scd.py`

```python
def build_scd2_column_expr_flag(...) -> str:
    """Delta (signature unchanged): after the tracked/untracked `source_expr`
    is formed and before the `derived: timestamp` arm, a `date_ref` arm
    returns `render_date_ref_expr(col_decl.date_ref, source_expr,
    col_decl.name, table_label, anchor)` — tracked sources read per version
    through the declared-type cast, untracked per record, the
    source-class-blind posture the other value renderings have. The
    compilation-per-mode docstring list gains the `date_ref` bullet."""
```

### `src/fabulexa_forge/exporters/dimensional/validation.py`

Signatures unchanged for every rule; each reads one more spelling. Existing
messages are kept verbatim (the doc: "existing message").

```python
def check_projection_column_exists(col_decl, table_decl, surface) -> None:
    """Delta: a `date_ref` parse shape's `from_` resolves off the grain's
    projectable surface exactly as `date_parse.from` does."""


def check_timestamp_source_available(col_decl, table_decl, source, surface) -> None:
    """Delta: the source under test is `derived.timestamp.source` when set,
    else `date_ref.source` when the instant shape is set, else the rule does
    not apply. Availability reading and message unchanged."""


def check_temporal_render_requires_anchor(col_decl, anchor) -> None:
    """Delta: a `date_ref` instant shape is an explicit `date` election and
    requires the anchor; the parse shape is exempt (reads no sim_time).
    Existing message, naming the column."""


def check_date_parse_source_column(col_decl, table_decl, source_table_name, sidecar) -> None:
    """Delta: reads `derived.date_parse.from_` or `date_ref.from_` (parse
    shape); VARCHAR requirement and `DateParseSourceColumn` message
    unchanged."""


def _collect_value_read_sources(col_decl) -> list[str]:
    """Delta: appends `date_ref.source` / `date_ref.from_` (whichever is
    set) — both shapes join `SliceOnlyColumnRefused`'s exhaustive value-read
    surface; `check_slice_only_column_reads` is unchanged."""


def check_scd2_column_mode_supported(col_decl, table_decl) -> None:
    """Delta: no refusal-logic change — `date_ref` is admitted by falling
    through the refused-mode ladder. The docstring's admitted-mode list and
    the message's "type2 columns support only ..." clause name `date_ref`."""


def validate_table(...) -> str:
    """Docstring delta only: the "Runs:" list notes which rules read
    `date_ref` sources. No call-order change; the range guard is a data
    guard at the entry points, never a `validate_table` rule."""
```

### `src/fabulexa_forge/exporters/dimensional/windowing.py`

```python
def _channel_variance(col_decl, table_decl, config, sidecar, source_table_name) -> str | None:
    """Delta: after the `derived` block, a `date_ref` column's channel is its
    source's — `date_ref.source` or `date_ref.from_` — classified through
    `_source_variance` exactly as `derived: timestamp` `source` /
    `date_parse.from` are. This is the ONE per-column variance reading;
    `window_delivery_class` and `check_key_columns_stable` change behaviour
    through it with no edit of their own: a `date_ref` over
    `deactivated_at` / `left_sim_time` / `lead_sim_time` / a tracked
    property makes the table `upsert`, and in `key` is refused with the
    existing `KeyColumnsStable` message naming the varying source."""
```

`_ordinal_variance` is unchanged: a `date_ref` named by `order_by` classifies
as "ordered by '<col>', not the grain's <key>" — outside the ordinal amendment,
like `date_parse`.

### `src/fabulexa_forge/exporters/dimensional/engine.py`

```python
def _table_references(
    table_decl: TableDecl,
    config: DimensionalConfig,
) -> Mapping[str, str]:
    """The table's output-column -> referenced-output-table map, declaration order.

    One entry per `fk` column (`check_fk_target_is_dim(col, table_decl,
    config).name` — the resolved dim's declared name) and per `date_ref`
    column (`DATE_DIMENSION_TABLE_NAME`); no entry otherwise. Pure; every
    fk target has already passed FkTargetIsDim in `validate_table`.

    Args:
        table_decl: The output table declaration.
        config: The dimensional config (fk resolution).

    Returns:
        Output column name -> referenced output table name; empty when the
        table references nothing.
    """


def build_query_specs(...) -> list[QuerySpec]:
    """Delta (signature unchanged): each appended `QuerySpec` carries
    `references=_table_references(table_decl, config)`; `calendar` stays
    None (the plan never compiles `dim_date` — the entry points do).
    `_build_windowed_query_specs` needs no edit: it derives every windowed
    spec via `dataclasses.replace(end_spec, ...)`, which carries
    `references` through."""


def export_dimensional(
    emit: Emit,
    config: ExportConfig,
    out: Path,
    fmt: Literal["csv", "duckdb"],
    anchor: EffectiveAnchor | None,
    notice_sink: NoticeSink,
    overlay: ReadmeOverlay | None,
    supplements: Sequence[ResolvedSupplement],
) -> ExportReport:
    """Delta (signature unchanged), in order after the plan compile and
    before any write:
    1. `calendar_specs` = `[compile_date_dimension_spec(config.date_dimension,
       "create")]` when the block is present, else `[]`.
    2. `supplement_specs = compile_supplement_specs(...)` as today.
    3. The output-name list handed to `check_supplement_sources_not_outputs`
       (through `_dimensional_output_paths`) and to `validate_overlay_tables`
       is plan names + calendar name + supplement names — so `dim_date.csv`
       joins the source-is-output list and `table: dim_date` is a valid
       overlay slot. Neither gate's signature changes.
    4. LAST pre-write gate: `check_date_refs_in_range(emit, specs,
       config.date_dimension)` when the block is present.
    5. `write_query_specs(emit, [*specs, *calendar_specs, *supplement_specs],
       out, fmt)` — plan iteration order: declared tables, `dim_date`,
       supplements.
    Returns one `TableReport` per declared table, then `dim_date` when
    declared, then one per supplement. Raises: adds `DateRefOutOfRange`."""
```

### `src/fabulexa_forge/incremental/driver.py`

```python
def export_window(...) -> WindowedExport:
    """Delta (dimensional arm only; source / base arms untouched): after the
    mode compile, `calendar_specs = [compile_date_dimension_spec(
    config.date_dimension, "replace")]` when the block is present — delivery
    `snapshot` in every emitting window, the empty window included, a
    supplement's posture; `all_specs = [*specs, *calendar_specs,
    *supplement_specs]` so `_windowed_output_paths` and the overlay check
    read the widened name list unchanged; then, as the LAST pre-write gate —
    after the source-is-output gate and the overlay check, before the
    `declare_keys` notice and the fmt dispatch — `check_date_refs_in_range(
    emit, specs, config.date_dimension)` over the window's compiled
    dimensional relations. Raises: adds `DateRefOutOfRange`."""


def _build_windowed_report(specs, written, *, include_keys) -> WindowedExport:
    """Delta: the `TableReport(...)` construction adds
    `calendar=spec.calendar, references=spec.references`."""
```

`incremental/fingerprint.py`: no code change — `date_dimension` is a
data-affecting `ExportConfig` field and rides `config.model_dump(mode="json")`
already; a test pins the mismatch.

### `src/fabulexa_forge/playback/shaped.py`

The head today binds `supplement_specs` and treats them as horizon-free
"static" tables (`_supplement_table_decls`, `_split_selection`,
`_select_supplements`, `_materialize_supplement_tables`). `dim_date` is one more
horizon-free spec in the same slot, ordered before the supplements. The four
helpers generalize from "supplement specs" to "static specs" (calendar +
supplements) keeping their shapes; a `_static_*` rename is the implementer's
call; docstrings say "the calendar and the supplements".

```python
class ShapedPlayback:
    def __init__(
        self,
        emit: Emit,
        config: ExportConfig,
        anchor: EffectiveAnchor | None,
        notice_sink: NoticeSink,
        table_decls: tuple[ShapedTableDecl, ...],
        election: Election,
        calendar_spec: QuerySpec | None,
        supplement_specs: tuple[QuerySpec, ...],
    ) -> None:
        """Delta: `calendar_spec` — the open-compiled `dim_date` spec, or
        None when the shape declares no `date_dimension` (always None for a
        source shape). The head's static tail is `(calendar_spec,) +
        supplement_specs` (calendar first); `tables()` = declared decls +
        `ShapedTableDecl(DATE_DIMENSION_TABLE_NAME, "snapshot")` when present
        + one `snapshot` decl per supplement."""

    def tables(self) -> tuple[ShapedTableDecl, ...]:
        """Delta: order is declared tables, then `dim_date` (when declared,
        `window_delivery='snapshot'`), then supplements."""

    def window(self, start_sim_time, end_sim_time, tables=None) -> tuple[ShapedTable, ...]:
        """Delta: (a) the selection splits over the static-name set
        (`dim_date` + supplements) — a `dim_date`-only or supplement-only
        selection opens no horizon and emits no notice; (b) after the
        dimensional compile (WindowKeyDuplicate included) and before any
        `query_arrow`, `check_date_refs_in_range(self._emit, specs,
        self._config.date_dimension)` when the block is present — a
        violation raises from the ask, atomically; (c) static tables are
        materialized after the dimensional ones: `dim_date` (identical for
        every window) then the selected supplements, declaration order.
        Raises: adds `DateRefOutOfRange`."""

    def state(self, at_sim_time, tables=None) -> tuple[ShapedTable, ...]:
        """Delta: identical to `window()`'s three deltas over the state
        compile (the specs' SQL is already base-relations-rewritten, so the
        guard's aggregate reads the truncated relations through
        `self._emit`); `dim_date` is identical at every T."""


def open_shaped_playback(emit, config, anchor, notice_sink, supplements) -> ShapedPlayback:
    """Delta: after the mode's open validation and before the supplement
    gates, `calendar_spec = compile_date_dimension_spec(config.date_dimension,
    "create")` when the block is present (a pure function of the block — no
    emit read), bound on the head. No new open-time refusal."""
```

### `src/fabulexa_forge/exporters/companion/manifest.py`

```python
_MANIFEST_FORMAT_VERSION = 5   # was 4


def _calendar_json(table: TableReport) -> dict[str, str] | None:
    """The `tables[].calendar` value.

    Args:
        table: The table's report.

    Returns:
        None for every table but the generated calendar; `{"from": "<ISO
        date>", "to": "<ISO date>"}` (`date.isoformat()`) for `dim_date`.
    """


def _column_json(doc, table, name, type_text) -> dict[str, object]:
    """Delta: adds `"references": table.references.get(name)` — the
    referenced output table's name for a `date_ref` (`"dim_date"`) or `fk`
    column, None elsewhere. Key always present."""


def _table_json(doc, table) -> dict[str, object]:
    """Delta: adds `"calendar": _calendar_json(table)`. Key always present.
    `supplement`, `primary_key`, `unique` are None on `dim_date` by
    construction."""
```

Module docstring: "format version 5 adds `tables[].calendar` and
`columns[].references`". `build_manifest_document` / `render_manifest_bytes`:
unchanged — no new top-level key.

### `src/fabulexa_forge/exporters/companion/dictionary.py`

```python
_DATE_DIMENSION_TABLE_DESCRIPTION: str
"""Pinned: a forge-generated calendar over the declared range, one row per
day, keyed by `yyyymmdd`."""

_DATE_DIMENSION_COLUMN_DESCRIPTIONS: dict[str, str]
"""Pinned per column — the Value column of the design doc's calendar table;
key set equals the names in `DATE_DIMENSION_COLUMNS` (a test pins the
equality)."""

_DATE_REF_DESCRIPTION_TEMPLATE: str
"""'Calendar key (`yyyymmdd`) into `dim_date`, derived from `{source}`' —
`{source}` the report's provenance `source_column` for the column."""


def resolve_table_description(doc, table) -> str | None:
    """Delta: after the author override and the event-log marker, a report
    with `table.calendar is not None` resolves to the pinned calendar table
    description. Order: author -> event_log -> calendar -> single-source
    forward -> None."""


def resolve_column_doc(doc, table, column_name, output_type) -> ColumnDoc | None:
    """Delta, two rules, in this order after the event-log branch:
    1. `table.calendar is not None` and `column_name` in the pinned set ->
       `ColumnDoc(description=pinned, unit=None, origin="forge")`.
    2. `table.references.get(column_name) == DATE_DIMENSION_TABLE_NAME`:
       with an `author_descriptions` entry -> `ColumnDoc(override,
       unit=None, origin="author")` — unit None by this rule (a key carries
       no unit), not by the ns-stop; without one -> the pinned `date_ref`
       prose with `{source}` = `table.provenance[column_name].source_column`,
       unit None, origin "forge". Never inherits the source column's own doc.
    Every other column: exactly today's resolution."""
```

`resolve_column_enum_options`, `resolve_kind_value_glosses`, `readme.py`,
`overlay.py`: unchanged.

## Phases

### Phase 1: Renderer authorities and the config grammar

**Delivers:** `date_key_expr`, `date_parse_expr`, `anchor_temporal_expr` (the
aliased renderers become wrappers, SQL byte-identical); `DateDimensionConfig`,
`DateRefSpec`, `ColumnDecl.date_ref`, `ExportConfig.date_dimension` and its
three validators. Nothing compiles a `date_ref` yet — a loaded config carrying
one is inert until Phase 2.
**Demo:** Loads a YAML config with both `date_ref` shapes and prints the
parsed models; drives each parse-time refusal (unquoted YAML date, `to <
from`, both shapes, time-only format, `date_ref` without the block,
`dim_date`-named table, block under `mode: source`) printing each message;
evaluates `date_key_expr` over three DATE literals in DuckDB and prints the
keys; shows `render_anchor_temporal_expr` / `render_date_parse_expr` output
equal to the bare authority + alias, byte-for-byte.
**Contracts:** `_sql.date_key_expr`, `_sql.date_parse_expr`,
`_sql.render_date_parse_expr` (delta), `anchor.anchor_temporal_expr`,
`anchor.render_anchor_temporal_expr` (delta), `DateDimensionConfig`,
`DateRefSpec`, `ColumnDecl` (delta), `ExportConfig` (delta).
**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/_sql.py` |
| Modify | `src/fabulexa_forge/anchor.py` |
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `tests/test_sql.py` |
| Modify | `tests/test_anchor.py` |
| Create | `tests/config/test_date_dimension.py` |
| Create | `docs/sprints/date-dimension/demos/phase_1_config_and_authorities.py` |

**Tests:**
- `date_key_expr("DATE '2024-01-02'")` evaluates to `20240102`; over
  `DATE '0999-12-31'` to `9991231`; over `NULL::DATE` to `NULL`; the returned
  text carries no alias.
- `date_parse_expr(...) + ' AS "x"'` equals `render_date_parse_expr(...)`
  byte-for-byte for a `DATE`, a naive `TIMESTAMP`, and a `TIME` format; every
  existing `render_date_parse_expr` test passes unchanged.
- `anchor_temporal_expr(...) + ' AS "x"'` equals `render_anchor_temporal_expr`
  for each of the four renderings (`timestamp` / `date` / `time` /
  `timestamptz`) under a UTC and a DST-observing anchor; the `anchor is None`
  pass-through is unchanged; every existing `render_anchor_temporal_expr` test
  passes unchanged.
- `DateDimensionConfig`: quoted ISO strings parse to `date`; `from == to`
  accepted; an unquoted YAML date (loader yields `datetime.date`) refused with
  a message naming the string rule; an integer refused; `to < from` refused.
- `DateRefSpec`: `{source}` accepted; `{from, format: "%Y-%m-%d"}` accepted;
  `{from, format: "%d/%m/%Y %H:%M"}` (naive `TIMESTAMP` denotation) accepted;
  neither refused; both refused; `format` without `from` refused; `from`
  without `format` refused; empty `source` refused; time-only `"%H:%M"`
  refused as not date-complete; an unknown directive refused by the
  declared-parse rules.
- `ColumnDecl`: `date_ref` alone accepted; `date_ref` + `from` refused with the
  seven-mode message; `date_ref` + `derived` refused.
- `ExportConfig`: block under `mode: source` refused; block under `mode: base`
  refused; `date_ref` in a dimensional table without the block refused, message
  naming the table and column; with the block, a declared table named
  `dim_date` refused; a supplement named `dim_date` refused; without the block,
  a table named `dim_date` accepted; block + no `date_ref` anywhere accepted.
- `ExportConfig.model_dump(mode="json")` carries `date_dimension` with `from_`
  / `to` as ISO strings.
- `tests/config/test_docstring_convention.py` passes over the two new models
  unchanged.
- Existing: `tests/config/test_models.py`, `tests/test_sql.py`,
  `tests/test_anchor.py`.

### Phase 2: The `date_ref` column mode

**Delivers:** `date_ref` compiled on every grain (records / interval / SCD-2
tracked and untracked) through `render_date_ref_expr`; the six business rules
extended; the per-column variance reading extended so the delivery classifier
and `KeyColumnsStable` see a `date_ref`'s source; `QuerySpec.references` stamped
for `date_ref` (→ `dim_date`) and `fk` (→ the resolved dim). The full export
writes the keys; `dim_date` itself and the range guard land in Phase 3.
**Demo:** Builds an emit with patients (`prop__dob` strings, some
`deactivated_at`) and a status history; compiles a fact with `date_ref
{source: sim_time}` and `date_ref {source: lead_sim_time}` (NULL while open)
and a dim with `date_ref {from: prop__dob, format}` plus a sibling `derived:
timestamp {as: date}`; prints rows side by side showing family agreement and
the NULL keys; prints `QuerySpec.references` per table; drives the refusals
(no anchor + instant shape, non-VARCHAR parse source, slice_only source,
`date_ref` over `deactivated_at` in a key) printing each message; prints the
delivery class of a fact keyed on `created_sim_time` vs one carrying a
`date_ref` over `deactivated_at`.
**Contracts:** `columns.render_date_ref_expr`, `columns.build_column_expr`
(delta), `columns.resolve_carried_source_column` (delta),
`scd.build_scd2_column_expr_flag` (delta), the six `validation.py` deltas,
`windowing._channel_variance` (delta), `engine._table_references`,
`engine.build_query_specs` (delta), `QuerySpec.references`.
**Steps:** `source → author (1 new file + 4 extended)` — the source step
reshapes five dimensional modules over the column-mode / grain / SCD-2 / variance
surface, and the new test suite enumerates that same surface (shape × grain ×
SCD class × variance); each reads it in a fresh context.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/query_spec.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/columns.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/scd.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/validation.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/windowing.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/engine.py` |
| Create | `tests/exporters/dimensional/test_date_ref.py` |
| Modify | `tests/exporters/dimensional/test_validation.py` |
| Modify | `tests/exporters/dimensional/test_windowing.py` |
| Modify | `tests/exporters/dimensional/test_scd2_source_filter.py` |
| Modify | `tests/exporters/dimensional/test_provenance.py` |
| Create | `docs/sprints/date-dimension/demos/phase_2_date_ref_columns.py` |

**Tests:**
- `render_date_ref_expr` instant shape: the fragment is `date_key_expr` over
  `anchor_temporal_expr(..., render=date)` + `AS "<out>"`; parse shape: over
  `date_parse_expr(...)` cast to `DATE` + `AS "<out>"`; instant shape with
  `anchor=None` asserts.
- Family agreement, records grain: for every row, `date_ref {source:
  created_sim_time}` equals `date_key_expr` evaluated over the sibling
  `derived: timestamp {source: created_sim_time, as: date}` — under a UTC
  anchor and under `America/New_York` with an instant that crosses local
  midnight (the local date differs from the UTC date).
- Family agreement, parse shape: `date_ref {from: prop__dob, format}` equals
  `date_key_expr` over `derived: date_parse {from: prop__dob, format}`;
  `1990-05-14` → `19900514`.
- `NULL` sources: `date_ref {source: deactivated_at}` on an active record →
  `NULL`; `date_ref {source: lead_sim_time}` on an open `history_interval` →
  `NULL`; a `NULL` `prop__` string under the parse shape → `NULL`.
- Parse shape, a non-matching cell → the export fails loudly, the message
  naming the output table (the declared-parse contract).
- `TemporalRenderRequiresAnchor`: instant shape with `anchor=None` refused,
  naming the column; parse shape with `anchor=None` compiles.
- `TimestampSourceAvailable`: instant shape over `lead_sim_time` on the
  `records` grain refused with the existing message; over a `prop__` BIGINT on
  the projectable surface accepted.
- `ProjectionColumnExists`: parse shape `from` naming an unknown column refused
  (existing message); `DateParseSourceColumn`: parse shape `from` naming a
  BIGINT column refused (existing message).
- `SliceOnlyColumnRefused`: instant shape over a `slice_only` column refused;
  parse shape over a `slice_only` VARCHAR refused.
- SCD-2: `date_ref` parse shape over a *tracked* VARCHAR property evaluates per
  version (two versions, two keys); instant shape over `created_sim_time`
  evaluates per record (same key on every version); the
  `Scd2ColumnModeSupported` message lists `date_ref`; the admitted-mode
  parametrize in `test_scd2_source_filter.py` gains a `date_ref` case.
- Provenance: a `date_ref` column's `ColumnProvenance` is
  `(source_table=<grain's resolved source>, source_column=<source | from>)`
  for both shapes; `QuerySpec.references` is `{date_ref_col: "dim_date",
  fk_col: "<resolved dim name>"}` in declaration order; a table with neither
  has `{}`; a non-dimensional spec (source, base) has `{}`.
- Windowing: a fact with `date_ref {source: created_sim_time}` classifies
  `append`; the same fact with a `date_ref {source: deactivated_at}` classifies
  `upsert`; a `date_ref` over a tracked property on a dim classifies `upsert`;
  `date_ref {source: deactivated_at}` in `key` refused by `KeyColumnsStable`
  naming `deactivated_at`; `date_ref {source: created_sim_time}` in `key`
  accepted; the windowed specs carry `references` through
  `dataclasses.replace`.
- `ordinal.partition_by` naming a `date_ref` column compiles; `order_by`
  naming one orders by its value, `record_id` tie-broken.
- `build_query_specs` SQL is byte-identical across two compiles of a
  `date_ref`-bearing config.
- Existing: every file under `tests/exporters/dimensional/`.

### Phase 3: The generated calendar, the range guard, and the full export

**Delivers:** `exporters/date_dimension.py` (`dim_date` compile + range
guard), `DateRefOutOfRange`, `QuerySpec.calendar`, `export_dimensional` wired
— `dim_date` written after the declared tables and before the supplements, the
guard as the last pre-write gate, `dim_date` in the overlay-slot and
source-is-output name lists — and the `date-dimension` recipe.
**Demo:** Exports the recipe's config against a self-contained emit to DuckDB
and CSV; prints `dim_date`'s first and last rows and its row count, a
`fact JOIN dim_date` grouped by `day_name`, the output-table order from the
report, and the CSV file list showing `dim_date.csv`; then re-runs with
`date_dimension: {from: "2024-01-01", to: "2024-01-31"}` against a 1985 birth
date and prints the `DateRefOutOfRange` message plus proof the output
directory holds no table file.
**Contracts:** `DATE_DIMENSION_TABLE_NAME`, `DATE_DIMENSION_COLUMNS`,
`CalendarSource`, `compile_date_dimension_spec`, `check_date_refs_in_range`,
`DateRefOutOfRange`, `QuerySpec.calendar`, `engine.export_dimensional` (delta).
**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Create | `src/fabulexa_forge/exporters/date_dimension.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `src/fabulexa_forge/exporters/query_spec.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/engine.py` |
| Create | `tests/exporters/test_date_dimension.py` |
| Create | `tests/exporters/dimensional/test_export_date_dimension.py` |
| Create | `examples/recipes/date-dimension/config.yaml` |
| Create | `examples/recipes/date-dimension/expect.yaml` |
| Create | `docs/sprints/date-dimension/demos/phase_3_calendar_and_guard.py` |

**Tests:**
- `compile_date_dimension_spec`: materialized relation has exactly the 13
  `DATE_DIMENSION_COLUMNS` names in order with the pinned DuckDB types
  (`INTEGER`, not `BIGINT`); `2024-01-01..2024-12-31` yields 366 rows;
  `from == to` yields one row; rows ordered by `date_key` ascending;
  `write_mode` passes through (`create` / `replace`); `keys is None`,
  `provenance == {}`, `kind_values == {}`, `author_descriptions == {}`,
  `author_table_description is None`, `event_log is False`, `supplement is
  None`, `calendar == CalendarSource(from, to)`, `references == {}`.
- Calendar values: `2024-01-01` → `date_key 20240101, year 2024, quarter 1,
  month 1, day 1, day_of_week 1, day_of_year 1, iso_year 2024, iso_week 1,
  month_name "January", day_name "Monday", is_weekend False`; `2021-01-03`
  (a Sunday) → `day_of_week 7, iso_year 2020, iso_week 53, is_weekend True`;
  `2024-12-30` → `iso_year 2025, iso_week 1`; `2024-02-29` → `day_of_year 60`.
- Determinism: the SQL text is byte-identical for the same `(from, to)`; the
  materialized rows are identical across two emits with different anchors.
- `check_date_refs_in_range`: all keys in range → returns; a key below `from`
  → `DateRefOutOfRange` reporting the column's minimum; a key above `to` →
  reports the maximum; both below and above → reports the minimum; an
  all-`NULL` column never violates; an empty relation never violates; a spec
  with `references == {}` (or `fk`-only) is not probed; two violating columns
  → the first in output order is named; the message equals
  `"table 'fact_x' column 'event_date_key': date key 19850101 lies outside
  date_dimension 2020-01-01..2026-12-31"`; the bound keys are computed in SQL
  (`from = 2024-02-29` → lower bound `20240229`).
- `export_dimensional`: the report lists declared tables, then `dim_date`,
  then supplements; under `fmt: csv` a `dim_date.csv` is written; under
  `duckdb` a `dim_date` table exists; every non-`NULL` `date_ref` key in the
  fact joins exactly one `dim_date` row; the block with no `date_ref` anywhere
  still writes `dim_date`; a `readme_overlay` with `table: dim_date` is a valid
  slot; a supplement whose `file` resolves to the output dir's `dim_date.csv`
  is refused by the source-is-output gate; a violating key refuses before any
  write — the output directory holds no data file and no DuckDB file
  afterwards; the guard runs after the supplement cell probe (a bad supplement
  cell is reported, not the range).
- Recipe `date-dimension`: `dim_date` over `1985-01-01..2024-12-31`
  (`row_count: 14610`, `contains_rows` for the first and last day), a
  `fact_status_event` with `event_date_key` `date_ref {source: sim_time}`
  (`20240102`, `20240103`, `20240104`) beside `event_ts`, and a `dim_patient`
  with `birth_date_key` `date_ref {from: prop__dob, format: "%Y-%m-%d"}`
  (`19900514`, `19851102`) beside `birth_date`; guarded by
  `tests/recipes/test_recipes.py` (load, run-and-assert, folder shape).
- Existing: `tests/exporters/dimensional/test_export_dimensional.py`,
  `tests/exporters/dimensional/test_export_supplements.py`,
  `tests/recipes/`.

### Phase 4: Incremental delivery

**Delivers:** `export_window` compiles `dim_date` as `snapshot` (`replace`) in
every emitting window, widens the output-name list, and runs the range guard as
the window's last pre-write gate.
**Demo:** Drips a `date_ref`-bearing config over three calendar windows
(`--next` ×3) to CSV; prints each window's delivered tables and delivery
classes (showing `dim_date snapshot` every time and the fact `upsert` because
of a `date_ref` over `deactivated_at`), diffs `dim_date.csv` across windows
(identical); then narrows the range in the config and prints the fingerprint
mismatch; then drips a config whose third window carries an out-of-range key
and prints the `DateRefOutOfRange` message with the cursor still at window 2.
**Contracts:** `driver.export_window` (delta).
**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Create | `tests/incremental/test_date_dimension.py` |
| Create | `docs/sprints/date-dimension/demos/phase_4_incremental.py` |

**Tests:**
- `dim_date` is delivered with `window_delivery == "snapshot"` and
  `write_mode == "replace"` in the first window, a later window, an empty
  window, and an explicit `--from/--to` range; its bytes are identical across
  windows.
- `dim_date` appears after the declared tables and before the supplements in
  the window's report.
- A fact with `date_ref {source: deactivated_at}` is delivered `upsert`; the
  same fact without it (keyed on `created_sim_time`) is `append`.
- A window whose delta carries an out-of-range key raises `DateRefOutOfRange`
  before that window's write: the earlier windows' files are intact, the cursor
  has not advanced, and no partial file for the window exists.
- The guard runs after the source-is-output gate and the overlay check (a
  `dim_date.csv`-sourced supplement is refused as source-is-output even when a
  key is out of range).
- Changing `date_dimension.to` between windows is a fingerprint mismatch;
  re-running with the original range is not.
- Existing: `tests/incremental/test_supplements.py`,
  `tests/incremental/test_horizon_acceptance.py`,
  `tests/incremental/test_driver.py`.

### Phase 5: Shaped playback

**Delivers:** `open_shaped_playback` compiles `dim_date` at open; `tables()`
lists it after the declared tables and before the supplements; `window()` /
`state()` materialize it as a static table, run the range guard per ask, and
compile `date_ref` columns as the export path does.
**Demo:** Opens a shaped playback over a `date_ref`-bearing config with one
supplement; prints `tables()`; asks `window()` twice and `state()` at two `T`s
printing the fact's date keys and proving `dim_date` is identical at each; asks
for `["dim_date"]` alone and prints that no horizon opened (the notice sink is
silent); then opens a config with a narrow range and prints the
`DateRefOutOfRange` raised from `state()`.
**Contracts:** `ShapedPlayback.__init__` / `tables` / `window` / `state`
(deltas), `open_shaped_playback` (delta).
**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/playback/shaped.py` |
| Create | `tests/playback/test_shaped_date_dimension.py` |
| Create | `docs/sprints/date-dimension/demos/phase_5_shaped_playback.py` |

**Tests:**
- `tables()` order: declared tables, `dim_date` (`window_delivery ==
  "snapshot"`), supplements; without the block, `dim_date` is absent and the
  order is unchanged.
- `window()` with `tables=["dim_date"]` and `state()` with `tables=["dim_date"]`
  return the calendar without opening a horizon (the pattern
  `tests/playback/test_shaped_supplements.py` uses for a supplement-only ask)
  and emit no notice.
- `dim_date` rows are identical across two `window()` asks with different
  bounds and two `state()` asks at different `T`.
- A `date_ref` column's values from `state(T)` equal the full export's values
  at the same horizon; from `window()` they equal the incremental driver's
  delta for the same bounds.
- A narrow range with an out-of-range key: `window()` raises
  `DateRefOutOfRange`; `state()` raises it; neither returns a partial tuple.
- A `tables` selection naming `dim_date` when the block is absent is refused by
  the existing unknown-table gate.
- A source-shape head has no `dim_date` and its `tables()` is unchanged.
- Existing: `tests/playback/test_shaped_open.py`,
  `tests/playback/test_shaped_selection.py`,
  `tests/playback/test_shaped_supplements.py`,
  `tests/playback/test_shaped_state.py`, `tests/playback/test_shaped_window.py`.

### Phase 6: Companion artifacts

**Delivers:** `TableReport.calendar` / `.references` (no default) forwarded by
the three report-assembly sites; manifest format version 5 with
`tables[].calendar` and `columns[].references`; the forge-pinned `dim_date`
and `date_ref` dictionary prose; every existing `TableReport` fixture migrated.
**Demo:** Exports the recipe config with a `fk` column added, to DuckDB with
companions; prints the manifest's `tables[]` names with their `calendar` values
and every column with a non-null `references`; prints the README's `dim_date`
section and the `event_date_key` / `birth_date_key` lines (one pinned, one
author-described, both without a unit); prints the manifest version.
**Contracts:** `TableReport` (delta), `write_query_specs` (delta),
`driver._build_windowed_report` (delta), `manifest._calendar_json`,
`manifest._column_json` / `_table_json` (deltas), `_MANIFEST_FORMAT_VERSION`,
the three `dictionary.py` constants, `dictionary.resolve_table_description` /
`resolve_column_doc` (deltas).
**Steps:** `source → migrate (fan-out, 6 files) → author (2 files)` — the
`TableReport` change is atomic (the suite is red until every construction
site states the two fields), so the migration lands in the same phase; the
new companion tests are authored in a fresh context over the migrated fixtures.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/query_spec.py` |
| Modify | `src/fabulexa_forge/incremental/driver.py` |
| Modify | `src/fabulexa_forge/exporters/companion/manifest.py` |
| Modify | `src/fabulexa_forge/exporters/companion/dictionary.py` |
| Modify | `tests/exporters/companion/_fixtures.py` |
| Modify | `tests/exporters/companion/test_manifest.py` |
| Modify | `tests/exporters/companion/test_readme.py` |
| Modify | `tests/exporters/companion/test_artifacts.py` |
| Modify | `tests/exporters/companion/test_paths.py` |
| Modify | `tests/exporters/dimensional/test_export_supplements.py` |
| Create | `tests/exporters/companion/test_date_dimension.py` |
| Modify | `tests/exporters/test_query_spec.py` |
| Create | `docs/sprints/date-dimension/demos/phase_6_companions.py` |

**Tests:**
- Migration: every `TableReport(...)` construction in the five companion test
  files states `calendar=None, references={}` (or the fixture's own values);
  the `manifest_format_version == 4` pins in `test_manifest.py` and
  `test_export_supplements.py` become `== 5`; the column-object literal in
  `test_manifest.py` gains `"references": None`. All migrated files green.
- `write_query_specs` forwards `calendar` and `references` verbatim on both
  the DuckDB and CSV arms; `_build_windowed_report` forwards them.
- Manifest: `manifest_format_version == 5`; `tables[].calendar ==
  {"from": "1985-01-01", "to": "2024-12-31"}` on `dim_date` and `null` on
  every other table (key present on all); `columns[].references == "dim_date"`
  on a `date_ref` column, the resolved dim's name on an `fk` column, `null`
  elsewhere (key present on all); `dim_date`'s `supplement`, `primary_key`,
  `unique` are `null`; the top-level key order is unchanged; bytes are
  deterministic across two renders.
- Dictionary: `dim_date`'s table description is the pinned prose; each of its
  13 columns resolves to the pinned per-column prose with `origin ==
  "forge"`, `unit is None`, no enum options; the pinned column-description key
  set equals the names in `DATE_DIMENSION_COLUMNS`.
- `date_ref` without an author description → `"Calendar key (\`yyyymmdd\`)
  into \`dim_date\`, derived from \`created_sim_time\`"`, `origin == "forge"`,
  `unit is None`; with an author description → the author's prose, `origin ==
  "author"`, `unit is None` even though the source is `created_sim_time` (a
  `ns` column) and the output type is `INTEGER`; the parse shape's prose names
  the `prop__` source and neither the shape nor the format.
- README: a `dim_date` section renders with the pinned description and its 13
  columns; the `date_ref` column lines carry no unit.
- Existing: `tests/exporters/companion/`, `tests/exporters/test_query_spec.py`,
  `tests/incremental/test_companion_artifacts.py`,
  `tests/exporters/test_companion_integration.py`.

## What Doesn't Change

- `exporters/dimensional/fk.py` — `fk` grammar, path-finding, and rendering are
  untouched; the manifest `references` stamp reuses `check_fk_target_is_dim`
  from `engine.py`.
- `exporters/supplements.py` — grammar, loader, `VALUES` relation, the three
  gates, the source-is-output gate's signature, fingerprint entry, pack
  membership. `dim_date` is not a supplement and carries no `SupplementSource`.
- `incremental/fingerprint.py` — `date_dimension` rides `model_dump` already.
- `exporters/companion/readme.py`, `overlay.py` — render from the dictionary's
  resolution and the caller's name list; no edit.
- Source, base, and streaming modes, their plan compilers, and their report
  assembly — every `TableReport` they produce states `calendar=None,
  references={}` through the shared `write_query_specs` site; no per-mode edit.
- The instant election vocabulary, anchor precedence, DST posture, the
  declared-parse directive set, `validate_date_parse_format`,
  `date_parse_denoted_type` — read, not changed.
- `derived: timestamp`, `derived: date_parse`, `derived: scd_window`, every
  other derived kind, `lookup`, `correlation`, `null` — `date_ref` is a mode
  beside them.
- `_ordinal_variance`, `window_delivery_class`, `check_key_columns_stable` —
  change behaviour only through `_channel_variance`; no edit of their own.
- `check_reserved_table_name` — `dim_date` is not a bookkeeping name; the
  parse-time reservation is the only `dim_date` name rule.
- `exporters/dimensional/init.py`, `exporters/init_annotations.py`,
  `keys_init.py` — no `init` proposal for either surface.
- The rendered SQL of `render_anchor_temporal_expr` and
  `render_date_parse_expr` — byte-identical; their existing tests pass
  unchanged.
- `compare/`, `datasets/`, `corrupters/`, `reader/`, `derivations/`, the
  writers, and the CLI — no edit (a pack carries the block inside the config
  it already packs; no new file).

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/_sql.py` | `date_key_expr`, `date_parse_expr`; `render_date_parse_expr` wraps the bare form |
| `src/fabulexa_forge/anchor.py` | `anchor_temporal_expr`; `render_anchor_temporal_expr` wraps the bare form |
| `src/fabulexa_forge/config/models.py` | `DateDimensionConfig`, `DateRefSpec`, `ColumnDecl.date_ref` + mode validator, `ExportConfig.date_dimension` + three validators |
| `src/fabulexa_forge/errors.py` | `DateRefOutOfRange` |
| `src/fabulexa_forge/exporters/date_dimension.py` | **New** — constants, `CalendarSource`, `compile_date_dimension_spec`, `check_date_refs_in_range` |
| `src/fabulexa_forge/exporters/query_spec.py` | `QuerySpec.references` (P2), `QuerySpec.calendar` (P3), `TableReport` two no-default fields + `write_query_specs` forwarding (P6) |
| `src/fabulexa_forge/exporters/dimensional/columns.py` | `render_date_ref_expr`; `build_column_expr` arm; `resolve_carried_source_column` |
| `src/fabulexa_forge/exporters/dimensional/scd.py` | `build_scd2_column_expr_flag` `date_ref` arm |
| `src/fabulexa_forge/exporters/dimensional/validation.py` | Six rules read `date_ref`; `_collect_value_read_sources`; docstrings |
| `src/fabulexa_forge/exporters/dimensional/windowing.py` | `_channel_variance` reads `date_ref` sources |
| `src/fabulexa_forge/exporters/dimensional/engine.py` | `_table_references` + `build_query_specs` stamp (P2); `export_dimensional` calendar compile, name-list widening, last-gate guard, write order (P3) |
| `src/fabulexa_forge/incremental/driver.py` | `export_window` calendar compile + guard (P4); `_build_windowed_report` forwarding (P6) |
| `src/fabulexa_forge/playback/shaped.py` | `calendar_spec` on the head; `tables` / `window` / `state` deltas; `open_shaped_playback` compile; static-spec helpers |
| `src/fabulexa_forge/exporters/companion/manifest.py` | Version 5; `_calendar_json`; `_column_json` / `_table_json` fields |
| `src/fabulexa_forge/exporters/companion/dictionary.py` | Pinned calendar / `date_ref` prose; `resolve_table_description` / `resolve_column_doc` rules |
| `tests/test_sql.py` | Bare-form identity + `date_key_expr` tests |
| `tests/test_anchor.py` | `anchor_temporal_expr` identity tests |
| `tests/config/test_date_dimension.py` | **New** — parse-time rules |
| `tests/exporters/dimensional/test_date_ref.py` | **New** — both shapes × grains × SCD-2 × variance |
| `tests/exporters/dimensional/test_validation.py` | Extended-rule cases |
| `tests/exporters/dimensional/test_windowing.py` | Variance / delivery / key-stability cases |
| `tests/exporters/dimensional/test_scd2_source_filter.py` | `date_ref` admitted-mode case |
| `tests/exporters/dimensional/test_provenance.py` | `references` stamping pins |
| `tests/exporters/test_date_dimension.py` | **New** — relation shape, values, determinism, range guard |
| `tests/exporters/dimensional/test_export_date_dimension.py` | **New** — full-export order, formats, gates, before-any-write |
| `examples/recipes/date-dimension/config.yaml` | **New** — the recipe |
| `examples/recipes/date-dimension/expect.yaml` | **New** — the recipe's expectations |
| `tests/incremental/test_date_dimension.py` | **New** — snapshot per window, delivery class, per-window guard, fingerprint |
| `tests/playback/test_shaped_date_dimension.py` | **New** — `tables()` order, static ask, per-ask guard |
| `tests/exporters/companion/_fixtures.py` | Migrate `TableReport` constructions |
| `tests/exporters/companion/test_manifest.py` | Migrate constructions, version pin, column literal |
| `tests/exporters/companion/test_readme.py` | Migrate constructions |
| `tests/exporters/companion/test_artifacts.py` | Migrate construction |
| `tests/exporters/companion/test_paths.py` | Migrate construction |
| `tests/exporters/dimensional/test_export_supplements.py` | Version pin 4 → 5 |
| `tests/exporters/companion/test_date_dimension.py` | **New** — manifest v5 fields, pinned prose, unit rule |
| `tests/exporters/test_query_spec.py` | Forwarding of the two new fields |
| `docs/sprints/date-dimension/demos/phase_1_config_and_authorities.py` | **New** — demo |
| `docs/sprints/date-dimension/demos/phase_2_date_ref_columns.py` | **New** — demo |
| `docs/sprints/date-dimension/demos/phase_3_calendar_and_guard.py` | **New** — demo |
| `docs/sprints/date-dimension/demos/phase_4_incremental.py` | **New** — demo |
| `docs/sprints/date-dimension/demos/phase_5_shaped_playback.py` | **New** — demo |
| `docs/sprints/date-dimension/demos/phase_6_companions.py` | **New** — demo |
