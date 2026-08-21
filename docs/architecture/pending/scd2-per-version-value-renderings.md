---
status: draft
---

# SCD-2 Per-Version Value Renderings

---

## Problem

The value-rendering elections gave every export surface a clean rendering for
noisy or misdeclared payload values except one: a **tracked** property on an
`scd: type2` dimensional table. The concrete case from the export-qa realism
round: saas `dim_account.engagement_score` — a 1–100 score whose history
values carry raw float64 noise (`4.800000000000001`, 15 fraction digits on
6,063 of 13,478 non-null version rows) — ships raw DOUBLE, while the same
property renders `DECIMAL(5,2)` in source mode's state table and, under the
per-kind agreement rule, in its audit-log `changes` entries:

```yaml
- name: engagement_score
  derived: {decimal: {from: prop__engagement_score, as: [5, 2]}}
# ERROR: column 'engagement_score' on table 'dim_account': derived: decimal
# is not supported on an scd: type2 table; type2 columns support only from,
# null, derived: scd_window, derived: timestamp, derived: date_parse, and
# derived: value_map
```

Two stacked plan-time gates produce this, both principled under the rule set
they were written for:

- The type2 column-mode gate admits only modes that are pure per-record
  functions of the **static** projectable surface; `derived: decimal` and
  `derived: json_precision` were excluded wholesale.
- A second gate refuses any admitted derived spec whose source property is
  not `temporal_class: constant`, because a tracked property's value surface
  is per-version and no per-version semantics existed for derived columns.

The gap is wider than decimal. The same two gates refuse **every** pure value
rendering over a tracked type2 source: a tracked payload BIGINT sim-instant
cannot be `derived: timestamp`-rendered, a tracked VARCHAR date string cannot
be `date_parse`-rendered, a tracked code cannot be `value_map`-rendered.
Each is a value the versioned reconstruction already produces, cast to its
sidecar type, one per version row — with no way to say what the value *is*.

## Solution

One rule change, uniformly applied. On an `scd: type2` table, a **pure
per-row value rendering** — `decimal`, `json_precision`, `timestamp`,
`date_parse`, `value_map` — becomes legal over a tracked source and is
evaluated **per version**: the rendering authority compiles against the
versioned reconstruction's cast per-version value instead of the composed
records relation's current-state value. Same rendering expression, different
source alias — the existing one-compiler property extends to a new attach
site rather than growing a second implementation. The genuinely
per-record-relational modes (`fk`, `correlation`, `derived: ordinal`,
`derived: elapsed`, `lookup`) stay refused; their refusal was never about
value purity.

```yaml
- name: dim_account
  role: dim
  scd: type2
  columns:
    - name: engagement_score
      derived: {decimal: {from: prop__engagement_score, as: [5, 2]}}  # per-version
    - name: status_changed_at
      derived: {timestamp: {source: prop__status_changed_at}}          # per-version
```

Version structure is election-invariant: boundaries derive from raw history
change points, so a rounding that renders adjacent versions visibly identical
(`4.801` → `4.80`, `4.804` → `4.80`) never merges, suppresses, or renumbers a
version row. This is the same posture the `scd_window` date election already
takes — rendering changes presentation, never the interval structure.

## Affected Subsystems

- **Dimensional exporter** — the type2 column-mode surface widens: the
  admitted derived set becomes `scd_window`, `timestamp`, `date_parse`,
  `value_map`, `decimal`, `json_precision`, each legal over both source
  classes (`constant` → per-record from the records relation, unchanged;
  `tracked` → per-version from the versioned reconstruction, new). The
  constant-source-only derived gate is retired outright — its remaining
  concern (`slice_only` sources) is already carried by the export-wide
  slice-only refusal surface, whose derived-source coverage is unchanged.
  The type2 compile binds the existing per-column rendering builders to the
  versioned value for tracked sources; no new rendering authority, no new
  election site species.
- **Validation runner** — the type2 column-mode rule's admitted set and
  message change; the constant-source derived rule is deleted. The
  source-type gates (`DecimalSourceIsDouble`, `JsonPrecisionSourceIsVarchar`,
  the timestamp source-domain rule, the date-parse VARCHAR rule) apply to
  tracked sources through the same sidecar declared-type authority they
  already read — a tracked property's declared type is the same sidecar fact
  whether its value is read per-record or per-version.

## What Doesn't Change

- **Config grammar.** The `derived` one-of, `DecimalSpec`,
  `JsonPrecisionSpec`, and the timestamp / date_parse / value_map spellings
  are untouched. This design changes where the specs are *legal*, not their
  shape.
- **The rendering authorities.** One authority per election kind; tie rules,
  overflow / NaN / bad-payload loud errors, byte preservation, and the pinned
  text forms are exactly the shipped contracts. Elected text stays identical
  at every attach site across modes.
- **The versioned-intervals derivation.** Version boundaries, value
  reconstruction, and the interval primitive's contract are untouched;
  renderings compose above it in mode compile, preserving the
  above-the-faithful-read invariant.
- **Non-type2 grains.** The records, `history_point`, `history_interval`,
  and membership grains' election surfaces are unchanged. In particular the
  `history_interval` grain's `value` column keeps its declared codec VARCHAR
  type and is deliberately out of scope: numeric series in interval facts
  ship as codec text regardless of this design, and giving them a numeric
  rendering is a re-typing question, separable and not taken up here.
- **The per-record-relational refusals.** `fk`, `correlation`,
  `derived: ordinal`, `derived: elapsed`, and `lookup` remain refused on
  type2 tables under their existing gates and messages.
- **Reader, derivations, conformance, corrupters, compare, writers,
  incremental, streaming, source, base.** No contract in any of them moves.
  The DECIMAL text form and the compare decimal family shipped with the
  value elections; the incremental fingerprint already treats elections as
  ordinary config content.

## Semantics

### Mode admissibility on `scd: type2`

| Column mode | Source class `constant` (or structural / projection-introduced) | Source class `tracked` | Source class `slice_only` (non-exempt) |
|---|---|---|---|
| `from` | Per-record, records relation (unchanged) | Per-version, versioned reconstruction (unchanged) | Refused (slice-only surface, unchanged) |
| `derived: scd_window` | n/a (structural bounds; unchanged) | n/a | n/a |
| `derived: timestamp` / `date_parse` / `value_map` | Per-record (unchanged) | **Per-version (new)** | Refused (slice-only surface) |
| `derived: decimal` / `json_precision` | **Per-record (new)** | **Per-version (new)** | Refused (slice-only surface) |
| `fk`, `correlation`, `derived: ordinal`, `derived: elapsed`, `lookup` | Refused (unchanged) | Refused (unchanged) | Refused (unchanged) |

A `constant` source under `decimal` / `json_precision` is per-record by the
same reasoning that admits it under `timestamp` today: its value is constant
across one record's version rows, so the rendered value repeats identically
per version. The admission rule generalizes from "pure per-record function of
static values" to: **a pure per-row value function is legal wherever the row
surface supplies its source value; on type2 that surface is the records
relation for constant sources and the versioned reconstruction for tracked
sources.**

The `slice_only` column refuses **non-exempt** sources only. The slice-only
surface's sub-typed-discriminator carve-out (`prop__<K>_type` with non-empty
`subtype_values` — exempt on every policing surface, and no surface applies a
narrower predicate) holds here as everywhere. An exempt discriminator is
untracked, so the row surface that supplies its value is the records relation:
it evaluates **per record**, exactly as a constant source does. This is a
deliberate widening — the retired constant-source gate refused a `value_map`
or `date_parse` over an exempt `slice_only` discriminator on a type2 table;
under this design it is legal, rendered per record from the current
classification value, carried as a classification and never presented as an
as-of value (the carve-out's shipped posture).

### Per-version evaluation

| Condition | Result |
|---|---|
| Tracked source, version row carries a value | The rendering authority's output for that version's cast value — byte-identical to what the same value renders at any other attach site (mode table, `changes` entry, streaming after-image) |
| Tracked source, version value is `NULL` (pre-first-assignment versions, including the creation row of a genesis-null property) | `NULL` of the output type — the shipped NULL rule, applied per version |
| Adjacent versions whose rendered values collide (`4.801` / `4.804` → `4.80`) | Both version rows emitted, values identical; `valid_from` / `valid_to` and version count unchanged |
| Tracked source whose value never changed post-creation (single reconstructed version) | One version row, rendered once — flag-authoritative membership is unchanged |
| Decimal overflow / NaN / Infinity in **any** version's value | The shipped loud export-time error naming table, column, and offending value — historical values are inside the guard's range, not just current state |
| Invalid JSON / non-numeric declared leaf in any version's payload | The shipped loud payload-guard error, same scope |
| `date_parse` source whose value in any version is non-`NULL` and fails the declared format | The shipped loud strict-parse export failure, same scope — historical text values must parse, not just the current one |
| `timestamp` over a tracked BIGINT with no resolved anchor | The explicit election is refused at validation (the shipped anchor-required rule); the unelected shorthand renders raw ns — both unchanged, now reachable from a tracked source |

### Invariants

Relied on (existing): version boundaries derive from raw history change
points; the rendering authorities are pure value functions; elected text is
identical at every attach site; the derivations layer serves unrendered
values; the versioned reconstruction's cast per-version value equals the
records relation's native value for the same underlying value — the codec
round-trip fidelity `from` on tracked sources already rests on, and the fact
that makes source-class-blind rendering (below) byte-identical.

Introduced:

1. **Version structure is election-invariant.** No rendering election can
   create, merge, suppress, renumber, or reorder a version row, and
   `valid_from` / `valid_to` are computed from raw bounds regardless of any
   value election on the table.
2. **Source-class-blind rendering.** For the same source value, the rendered
   output is byte-identical whether the value was read per-record or
   per-version — the election has one semantics; the source class only
   selects which rows supply values.

## Configuration

No new fields. The existing derived spellings become legal on `scd: type2`
columns over tracked sources:

```yaml
- name: dim_account
  role: dim
  scd: type2
  source: {grain: records, kind: entity, sub_types: [company_hub]}
  key: [account_id, valid_from]
  columns:
    - {name: account_id, from: presentation_id}
    - name: engagement_score
      derived: {decimal: {from: prop__engagement_score, as: [5, 2]}}
    - {name: valid_from, derived: {scd_window: valid_from}}
    - {name: valid_to, derived: {scd_window: valid_to}}
```

## Interface Contracts

No new public interfaces and no new config models. Two plan-time validators
change contract; the per-column rendering builders gain an attach site with
their signatures unchanged (they already take the source expression to wrap).

### Functions

```python
def check_scd2_column_mode_supported(
    col_decl: "ColumnDecl",
    table_decl: "TableDecl",
) -> None:
    """Enforce Scd2ColumnModeSupported: type2 columns use supported modes.

    The type2 surface admits from, null, derived: scd_window, and the pure
    per-row value renderings derived: timestamp / date_parse / value_map /
    decimal / json_precision — each a pure function of one row's source
    value, evaluated per record for constant sources and per version for
    tracked sources. It refuses fk, correlation, derived: ordinal, and
    derived: elapsed — cross-row or grain-surface semantics the type2 build
    does not define. (lookup is gated separately by LookupColumnSafety;
    slice_only sources by the export-wide slice-only surface.)

    Args:
        col_decl: The column declaration.
        table_decl: The output table declaration (gate applies iff
            scd: type2; also used for error messages).

    Raises:
        ExportError: The column uses an unsupported mode on an scd: type2
            table.
    """
```

`check_scd2_derived_source_constant` is **deleted** (breaking-changes
principle — no shim, no alias). Its two concerns are covered without it:
tracked sources are now legal by design, and `slice_only` sources are refused
by the export-wide slice-only surface, whose exhaustive derived-source list
already names every spelling this design touches.

## Validation Rules

### Parse-Time (Pydantic)

Unchanged — the election models' bounds and shape validators are untouched.

### Business Rules

| Rule | Checks | Error Message |
|---|---|---|
| `Scd2ColumnModeSupported` (changed) | Every column of an `scd: type2` table uses an admitted mode — `from`, `null`, `derived: scd_window`, or a pure per-row value rendering (`derived: timestamp` / `date_parse` / `value_map` / `decimal` / `json_precision`); `fk`, `correlation`, `derived: ordinal`, `derived: elapsed` refused | `"column '{column}' on table '{table}': {mode} is not supported on an scd: type2 table; type2 columns support only from, null, derived: scd_window, and the value renderings derived: timestamp, derived: date_parse, derived: value_map, derived: decimal, and derived: json_precision"` |
| `Scd2DerivedSourceConstant` (deleted) | — | — |
| `DecimalSourceIsDouble`, `JsonPrecisionSourceIsVarchar`, `DateParseSourceColumn`, timestamp source-domain rule (unchanged rules, widened reach) | The same declared-type gates, now also satisfied or violated by tracked sources on type2 — the declared type is a sidecar fact independent of source class | Existing messages |
| Slice-only refusal (unchanged) | An elected or derived source column is not non-exempt `slice_only`, on type2 exactly as elsewhere | Existing messages |
| Export-time overflow / parse / payload guards (unchanged rules, widened reach) | The in-SQL decimal overflow guard, the `date_parse` strict-parse failure, and the JSON payload guards now range over every version's value on a type2 tracked source | Existing messages |
