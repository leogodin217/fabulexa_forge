# Sprint: value-rendering-elections

## Purpose

Implement the value-rendering-elections design
([`docs/architecture/pending/value-rendering-elections.md`](../../architecture/pending/value-rendering-elections.md),
authoritative for all semantics): three new author-elected renderings —
`decimal`, `instant`, `json_precision` — plus the unified property-first
`render:` map on the source and base grammars, attached across all four export
modes. An author adds one `render:` entry per column in YAML and the elected
form renders byte-identically at every surface that shows the value.

## Scope

**Capabilities touched:**
- Export-config grammar: unified `render:` map (source/base), `date_parse:`
  absorption, base `columns` → `render` rename, dimensional `derived` one-of
  gains `decimal`/`json_precision`, streams gain a numeric-only `render:` map
- Shared SQL rendering: two new authorities + a registered scalar function
- Reader: `register_render_functions` at open (registration-only seam)
- Source mode: table/junction attach + event-log reach (agreement gate,
  elected `changes` entries)
- Base mode: table attach
- Dimensional mode: two new derived kinds
- Streaming mode: per-stream numeric attach at the codec seam
- Writers: pinned CSV text form for `DECIMAL(p, s)`
- Compare: the decimal canonical family

**Not included** (design § Boundaries + process): log-only election surface,
nested JSON paths / wildcards, streaming temporal elections, general casts,
corrupter changes, `init` proposals, election-specific fingerprint rules.
New-election recipes and the doc fold ship after sprint archival.

## Breaking Changes

- **`SourceTableDecl.date_parse` and `BaseRenderDecl.date_parse` are removed**;
  the parse is spelled `{date_parse: "<format>"}` inside the unified `render:`
  map. Existing configs using the old field fail at load (`extra="forbid"`).
- **`BaseRenderDecl.columns` is renamed `render`** — a base entry is
  `{table, render}`. Old spelling fails at load.
- **`_require_render_date_parse_disjoint` is deleted** (one map makes the
  collision unrepresentable). Its sibling helpers survive only where still
  referenced (the events block keeps its narrow shorthand map).
- **`RenderKeyIsInstantColumn` is renamed `RenderKeyResolves`** with an amended
  domain (typed forms address payload columns; shorthand still requires an
  instant-carrying structural column).
- **Behavior change:** a `date_parse`-elected property's event-log `changes`
  entries carry the parsed temporal text (pinned forms) instead of the raw
  codec string (design § Event-log and after-image reach).

Affected recipes (`source/source-render-election`, `base/base-render-election`)
migrate in their phases. `derived-date-parse` (dimensional) is unchanged —
dimensional's `derived:` spellings do not move.

## Success Criteria

- [ ] `decimal` / `instant` / `json_precision` elect per column in source and
      base via one property-first `render:` map; `date_parse` and the temporal
      shorthand live in the same map with unchanged semantics
- [ ] Dimensional `derived: {decimal: …}` / `derived: {json_precision: …}`;
      streaming per-stream `render:` (numeric only)
- [ ] The same election renders byte-identical text in every mode that
      attaches it (one authority per election kind)
- [ ] Event-log `changes` entries carry elected forms under the per-kind
      agreement gate (`ElectionKindConflict`); event sets and `id` numbering
      are election-invariant
- [ ] All plan-time gates and export-time guards fire with the design's
      messages; a config with no election renders byte-identical to today
- [ ] CSV pins the `DECIMAL(p, s)` text form; `compare` accepts DECIMAL via
      the new canonical family (scale-normalized)
- [ ] `make test` and pre-commit green; demos run

## Contracts

Semantics: the design doc's § Semantics and § Validation Rules are normative;
signatures below are the design's Interface Contracts, restated for the
implementer. No default parameters; no implementation code.

### Config models (`src/fabulexa_forge/config/models.py`)

```python
class DecimalElection(StrictBaseModel):
    """Numeric precision rendering: DOUBLE source -> DECIMAL(p, s)."""

    decimal: tuple[int, int]
    """(precision, scale); 1 <= precision <= 38, 0 <= scale <= precision."""


class InstantElection(StrictBaseModel):
    """Payload sim-instant declaration: BIGINT ns source, rendered via the
    anchor through the shared instant-election vocabulary."""

    instant: TemporalRender
    """Which instant rendering the declared ns offset receives."""


class JsonPrecisionElection(StrictBaseModel):
    """In-place rounding of named top-level numeric leaves of a JSON payload."""

    json_precision: dict[str, int]
    """Top-level key -> fraction digits (0..12); non-empty."""


class DateParseElection(StrictBaseModel):
    """The declared parse, relocated into the unified render map; format
    semantics unchanged (validated by validate_date_parse_format)."""

    date_parse: str
    """strptime-style format; shared format rules."""


#: A render-map value: bare temporal-election literal (structural instant
#: shorthand) or one typed election object.
RenderElection = (
    TemporalRender
    | DateParseElection
    | InstantElection
    | DecimalElection
    | JsonPrecisionElection
)


class DecimalSpec(StrictBaseModel):
    """Dimensional derived spelling of the decimal election."""

    from_: str = Field(alias="from")
    """The grain-surface source column (DOUBLE payload column)."""
    as_: tuple[int, int] = Field(alias="as")
    """(precision, scale), same bounds as DecimalElection."""


class JsonPrecisionSpec(StrictBaseModel):
    """Dimensional derived spelling of the json_precision election."""

    from_: str = Field(alias="from")
    """The grain-surface source column (VARCHAR JSON payload)."""
    leaves: dict[str, int]
    """Top-level key -> fraction digits (0..12); non-empty."""
```

Model deltas (behavioral, no new signatures):
- `SourceTableDecl.render: dict[str, RenderElection] | None`; `date_parse`
  field removed; `table_shape` drops the disjointness call.
- `BaseRenderDecl`: `columns` renamed `render`, type
  `dict[str, RenderElection] | None`; `date_parse` removed; `entry_well_formed`
  adjusted; a base entry is `{table, render}`.
- `SourceEventsDecl.render` keeps its narrow `dict[str, TemporalRender] | None`
  type — the typed forms are unrepresentable there (one legal key,
  `event_sim_time`). `SourceEventSourceDecl` gains no field.
- `KindStream` / `MembershipStream` gain
  `render: dict[str, DecimalElection | JsonPrecisionElection] | None` keyed by
  bare property / field name.
- `DerivedSpec` gains `decimal: DecimalSpec | None` and
  `json_precision: JsonPrecisionSpec | None` in the existing one-of
  (`exactly_one_derived` extends).
- Parse-time validators per the design: `decimal_bounds` (on both decimal
  spellings), `json_precision_shape` (leaf map non-empty; keys non-empty;
  `0 <= digits <= 12`; on both spellings).

### Functions (`src/fabulexa_forge/_sql.py`)

```python
def render_decimal_expr(
    source_expr: str,
    precision: int,
    scale: int,
    column_label: str,
    table_label: str,
) -> str:
    """
    Compile the decimal election to its SQL expression — the one decimal
    rendering authority every mode composes. Returns a bare expression (no
    alias); callers alias per their own naming.

    Args:
        source_expr: SQL expression producing the DOUBLE source value.
        precision: Declared DECIMAL precision (1..38).
        scale: Declared DECIMAL scale (0..precision).
        column_label: Column name interpolated into the guard's error message.
        table_label: Output table name interpolated likewise.

    Returns:
        A SQL expression yielding DECIMAL(precision, scale); NULL in, NULL
        out; ties away from zero; raises the enriched conversion error in SQL
        on overflow, NaN, or infinity, naming table, column, and the offending
        value.
    """


def render_json_precision_expr(
    source_expr: str,
    leaves: Mapping[str, int],
    column_label: str,
    table_label: str,
) -> str:
    """
    Compile the json_precision election to its SQL expression — a call to the
    registered scalar (forge_json_precision), the one JSON-leaf rendering
    authority every mode composes. Returns a bare expression (no alias). The
    leaf map and the two attribution labels are spliced as constant arguments.

    Args:
        source_expr: SQL expression producing the VARCHAR JSON payload.
        leaves: Top-level key -> fraction digits, non-empty.
        column_label: Column name for guard attribution.
        table_label: Output table name for guard attribution.

    Returns:
        A SQL expression yielding the scalar's result: the payload with
        declared leaves rounded in place, all other bytes preserved; NULL in,
        NULL out.
    """


def forge_json_precision(
    payload: str | None,
    leaves_json: str,
    column_label: str,
    table_label: str,
) -> str | None:
    """
    The json_precision scalar — exact, byte-preserving token replacement over
    one payload. Registered on the emit's connection at open
    (register_render_functions) and invoked only by expressions
    render_json_precision_expr compiles. Python-side because the
    transformation is not expressible in SQL (top-level-key targeting; no
    re-serialization). A pure function of its arguments.

    Args:
        payload: The JSON payload text, or None (SQL NULL).
        leaves_json: The declared leaf map as a compact JSON literal, spliced
            as a constant by the compiler.
        column_label: Column name for guard attribution.
        table_label: Output table name for guard attribution.

    Returns:
        The payload with each present, numeric declared top-level leaf's value
        token replaced in place by its rounded decimal text — exact decimal
        arithmetic on the token, exactly the declared fraction digits, ties
        away from zero, plain decimal notation, unsigned when the rounded
        value is zero — all other bytes preserved; None for None; a leaf
        present as JSON `null` left verbatim.

    Raises:
        ValueError: invalid payload, non-numeric non-null declared leaf, or
            duplicate top-level key — the message naming table, column, key;
            DuckDB surfaces it as the query's failure.
    """


def register_render_functions(conn: duckdb.DuckDBPyConnection) -> None:
    """
    Register the shared rendering scalar functions — today exactly one,
    forge_json_precision — on a connection. Called once by the reader at open:
    connection-scoped session setup, the session-zone pin's species. Pure
    functions only; registration adds nothing to the determinism statement.

    Args:
        conn: The emit's DuckDB connection.
    """
```

The payload `instant` election compiles through the existing
`render_anchor_temporal_expr` (`anchor.py:254`); no new temporal authority.

### Error classes (`src/fabulexa_forge/errors.py`)

Same docstring-only, message-at-raise-site shape as neighbors:
- `RenderKeyResolves(ExportError)` — rename + amend of
  `RenderKeyIsInstantColumn` (all call sites: `source/plan.py` ×3,
  `base/plan.py` ×2, both modes' `test_plan.py`).
- `DecimalSourceIsDouble(ExportError)`, `InstantSourceIsBigint(ExportError)`,
  `JsonPrecisionSourceIsVarchar(ExportError)` — source-type gates, messages
  per the design's Business Rules table.
- `ElectionKindConflict(ExportError)` — the per-kind agreement gate, both
  message shapes (conflicting pair; elected-beside-silent).

`TemporalRenderRequiresAnchor` extends to payload `instant` elections
(base's optional anchor; source's mandatory anchor subsumes).

### Mode attach points (behavioral deltas — no new public signatures)

- `exporters/source/renders.py` — `build_state_render_sql` /
  `build_junction_render_sql`: per-column loop dispatches on the
  `RenderElection` form (shorthand → wallclock renderer, `date_parse` →
  `render_date_parse_expr`, `instant` → `render_anchor_temporal_expr`,
  `decimal` / `json_precision` → the new authorities). Junction typed keys
  address `elem__<f>` columns; member pair columns are outside the domain.
- `exporters/source/plan.py` — resolve the unified map; run
  `RenderKeyResolves` per form-domain plus the three source-type gates;
  elected sources join the `slice_only` refusal exactly as `date_parse`
  sources do today; the `ElectionKindConflict` gate scoped to
  log-rendered properties (phase 4).
- `exporters/source/events.py` — `changes` entries render elected forms at
  the codec seam (cast-back per declared source type), applied to the
  emitted `[old, new]` values; pinned in-JSON temporal text forms (writers'
  CSV forms; naive `TIMESTAMP` pinned as `YYYY-MM-DD HH:MM:SS.ffffff`, µs
  field omitted when zero) by explicit formatting, never an incidental
  VARCHAR cast; diff comparison inputs stay raw.
- `exporters/base/renders.py` / `plan.py` — same dispatch and gates for the
  base per-table `render` declarations.
- `exporters/dimensional/columns.py` — `build_decimal_expr` /
  `build_json_precision_expr` following the `build_date_parse_expr` shape;
  `build_column_expr` dispatch extends. `validation.py` runs the source-type
  gates against the `from` column through grain-projection resolution.
- `exporters/streaming/engine.py` — per-stream `render:` validated against
  the kind's sidecar types (a key must name a declared property /
  membership field of the stream's projection); the authorities compose into
  the stream's SQL upstream of the Python after-image assembly, at the codec
  seam (`decimal` on the codec string cast back to DOUBLE;
  `json_precision` on the payload text directly), so assembly receives
  already-elected text. Value schemas, keys, ordering, `seq`, `ts` untouched.
- `writers/csv.py` — `_format_value` gains a decimal branch: sign +
  exactly `s` fraction digits, `s = 0` bare integer, no exponent.
- `compare/canonical.py` / `engine.py` — `CanonicalFamily` gains `decimal`
  (`DECIMAL` expected ↔ any `DECIMAL` actual); `family_of` maps DECIMAL;
  `encode_value` normalizes scale (trailing fractional zeros strip). No
  other family, tolerance, or verdict change.
- `reader/emit.py` — `open_emit` calls `register_render_functions` on the
  connection it creates. No read surface, sidecar accessor, or query API
  changes.

## Phases

### Phase 1: Rendering authorities, registration, CSV decimal form
**Delivers:** The two new rendering authorities + the registered scalar in
`_sql.py`, registration at reader open, and the pinned CSV decimal text form.
**Demo:** Registers the functions on a scratch DuckDB connection; renders a
DOUBLE column to `DECIMAL(4,3)` text, rounds a JSON leaf in place
byte-preserving, and shows the overflow error naming table/column/value.
**Contracts:** `render_decimal_expr`, `render_json_precision_expr`,
`forge_json_precision`, `register_render_functions`; CSV decimal form.
**Steps:** `source → author` (the new-test suite is enumerative over the
semantic tables and re-reads the same surface).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/_sql.py` |
| Modify | `src/fabulexa_forge/reader/emit.py` |
| Modify | `src/fabulexa_forge/writers/csv.py` |
| Modify | `tests/test_sql.py` |
| Modify | `tests/reader/test_open_emit.py` |
| Modify | `tests/writers/test_csv.py` |
| Create | `docs/sprints/value-rendering-elections/demos/phase_1_authorities.py` |

**Tests:**
- decimal: value rounds to exactly `s` fraction digits; exact binary half
  (e.g. `2.5` → `DECIMAL(2,0)`) rounds away from zero; NULL → typed NULL;
  integer-digit overflow raises in SQL naming table, column, value; NaN and
  ±Infinity raise the same error; `s = 0` output; negative values
- json scalar: declared present numeric leaf replaced in place — whitespace,
  key order, undeclared values byte-identical around it; absent key → payload
  unchanged; leaf = JSON `null` → unchanged; non-numeric leaf → ValueError
  naming table/column/key; duplicate top-level key → error; non-object /
  unparseable payload → error; `None` → `None`; exponent token (`6.5e1`)
  rounds to plain form; `-0.001` @ 2 → `0.00` (no negative zero); `0` digits
  → bare integer; same-named key nested deeper is NOT touched; exact decimal
  half (`0.005` @ 2 → `0.01`) — never float64 re-parse
- `render_json_precision_expr` splices leaves + labels safely (quote-bearing
  labels); expression round-trips through DuckDB with the registered scalar
- registration: `open_emit`'s connection evaluates a `forge_json_precision`
  call; conformance/corrupter reads unaffected (no SQL calls it)
- CSV: `DECIMAL(7,2)` renders `1234.50`; `DECIMAL(4,0)` renders `-17`; no
  exponent for large values
- Existing `test_sql.py` / `test_csv.py` / reader tests still pass unchanged

### Phase 2: Unified render map + source-mode attach
**Delivers:** The breaking grammar restructure (election models,
`RenderElection`, `SourceTableDecl.render` retype, `date_parse` field removal)
and the full source-mode attach: state + junction dispatch, plan-time gates,
slice-only join. `RenderKeyIsInstantColumn` → `RenderKeyResolves` everywhere
(base call sites update mechanically; base grammar itself moves in phase 3).
**Demo:** A source export over a fixture emit electing `decimal`, `instant`,
and `json_precision` (plus a relocated `date_parse`) in one `render:` map;
prints rendered rows and one refused config per gate.
**Contracts:** Election models, `RenderElection`, source model deltas, error
classes, source attach points.
**Steps:** `source → migrate (fan-out, 7 files) → author (2 files)` — atomic:
the field removal leaves every old spelling red until migrated.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/source/renders.py` |
| Modify | `src/fabulexa_forge/exporters/base/plan.py` |
| Modify | `tests/config/test_models.py` |
| Modify | `tests/config/test_source_decls.py` |
| Modify | `tests/exporters/source/test_plan.py` |
| Modify | `tests/exporters/source/test_renders.py` |
| Modify | `tests/exporters/base/test_plan.py` |
| Modify | `examples/recipes/source/source-render-election/config.yaml` |
| Modify | `examples/recipes/source/source-render-election/expect.yaml` |
| Create | `tests/exporters/source/test_value_election_plan.py` |
| Create | `tests/exporters/source/test_value_election_renders.py` |
| Create | `docs/sprints/value-rendering-elections/demos/phase_2_source_elections.py` |

**Tests (authored):**
- model: each election form parses; unknown election key refused; one column
  one election (YAML key uniqueness); `decimal` bounds (`p=0`, `p=39`,
  `s > p` refused); `json_precision` shape (empty map, empty key, digits 13
  refused); old `date_parse:` field refused (`extra="forbid"`)
- plan: typed election on a structural column refused (`RenderKeyResolves`);
  shorthand on a payload column refused; key naming an omitted/non-existent
  column refused; `decimal` on non-DOUBLE refused (`DecimalSourceIsDouble`);
  `instant` on non-BIGINT refused; `json_precision` on non-VARCHAR refused;
  junction: typed key addresses `elem__<f>`, member pair columns refused;
  elected source joins the `slice_only` refusal; events block still accepts
  only `event_sim_time` shorthand
- render: each election renders through its authority (SQL text asserted);
  `instant` renders identically to a structural instant of the same value;
  no-election config renders byte-identical SQL to today; rename composes
  (elected column under `rename` keeps source-name addressing)
- Migrated files: intent preserved — every existing render/date_parse test
  re-spelled to the unified map, still green; recipe re-spelled, recipe suite
  green

### Phase 3: Base-mode attach
**Delivers:** `BaseRenderDecl` restructure (`columns` → `render`, unified
values, `date_parse` removal, disjointness helper deleted) and the full
base-mode attach with the same gates; `instant` under base's optional anchor
requires a resolved anchor (`TemporalRenderRequiresAnchor`).
**Demo:** A base export electing all three new forms plus a relocated
`date_parse`; shows the anchor-required refusal for `instant` with no anchor.
**Contracts:** Base model delta, base attach points.
**Steps:** `source → migrate (fan-out, 5 files) → author (2 files)` — atomic
(the rename breaks every old base spelling).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/exporters/base/plan.py` |
| Modify | `src/fabulexa_forge/exporters/base/renders.py` |
| Modify | `tests/config/test_base_config.py` |
| Modify | `tests/exporters/base/test_plan.py` |
| Modify | `tests/exporters/base/test_renders.py` |
| Modify | `tests/exporters/base/_base_fixtures.py` |
| Modify | `examples/recipes/base/base-render-election/config.yaml` |
| Create | `tests/exporters/base/test_value_election_plan.py` |
| Create | `tests/exporters/base/test_value_election_renders.py` |
| Create | `docs/sprints/value-rendering-elections/demos/phase_3_base_elections.py` |

**Tests (authored):**
- model: `{table, render}` parses; old `columns` / `date_parse` spellings
  refused; disjointness helper gone (no import site remains)
- plan: the four source-type/domain gates on base tables; `instant` with no
  effective anchor refused; elected source joins `slice_only` refusal
- render: each election renders via the shared authority — byte-identical
  text to the source-mode render of the same value; reference-value columns
  and cast-back branch unaffected
- Migrated files re-spelled, green; base recipe green

### Phase 4: Event-log reach
**Delivers:** The per-kind agreement gate (`ElectionKindConflict`) scoped to
log-rendered properties, and elected `changes` entries for every election
kind — including the pinned in-JSON temporal text forms and log-site
export-time guards. Changeset membership and `id` numbering stay raw.
**Demo:** A source export with an audited elected property: `changes` entries
show the elected text; adding a second conflicting declared table shows the
plan-time refusal naming both tables; narrowing via `ignore` legalizes it.
**Contracts:** `ElectionKindConflict`; events attach point.
**Steps:** `source → author (2 files)` — the migrated assertions are
intent-changing (raw-codec `changes` text becomes elected text), per-file
judgment against the spec.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/source/events.py` |
| Modify | `tests/exporters/source/test_events_render.py` |
| Create | `tests/exporters/source/test_value_election_events.py` |
| Create | `docs/sprints/value-rendering-elections/demos/phase_4_event_log_reach.py` |

**Tests:**
- agreement gate: two tables of one kind, identical election → elected
  `changes`; differing elections + log renders the property → refusal naming
  both tables; elected-beside-silent → refusal (silent-table message shape);
  differing elections, no log renders the property → legal; property narrowed
  out via `only`/`ignore` → legal; kind audited with no declared table → raw
  codec text
- rendering: `create` after-image and `u` `[old, new]` pairs carry elected
  text for `decimal`, `json_precision`, `instant`, `date_parse`; table column
  and `changes` entry byte-identical for the same value; naive `TIMESTAMP`
  form `YYYY-MM-DD HH:MM:SS.ffffff` with µs field omitted when zero; `date` /
  `time` / `timestamptz` forms match the writers' pinned CSV forms
- invariance: two raw values rounding to one decimal text still emit the `u`
  row (equal-looking pair); event set and dense `id` numbering identical with
  and without elections; junction `elem__<f>` election reaches the log's bare
  field `<f>`
- guards: decimal overflow and JSON payload errors fire at the log site on a
  value no declared table selects

### Phase 5: Dimensional derived members
**Delivers:** `derived: {decimal: …}` and `derived: {json_precision: …}` —
specs, one-of extension, expression builders, and validation gates through
grain-projection resolution. Purely additive.
**Demo:** A dimensional export deriving a `DECIMAL(4,3)` column and a
leaf-rounded JSON column on a fact; shows a non-DOUBLE `from` refused.
**Contracts:** `DecimalSpec`, `JsonPrecisionSpec`, `DerivedSpec` delta,
dimensional attach points.
**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/columns.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/validation.py` |
| Modify | `tests/config/test_models.py` |
| Modify | `tests/exporters/dimensional/test_columns.py` |
| Modify | `tests/exporters/dimensional/test_validation.py` |
| Create | `docs/sprints/value-rendering-elections/demos/phase_5_dimensional_derived.py` |

**Tests:**
- model: both specs parse; bounds validators shared with the render-map
  spellings; `exactly_one_derived` still refuses pairs
- validation: `decimal` `from` must resolve to a DOUBLE grain-surface column;
  `json_precision` `from` must resolve to VARCHAR — the mode's own message
  addressing
- expr: both compile through the shared authorities; rendered text
  byte-identical to a source-mode render of the same value; existing derived
  kinds (`timestamp`, `date_parse`, `ordinal`, `value_map`, `elapsed`,
  `scd_window`) unchanged

### Phase 6: Streaming attach
**Delivers:** Per-stream `render:` maps (`decimal` / `json_precision` only)
on `KindStream` / `MembershipStream`, validated against the stream's
projection and sidecar types, applied through the shared authorities upstream
of after-image assembly.
**Demo:** A stream over a fixture emit with both numeric elections; prints
`c`/`u` events showing elected after-image text, an unchanged `d` tombstone,
and an unchanged Debezium value schema.
**Contracts:** Stream model deltas; streaming attach point.
**Steps:** `source → author (2 files)` — the engine is a deep read and the
new suite re-reads the same surface.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/exporters/streaming/engine.py` |
| Modify | `tests/config/test_stream_config.py` |
| Create | `tests/exporters/streaming/test_value_election_stream.py` |
| Create | `docs/sprints/value-rendering-elections/demos/phase_6_streaming_render.py` |

**Tests:**
- model: `render:` parses on both stream species; temporal forms
  unrepresentable (union excludes them); keys are bare names
- validation: a key must name a declared property / membership field of the
  stream's projection; `decimal` on non-DOUBLE / `json_precision` on
  non-VARCHAR refused against sidecar types — streaming's own addressing
- events: elected property's `c` / `u` after-image entries carry the elected
  text — byte-identical to the table modes' render of the same value; `d`
  tombstones unaffected; Debezium value schema string-typed as before;
  message key, merge order, `seq`, `ts` unchanged with elections on
- no-election streams byte-identical to today

### Phase 7: Compare decimal family
**Delivers:** The decimal canonical family: `family_of` maps DECIMAL,
`encode_value` normalizes scale, engine family-coverage accepts it.
**Demo:** `compare` verdicts: a decimal-elected export equals its expected
render; the same values at different declared scales equal; a genuinely
differing value reports as a row difference.
**Contracts:** Compare attach point (no new signatures).
**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/compare/canonical.py` |
| Modify | `src/fabulexa_forge/compare/engine.py` |
| Modify | `tests/compare/test_canonical.py` |
| Modify | `tests/compare/test_engine.py` |
| Create | `docs/sprints/value-rendering-elections/demos/phase_7_compare_decimal.py` |

**Tests:**
- `family_of(DECIMAL(p,s))` → the decimal family for any `(p, s)`; expected
  DECIMAL ↔ actual DECIMAL at different `(p, s)` compares equal when values
  are equal; `1.50` ≡ `1.5` (scale normalization); `1.50` ≠ `1.51`
- engine: family-coverage validation admits DECIMAL columns; the previous
  "unsupported type" refusal for DECIMAL is gone; no other family's encoding
  changes (existing canonical tests untouched and green)

## What Doesn't Change

Normative list: design doc § What Doesn't Change. Sprint-operational
highlights — do NOT modify:

- `reader/` read surfaces, sidecar accessors, query APIs, and
  `reader/conformance.py` (C1–C14, the codec, `to_csv_text`,
  `_ROUND_TRIPPABLE_TYPES`) — the reader's only edit is the one registration
  call in `open_emit`
- The derivations layer (`derivations/`): folds still emit codec VARCHAR
  after-images; elections apply in the modes
- `validate_date_parse_format`, `render_date_parse_expr`,
  `render_anchor_temporal_expr`, `pin_session_timezone` — unchanged
  signatures and semantics; their existing tests pass unmodified
- Dimensional's existing `derived:` spellings (incl. `date_parse` /
  `timestamp`) and the `derived-date-parse` recipe
- The corrupter family, the incremental driver's fingerprint and window
  rules, key election, anchor resolution, row ordering
- Streaming's `ts` contract, value-schema typing, message keys, `seq`
- The audited-property diff comparison inputs (raw values) and the log's
  `id` numbering
- `writers/duckdb.py` (native types need no text form)

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/_sql.py` | Two new rendering authorities + registered scalar + `register_render_functions` |
| `src/fabulexa_forge/reader/emit.py` | `open_emit` registers render functions on the new connection |
| `src/fabulexa_forge/writers/csv.py` | Pinned `DECIMAL(p,s)` CSV text form in `_format_value` |
| `src/fabulexa_forge/config/models.py` | Election models, `RenderElection`, source/base render map restructure, stream render fields, `DerivedSpec` members, validators; disjointness helper deleted |
| `src/fabulexa_forge/errors.py` | `RenderKeyResolves` rename, three source-type gate classes, `ElectionKindConflict` |
| `src/fabulexa_forge/exporters/source/plan.py` | Unified-map resolution, gates, slice-only join, agreement gate |
| `src/fabulexa_forge/exporters/source/renders.py` | State + junction election dispatch through the authorities |
| `src/fabulexa_forge/exporters/source/events.py` | Elected `changes` entries, pinned in-JSON temporal text, log-site guards |
| `src/fabulexa_forge/exporters/base/plan.py` | Rename adoption (P2); unified-map resolution + gates (P3) |
| `src/fabulexa_forge/exporters/base/renders.py` | Election dispatch through the authorities |
| `src/fabulexa_forge/exporters/dimensional/columns.py` | `build_decimal_expr` / `build_json_precision_expr` + dispatch |
| `src/fabulexa_forge/exporters/dimensional/validation.py` | Source-type gates for the two new derived kinds |
| `src/fabulexa_forge/exporters/streaming/engine.py` | Per-stream render validation + authority application at the codec seam |
| `src/fabulexa_forge/compare/canonical.py` | Decimal canonical family + scale-normalized encoding |
| `src/fabulexa_forge/compare/engine.py` | Family coverage admits DECIMAL |
| `tests/test_sql.py` | New authority tests |
| `tests/reader/test_open_emit.py` | Registration test |
| `tests/writers/test_csv.py` | Decimal text form tests |
| `tests/config/test_models.py` | Grammar migration (P2) + derived-spec additions (P5) |
| `tests/config/test_source_decls.py` | Grammar migration |
| `tests/config/test_base_config.py` | Grammar migration |
| `tests/config/test_stream_config.py` | Stream render additions |
| `tests/exporters/source/test_plan.py` | Migration to unified map + rename |
| `tests/exporters/source/test_renders.py` | Migration to unified map |
| `tests/exporters/source/test_value_election_plan.py` | New: source plan gates |
| `tests/exporters/source/test_value_election_renders.py` | New: source render elections |
| `tests/exporters/source/test_events_render.py` | Intent-changing rewrite: elected `changes` text |
| `tests/exporters/source/test_value_election_events.py` | New: event-log reach |
| `tests/exporters/base/test_plan.py` | Rename (P2) + migration (P3) |
| `tests/exporters/base/test_renders.py` | Migration to unified map |
| `tests/exporters/base/_base_fixtures.py` | Migration to unified map |
| `tests/exporters/base/test_value_election_plan.py` | New: base plan gates |
| `tests/exporters/base/test_value_election_renders.py` | New: base render elections |
| `tests/exporters/dimensional/test_columns.py` | Derived decimal/json_precision tests |
| `tests/exporters/dimensional/test_validation.py` | Derived gate tests |
| `tests/exporters/streaming/test_value_election_stream.py` | New: streaming attach |
| `tests/compare/test_canonical.py` | Decimal family tests |
| `tests/compare/test_engine.py` | Family coverage tests |
| `examples/recipes/source/source-render-election/config.yaml` | Re-spelled to unified map |
| `examples/recipes/source/source-render-election/expect.yaml` | Re-spelled alongside |
| `examples/recipes/base/base-render-election/config.yaml` | Re-spelled to unified map |
| `docs/sprints/value-rendering-elections/demos/phase_*.py` | One demo per phase (7) |
