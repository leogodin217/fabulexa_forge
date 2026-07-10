# Effective Anchor

**Status:** Implemented. Code is the contract — see
[`anchor.py`](../../src/fabulexa_forge/anchor.py),
[`errors.py`](../../src/fabulexa_forge/errors.py) (the `Rebase*` hierarchy),
[`config/models.py`](../../src/fabulexa_forge/config/models.py) (`RebaseConfig`),
and [`tests/test_anchor.py`](../../tests/test_anchor.py). Public API:
`resolve_effective_anchor`, the `EffectiveAnchor` dataclass, and
`render_anchor_timestamp_expr` (the SQL renderer every wallclock mode shares).

`sim_time` is an integer-nanosecond offset since run start; to become a wallclock
datetime it needs an origin and a timezone. The effective anchor is the one
mode-agnostic surface that resolves that origin and zone for a single
`fabulexa-forge export` invocation — combining the emit's sidecar `runtime` block, the
export config's optional `rebase` block, and the CLI `--base-date` / `--timezone`
overrides into a single `EffectiveAnchor` (or `None`, meaning "no anchor — render
raw integers"). Every exporter that renders wallclock time reads through this one
result; no mode resolves its own anchor. It is the single authority that *parses*
the sidecar's raw `timezone` / `start_datetime` strings, applies precedence, and
validates zone and instant resolvability.

```
sidecar.runtime ┐
config.rebase   ├─► resolve_effective_anchor ─► EffectiveAnchor(start_instant, timezone)
--base-date     │                            └─► None  (no anchor → raw sim_time integers)
--timezone      ┘
```

Two author-controlled knobs are resolved against the sidecar:

- **Origin** (`base_date`) — the civil instant that `sim_time = 0` maps to. Absent
  → keep the sidecar origin.
- **Zone** (`timezone`) — the IANA zone that localizes the origin *and* governs
  display. Absent → inherit the sidecar zone.

---

## Boundary

- **Inputs.** The reader's typed `RuntimeAnchor` (or `None` when the emit's
  scenario declared no `runtime:` block); the config `rebase` block (or `None`);
  the two CLI overrides parsed at the CLI boundary. The reader surfaces
  `RuntimeAnchor` as raw strings — parsing the sidecar's `start_datetime` /
  `timezone` lives here, not in the reader or its consumers.
- **Output.** One `EffectiveAnchor(start_instant: datetime, timezone: ZoneInfo)`,
  or `None` when no anchor is determinable. `start_instant` is tz-aware.
- **CLI parse vs. resolution.** `--base-date` is declared with argparse
  `type=datetime.fromisoformat` (the verb's typed-callable pattern), so a bare
  date (`2020-03-01` → naive midnight) or a full datetime parses and any other
  string is an argparse usage error (exit 2). `--timezone` is a plain `str`.
  All *semantic* validation — naive-ness, zone existence, instant resolvability —
  lives in the resolver, so a well-formed but tz-aware `--base-date`
  (`2020-03-01T00:00:00+05:00`) parses cleanly and is then rejected by the
  resolver with `RebaseDateNotNaive`, the same path a tz-aware config
  `rebase.base_date` takes. CLI and config share one naive-ness check and one zone
  check.
- **Call site.** `cmd_export` owns resolution. Inside its existing
  `try`/`except (ReaderError, ExporterError)` funnel, after `open_emit` and
  `load_export_config`, it reads `emit.sidecar.runtime()` and calls
  `resolve_effective_anchor`, then threads the `EffectiveAnchor | None` into
  `export_dimensional(emit, config, out, fmt, anchor)`, from where it flows
  `build_query_specs → build_grain_sql → build_timestamp_expr(col_decl, anchor)`.

## Semantics

### The two knobs are orthogonal

| `base_date` | `timezone` | Operation |
|---|---|---|
| set | absent | **Rebase only** — move the origin; keep the run's zone. |
| absent | set | **Re-zone only** — display the same absolute instants in a new zone (`astimezone`; no origin change). |
| set | set | **Both** — origin at midnight / the given civil time *in* the given zone. |
| absent | absent | Identity — sidecar anchor verbatim, or raw integers if the sidecar has none. |

### Precedence (two independent chains)

| Quantity | Resolution order |
|---|---|
| `timezone` | `--timezone` → `rebase.timezone` → `sidecar.runtime.timezone` |
| `base_date` | `--base-date` → `rebase.base_date` → (none — keep the sidecar origin) |

### Effective-anchor resolution

| sidecar `runtime` | resolved `base_date` | resolved `timezone` | Result |
|---|---|---|---|
| present | none | none | Identity: parse sidecar `start_datetime` + sidecar zone. |
| present or absent | set | resolves | `start_instant = localize(base_date, timezone)`. |
| present | none | set | Re-zone: `start_instant = sidecar.start_datetime.astimezone(timezone)`. |
| absent | none | none | `None` — no anchor; consumers render raw integers. |
| present or absent | set | none anywhere | Error — `RebaseTimezoneUnresolvable` (a naive `base_date` needs a zone). |
| absent | none | set | Error — `RebaseOriginUnresolvable` (a zone override with no origin and no `base_date`). |

The sidecar instant is always absolute. The base-format contract guarantees a
present `runtime.start_datetime` is a tz-aware ISO-8601 instant, localized in
`runtime.timezone` and mutually consistent with it
([`contract/base-format.md`](../../contract/base-format.md) § Branch enumeration
and runtime anchor). The resolver parses it to an absolute instant with no
system-local assumption: the identity case takes that instant verbatim, and
re-zone calls `astimezone(timezone)` on it (single-valued, because the input is
already absolute). A sidecar `start_datetime` that does not parse as a tz-aware
datetime — including a naive value — is a malformed emit and raises
`RebaseInvalidRuntimeAnchor`. It is never silently localized.

### `base_date` granularity

| Input | Interpreted as |
|---|---|
| bare date `2020-03-01` | `2020-03-01T00:00:00` (midnight) localized in the effective zone. |
| naive datetime `2020-03-01T08:00:00` | that civil time localized in the effective zone. |
| value carrying a UTC offset / tzinfo | rejected — `RebaseDateNotNaive`. |

The zone is set *only* via `timezone` / `--timezone`, so there is one way to
express it and no offset-vs-zone conflict. Midnight is the canonical instant of a
bare date — an interpretation of the author's input, not an invented parameter
(Principle #7). Because `base_date` is typed `datetime`, a bare date is coerced to
`T00:00:00` at parse time; bare-date and explicit-midnight inputs are
indistinguishable to the resolver (both resolve to the same instant), which is
harmless by design.

### Instant-shift semantics and DST

Resolution and rendering apply a pure **affine origin shift**: only the origin
(and optionally the zone) move; every inter-event physical-ns duration is
invariant. The resolved instant for `sim_time = N` ns is `start_instant + N ns`.
DST is resolved against IANA rules — `zoneinfo` when localizing and validating the
origin in the resolver, DuckDB's bundled tz database when a dimensional SQL
expression projects each event's wall clock (§ dimensional rendering below). The
package holds no DST policy of its own.

Because a rebased window may traverse *different* DST transitions than the
original run, an event's local time-of-day can drift relative to the original run
(an event at "09:00" in a winter-anchored run lands at "10:00" after rebasing into
a summer window). This is inherent to physical-ns time and is the faithful
behavior; a civil-time-preserving rebase would contradict "physical ns is the
data."

### Ambiguous / nonexistent origin

| Condition | Result |
|---|---|
| `localize(base_date, timezone)` is a nonexistent civil time (DST gap) | Error — `RebaseDateUnresolvable`. |
| `localize(base_date, timezone)` is ambiguous (DST fall-back fold) | Error — `RebaseDateUnresolvable`. No silent fold pick. |
| Re-zone of the sidecar's `start_datetime` | Never ambiguous — the sidecar instant is absolute; `astimezone` is single-valued. |

Rejecting an ambiguous origin rather than picking a fold is fail-fast, matching
the producer's stance on author-supplied wallclock instants.

### Anchored-timestamp rendering

`render_anchor_timestamp_expr(anchor, qualified_source, out_name)` is the one SQL
renderer every wallclock mode shares. It represents the resolved instant as a naive
local wall-clock DuckDB `TIMESTAMP` in `anchor.timezone`, or — when `anchor` is
`None` — emits the raw `sim_time` `BIGINT` column unchanged. The dimensional mode
calls it for `derived: timestamp` (see [`dimensional.md`](dimensional.md) §
Timestamp source and the runtime anchor); it is the one renderer every wallclock
mode shares, so any future mode renders byte-identically by calling the same
function. The renderer interpolates exactly `str(anchor.timezone)` (the IANA key)
and `anchor.start_instant.isoformat()` (the origin literal carrying the UTC offset
at the origin instant only); DuckDB re-derives each event's local wall clock with
full DST rules, so a single origin offset is sufficient. The expression and its byte
content are this module's contract.

## Invariants

1. **Single effective anchor per export invocation.** All timestamp rendering in
   one `fabulexa-forge export` run reads through the one resolved `EffectiveAnchor` (or
   all render raw integers). No mode resolves its own anchor.
2. **Monotonic time.** The affine shift preserves strict ordering of `sim_time`
   and of the absolute instant. A *rendered* naive local wall clock may step
   backward across a DST fall-back, mirroring real clocks; monotonicity is a
   property of `sim_time` and the absolute instant, not of the local-time string.
   Row ordering is pinned by `sim_time`, never by the rendered timestamp.
3. **Causal consistency.** Relative ordering and all durations are preserved;
   rebasing introduces no forward reference.
4. **Faithful reshaping.** Every rendered timestamp traces to a base-layer
   `sim_time` plus an author-declared anchor; nothing is fabricated. The base-layer
   contract and the sidecar `runtime` block are read-only inputs — re-exporting
   with a different `base_date` re-reads no emit data and writes nothing back.
5. **Determinism.** Rendering is a pure function of `(sim_time, EffectiveAnchor)`;
   resolution is a pure function of `(sidecar runtime, config rebase, CLI
   overrides)`. Same inputs → same output.

## Validation Rules

`RebaseConfig` ([`config/models.py`](../../src/fabulexa_forge/config/models.py))
declares only shape; semantic checks belong to the resolver, so config-supplied
and CLI-supplied values share one validation path. An empty `rebase: {}` (both
fields absent) fails the model's `at_least_one_knob` validator at config-load
time; `load_export_config` funnels the Pydantic `ValidationError` into
`ConfigError` (an `ExporterError`). This rejection is at load, not in the
resolver — which is why the resolver does not raise it.

Resolution-time business rules (each raises a subclass of `RebaseError`, itself an
`ExporterError`, so the CLI's `except (ReaderError, ExporterError)` funnel reports
them with a non-zero exit — see [`errors.py`](../../src/fabulexa_forge/errors.py)):

| Rule | Checks | Error |
|------|--------|-------|
| Zone present for an origin | A resolved `base_date` has a resolvable zone | `RebaseTimezoneUnresolvable` |
| Origin present for a zone override | A resolved `timezone` with no `base_date` has a sidecar origin to re-zone | `RebaseOriginUnresolvable` |
| Naive origin | The winning `base_date` carries no tzinfo/offset | `RebaseDateNotNaive` |
| Resolvable origin | `localize(base_date, timezone)` resolves to exactly one instant (neither a DST gap nor a fold) | `RebaseDateUnresolvable` |
| Known zone | The resolved IANA string is a known `zoneinfo` zone | `RebaseUnknownTimezone` |
| Parseable sidecar anchor | `sidecar_runtime.start_datetime` parses to a tz-aware ISO-8601 datetime when needed | `RebaseInvalidRuntimeAnchor` |

## Rationale

- **One resolver, shared by every mode.** Origin, precedence, and DST/ambiguity
  logic live in one surface so a new wallclock mode adds only its own
  representation of the resolved instant and reinvents none of the resolution.
  This is why the anchor is resolved once in `cmd_export` and threaded down, rather
  than each exporter reading the sidecar itself.
- **Zone via `timezone` only, not via an offset on `base_date`.** A single way to
  express the zone removes the offset-vs-zone conflict and keeps `base_date` purely
  a civil instant. A tz-aware `base_date` is therefore an error, not a second zone
  channel.
- **Single timezone per emit.** Resolution yields exactly one effective zone;
  there is no per-table or per-column zone. One zone keeps cross-event ordering
  zone-independent — the producer's single-timezone discipline.
- **Fail-fast on ambiguous origins.** Picking a DST fold silently would fabricate
  a choice the author did not make; rejecting it matches the producer's stance on
  author-supplied wallclock instants.
- **Re-export, never re-simulate.** Rebasing is an export-time presentation choice.
  Inter-event durations are origin-independent, so a run anchored at one date is
  re-presentable at any other without touching the persisted run — the value an
  emit-less re-date provides.

## Boundaries

- **A plain projection still yields the raw integer.** Only `derived: timestamp`
  (and future mode renderers that opt in) apply an anchor; a `from: <sim_time
  column>` projection yields the raw `sim_time`.
- **No anchor, no rebase → raw integers, except the source mode.** An anchor-less
  emit with no rebase input renders raw `sim_time` integers in every mode but one:
  the source mode requires a resolved anchor and errors instead
  (`SourceAnchorRequired`) — an operational dump has no natural "no timestamp"
  representation. Resolution and rendering are otherwise identical to the
  dimensional mode's, through the same `render_anchor_timestamp_expr` (see
  [`source.md`](source.md) § Wallclock timestamps).
- **Future-mode rendering (streaming).** A future mode resolves through the same
  `EffectiveAnchor` and adds only its own representation of the resolved instant. A
  mode needing nanosecond fidelity computes on the anchor's epoch-ns directly
  (`origin_epoch_ns + sim_time_ns`); the datetime origin is losslessly convertible
  because the nanosecond tail lives in `sim_time`, not the origin. The streaming
  timestamp shape (epoch-nanosecond integer, ISO-8601 wall-clock string, or both)
  is undecided and determines that mode's renderer; no Python `datetime` renderer
  exists in this package, because the dimensional mode renders through
`render_anchor_timestamp_expr` in SQL — a renderer
  is added with the first non-SQL mode, not before (Principle #8).
- **Rendering tz database.** Dimensional DST is resolved by DuckDB's bundled tz
  database while origin localization uses Python `zoneinfo`. Both track IANA; a
  tz-database version skew between them (or across DuckDB versions) could shift a
  historical DST boundary — the same class as "cross-version reproducibility is not
  guaranteed," accepted rather than engineered around.
- **One anchor per invocation.** Per-table or per-column anchors are excluded;
  multi-zone output runs as separate exports.
- **Pacing.** The anchor sets the *stamped* event time only — never *when* a record
  is delivered. Replay pacing / drip-feed cadence is a separate concern.
- **Branch awareness.** The anchor is run-level, shared across all branches and
  forks by construction; there is no per-branch anchor interaction.

## Related

| Document | Why |
|---|---|
| [`dimensional.md`](dimensional.md) | § Timestamp source and the runtime anchor — the `derived: timestamp` renderer that consumes the resolved anchor and pins the SQL serialization. |
| [`source.md`](source.md) | § Wallclock timestamps — the anchor's first *mandatory* consumer; every structural sim-time column renders through the same SQL renderer as the dimensional mode. |
| [`reader.md`](reader.md) | § Surface — `RuntimeAnchor` and the typed sidecar the resolver reads as raw strings. |
| [`../../contract/base-format.md`](../../contract/base-format.md) | § Branch enumeration and runtime anchor — the vendored contract for the sidecar `runtime` block. |
| [`config/models.py`](../../src/fabulexa_forge/config/models.py) | `RebaseConfig` and `ExportConfig.rebase` — the config grammar these semantics bind. |
| [`README.md`](README.md) | Design index, package layout, staged roadmap. |
| [`../../CLAUDE.md`](../../CLAUDE.md) | Principles, the isolation boundary, vocabulary. |
