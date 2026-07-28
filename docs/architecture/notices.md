# Notice Channel

The export package's one informational output channel. A **notice** is a frozen,
deterministic record of a plan-time fact the author did not ask about — a policy
omission, an empty-table warning — delivered synchronously to a caller-supplied
sink. Notices are data: same emit + config + code version → the identical notice
sequence, assertable in tests. They never touch stdout, never alter output data,
and never change the exit code. Every informational emission in the package flows
through this channel; there is no `warnings.warn` surface.

**Source:** [`src/fabulexa_forge/exporters/notices.py`](../../src/fabulexa_forge/exporters/notices.py),
tests in [`tests/exporters/test_notices.py`](../../tests/exporters/test_notices.py).

## Boundary

- **In:** a `Notice` from any plan/compile path (dimensional validation, source
  plan, base plan, `init` proposal).
- **Out:** each notice passed, synchronously and in discovery order, to the
  `NoticeSink` the caller supplied.
- Every entry point that can emit a notice takes a **required** `notice_sink`
  parameter — no default (Principle #7 applied to an output channel). A caller
  that wants silence passes a discarding sink; the library never chooses silence
  for it. Streaming entry points carry no sink: the mode emits no notices.

## Semantics

| Property | Rule |
|---|---|
| Determinism | Same emit + config + code version → identical notice sequence, content and order. Notices follow plan iteration order |
| Severity | Informational only: a notice never changes output data, table sets, or the exit code |
| Channel | Delivered synchronously to the `NoticeSink` as discovered; the CLI's sink ([`render_notice_stderr`](../../src/fabulexa_forge/exporters/notices.py)) writes one `notice: {message}` line to stderr. stdout is data delivery's channel (`init` prints its candidate YAML there) and is never touched |
| Timing | Plan-time notices are emitted before any data is written or streamed |
| Incremental | Every driver invocation compiles exactly once, so the sink threads through with no forwarding or dedup logic; a `--next` drip re-emits its compile's notices each invocation (see [`incremental.md`](incremental.md)) |
| Errors vs notices | Anything knowable as wrong at validation time is an error, never a notice. Notices report policy outcomes the author did not ask about |
| Shape | `code` + fully rendered `message` only — no structured subject fields. Tests key on `code`, and on the verbatim `message` where the subject (table, column, unit) matters |

### Notice codes

| `code` | Emitted by | Meaning |
|---|---|---|
| `slice-only-column-omitted` | source plan (per unit × column), base plan (per kind × column), `init` (per kind × column) | A `slice_only` column was dropped from an auto-projected surface (see [`slice-only.md`](slice-only.md)) |
| `discriminator-value-unobserved` | dimensional validation | A records `filter` value is not among the kind's observed `enum_domains` values; the table will be empty |
| `reference-key-target-absent` | base plan (per kind × property) | A reference property's target kind has no records table in the emit, so no index-space key column is produced for that edge; the id-space column is unaffected (see [`base.md`](base.md) § Record-index key columns) |

## Invariants

1. Notices are deterministic data: sequence content and order are a function of
   emit + config + code version.
2. A notice is non-fatal and side-effect-free on data: output tables, streams,
   and exit codes are identical with any sink.
3. stdout is never the notice channel.
4. The sink is caller-supplied and required; no entry point defaults it.

## Rationale

- **A sink parameter, not `warnings.warn`** — warnings are process-global,
  filterable by ambient configuration, and unordered as data; a synchronous sink
  makes the sequence deterministic, testable, and embeddable (a library caller
  routes notices wherever it likes).
- **stderr for the CLI** — stdout belongs to data delivery; an omission report
  interleaved with candidate YAML or piped data would corrupt both.
- **No structured subject fields** — determinism makes the rendered message
  itself assertable; structured fields wait for a consumer that needs them
  (Principle #8).

## Related

| Document | Why |
|---|---|
| [`slice-only.md`](slice-only.md) | The policy whose omissions the channel reports |
| [`dimensional.md`](dimensional.md) | Emits `discriminator-value-unobserved`; `init` emits skip notices |
| [`source.md`](source.md) | Emits `slice-only-column-omitted` per unit × column |
| [`base.md`](base.md) | Emits `slice-only-column-omitted` per kind × column and `reference-key-target-absent` per kind × property |
| [`incremental.md`](incremental.md) | Threads the sink through windowed compiles |
