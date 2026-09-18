---
status: draft
---

# `date_ref` over an SCD-2 window bound

A third `date_ref` shape, `{scd_window: valid_from | valid_to}`, that keys an
`scd: type2` dim's version row into `dim_date` by the local date on which the
version took effect or ended — so the calendar is reachable by key from every
table family the dimensional mode produces, not from every family but the
versioned dims.

---

## Problem

A `date_ref` column renders a `yyyymmdd` key from a **base-column identity**: the
instant shape addresses a structural instant or a `prop__` column, the parse shape
a VARCHAR column. On an `scd: type2` dim the version-window bounds are neither.
They are computed by the versioned-intervals reconstruction — raw ns
`version_start` / `version_end` at history change points — and reach the output
only as `derived: scd_window` columns. There is no spelling for "the calendar key
of the day this version took effect":

```yaml
- name: dim_company
  scd: type2
  key: [company_id, valid_from]
  columns:
    - name: valid_from
      derived: {scd_window: {bound: valid_from, as: date}}
    - name: valid_from_date_key
      date_ref: {source: valid_from}
      # refused at plan time:
      #   timestamp source 'valid_from' is not available on grain 'records'
```

`valid_from` is a sibling output column, and `date_ref` addresses base columns
only — by design, so the key and the timestamp stay independent. The versioned
dims are therefore the one table family whose rows reach `dim_date` only by
joining a date-grained `scd_window` column on `dim_date.date`, while every fact
reaches it by key. A warehouse that keys every temporal edge uniformly — the shape
most teaching material and most BI tooling assume — is not expressible.

## Solution

`DateRefSpec` gains a third, mutually exclusive shape that names a version-window
bound instead of a base column. The bound shape renders the bound's raw ns through
the shared anchor renderer with the `date` election and then through the one
date-key expression — the same two renderers, in the same order, as the instant
shape; only the SQL fed in differs (the version relation's bound column rather
than a grain column). A sibling `scd_window: {bound, as: date}` and the key
therefore agree by construction, mirroring the instant shape's agreement with
`derived: timestamp {source, as: date}`.

```yaml
- name: dim_company
  scd: type2
  key: [company_id, valid_from]
  columns:
    - name: company_id
      from: presentation_id
    - name: valid_from
      derived: {scd_window: {bound: valid_from, as: date}}
    - name: valid_from_date_key
      date_ref: {scd_window: valid_from}        # 20260314
    - name: valid_to_date_key
      date_ref: {scd_window: valid_to}          # NULL on the current version
```

The bound shape is legal only on `scd: type2` tables (elsewhere there is no window
to address — refused at plan time); it is an explicit `date` election, so it is
anchor-required; a `valid_from` key is horizon-invariant and a `valid_to` key never
is (the successor closes the version — the reading `derived: scd_window` already
has); the range guard and the manifest's `references` transcription are
shape-blind and unchanged. Because a version bound is computed rather than
carried, the column has no provenance entry; the report carries which bound the
key addresses so the pinned documentation — the one downstream consumer that
changes — can name it.

## Affected Subsystems

- **Date dimension — the `date_ref` column mode.** The grammar gains the bound
  shape; the derivation, availability, NULL, family-agreement, key, and
  windowing rules extend to it. The "no `scd_window` source shape" boundary is
  retired. `resolve_date_ref_source` — today "the one source column a spec
  reads" — becomes nullable: the bound shape reads no base column. Provenance
  stamps no entry for it; the slice-only read-surface collector contributes no
  name for it (it never appends `None` to the column's read list); window
  variance answers from its bound-shape branch (below).
- **Dimensional SCD-2 builder.** The type-2 column surface admits the bound shape
  and hands the `date_ref` renderer the version relation's `version_start` /
  `version_end` column — the same SQL identity `derived: scd_window` reads —
  rather than a tracked cast or a records-relation column. The records-grain
  column builder never sees the shape — the plan-time rule below refuses it on
  any non-versioned table before compile — and asserts that a `date_ref` it is
  handed has a source column, as it asserts `table_decl`; it never renders a
  reference to a `None` column.
- **Dimensional plan-time validation.** One new always-on business rule,
  `DateRefWindowBoundRequiresScd2`. `TemporalRenderRequiresAnchor` covers the
  bound shape as an explicit `date` election. `Scd2ColumnModeSupported` admits
  it (it admits `date_ref` today, shape-blind). `TimestampSourceAvailable`,
  `DateParseSourceColumn`, and `SliceOnlyColumnRefused` do not apply — the shape
  reads no base column.
- **Incremental windowing.** The one per-column variance reading gains an
  explicit bound-shape branch, mirroring its `derived: scd_window` branch:
  `valid_from` invariant, `valid_to` varying. The branch is evaluated before any
  source-column lookup — nulling the source alone is not the change, since the
  reading's fall-through answer is "invariant" and would misclassify `valid_to`.
  `window_delivery_class` and `KeyColumnsStable` change behaviour through the
  reading with no edit of their own.
- **Companion artifacts and the documentation channel.** `QuerySpec` and
  `TableReport` carry one more per-column map, `window_bounds`, stamped at
  dimensional plan compile beside `references` and forwarded verbatim by both
  report-assembly sites, read by the dictionary alone. The dictionary's pinned
  `date_ref` description gains a bound-shape variant that names the bound from
  that map instead of a provenance source column. The manifest is unchanged:
  it transcribes `references` as for every shape and exposes no shape-specific
  field — the bound is on record in the embedded config, exactly as the instant
  shape's `source` and the parse shape's `format` are.

## What Doesn't Change

- `dim_date` itself — its name, thirteen columns, generation, determinism,
  delivery class, and pack membership.
- The instant and parse shapes — their grammar, availability rules, rendering,
  provenance, variance, and pinned description.
- The range guard — it probes by `references`, shape-blind; the bound shape's
  keys are probed exactly as the others'.
- `derived: scd_window` — its bare and object forms, elections, version
  structure, the ordinal amendment, and the `Scd2NeedsHistory` rule, which still
  requires a `valid_from` `scd_window` column in `key`. A bound-shape `date_ref`
  in `key` does not satisfy it (§ Semantics — a date-grained key cannot
  identify a version).
- `references` — the bound shape stamps `dim_date` as the other shapes do.
- The manifest — its field set and `manifest_format_version` are unchanged;
  `columns[].references` is `"dim_date"` for the bound shape as for every
  shape, and no shape-specific field is added.
- The SCD-2 reconstruction — version bounds come from history change points and
  no election or key rendering reads, merges, or renumbers a version row.
- `init` proposes no `date_ref` of any shape; `StreamConfig`, `mode: source`,
  and `mode: base` have no `date_ref`.
- `ColumnProvenance` — a computed column still gets no entry; the bound shape
  does not invent a virtual source column.

## Semantics

### Shape and derivation

| Shape | Spec | Date derivation | Availability rule |
|---|---|---|---|
| Instant | `{source: <col>}` | unchanged | unchanged |
| Parse | `{from: <col>, format: "<fmt>"}` | unchanged | unchanged |
| **Bound** | `{scd_window: valid_from \| valid_to}` | The bound's local wall-clock date in the anchor's zone — the shared anchor renderer with the `date` election over the version relation's `version_start` (`valid_from`) or `version_end` (`valid_to`), exactly what `derived: scd_window: {bound, as: date}` renders, then `date_key_expr` | The declaring table is `scd: type2` (`DateRefWindowBoundRequiresScd2`); no base column is read, so no source-availability or type gate applies |

The bound shape reads no base column. `resolve_date_ref_source` returns `None`
for it; a reader that needs the bound reads `spec.scd_window` directly.

### Behaviour

| Condition | Result |
|---|---|
| Bound shape, anchor resolves | Key of the bound's local date in `anchor.timezone` (DST observed, as for every wallclock rendering) |
| Bound shape, anchor is `None` | Refused at validation — `TemporalRenderRequiresAnchor`, naming the column and the `date` rendering. A bound-shape `date_ref` is inherently an explicit `date` election, as the instant shape is |
| Bound shape on a table that is not `scd: type2` | Refused at plan time — `DateRefWindowBoundRequiresScd2`, naming the column, the table, and the bound; runs whether or not the table is windowed |
| `scd_window: valid_to` on the current (open) version | `NULL` key — `version_end` is `NULL`; the faithful value, breaking no reference, as for `deactivated_at` under the instant shape |
| `scd_window: valid_from` | Never `NULL` — every version has a start |
| Two versions start on the same local date | Both rows carry the same `valid_from` key. Version structure is election-invariant: the key never merges, suppresses, or renumbers a version row (the underlying raw-ns bounds remain distinct) |
| Bound shape beside a `derived: scd_window: {bound: <same>, as: date}` | The key equals `date_key_expr` over the sibling's value on every row — Family agreement, extended |
| Bound shape with no `scd_window` sibling of that bound | Allowed — the author decides whether the date travels beside the key, exactly the instant shape's posture toward `derived: timestamp` |
| Bound shape named in a table's `key` | Allowed for `valid_from` (horizon-invariant under `KeyColumnsStable`); refused for `valid_to` under `KeyColumnsStable` (its value can change over the run — the successor closes it). A `valid_from` key does **not** satisfy `Scd2NeedsHistory`'s "a `valid_from` `scd_window` column in `key`" — same-day versions collapse at date grain, so a date-grained key cannot identify a version; the rule reads `derived: scd_window` columns only |
| Bound shape on a windowed table | Its value channel is the bound's: `valid_from` horizon-invariant, `valid_to` never — the reading `derived: scd_window` has. A type-2 dim carrying a `valid_to` key of either form is `upsert`; one carrying only `valid_from` bounds and keys may still be `append` if every other channel is invariant |
| Bound shape named by `ordinal.partition_by` / `order_by` | Not reachable — `derived: ordinal` is refused on `scd: type2` tables (`Scd2ColumnModeSupported`) |
| Source is `temporal_class: slice_only` | Not applicable — no source column; the collector that gathers a column's value-read sources for `SliceOnlyColumnRefused` contributes no name for the bound shape, so the check sees nothing to classify |
| Bound shape, `date_dimension` absent | Refused at parse, as for any `date_ref` — `ExportConfig.date_refs_require_date_dimension` is shape-blind |
| Range guard | Unchanged — probed by `references` in output order; a bound key outside `[from, to]` refuses before the first write with `DateRefOutOfRange` |
| Provenance of a bound-shape column | **No entry** — a version bound is computed, not carried (the `scd_window` columns themselves carry none). `references[<name>] = "dim_date"` is stamped as for every shape; `window_bounds[<name>]` is stamped with the bound |
| Description of a bound-shape column | Author's `description` when given, at origin `author`; else the pinned bound-shape prose — "Calendar key (`yyyymmdd`) into `dim_date`, derived from this version's `valid_from` bound" / "… `valid_to` bound" — at origin `forge`; `unit` is `None` on both, as for every `dim_date` key. The dictionary resolves it from `references` and `window_bounds` on the report alone |
| Manifest | `columns[].references` is `"dim_date"` as for every shape; no other field changes — the bound is on record in the embedded config |

### Invariants

Relied on:

- The versioned-intervals derivation's `version_start` is never `NULL` and
  `version_end` is `NULL` exactly on a record's last version; both are raw ns on
  the same scale as every structural instant, so the shared anchor renderer
  applies to them unchanged — `derived: scd_window` already relies on this.
- The `date` election is the local wall-clock date in the anchor zone and a pure
  projection of the `timestamp` rendering.
- `version_start` is a version row's identity under horizon truncation and
  `version_end` is not — the reading the windowing classifier already encodes
  for `derived: scd_window`.

Introduced or extended:

1. **One key authority** — unchanged in statement: the bound shape's keys are
   produced by `date_key_expr` like every other `date_ref` value.
2. **Family agreement, extended.** For any version row, `date_ref {scd_window:
   b}` equals `date_key_expr` over `derived: scd_window {bound: b, as: date}`.
3. **No provenance for a computed key.** A bound-shape `date_ref` has no
   `ColumnProvenance` entry; the report's `window_bounds` entry is the one fact
   the documentation channel reads for it. `references`, `provenance`, and
   `window_bounds` partition the calendar keys: every `window_bounds` key is a
   `references` key mapped to `dim_date`, and every `references` key mapped to
   `dim_date` has exactly one of a `provenance` entry (the instant and parse
   shapes) or a `window_bounds` entry (the bound shape). The dictionary's
   `dim_date` branch reads whichever is present and never both.
4. **Election-invariant version structure** — unchanged: a bound key is a
   rendering over the reconstructed bounds and never alters the version rows.

## Configuration

```yaml
date_dimension: {from: "2026-01-01", to: "2030-12-31"}

dimensional:
  tables:
    - name: dim_company
      role: dim
      scd: type2
      source: {grain: records, kind: actor, filter: {prop__actor_type: company}}
      key: [company_id, valid_from]
      columns:
        - name: company_id
          from: presentation_id
        - name: status
          from: prop__status
        - name: valid_from
          derived: {scd_window: {bound: valid_from, as: date}}
        - name: valid_to
          derived: {scd_window: {bound: valid_to, as: date}}
        - name: valid_from_date_key
          date_ref: {scd_window: valid_from}
          description: Calendar key of the day this version took effect.
        - name: valid_to_date_key
          date_ref: {scd_window: valid_to}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `date_ref.scd_window` | `valid_from` \| `valid_to` | One of `source` / `from` / `scd_window` | The SCD-2 version-window bound whose local date keys the row into `dim_date`. Bare literal only — the `date` election is implied, so there is nothing to elect. Legal only on an `scd: type2` table |

## Interface Contracts

### Config Models

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
        a date.

        Raises:
            ValueError: Zero or more than one shape set; `format` without
                `from_` or `from_` without `format`; an empty column name;
                a `format` that is not date-complete (time-only) or that
                fails the declared-parse directive rules.
        """
```

### Runtime Types

```python
@dataclass(frozen=True)
class QuerySpec:
    """(existing fields unchanged)

    `window_bounds` maps each bound-shape `date_ref` output column to the
    bound it addresses. Stamped at dimensional plan compile beside
    `references`, in declaration order; every other mode leaves it empty.
    Every key is also a `references` key mapped to `dim_date`. Forwarded
    to `TableReport` by both report-assembly sites.
    """

    window_bounds: "Mapping[str, Literal['valid_from', 'valid_to']]" = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class TableReport:
    """(existing fields unchanged)

    `window_bounds` is forwarded verbatim from the compiled `QuerySpec` —
    no default, so every report-assembly call site states it explicitly.
    """

    window_bounds: "Mapping[str, Literal['valid_from', 'valid_to']]"
```

### Functions

```python
def resolve_date_ref_source(spec: "DateRefSpec") -> str | None:
    """The one base column a `date_ref` spec reads, or None.

    The instant shape's `source`, the parse shape's `from_`, or None for
    the bound shape, which reads no base column — its input is the SCD-2
    version relation's bound. Shared by every reader of a `date_ref`
    column's source identity (provenance, the slice-only read surface,
    window-variance classification); each takes its no-column path on
    None, and a reader that needs the bound reads `spec.scd_window`.

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
    """Build a SQL expression for one SCD-2 column.

    (existing contract unchanged, plus:)

    - A bound-shape `date_ref` renders through render_date_ref_expr handed
      `"<version_alias>"."version_start"` / `"version_end"` per its bound —
      the same input `derived: scd_window` reads — before any source-column
      resolution; an instant- or parse-shape `date_ref` is handed the
      tracked/untracked source_expr as today.

    Args / Returns / Raises: unchanged.
    """


def check_date_ref_window_bound_on_scd2(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
) -> None:
    """Enforce DateRefWindowBoundRequiresScd2: a bound-shape `date_ref`
    declares on an `scd: type2` table.

    Always-on, full export included; a pure function of the declaration.
    The version window the shape addresses exists only on a versioned dim.

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

    Args / Raises: unchanged.
    """


def resolve_column_doc(
    doc: "Documentation", table: "TableReport", column_name: str, output_type: str
) -> "ColumnDoc | None":
    """One output column's resolved documentation.

    (existing contract unchanged, except on a column whose `references`
    entry is `dim_date`:) with an `author_descriptions` entry, the author's
    description at origin "author"; without one, at origin "forge", the
    pinned bound-shape prose naming `table.window_bounds[column_name]` when
    the column has a `window_bounds` entry, else the pinned `date_ref`
    prose naming the column's provenance source column. Either way `unit`
    is None and no source doc is inherited.

    Args / Returns: unchanged.
    """
```

The bound-shape pinned constant, beside the existing template: `"Calendar key
(`yyyymmdd`) into `dim_date`, derived from this version's `{bound}` bound"`,
`{bound}` the report's `window_bounds` entry.

### Window variance

The one per-column variance reading gains a bound-shape branch beside its
existing `derived: scd_window` branch and answers from it before any
source-column lookup: `valid_from` invariant (`None`); `valid_to` varying, with
the same reason text — "is the SCD-2 valid_to bound, closed by the next
version". The reading's fall-through for a column with no source is "invariant",
so the branch is load-bearing: without it `valid_to` would be misclassified and
`KeyColumnsStable` and the delivery classifier would wrongly admit it.
`window_delivery_class` and `check_key_columns_stable` pick the answer up
through the shared reading.

## Validation Rules

### Parse-Time (Pydantic)

| Validator | Refuses |
|---|---|
| `DateRefSpec.exactly_one_shape` | Zero or more than one of `source` / `from` / `scd_window`; `format` without `from` or `from` without `format`; an empty column name; a `format` outside the declared-parse directive rules or not date-complete. A `scd_window` value outside `valid_from` / `valid_to` is a type refusal |
| `ColumnDecl.exactly_one_column_mode` | unchanged — `date_ref` together with any other column mode |
| `ExportConfig.date_refs_require_date_dimension` | unchanged, shape-blind — any `date_ref` with no `date_dimension` block |

### Business Rules

| Rule | Checks | Error Message |
|---|---|---|
| `DateRefWindowBoundRequiresScd2` (new, always-on) | A bound-shape `date_ref` declares on an `scd: type2` table | `"column '{name}' on table '{table}': date_ref scd_window: {bound} addresses the SCD-2 version window, and the table is not scd: type2"` |
| `TemporalRenderRequiresAnchor` (extended) | A bound-shape `date_ref` is an explicit `date` election and needs a resolved anchor | existing message, rendering `date` |
| `Scd2ColumnModeSupported` (unchanged in behaviour) | `date_ref` of every shape is admitted on a type-2 table | unchanged |
| `KeyColumnsStable` (through the shared variance reading) | A `valid_to` bound-shape key column is refused; `valid_from` admitted | existing message, variance "is the SCD-2 valid_to bound, closed by the next version" |
| `Scd2NeedsHistory` (unchanged) | Still requires a `valid_from` `derived: scd_window` column in `key`; a bound-shape `date_ref` does not satisfy it | unchanged |
| `TimestampSourceAvailable`, `DateParseSourceColumn`, `SliceOnlyColumnRefused` | Not applied to the bound shape — no base column is read | — |
| `DateRefOutOfRange` (unchanged) | Probes bound-shape keys through `references`, shape-blind | unchanged |

Rule ordering: `DateRefWindowBoundRequiresScd2` runs in the per-column rule
loop, ahead of `TemporalRenderRequiresAnchor`. That is the one constraint that
matters: no other per-column rule reads the bound shape (`ProjectionColumnExists`
and `TimestampSourceAvailable` read the parse shape's `from` and the instant
shape's `source` directly and skip it), so without this placement a bound shape
on a non-versioned table would refuse under the anchor rule when no anchor
resolves — or, when one does, pass every plan-time rule and fail only at
compile, where the records-grain builder asserts a source column. Its position
relative to the table-level type-2 rules is immaterial: on a type-2 table the
rule is a no-op.

## Rationale

- **A shape of `date_ref`, not a new column mode.** The output is a reference
  into `dim_date` — the thing `date_ref` is — and the manifest's `references`
  field, the range guard, and every consumer of the compiled spec are already
  shape-blind. Adding a shape reaches all of them for free; a new mode would
  duplicate each.
- **The version bound is the third input `date_ref` can key.** The instant shape
  keys what the emit says happened at an instant; the parse shape keys a date the
  producer minted upstream; the bound shape keys when a *version* took effect,
  which is the SCD-2 reconstruction's own fact. Nothing else in the dimensional
  mode carries a date the calendar cannot already be reached from.
- **Same renderers, same order — agreement by construction.** Routing the bound
  through the anchor renderer's `date` election and then `date_key_expr` is what
  makes "the key and the sibling `scd_window: {…, as: date}` never disagree" a
  property of the SQL rather than of a test.
- **Bare literal, no election.** The object form of `scd_window` exists to elect a
  rendering; a `date_ref` is by definition the `date` election, so a
  `{bound, as}` object would carry a knob with exactly one legal value.
- **No sibling required.** Authors may emit the key without the date, the date
  without the key, or both — the independence the instant shape already grants,
  and the reason `date_ref` addresses inputs rather than sibling outputs.
- **No provenance, one stamped fact.** Provenance means "faithfully carried from
  one source column"; a version bound is computed, and the `scd_window` columns
  correctly carry none. Inventing a virtual source column for the key would
  misstate that. The documentation channel never re-derives from config or SQL,
  so the one fact its pinned prose needs — which bound — travels on the report,
  stamped at the same site and in the same posture as `references`.
- **A date-grained key is not a version identity.** Two versions can start on
  one local date. `Scd2NeedsHistory` keeps requiring the `scd_window` column in
  `key` for exactly this reason, and the bound-shape key is admitted in `key`
  only as a redundant, invariant member beside it.
- **Refused off type-2, not silently `NULL`.** A version window exists only on a
  versioned dim; a bound shape on any other table is a declaration error, not a
  column of `NULL`s.
