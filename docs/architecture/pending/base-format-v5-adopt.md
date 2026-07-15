---
status: draft
---

# Adopt base-format v5 — read the class, judge the emit

## Problem

`contract/` vendors `base_format_version: 5`. The reader's version gate declares `4`.
No emit currently satisfies both the gate and C1, so **nothing validates end-to-end**:
every positive fixture fails C1 with `5 was expected`, and 156 tests fail behind that one
mismatch. The repo is broken against its own vendored contract.

v5 changes the boundary in three ways, and forge must answer all three before it can
claim to read the format at all:

| v5 change | What it obliges forge to do |
|---|---|
| Per-column `temporal_class ∈ {constant, tracked, slice_only}`, paired with `history_tracked` | Model it on the sidecar column; **judge the pairing** (C13) |
| The creation seed becomes **unconditional** — every history-tracked property of every record carries a genesis `history` row at that record's `created_sim_time`, NULL-valued when the property was absent at creation | Judge it (C13's semantic clause, C11's new converse clause); absorb the extra `history` rows |
| `last_mutation_sim_time` promoted to a binding whole-lifecycle high-water mark | Nothing — forge already treats it as one |

The first two are what make v5 a *bump* rather than an additive extension. A v4 emit's
`history_tracked` bit conflated two opposite point-in-time contracts — a column that
never changes (current value exact at every T) and a column that changes without a trace
(past unknowable) — and a type-2 property with zero `history` rows was legal, meaning
"created NULL, never changed". v5 deletes both ambiguities. `temporal_class` splits the
conflated bit; the unconditional seed makes an empty as-of result mean *exactly*
`T < created_sim_time`.

**This doc adopts v5. It does not act on it.** Reading the class and using the class are
different jobs, and the second is a family of decisions (which exports refuse a
`slice_only` column, how corrupters label a broken genesis row) that deserve their own
designs. See § *Deferred, deliberately*.

### What adopting actually costs

Measured, not estimated. A throwaway spike flipped the gate to 5 and ran the suite:

| Change | Result |
|---|---|
| Baseline (gate 4, vendored schema 5) | **156 failed** / 3063 passed |
| Gate → 5, version literal threaded through the test files hard-coding `4` | **3 failed** — all version-gate negative tests that used `5` as their stand-in for "an unsupported version" |
| Fixture made genuinely v5-shaped (paired class, unconditional genesis rows) | **0 failed** |
| A presentation column added — the forced `history` superset | **1 failed** (below) |
| A real `slice_only` column added | **0 additional** |

Two findings follow, and they shape this design.

**The product code is additively compatible.** All 156 baseline failures are one root
cause — C1 schema validation against a sidecar declaring the wrong version. The exporters,
derivations, and corrupters read a v5 emit, genesis rows and all, without complaint. That
is the correct and reassuring result for an additive field: forge is not broken by v5,
and *using* v5 is a choice rather than a repair.

**But forge does not only consume the format — it judges it.** `fabulexa-forge validate`
is a product surface, and once C13 exists, forge's own fixtures become invalid inputs to
forge's own validator: they carry `history_tracked` with no paired `temporal_class`
(C13's structural clause), and `refs_dangling` carries a records row with a tracked
property and an empty `history` (C11's converse). The fixture work below is forced by the
checks, not chosen for tidiness.

**One red is real semantic churn**, and the design must absorb it: with `doctor` carrying
a tracked presentation column, deleting a doctor row orphans a `history` series, so the
corrupter's impact oracle adds `C6` and the published recipe ground truth for
`hard-deleted-parents` moves from `impact: [C10]` to `[C6, C10]`. That churn arrives via
the *format*, not via any policy this doc chooses.

## Solution

Adopt v5 as the sole supported version (Principle #9 — the contract is external and
version-gated; `contract/` vendors exactly one version, and forge carries no compatibility
shim for the version it no longer defines). Four moves.

**1. The boundary learns the class.** The sidecar column model carries `temporal_class`
alongside `history_tracked`, and the version gate moves to 5. No inference fallback
exists: deriving a class from `history_tracked` is precisely the fiction v5 deletes, so a
column that declares no class has *no class*, and a surface that needs one refuses rather
than guesses.

**2. Conformance judges v5.** C11 becomes bidirectional — its existing pass iterates
`(kind, property)` drawn *from* `history`; the new converse pass iterates the sidecar's
flagged columns and requires each to have history rows. C13 is new: the structural
attribute-pairing clauses plus the genesis-row clause. Both are implemented faithfully
to the published procedure, skip guards included — with one declared strictness choice:
the genesis clause checks every record where the procedure requires only a sample of ten
(§ *Validation Rules → Conformance*).

**3. The genre trichotomy keys on the class.** v5 makes every presentation column
`history_tracked: true`, so a predicate keyed on that bit would reclassify any kind
carrying a presentation column into change-log genre — including a kind whose "change
log" holds nothing but genesis rows. The trichotomy keys on `temporal_class == "tracked"`
instead: a kind is change-log genre iff something about it genuinely changes.

**4. The next re-vendor is cheap.** One version authority; one place that knows a
column's shape. See § *Re-vendor hardening*.

The known fiction — a mutable-untracked column read as though it were constant — **keeps
running** after this doc lands. It runs today; adopting v5 does not regress it. What
changes is that forge can now *see* it, which is the precondition for refusing it.

## Affected Subsystems

- **reader** — `ColumnSpec` gains `temporal_class`. The version gate moves to 5. The
  sidecar gains a typed per-column class accessor that raises rather than infers. The
  reader **does not** gate on class coverage at open — see *What Doesn't Change*.

- **conformance** — C11 gains its converse clause. C13 is new. The check registry and
  `fabulexa-forge validate` enumerate C1–**C13**.

- **source exporter** — the genre trichotomy's tracked-ness predicate keys on
  `temporal_class == "tracked"` rather than `history_tracked is True`. A kind whose only
  tracked column is a presentation value reclassifies from reference/transaction genre to
  change-log genre — accepted as faithful (a name that genuinely changes over time *is* a
  change log), and shipped as a documented behavior change.

- **derivations** — no contract changes. Row-state-events sees the genesis superset and
  must keep excluding genesis-coincident rows from the update stream (below). The
  `records__` join it retains is unchanged.

- **corrupters** — the base-emit writer must round-trip every column attribute the reader
  models, `temporal_class` included; today it reconstructs column objects from a
  hard-coded attribute set, which would silently strip the class and emit a sidecar
  claiming v5 while violating C13 *by construction and undeclared*. `drop_events`'s
  impact oracle gains an emptied-series clause: a draw that removes a `(kind, property)`
  series' every row breaks C11's converse and declares `C11` by name — C11 is inside the
  manifest's vocabulary, so the sentinel there would be false, not vague. And `C11`
  alone: the co-occurring C13 break cannot sit beside a real code in the exclusive
  vocabulary (§ *Corrupted emits stay structurally conformant*). Published
  recipe ground truth churns where a kind becomes tracked and where a drop empties a
  series.

- **fixtures** — the spanning builders emit v5; new positive and negative variants
  exercise the new checks; the version literal gets a single authority.

## What Doesn't Change

- **The reader does not gate on class coverage at open.** `open_emit` does not refuse an
  emit whose `prop__` columns carry no `temporal_class` — nor one whose declared class is
  outside the enum; the sidecar model carries the declared value verbatim. Refusing at
  open would make `fabulexa-forge validate` unable to *diagnose* the very emit you would
  reach for it with, since validate reads through the reader. The reader reads,
  conformance judges, and the modes refuse what they cannot answer honestly.

- **The version gate stays a single integer.** No dual-version support, no auto-upgrade,
  no inference fallback for an absent class.

- **The defect manifest's impact vocabulary, its schema version, and the sentinel's
  exclusivity.** The sentinel is named `beyond-c1-c12` and it means *"no C1–C12 code
  fired"* — exclusive by construction; the manifest rejects any impact that mixes it
  with a real code. A defect that breaks *only* C13 carries the sentinel — accurate,
  under-informative, not false. A defect that breaks C11 — *inside* the vocabulary —
  declares `C11` by name, never the sentinel; and when the same defect also breaks C13
  (an emptied series does — zero rows implies no genesis row), the impact stays `[C11]`
  alone: the exclusive vocabulary cannot express the co-break, and naming it belongs to
  the deferred sharpening (§ *Corrupted emits stay structurally conformant*). Sharpening
  the C13 labels is deferred (below), and no manifest version bump is warranted here —
  the vocabulary gains no new code.

- **The `history` table's shape, the membership tables, every derivation's contract, the
  anchor surface, the streaming routing/pacing/mixer surfaces, the writers, and the
  incremental driver's window math.** They see more `history` rows; none of their
  contracts change.

- **The genesis-origin question.** v5 guarantees a genesis row exists; it does not mark
  whether that row's value is an *intrinsic birth value* or a *truncated as-of initial
  condition* whose real history predates the run. The contract carries no such marker.
  forge stays silent on the distinction rather than guessing at it.

## Deferred, deliberately

All three are real, all are designed separately (the follow-on design carved from the
full-scope v5 material), and none is a silent omission.

- **The `slice_only` policy.** After this doc, forge can *see* that a column is
  `slice_only` and still exports it everywhere — including into point-in-time folds,
  where it stamps the slice value at a horizon the emit cannot speak to. That fiction
  runs today and this doc does not regress it. Refusing it is a family of decisions
  (omit vs refuse per mode, a notice channel that does not exist yet, narrowing the
  point-in-time folds, `updated_at` for fully-traced kinds) with no existing test
  coverage to guide it. It is greenfield work and gets its own design.

- **Exploiting the genesis guarantee.** The unconditional seed makes an as-of lookup over
  `history` complete — an empty result means exactly `T < created_sim_time` — so the
  point-in-time folds can drop their `records__` fallback and the variable-horizon form
  becomes exact. This doc *judges* the guarantee (C11, C13) and absorbs its rows;
  redesigning the folds to lean on it is the follow-on design's.

- **Naming C13 breaks in the defect manifest.** `insert_rows` writes a `records__` row
  with no `history` rows at all, and `shift_sim_time`'s offset mode can move a series'
  earliest row off `created_sim_time`; both now break C13, as can `drop_events`. C13 is
  outside the manifest's impact vocabulary, so the oracle labels these `beyond-c1-c12`
  when no in-vocabulary code fired alongside — *accurate* (see above) but not precise —
  and leaves the C13 co-break unlabeled when one did, the sentinel being inexpressible
  beside a real code. Sharpening both — and bumping the manifest
  schema version when a label consumers read as the sentinel starts reading as a real
  code — gets its own design. (An emptied series breaking C11 is **not** deferred: C11 is
  inside the vocabulary, and a sentinel there would be false, not vague — see § *Corrupted
  emits stay structurally conformant*.)

## Semantics

### The three classes

| `temporal_class` | Value at horizon T (T ≥ `created_sim_time`) | Modelled as |
|---|---|---|
| `constant` | the current value — exact at every T | read `records__<kind>.prop__<name>` |
| `tracked` | exact | an ordered lookup over `history` |
| `slice_only` | **unknowable** | *(no surface consumes this in this doc)* |

`tracked` implies `history_tracked: true`; `slice_only` implies `history_tracked: false`;
`constant` admits either — a constant column that is also history-tracked holds exactly
its genesis row. A column carries `history_tracked` **iff** it carries `temporal_class`;
the pairing is structural, and `presentation_id` carries neither.

**Never inferred.** A column that declares no class has no class. Any surface needing one
raises `TemporalClassUnavailableError` and directs the caller to `fabulexa-forge validate`.
Deriving a class from `history_tracked` is the fiction v5 exists to delete.

### The genesis guarantee

Every `history_tracked: true` property of every record carries a `history` row at that
record's `created_sim_time` — NULL-valued when the property was absent at creation.

| Condition | v4 meaning | v5 meaning |
|---|---|---|
| No `history` row for `(kind, record, property)` at or before T | record absent, **or** created-NULL-never-changed, **or** value only in `records__` | **exactly** `T < created_sim_time` |
| A `history` row whose `value` is NULL | value genuinely NULL at T | value genuinely NULL at T |
| Zero `history` rows for a flagged column on an extant record | legal (the carve-out) | **C11 violation** |

This doc *judges* the guarantee (C11, C13) and *absorbs* its consequences. Exploiting it —
the as-of join that makes point-in-time reconstruction exact — is deferred
(§ *Deferred, deliberately*).

A NULL-valued genesis row must round-trip through C6 as NULL-against-NULL: the property's
`records__` cell is NULL, and its latest pre-slice `history` value is NULL. This is
already C6's regime (NULL is a legal *history-entry* value — `contract/base-format.md`
§ `history`, the NULL layer-scoping note under the creation-seed guarantee); it simply
had no live instance before v5.

### Consequences of the projected-history superset

At v5 every presentation column carries `history_tracked: true` — class `tracked` when its
bound source is tracked, otherwise `constant` (`contract/base-format.md` § Column
temporal semantics → *Which columns carry the pair*; a presentation column is never
`slice_only`). Together with the unconditional seed, `history`
becomes a deterministic **superset** of what v4 carried. Every consequence is a behavior
change, not a contract change:

| Surface | Consequence |
|---|---|
| Row-state-events | Genesis-coincident rows **must remain excluded** from the update stream, or every record emits a spurious `u` at its own creation instant coincident with its `c`. Presentation-value changes now emit `u` events. |
| Streaming CDC | More events; a record whose only change is a re-minted name now appears on the stream. Faithful. |
| Dimensional SCD-2 | More version rows; presentation values now version. A capability gain. |
| Source genre trichotomy | A kind whose *only* `tracked` column is a presentation value reclassifies from reference/transaction to change-log genre. A kind whose presentation column is class `constant` does **not** — see below. |
| C6 | Presentation columns enter its input set — they now have two representations to round-trip. |
| Corrupter impact | A kind that becomes tracked gains a `history` series, so `delete_rows` on it now orphans that series and the oracle adds `C6`. Published recipe ground truth changes. |

**Invariant relied on:** a `history` row's `property` value always names a
`prop__<property>` column on `records__<kind>`, presentation sub-picks included. The
superset introduces no history property without a corresponding records column.

### Tracked-ness is a property of the class, not of the bit

The genre trichotomy asks *"does this kind change over time?"*. At v4, `history_tracked`
answered it. At v5 it does not: a presentation column bound to an immutable source is
`history_tracked: true` and class `constant`, and it holds **exactly one** `history` row —
its genesis row. A predicate keyed on the bit would classify such a kind as change-log
genre and render it as a change log with no changes.

**A kind is tracked iff any of its `prop__` columns is `temporal_class: "tracked"`.**

| Kind's `prop__` columns | Tracked? | Genre |
|---|---|---|
| No column carries `history_tracked` | no | reference / transaction (by role) |
| Every history-tracked column is class `constant` | no | reference / transaction (by role) |
| Any column is class `tracked` | yes | change-log |
| A history-tracked column declares no class | — | `TemporalClassUnavailableError` |

The first row mirrors C11's and C13's skip guard rather than any legal v5 shape: v5
coverage is total (every value-carrying column carries the pair), so an emit carrying no
`history_tracked` anywhere predates the attributes — and the version gate refuses it
before the predicate ever runs. The guard is retained for the same reason the checks
retain theirs: the predicate is a correct standalone implementation, and a kind with
nothing flagged needs no class to be classified — nothing is tracked. The last row is a
non-conformant emit, and the predicate refuses rather than guessing.

Only a `history_tracked: true` column can be class `tracked` (the contract constrains it),
so the predicate consults the class only for the columns carrying the bit. A `slice_only`
column is `history_tracked: false` and is therefore never consulted here — it flows
through untouched, which is precisely the fiction the deferred `slice_only` policy
exists to refuse (§ *Deferred, deliberately*).

The refusal is one-directional, and deliberately so. The pairing's other half broken — a
column declaring a `temporal_class` with no `history_tracked` — is never consulted: the
predicate classifies the kind from the flagged columns alone and stays silent. That is
the contract-consistent reading (only a flagged column can be `tracked`), not a guess;
the broken pairing is C13's to report, and `validate` names it.

### Re-vendor hardening

A re-vendor has three costs. Only the third deserves thought, and today the first two bury
it.

| Cost | Today | After |
|---|---|---|
| **Version-integer churn** | the literal `4` is hard-coded across the test tree, including several module-level `SUPPORTED_VERSION` redefinitions | one authority; everything imports it |
| **Sidecar-shape churn** | three-plus independent hand-rolled sidecar builders, each separately encoding what a column object looks like — and nothing tells you when you've missed one | one place that knows a column's shape; a new paired attribute is one signature change, and the type checker names every call site |
| **Semantic churn** | buried under the other two | the only thing left to think about |

**Invariant introduced:** *the supported version appears as a literal exactly once.* Every
other site derives from it. A version-gate negative test uses a sentinel that can never
become valid (never a neighbouring real version — three spike failures were tests using
`5` as their example of "unsupported", which quietly became valid on the bump).

**Invariant introduced:** *every fixture sidecar is written through one function, and every
value-carrying column is constructed through one constructor.* The constructor takes the
attribute pair; adding a third attribute changes one signature.

This is a refactor of test infrastructure that already exists, not scaffolding for a
feature that does not (Principle #8). It deliberately does **not** try to absorb semantic
churn: the `hard-deleted-parents` ground-truth move is the system reporting that the
contract changed meaning, and that alarm stays loud.

The model it serves is two-phase, and worth naming because it is how every future bump
should go:

1. **Compatibility.** Bump the gate, run the suite. *Green is the goal* — it means the new
   format is additively compatible and forge is not broken. Every red is a guarantee having
   changed underneath, and each red is a decision.
2. **Adoption.** Decide, deliberately, which new capability to use. Its own design, its own
   sprint.

This doc is phase 1 for v5, plus the conformance work that phase 1 obliges (forge judges
the format; it cannot adopt a version whose guarantees it does not check).

### Corrupted emits stay structurally conformant

**Invariant introduced:** *the base-emit writer round-trips every sidecar column attribute
the reader models — a declared attribute is carried verbatim, an absent attribute stays
absent.* Today the writer reconstructs column objects from a hard-coded attribute list, so
a new attribute is dropped silently. Both halves are load-bearing. A stripped
`temporal_class` would violate structural conformance undeclared — and would produce a
tape forge's own point-in-time surfaces could not read. And an *emitted*
`temporal_class: null` on a structural column fails C1 — the vendored schema types the
attribute and enum-constrains its value, so absence is representable only by omission. A
corrupted emit is structurally conformant by construction (C1–C5, C8, and C13's
structural clauses).

A corrupter may still break C13's *semantic* clause (a dropped genesis row) or C11's
converse (an emptied series). That is a corrupter doing its job — breaking semantic
conformance is its declared purpose. The two breaks are labeled differently, and the
split follows the manifest's vocabulary. C11 is *inside* it: an operation whose draw
empties a `(kind, property)` series — leaves zero `history` rows for a flagged column of
a kind whose `records__<kind>` still has rows; `drop_events` is the operation that can —
declares `C11` in its impact, because the sentinel's meaning ("no C1–C12 code fired")
would be false there, and `validate` would name a failure the ground truth denied. C13 is
*outside* it: a break of C13 alone carries the sentinel, which stays accurate though not
precise. The vocabulary is exclusive — the manifest rejects an impact that mixes the
sentinel with a real code — so the emptied series, which breaks both (zero rows implies
no genesis row), declares `[C11]` alone; its C13 co-break is unlabeled, not mislabeled,
and naming it belongs to the deferred sharpening (§ *Deferred, deliberately*).

## Configuration

No new author-facing config surface, and no new knob. `temporal_class` is a property of the
*emit*, never of a config.

The observable changes are: a stricter `validate` (two more checks), a genre
reclassification for kinds whose only tracked column is a presentation value, and a
`TemporalClassUnavailableError` at plan time on a non-conformant emit.

```
TemporalClassUnavailableError: records__patient.prop__triage_band declares
history_tracked but no temporal_class; the emit is non-conformant (C13).
Run `fabulexa-forge validate`.
```

## Interface Contracts

### Runtime Types

```python
TemporalClass = Literal["constant", "tracked", "slice_only"]
"""The point-in-time contract for one value-carrying column, read from the sidecar.

Never inferred: a column that declares no class has no class, and a surface that
needs one refuses rather than deriving it from history_tracked.
"""


@dataclass(frozen=True)
class ColumnSpec:
    """One column of a base-layer table, as declared in base.json."""

    name: str
    type: str
    references: str | None
    history_tracked: bool | None
    temporal_class: str | None
```

`ColumnSpec.temporal_class` is `str | None`, not `TemporalClass | None`, deliberately: the
sidecar's declared value is carried **verbatim**, neither validated nor coerced at parse.
The reader reads, conformance judges — C13's enum clause must be able to *see* an
out-of-enum declared value, and `validate` reads through the reader, so a parse-time
rejection (or a coerce-to-`None`) would hide the very defect `validate` exists to report.
The narrowing to `TemporalClass` happens in exactly one place: the sidecar's
`temporal_class` accessor (below).

### Reader

A method on `Sidecar` — the reader subsystem's typed sidecar model, and the same object
`is_kind_tracked` receives — so any surface holding a sidecar resolves a class without an
open connection:

```python
def temporal_class(self, table_name: str, column_name: str) -> TemporalClass:
    """The declared point-in-time class of one value-carrying column.

    The single point where the sidecar's verbatim declared value narrows to a
    TemporalClass; every surface that needs a class (the genre predicate) resolves
    through it.

    Args:
        table_name: DuckDB table name.
        column_name: Column name, including its prop__ prefix.

    Returns:
        The column's declared TemporalClass.

    Raises:
        TableNotFoundError: No table named `table_name` is declared.
        ColumnNotFoundError: The table declares no column named `column_name`.
        TemporalClassUnavailableError: The column has no usable class. Three cases,
            distinguished in the message: the column carries neither temporal
            attribute (a structural, identity, or membership column — conformant;
            it has no temporal semantics to ask about); it declares history_tracked
            but no temporal_class (non-conformant, C13); or it declares a value
            outside the three-class enum (non-conformant — C13's enum clause, and
            C1, since the vendored schema enum-constrains the value). The
            non-conformant messages direct the caller to `fabulexa-forge validate`.
            No class is ever inferred.
    """
```

### Source — the genre predicate

```python
def is_kind_tracked(sidecar: Sidecar, table_name: str) -> bool:
    """Whether any property of `table_name`'s kind genuinely changes over time.

    A kind is tracked iff one of its prop__ columns is temporal_class 'tracked'.
    Keyed on the class, not on history_tracked: at v5 every presentation column is
    history_tracked, but one bound to an immutable source is class 'constant' and
    holds exactly its genesis row — a kind carrying only such a column does not
    change, and rendering it as a change log would render a change log with no
    changes.

    Only a history_tracked column can be class 'tracked' (the contract constrains
    it), so the class is consulted only for the columns carrying the bit — resolved
    through the sidecar's temporal_class accessor, the single narrowing point. A
    kind carrying no history_tracked prop__ column is untracked without consulting
    any class — the same defensive skip signal C11 and C13 key on, unreachable past
    the version gate against a producer-written v5 emit (coverage is total) and
    retained so the predicate is correct standalone.

    Args:
        sidecar: The open emit's sidecar.
        table_name: The records-category table name.

    Returns:
        True iff some prop__ column of the kind is temporal_class 'tracked'.

    Raises:
        TableNotFoundError: `table_name` is not in the sidecar.
        TemporalClassUnavailableError: A prop__ column declares history_tracked but
            no temporal_class, or declares a class outside the enum. The emit is
            non-conformant (C13); no class is inferred.
    """
```

### Conformance

```python
def _check_c11(emit: Emit) -> CheckResult:
    """C11 — column SCD class consistency, bidirectional at v5.

    Skips when no records-category prop__ column carries history_tracked (the
    published additive-field guard).

    Forward clause (existing): for each distinct (kind, property) in history, the
    prop__<property> column on records__<kind> is present in the sidecar and flagged
    history_tracked true.

    Converse clause (new): for each records__<kind> with at least one row, each
    prop__<property> column flagged history_tracked true has at least one history row
    for (kind, property). Zero rows is a violation — the unconditional creation seed
    removed the "created NULL, never changed" carve-out that made it legal at v4.

    Collection-struct properties emit membership tables, not history rows, and stay
    outside C11's input set — excluded by the same gate shipped C6 uses: the converse
    clause consults only flagged prop__ columns whose declared type is in the
    round-trippable set (BIGINT, DOUBLE, BOOLEAN, VARCHAR). The forward clause needs
    no gate — a collection property never appears in history.

    Args:
        emit: The open emit.

    Returns:
        A CheckResult for check id "C11".
    """


def _check_c13(emit: Emit) -> CheckResult:
    """C13 — temporal-class consistency.

    Skips when no records-category prop__ column carries history_tracked (the
    published additive-field guard; unreachable against a producer-written v5 emit,
    retained so the checker is a correct standalone implementation of the procedure).

    Structural clauses, over every records-category prop__ column: history_tracked is
    present iff temporal_class is present; a present temporal_class is one of the
    three declared values; 'tracked' implies history_tracked true; 'slice_only'
    implies history_tracked false.

    Semantic clause, over every prop__ column flagged history_tracked true (any
    class), for every record of that kind: history carries a row for
    (kind, record_id, property) at that record's own created_sim_time — the genesis
    row. record_id is part of the match: a rowless record does not pass because a
    sibling of the same kind shares its created_sim_time. (The published
    pseudocode's "(kind, property)" shorthand is scoped inside its per-record loop;
    the tuple here names the full key.)
    The published procedure requires a sample of up to ten records; forge checks
    every record — the same strictness choice its C6 makes (the contract:
    "exhaustive checking is the consumer's choice"), and the exhaustive pass
    needs no sample-selection rule, keeping validate deterministic.

    Collection-struct properties stay outside the semantic clause's input set —
    excluded by the same round-trippable-type gate shipped C6 uses (BIGINT, DOUBLE,
    BOOLEAN, VARCHAR); their changes emit membership tables, not history rows. The
    structural clauses have no history side and run ungated, over every
    records-category prop__ column.

    Args:
        emit: The open emit.

    Returns:
        A CheckResult for check id "C13".
    """
```

### Errors

```python
class TemporalClassUnavailableError(ReaderError):
    """A column whose point-in-time class is required has no usable one.

    Raised for a column carrying neither temporal attribute (a structural or
    identity column — conformant, but it has no temporal semantics to ask about),
    for a declared history_tracked with no paired temporal_class, and for a
    declared class outside the three-value enum (both non-conformant, C13). The
    message distinguishes the cases; the non-conformant ones direct the caller to
    `fabulexa-forge validate`. Raised rather than inferring a class from
    history_tracked: that inference is the fiction base_format_version 5 exists
    to delete.
    """


class ColumnNotFoundError(ReaderError):
    """A named column is not declared on the named table."""
```

### Fixture support

```python
def prop_column(
    name: str,
    type: str,
    *,
    history_tracked: bool,
    temporal_class: TemporalClass,
    references: str | None = None,
) -> dict[str, object]:
    """Build one value-carrying sidecar column.

    The sole constructor for a prop__ column across every fixture builder. Both
    temporal attributes are required and passed together, because the contract pairs
    them: a column carries history_tracked iff it carries temporal_class. A future
    paired attribute changes this one signature, and every call site with it.

    The constructor builds only conformant columns — temporal_class is typed to the
    enum, and the contract's implication clauses are validated: 'tracked' requires
    history_tracked True, 'slice_only' requires history_tracked False. A negative
    variant that breaks the pairing, the enum, or an implication mutates the returned
    dict; a defect is never expressible through the constructor.

    Raises:
        ValueError: temporal_class 'tracked' with history_tracked False, or
            'slice_only' with history_tracked True.
    """


def write_emit(
    dest: Path,
    *,
    tables: list[dict[str, object]],
    branches: list[dict[str, object]] | None = None,
    extra: dict[str, object] | None = None,
    base_format_version: int | None = None,
    schema_valid: bool = True,
) -> None:
    """Write one fixture emit's base.json.

    The sole writer of a fixture sidecar — the wrong-version negative fixture
    included, via the override below; no fixture writes or rewrites a sidecar by
    hand.

    Args:
        dest: The emit directory; base.json is written inside it.
        tables: The sidecar's tables list; value-carrying columns built via
            prop_column.
        branches: The branches list. Defaults to the single-trunk entry.
        extra: Optional top-level sidecar blocks (runtime, pinned_ids, enum_domains,
            record_roles), carried verbatim.
        base_format_version: The version to stamp. None (the default) stamps
            SUPPORTED_BASE_FORMAT_VERSION — the supported version appears as a
            literal nowhere in the test tree. An explicit value exists for the
            version-gate negative fixture alone, which passes the never-valid
            sentinel (§ Re-vendor hardening), composed with schema_valid=False:
            the vendored schema pins the version, so any override is
            schema-invalid by construction.
        schema_valid: When True (the default), validate the result against the
            vendored contract/base-format.schema.json before writing, so a fixture
            that has not learned a new required field fails at construction, naming
            the field, rather than surfacing as an unrelated C1 failure at read
            time. False is reserved for negative fixtures whose declared defect is
            schema-level (a wrong version, an out-of-enum class) — they must remain
            writable, and their expectations name the C1 failure.
    """
```

## Validation Rules

### Parse-Time (Pydantic)

None. `temporal_class` is a property of the *emit*, not of any config model, and the config
models cannot see a sidecar at parse time.

### Business Rules

| Rule | Checks | Error |
|---|---|---|
| Version gate | `base.json`'s `base_format_version` equals `SUPPORTED_BASE_FORMAT_VERSION` | `UnsupportedBaseFormatVersionError`, carrying the found version. No auto-upgrade. |
| Class is declared where it is needed | A `prop__` column the genre predicate inspects (i.e. one flagged `history_tracked`) declares a `temporal_class` within the three-value enum | `TemporalClassUnavailableError` — `"records__{kind}.{col} declares history_tracked but no temporal_class; the emit is non-conformant (C13). Run \`fabulexa-forge validate\`."` (an out-of-enum declared value raises the same error, its message naming the value) |

Resolved at plan time, against the open emit's sidecar, before any data read.

### Conformance

C11 gains its converse clause; C13 is new. Both follow the published procedure
(`contract/base-format.md` § C11, § C13), skip guards included, with one declared
strictness choice: C13's genesis clause checks every record where the procedure samples
up to ten — the same exhaustive choice forge's shipped C6 makes (the contract permits it:
"exhaustive checking is the consumer's choice"), and deterministic by construction where
a sample would need a selection rule. The check registry and `fabulexa-forge validate`
enumerate C1–C13.

## Fixtures

Named by what they exercise, never by format version (the existing convention).

**Forced by the new checks** — the current fixtures are not v5 emits:

- Every value-carrying `prop__` column gains the attribute pair — not only the flagged
  ones: v5 coverage is total (C13's structural clause guards the *pairing*; the
  contract's coverage rule puts the pair on every value-carrying column). A column
  carrying neither attribute today gets `history_tracked: false` plus the class the
  fixture author assigns it — `constant` for a value fixed at creation, `slice_only`
  for a mutable-untracked one — all built through `prop_column`, which requires both.
- Every history-tracked property of every record gains its genesis row at
  `created_sim_time`, including NULL-valued rows for properties absent at creation, which
  must round-trip through C6 as NULL-against-NULL.
- `refs_dangling` — a records row with a tracked property and an empty `history` — gains a
  genesis row, or it fails C11's converse for a reason unrelated to the defect it exists
  to exercise. Every negative fixture gets the same audit: a fixture must fail the check it
  is named for, and no other — except a coupling the contract itself forces, which the
  fixture's expectation then names in full (below).

**New coverage** for what v5 introduces:

- A **presentation column** on an otherwise-untracked kind — the fixtures carry none today,
  so the projected-history superset (the genre reclassification, the extra SCD-2 rows, the
  extra `u` events) is currently invisible to the suite. Both classes are represented: one
  bound to a tracked source (class `tracked` — flips its kind's genre) and one bound to an
  immutable source (class `constant` — does not).
- A `slice_only` column, so the class is represented in the sidecar and the reader's
  accessor is exercised across all three values. Nothing consumes it in this doc.
- Negative variants for the new checks: a broken attribute pairing (fails C13's structural
  clause alone — the vendored schema does not enforce the pairing), an out-of-enum class
  (fails C13's enum clause **and necessarily C1** — the schema enum-constrains the value;
  the fixture's expectation names both), a missing genesis row with later rows intact
  (fails C13's semantic clause alone — C11's converse still sees rows), and an emptied
  `(kind, property)` series (fails C11's converse **and** C13's genesis clause — zero rows
  implies no genesis row; the expectation names both).

**Recipe ground truth.** `hard-deleted-parents` moves from `impact: [C10]` to `[C6, C10]`
where a kind becomes tracked, and any `drop_events` recipe whose seeded draw empties a
`(kind, property)` series adds `C11`. Both are forced by the format and are the places
the bump changes a *published* artifact.
