# FabulMixer — live-perform board (POC frontend)

A throwaway-grade control surface for the FabulMixer live-perform POC: a channel
strip per topic (vertical **rate** fader, horizontal **lag** slider, **mute** toggle)
plus a master transport (**play/pause**, log-scale **speed**) and read-only meters
(backlog, delivery-lag, watermark).

This is an **app over the library**, outside the exporter package boundary. It talks
only to the HTTP control API — never the bundle or `contract/`. It adds no dependency
to the Python package.

## Run

```bash
cd frontend
npm install          # one runtime dep (vue) + vite/tsc dev tooling
npm run dev          # http://localhost:5173
```

Vue 3 + TypeScript + Vite. Build/typecheck: `npm run build`.

## Mock vs live backend

By default the board runs against an **in-memory simulated frontier** — no backend
needed. Turn a lag fader up and watch that strip's backlog climb and its watermark
stall; mute a strip and its backlog runs away; raise master speed to drain. This is
the money demo, performable client-side today.

Point it at the real FabulMixer driver once that lands:

```bash
VITE_API_MODE=http npm run dev   # proxies /api -> http://localhost:8765 (vite.config.ts)
```

The two implementations satisfy one interface (`src/api/types.ts` → `FabulMixerApi`);
nothing else in the app changes.

## The contract

The HTTP API is the frozen seam between this frontend and the backend driver:
[`../docs/architecture/pending/fabulmixer-control-api.md`](../docs/architecture/pending/fabulmixer-control-api.md).
`src/api/types.ts` is its TypeScript mirror.

## Layout

```
src/
  api/        types.ts (contract mirror) · mockApi.ts · httpApi.ts · index.ts (selector)
  composables/useControlBoard.ts   reactive store, 5 Hz meter poll, throttled dial writes
  components/ MasterTransport.vue · ChannelStrip.vue · MeterBar.vue
  App.vue     the board
```
