# Dataset Distribution

The package's dataset-distribution surface: real producer-emitted example
datasets — a bundle (`run.duckdb` + `base.json` + `ATLAS.md`) beside authored
export / corrupt / stream configs — published as self-contained packs on GitHub
Releases, named by a manifest baked into the wheel, and reachable through
`fabulexa-forge datasets list|get` without a repo checkout. The audience
installs the package rather than cloning the repo, and the example bundles are
gitignored binaries (1–44 MB); this surface is the logistics that puts a real
emit and ready-to-run configs in an installed user's hands. Distribution moves
bytes already shaped by the contract — it adds no coupling to the producer and
no second interpretation of the bundle.

**Source:** [`src/fabulexa_forge/datasets/`](../../src/fabulexa_forge/datasets/)
([`models.py`](../../src/fabulexa_forge/datasets/models.py) manifest models,
[`manifest.py`](../../src/fabulexa_forge/datasets/manifest.py) loader + listing
renderer, [`fetch.py`](../../src/fabulexa_forge/datasets/fetch.py) fetch /
verify / extract), CLI verb in [`cli.py`](../../src/fabulexa_forge/cli.py),
repo-side pack builder in
[`tools/build_dataset_pack.py`](../../tools/build_dataset_pack.py). Tests in
[`tests/datasets/`](../../tests/datasets/) and
[`tests/test_cli_datasets.py`](../../tests/test_cli_datasets.py).

## Boundary

- **In:** the shipped manifest (package data inside the wheel), a dataset name,
  and — for `get` only — the bytes an anonymous HTTPS GET serves for the
  entry's `url`, read through an injectable `Transport`
  ([`fetch.py`](../../src/fabulexa_forge/datasets/fetch.py)).
- **Out:** the listing payload on stdout; an extracted pack directory; a frozen
  `GetResult` (target directory + substituted example commands). Failures in
  the dataset contract raise `DatasetError` — the CLI maps it to stderr +
  exit 1.
- **Non-inputs:** no emit is opened and no plan is compiled, so the notice
  channel's population here is empty by construction — download progress and
  diagnostics are CLI presentation on stderr, never `Notice` records
  ([`notices.md`](notices.md)). No new runtime dependencies: download
  (`urllib`), hashing (`hashlib`), and extraction (`tarfile`) are stdlib.
- **One runtime version gate.** `open_emit` alone refuses an unknown
  `base_format_version`; this surface carries no second runtime version
  check. The manifest's version *field* is enforced
  mechanically before a wheel ships (§ Validation Rules); wrong-version bytes
  behind a correct pin still hit the reader's own gate at first use.
- **Tests never touch the network.** All fetch-path tests run through the
  `Transport` seam against local bytes. Downloaded datasets are a user-facing
  asset, never a test dependency — the test fixtures are synthesized and CI
  is offline.

## Semantics

### Manifest

The manifest is a YAML document validated through Pydantic at load
([`DatasetEntry` / `DatasetManifest`](../../src/fabulexa_forge/datasets/models.py)
own the field set and shapes). It lives at
`src/fabulexa_forge/datasets/manifest.yaml`, inside the package tree, so the
wheel build (`packages = ["src/fabulexa_forge"]`) ships it as package data;
`importlib.resources` resolves one path valid in both the wheel and in-tree
layouts. Loading is offline and deterministic.

- Entries are an **authored allowlist**. An example directory participates only
  by having an entry; absence is the only exclusion mechanism.
- Entry order is authored order and is preserved everywhere (listing output,
  iteration) — no re-sorting.
- `base_format_version` on an entry is a **recorded fact about the pack**,
  stamped by the pack builder from the pack's own `base.json` (read through
  `open_emit`, never parsed ad hoc) — never hand-typed. The single version
  authority is `SUPPORTED_BASE_FORMAT_VERSION`; the hygiene test pins
  every entry to it, so an install can only ever list datasets its own reader
  opens. A contract-version bump turns stale entries into a red hygiene test
  at bump time, naming each stale entry — never into a runtime surprise for an
  installed user.

### `datasets list`

Fully offline — `list` performs no network I/O, ever; its output is a pure
function of the manifest. Text format renders every entry in authored order:
name, human-readable size (from `size_bytes`), `base_format_version`, config
coverage (the entry's `configs` filenames verbatim — a display of the list,
not a derivation), description. `--format json` renders the manifest itself as
a byte-stable document: the model's field set verbatim, raw values
(`size_bytes` as the integer), entries in authored order, keys sorted,
separators `(",", ":")`, no trailing newline. An empty catalog is not an
error: text renders a clear no-datasets line, JSON renders the model document
verbatim (`{"datasets":[]}`), exit 0 either way. Renderer:
[`render_dataset_listing`](../../src/fabulexa_forge/datasets/manifest.py);
cases in [`tests/datasets/test_manifest.py`](../../tests/datasets/test_manifest.py).

### `datasets get <name>`

Pipeline: resolve entry → check target path (refuse if occupied without
`--force`; nothing mutated yet) → download to a temporary file → verify (byte
count, sha256, then every archive member's safety) → prepare the target
directory (`--force` removal happens here, only after all verification passes)
→ extract → delete the temporary archive → print next steps. **The target path
is never touched before the downloaded archive fully verifies** — every
pre-extraction failure (unknown name, occupied target, download failure, size
or digest mismatch, unsafe member) leaves it exactly as found, reported as
`DatasetError` to stderr, exit 1. The temporary archive never survives `get`:
deleted on success and on every failure path alike. The default target is
`./<name>`; `--dir DIR` overrides it, and `{dir}` in the printed commands
substitutes to the path as given — never absolutized. Behavior cases:
[`tests/datasets/test_get_dataset.py`](../../tests/datasets/test_get_dataset.py),
[`tests/test_cli_datasets.py`](../../tests/test_cli_datasets.py).

- An archive member is **unsafe** when its path escapes the target directory
  (absolute, or containing a `..` component) or it is not a regular file or
  directory. Every member is checked before any is extracted, so a refusal
  writes nothing.
- The **`OSError` → `DatasetError` mapping boundary is a phase, not a
  source**: any `OSError` raised during the download-and-verify phase —
  transport open, transport read, and temporary-file write alike (one
  streaming loop; attributing an `OSError` to one side is not implementable) —
  maps to `DatasetError`. Any `OSError` raised *after* verification (target
  preparation, `--force` removal, extraction — disk full, permissions) is
  outside the `DatasetError` population and outside the atomicity guarantee:
  it propagates unwrapped and the target may hold a partial tree. This matches
  the other verbs' posture toward environmental I/O failure.
- The CLI-wired transport applies a network timeout so a dead connection fails
  instead of hanging. The timeout's value is CLI presentation, not contract or
  config; its expiry surfaces as `OSError` inside the download-and-verify
  phase and maps like any other download failure.
- **`sha256` is the authority on content identity.** GitHub release assets can
  be replaced under the same tag; the pin makes a swap a loud verification
  failure instead of a silent content change.
- The printed example commands are documentation output, not configuration:
  the tool never executes them and they carry no defaults into any config
  surface. They are authored per entry precisely so flag choices (`--fmt`,
  anchor flags, sink flags) are a maintainer decision, not a code invention.

### Pack

- Layout: `bundle/run.duckdb`, `bundle/base.json`, `bundle/ATLAS.md`, and the
  manifest-named config YAMLs at the archive root. All member paths relative;
  no wrapper directory; no `exports/`, no demo-glue files.
- Format: gzip-compressed tar. One archive per dataset per release; the
  manifest URL pins the exact asset.
- The builder produces regular-file members only; `get` additionally accepts
  directory members and refuses everything else, so the safety property holds
  even against a hand-built archive.
- A pack is self-describing to the existing toolchain: after extraction,
  `<dir>/bundle` is a valid emit directory and each config runs against it
  with the shipped CLI verbs.

### Pack builder (repo-side tool)

[`tools/build_dataset_pack.py`](../../tools/build_dataset_pack.py) — run
through this repo's venv (`uv run` — it imports `open_emit`), never shipped in
the wheel. It takes the dataset `<name>` (one entry per invocation) and
`--out DIR`; the archive is written to `<out>/<name>.tar.gz`. Refusals go to
stderr, exit 1. It never talks to the network.

- **The entry comes first; the builder stamps it.** The build is driven by the
  entry's *authored* fields: `name` locates `docs/examples/<name>/`, `configs`
  names the YAMLs packed. The *stamped* fields (`sha256`, `size_bytes`,
  `base_format_version`) are ignored on read and recomputed — a first-time
  entry is authored with any syntactically valid values there.
- **Print, never edit.** The builder emits the stamped fields to stdout as a
  paste-ready YAML fragment; it never rewrites the manifest in place. The
  manifest is a hand-authored document with builder-supplied values pasted
  in.
- **Reader-first, even repo-side.** The builder never parses `base.json` ad
  hoc: it opens the bundle through `open_emit` — the sole sanctioned path —
  and stamps `base_format_version` from the opened sidecar. The reader's
  version gate is therefore also the builder's publishability gate: a bundle
  it refuses (a `base_format_version` other than
  `SUPPORTED_BASE_FORMAT_VERSION` included) is a build refusal rendering the
  gate's own diagnostic. An incomplete bundle triple or an absent `configs`
  file is likewise a refusal naming the missing file.
- **Deterministic archive** means byte-identical: same input tree → the same
  bytes → the same `sha256`, so a rebuild never silently forces a manifest
  re-stamp. Normalization contract: members added in sorted-path order; member
  `mtime` 0, `uid`/`gid` 0, `uname`/`gname` empty, mode 0644 (file members
  only — the archive carries no directory members; directories materialize at
  extraction); gzip stream with `mtime` 0 and an empty original-filename
  field.

Publishing a dataset is: author configs in `docs/examples/<name>/` → run the
pack builder → upload the archive to the release → commit the manifest entry.
Uploading and tagging are manual/`gh`-driven. The pack is the publication
unit — the sidecar and atlas ship inside it, so the repo commits no copies of
them beside the gitignored bundle binary.

### The CLI verb

`datasets` is the first verb with sub-verbs (`list`, `get`): one entry in the
verb registry; the sub-verb split is parsed inside the handler via argparse
subparsers. Exit-code contract: 0 success, 1 failure (`DatasetError`), 2 usage
error — a missing or unknown sub-verb, like any bad flag, is an
argparse-shaped usage error to stderr, exit 2 (the argparse precedent, not the
top-level unknown-verb exit 1). Progress and diagnostics go to stderr; the
payload (listing, next-step commands) goes to stdout.

## Invariants

1. **Determinism.** Same manifest + same downloaded bytes → identical
   extracted tree and identical stdout. `list` output is a pure function of
   the manifest.
2. **Offline listing.** `list` performs no network I/O, ever.
3. **Version agreement by construction.** Every manifest entry in a shipped
   wheel satisfies `base_format_version == SUPPORTED_BASE_FORMAT_VERSION`,
   enforced by the hygiene test, fed by the builder's stamp-from-the-pack
   rule.
4. **Content pinning.** No downloaded byte is trusted before its sha256
   matches the manifest.
5. **Failure atomicity.** `get` mutates the target path only after the
   downloaded archive fully verifies (byte count, sha256, member safety);
   every earlier failure leaves the path exactly as found.
6. **No temporary residue.** The temporary archive is deleted on success and
   on every failure path; `get` never leaves it behind.
7. **No network in tests.** All fetch-path tests run through the transport
   seam against local bytes.

## Validation Rules

**Parse-time (Pydantic).** The manifest models validate every entry at load —
name slug, https URL, 64-hex sha256, positive sizes/versions, non-empty
`configs` (bare `.yaml` filenames, no path separators) and `commands` (each
containing `{dir}` and no other placeholder), unique entry names. The rules
live as the models' validators
([`models.py`](../../src/fabulexa_forge/datasets/models.py); cases in
[`tests/datasets/test_models.py`](../../tests/datasets/test_models.py)).

**Business rules** are enforced by tests and the pack builder, not at runtime —
the runtime never sees a manifest that violates them, since it ships
pre-validated in the wheel:

| Rule | Enforced by |
|---|---|
| Version agreement — every entry's `base_format_version` equals `SUPPORTED_BASE_FORMAT_VERSION` | Hygiene test — [`tests/datasets/test_manifest_hygiene.py`](../../tests/datasets/test_manifest_hygiene.py) |
| Command/config coherence — every `{dir}/`-prefixed `.yaml` reference in a command (`=`-attached forms included) names a file in that entry's `configs` | Hygiene test — same module |
| Pack completeness — bundle triple present; every `configs` file exists in the example dir | Pack builder |
| Pack version — the pack's `base.json` version equals the supported constant | Pack builder (via `open_emit`) |

Of the stamped fields, only `base_format_version` is mechanically enforced
before a wheel ships: the hygiene test runs offline against a repo that holds
no archive, so it *cannot* verify `sha256`/`size_bytes` against the released
asset. For those two, "stamped, never hand-typed" is a steady-state
discipline — committed values are always a paste of builder output — and the
discipline is backstopped, not blind: a wrong pin makes every `get` fail
sha256 verification loudly.

## Rationale

- **GitHub Releases, not git.** Committing 1–44 MB bundles bloats history
  permanently; Git LFS bills bandwidth on exactly the free-public-download
  pattern this surface serves; release assets serve anonymous GET for free.
- **An authored allowlist, not a scan.** The manifest is authored, never
  derived by scanning `docs/examples/` — not every example qualifies for
  publication, and absence-as-exclusion keeps the decision editorial.
- **Manifest in the wheel.** Shipping the manifest beside
  `SUPPORTED_BASE_FORMAT_VERSION` in one wheel is what makes version agreement
  a build-time property rather than a runtime check: the pair can only ever
  ship in a state the hygiene test has passed.
- **The sha256 pin over trust in the URL.** The URL names a location; the
  digest names the content. Only the latter is stable under asset replacement.
- **Host-agnostic by field shape.** `url` is any anonymous-GET https URL, so
  publishing a pack on an additional host is an additive per-entry fact, not a
  design change.

## Boundaries

- **Logistics only.** Exporters, corrupters, streaming, compare, and playback
  are untouched by this surface: the downloaded pack is an ordinary emit
  directory plus ordinary config files, consumed through the same verbs as any
  other.
- **Publication venue is not contract.** The surface owns fetch + verify +
  extract against a manifest URL; where archives are hosted, uploaded, and
  tagged is maintainer process outside the code.
- **The recipe set is a separate pillar.** Recipes (minimal, fixture-guarded,
  feature-teaching — `docs/recipes/`) and these domain-example configs serve
  different purposes; neither replaces the other.
- **Commands are printed, never executed.** The tool ends at printing the
  entry's next-step commands; running them is the user's act.
- **The builder is repo-side.** It imports `open_emit` and reads
  `docs/examples/`; it is not part of the installed package's surface.

## Related

| Document | Why |
|---|---|
| [`reader.md`](reader.md) | `open_emit` — the version gate the builder publishes through and every extracted pack is consumed through |
| [`notices.md`](notices.md) | The notice channel this surface deliberately does not emit into (no emit, no plan) |
| [`../CLAUDE.md`](../CLAUDE.md) | The bundle boundary distribution moves bytes across without extending |
