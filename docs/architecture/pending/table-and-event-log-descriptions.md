---
status: draft
---

# Table and Event-Log Descriptions

Two completions of the documentation channel: forge-pinned documentation for
the source event log (table and columns — a source-mode internal feature, no
config surface), and an author table-level description override on the
dimensional, source, and base modes (the table-granularity twin of the
shipped per-column override).

---

## Problem

Two rendered documentation surfaces remain that no authority answers, and
both sit at the top of what a reader of a companion README sees first.

**The source event log renders six undescribed columns.** The log's output
column set is forge-fixed — `id`, `item_type`, `item_id`, `event`,
`occurred_at`, `changes`, with no rename surface — and every column is
forge-constructed, so under the column-inheritance rule each inherits
nothing, and the per-column author override cannot reach them (the events
declaration carries no `descriptions` map). Every source export in every
domain therefore ships its most learner-visible table — the CDC-shaped
change log — with an empty data dictionary. Yet the columns mean exactly the
same thing in every export: their semantics are the mode's published
contract (the fixed column set, the first id, the changeset encoding), which
makes author prose the wrong tool. Forge constructed these columns; forge
should describe them.

**Table-level descriptions have no author tier.** The per-table description
in the README and manifest forwards the sidecar's `tables[].description`
verbatim (when the table's carried columns agree on one source table), so an
event-backed table renders engine narration — "One event-shaped record of a
decision firing (select, recall, seize…)" — and a journey-backed table "One
actor's traversal of one journey", directly above column dictionaries the
per-column override has made fully domain-voiced. `readme_overlay`'s
`table:` slots render *additional* prose beside the forwarded line; nothing
can replace the line itself. And the event log's table slot renders nothing
at all: its columns span every audited kind, so the single-source-agreement
rule correctly forwards no description.

```yaml
# Today: nothing an author writes changes this table's rendered description,
# and the audit_log section renders no table prose and no column prose.
source:
  tables:
    - name: sessions
      kind: journey_instance      # README: "One actor's traversal of one journey…"
  events:
    name: audit_log               # README: no description, six undescribed columns
```

## Solution

Complete the channel's two resolution surfaces symmetrically with the tiers
that already exist.

1. **Forge-pinned event-log documentation.** The companion dictionary — the
   established home of forge-authored companion prose (the interval-end
   description, the export structural rewrites) — gains a pinned table
   description and six pinned column descriptions for the event log. The
   source plan compiler marks the compiled event-log table as such; the
   dictionary answers for a marked table from the pinned set. No config
   surface: the log's documentation is mode-definitional, like its first id.

2. **Author table-level description override.** Each companion-writing
   mode's table-addressing idiom gains an optional table `description`,
   parallel to the column override: a field on the dimensional table entry,
   on the source declared table, and on the base rename entry. It is
   translated at plan compile onto the compiled table beside the three
   existing per-column documentation maps, forwarded verbatim by both
   report-assembly sites, resolved author-first by the shared dictionary,
   and rendered identically by README and manifest.

```yaml
source:
  tables:
    - name: sessions
      kind: journey_instance
      description: "One browsing session, from first page view to checkout or abandonment."
  events:
    name: audit_log               # renders forge-pinned table + column docs; no knob
```

## Affected Subsystems

- **Export-config models** — three grammars gain the table-level override:
  the dimensional table entry and the source declared table each gain an
  optional `description` field; the base rename entry gains an optional
  `description` field (singular, the entry's target table's prose) that
  counts toward the entry's at-least-one-field rule. The events declaration
  gains nothing. All are load-validated for shape; no plan-time key gate is
  needed — each field rides a declaration that already addresses exactly one
  output table.
- **The documentation channel** — table-description resolution gains two
  tiers above the sidecar forward: the author override first, then the
  forge-pinned event-log table description. Column resolution's forge-pinned
  tier (today the interval-end description and the four export rewrites)
  gains the six event-log column constants. The resolved-doc `origin`
  vocabulary gains `forge`, produced only by the companion dictionary — the
  reader's documentation view still never produces anything beyond
  contract / sidecar.
- **The compiled-plan carriage** — the mode-neutral compiled table and the
  per-table report gain two fields forwarded verbatim by both
  report-assembly sites under the existing carriage discipline: the author
  table description (`str | None`) and the event-log marker (`bool`). No
  builder entry-point signature changes.
- **The three batch-mode plan compilers** — dimensional and source stamp the
  author table description from their table declarations; base stamps it
  from the matched rename entry. The source compiler additionally marks the
  one event-log spec it compiles. Only the source compiler ever sets the
  marker.
- **The companion dictionary and builders** — table resolution consults
  author, then pinned, then the single-source forward; the README renders
  the resolved answer in the existing description slot of its per-table
  section order, and the manifest's per-table `description` mirrors it.
  Event-log column resolution answers from the pinned constants with
  `origin: "forge"`.
- **The incremental driver's fingerprint** — the canonical config dump
  excludes the new table-description fields, extending the standing rule
  that documentation is run-level presentation and can never make a resumed
  drip refuse.

## What Doesn't Change

- **The per-column author override** — surfaces, precedence, key gates, and
  carriage are untouched; this design adds tiers beside it, not under it.
- **`readme_overlay`** — remains the additive table- and export-level prose
  channel; its `table:` note still renders before the description line. The
  override replaces the one forwarded description line, nothing else.
- **The reader's documentation view** — two-authority (contract / sidecar),
  verbatim, unchanged. Pinned event-log prose and the `forge` origin exist
  only in the companion dictionary's resolution.
- **Kind-name-as-value gloss lists** — `item_type`'s per-value gloss list
  still sources each audited kind's sidecar `tables[].description` and
  renders beneath the column's (now pinned) description. An author
  table-level override on a *declared* table does not feed glosses; glosses
  are sidecar-sourced by design (they describe the bundle's kinds, not the
  export's tables).
- **The event log's data surface** — column set, first id, `item_type`
  vocabulary and `kind_labels`, `changes` encoding, key election of
  `item_id`, temporal rendering of `occurred_at`. Documentation only.
- **`init` proposal annotations** — unchanged; they already forward sidecar
  prose as YAML comments and gain no new annotation site.
- **Streams and the corrupter** — streams carry no companions; the
  corrupter's sidecar forwarding is not a companion surface.
- **Dataset bytes** — companions are the only artifacts that change;
  datasets are byte-identical with or without any of this (documentation
  inertness, as for the column override).

## Semantics

### Table-description resolution

For one output table, the rendered description resolves first-present-wins:

| Tier | Source | Applies to |
|---|---|---|
| 1 | The author table override | Any table of the three batch modes |
| 2 | The forge-pinned event-log table description | The marked event-log table |
| 3 | The single-source sidecar forward (`tables[].description`, when every carried column agrees on one source table) | Tables with single-source provenance |
| 4 | Nothing | Everything else |

Tiers 1 and 2 never compete: the events declaration has no `description`
field, so the marked table can never carry an author entry — stated as an
invariant below. The resolved answer renders in the README's existing
per-table slot (overlay note, then description, then columns, then glosses,
then row count) and in the manifest's per-table `description` field; absence
still renders nothing / JSON `null`.

| Condition | Result |
|---|---|
| Declared table carries `description` | That prose renders; the sidecar forward is not consulted |
| Declared table without `description`, single-source provenance | Sidecar `tables[].description` forwards as today |
| Declared table without `description`, multi-source provenance (e.g. dimensional lookup) | Nothing renders, as today |
| The marked event-log table | The pinned table description renders |
| Base rename entry with only `description` | Legal entry; the target table's description is overridden, names untouched |
| Overlay `table:` note also present | Note renders first, then the resolved description — both, never either-or |

### The pinned event-log documentation

The pinned strings live beside the dictionary's existing forge-authored
constants and are applied only to a table whose report carries the event-log
marker. Initial pinned prose (forge-maintained, like the mode templates —
its only change driver is the log's own contract changing):

| Surface | Pinned description |
|---|---|
| table | "The change log: one row per change to an audited item — a creation, an update, or a deletion — in event order." |
| `id` | "Sequence number of this log row: dense, ascending in event order, starting at 1." |
| `item_type` | "The type of the changed item. The values listed below name each audited item type." |
| `item_id` | "Identifier of the changed item, scoped by item_type: one item keeps one identifier across its rows." |
| `event` | "What happened to the item: 'create', 'update', or 'destroy'." |
| `occurred_at` | "When the change took effect. Changes are logged in order, so this never decreases as id ascends." |
| `changes` | "JSON object of the fields this change touched, each mapped to an [old, new] value pair — old is null on a creation, new is null on a deletion." |

The prose is written to stay true under every author knob and both source
shapes the log carries: it speaks of *items*, never tables or rows (a kind
may be audited with no declared table, and a membership source's item is
the owner's collection — `(item_type, item_id)` is the log's dereference
key, not a table row address); it names no id surface (`item_id` renders
the elected surface per target — creation-constant, hence "one item keeps
one identifier"); no time unit or rendering (`occurred_at` may render raw
ns or any elected temporal form); and no `item_type` vocabulary (values
are kind labels or verbatim kinds). `changes`' pair encoding holds across
every event shape: creations and membership joins carry `[null, value]`,
deletions and leaves `[value, null]`, updates only the fields whose values
differ — and a lifecycle event over an empty audited set renders `{}`,
which the prose does not contradict. A pinned column doc resolves with `origin: "forge"`, no
unit, and no enum options; `item_type`'s gloss list renders beneath it
exactly as today. The `changes`-key vocabulary (bare names, per-source
`rename`) needs no per-column prose: it is the mode template's subject, and
stays so.

| Condition | Result |
|---|---|
| Source export with an events declaration | The log's table + six column descriptions render pinned, README and manifest alike |
| `item_id` under an elected key surface | Same pinned prose — it claims no surface |
| `occurred_at` under a temporal rendering | Same pinned prose; no unit is claimed or inherited |
| Any other mode's table | Marker false, pinned set never consulted |
| Undocumented emit (bare sidecar) | Pinned prose still renders — it depends on nothing inherited |

### Carriage and rendering

Both new facts are answered once at plan compile and carried like the three
documentation maps: stamped on the compiled table, forwarded verbatim by the
shared full-export write dispatch and the incremental driver's windowed
report assembler, never re-derived by the builders from SQL, config, or the
materialized schema. Absence (`None` / `False`) is the answer — no default,
no sentinel. README and manifest render the same resolution because it
lives in the one shared dictionary; the manifest's embedded config carries
the new fields like any other config content, so authored table prose is on
record there.

### Incremental behavior

Documentation is run-level: every window renders identical companions under
the whole-state rewrite rule, and the fingerprint's canonical config dump
excludes the table-description fields exactly as it excludes the column
override surfaces and `readme_overlay` — an edited table description renders
from the next emitting window and can never make a resumed drip refuse.

### Invariants

- **Sourced, never invented** — the export config joins the enumerated
  documentation sources at table granularity; pinned event-log prose is
  mode-definitional, owned and versioned with the mode's contract. No other
  prose is forge-invented.
- **One authority per surface** — a rendered table description has exactly
  one origin (author / forge-pinned / sidecar); tiers never blend.
- **The marked table has no author tier** — no config surface addresses the
  event log's documentation; the events declaration rejects a `description`
  key (strict models).
- **At most one marked table per source plan, zero elsewhere** — only the
  source compiler sets the marker, only on the event-log spec; exactly one
  when the plan carries an events declaration, none otherwise.
- **Determinism** — same emit + config + code version → byte-identical
  companions.
- **Documentation inertness** — no dataset byte depends on any surface this
  design adds.

## Configuration

```yaml
# dimensional — a field on the table entry
dimensional:
  tables:
    - name: fact_customer_action
      role: fact
      description: "One shopping action a customer took: a visit, product view, cart add, comparison, or purchase."
      source: {grain: event, kind: tick_decision}
      key: [action_id]
      columns: [...]

# source — a field on the declared table (events takes none)
source:
  tables:
    - name: sessions
      kind: journey_instance
      description: "One browsing session, from first page view to checkout or abandonment."
  events:
    name: audit_log

# base — a field on the rename entry; a description-only entry is legal
base:
  rename:
    - table: records__actor
      name: customers
      description: "One row per customer account, at its latest state."
    - table: records__entity
      description: "Catalogue items at their current state."
```

| Field | Type | Required | Description |
|---|---|---|---|
| dimensional `tables[].description` | str | No | Rendered table description; replaces the forwarded sidecar prose in README and manifest. Non-empty, non-whitespace. |
| source `tables[].description` | str | No | Same, on a declared state/junction table. Non-empty, non-whitespace. |
| base `rename[].description` | str | No | Same, for the entry's target table. Non-empty, non-whitespace. Counts toward the entry's at-least-one-field rule. |

## Interface Contracts

### Config Models

```python
class TableDecl(StrictBaseModel):
    description: str | None = None
    """Author-supplied rendered description for this output table. Replaces
    the forwarded source-table description in the companion README and
    manifest. Absent -> forwarding as before."""
```

```python
class SourceTableDecl(StrictBaseModel):
    description: str | None = None
    """Author-supplied rendered description for this output table. Replaces
    the forwarded source-table description in the companion README and
    manifest. Absent -> forwarding as before."""
```

```python
class RenameEntry(StrictBaseModel):
    description: str | None = None
    """Author-supplied rendered description for the entry's target table.
    Replaces the forwarded source-table description in the companion README
    and manifest. Counts toward the entry's at-least-one-field rule.
    Absent -> forwarding as before."""
```

`SourceEventsDecl` is unchanged; strict models make a `description` key on
it a parse error by construction.

### Runtime Types

```python
@dataclass(frozen=True)
class QuerySpec:
    author_table_description: str | None = None
    """The mode's table-level override translated at plan compile; None
    means no override. Forwarded verbatim to TableReport."""
    event_log: bool = False
    """True iff this spec is the source mode's compiled polymorphic event
    log — the one table whose documentation the companion dictionary answers
    from the forge-pinned event-log set. Stamped only by the source plan
    compiler."""
```

```python
@dataclass(frozen=True)
class TableReport:
    author_table_description: str | None
    event_log: bool
    """Both forwarded verbatim from the compiled QuerySpec — no default, so
    every report-assembly call site states them explicitly, matching the
    three documentation maps."""
```

```python
class ColumnDoc:
    origin: Literal["contract", "sidecar", "author", "forge"]
    """"forge" names a companion-dictionary resolution answered by the
    forge-pinned event-log column set. Like "author", it is stamped only
    downstream — the reader's documentation view never produces it."""
```

### Functions

```python
def resolve_table_description(
    doc: "Documentation", table: "TableReport"
) -> str | None:
    """One table's resolved description, author-first.

    Args:
        doc: The emit's documentation view.
        table: The output table report.

    Returns:
        The report's author table description when present; else the pinned
        event-log table description when the report is marked as the event
        log; else the single source table's `tables[].description` when
        every carried column agrees on one source table; else None.
    """
```

```python
def resolve_column_doc(
    doc: "Documentation", table: "TableReport", column_name: str, output_type: str
) -> "ColumnDoc | None":
    """Unchanged signature. Resolution order gains one clause: on a report
    marked as the event log, a column named in the pinned event-log set
    resolves to a description-only ColumnDoc with origin "forge" (author
    entries cannot exist there; nothing inherits there today). All other
    resolution is unchanged.
    """
```

## Validation Rules

### Parse-Time (Pydantic)

- `TableDecl.description`, `SourceTableDecl.description`,
  `RenameEntry.description`: when present, non-empty and non-whitespace
  (the column-override string rule).
- `RenameEntry`: `description` joins `name` / `columns` / `descriptions` in
  the at-least-one-field rule.
- `SourceEventsDecl`: a `description` key is `extra_forbidden` — no change,
  stated as contract.

### Business Rules

- No plan-time key gate exists or is needed for the table-level override:
  each field rides a declaration whose table addressing is already gated
  (dimensional/source by the declaration itself; base by the rename entry's
  existing `table` / `sub_type` resolution errors).
- The source plan compiler marks exactly the event-log spec; a source plan
  with no events declaration marks nothing.
- The incremental fingerprint's exclusion set gains the three
  table-description fields; the marker is not config and never enters the
  fingerprint question.
