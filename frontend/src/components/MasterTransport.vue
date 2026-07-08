<script setup lang="ts">
import { computed } from 'vue'
import type { ConsumerMeters, Meters, Transport } from '../api'

const props = defineProps<{
  transport: Transport
  meters: Meters | null
  // Consumer side — null when the backend ran producer-only.
  consumerMeters: ConsumerMeters | null
}>()

const emit = defineEmits<{
  (e: 'change', patch: Partial<Transport>): void
}>()

// Speed slider is linear in log space across 4 decades: 0.1x .. 1000x.
const DECADES = 4
const MIN_LOG = -1 // log10(0.1)
function speedToPos(speed: number): number {
  return (Math.log10(speed) - MIN_LOG) / DECADES
}
function posToSpeed(pos: number): number {
  return 10 ** (MIN_LOG + pos * DECADES)
}

const sliderPos = computed(() => speedToPos(props.transport.speed))

function onSpeed(e: Event) {
  const pos = Number((e.target as HTMLInputElement).value)
  emit('change', { speed: Number(posToSpeed(pos).toPrecision(3)) })
}
function togglePlay() {
  emit('change', { playing: !props.transport.playing })
}

const frontier = computed(() => {
  const f = props.meters?.frontier_sim_time
  return f ? f.replace('T', ' ').slice(0, 19) : '—'
})
const elapsed = computed(() => {
  const ms = props.meters?.wall_elapsed_ms ?? 0
  const s = Math.floor(ms / 1000)
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
})
const watermark = computed(() => {
  const w = props.consumerMeters?.global_watermark_sim_time
  return w ? w.replace('T', ' ').slice(0, 19) : '—'
})
</script>

<template>
  <section class="master">
    <button class="transport" :class="{ playing: transport.playing }" @click="togglePlay">
      {{ transport.playing ? '⏸ pause' : '▶ play' }}
    </button>

    <label class="speed">
      <div class="speed-head"><span>master speed</span><span class="speed-val">{{ transport.speed }}×</span></div>
      <input type="range" min="0" max="1" step="0.001" :value="sliderPos" aria-label="master speed" @input="onSpeed" />
    </label>

    <div class="readouts">
      <div class="readout"><span>frontier</span><code>{{ frontier }}</code></div>
      <div v-if="consumerMeters" class="readout">
        <span>watermark</span><code>{{ watermark }}</code>
      </div>
      <div class="readout"><span>elapsed</span><code>{{ elapsed }}</code></div>
    </div>
  </section>
</template>

<style scoped>
.master {
  display: flex; align-items: center; gap: 28px; flex-wrap: wrap;
  padding: 16px 20px; background: var(--panel);
  border: 1px solid var(--line); border-radius: 12px;
}
.transport {
  padding: 12px 22px; font-size: 16px; font-weight: 600; cursor: pointer;
  border-radius: 8px; border: 1px solid var(--line); background: #161b22; color: var(--fg);
  min-width: 120px;
}
.transport.playing { background: #238636; border-color: #238636; color: #fff; }
.speed { display: flex; flex-direction: column; gap: 6px; min-width: 280px; flex: 1; }
.speed-head { display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); }
.speed-val { color: var(--fg); font-variant-numeric: tabular-nums; font-weight: 600; }
.speed input { width: 100%; accent-color: #2f81f7; cursor: pointer; }
.readouts { display: flex; gap: 24px; }
.readout { display: flex; flex-direction: column; gap: 2px; font-size: 12px; color: var(--muted); }
.readout code { color: var(--fg); font-size: 14px; font-variant-numeric: tabular-nums; }
</style>
