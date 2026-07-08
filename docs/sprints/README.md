# Sprints

Sprint scope lives here, one directory per sprint: `docs/sprints/<sprint-name>/`.

A sprint defines **what gets built now**. It is independent of architecture docs, which capture **how a subsystem works** (see [`../PROCESS.md`](../PROCESS.md) § Sprints vs Design).

## Sprint directory layout

```
docs/sprints/<sprint-name>/
  spec.md       ← the sprint plan: goal, phases, per-phase contracts and gates
  state.yaml    ← machine-readable status: current phase, gates, demo paths, parent branch
  review.md     ← review findings recorded during implementation
  demos/        ← per-phase demo scripts proving each phase works end-to-end
```

- `/create-sprint` (or `/create-sprint-from-pending`) scaffolds a new `<sprint-name>/` and commits it on the parent branch.
- `/implement-sprint` and `/ship-pending` drive a scaffolded sprint phase-by-phase, updating `state.yaml` and committing demos.

## Conventions

- **Active vs. complete is tracked per sprint in `state.yaml`**, not by a `current/` vs `archive/` directory split.
- A sprint directory is committed at its parent branch HEAD before implementation begins, so the worktree-based implement loop can read the spec from the parent.
- Sprint names are short, hyphenated, and describe the deliverable (e.g. `dimensional-fk-resolution`), not a date.

No sprints have run in this repo yet. The first standalone sprint creates the first `<sprint-name>/` directory here.
