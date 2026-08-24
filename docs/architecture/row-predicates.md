# Config Row Predicates

Nine surfaces across three export modes select rows with a predicate. Five belong
to the dimensional grammar: `source.filter` (records-grain rows), `source.where`
(membership-grain rows), `source.value` (history-point rows), `fk.where` (the
membership interval a foreign key resolves through), and
`derived.elapsed.other_where` (the counterpart row of an elapsed correlation).
Two belong to the source grammar: `tables[].where` and `events.sources[].where`,
each gated to the declaring unit's `constant`-class payload properties
([`source.md`](source.md) § Row selection). Two belong to the streaming grammar:
`streams[].where` on each of the two stream shapes, under the same constant-class
gate over the stream's subject kind ([`streaming.md`](streaming.md) § Row
selection). All nine share one grammar — **a
predicate value is either a scalar or a non-empty list; a scalar compiles to `=`,
a list compiles to `IN`; predicates over distinct columns are AND-joined** — and
one rendering authority. This doc owns that grammar: its well-formedness rule,
its literal typing, the single compile path every surface routes through, and the
operator set it deliberately stops at.

**Source:** [`render_predicate_condition`](../../src/fabulexa_forge/_sql.py) (the
rendering authority) and
[`PredicateValue`](../../src/fabulexa_forge/config/models.py) (the value type
carrying the well-formedness rule). Tests:
[`tests/test_sql.py`](../../tests/test_sql.py) for rendering,
[`tests/config/test_models.py`](../../tests/config/test_models.py) for the parse
rule, and the per-surface suites under
[`tests/exporters/dimensional/`](../../tests/exporters/dimensional/),
[`tests/exporters/source/test_where_plan.py`](../../tests/exporters/source/test_where_plan.py),
and [`tests/exporters/streaming/test_selection.py`](../../tests/exporters/streaming/test_selection.py).

## Boundary

- **In:** one predicate entry — a column name, its value (scalar or list), and
  the column's DuckDB type as the sidecar declares it. Plain values only: the
  authority takes no config object and no `Sidecar`, so the derivations layer can
  compose it without importing the exporters.
- **Out:** one unparenthesized SQL condition, suitable for AND-joining with its
  siblings.
- **Refusals:** `ExportError` when the column's type is not one the shared
  typed-literal renderer recognizes. Malformed values never reach the authority —
  emptiness and duplication are parse-time failures.
- Resolving a predicate column's type belongs to the relation builder that knows
  the table; the authority receives the type as an argument and never reads a
  sidecar.

## Semantics

### Rendering

| Predicate value | Condition |
|---|---|
| Scalar `v` | `"col" = <literal(v)>` |
| List `[a]` | `"col" IN (<literal(a)>)` |
| List `[a, b, c]` | `"col" IN (<literal(a)>, <literal(b)>, <literal(c)>)` |
| Entries over several columns | AND-joined in the config's key order |

The rule depends on the value's **form**, not its length: a one-element list
renders `IN`, so the branch in the one place the rule is defined never consults a
list's cardinality. Discrimination is `isinstance(value, str)` — never
`Sequence`, which a `str` itself satisfies.

Element order is preserved verbatim in the rendered `IN` list. Two configs
differing only in element order render different SQL text but select identical row
sets under identical `ORDER BY` clauses, so their output is identical.
Determinism is a property of the output, and it holds.

The predicate column is quoted through the shared identifier helper, so a column
name carrying a quote cannot break out of the identifier position.

### Literal typing

Each element is typed independently: cast to the predicate column's DuckDB type as
the sidecar declares it, through the one shared typed-literal renderer. A list is
exactly the scalar rule applied element-wise.

The history-point `value` predicate is not an exception — it is the rule invoked
with `sql_type: VARCHAR`. `history.value` is `VARCHAR` by contract, and for
`VARCHAR` the shared renderer emits the single-quoted escaped literal directly,
which is the untyped comparison that surface needs. Raw-literal behavior is a
*caller's* type choice, not a mode of the authority: there is no bypass, no `raw`
flag, and no second function.

Typing is a render-time concern for the dimensional surfaces and additionally a
plan-time one for source's: source constant-evaluates each element's cast at plan
time on every `where`-bearing unit, so a literal the declared type cannot cast is
refused before any write rather than raising from the rendered `CAST` mid-export
([`source.md`](source.md) § Row selection). It is the same cast, evaluated
early — not a second typing rule.

An unrecognized type is refused, naming the type, rather than falling back to a
`VARCHAR` literal — a comparison that would quietly match nothing. So a predicate
on a `BLOB` column, or on a producer-custom array or struct column, is an
`ExportError`. Parameterized types are admitted by an **anchored** grammar
(digits-only arguments, nothing after the closing paren), never by prefix, so a
sidecar-supplied type string cannot close the `CAST` and append SQL.

### One rendering authority

Every condition compiled from a config predicate value is rendered by
`render_predicate_condition`, in the reader's faithful-read builders, the
derivations layer's membership edge, the dimensional exporter's foreign-key and
elapsed compilation, the source exporter's state, junction, and event-log
narrowing, and the shared selection spine both source and streaming resolve their
row scope through ([`selection-spine.md`](selection-spine.md)) alike. No surface
renders `=` or `IN` over a config predicate itself,
and there is no second implementation of the scalar/list rule to drift.

The invariant is scoped to *config* predicates. Engine-internal scoping conditions
(`fork_path`, `kind`, `property`) keep their own raw-literal rendering; sub-type
population filters and semi-join `IN (SELECT …)` constructs are not config row
predicates and are outside it.

### The unknowable-past gate

The `slice_only` refusal ([`slice-only.md`](slice-only.md)) is evaluated per
predicate **column**. Of the seven surfaces, the records `filter` keys and the
derived-elapsed `other_where` keys fall inside its population; membership element
predicates and the history-point `value` are outside it by construction, because
`elem__` and `history` columns carry no `temporal_class`. Since the check reads a
column's class and never its value, the predicate value's form is irrelevant to
it, and the sub-typed discriminator's carve-out applies in both forms.

The source and streaming surfaces are inside the population and are refused by a
stricter rule that subsumes it: a `where` key must name a `constant`-class
property, so `slice_only` and `tracked` keys alike are refused with a message
naming the class — `SourceWhereNotConstant` in source
([`source.md`](source.md) § Row selection), `StreamWhereNotConstant` in streaming
([`streaming.md`](streaming.md) § Row selection). The stricter gate is
what makes row membership horizon-invariant under a mode that reconstructs state
at a horizon, and time-invariant under a mode that replays every instant of the
tape; the dimensional records grain, current state by construction, needs
only the `slice_only` refusal.

### Fan-out

The membership-edge relation is not fan-out-free: with no narrowing predicate, an
owner bound to several members yields several rows. A `where` is the author's
instrument for narrowing, and a list-valued `where` sits between a scalar and an
absent predicate on that spectrum — it can admit more intervals per owner than a
scalar does. Cardinality is the author's responsibility; the engine neither
deduplicates nor refuses (§ Rationale).

## Invariants

1. **One rendering authority.** Every condition compiled from a config predicate
   value — dimensional's `filter`, `where`, `value`, `fk.where`, `other_where`,
   source's `tables[].where` / `events.sources[].where`, and streaming's
   `streams[].where` on both stream shapes — is rendered by
   `render_predicate_condition`. No other module renders `=` or `IN` over a config
   predicate.
2. **One well-formedness rule, carried by the type.** The non-empty and
   no-duplicates rule is attached to `PredicateValue`, not to the models that use
   it, so every field declared with that type carries it — including as the value
   type of a `dict[str, PredicateValue]`, where it applies per entry — and a
   future predicate field inherits it without wiring.
3. **A predicate value is never empty.** An empty list is rejected at parse time,
   so no surface renders a degenerate `IN ()`. The grammar's one *required*
   predicate mapping (`other_where`) is likewise never empty, so no surface renders
   a conditionless correlation.
4. **Typing is sidecar-resolved and total.** Every element is cast to the
   predicate column's declared DuckDB type; a type the shared renderer does not
   recognize is refused, never inferred.
5. **Rendering is form-determined.** The `=` / `IN` choice reads only whether the
   value is a `str` or a `list` — never its length, and never the column.

## Validation Rules

Parse-time, carried by `PredicateValue`
([`config/models.py`](../../src/fabulexa_forge/config/models.py)):

| Condition | Result |
|---|---|
| Value is a scalar | Accepted |
| Value is a list of one or more distinct elements | Accepted |
| Value is an empty list | `ValueError` — an empty predicate selects nothing; omit the entry or the table |
| Value is a list with a repeated element | `ValueError`, naming the repeated element |
| `other_where` is an empty mapping | `ValueError` — the required predicate mapping must name at least one entry |

Because the rule rides the type rather than a model validator, a failure is
reported at the offending field's path — for a mapping, at the offending entry —
rather than at model level.

The grain gate is separate and orthogonal: `filter` is records-only, `where`
membership-only, `value` history-point-only, and the membership-only foreign-key
fields are forbidden on a reference foreign key
([`dimensional.md`](dimensional.md) § Validation Rules). The value's form plays no
part in it.

The business rules that read predicates belong to the modes and are stated there:
dimensional's `slice_only` refusal, unobserved-discriminator notice, and dim
source population set's declared-domain refusal
([`dimensional.md`](dimensional.md)); source's constant-class, discriminator,
castability, and selection-aware disjointness gates plus its reuse of the
per-element unobserved-value notice ([`source.md`](source.md) § Validation
Rules); and streaming's constant-class, discriminator, resolvability, and
castability gates over the same shared notice
([`streaming.md`](streaming.md) § Validation Rules). The rules that evaluate per
element are the notices and the source and streaming
castability checks; the rules that validate predicate *columns*
(`ProjectionColumnExists`, `MembershipEdgeResolvable`, the elapsed-column check,
source's key resolution) are indifferent to the value's form.

## Rationale

- **One grammar across every surface, not one widened surface.** They are one
  concept — a conjunction of equality predicates — over one literal-typing
  helper. "Predicates take a value or a list" has no exception an author must look
  up, where "`filter` takes a list but `where` does not" must be consulted every
  time. Uniformity also concentrates the scalar/list decision in one contract
  instead of leaving each site able to drift, and it is why a mode adding a
  predicate surface adds a *gate* on which columns are addressable, never a
  second value grammar.
- **Sub-type selection is a separate surface with a separate guarantee.** Source's
  and streaming's `sub_types` are multi-valued fields of their own, and
  they sit outside this grammar. The sub-typed discriminator is exempt from the
  rule that no exported row's membership may derive from a column whose past is
  unknowable, because it is a structural tag saying what a row *is* rather than a
  value with a history — that exemption is mechanical and surface-total, and it is
  what lets sub-type selection work unconditionally on an emit that marks the
  discriminator `slice_only`. A general predicate cannot inherit the exemption
  without leaking it to columns that must not have it. Dimensional has no dedicated
  field: its sub-type selection lives *inside* `filter` as a discriminator
  conjunct already carrying a mechanical, column-scoped exemption, so it takes the
  general rule, with the population-set semantics in
  [`dimensional.md`](dimensional.md) § Foreign keys.
- **Equality and set membership only.** No range, negation, or null-test predicate
  has a case in hand, and adding one would be scaffolding (Principle #8). Should
  negation arrive, `not_in` is the name — it matches the producer's own deferred
  condition-operator vocabulary, and cross-repo consistency outweighs a locally
  prettier spelling.
- **Duplicates are rejected, not deduplicated.** A repeated element in a
  hand-written list is a typo or a copy-paste error, and naming it is more useful
  than absorbing it. Silent dedupe would also make two configs that render
  different SQL indistinguishable in their diagnostics.
- **Fan-out is the author's problem.** Deduplicating under a list-valued predicate
  would discard rows that faithfully trace to base-layer values (Principle #3);
  refusing would make a legitimate query unexpressible. The engine renders what was
  asked for.
- **The authority takes a type, not a sidecar.** Keeping type resolution with the
  relation builder that knows the table is what lets the derivations layer compose
  the authority while staying pure and anti-weld
  ([`derivations.md`](derivations.md) § The layer contract).

## Boundaries

- **Composition is a conjunction over distinct columns.** No disjunction across
  columns, no nesting, no repeated entry for one column — a mapping key appears
  once, and its value carries the alternatives.
- **The base mode carries no row predicate.** Its contract is "every records
  kind, one flat table", and row filtering there is a separate, undecided
  feature. The horizon question a predicate poses under a state-at
  reconstruction — as-of-the-horizon value or current records value, which select
  different row sets — is answered in source by the constant-column gate, which
  makes the question unposable rather than picking a side. Whoever designs base's
  field inherits this grammar, the rendering authority, that gate as the
  precedent, and the two reader builders base already composes with an empty
  predicate.
- **The corrupter's row selector is a separate grammar.** It names rows to
  *damage*, and an operation's declared impact is computed from what it matched, so
  its shape is a question about corrupter blast radius rather than about predicate
  expressiveness ([`corrupters.md`](corrupters.md)).
- **A membership-grain `fk.where` is refused.** When a table's own grain is
  `membership`, a plain `via: membership` foreign key reads the member id off the
  grain row itself; there is no separate edge relation to narrow, so the clause
  has no meaning in either value form. `TableDecl` rejects the combination at
  parse time and points the author at `source.where` — the surface that narrows a
  membership grain's rows. The point-in-time form (`as_of`) correlates its own
  membership subquery and keeps `where`.
- **`init` proposes scalars, and no predicate at all in source.** Sub-type splits
  are genuine one-value splits, deterministic from a declared discriminator
  domain. A de facto discriminator is not mechanically distinguishable from any
  other constant enum property, so proposing a `where` from observed values would
  be invention ([`source.md`](source.md) § `init --mode source` inference
  contract).
- **The writers and the mixer read none of these fields.** A predicate is resolved
  and compiled before either sees a row. Tier-2
  shaped playback compiles a declared `ExportConfig` through the dimensional
  compile surface, so a predicate transits it verbatim — a consumer of this
  grammar, not a surface of it ([`playback.md`](playback.md)).

## Related

| Document | Why |
|---|---|
| [`dimensional.md`](dimensional.md) | The five dimensional surfaces' grain semantics, the business rules that read predicates, and the dim source population set |
| [`source.md`](source.md) | The two source surfaces — the constant-column gate, the parent lookup, and the selection-aware event-source disjointness rule |
| [`streaming.md`](streaming.md) | The two streaming surfaces — the same constant-column gate over a stream's subject kind, and the satisfying-record-set row scoping it feeds |
| [`selection-spine.md`](selection-spine.md) | The mode-neutral relation source and streaming compile these conditions against |
| [`reader.md`](reader.md) | The faithful-read builders that resolve each predicate column's type and compose the authority |
| [`derivations.md`](derivations.md) | The membership edge and versioned-intervals relations on the predicate's path |
| [`slice-only.md`](slice-only.md) | The per-column unknowable-past refusal over predicate keys |
| [`notices.md`](notices.md) | The channel the per-element unobserved-value notice flows through |
| [`config-docstrings.md`](config-docstrings.md) | The docstring convention `PredicateValue` and the predicate-bearing models follow |
