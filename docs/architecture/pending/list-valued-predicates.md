---
status: draft
---

# List-valued predicates

Every equality predicate in the dimensional export grammar accepts either a
scalar or a non-empty list. A scalar compiles to `=`; a list compiles to `IN`.

## Problem

The dimensional exporter's row predicates are single-value equality, on every
surface that has one. A table selects its rows with `filter: {prop__decision_type:
ed_arrival}` — one column, one value. There is no way to express "one of these
values".

This makes a class of domain-shaped output unbuildable rather than merely
awkward. A records kind whose rows span several domain processes — distinguished
by a discriminator column that is not a sub-type — cannot be projected into one
table per process. In the NHS bundle, `records__tick_decision` carries 30
distinct `prop__decision_type` values across nine clinical processes, and an NHS
warehouse ships those processes as separate named datasets (Emergency Care,
Admitted Patient Care, Diagnostics, Cancer Waiting Times), each grouping several
decision types.

Today the author has two expressible options, and neither is the domain's shape:

```yaml
    # Option 1 — one table per discriminator value. Thirty near-identical blocks.
    - name: fact_ed_arrival
      source: {grain: records, kind: tick_decision, filter: {prop__decision_type: ed_arrival}}
    - name: fact_triage
      source: {grain: records, kind: tick_decision, filter: {prop__decision_type: triage}}
    # ... 28 more

    # Option 2 — one undifferentiated table; the consumer slices it themselves.
    - name: fact_clinical_event
      source: {grain: records, kind: tick_decision}
```

The same single-value limit applies to the membership-grain `where`, the
membership foreign key's `where`, the derived-elapsed `other_where`, and the
history-point `value`. All four hit it the same way and for the same reason.

Nothing about temporal honesty is at stake in the motivating case: the
discriminator is `temporal_class: constant`. The limit is the grammar's
expressiveness alone.

## Solution

One rule, applied uniformly across the grammar:

> **A predicate value is either a scalar or a non-empty list. A scalar compiles
> to `= <literal>`; a list compiles to `IN (<literal>, …)`. Predicates over
> distinct columns remain AND-joined.**

Five surfaces carry a predicate today, and all five take the rule:

| Surface | Selects | Predicate columns |
|---|---|---|
| `SourceDecl.filter` | records-grain rows | any records column of the kind |
| `SourceDecl.where` | membership-grain rows | membership element columns |
| `SourceDecl.value` | history-point rows | the `history.value` column |
| `FkClause.where` | the membership interval a foreign key resolves through, when the declaring table's own grain is not already that membership (§ What Doesn't Change) | membership element columns |
| `ElapsedSpec.other_where` | the counterpart row of a derived-elapsed correlation | columns of the correlation's source table |

Widening all five is a smaller contract to state and to remember than widening
one. "Predicates take a value or a list" has no exceptions an author must look
up; "`filter` takes a list but `where` does not" is a rule that has to be
consulted every time.

On one surface the predicate does double duty: a dim's conjunct on the
sub-typed discriminator also selects the dim's **source population set**, and
its value set widens that selection the same way — a list selects exactly those
populations (§ The dim source population set).

The end state, in the motivating config:

```yaml
    - name: fact_emergency_care
      role: fact
      source:
        grain: records
        kind: tick_decision
        filter:
          prop__decision_type: [ed_arrival, triage, ed_assessment, ed_diagnosis]
      key: [ed_event_id]
      columns:
        - {name: ed_event_id,   from: record_id}
        - {name: attendance_id, from: prop__journey_instance}
        - {name: patient_id,    fk: {to: dim_patient, via: reference}}
        - {name: milestone,     from: prop__decision_type}
        - {name: recorded_at,   derived: {timestamp: {source: created_sim_time}}}
```

Rendering is owned in one place. The package's shared SQL-string utilities —
already the home of the one identifier-quoting helper and the one typed-literal
renderer, and already imported by the reader, the derivations layer, the
exporters, and the corrupters — gain a single public contract that turns a column
plus a scalar-or-list into one SQL condition. Every predicate surface routes
through it, and the two private literal-renderer forks that exist today are
deleted (§ Consolidating the literal renderers). There is no second
implementation of the scalar/list rule.

## Affected Subsystems

- **The config envelope.** Five field types widen from a scalar value to a
  scalar-or-list value. One shared parse-time rule rejects empty and
  duplicate-bearing lists; one model-level rule closes a pre-existing hole on
  the grammar's sole required predicate mapping (`other_where` must be
  non-empty — § Parse-Time). No new field, no new model, no change to which
  grains accept which predicate.

- **The shared SQL utilities.** They gain the one predicate-condition contract and
  become the sole authority for compiling a config predicate value into SQL. The
  two private typed-literal forks — one in the reader's relation builders, one in
  the derivations layer's reference resolution — are deleted in favor of the
  shared renderer they were copied from.

- **The reader.** Its faithful-read SQL builders are the sole faithful namer of
  base tables; they keep resolving each predicate column's type from the sidecar
  and now delegate the condition itself. The records, history, and membership
  relation builders each take a scalar-or-list predicate value. Widening a
  parameter type is safe for every existing caller — the base, source,
  dimensional, election, and versioned-intervals callers that pass scalar-only
  mappings are unaffected and keep producing byte-identical SQL for every
  column type the contract's recommended mapping produces (the renderer
  consolidation's refusal cases are § Consolidating the literal renderers).

- **The derivations layer.** The reference-resolution resident's membership-edge
  relation takes a predicate for the same reason its reader counterpart does, and
  renders through the same authority. The versioned-intervals relation forwards a
  widened records predicate to the reader builder it composes and renders nothing
  itself. The layer stays pure and anti-weld: it receives plain values, not config
  objects.

- **The dimensional exporter.** Its records, history-point, and membership grain
  relations, its membership foreign-key resolution, and its derived-elapsed
  correlation each pass a predicate through rather than render it. (The
  history-interval grain carries no predicate — the grain gate rejects all three
  there, and it composes the versioned-intervals derivation with an empty one.)
  The elapsed correlation and the point-in-time membership foreign key render
  their own conditions today and stop doing so. Two of its validation rules become
  per-element: the unknowable-past refusal over predicate keys (unchanged — the
  gate is per column, and a list changes no column), and the
  unobserved-discriminator-value notice, which now evaluates each element and
  carries a different consequence on one branch (below).

- **The dimensional election surface.** The dim source population resolver
  widens from "one sub-type or all" to "the selected subset": a discriminator
  conjunct's value set selects the dim's source population set, and its
  declared-domain refusal becomes per-element (§ The dim source population
  set). Its consumers — foreign-key surface inheritance, the edge union-safety
  gate, the identity-relation restriction spine, the guard's dim-side leg, and
  the dim-key agreement check — are already subset-general (the unfiltered
  sub-typed dim carries the full multi-population domain today) and keep their
  rules unchanged.

## What Doesn't Change

- **Sub-type selection.** Source mode's `sub_types` and streaming's `types` are
  already multi-valued and stay exactly as they are, including source's own `IN`
  rendering. They carry a guarantee a general predicate cannot inherit: the
  sub-typed discriminator is the one column exempt from the rule that no exported
  row's membership may derive from a column whose past is unknowable. Folding them
  into the general predicate grammar would leak that exemption to columns that
  must not have it.

- **Base and source gain no row predicate.** Neither mode has one today, and this
  design does not give them one. That is a separable feature, deferred
  deliberately (see § Rationale for the deferral).

- **The operator set.** Equality and set membership only. No ranges, no negation,
  no null-tests, no pattern matching. A future `not_in` is not scaffolded for.

- **Predicate composition.** Entries over distinct columns stay AND-joined. There
  is no disjunction across columns, and no nesting.

- **Which grain accepts which predicate.** `filter` stays records-only, `where`
  stays membership-only, `value` stays history-point-only. The grain gate is
  untouched.

- **Determinism, ordering, and output shape.** Row order, key columns, and table
  emission are unaffected. A grain that matches zero rows still emits an empty
  typed table.

- **`init`.** It proposes sub-type splits, which are genuine one-value splits; it
  continues to propose scalar predicates only.

- **The corrupter config's own selector grammar.** Its row selector is a separate
  envelope with a separate purpose: it names rows to *damage*, and the operation's
  declared impact is computed from what it matched. Widening it is a change to
  what a corrupter promises about its own blast radius, not to how an export
  reshapes. Deferred deliberately; it would inherit the shared mechanism free
  (§ Rationale).

- **A membership-grain `fk.where` stays inert.** When a table's own grain is
  `membership`, a `via: membership` foreign key reads the member id off the grain
  row itself — there is no separate edge relation to narrow, so the clause has no
  meaning there and is accepted-and-ignored today. This design does not change
  that: a list is ignored exactly as a scalar is. Refusing the clause outright is
  the Principle #7-correct end state and is tracked separately; it is a breaking
  config change that wants its own decision, not a rider on this one.

- **Streaming, writers, and the mixer.** None of them reads these fields.

- **Playback.** Tier 1 reads none of these fields. Tier-2 shaped playback
  compiles a declared `ExportConfig` through the dimensional compile surface, so
  a list-valued predicate transits it — the same compiled SQL, with no
  playback-side change; it is a consumer of the widening, not a surface of it.

- **Incremental.** Of the five surfaces only the records `filter` reaches it at
  all — membership and history-interval grains, `derived: elapsed`, and
  `via: membership` foreign keys are each already refused under a window. The one
  rule that reads `filter` gates the discriminator *column*'s mutability and never
  looks at the value, so a list is inert there.

## Semantics

### Rendering

| Predicate value | Rendered condition |
|---|---|
| Scalar `v` | `"col" = <literal(v)>` — byte-identical to today* |
| List `[a]` (one element) | `"col" IN (<literal(a)>)` |
| List `[a, b, c]` | `"col" IN (<literal(a)>, <literal(b)>, <literal(c)>)` |
| Multiple columns | Conditions AND-joined, in the config's key order — unchanged |

\* For every column type the contract's recommended mapping produces; the
deliberate exceptions are in § Consolidating the literal renderers.

Element order is preserved verbatim in the rendered `IN` list. Two configs
differing only in element order therefore render different SQL text but select
identical row sets and carry identical `ORDER BY` clauses, so their output is
identical. Determinism is a property of the output, and it holds.

### Literal typing

Each element is typed independently, by the same rule that types a scalar today:
cast to the predicate column's DuckDB type as declared by the sidecar. Resolving
that type stays with the relation builder that knows the table; the authority
receives it as an argument.

The history-point `value` predicate is **not** an exception to that rule — it is
the rule invoked with `sql_type: VARCHAR`. `history.value` is VARCHAR by contract,
and the shared renderer emits, for VARCHAR, exactly the single-quoted escaped
literal the history builder renders today. So the scalar case stays byte-identical
and a list renders `IN` over those same literals. The raw-literal behavior is a
*caller's* type choice, not a mode of the renderer: this surface needs no bypass,
no `raw` flag, and no second function.

### Consolidating the literal renderers

Three typed-literal renderers exist today: the shared one, and private forks in
the reader's relation builders and in the derivations layer's reference
resolution — each copied to respect a layer-direction rule (the derivations
fork so that layer never imports the exporters; the reader fork additionally so
the reader carried no `ExportError` dependency). They diverge from the shared
renderer in two ways that matter.

The reader fork **falls back to a VARCHAR literal** on a type it does not
recognize, where the shared renderer — and the derivations fork, which already
matches the shared renderer here — refuses naming the type. And both forks
admit a parameterized type by **unanchored prefix**, where the shared renderer
matches an anchored grammar written precisely to stop a sidecar-supplied type
string from closing the `CAST` and appending SQL.

The rendering authority therefore composes the *shared* renderer, and the two
forks are deleted. For every column type the contract's recommended mapping
produces — `VARCHAR`, `BIGINT`, `DOUBLE`, `BOOLEAN`, and `DECIMAL(p,s)` — this is
byte-identical. Two behaviors change, both deliberately, both toward the
package's stated posture that the unverifiable is refused rather than inferred:

- On the reader-composed surfaces, a predicate on a `BLOB` column, or on a
  producer-custom array or struct column, moves from a silently rendered VARCHAR
  literal — a comparison that quietly matches nothing — to a refusal naming the
  type. (The derivations-composed membership-edge surface refuses such a type
  already.)
- On every surface, a parameterized type string that passes the forks' prefix
  test but fails the anchored grammar — arguments that are not plain digits, or
  trailing text after the closing paren — moves from rendered to refused. The
  anchored grammar exists to keep a sidecar-supplied type string inside the
  `CAST`; the forks were the two places it did not reach.

Routing the reader's builders through the shared renderer gives the reader a
failure mode it did not have: an unrecognized predicate-column type now raises
`ExportError` (§ Functions lists it per builder). That is the deliberate end of
the reader fork's fallback, not an accident of consolidation.

The authority also quotes the predicate column through the shared identifier
helper instead of splicing it raw: byte-identical for every real column name,
correct for one carrying a quote.

### The unknowable-past gate

Unchanged. The gate's population is the `prop__` columns of `records__<kind>`; its
predicate returns false on any other name without reading a class at all. Two
predicate surfaces fall inside that population — records `filter` keys, through
the filter-key refusal, and the derived-elapsed `other_where` keys, through the
sibling value-read check — and on both, a non-exempt key whose past is
unknowable is refused exactly as today. Membership element predicates
(`SourceDecl.where`, `FkClause.where`) and the history-point `value` are outside
it by construction: `elem__` and `history` columns carry no `temporal_class`.

Across every surface the gate does cover, the refusal is evaluated per predicate
*column* and a list changes no column, so the value's form is irrelevant. The
sub-typed discriminator remains exempt in both forms.

### The unobserved-value notice

The notice that fires when a records predicate names a value the emit never
observed becomes per-element. Its notice code (`discriminator-value-unobserved`)
is unchanged; the message changes wording only on the one branch where the old
message would be false.

| Condition | Notices | Message |
|---|---|---|
| Scalar observed, or every list element observed | None | — |
| Scalar unobserved | One | `… table will be empty` — verbatim as today |
| List, no element observed | One per element | `… table will be empty` |
| List, some elements unobserved | One per unobserved element | `… it contributes no rows` — the table is **not** empty |
| Column absent from the observed-value set registry | None, any form | — |

The existing message asserts the table will be empty. That stays true for a scalar
and for a wholly-unobserved list, so both keep it verbatim; only the
partially-observed list, where the observed elements still contribute rows, takes
the weaker per-element wording. The unobserved set is therefore computed before
any notice is emitted, and notices follow the config's element order. It remains a
notice, never an error — a declared-but-unobserved value is a legitimate way to
write a config against a family of emits.

The observed-value set is the sidecar's `enum_domains` registry, and a property
absent from it carries none. `prop__decision_type` is exactly such a property — a
modelling discriminator, not a sub-type tag — so **the motivating config triggers
no notice at all**. The per-element rule governs discriminators that do carry an
observed set, such as a sub-typed kind's `prop__<kind>_type`.

### The dim source population set

The population set exists to keep foreign-key output closed over its target: an
`fk` edge joins the destination dim's population-restricted identity relation,
and an owner outside the dim's source population set resolves to NULL rather
than to a key the dim does not contain. That closure is why the set must be
exactly what the filter selects — a set wider than the dim's rows would let a
fact render a key value its dim excludes, a dangling reference.

For a dim on a sub-typed kind, the discriminator conjunct's **value set** — a
scalar's singleton, or a list's elements — selects exactly those populations as
the dim's source population set. Selection is by value set where rendering is
by form: a one-element list still renders `IN`, and selects the same population
set as the scalar. The two reads of the conjunct never disagree on rows.

- **Per-element domain gate.** Every element must be in the kind's declared
  sub-type domain; the existing refusal — a population that cannot exist fails
  loudly rather than resolving to an empty set (Principle #7) — is evaluated
  per element and names the offending element.
- **Proper subset is strict set inclusion.** A restriction spine is composed
  exactly when the selected set is a strict subset of the declared domain. A
  list naming the full domain, in any order, composes no restriction —
  identical to omitting the conjunct.
- **Population order is config element order**, so the row predicate's `IN` and
  the restriction spine's `IN` render from the same list in the same order.
- **Consumers keep their rules.** Foreign-key surface inheritance ranges over
  the selected set and refuses a set electing differing surfaces with the
  existing ambiguity error, whose remedies (filter to a single sub-type, unify
  the election, set an explicit `target_key`) all remain valid; the edge
  union-safety gate, the identity-relation restriction spine, the guard's
  dim-side leg, and the dim-key agreement check all take the selected set
  through their unchanged contracts. Only the set's cardinality range changes —
  from "one sub-type or the full domain" to "any non-empty declared subset".

Further conjuncts narrow rows within the selected set, never the set — the
existing rule, unchanged. On a kind that is not sub-typed, a
discriminator-named conjunct remains an ordinary column conjunct in both forms.

### Membership fan-out

The membership-edge relation is not fan-out-free today: with no narrowing
predicate, an owner bound to several members yields several rows. A `where` is
the author's instrument for narrowing, and a list-valued `where` sits between a
scalar and an absent predicate on that existing spectrum — it can admit more
intervals per owner than a scalar did, and therefore fan out where a scalar did
not.

This is a pre-existing property of the relation, unchanged in kind, and it stays
the author's responsibility exactly as omitting `where` entirely does today. The
engine neither deduplicates nor refuses; doing either would silently discard rows
that faithfully trace to base-layer values.

### Derived-elapsed correlation

The counterpart subquery selects the earliest interval-start among rows matching
`other_where`, grouped by the correlation key. That "earliest wins" rule already
governs the multi-row case today and is unchanged. A list widens the set of rows
considered for each correlation key; the earliest matching start is still the one
correlated.

### Invariants relied upon

- Predicate literal typing reads the column's DuckDB type from the sidecar; no
  column type is hard-coded.
- `history.value` is VARCHAR for every supported contract version.
- Every column type a records or membership predicate can target is one the shared
  typed-literal renderer already recognizes — the contract's recommended mapping
  yields `VARCHAR`, `BIGINT`, `DOUBLE`, and `BOOLEAN` for scalar properties.
- The membership-edge relation is not fan-out-free, and authors narrow it.
- The unknowable-past refusal is evaluated per column, not per value, and its
  population is the `prop__` columns of a records table.
- The FK out-of-set condition: an owner outside the destination dim's source
  population set resolves to NULL, never to a key the dim does not contain. The
  population-set widening preserves this by selecting exactly the filtered
  populations (§ The dim source population set).
- The election consumers — surface inheritance, union safety, the restriction
  spine, the guard's dim-side leg, dim-key agreement — already operate over
  multi-population sets (the unfiltered sub-typed dim's full domain).

### Invariants introduced

- **One rendering authority for config row predicates.** Every condition compiled
  from a config predicate value — `filter`, `where`, `value`, `fk.where`,
  `other_where` — is rendered by the one shared contract, in the reader, the
  derivations layer, and the dimensional exporter alike. No surface renders `=` or
  `IN` over a config predicate itself. The invariant is scoped to config
  predicates: engine-internal scoping conditions (`fork_path`, `kind`, `property`)
  keep their existing raw-literal rendering, and sub-type population filters and
  semi-join `IN (SELECT …)` constructs are not config row predicates and are
  untouched.
- **One well-formedness rule, carried by the type.** The non-empty and
  no-duplicates rule is attached to the predicate-value type, not to the models
  that use it, so every field declared with that type carries it and a future
  predicate field inherits it without wiring.
- **A predicate value is never empty.** An empty list is rejected at parse time,
  so no surface renders a degenerate `IN ()`. The grammar's one *required*
  predicate mapping (`other_where`) is likewise never empty, so no surface
  renders a conditionless correlation either.
- **The scalar path is byte-identical**, for every column type the contract's
  recommended mapping produces. Any config valid before this change renders the
  same SQL after it, with the deliberate exceptions in § Consolidating the
  literal renderers.

## Configuration

```yaml
mode: dimensional

dimensional:
  tables:
    # A list on a records filter — several discriminator values as one table.
    - name: fact_emergency_care
      role: fact
      source:
        grain: records
        kind: tick_decision
        filter:
          prop__decision_type: [ed_arrival, triage, ed_assessment, ed_diagnosis]
      key: [ed_event_id]
      columns:
        - {name: ed_event_id, from: record_id}
        - {name: milestone,   from: prop__decision_type}

    # A scalar predicate — unchanged, and still the right form for a true split.
    - name: dim_ward
      role: dim
      scd: type1
      source:
        grain: records
        kind: entity
        filter: {prop__entity_type: ward}
      key: [ward_id]
      columns:
        - {name: ward_id, from: record_id}

    # A list on a sub-typed dim's discriminator — the dim's source population
    # set is exactly the selected subset (§ The dim source population set).
    - name: dim_clinical_staff
      role: dim
      scd: type1
      source:
        grain: records
        kind: staff
        filter:
          prop__staff_type: [consultant, registrar, nurse]
      key: [staff_id]
      columns:
        - {name: staff_id, from: record_id}

    # A list on a membership grain, and on a membership foreign key's where.
    - name: fact_clinical_supply
      role: fact
      source:
        grain: membership
        kind: tick_decision
        property: bindings
        where:
          elem__role_name: [dx_test, admitted_meds]
      key: [decision_id, item_id]
      columns:
        - {name: decision_id, from: record_id}
        - {name: item_id,     from: member__entity__id}
        - {name: role_name,   from: elem__role_name}

    # Mixed scalar and list across columns — AND-joined, as always.
    - name: fact_urgent_surgical_activity
      role: fact
      source:
        grain: records
        kind: tick_decision
        filter:
          prop__decision_type: [surgeon_assigned, pre_op_assessment, surgery_performed]
          prop__context: urgent
      key: [event_id]
      columns:
        - {name: event_id, from: record_id}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `source.filter` | `dict[str, str \| list[str]]` | No | Records-grain row predicate; each value a scalar or non-empty list |
| `source.where` | `dict[str, str \| list[str]]` | No | Membership-grain row predicate over element columns |
| `source.value` | `str \| list[str]` | No | History-point predicate over `history.value` |
| `fk.where` | `dict[str, str \| list[str]]` | No | Membership-interval predicate on a `via: membership` foreign key |
| `derived.elapsed.other_where` | `dict[str, str \| list[str]]` | Yes | Counterpart-row predicate for the elapsed correlation |

## Interface Contracts

### Config Models

```python
def _reject_malformed_predicate(value: str | list[str]) -> str | list[str]:
    """Reject the two malformed list shapes; say nothing about the scalar form.

    Args:
        value: A parsed predicate value.

    Returns:
        The value unchanged.

    Raises:
        ValueError: the value is an empty list, or a list containing a repeated
            element (the message names the repeated element).
    """


PredicateValue: TypeAlias = Annotated[
    str | list[str], AfterValidator(_reject_malformed_predicate)
]
"""One predicate's required value: a scalar (compiles to `=`) or a non-empty,
duplicate-free list of alternatives (compiles to `IN`).

The well-formedness rule rides the type, not the models. Every field declared
`PredicateValue` carries it — including as the value type of a
`dict[str, PredicateValue]`, where it applies per entry — so the three
predicate-bearing models need no shared validator, the failure is reported at the
offending field's path rather than at model level, and a future predicate field
on any mode's config inherits the rule without wiring."""


class SourceDecl(StrictBaseModel):
    """The grain source binding for one output table."""

    grain: Literal["records", "history_point", "history_interval", "membership"]
    kind: str
    property: str | None = None
    value: PredicateValue | None = None
    """For history_point grain, the property value(s) to filter on."""
    where: dict[str, PredicateValue] | None = None
    """Membership-only row predicate matched against membership element columns."""
    filter: dict[str, PredicateValue] | None = None
    """Records-only row predicate matched against the kind's records columns."""


class FkClause(StrictBaseModel):
    """A dimension foreign key resolved by a labeled-edge pathfind."""

    where: dict[str, PredicateValue] | None = None
    """Membership predicate matched against membership-table element columns."""


class ElapsedSpec(StrictBaseModel):
    """A cross-row elapsed time-delta between two correlated events."""

    other_where: dict[str, PredicateValue]
    """Predicate identifying the counterpart event row(s); the earliest
    matching interval start per correlation key is the one correlated."""

    @model_validator(mode="after")
    def other_where_non_empty(self) -> Self:
        """`other_where` names at least one predicate entry.

        The grammar's one required predicate mapping: an empty mapping renders
        no condition at all — a degenerate correlation the elapsed subquery
        cannot express. (The optional mappings have a meaning when empty —
        select all rows — that `other_where` does not.)

        Raises:
            ValueError: `other_where` is empty.
        """
```

### Functions

The single rendering authority, a public sibling of the shared typed-literal
renderer and identifier-quoting helper, in the same shared SQL-utilities module:

```python
def render_predicate_condition(
    column: str,
    value: str | list[str],
    sql_type: str,
    alias: str | None,
) -> str:
    """Render one config predicate entry as a SQL condition.

    A `str` is a scalar and renders `= <literal>`; a `list` is a set of
    alternatives and renders `IN (<literal>, ...)` preserving element order.
    Discrimination is on `isinstance(value, str)` — never on `Sequence`, which a
    `str` itself satisfies. Every element is typed by the shared typed-literal
    renderer against `sql_type`, so a list is exactly the scalar rule applied
    element-wise.

    Args:
        column: The predicate column name, quoted through the shared identifier
            helper.
        value: The required value — a scalar, or a non-empty list of
            alternatives. Emptiness is a parse-time failure and is not
            re-checked here.
        sql_type: The column's DuckDB type from the sidecar, used to type each
            literal. `VARCHAR` yields the raw single-quoted literal, which is how
            the `history.value` surface gets its untyped comparison — a caller's
            type choice, not a mode of this function.
        alias: A relation alias to qualify the column with, or None for an
            unqualified condition. Only the point-in-time membership foreign key
            needs one; every other caller passes None explicitly.

    Returns:
        One SQL condition, unparenthesized, suitable for AND-joining with
        sibling conditions.

    Raises:
        ExportError: `sql_type` is not a recognized DuckDB type. Raised by the
            shared typed-literal renderer, which never falls back to VARCHAR.
    """
```

The reader's faithful-read builders, with widened predicate parameters. Scalar
behavior is unchanged for every column type the contract's recommended mapping
produces; the refusal cases the renderer consolidation introduces are
§ Consolidating the literal renderers, and with them the records and membership
builders gain `ExportError` as a raise surface — the deliberate end of the
reader fork's VARCHAR fallback:

```python
def build_records_relation_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    discriminator_filter: Mapping[str, str | list[str]],
) -> str:
    """Build a faithful SELECT over records__<kind>.

    The relation is filtered to fork_path and to the predicate — each entry
    rendered by `render_predicate_condition` against the column's sidecar type,
    all entries AND-joined. Columns are the kind's full sidecar column list,
    unprojected; no ORDER BY.

    Args:
        sidecar: The open emit's sidecar (schema and column-type source).
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind; resolves to the records__<kind> table.
        discriminator_filter: Column -> required value or list of alternatives;
            an empty mapping selects all rows.

    Returns:
        A complete SELECT producing the kind's matching records, in no declared
        order.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
        ExportError: a predicate column's sidecar type is not one the shared
            typed-literal renderer recognizes (§ Consolidating the literal
            renderers).
    """


def build_history_relation_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    property_name: str,
    value_filter: str | list[str] | None,
) -> str:
    """Build a faithful SELECT over history for one (kind, property[, value]).

    Filtered to fork_path, kind, property_name, and — when value_filter is not
    None — value. The value comparison goes through `render_predicate_condition`
    with `sql_type: VARCHAR` (`history.value` is VARCHAR by contract), yielding
    the raw literals it renders today rather than a coercion against a source
    property's type; a list renders `IN` over those same raw literals.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind to filter history to.
        property_name: The property to filter history to.
        value_filter: A required value or non-empty list of alternatives matched
            against history.value, or None for no value filter. Callers pass
            None explicitly.

    Returns:
        A complete SELECT producing the matching history rows in no declared
        order.
    """


def build_membership_relation_sql(
    sidecar: "Sidecar",
    fork_path: str,
    owner_kind: str,
    property_name: str,
    where_predicate: Mapping[str, str | list[str]],
) -> str:
    """Build a faithful SELECT over the membership table for (owner_kind, property).

    Resolves membership__<owner_kind>__<property_name> from the sidecar, filtered
    to fork_path and to the predicate over elem__ columns, each entry rendered by
    `render_predicate_condition` against the column's sidecar type. Columns are
    the membership table's full sidecar column list; no ORDER BY.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        owner_kind: The membership owner kind.
        property_name: The membership property naming the table.
        where_predicate: Element column -> required value or list of
            alternatives; an empty mapping selects all rows.

    Returns:
        A complete SELECT producing the matching membership rows, in no declared
        order.

    Raises:
        TableNotFoundError: the membership table is not in the sidecar.
        ExportError: a predicate column's sidecar type is not one the shared
            typed-literal renderer recognizes (§ Consolidating the literal
            renderers).
    """
```

The derivations layer's two predicate-bearing relations, widened for the same
reason. The versioned-intervals relation renders no condition of its own — it
forwards the predicate to the reader records builder it composes — but its
signature is on the path a list-valued `filter` takes into an `scd: type2` dim,
so it widens too. (The history-interval grain also composes this builder, but
always with an empty predicate — the grain gate rejects `filter` on that grain —
so no list reaches it by that path:)

```python
def build_versioned_intervals_sql(
    sidecar: "Sidecar",
    fork_path: str,
    kind: str,
    tracked_properties: frozenset[str],
    discriminator_filter: Mapping[str, str | list[str]],
) -> str:
    """Build the canonical versioned-intervals SELECT for a kind over history.

    Unchanged but for the predicate's type. When discriminator_filter is
    non-empty the relation is restricted to the matching records by semi-join on
    the reader records relation, which owns the rendering; this builder passes
    the predicate through untouched.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        kind: The record kind to reconstruct versions for.
        tracked_properties: The tracked property names whose change points define
            the version boundaries.
        discriminator_filter: Column -> required value or list of alternatives;
            an empty mapping applies no restriction.

    Returns:
        A complete SELECT of one row per (record_id, version) interval.

    Raises:
        TableNotFoundError: records__<kind> is not in the sidecar.
        ExportError: a predicate column's sidecar type is not one the shared
            typed-literal renderer recognizes — propagated from the composed
            reader records builder (§ Consolidating the literal renderers).
    """
```


```python
def build_membership_edge_sql(
    sidecar: "Sidecar",
    fork_path: str,
    owner_kind: str,
    property_name: str,
    member_field: str,
    member_kind: str,
    where_predicate: Mapping[str, str | list[str]],
) -> str:
    """Build the owner -> member resolution relation over a membership table.

    Narrowed to member rows whose member kind is member_kind and to the
    predicate over element columns. Not fan-out-free: an owner matching several
    member rows yields several result rows, and a list-valued predicate can admit
    more of them than a scalar. Narrowing is the caller's responsibility.

    Args:
        sidecar: The open emit's sidecar.
        fork_path: The sole branch, from require_single_branch.
        owner_kind: The membership owner kind.
        property_name: The membership property naming the table.
        member_field: The reference field holding the member identity.
        member_kind: The member kind the edge resolves to; a row whose member
            kind differs is excluded.
        where_predicate: Element column -> required value or list of
            alternatives; an empty mapping applies no narrowing.

    Returns:
        A complete SELECT of (owner record_id, resolved member id) pairs, in no
        declared order.

    Raises:
        ExportError: the membership table, member_field, or a predicate column
            is unresolvable, or a predicate column's sidecar type is not one
            the shared typed-literal renderer recognizes (this surface refuses
            such a type today — § Consolidating the literal renderers).
    """
```

The dimensional election surface's population resolver. Its signature already
admits both forms (`Mapping[str, object]`); only its resolution rule widens:

```python
def resolve_dim_source_populations(
    sidecar: "Sidecar",
    source_kind: str,
    source_filter: "Mapping[str, object] | None",
) -> DimSourcePopulations:
    """Resolve a dim's source population set from its kind + filter.

    The discriminator conjunct's value set — a scalar's singleton, or a list's
    elements in config order — selects exactly those populations. Absent, the
    set is the kind's whole population set, exactly as today. `proper_subset`
    is strict set inclusion against the declared domain, so a list naming the
    full domain composes no restriction. Further conjuncts narrow rows within
    the set, never the set (§ The dim source population set).

    Args:
        sidecar: The open emit's sidecar.
        source_kind: The destination dim's `source.kind`.
        source_filter: The dim's `source.filter` mapping, verbatim from the
            TableDecl; None when the dim declares none.

    Returns:
        The resolved population set, populations in selection order.

    Raises:
        ExportError: an element of the discriminator conjunct's value set is
            not a declared sub-type of the kind's domain — evaluated per
            element, naming the offending element. (Reachable only when the
            kind is sub-typed.)
    """
```

## Validation Rules

### Parse-Time (Pydantic)

One shared rule covers every predicate-bearing model, and it is carried by the
`PredicateValue` type rather than declared on the models (§ Config Models). It
rejects the two malformed list shapes and says nothing about the scalar form.
The three models keep their existing validators unchanged — in particular
`SourceDecl`'s grain gate. One model gains a rule: `ElapsedSpec` requires
`other_where` non-empty, closing a pre-existing degenerate on the grammar's
sole *required* predicate mapping (an empty mapping renders no condition at
all; the optional mappings' empty form means "all rows" and is untouched).

| Condition | Result |
|---|---|
| Value is a scalar | Accepted |
| Value is a list with one or more distinct elements | Accepted |
| Value is an empty list | `ValueError` — an empty predicate selects nothing; omit the entry or the table |
| Value is a list with a repeated element | `ValueError` — naming the element identifies a config error rather than silently deduplicating |
| `other_where` is an empty mapping | `ValueError` — the required predicate mapping must name at least one entry |

The existing grain gate is unchanged: `filter` stays records-only, `where`
membership-only, `value` history-point-only, and the membership-only foreign-key
fields stay forbidden on a reference foreign key.

### Business Rules

Two existing rules become per-element; one is untouched. No rule is added or
removed. (The first two names below are the conceptual labels the mode's doc
uses, not code identifiers; the third is a descriptive label for the population
resolver's existing refusal, which no doc names.)

| Rule | Checks | Error / notice |
|---|---|---|
| `SliceOnlyColumnRefused` | **Unchanged, including its surface list.** Of the five predicates only the records `filter` keys fall inside this rule's population; the derived-elapsed `other_where` keys are refused by the sibling value-read check, likewise unchanged. Membership element predicates and the history-point `value` are outside both, because `elem__` and `history` columns carry no `temporal_class`. On every covered surface the check is per column and the value's form is irrelevant | existing refusal messages, unchanged |
| `DiscriminatorValueObserved` | Per element, for each records-predicate element whose column carries an observed-value set. Fires the existing `discriminator-value-unobserved` notice, keeping its message verbatim wherever the table really is empty and taking the per-element wording only for a partially-observed list (§ The unobserved-value notice) | notice, never an error |
| Dim-population domain gate | Per element, for a dim on a sub-typed kind whose discriminator conjunct carries a list: every element must be a declared sub-type of the kind's domain (§ The dim source population set). The existing scalar refusal applied element-wise, naming the offending element. Distinct from `DiscriminatorValueObserved`: the domain is the declared sub-type registry, not the observed-value set, and an out-of-domain population is an error, not a notice — a population that cannot exist must fail loudly (Principle #7) | existing refusal, per element |

`ProjectionColumnExists`, `MembershipEdgeResolvable` (including its element-column
check on a foreign key's `where`), and the elapsed-column existence check all
validate predicate *columns* and are unaffected by the value's form.

## Rationale

**Why widen all five surfaces rather than only the motivating one.** The
motivating case needs only the records `filter`. But the five surfaces are one
concept — a conjunction of equality predicates — implemented five times over the
same literal-typing helper. Widening one of them creates a rule with an
exception, and an author who learns lists on `filter` will reasonably try one on
`where`. The uniform rule is cheaper to state, cheaper to document, and removes
the "which one was it?" lookup. It also concentrates the scalar/list decision in
one rendering contract instead of leaving four sites able to drift.

**Why sub-type selection stays separate — in the modes that have a field for
it.** The claim is about source's `sub_types` and streaming's `types`. The
sub-typed discriminator is exempt from the rule that no exported row's
membership may derive from a column whose past is unknowable, because it is a
structural tag saying what a row *is* rather than a value with a history. That
exemption is mechanical and surface-total, and it is what lets sub-type
selection work unconditionally on emits where the discriminator is marked
unknowable-past. A general predicate cannot inherit it without leaking it to
columns that must not have it, so those dedicated fields stay distinct from the
predicate grammar, with distinct guarantees. Dimensional has no dedicated
field: its sub-type selection already lives *inside* `filter` as a
discriminator conjunct carrying a mechanical, column-scoped exemption — so it
takes the general scalar-or-list rule, with the population-set semantics of
§ The dim source population set, rather than sitting outside it.

**Why a list on the dim's discriminator selects populations — not refused, not
a plain row predicate.** Three answers were possible, and two fail. Treating
the list as an ordinary row-narrower (population set stays the full domain)
is incorrect: the population set is what keeps FK output closed over its
target dim — an out-of-set owner resolves to NULL precisely because the
identity relation is restricted to the set — so a set wider than the dim's
rows lets a fact render a key its dim excludes, a dangling reference
(Principle #4); it would also run union safety and the uniqueness guard over
populations the dim doesn't hold, refusing configs that are actually safe.
Refusing the list on that one conjunct is worse than an ordinary exception:
sub-typed-ness is a per-emit fact invisible at parse time, so the identical
YAML form would be legal against one emit and refused against another — and
the refusal would need a new sidecar-aware rule, where selection needs none.
Selection is also nearly free: every consumer of the population set is already
subset-general, because the unfiltered sub-typed dim carries the full
multi-population domain today; the singleton constraint lives in exactly one
`isinstance` check in the resolver. The one cost is honest: a subset spanning
differing elections hits the existing inheritance-ambiguity refusal — the
existing rule doing its job on a new input, and its message already names
every exit.

**Why base and source are deferred, and why that deferral is real.** Neither mode
has a row predicate today, so giving them one is a new field with new semantics,
not a widened type. It raises two questions this design never has to answer.

Base emits re-derived key columns for every reference edge, and source's event log
references its state tables — in both, filtering rows out of a table means
referrers point at rows that are no longer present, and base already has a notice
for that condition that would begin firing as a matter of course. Dimensional
already has a settled answer to that half — an unresolvable foreign key becomes a
typed NULL, never a fabricated value — which is precisely why widening it is safe.

The second question is concrete enough to name. Both modes reconstruct state at a
point-in-time horizon, and on that path they do not compose the reader's
predicate-bearing relation builders at all: they compose the state-at derivations,
whose signatures carry a *property* selection and a horizon and no row predicate
whatsoever. So a predicate on those modes is not an unimplemented parameter — it
is an undecided one. Under a horizon reconstruction, a predicate on a
history-tracked property could mean the value as-of the horizon or the current
records value, and those select different rows; the dimensional records grain
(current state, by construction) never poses the question. Widening those builders
now would be scaffolding for a caller that does not exist and a semantic that has
not been chosen.

What source *does* inherit for free, the day someone designs its field: the shared
rendering authority, the well-formedness rule riding `PredicateValue`, and — for
full export — the two reader builders it already composes, which this design
widens and which source calls with an empty predicate today. Designing the base
and source posture properly is its own doc; the mechanism will be waiting for it.

**Why the corrupter selector is deferred too.** It would inherit the same shared
mechanism, so the cost is not implementation. Its selector names rows to *damage*,
and the operation's declared impact is computed from what it matched — widening it
changes what a corrupter promises about its own blast radius, which is a question
about corrupter semantics rather than about predicate grammar.

**Why equality and membership only.** No range, negation, or null-test predicate
has a case in hand. Adding them now would be scaffolding for hypothetical work.
If negation arrives later, `not_in` is the name to use — it matches the producer's
own deferred condition-operator vocabulary, and cross-repo consistency is worth
more than a locally prettier spelling.

**Why duplicates are rejected rather than deduplicated.** A repeated element in a
hand-written list is a typo or a copy-paste error, and naming it is more useful
than absorbing it. Deduplicating silently would also make two configs that render
different SQL indistinguishable in their diagnostics.

**Why fan-out stays the author's problem.** The membership-edge relation already
fans out with no narrowing predicate. Deduplicating under a list-valued predicate
would discard rows that faithfully trace to base-layer values, and refusing would
make a legitimate query unexpressible. The engine's job is to render what was
asked for.

## Open Decisions

Two calls that could reasonably go the other way:

1. **Duplicate elements.** Proposed: reject at parse time. The alternative is a
   silent dedupe, which is friendlier to generated configs but hides an error in
   hand-written ones.

2. **Whether a one-element list renders `=` or `IN`.** Proposed: `IN`, because
   the rendering rule then depends only on the value's *form*, not its length.
   Rendering `=` for a one-element list would make the SQL marginally tidier at
   the cost of a length-dependent branch in the one place the rule is defined.
   Neither choice affects the result set.
