---
status: draft
---

# Per-Column Description Override

## Problem

The companion data dictionary resolves every output column's documentation by
inheritance: a faithfully-carried column gets its *source* column's prose (the
sidecar's `description` for a payload column, the contract's pinned string for
a structural column), and a computed column gets nothing. There is no author
input — the documentation channel's dictionary carries no author-facing
config; `readme_overlay` is the author's prose channel but is table-level
only (`overview` / `table: <name>` slots).

That inheritance is exactly right until the author renames into domain
vocabulary. The retail example's `fact_action_product` renames engine columns
into retail names, but the dictionary still renders the producer's
engine-voiced prose beside them:

```yaml
- {name: role, from: elem__role_name}
# README renders: "Name of the role this binding fills"
# — engine-voiced, meaningless to the export's retail audience
```

The author can rename a column but cannot re-voice its description. Forge
already recognizes the underlying problem for its own four pinned contract
strings whose prose points at base structure a shaped export lacks
(`_EXPORT_STRUCTURAL_REWRITES` — the export has left the base naming domain)
— but producer-authored sidecar prose is not forge's to rewrite. That call
belongs to the author, and today there is no place to make it. Computed
columns are the same gap from the other side: a derived measure or an SCD-2
validity column inherits nothing and the author cannot say what it means in
the table's own vocabulary; only the mode template's generic prose covers it.

## Solution

An optional per-column **`description` override** in the export config of the
three companion-writing modes (dimensional, source, base), consumed only by
the companion dictionary. Where present, the author's prose is the column's
rendered description — winning over sidecar inheritance, the contract
strings, forge's export rewrites, and the "inherits nothing" answer for
computed columns. Everywhere else the documentation channel is untouched:
units, declared-value lists, table descriptions, glosses, and the corrupter's
sidecar forwarding all resolve exactly as today.

Each mode attaches the override in its existing column-addressing idiom:

```yaml
# dimensional — a key on the per-column entry
- {name: role, from: elem__role_name,
   description: "The product's role in the action: the item viewed, carted, or purchased."}

# source — a map on the declared table, keyed by source identity (parallel to rename/render)
tables:
  - name: products
    kind: entity
    rename: {prop__price_cents: price_cents}
    descriptions:
      prop__price_cents: "Catalogue price in cents at the current state."

# base — a map on the rename entry, keyed by source identity (parallel to columns)
base:
  rename:
    - table: records__actor
      columns: {prop__tier: loyalty_tier}
      descriptions:
        prop__tier: "Loyalty program tier (bronze / silver / gold)."
```

The override is translated to output-column names at plan compile, stamped on
the compiled plan as a third per-table documentation map beside the
provenance and kind-value maps, forwarded verbatim by both report-assembly
sites, and resolved author-first by the shared dictionary — so README and
manifest render it identically, and datasets are byte-identical with or
without it.

## Affected Subsystems

- **Export-config models** — three grammars gain the override surface:
  the dimensional per-column entry gains an optional `description` field; the
  source declared-table gains an optional `descriptions` map keyed by source
  identity; the base rename entry gains an optional `descriptions` map keyed
  by source identity (and its "at least one of" well-formedness widens to
  admit a descriptions-only entry). All three are load-validated for shape;
  the two maps are plan-gated for key validity.
- **The documentation channel** — the column-description resolution gains an
  author tier with first precedence. Its "no author-facing config" boundary
  narrows to: the dictionary has exactly one author input, per-column
  description prose. Its "sourced, never invented" invariant gains the export
  config as an enumerated documentation source (the same standing the
  `readme_overlay` and `value_map` already have on their surfaces). The
  resolved-doc `origin` vocabulary gains `author`, produced only by the
  companion dictionary's resolution — the reader's documentation view never
  emits it and remains two-authority (contract / sidecar).
- **The compiled-plan carriage** — the mode-neutral compiled table and the
  per-table report gain a third per-output-column documentation map
  (output column name → author description), stamped at plan compile,
  forwarded verbatim by the shared full-export write dispatch and the
  incremental driver's windowed report assembler — the same carriage
  discipline as the provenance and kind-value maps; no builder entry-point
  signature changes.
- **The three batch-mode plan compilers** — each translates its config
  surface to the output-name-keyed map while compiling (dimensional from the
  column entries; source and base from the source-identity-keyed maps through
  their rename resolution), and gates unknown map keys as plan-time errors.
- **The companion dictionary** — column resolution consults the author map
  first. For a column with provenance, the author's description replaces the
  inherited one (unit, enum options, and gloss resolution unchanged). For a
  column without provenance, the resolution now yields a description-only
  resolved doc instead of nothing.
- **The incremental driver's fingerprint** — the canonical config dump
  excludes the description surfaces, honoring the standing rule that
  documentation is run-level presentation and can never make a resumed drip
  refuse (the posture `readme_overlay` already has).

## What Doesn't Change

- **Streaming.** No companion artifacts exist; no override surface is added.
  A streaming-side documentation surface remains a separate design.
- **Corrupters.** The regenerated sidecar forwards producer documentation
  attributes verbatim; corrupt configs gain no description surface.
- **The reader's documentation view.** Contract-vs-sidecar resolution,
  placeholder substitution, enum glosses, scenario narrative — untouched. The
  reader never sees export config.
- **Units and declared-value lists.** These are facts about the value, not
  voice; they resolve exactly as today even on an overridden column. There is
  no author unit override and no enum-gloss override.
- **Table-level prose.** `readme_overlay` remains the table- and export-level
  prose channel; the forwarded `tables[].description` is not overridable or
  suppressible.
- **The source event log.** Its columns are mode-definitional and
  template-documented; the log declaration gains no description surface.
  (Its `changes`-key and `item_type` vocabularies are value surfaces, not
  documentation.)
- **No suppression.** The override re-voices; it cannot silence. An empty or
  whitespace-only description is a load-time error, and there is no "render
  nothing" spelling. An author who wants no inherited prose beside a column
  writes better prose.
- **`init` proposals.** No engine proposes description stubs or slots;
  annotations remain YAML comments.
- **Data planes.** Row membership, linkage, ordering, values, notices, exit
  codes — byte-identical with and without overrides (documentation is
  presentation).
- **Conformance.** No C1–C15 check ranges over documentation; none is added.

## Semantics

### Resolution precedence

For one output column, the rendered description resolves in this order; the
first present answer wins, and each later tier is exactly today's behavior:

| Tier | Source | Applies to |
|---|---|---|
| 1 | Author override (this design) | Any output column of the table |
| 2 | Forge-pinned dictionary constants (interval-end description; the four export rewrites of base-pointing contract strings) | Carried columns those constants address |
| 3 | Inherited source-column answer (sidecar prose / contract string) | Columns with single-source provenance |
| 4 | Nothing | Everything else |

The override replaces the *description only*. On a carried column, unit
inheritance (including the ns-stops-at-temporal-rendering rule), declared
enum-value lists (including post-`value_map` translation), and kind-value
gloss lists resolve as today, unchanged by the presence of an override.

| Condition | Result |
|---|---|
| Override on a renamed payload column | Author prose renders; source unit and enum options still inherit |
| Override on a projected structural column | Author prose renders; the contract string (and any export rewrite of it) is not consulted |
| Override on a computed column (derived measure, SCD-2 `valid_from`/`valid_to`, elapsed, `null:` placeholder, re-derived key surface) | Author prose renders where today nothing renders; unit stays absent |
| Override on the history-interval end column | Author prose replaces the forge-authored end-of-validity constant; unit resolution through `sim_time` unchanged, origin `author` (as for every override — § Rendered surfaces) |
| Override on a `derived: value_map` column | Author prose renders; the documented domain remains the post-map value list |
| No override | Byte-identical output to today, README and manifest both |
| Override present, documentation absent in the emit (undocumented sidecar) | Author prose renders — the override does not depend on anything inheriting |

### Carriage

The author map is answered once, **at plan compile** — the one point each
mode knows both the author's config addressing and the final output-column
names. The compiled table representation carries one additional per-table
map, output column name (post-rename) → author description; both
report-assembly sites forward it verbatim onto the per-table report, which is
how it reaches the companion builders on the report they already receive.
Builders never re-derive it from config. Absence of an entry means "no
override" — there is no fallback and no empty-string sentinel.

Addressing is each mode's existing idiom:

| Mode | Author addresses by | Translated to output name via |
|---|---|---|
| dimensional | the column entry itself (`description` beside `name`) | the entry's own `name` |
| source | source identity (the `rename`/`render` key vocabulary of that table) | the table's rename resolution |
| base | source identity (the rename entry's `columns` key vocabulary) | the entry's rename resolution |

### Rendered surfaces

The README's per-column line and the manifest's per-column `description`
field both render the resolved (author-first) description — the two surfaces
must never disagree, so the resolution stays in the one shared dictionary.
The manifest's embedded config carries the override fields like any other
config content, so the provenance of an authored description is on record.
The resolved-doc `origin` value for an author-answered description is
`author`; it is produced only by the companion dictionary's resolution, never
by the reader.

### Incremental behavior

Documentation is run-level: every emitting window's whole-state artifact
rewrite renders the same resolved dictionary. The incremental fingerprint's
canonical config dump excludes the three description surfaces, so editing a
description mid-drip never raises a fingerprint mismatch; the next emitting
window's whole-state rewrite renders the current prose (the `readme_overlay`
posture, generalized to the whole documentation-presentation class).

### Invariants

Relied on: plan iteration order is deterministic; the report-assembly sites
forward per-table maps verbatim; README and manifest render from one shared
resolution; documentation has no data-plane effect.

Introduced or amended:

1. **Author-first, then one authority.** With an override present, the
   author's prose is the column's description; with none, exactly one
   authority answers as today. Never a blend, never a fallback chain beyond
   this table, never inference from names, types, or rows.
2. **Sourced, never invented** — unchanged in force, wider in enumeration:
   every rendered description traces to the sidecar, the vendored contract, a
   forge-pinned constant, or the author's export config.
3. **Presentation only.** Datasets, notices, and exit codes are byte-identical
   with and without overrides; a description can never make a resumed drip
   refuse.
4. **Determinism.** Same emit + same config + same code version →
   byte-identical rendered documentation.

## Configuration

```yaml
# dimensional
dimensional:
  tables:
    - name: fact_action_product
      role: fact
      source: {grain: membership, kind: tick_decision, property: bindings}
      key: [action_id, role]
      columns:
        - {name: role, from: elem__role_name,
           description: "The product's role in the action: the item viewed, carted, or purchased."}
        - name: added_at
          derived: {scd_window: valid_from}
          description: "When the item entered the action's basket set."
```

```yaml
# source
source:
  tables:
    - name: products
      kind: entity
      sub_types: [product]
      rename: {prop__price_cents: price_cents}
      descriptions:
        prop__price_cents: "Catalogue price in cents at the current state."
        created_sim_time: "When the product entered the catalogue."
```

```yaml
# base
base:
  rename:
    - table: records__actor
      columns: {prop__tier: loyalty_tier}
      descriptions:
        prop__tier: "Loyalty program tier (bronze / silver / gold)."
    - table: records__entity          # descriptions without rename is legal
      descriptions:
        prop__error_rate: "Fraction of requests the storefront host is failing."
```

| Field | Type | Required | Description |
|---|---|---|---|
| dimensional `columns[].description` | str | No | Rendered description for this output column; replaces any inherited or forge-pinned prose in README and manifest. Non-empty, non-whitespace. |
| source `tables[].descriptions` | dict[str, str] | No | Source identity → rendered description. Keys use the table's source-column vocabulary (the `rename` key vocabulary). Non-empty map, non-empty keys, non-empty non-whitespace values. |
| base `rename[].descriptions` | dict[str, str] | No | Source identity → rendered description. Keys use the entry's `columns` key vocabulary (state-at column identities). Non-empty map, non-empty keys, non-empty non-whitespace values. Counts toward the entry's at-least-one-field rule. |

## Interface Contracts

### Config Models

```python
class ColumnDecl(StrictBaseModel):
    """One output column declaration with exactly one source mode."""

    # ... existing fields unchanged ...
    description: str | None = None
    """Author-supplied rendered description for this output column. Replaces
    the inherited (or forge-pinned) description in the companion README and
    manifest; unit and declared-value resolution are unaffected. Absent ->
    inheritance as before. Non-empty when present."""
```

```python
class SourceTableDecl(StrictBaseModel):
    """One declared output table: a name, one population source, optional
    column selection, renames, row selection, and descriptions."""

    # ... existing fields unchanged ...
    descriptions: dict[str, str] | None = None
    """Source column identity -> author-supplied rendered description, keyed
    like `rename` (source identity, never the output name). Replaces the
    inherited description in the companion README and manifest for the
    addressed output column. Keys validated at plan time against the table's
    source columns. Absent -> inheritance as before."""
```

```python
class RenameEntry(StrictBaseModel):
    """One table's output-name and description overrides, keyed by sidecar
    identity."""

    # ... existing fields unchanged ...
    descriptions: dict[str, str] | None = None
    """Source column identity -> author-supplied rendered description, keyed
    like `columns` (state-at column identities). Replaces the inherited
    description in the companion README and manifest. Keys validated at plan
    time against the target table's columns. Counts toward the entry's
    at-least-one-field rule. Absent -> inheritance as before."""
```

### Runtime Types

```python
@dataclass(frozen=True)
class QuerySpec:
    """A compiled output table: name, SELECT, write mode, optional view pair.

    `author_descriptions` is keyed by output column name (post-rename), like
    `provenance` and `kind_values`; stamped at plan compile from the mode's
    config surface. Empty means no overrides.
    """

    # ... existing fields unchanged ...
    author_descriptions: "Mapping[str, str]" = field(default_factory=dict)
```

```python
@dataclass(frozen=True)
class TableReport:
    """One output table as written.

    `author_descriptions` is forwarded verbatim from the compiled `QuerySpec`
    that produced this table — no default, so every report-assembly call site
    states it explicitly, like `provenance` and `kind_values`.
    """

    # ... existing fields unchanged ...
    author_descriptions: "Mapping[str, str]"
```

```python
@dataclass(frozen=True)
class ColumnDoc:
    """Resolved documentation for one declared column.

    origin names the single authority that answered: "contract" for a
    structural column (pinned strings, instance placeholders bound),
    "sidecar" for a per-run column (verbatim carry), "author" for a
    companion-dictionary resolution answered by the export config's
    per-column description override. The reader's documentation view never
    produces "author" — it is stamped only downstream, by the companion
    dictionary.
    """

    description: str | None
    unit: str | None
    origin: Literal["contract", "sidecar", "author"]
```

### Functions

```python
def resolve_column_doc(
    doc: "Documentation", table: "TableReport", column_name: str, output_type: str
) -> "ColumnDoc | None":
    """One output column's resolved documentation.

    Args:
        doc: The emit's documentation view.
        table: The output table report.
        column_name: The output column name (post-rename).
        output_type: The column's materialized DuckDB type text.

    Returns:
        With an `author_descriptions` entry for the column: the resolved doc
        with the author's description and origin "author" — on a carried
        column the inherited unit rides along under today's unit rules; on a
        column with no provenance the doc is description-only (unit None).
        Without an entry: exactly today's resolution — the inherited source
        answer (with the interval-end constant, the export structural
        rewrites, and the ns-unit stop applied), or None for a column with no
        carried provenance or whose source carries neither description nor
        unit.
    """
```

## Validation Rules

### Parse-Time (Pydantic)

```python
@model_validator(mode="after")
def description_nonempty(self) -> Self:
    """ColumnDecl.description, when present, is non-empty and
    non-whitespace."""
```

```python
@model_validator(mode="after")
def table_shape(self) -> Self:
    """SourceTableDecl: `descriptions`, when present, is a non-empty map
    with non-empty, distinct keys and non-empty, non-whitespace values —
    folded into the existing shape validator alongside `rename` / `render`."""
```

```python
@model_validator(mode="after")
def entry_well_formed(self) -> Self:
    """RenameEntry: at least one of name / columns / descriptions is set;
    `descriptions`, when present, is a non-empty map with non-empty keys and
    non-empty, non-whitespace values — extending the existing well-formedness
    validator."""
```

### Business Rules

Plan-time gates, run after plan compile and before any write (the overlay
drift-alarm posture — an addressing error never leaves a half-written
target). Neither mode mints a new error: a bad `descriptions` key is the
same addressing mistake as a bad `rename` key, so each mode's existing
rename-key gates widen their range to the entry's `descriptions` keys and
raise under the same identities — the not-addressable and
slice-only-unsatisfiable refusals ride along with the same widening:

| Rule | Checks | Error |
|---|---|---|
| Source descriptions key valid | Every `descriptions` key names a source column the declared table selects (same key vocabulary and gate point as `rename`) | `SourceColumnUnresolved` (or `SourceColumnNotAddressable` / the slice-only refusal, exactly as the same key would fail as a `rename` key), naming the table and the offending key |
| Base descriptions key valid | Every `descriptions` key names a state-at column identity of the entry's target table (same key vocabulary and gate point as `columns`) | `BaseRenameUnresolved` (or `BaseRenameSliceOnly`, exactly as the same key would fail as a `columns` key), naming the table and the offending key |

The dimensional surface needs no key gate: the description rides the column
entry itself and cannot address a column that does not exist.

**Fingerprint exclusion (behavioral, test-guarded).** The incremental
fingerprint's canonical config dump excludes all three description surfaces;
changing only a description never raises a fingerprint mismatch.
