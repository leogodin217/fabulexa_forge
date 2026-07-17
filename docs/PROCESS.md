# Development Process

How we architect, develop, and document Fabulexa Forge.

---

## Design Timing

**Last responsible moment:** Design when you have enough context to decide well—not before, not after.

This is about information, not calendars. Design a subsystem when:
- You understand the problem it solves
- You know the constraints it operates under
- Delaying further would force rework

---

## What Gets Designed When

| Category | When to Design | Examples |
|----------|----------------|----------|
| **Foundational** | Before any implementation touches it | The base reader, the reader→exporter boundary, the export-config schema, core invariants |
| **Structural** | Before implementing the subsystem | A new exporter shape (dimensional, source, streaming), a corrupter family, the anchor/rebasing model |
| **Emergent** | During implementation | Specific column mappings, query-spec shapes, optimizations |

Foundational designs can (and should) exist before sprints that implement them. The design captures the vision; sprints implement pieces of it.

---

## Sprints vs Design

**Sprints** define implementation scope—what gets built now.

**Architecture docs** capture system design—how subsystems work.

These are independent:
- A sprint may implement part of an existing design
- A sprint may require new design work
- Design docs may describe things not yet implemented

See [`sprints/README.md`](sprints/README.md) for the sprint-directory layout.

---

## Code Is Truth

Once implemented, code is the specification.

| Before Implementation | After Implementation |
|-----------------------|----------------------|
| Doc specifies behavior | Code specifies behavior |
| Doc defines interfaces | Code defines interfaces |
| Doc has examples | Tests have examples |

Docs link to code, not duplicate it.

---

## What Lives Where

| Content | Location |
|---------|----------|
| Principles, invariants, boundary, vocabulary | `CLAUDE.md` |
| The input contract (vendored) | `contract/base-format.md` + `.schema.json` |
| Architecture index + staged roadmap | `docs/architecture/README.md` |
| Per-area design | `docs/architecture/*.md` (one per reader/exporter/corrupter area) |
| Informational / speculative design context | Obsidian (`note` skill, area `export`) |
| Sprint scope | `docs/sprints/<sprint>/` |
| Schema | JSON Schema files or code |
| Algorithms | Code + docstrings |
| Examples | Tests |
| Author-facing documentation | Recipes + config-model docstrings — see § Authoring Documentation |

The base-format contract is **external** — vendored, not authored here (CLAUDE.md § The boundary). It is not subject to this process; we adapt to its version bumps, we never redesign it.

---

## Adopting a contract version bump

A `base_format_version` bump is adopted in two phases, each its own unit of work:

1. **Compatibility.** Re-vendor `contract/`, bump the gate
   (`SUPPORTED_BASE_FORMAT_VERSION`), make the fixtures genuinely new-version-shaped,
   run the suite. *Green is the goal* — it means the new format is additively
   compatible and forge is not broken. Every red is a guarantee having changed
   underneath, and each red is a decision to make deliberately, never a test to
   silence. Phase 1 includes the conformance work the bump obliges: forge judges the
   format, so it cannot adopt a version whose guarantees it does not check. Phase 1
   touches the version integer in exactly the sites the hygiene test allowlists —
   the code literal, the architecture README's status row, and `contract/` itself
   (re-vendored wholesale). If the bump tempts you to write 'vN' anywhere else,
   that sentence is delta voice: state the new contract plainly or cite its
   section. The hygiene test failing on a new literal is the mechanism working.
2. **Adoption.** Decide, deliberately, which new capability to *use* (a new
   attribute consulted, a fold redesigned around a new guarantee). Each is its own
   design and its own sprint.

Three costs attend a re-vendor; only the third deserves thought, and the fixture
invariants exist so the first two cannot bury it (see
[`architecture/README.md`](architecture/README.md) § Inputs and fixtures):

| Cost | Contained by |
|---|---|
| Version-integer churn | The single version-literal authority; the never-valid sentinel for gate-negative tests |
| Sidecar-shape churn | The single fixture sidecar writer (`write_emit`, schema-validating) and column constructor (`prop_column`) |
| **Semantic churn** | Nothing — deliberately. A published ground truth that moves (a recipe's `impact` set, a genre reclassification) is the system reporting that the contract changed meaning, and that alarm stays loud |

---

## Note conventions

The `note` skill (Obsidian-backed) tracks findings, features, questions, retros, decisions, and research. Conventions:

- **`area` is required, one value per note.** Notes from this repo use **`export`** (or `cross-cutting` for a note that spans multiple areas). The area vocabulary is a controlled list sourced from the vault (`meta/note-areas.md`); the skill enforces it at creation. To add a finer-grained area, edit that vault file — not `cli.py`.
- **Tags are free-form and topic-only.** Run `note tags` before inventing a new tag — reuse existing ones. `kind`, `severity`, and `discovered-in` are structured fields — never duplicate them as tags. **Separator convention (enforced): tags use `-` only and must not contain `:`; property values (e.g. `discovered-in`) use `__` as the domain/topic separator, never `:`** (Obsidian renders colons as broken links).
- **Cross-note references go in the `related-notes` frontmatter field as wikilinks** (e.g. `"[[other-note]]"`). `note lint` validates that wikilinks resolve.
- **Slug references in prose must use the exact filename slug.** The skill does not lint prose; consistency is convention.

---

## Documentation Lifecycle

### Write (when designing architecture)

- Interfaces and contracts
- Function signatures with detailed docstrings (no implementation)
- Non-obvious decisions and rationale
- Constraints (what we ruled out)
- Invariants (what must always hold)

### Prune (after implementation)

- Schema details → link to schema files
- Algorithm steps → link to code
- Examples → link to tests
- Delete anything code makes obvious

### Keep

- Rationale (why X over Y)
- Constraints (what's explicitly not supported)
- Invariants (rules that must hold)

---

## Folding Pending Information

Pending docs live at `docs/architecture/pending/<name>.md`. When a sprint that implemented a pending doc ships, the information in that doc is folded into the canonical architecture. The `/fold-pending` skill runs this process end to end; this section is the rationale behind it.

**Folding transfers information; it does not promote a file.** The pending doc was a temporary carrier — a single self-contained narrative written for design review. Post-implementation each unit of information has a correct home determined by who owns the contract now, not by where the pending file lived. Nothing "graduates": the pending file is deleted at the end.

Two steps are gated and must not be skipped:

1. **Verify delivery.** Everything the doc claims as shipped must exist in code with the matching contract. If any code is missing or partial, **STOP** and ask what to do.
2. **Re-voice and re-structure.** Folded information is rewritten from delta voice ("X now does Y", "we rejected W") into steady-state voice — a plain statement of current contract — and re-sorted out of the pending doc's narrative arc (Problem → Solution → design) into canonical contract sections (Boundary, Semantics, Invariants, Rationale, Boundaries). A reader who never saw the pending doc must read every folded sentence as settled contract, not proposal.

The deciding question per unit: *who owns this contract now, and where would an implementer or reviewer expect to find it?*

| Content | Destination |
|---|---|
| Semantics extending an existing subsystem | Merged into that subsystem's `docs/architecture/*.md` doc |
| A coherent new contract surface | Its own doc under `docs/architecture/<name>.md`; add to the README reading order |
| Rationale, constraints, invariants, boundaries still load-bearing | Kept (per Documentation Lifecycle § Keep) |
| Schema / field shapes the code restates | Deleted — link to the model |
| Normative or non-obvious algorithms | Kept — state the contract the code conforms to |
| Examples | Deleted — tests are the examples |
| Speculative / forward-notes | Resolved in Obsidian via `note status <slug> complete/answered` |

A large pending doc often **splits**: most sections merge into an existing arch doc, but a coherent subset (its own contract surface) is promoted to a sibling doc rather than buried. Make the split call explicitly before folding — "merge everything into the nearest existing doc" is the default failure mode.

A folded doc — merged or new — targets the canonical section spine in `.claude/skills/fold-pending/templates/canonical-arch-doc.md`. The pending doc carries Interface Contracts and example config in full because no code exists yet; folding prunes those to links.

Mechanical steps after the redistribution decision:

1. Write the new / merged content into its destination doc(s); update the `docs/architecture/README.md` reading order and status table.
2. Resolve Obsidian forward-notes for the shipped area (`note list --tags forward-note --area export`, then `note status … complete/answered`).
3. Delete the `pending/<name>.md` file.
4. Run `note lint` to catch broken `related-notes` wikilinks.

---

## Authoring Documentation

Export config is authored in YAML by non-Python users (CLAUDE.md § Audience). Author-facing documentation rests on two pillars, split by audience and by source-of-truth direction:

- **Recipes** — minimal, single-feature, domain-agnostic export configs, test-guarded against a fixture emit. The primary author-facing doc: an author learns a feature by reading the recipe that uses it. They live at `examples/recipes/<name>/` (streaming recipes nest under `examples/recipes/streaming/<name>/`) with paired tests in `tests/recipes/`, indexed by [`docs/recipes/README.md`](recipes/README.md). Authored via the [`/export-config`](../.claude/skills/export-config/SKILL.md) skill.
- **Model documentation** — the export-config Pydantic models carry the developer reference layer through the three-channel docstring convention (class / attribute / validator). See [`architecture/config-docstrings.md`](architecture/config-docstrings.md). There are deliberately **no field-level author-facing docs** — authors learn fields through recipes.

**Direction of truth.** The config models are authoritative. Any author-facing schema or reference is generated from them, never hand-edited; arch docs link to the model, never restate fields (the duplicate-schema-in-prose anti-pattern).

**Process placement.** Authoring documentation lands *outside* the standard arch-design → sprint cycle, and is independent of folding (folding is unchanged):

- The **model-documentation** pillar lands incrementally by hand — a docstring pass whenever a config model is added or changed, gated by the convention test (`tests/config/test_docstring_convention.py`).
- **Recipe creation** is its own lifecycle step after a feature ships (design → sprint → implement → *create recipes*), warranting a sprint of its own only when the recipe set is large enough to merit one.

---

## Anti-Patterns

| Don't | Why |
|-------|-----|
| Duplicate schema in prose | Drifts from code |
| Write examples in docs | Tests are better examples |
| Keep stale documentation | Misleads future readers |
| Design without understanding | Premature abstraction |
| Delay design until a sprint needs it | Fragments coherent subsystems |
| Fold pending information by moving the file | Skips the redistribution and re-voicing steps; leaks proposal voice into canon |
| Redesign the base-format contract | It is external and vendored — adapt to its versions, never redefine |

---

## Verifying Features

A feature is verified when its behavior is exercised by the conformance suite and the test suite, and `make check` is green (lint + typecheck + conformance + tests). Determinism, faithful reshaping, and version-gating are the invariants worth a dedicated test (CLAUDE.md § Key Invariants).
