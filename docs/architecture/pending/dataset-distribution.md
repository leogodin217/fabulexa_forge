---
status: draft
---

# Dataset Distribution

Ship real producer-emitted example datasets — bundle + authored configs — as
self-contained packs on GitHub Releases, named by a manifest baked into the
wheel, reachable through `fabulexa-forge datasets list|get` without a repo
checkout.

Origin: finding `ship-real-producer-datasets-outside-git`.

---

## Problem

The example datasets under `docs/examples/<domain>/` — authored export /
corrupt / stream configs beside a producer-emitted bundle (`run.duckdb` +
`base.json` + `ATLAS.md`) — are the first real datasets downstream users need.
But the bundles are gitignored binaries (1–44 MB each), and the audience
installs the package rather than cloning the repo. Someone who
`pip install fabulexa-forge` gets a CLI that can export, corrupt, and stream —
and no emit to run it against:

```console
$ fabulexa-forge export ./bundle dimensional.yaml out --fmt duckdb
# ...they have neither ./bundle nor dimensional.yaml, and nothing in the
# tool tells them real datasets exist or where to get them.
```

They have no producer access; the test fixtures are synthesized and test-only;
committing bundles to git bloats history permanently; Git LFS bills bandwidth
on exactly the free-public-download pattern this feature exists to serve.

## Solution

A dataset-distribution surface with three parts:

1. **Manifest** — an authored allowlist of participating datasets, shipped as
   package data inside the wheel. Per entry: name, description, download URL,
   sha256, size, the pack's `base_format_version`, the config files the pack
   carries, and the example command lines to print after download. Authored,
   never derived by scanning `docs/examples/` (not every example qualifies).
2. **Pack** — one archive per dataset attached to a GitHub Release, built at
   release time from `docs/examples/<name>/`: the `bundle/` directory plus
   exactly the config YAMLs the manifest entry names. Self-contained — an
   installed user needs nothing from git.
3. **CLI** — `fabulexa-forge datasets list` (fully offline, reads the
   manifest) and `fabulexa-forge datasets get <name>` (anonymous HTTPS
   download, sha256 verification, safe extraction, then the entry's example
   commands printed ready to run).

```
wheel ──────────────┐            GitHub Release ─────────────┐
│ code              │            │ nhs.tar.gz                │
│ SUPPORTED_..._VER │            │ retail.tar.gz             │
│ datasets manifest │──"nhs"──▶  │   bundle/run.duckdb       │
└───────────────────┘   HTTPS    │   bundle/base.json        │
                        GET      │   bundle/ATLAS.md         │
  datasets list  (offline)       │   dimensional.yaml, ...   │
  datasets get   (fetch+verify)  └───────────────────────────┘
```

Because the manifest ships in the same wheel as
`SUPPORTED_BASE_FORMAT_VERSION`, and a release-hygiene test pins every entry's
`base_format_version` to that constant, an install can only ever list datasets
its own reader opens. A contract-version bump turns stale datasets into a red
hygiene test at bump time (naming each stale entry) — never into a runtime
surprise for an installed user.

Publishing beyond GitHub Releases — the origin finding's Hugging Face /
Kaggle dual-publish prong (free storage plus discovery for the student / ML
audience) — is **explicitly deferred**. The manifest is already host-agnostic
(`url` is any anonymous-GET https URL), so adding another host later is an
additive per-entry fact, not a design change.

## Affected Subsystems

- **CLI** — gains a `datasets` verb, the first verb with sub-verbs (`list`,
  `get`). One entry in the verb registry; the sub-verb split is parsed inside
  the handler. Exit-code contract matches the existing verbs: 0 success, 1
  failure, 2 usage error. A missing or unknown sub-verb, like any bad flag,
  is a usage error — argparse-shaped usage text to stderr, exit 2 (the
  argparse precedent, not the top-level unknown-verb exit 1). Progress and
  diagnostics go to stderr; the payload (listing, next-step commands) goes to
  stdout.
- **Dataset distribution (new subsystem)** — the manifest models + loader
  (package data via `importlib.resources`), and the fetch/verify/extract
  surface with an injectable transport so tests never touch the network.
- **Packaging / release process** — the wheel build force-includes the
  manifest as package data (same mechanism as the vendored contract schema).
  A repo-side pack-builder tool assembles release archives from
  `docs/examples/<name>/` and stamps manifest entries; a hygiene test guards
  manifest ↔ code-version agreement.
- **Examples convention** — `docs/examples/<name>/` becomes the source tree
  packs are built from. The anticipated-but-unimplemented
  `docs/examples/*/published/` convention (sidecar + atlas committed beside a
  release-shipped binary) is superseded: the pack carries `base.json` and
  `ATLAS.md`, so committed copies would be redundant duplicates.

## What Doesn't Change

- **The reader and its version gate.** `open_emit` remains the sole authority
  that refuses an unknown `base_format_version`. This design adds no second
  runtime version check. The manifest's *version field* is mechanically
  enforced (the hygiene test pins it to `SUPPORTED_BASE_FORMAT_VERSION`);
  that the *downloaded bytes* match that field rests on the stamp discipline
  (committed `sha256`/`size_bytes` are always a paste of builder output, and
  the builder refuses a wrong-version pack) — a discipline lapse is not
  silent: a wrong pin fails `get`'s sha256 verification loudly, and
  wrong-version bytes behind a correct pin still hit the reader's own gate at
  first use.
- **Exporters, corrupters, streaming, compare, playback** — untouched. The
  downloaded pack is an ordinary emit directory plus ordinary config files.
- **Test fixtures stay synthesized and CI stays offline.** Downloaded datasets
  are a user-facing asset, never a test dependency. The fetch surface is
  tested through the injectable transport against local bytes.
- **No new runtime dependencies.** Download (`urllib`), hashing (`hashlib`),
  and extraction (`tarfile`) are stdlib.
- **The notice channel.** The notice channel remains the package's one
  informational output surface for plan/compile-time facts of a run over an
  emit. `datasets` opens no emit and compiles no plan, so its population is
  empty there by construction: download progress and diagnostics are CLI
  presentation on stderr, not `Notice` records. The two claims compose —
  nothing informational about an emit run bypasses the channel.
- **Recipes.** The recipe set (minimal, fixture-guarded, feature-teaching)
  is a separate documentation pillar from these domain-example configs.
- **The bundle boundary.** Distribution is logistics for bytes already shaped
  by the contract; no new coupling to the producer.

## Semantics

### Manifest

- The manifest is a YAML document shipped as package data and validated
  through Pydantic at load. Loading is offline and deterministic.
- Entries are an authored allowlist. An example directory participates only
  by having an entry; absence is the only exclusion mechanism.
- Entry order is authored order and is preserved everywhere (listing output,
  iteration) — no re-sorting.
- `base_format_version` on an entry is a **recorded fact about the pack**,
  stamped by the pack builder from the pack's own `base.json` (read through
  `open_emit`, never parsed ad hoc) — never hand-typed. The single version
  authority remains
  `SUPPORTED_BASE_FORMAT_VERSION`; the hygiene test asserts every entry equals
  it, so the wheel can never ship an entry its own reader would refuse.

### `datasets list`

| Condition | Result |
|-----------|--------|
| No arguments | Every manifest entry, authored order, to stdout: name, size (human-readable, rendered from `size_bytes`), `base_format_version`, config coverage (the entry's `configs` filenames verbatim, authored order — a display of the list, not a derivation), description. Exit 0. |
| `--format json` | The manifest itself as a byte-stable JSON document: the model's field set verbatim (`datasets` list of entries, each with `name`, `description`, `url`, `sha256`, `size_bytes`, `base_format_version`, `configs`, `commands`), raw values (`size_bytes` as the integer, never the human rendering), entries in authored order, keys sorted, separators `(",", ":")`, no trailing newline. Exit 0. |
| Empty manifest | Text format: a clear "no datasets published for this version" line to stdout. `--format json`: the model document verbatim (`{"datasets":[]}`) — the empty-catalog line is human presentation only. Exit 0 either way — an empty catalog is not an error. |
| Network unavailable | Irrelevant — `list` performs no network I/O. |

### `datasets get <name>`

Pipeline: resolve entry → check target path (refuse if occupied without
`--force`; nothing mutated yet) → download to a temporary file → verify
(byte count, sha256, then every archive member's safety) → prepare target
directory (`--force` removal happens here, only after all verification
passes) → extract → delete the temporary archive → print next steps. The
target path is never touched before the downloaded archive fully verifies:
every pre-extraction failure leaves it exactly as found. The temporary
archive never survives `get` — it is deleted on success and on every failure
path alike.

| Condition | Result |
|-----------|--------|
| `<name>` not in manifest | Error to stderr naming the unknown dataset and listing valid names. Exit 1. Nothing downloaded. |
| Target path (default `./<name>`, or `--dir DIR`) absent | Proceed; the directory is created (parents included) after verification, before extraction. |
| Target path is an empty directory | Proceed. |
| Target path exists and is occupied (a non-empty directory, or a non-directory), no `--force` | Refuse with an error naming the path. Exit 1. Nothing downloaded. |
| Target path exists and is occupied, `--force` | Proceed; the existing path (directory tree or file) is removed and the directory recreated only after download and verification succeed, immediately before extraction. |
| Download fails (connection, HTTP error, timeout) | Error to stderr naming the URL and cause. Exit 1. Temporary file removed; target path left exactly as found (a `--force` target is not yet removed). |
| Downloaded byte count ≠ `size_bytes` | Error to stderr naming the dataset, expected and actual byte counts. Exit 1. Temporary file removed; nothing extracted; target path left as found. |
| sha256 of downloaded bytes ≠ manifest `sha256` | Error to stderr naming the dataset, expected and actual digests. Exit 1. Temporary file removed; nothing extracted; target path left as found. |
| Archive member unsafe — path escapes the target directory (absolute path or `..`), or the member is not a regular file or directory (link, device, fifo) | Extraction refused with an error naming the member. Every member is checked before any is extracted, so nothing has been written and the target path is left as found (a `--force` target is not yet removed). Exit 1. |
| Success | Pack content extracted directly into the target directory (archive members are relative, no wrapper directory). The entry's example commands printed to stdout with the `{dir}` placeholder replaced by the target path as resolved for extraction — the `--dir` value verbatim, or the `./<name>` default — never absolutized. Exit 0. |

- Download progress goes to stderr (byte counts are known from `size_bytes`);
  it is presentation, not contract.
- The `OSError` → `DatasetError` mapping boundary is a *phase*, not a source:
  any `OSError` raised during the download-and-verify phase — transport open,
  transport read, and temporary-file write alike (the two are one streaming
  loop; attributing an `OSError` to one side is not implementable) — maps to
  `DatasetError` ("Download fails" above). Any `OSError` raised *after*
  verification (target preparation, `--force` removal, extraction — disk
  full, permissions) is outside the `DatasetError` population and outside the
  atomicity guarantee: it propagates unwrapped and the target may hold a
  partial tree. This matches the existing verbs' posture toward environmental
  I/O failure; only pre-extraction failures get the mapped diagnostic.
- The CLI-wired transport applies a network timeout so a dead connection
  fails instead of hanging. The timeout's value is CLI presentation, not
  contract or config; its expiry surfaces as `OSError` inside the
  download-and-verify phase and maps like any other download failure.
- `sha256` is the authority on content identity. GitHub release assets can be
  replaced under the same tag; the pin makes a swap a loud verification
  failure instead of a silent content change.
- The printed example commands are documentation output, not configuration:
  the tool never executes them and they carry no defaults into any config
  surface. They are authored per entry precisely so flag choices (`--fmt`,
  anchor flags, sink flags) are a maintainer decision, not a code invention.

### Pack

- Layout: `bundle/run.duckdb`, `bundle/base.json`, `bundle/ATLAS.md`, and the
  manifest-named config YAMLs at the archive root. All member paths relative;
  no wrapper directory; no `exports/`, no demo-glue files (e.g. the mixer
  demo preset, which the CLI never reads).
- Format: gzip-compressed tar. One archive per dataset per release;
  the manifest URL pins the exact asset.
- Members are regular files and directories only — no links, devices, or
  fifos. The builder can produce nothing else; `get` refuses any non-regular
  member (the unsafe-archive condition above), so the safety property holds
  even against a hand-built archive.
- A pack is self-describing to the existing toolchain: after extraction,
  `<dir>/bundle` is a valid emit directory and each config runs against it
  with the shipped CLI verbs.

### Pack builder (repo-side tool)

| Condition | Result |
|-----------|--------|
| `docs/examples/<name>/bundle/` missing any of `run.duckdb` / `base.json` / `ATLAS.md` | Refuse, naming the missing file. |
| A manifest-entry config file absent from `docs/examples/<name>/` | Refuse, naming the file. |
| Bundle refuses to open under `open_emit` — including a `base_format_version` other than `SUPPORTED_BASE_FORMAT_VERSION` | Refuse — a pack is only publishable for the version the accompanying wheel supports. The version refusal renders the reader's `UnsupportedBaseFormatVersionError.found_version`. |
| Success | Deterministic archive written; sha256 + size computed; the manifest entry's stamped fields (`sha256`, `size_bytes`, `base_format_version`) printed to stdout as a paste-ready YAML fragment for the maintainer to commit. |

- **Invocation.** A repo-side tool run through this repo's venv (`uv run` —
  it imports `open_emit`), never shipped in the wheel. It takes the dataset
  `<name>` (one entry per invocation) and `--out DIR`; the archive is written
  to `<out>/<name>.tar.gz`. Refusals go to stderr, exit 1.
- **The entry comes first; the builder stamps it.** The build is driven by
  the entry's *authored* fields: `name` locates `docs/examples/<name>/`,
  `configs` names the YAMLs packed. The *stamped* fields (`sha256`,
  `size_bytes`, `base_format_version`) are ignored on read and recomputed —
  a first-time entry is authored with any syntactically valid values there.
  Of the three, only `base_format_version` is mechanically enforced before a
  wheel ships (the hygiene test pins it to the supported constant); the
  hygiene test runs offline against a repo that holds no archive, so it
  *cannot* verify `sha256`/`size_bytes` against the released asset. For those
  two, "stamped, never hand-typed" is the steady-state *discipline*:
  committed values are always a paste of builder output. The discipline is
  backstopped, not blind — a wrong pin makes every `get` fail sha256
  verification loudly.
- **Print, never edit.** The builder emits the stamped fields to stdout; it
  never rewrites the manifest in place. The manifest stays a hand-authored
  document with builder-supplied values pasted in.
- **Reader-first, even repo-side.** The builder never parses `base.json` ad
  hoc: it opens the bundle through `open_emit` — the sole sanctioned path —
  and stamps `base_format_version` from the opened sidecar. The reader's
  version gate is therefore also the builder's publishability gate; a
  mismatched bundle surfaces as the gate's own error, whose `found_version`
  the refusal message renders.
- **Deterministic archive** means byte-identical: same input tree → the same
  bytes → the same `sha256`, so a rebuild never silently forces a manifest
  re-stamp. Normalized: members added in sorted-path order; member `mtime` 0,
  `uid`/`gid` 0, `uname`/`gname` empty; mode 0644 for files, 0755 for
  directories; gzip stream with `mtime` 0 and an empty original-filename
  field.

Publishing a dataset is therefore: author configs in `docs/examples/<name>/`
→ run the pack builder → upload the archive to the release → commit the
manifest entry. Uploading and tagging stay manual/`gh`-driven; the tool never
talks to the network.

### Invariants

- **Determinism.** Same manifest + same downloaded bytes → identical
  extracted tree and identical stdout. `list` output is a pure function of
  the manifest.
- **Offline listing.** `list` performs no network I/O, ever.
- **Version agreement by construction.** Every manifest entry in a shipped
  wheel satisfies `base_format_version == SUPPORTED_BASE_FORMAT_VERSION`,
  enforced by the hygiene test, fed by the builder's stamp-from-the-pack rule.
- **Content pinning.** No downloaded byte is trusted before its sha256
  matches the manifest.
- **Failure atomicity.** `get` mutates the target path only after the
  downloaded archive fully verifies (byte count, sha256, member safety);
  every earlier failure leaves the path exactly as found.
- **No temporary residue.** The temporary archive is deleted on success and
  on every failure path; `get` never leaves it behind.
- **No network in tests.** All fetch-path tests run through the transport
  seam against local bytes.

## Configuration

There is no author-facing (export-config) surface. The manifest is
maintainer-authored package data:

```yaml
# Dataset manifest (package data — ships inside the wheel)
datasets:
  - name: nhs
    description: NHS elective-care pathway simulation — patients, consultants,
      clinics, referral-to-treatment pathways.
    url: https://github.com/<org>/fabulexa-forge/releases/download/v0.1.0/nhs.tar.gz
    sha256: 9f2c…e41a          # stamped by the pack builder
    size_bytes: 20873216        # stamped by the pack builder
    base_format_version: 8      # stamped from the pack's own base.json
    configs:
      - dimensional.yaml
      - source.yaml
      - base.yaml
      - stream.yaml
      - corrupt.yaml
    commands:
      - "fabulexa-forge export {dir}/bundle {dir}/dimensional.yaml {dir}/exports/dimensional --fmt duckdb"
      - "fabulexa-forge export {dir}/bundle {dir}/source.yaml {dir}/exports/source --fmt csv"
      - "fabulexa-forge stream {dir}/bundle {dir}/stream.yaml --fmt jsonl --sink stdout"
      - "fabulexa-forge corrupt {dir}/bundle --config {dir}/corrupt.yaml --out {dir}/corrupted"
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | slug (`[a-z0-9]+(-[a-z0-9]+)*`) | Yes | Dataset identity; the `get` argument and default target directory name. Unique. |
| `description` | str | Yes | One- or two-sentence domain description shown by `list`. |
| `url` | https URL | Yes | The release asset. Anonymous GET must serve it. |
| `sha256` | 64-hex | Yes | Digest of the archive bytes. Stamped by the builder. |
| `size_bytes` | int > 0 | Yes | Archive size. Stamped by the builder; drives progress reporting and the post-download byte-count verification. |
| `base_format_version` | int ≥ 1 | Yes | The pack's format version. Stamped from the pack's `base.json`. |
| `configs` | list[str] | Yes, non-empty | Config filenames the pack carries (also `list`'s coverage display). Bare `*.yaml` filenames — no path separators; packs are flat at the archive root. |
| `commands` | list[str] | Yes, non-empty | Example command lines printed after `get`; each must contain `{dir}`. |

## Interface Contracts

### Config Models

```python
class DatasetEntry(StrictBaseModel):
    """One published dataset: identity, pinned bytes, pack contents, next steps."""

    name: str
    description: str
    url: str
    sha256: str
    size_bytes: int
    base_format_version: int
    configs: list[str]
    commands: list[str]


class DatasetManifest(StrictBaseModel):
    """The authored allowlist of published datasets, in authored order."""

    datasets: list[DatasetEntry]
```

### Runtime Types

```python
Transport = Callable[[str], BinaryIO]
"""Opens a URL for streamed reading. The CLI wires the stdlib HTTPS opener
with a network timeout (value is CLI presentation, not contract); tests wire
local-bytes openers. The seam that keeps the suite offline.

Failure contract: a failed open or read — timeout included — raises OSError
(which subsumes urllib's URLError/HTTPError). get_dataset maps OSError by
phase, not source: any OSError during download-and-verify (transport open,
transport read, temporary-file write — one streaming loop) becomes
DatasetError; nothing broader is caught, and post-verification OSError
propagates unwrapped."""


@dataclass(frozen=True)
class GetResult:
    """Outcome of a successful dataset get.

    Attributes:
        target_dir: Directory the pack was extracted into.
        commands: The entry's example commands with {dir} substituted.
    """

    target_dir: Path
    commands: tuple[str, ...]


class DatasetError(Exception):
    """A dataset operation failed: unknown name, occupied target, download
    failure, verification mismatch, or unsafe archive. The message is the
    user-facing diagnostic; the CLI maps it to stderr + exit 1."""
```

### Functions

```python
def load_manifest() -> DatasetManifest:
    """Load and validate the dataset manifest shipped as package data.

    Returns:
        The validated manifest, entries in authored order.

    Raises:
        ValidationError: If the packaged manifest does not satisfy the model.
        yaml.YAMLError: If the packaged document is not parseable YAML,
            propagated from the parser.
        (Both unreachable in a released wheel — the hygiene test loads the
        manifest and gates the build — but loud during development.)
    """


def get_dataset(
    manifest: DatasetManifest,
    name: str,
    target_dir: Path | None,
    force: bool,
    transport: Transport,
    progress: Callable[[int, int], None] | None,
) -> GetResult:
    """Download, verify, and extract one published dataset pack.

    Args:
        manifest: The loaded manifest.
        name: Dataset to fetch; must match an entry's name.
        target_dir: Extraction directory; None means ./<name>. Created
            (parents included) when absent; the path as given here is what
            {dir} substitutes to — never absolutized.
        force: If True, an occupied target path (non-empty directory or
            non-directory) is removed and the directory recreated — only
            after the downloaded archive fully verifies (size, sha256,
            member safety), never before; if False, that condition refuses
            up front, before any download.
        transport: URL opener (HTTPS in the CLI, local bytes in tests).
        progress: Optional callback (bytes_received, size_bytes) for
            presentation-only progress reporting.

    Returns:
        GetResult with the target directory and substituted example commands.

    Raises:
        DatasetError: Unknown name; occupied target without force; download
            failure (any OSError during the download-and-verify phase —
            transport open/read and temporary-file write alike — mapped);
            size or sha256 mismatch; unsafe archive member (path escaping
            the target directory, or a non-regular member). All
            pre-extraction failures leave the target path exactly as found
            and the temporary archive deleted.
        OSError: An I/O failure after verification (target preparation or
            extraction), propagated unwrapped — environmental, not a
            dataset-contract failure; the target may hold a partial tree.
    """


def render_dataset_listing(manifest: DatasetManifest, fmt: str) -> str:
    """Render the manifest as the `datasets list` payload.

    Args:
        manifest: The loaded manifest.
        fmt: "text" for the human table, "json" for the byte-stable
            document (the manifest's field set verbatim, raw values,
            authored entry order, sorted keys, separators (",", ":")).
            An empty manifest renders the no-datasets line under "text"
            and the model document verbatim under "json".

    Returns:
        The complete stdout payload, without trailing newline.
    """
```

The `datasets` verb handler follows the existing `Verb` registry shape: it
parses the sub-verb (`list` / `get`) and flags, wires the stdlib transport,
calls the functions above, and maps `DatasetError` to stderr + exit 1.

## Validation Rules

### Parse-Time (Pydantic)

```python
@model_validator(mode="after")
def entry_well_formed(self) -> Self:
    """name matches `[a-z0-9]+(-[a-z0-9]+)*` (lowercase alphanumeric runs
    separated by single hyphens); url is https; sha256 is 64 lowercase hex;
    size_bytes > 0; base_format_version >= 1; configs and commands non-empty;
    every configs entry is a bare filename ending '.yaml' with no path
    separator ('/' or '\\\\'); every command contains '{dir}' at least once,
    and every brace-delimited run in it (each match of `\\{[^{}]*\\}`) is
    exactly '{dir}' — no other placeholder exists."""


@model_validator(mode="after")
def names_unique(self) -> Self:
    """Manifest entry names are unique."""
```

### Business Rules

Enforced by tests and the pack builder, not at runtime (the runtime never
sees a manifest that violates them — it ships pre-validated in the wheel):

| Rule | Checks | Error Message |
|------|--------|---------------|
| Version agreement (hygiene test) | Every entry's `base_format_version == SUPPORTED_BASE_FORMAT_VERSION` | `"dataset {name} is stale: pack is v{entry}, wheel supports v{supported}"` |
| Command/config coherence (hygiene test) | A command references a config file via a `{dir}/`-prefixed path: at every occurrence of `{dir}/` in a command, the path run it starts (through the end of the maximal non-whitespace run containing it — so `=`-attached forms like `--config={dir}/x.yaml` are covered) whose final path segment ends in `.yaml` names a file that appears in that entry's `configs` | `"dataset {name}: command references {file} not in configs"` |
| Pack completeness (builder) | Bundle triple present; every `configs` file exists in the example dir | `"dataset {name}: missing {file}"` |
| Pack version (builder) | Pack's `base.json` version equals the supported constant | `"dataset {name}: bundle is v{found}, cannot publish for a v{supported} wheel"` |
