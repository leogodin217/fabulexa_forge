# Vendored base-layer contract

These two files are the **only** coupling between this repo and the Fabulexa
substrate that produces the emits we read:

| File | What it is |
|---|---|
| `base-format.md` | The base-layer format spec (prose) |
| `base-format.schema.json` | The `base.json` sidecar JSON Schema |

## Do not hand-edit

They are **vendored copies** maintained upstream. The canonical contract lives in
the producer repo; a scrubbed copy is regenerated there and pushed here as a
contract-sync PR. Editing them by hand silently breaks the guarantee that this
repo conforms to what producers actually emit.

To change the contract, the change is made upstream and re-synced — it does not
originate here. This repo never reaches up into the producer's tree; the sync is
always a push from upstream into this `contract/` directory.

## Trusting these copies

Within this repo, `contract/` is the source of truth: the reader version-gates
against it, and the docs that describe contract semantics
(`docs/architecture/{anchor,bundle,conformance}.md`) cite only what these files
define. Correctness against the upstream canonical is guaranteed at sync time, not
re-checked here.
