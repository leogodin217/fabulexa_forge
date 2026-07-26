---
status: draft
---

# Structural temporal columns: one reader-owned answer

## Problem

The sidecar describes every column's name, type, and — for `prop__` columns —
its temporal pair. It says nothing about the **structural** columns: whether
`created_sim_time` carries a sim-time instant, whether `deactivated_at` can
change after a record is created, what `last_mutation_sim_time` means. Those
facts are pinned by the contract, positionally and by name, and are
conformance-checked; they are simply not machine-readable from the emit.

So each consumer that needs one of those facts hardcodes it at the point of
use. There are now five independent encodings of *"which records-table
structural columns hold a sim-time instant"*, and they disagree:

| Encoding | Answers |
|---|---|
| source mode's wallclock render | `created_sim_time`, `deactivated_at`, `last_mutation_sim_time` |
| base mode's wallclock render | `created_sim_time`, `deactivated_at` |
| source mode's junction render | `joined_sim_time`, `left_sim_time` |
| dimensional's `derived: timestamp` source allowlist | `last_mutation_sim_time` |
| dimensional's incremental window key | `last_mutation_sim_time` |

The last one is correct — window membership genuinely keys on "when did this row
last change" — though even it is not one encoding: dimensional holds it in more
than one private copy, one of them unreferenced. The second is narrower than the
first for a structural reason, not
a divergent judgment: base renders over its state-at reconstruction, whose
projection carries only two of the three records instants. The fourth is stale,
and it is the defect. It descends from an
upstream recipe written before `created_sim_time` existed as a column, which
recommended `last_mutation_sim_time` as a stand-in for a write-once record's
firing time. That recipe was never revisited; the column it names later gained
the guarantee that it also advances on deactivation, which turned the shortcut
into a trap.

The consequence is that a records-grain fact cannot render its own birth time:

```
ERROR: timestamp source 'created_sim_time' is not available on grain 'records'
       for 'fact_pairing.opened_at'
ERROR: timestamp source 'deactivated_at' is not available on grain 'records'
       for 'fact_pairing.closed_at'
```

Both columns project fine as raw `BIGINT` through `from:`, so the projection
surface and the timestamp surface disagree with each other as well. For a
short-lived record — created, then deactivated, with no property writes between
— `last_mutation_sim_time` equals the *close* instant on every row, so the only
reachable timestamp is the moment the record ended. The instant it began, which
is the natural event time, has no expression. A fact in that shape cannot carry
a `TIMESTAMP` comparable to an SCD-2 dimension's `valid_from` / `valid_to`, so
the standard effective-dated join is impossible for it.

This was hit four times independently across five example scenarios during a
single QA round, and in each case the author had no config-expressible
workaround: substituting `last_mutation_sim_time` would have been an
approximation of a different quantity.

## Solution

The reader gains one surface owning the contract's structural-column temporal
facts, in the shape of the records-column taxonomy it already owns: pure,
name-based, no sidecar and no DuckDB, total over the contract's column
families, and loud on anything it does not recognise. Every consumer that needs
one of those facts reads through it instead of keeping a private copy.

The surface answers two questions and deliberately no others:

1. **Which structural columns of a table category carry a sim-time instant, and
   which instant does each name** — `created` / `closed` / `last_touched` for
   records, `changed` for the change log, `joined` / `left` for a membership
   interval.
2. **Which records structural columns may change after the record is created** —
   the fact incremental export needs to decide whether a column is safe to read
   once and treat as settled.

The dimensional records-grain timestamp allowlist becomes all three records
instants. Source and base drop their private wallclock sets in favour of the
same surface. Behavior is unchanged in both, for different reasons: source
already holds the full answer, while base renders over its state-at
reconstruction, whose projection never carries `last_mutation_sim_time` — the
surface's `last_touched` entry is simply unreachable there. Either way the
private copies go, and with them the possibility of drifting apart from the
contract later.

The *instant* is a contract fact and belongs in the reader. The *name* an
instant is given in an output is presentation, and stays with each mode: source
renders operational names, base keeps the structural names under its own
minimal default, dimensional is author-verbatim by design. The shared instant
vocabulary is what lets three different naming policies rest on one set of
facts.

## Affected Subsystems

- **Reader** — gains the structural-temporal surface as a second name-based
  classifier alongside the records-column taxonomy. Pure, emit-independent, no
  new dependency on the sidecar or DuckDB. It carries contract facts only, never
  presentation. The sidecar parse additionally gains category validation: a
  table's `category` is admitted against the closed contract set — `fixed`,
  `records`, `membership` — and an unrecognised value is refused when the
  sidecar is read, the same failure class as any other malformed sidecar field.
  That refusal is what makes an unrecognised category at the new surface a
  caller error rather than emit data.
- **Dimensional exporter** — its timestamp allowlist, one map over all of its
  grains, resolves through the reader rather than being held privately: each
  grain's set is the instant-carrying structural columns of the grain's table
  category, plus the grain's virtual interval-end column where the grain
  defines one. The records grain is the only one whose reachable set changes,
  widening from one column to three; the two history grains — point and
  interval, both reading the fixed-category change log, with only the
  interval grain defining a virtual interval-end column — and the membership
  grain reproduce their current sets exactly. Its structural mutability set is
  likewise resolved through the reader. Its grain-to-category mapping and its
  one virtual grain column remain its own, because both are dimensional
  concepts the contract does not define. Its incremental window key is
  unchanged and stays private — it answers a different question and answers it
  correctly. That key is held today in more than one private copy inside
  dimensional, one of them unreferenced; collapsing those copies to a single
  private constant is a mode-internal cleanup, explicitly out of this design's
  scope.
- **Source exporter** — its records and junction wallclock sets resolve through
  the reader. Behavior is unchanged. Its operational rename map stays where it
  is: which real-world name an instant takes is source's presentation policy.
- **Base exporter** — its wallclock set resolves through the reader. Behavior is
  unchanged: the render iterates the state-at relation's columns, and that
  projection carries no `last_mutation_sim_time`, so the surface's
  `last_touched` entry is vacuous for base.
- **Conformance** — its checks are unchanged, but one failure is reclassified
  out of its reach: an emit whose sidecar carries an out-of-set `category` no
  longer opens, so `validate` surfaces the sidecar-parse refusal instead of a
  C1 `CheckResult` (see *Reader — sidecar category validation*). Its pinned
  column lists must remain literal: they *are* the check that the contract's
  structural prefix is present and correctly ordered, so expressing them in
  terms of a shared surface would make the check test itself.

## What Doesn't Change

- The `slice_only` policy, in every mode. It is a truthfulness gate, not a
  provenance or presentation gate, and nothing here relaxes it.
- Conformance C1–C14 and its pinned structural column lists.
- Each derivation's own fold-output column tuple — those are each derivation's
  published shape, not a restatement of contract facts.
- Dimensional's incremental window-key selection.
- Every mode's output naming policy and rename map. Naming stays with the mode.
- The projection surface for `from:` / `correlation:`, which already resolves
  from the sidecar and already admits every structural column.
- No presentation-column detection is introduced. The emit carries no marker
  distinguishing a producer-minted column from an author-declared one, and no
  inference recovers one reliably.
- No record-kind archetype classification is introduced. Which instant a fact
  means is the author's modelling decision (Principle #7).
- No new config surface. Authors gain reachable timestamp sources, not new
  fields.

## Semantics

### The instant vocabulary

Each structural sim-time column names exactly one instant. The vocabulary is
closed and derives from the contract's column definitions.

| Category | Column | Instant | Nullable |
|---|---|---|---|
| `records` | `created_sim_time` | `created` | no |
| `records` | `deactivated_at` | `closed` | yes |
| `records` | `last_mutation_sim_time` | `last_touched` | no |
| `fixed` | `sim_time` | `changed` | no |
| `membership` | `joined_sim_time` | `joined` | no |
| `membership` | `left_sim_time` | `left` | yes |

A structural column absent from this table carries no instant. A `prop__`
column may hold a time-valued payload, but that is a declared property with a
sidecar type, not a structural instant, and it is outside this surface.

### Mutability

| Records structural column | May change after creation |
|---|---|
| `created_sim_time` | no — set once at creation, unchanged by any later write or deactivation |
| `active` | yes |
| `deactivated_at` | yes |
| `last_mutation_sim_time` | yes |
| `fork_path`, `record_id`, `record_index`, `presentation_id` | no |
| `ref_index__<name>` | tracks its sibling `prop__<name>`; outside the surface's domain — asking raises, and the caller resolves it through the sibling's sidecar answer |

Mutability of a `prop__` column is a sidecar question, answered by its temporal
pair, and remains where it is answered today. A `ref_index__<name>` column
follows its sibling `prop__<name>` and is resolved the same way. This surface
covers the structural half only, and its domain is closed: asked about any
name outside the pinned structural set — a `prop__<name>`, a
`ref_index__<name>`, or a name the contract does not pin at all — it raises
rather than guessing.

"Structural" here is defined against the records-column taxonomy's families:
the `identity`, `presentation`, and `lifecycle` families, minus the ref-index
prefix. The taxonomy classifies `ref_index__<name>` into `identity` — family
alone does not isolate it — so the caller's dispatch is family *plus* the
taxonomy's own ref-index prefix rule: `payload` routes to the sidecar; an
`identity` name matching the ref-index prefix routes to its sibling's sidecar
answer; everything else in `identity` / `presentation` / `lifecycle` is this
surface's domain. The two halves are asked together by the consumer that needs
both; neither subsumes the other.

### Loudness

The loud conditions differ in kind, and so do their signals. For a raise to be
a caller-error signal, no emit data may reach the surface unvalidated: the
sidecar parse refuses an unrecognised `category` at read time, closing the set
before any consumer asks.

| Condition | Result |
|---|---|
| Unrecognised table category | Raise. The category set is closed, contract-pinned, and validated at read time, so an unrecognised value is a programming error, not emit data. |
| Structural column name carrying no instant | Return no instant. The column-name space is open — every `prop__<name>` lives in it — so absence is an ordinary answer the caller interprets. |
| Structural column name carrying no instant, used as a `derived: timestamp` source | The caller refuses, naming the column and the grain. |
| Non-structural name asked for mutability | Raise. The records structural set is contract-pinned and closed; the open name space is the taxonomy's to classify, and the caller dispatches through it — family plus the ref-index prefix rule (see *Mutability*) — before asking. A `prop__` or `ref_index__` mutability question belongs to the sidecar, and a name the contract does not pin has no mutability answer at all — a quiet "immutable" would be an invented fact. |

### Timestamp source availability, after the change

| `derived: timestamp` source on grain `records` | Result |
|---|---|
| `created_sim_time` | accepted — the record's birth instant |
| `deactivated_at` | accepted — the record's close instant; NULL for a row still active, which propagates to a NULL timestamp |
| `last_mutation_sim_time` | accepted — the record's last-touched instant |
| a `prop__<name>` present on the grain surface | accepted, as today |
| anything else | refused, naming source and grain, as today |

Widening the allowlist changes no rendering path: the timestamp renderer
qualifies whatever column it is given and hands it to the anchor renderer, so
all three instants render through the same expression, and all three fall back
to the raw nanosecond integer when no anchor resolves.

A NULL `deactivated_at` renders as a NULL timestamp rather than an error. This
matches how the membership grain already treats a NULL `left_sim_time`, and it
is the honest rendering: the record has not closed.

### Invariants relied on

- The structural prefix of a records-category table is pinned by name and
  position and is conformance-checked, so classifying it by name is sound. This
  is the one place where naming a column list in code is correct rather than a
  contract violation — the prohibition on hardcoding column lists exists because
  the *variable tail* is producer-extensible, and it has no force against a
  pinned prefix.
- `created_sim_time` is non-NULL on every row of every records table.
- `deactivated_at` is NULL exactly when `active` is true (C7).

### Invariants introduced

- Exactly one module answers "does this structural column carry an instant, and
  which one". A mode that needs the answer reads it; a mode that holds a private
  copy is a defect.
- The instant vocabulary is presentation-free. No output name appears in it.
- The reader admits only the contract's three table categories: an unrecognised
  `category` in the sidecar is refused when the sidecar is read, so no consumer
  ever sees one.

## Interface Contracts

### Reader — structural temporal columns

```python
StructuralInstant = Literal[
    "created", "closed", "last_touched", "changed", "joined", "left"
]
```

```python
def structural_instant_columns(category: str) -> Mapping[str, StructuralInstant]:
    """
    The structural columns of a table category that carry a sim-time instant.

    Pure and emit-independent: the mapping is a property of the contract's
    pinned column layout for the category, not of any particular emit. A
    category's mapping is the same for every emit at the supported format
    version. Columns absent from the returned mapping carry no instant.

    Args:
        category: The sidecar table category — "fixed", "records", or
            "membership".

    Returns:
        Column name to the instant it names, for every instant-carrying
        structural column of the category. Empty for a category that pins
        none.

    Raises:
        ValueError: `category` is not a recognised table category. The
            category set is closed and validated when the sidecar is read, so
            an unrecognised value is a caller error, never emit data.
    """
```

```python
def records_structural_column_is_mutable(name: str) -> bool:
    """
    Whether a records-table structural column's value may change after the
    record is created.

    Answers the structural half of temporal mutability only, over a closed
    domain: the contract's pinned records structural columns. A
    `prop__<name>` column's mutability is declared per-emit by its sidecar
    temporal pair and is not answered here; a `ref_index__<name>` column
    tracks its sibling `prop__<name>` and follows the sibling's answer. A
    caller needing both halves classifies through the records-column
    taxonomy first — routing by family plus the taxonomy's ref-index
    prefix rule, since `ref_index__<name>` classifies as `identity` — and
    asks each half in turn.

    Args:
        name: A records structural column name — one the records-column
            taxonomy classifies as `identity` (excluding the ref-index
            prefix), `presentation`, or `lifecycle`.

    Returns:
        True when the column is one whose value the producer may change
        after creation; False when the contract pins it as set once.

    Raises:
        ValueError: `name` is not a records structural column — a
            `prop__<name>`, a `ref_index__<name>`, or a name the contract
            does not pin. The structural set is closed; mutability of the
            open remainder is either the sidecar's question (`prop__`,
            `ref_index__`) or nowhere guaranteed (a producer-added column),
            so a silent False would state a fact the contract does not
            hold.
    """
```

### Reader — sidecar category validation

Not a new surface — a narrowing of the existing sidecar parse. Today the
parse admits any string as a table's `category` and defers value diagnosis to
`validate`; after this change the parse refuses a value outside the contract
set at the same point where it already refuses a missing or non-string one:

```python
# Sidecar parse, per table entry — after the existing structural check:
#   category not in {"fixed", "records", "membership"}
#       → SidecarStructureError(
#             f"table '{name}' unrecognised category '{category}'"
#         )
```

The sidecar reader's deliberate permissive-parse posture — parse
structurally, diagnose values in `validate` — is narrowed for this one field
and only this one: the structural-temporal surface raises on an unrecognised
category as a *caller* error, and that signal is honest only if no
emit-supplied category can reach a consumer unvalidated. The value set
restates the vendored schema's `category` enum — a contract-pinned closed
set, the same hardcoding class as the pinned column lists (see *Invariants
relied on*).

The narrowing reclassifies one diagnosis. Today an out-of-set category parses
fine and surfaces later as a C1 conformance failure; after the change the
emit refuses to open, and `validate` reports the structural refusal instead
of a `CheckResult` — the same observable behavior as a *missing* category
today. The failure moves from conformance diagnosis to the structural floor;
it does not disappear. C1's whole-document schema check keeps its `category`
enum clause unchanged — the clause is simply unreachable once the parse
refuses first.

## Validation Rules

### Parse-Time (Pydantic)

None. No config model changes. The sidecar-parse category check is a reader
contract, not a config rule — see *Interface Contracts*.

### Business Rules

| Rule | Checks | Error Message |
|---|---|---|
| `TimestampSourceAvailable` | A `derived: timestamp` source on a grain is either an instant-carrying structural column of the grain's table category, the grain's virtual interval-end column where the grain defines one, or a `prop__<name>` present on the grain's projectable surface | `"timestamp source '{source}' is not available on grain '{grain}' for '{table}.{column}'"` |

The rule's shape and message are unchanged; only the set it consults moves from
a private literal to the reader surface, which is what widens it from one
records instant to three.
