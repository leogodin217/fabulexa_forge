# Sprint: temporal-elections

## Purpose

Implement the Temporal Rendering Elections design
(`docs/architecture/pending/temporal-rendering-elections.md`): author-electable
temporal output types — `date` / `time` / `timestamptz` on every wallclock
instant surface, `interval` on `derived: elapsed`, and a declared VARCHAR→DATE
parse — so an author targeting a realistic app database or warehouse can emit
`DATE` admission dates, `TIME` columns, zone-carrying `TIMESTAMPTZ` instants,
and `INTERVAL` waits from YAML alone.

The design doc owns semantics and rationale; this spec owns contracts, phases,
and test cases. Section references (`§ …`) point into the design doc.

## Scope

**Capabilities touched:**

- Effective anchor: the shared wallclock renderer generalizes to an elected
  temporal type (`render_anchor_temporal_expr`); `timestamp` election
  byte-identical to today
- Reader: invocation-scoped session-zone pin (`pin_session_timezone`)
- Writers: pinned, machine-independent CSV text forms for DATE / TIME /
  TIMESTAMPTZ / INTERVAL
- Config models: the `TemporalRender` vocabulary and its attach points
  (dimensional `as` / `scd_window` object form / `date_parse` derivation;
  source per-table + events `render` / `date_parse` maps; base `render`
  declaration list)
- Dimensional exporter: four derivation surfaces wired; ordinal amendment
  extended (monotone renderings substitute raw-ns; `time` orders by value)
- Incremental: election-aware window-key membership (`time` excluded)
- Source exporter: per-table / event-log attach points, instant-key gating
- Base exporter: per-table render declarations
- Playback (tier-2 shaped): joins the session-zone pin; elections otherwise
  flow through the modes' own compiles

**Not included:** streaming elections (excluded by design — string-typed
payloads), any sidecar/contract change, corrupter changes, `init` proposing
elections, recipes (separate lifecycle step after the feature ships).

## Breaking Changes

Internal only — the author-facing config surface is purely additive; every
existing YAML config parses and renders byte-identically.

- `render_anchor_timestamp_expr` is renamed to `render_anchor_temporal_expr`
  and gains a required `render` parameter. All five caller modules and the
  tests naming it update in Phase 1 (greenfield rename, no shim).
- `ElapsedSpec.unit` becomes `Literal[...] | None = None` (absence detection
  for the new exactly-one-of `unit` / `as` rule — omitting both stays a
  load-time error, so no config that was valid becomes silently defaulted,
  and no config that was invalid becomes valid).
- `DerivedSpec.scd_window` widens from `Literal["valid_from", "valid_to"]` to
  the union with the object form `ScdWindowSpec`; the exactly-one validator
  message now lists six kinds. Bare-literal configs are unaffected.

## Success Criteria

- [ ] A config with no election renders byte-identical output to today
      (existing suite green, unchanged expectations, both formats)
- [ ] `as: date` / `time` / `timestamptz` on dimensional `derived: timestamp`
      and the `scd_window` object form emit those DuckDB types; same for
      source/base `render` maps on structural instant columns
- [ ] `as: interval` on `derived: elapsed` emits `INTERVAL` equal to the
      numeric delta at µs
- [ ] `date_parse` renders `DATE` on all three modes; a non-matching non-NULL
      value fails the export loudly, naming table, column, and value
- [ ] Every explicitly-elected instant rendering without a resolved anchor is
      refused at plan time (`TemporalRenderRequiresAnchor`)
- [ ] CSV serialization of the four new types matches the pinned text forms
      (§ Serialization) regardless of machine locale/zone; DuckDB output
      stores native types
- [ ] `order_by` on a `date`/`timestamptz`-elected amendment column compiles
      to raw-ns; on a `time`-elected column it orders by rendered value; an
      append-mode `order_by` naming a `time`-elected column is refused
- [ ] `make check` green (lint + typecheck + conformance + tests)

## Contracts

Contracts below are normative for the implementer. Model shapes and validator
semantics are specified in full in the design doc (§ Interface Contracts,
§ Validation Rules); signatures are restated here, docstring semantics
abbreviated — the design doc text governs.

### Config models (`src/fabulexa_forge/config/models.py`)

```python
TemporalRender = Literal["timestamp", "date", "time", "timestamptz"]
"""The instant-rendering election vocabulary, shared by every attach point."""


class TimestampSpec(StrictBaseModel):
    source: str
    as_: TemporalRender | None = Field(None, alias="as")
    """Absent (None) = the mode-definitional default `timestamp` rendering
    (absence detection). Any set value — `timestamp` included — is an
    explicit election and makes the column anchor-required."""


class ScdWindowSpec(StrictBaseModel):
    """An SCD-2 validity bound with an instant-rendering election."""

    bound: Literal["valid_from", "valid_to"]
    as_: TemporalRender = Field(alias="as")
    """Required — the object form exists to elect; the bare-literal
    shorthand remains the no-election form."""


class DateParseSpec(StrictBaseModel):
    """A declared reinterpretation of a VARCHAR source column as DATE."""

    from_: str = Field(alias="from")
    format: str
    """Closed strptime directive set, must denote a complete calendar date
    (validator `format_denotes_a_date`). Never defaulted."""


class ElapsedSpec(StrictBaseModel):
    correlate_on: str
    other_where: dict[str, PredicateValue]
    start_source: str
    end_source: str
    unit: Literal["minutes", "seconds", "hours"] | None = None
    as_: Literal["interval"] | None = Field(None, alias="as")
    # validator exactly_one_rendering: exactly one of unit / as_ is set;
    # omitting both is an error, setting both is an error.


class DerivedSpec(StrictBaseModel):
    ordinal: OrdinalSpec | None = None
    value_map: ValueMapSpec | None = None
    timestamp: TimestampSpec | None = None
    scd_window: Literal["valid_from", "valid_to"] | ScdWindowSpec | None = None
    elapsed: ElapsedSpec | None = None
    date_parse: DateParseSpec | None = None
    # exactly-one validator extends to six kinds.


class SourceTableDecl(StrictBaseModel):  # new fields only
    render: dict[str, TemporalRender] | None = None
    """Keys are source identities (e.g. `created_sim_time`), validated at
    plan time against the table category's instant-carrying structural
    columns (business rule RenderKeyIsInstantColumn)."""
    date_parse: dict[str, str] | None = None
    """Payload source column (this mode's addressing convention) -> format."""


class SourceEventsDecl(StrictBaseModel):  # new field only
    render: dict[str, TemporalRender] | None = None
    """The log's one legal key is `event_sim_time` (mode-definitional)."""


class BaseRenderDecl(StrictBaseModel):
    """Per-table temporal elections for the base mode."""

    table: str
    """The sidecar `records__<kind>` table; targets disjoint across entries
    (the existing base entries-disjoint rule extends to this list)."""
    columns: dict[str, TemporalRender] | None = None
    """Keyed on pre-default column identities; `last_mutation_sim_time` is
    outside the key domain (the mode never emits it)."""
    date_parse: dict[str, str] | None = None
    """`prop__<p>` -> parse format."""


class BaseConfig(StrictBaseModel):  # new field only
    render: list[BaseRenderDecl] | None = None
```

Parse-time validators per § Validation Rules: `exactly_one_rendering`
(ElapsedSpec), `format_denotes_a_date` (DateParseSpec — closed set `%Y %y %m
%d %b %B %%` + literal text; at least one year, one month, and `%d`),
`render_maps_valid` (Source/Base decls — non-empty maps, non-empty keys,
formats denote complete dates, a column in at most one of the two maps),
`BaseConfig.at_least_one_field` grows `render`.

### Functions

```python
def render_anchor_temporal_expr(
    anchor: EffectiveAnchor | None,
    qualified_source: str,
    out_name: str,
    render: TemporalRender,
) -> str:
    """Render the SQL SELECT fragment for a wallclock value derived from a
    nanosecond sim_time column through the effective anchor, in the elected
    temporal type.

    Generalizes render_anchor_timestamp_expr; the `timestamp` election
    reproduces its expression byte-identically. `date` and `time` project
    the same local wall clock; `timestamptz` renders the absolute instant.
    Interpolations stay pinned: zone = the anchor's IANA key, origin = the
    anchor instant's ISO form. The one renderer every wallclock mode shares.

    When `anchor` is None, `render` must be the caller-side default
    `timestamp` and the raw source column is aliased through unchanged
    (the existing no-anchor path). Callers enforce the
    elected-rendering-requires-anchor rule at validation; a non-default
    election with a None anchor is a caller bug.

    Args:
        anchor: The resolved EffectiveAnchor, or None for the no-anchor path.
        qualified_source: The fully table-qualified BIGINT-ns source column SQL.
        out_name: The output column name (the AS alias).
        render: The elected temporal rendering.

    Returns:
        A SQL SELECT-list expression fragment ending in `AS "<out_name>"`.
    """
```

```python
def render_date_parse_expr(
    qualified_source: str,
    date_format: str,
    out_name: str,
    table_label: str,
) -> str:
    """Render the SQL SELECT fragment reinterpreting a VARCHAR column as
    DATE under an author-declared format. Lives in the shared SQL utilities
    (`src/fabulexa_forge/_sql.py`) — all three modes render through it.

    NULL source values yield NULL. A non-NULL value not matching the format
    fails the export loudly at query time — never a silent NULL. The
    renderer owns attribution: the fragment embeds an in-SQL guard raising
    an error that names `table_label`, the source column, and the offending
    value. The format string is validated at config load; this renderer
    assumes a valid format.

    Args:
        qualified_source: The fully table-qualified VARCHAR source column SQL.
        date_format: The author-declared strptime-style format.
        out_name: The output column name (the AS alias).
        table_label: The output table name interpolated into the guard's
            error message.

    Returns:
        A SQL SELECT-list expression fragment ending in `AS "<out_name>"`.
    """
```

```python
def pin_session_timezone(emit: Emit, anchor: EffectiveAnchor) -> None:
    """Pin the materialization session's time zone to the anchor zone for
    this invocation. Lives in the reader (`src/fabulexa_forge/reader/emit.py`).

    Called once by the anchor-resolving driver (the export driver in cli.py;
    tier-2 shaped playback's open) after anchor resolution, before any
    relation materializes. Connection-scoped: covers both reader query
    surfaces (row-tuple and columnar). A pure function of the resolved
    anchor — same anchor -> same session state -> byte-identical
    zone-bearing text forms on any machine. Never called by a mode or a
    writer. With no resolved anchor there is no call.

    Args:
        emit: The open emit whose materialization session is pinned.
        anchor: The resolved effective anchor supplying the IANA zone.
    """
```

### CSV writer behavior (`src/fabulexa_forge/writers/csv.py`)

`_format_value` (and only it) grows pinned per-type text forms for the four
new Arrow value types, formatting by rule — never an incidental `str()` of
the in-memory value (§ Serialization):

| Type | CSV text form (pinned) |
|---|---|
| `DATE` | `YYYY-MM-DD` |
| `TIME` | `HH:MM:SS.ffffff` — fixed six-digit µs field |
| `TIMESTAMPTZ` | `YYYY-MM-DD HH:MM:SS.ffffff±HH:MM` — local wall clock in the value-attached (anchor) zone with that instant's UTC offset, fixed six-digit µs |
| `INTERVAL` | signed µs delta as `[-]H:MM:SS.ffffff` — unbounded hours, no calendar components |

The existing `TIMESTAMP` and `DOUBLE` forms keep their current serialization
byte-identically, incidental form and all. The writer stays generic:
type-driven, no mode/schema/anchor knowledge (the zone arrives as
value-attached Arrow metadata via the session-zone pin).

### Elapsed builder

`build_elapsed_expr` keeps its signature; its expression grows the `interval`
branch — a µs-precision `INTERVAL` from the same ns delta, sign-preserving,
equal to the numeric rendering at µs.

### Business rules (plan-time)

Per § Business Rules, with the design doc's error messages verbatim:

- `TemporalRenderRequiresAnchor` — every explicitly-elected instant rendering
  (dimensional `as`, `scd_window` object form, source/base `render` entries)
  requires a resolved effective anchor. Source's global anchor requirement
  subsumes its entries; the rule still names the offending column.
- `DateParseSourceColumn` — each parse source resolves per its surface's rule
  (dimensional `from` resolves off the grain's projectable surface exactly as
  `value_map.from` does), carries a declared VARCHAR type, and is not
  `slice_only` (the source read joins the dimensional
  `_collect_value_read_sources` surface list).
- `RenderKeyIsInstantColumn` — a source declared-table or base-entry `render`
  key names an instant-carrying structural column of the table's category per
  the reader's `structural_instant_columns` — never a hardcoded list. The
  event log's one legal key is `event_sim_time` (mode-definitional). A key
  must name a column the render emits: `last_mutation_sim_time` is outside
  the base key domain; a source entry keying it under a windowed invocation
  is refused by the windowed business-rule pass.
- Incremental append-mode `order_by` (existing rule
  `check_incremental_ordinal_order_by`, amended) — window-key membership
  becomes election-aware: a `time`-elected column over the window's raw-ns
  source is excluded from the window-key set.

## Phases

### Phase 1: Temporal renderer generalization

**Delivers:** `render_anchor_temporal_expr` — the shared wallclock renderer
with the election parameter — with every caller updated to pass the default
`timestamp` election. No config surface yet.

**Demo:** Prints the four elections' SQL fragments for one anchor, executes
them against an in-memory DuckDB, and shows (a) the `timestamp` fragment is
byte-identical to the pre-sprint expression, (b) the family identity: `date`
= the naive timestamp's date part, `time` its time part, `timestamptz` the
same absolute instant.

**Contracts:** `render_anchor_temporal_expr`.

**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/anchor.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/columns.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/scd.py` |
| Modify | `src/fabulexa_forge/exporters/base/renders.py` |
| Modify | `src/fabulexa_forge/exporters/source/renders.py` |
| Modify | `src/fabulexa_forge/exporters/source/events.py` |
| Modify | `tests/test_anchor.py` |
| Modify | `tests/exporters/source/test_renders.py` |
| Create | `docs/sprints/temporal-elections/demos/phase_1_temporal_renderer.py` |

**Tests:**
- `timestamp` election with an anchor: expression string equals the
  pre-sprint `render_anchor_timestamp_expr` output byte-for-byte
- `date` / `time` / `timestamptz` elections: executed against DuckDB, output
  column types are DATE / TIME / TIMESTAMP WITH TIME ZONE
- Family identity on a concrete instant: `date` == naive timestamp's date,
  `time` == its time-of-day, in the anchor zone
- DST fold instant: naive renderings step backward (existing accepted
  behavior); `timestamptz` values stay strictly increasing
- `anchor=None` + `render="timestamp"`: raw source aliased through unchanged
- µs truncation: a ns-precision instant truncates identically across all
  four elections
- Existing `tests/exporters/source/test_renders.py` call sites migrate to the
  new name/signature; all existing rendering expectations unchanged

### Phase 2: Session-zone pin + CSV temporal text forms

**Delivers:** `pin_session_timezone` on the reader, called by the export
driver and tier-2 shaped playback's open after anchor resolution; the CSV
writer's pinned text forms for DATE / TIME / TIMESTAMPTZ / INTERVAL.

**Demo:** Opens a fixture emit, pins the session to an anchor zone,
materializes literals of the four types through `query_arrow`, writes CSV,
and prints the exact bytes — then shows the same bytes under a different
process `TZ`, demonstrating machine-independence.

**Contracts:** `pin_session_timezone`; CSV writer behavior table.

**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/reader/emit.py` |
| Modify | `src/fabulexa_forge/cli.py` |
| Modify | `src/fabulexa_forge/playback/shaped.py` |
| Modify | `src/fabulexa_forge/writers/csv.py` |
| Modify | `tests/writers/test_csv.py` |
| Create | `tests/reader/test_session_pin.py` |
| Modify | `tests/playback/test_shaped_open.py` |
| Create | `docs/sprints/temporal-elections/demos/phase_2_session_pin_csv.py` |

**Tests:**
- Pin then `SELECT current_setting('TimeZone')` (or a TIMESTAMPTZ
  materialization) reflects the anchor zone on both query surfaces
- Pin is a pure function of the anchor: same anchor pinned twice → same
  session state; different `TZ` env → same materialized values
- Export driver pins when an anchor resolves and does not touch session
  state when none does (CLI-level test through a fixture emit)
- Shaped playback open pins exactly as the export driver does (anchor
  supplied → pinned; None → not)
- CSV: each of the four types serializes to the pinned form — DATE
  `YYYY-MM-DD`; TIME fixed six-digit µs; TIMESTAMPTZ local-wall-clock+offset
  with the value-attached zone; INTERVAL `[-]H:MM:SS.ffffff` including a
  negative delta and an hours field > 24 (no day component)
- CSV: NULL of each new type serializes as today's NULL form
- CSV: existing TIMESTAMP and DOUBLE serialization byte-unchanged
  (regression pin on current behavior)

### Phase 3: Election config models

**Delivers:** The complete election grammar in `config/models.py` — parse
surface only; no mode consumes an election yet.

**Demo:** Loads a YAML exercising every election form (dimensional `as` on
timestamp/scd_window/elapsed, `date_parse` derivation, source `render` /
`date_parse` maps, events `render`, base `render` list), prints the parsed
models; then shows the refusals: both-set / neither-set elapsed, an
incomplete `date_parse` format, a `%H` directive, a column in both maps.

**Contracts:** All config models above.

**Steps:** `source → author (4 files)` — the model reshape and the
enumerative validator test suite each re-read the same deep config surface.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/config/models.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/columns.py` |
| Modify | `tests/config/test_models.py` |
| Modify | `tests/config/test_source_decls.py` |
| Modify | `tests/config/test_base_config.py` |
| Modify | `tests/exporters/dimensional/test_elapsed.py` |
| Create | `docs/sprints/temporal-elections/demos/phase_3_election_grammar.py` |

The `columns.py` touch is minimal: `build_elapsed_expr` narrows the
now-optional `unit` (`assert spec.unit is not None` on the numeric path —
the exact narrowing the Phase 4 `interval` branch keeps) so mypy-strict
stays green; behavior unchanged.

**Tests:**
- `TimestampSpec`: `as` absent → `as_ is None`; each of the four values
  parses; an unknown value is refused
- `ScdWindowSpec`: object form requires both `bound` and `as`; bare-literal
  `scd_window: valid_from` still parses; a bound-only object is refused
- `DateParseSpec`: valid formats parse (`%Y-%m-%d`, `%d %B %Y`, `%%`
  literal); missing year/month/day directive refused; `%H` and locale
  directives refused; empty `from` / `format` refused
- `ElapsedSpec`: `unit` alone OK; `as: interval` alone OK; both refused;
  neither refused (migrated `test_elapsed_spec_all_fields_required_missing_unit`
  keeps its intent — missing rendering is still an error)
- `DerivedSpec`: exactly-one across six kinds — `date_parse` alone OK,
  `date_parse` + `timestamp` refused
- `SourceTableDecl` / `SourceEventsDecl`: `render` / `date_parse` maps —
  empty map refused, empty key refused, a column in both maps refused
- `BaseRenderDecl` / `BaseConfig.render`: entries with duplicate `table`
  refused (entries-disjoint extension); `render` alone satisfies
  `at_least_one_field`
- Docstring-convention test stays green over the new models

### Phase 4: Dimensional attach points

**Delivers:** All four dimensional derivation surfaces wired — `as` on
`derived: timestamp`, the `scd_window` object form, `as: interval` on
elapsed, the `date_parse` derivation via the shared
`render_date_parse_expr` — plus the business rules
(`TemporalRenderRequiresAnchor`, `DateParseSourceColumn`, slice-only surface
growth) and the ordinal/incremental amendments.

**Demo:** A dimensional export over a fixture emit with an admission-date
`DATE`, a `timestamptz` instant, a date-grained SCD-2 window, an `INTERVAL`
wait, and a parsed `birth_date` — profiled output types shown; then the
refusals: an election with no anchor, a parse from a non-VARCHAR column, a
mutated date value failing loudly with table/column/value attribution, an
append-mode `order_by` on a `time`-elected column.

**Contracts:** `render_date_parse_expr`; elapsed builder; business rules;
ordinal amendment (§ Ordering and the ordinal amendment).

**Steps:** `source → author (6 files)` — the source reshape spans four
modules and the new-test suite enumerates per-election assertion groups over
the same surfaces.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/_sql.py` |
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/columns.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/scd.py` |
| Modify | `src/fabulexa_forge/exporters/dimensional/validation.py` |
| Modify | `tests/test_sql.py` |
| Modify | `tests/exporters/dimensional/test_columns.py` |
| Modify | `tests/exporters/dimensional/test_scd.py` |
| Modify | `tests/exporters/dimensional/test_elapsed.py` |
| Modify | `tests/exporters/dimensional/test_validation.py` |
| Modify | `tests/exporters/dimensional/test_windowed_failfast.py` |
| Create | `docs/sprints/temporal-elections/demos/phase_4_dimensional_elections.py` |

**Tests:**
- `render_date_parse_expr` (tests/test_sql.py): match → DATE; NULL → NULL;
  mismatch → error naming table, column, value; format/label with quotes
  splice safely
- `derived: timestamp` with each `as` value → correct DuckDB output type;
  no `as` → byte-identical current TIMESTAMP SQL
- `scd_window` object form: `{bound: valid_from, as: date}` renders a
  date-grained window; same-day versions collapse to `valid_from = valid_to`
  while raw version order is preserved; open interval's `valid_to` stays NULL
  under every election; bare literal unchanged
- Elapsed `as: interval`: INTERVAL equal to the numeric delta at µs;
  negative delta sign-preserved
- `derived: date_parse` end-to-end on a `prop__` VARCHAR; on a membership
  grain's `elem__` field; refusal on structural/virtual/constant sources and
  non-VARCHAR declared types (`DateParseSourceColumn`); refusal on a
  `slice_only` source (surface-list growth)
- `TemporalRenderRequiresAnchor`: explicit `as: timestamp` with no anchor
  refused at plan time, message names the column; unelected column with no
  anchor keeps today's raw-ns rendering
- Ordinal amendment: `order_by` on a `date`- and `timestamptz`-elected
  timestamp column compiles to raw-ns + `record_id`; on a `time`-elected
  column orders by rendered TIME + `record_id`; `{bound: valid_from}` object
  form joins the amendment population, `valid_to` bound stays outside
- Incremental (test_windowed_failfast): append-mode `order_by` naming a
  `time`-elected window-source column refused; a `date`-elected one accepted
- Existing dimensional suite green with unchanged expectations

### Phase 5: Source attach points

**Delivers:** Source-mode `render` / `date_parse` maps on declared tables
(`state` / `junction`) and the event log's `render` map, with
`RenderKeyIsInstantColumn` and the windowed omitted-column posture.

**Demo:** A source export with `render: {created_sim_time: date,
last_mutation_sim_time: timestamptz}` and `date_parse` on a payload column,
plus an event log rendered `event_sim_time: date` — profiled types shown;
then the refusals: a `render` key naming a payload column, an event-log key
other than `event_sim_time`, a key naming a column the `columns` selection
omits.

**Contracts:** `SourceTableDecl.render` / `date_parse`,
`SourceEventsDecl.render`, `RenderKeyIsInstantColumn`.

**Steps:** `source → author (3 files)`.

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/errors.py` |
| Modify | `src/fabulexa_forge/exporters/source/plan.py` |
| Modify | `src/fabulexa_forge/exporters/source/renders.py` |
| Modify | `src/fabulexa_forge/exporters/source/events.py` |
| Modify | `tests/exporters/source/test_plan.py` |
| Modify | `tests/exporters/source/test_renders.py` |
| Modify | `tests/exporters/source/test_events_render.py` |
| Create | `docs/sprints/temporal-elections/demos/phase_5_source_elections.py` |

**Tests:**
- State table `render` on each of its category's instant columns (via the
  reader's `structural_instant_columns`, never a hardcoded list) → elected
  types in the output; junction twin over interval columns
- `render` key naming a payload / non-instant column refused
  (`RenderKeyIsInstantColumn`); key naming a column the table's `columns`
  selection omits refused (existing omitted-declaration posture)
- Windowed invocation: a `render` key on a column the windowed render omits
  is refused by the windowed business-rule pass
- Renamed column stays addressable: `render` keys are source identities,
  composing with `rename`
- `date_parse` on a payload VARCHAR renders DATE in place (output name still
  governed by defaults + `rename`); non-VARCHAR refused; mismatch fails
  loudly with attribution
- Event log: `render: {event_sim_time: date}` renders the log's instant as
  DATE; any other key refused (mode-definitional domain)
- Source's global anchor requirement subsumes `TemporalRenderRequiresAnchor`
  (no new anchor error path; elections behind the existing posture)
- Existing source suite green with unchanged expectations

### Phase 6: Base attach points

**Delivers:** The base mode's per-table `render` declaration list —
lifecycle-instant elections and payload date parses keyed on pre-default
column identities — full and windowed.

**Demo:** A base export with `render` entries electing `created_sim_time:
date` and parsing a `prop__signup_date`, full and under an incremental
window — profiled types shown; then the refusals: an election with no anchor
(base's anchor is optional), a `last_mutation_sim_time` key, a duplicate
`table` entry.

**Contracts:** `BaseRenderDecl`, `BaseConfig.render`;
`TemporalRenderRequiresAnchor` on the base attach point.

**Steps:** none (single implementer).

**Files:**
| Action | File |
|--------|------|
| Modify | `src/fabulexa_forge/exporters/base/plan.py` |
| Modify | `src/fabulexa_forge/exporters/base/renders.py` |
| Modify | `tests/exporters/base/test_plan.py` |
| Modify | `tests/exporters/base/test_renders.py` |
| Modify | `tests/exporters/base/test_engine.py` |
| Create | `docs/sprints/temporal-elections/demos/phase_6_base_elections.py` |

**Tests:**
- `render` elections on `created_sim_time` / `deactivated_at`'s pre-default
  identities → elected types, composing with `rename` (keys stay pre-default)
- `last_mutation_sim_time` key refused (outside the key domain — the mode
  never emits it)
- Election with no resolved anchor refused (`TemporalRenderRequiresAnchor`
  names the column); no-anchor default rendering keeps raw-ns
- `date_parse` on a `prop__` VARCHAR renders DATE; non-VARCHAR refused;
  mismatch fails loudly with attribution; NULL flows through as NULL
- Windowed base export: elections apply per window identically to the full
  export; cast-back-to-sidecar-types posture unaffected for unelected columns
- `slice_only` posture: a `date_parse` source that is `slice_only` is
  refused (the mode's omission posture composing with the parse's refusal)
- Existing base suite green with unchanged expectations

## What Doesn't Change

Per the design doc § What Doesn't Change, binding on every phase:

- No-election configs render byte-identical SQL and output — the existing
  suite's expectations are the regression harness; no expectation rewrites
  except where a test names `render_anchor_timestamp_expr` itself (Phase 1)
  or asserts a validator message that now lists six kinds (Phase 3)
- Streaming: no election attaches; `render_ts` and the Python-side `ts`
  contract untouched
- Anchor resolution (`resolve_effective_anchor`): precedence, DST/ambiguity
  rules, one-anchor-per-invocation — untouched
- Ordering doctrine: every table's total order stays over raw sim-time keys
  and identity (the `time` amendment exception is an *ordering by value*,
  already the non-amendment default)
- No general cast knob: the election set is closed
- The sidecar and the vendored contract: no field added or read differently
- Corrupters: untouched (`schema_drift.retype_to` remains the only
  type-breaking surface)
- Incremental window membership, cursor, fingerprint: computed over raw
  sim-time and config identity as today; an election is ordinary config
  content under the existing fingerprint
- `init` proposal engines: never propose an election or a parse
- `value_map`, FK resolution, key election, lookup, row predicates:
  untouched except the named surface-list growths

## Module Changes Summary

| File | Change |
|------|--------|
| `src/fabulexa_forge/anchor.py` | `render_anchor_timestamp_expr` → `render_anchor_temporal_expr` with the election parameter (P1) |
| `src/fabulexa_forge/_sql.py` | New shared `render_date_parse_expr` (P4) |
| `src/fabulexa_forge/errors.py` | New error classes for the three new business rules (P4, P5) |
| `src/fabulexa_forge/config/models.py` | Election vocabulary + all attach-point models/validators (P3) |
| `src/fabulexa_forge/reader/emit.py` | `pin_session_timezone` (P2) |
| `src/fabulexa_forge/cli.py` | Export driver pins the session zone after anchor resolution (P2) |
| `src/fabulexa_forge/playback/shaped.py` | Shaped open joins the session-zone pin (P2) |
| `src/fabulexa_forge/writers/csv.py` | Pinned text forms for DATE/TIME/TIMESTAMPTZ/INTERVAL (P2) |
| `src/fabulexa_forge/exporters/dimensional/columns.py` | Default-election call (P1); elapsed `unit` narrowing (P3); `as` pass-through, `date_parse` branch, elapsed `interval` branch, ordinal amendment extension (P4) |
| `src/fabulexa_forge/exporters/dimensional/scd.py` | Default-election call (P1); `scd_window` object form (P4) |
| `src/fabulexa_forge/exporters/dimensional/validation.py` | `TemporalRenderRequiresAnchor`, `DateParseSourceColumn`, slice-only surface growth, election-aware incremental `order_by` rule (P4) |
| `src/fabulexa_forge/exporters/source/plan.py` | `render` / `date_parse` plan resolution + `RenderKeyIsInstantColumn` + windowed posture (P5) |
| `src/fabulexa_forge/exporters/source/renders.py` | Default-election call (P1); elected renderings + date parses (P5) |
| `src/fabulexa_forge/exporters/source/events.py` | Default-election call (P1); event-log `render` map (P5) |
| `src/fabulexa_forge/exporters/base/plan.py` | `BaseRenderDecl` resolution, key-domain and anchor gating (P6) |
| `src/fabulexa_forge/exporters/base/renders.py` | Default-election call (P1); elected renderings + date parses (P6) |
| `tests/test_anchor.py` | Renderer election tests (P1) |
| `tests/test_sql.py` | `render_date_parse_expr` tests (P4) |
| `tests/writers/test_csv.py` | Pinned text-form tests (P2) |
| `tests/reader/test_session_pin.py` | New — session-zone pin tests (P2) |
| `tests/playback/test_shaped_open.py` | Shaped-open pin tests (P2) |
| `tests/config/test_models.py` | Election model/validator tests (P3) |
| `tests/config/test_source_decls.py` | Source decl map tests (P3) |
| `tests/config/test_base_config.py` | `BaseRenderDecl` tests (P3) |
| `tests/exporters/dimensional/test_columns.py` | Instant-election + date-parse + amendment tests (P4) |
| `tests/exporters/dimensional/test_scd.py` | `scd_window` object-form tests (P4) |
| `tests/exporters/dimensional/test_elapsed.py` | Model-test migration (P3); `interval` tests (P4) |
| `tests/exporters/dimensional/test_validation.py` | New business-rule tests (P4) |
| `tests/exporters/dimensional/test_windowed_failfast.py` | Election-aware append-mode `order_by` tests (P4) |
| `tests/exporters/source/test_renders.py` | Renderer-name migration (P1); election tests (P5) |
| `tests/exporters/source/test_plan.py` | `render` / `date_parse` plan + refusal tests (P5) |
| `tests/exporters/source/test_events_render.py` | Event-log render tests (P5) |
| `tests/exporters/base/test_plan.py` | `BaseRenderDecl` plan tests (P6) |
| `tests/exporters/base/test_renders.py` | Base election render tests (P6) |
| `tests/exporters/base/test_engine.py` | Full + windowed election engine tests (P6) |
| `docs/sprints/temporal-elections/demos/phase_*_*.py` | One demo per phase (P1–P6) |
