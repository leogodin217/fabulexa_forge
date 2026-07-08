<script setup lang="ts">
import { computed } from 'vue'
import type { ConsumerMeters } from '../api'
import MeterBar from './MeterBar.vue'

const props = defineProps<{
  meters: ConsumerMeters
}>()

const windows = computed(() => props.meters.windows)
const joins = computed(() => props.meters.joins)

function sizeLabel(ms: number): string {
  if (ms % 3_600_000 === 0) return `${ms / 3_600_000} h`
  if (ms % 60_000 === 0) return `${ms / 60_000} min`
  return `${Math.round(ms / 1000)} s`
}
function endLabel(iso: string | null): string {
  return iso ? iso.slice(11, 19) : '—'
}
function pct(rate: number | null): number {
  return rate === null ? 0 : rate * 100
}
function rateLabel(rate: number | null): string {
  return rate === null ? '—' : `${(rate * 100).toFixed(0)}%`
}
</script>

<template>
  <section class="consumer-panel">
    <header class="cp-head">consumer pipeline</header>
    <div class="cp-cols">
      <div v-if="windows.length" class="cp-group">
        <div class="cp-group-cap">tumbling windows</div>
        <div v-for="(w, i) in windows" :key="i" class="cp-row">
          <span class="cp-row-name">{{ sizeLabel(w.size_ms) }} window</span>
          <span class="cp-row-stat">fired <code>{{ w.fired_count }}</code></span>
          <span class="cp-row-stat">latest end <code>{{ endLabel(w.latest_window_end_sim_time) }}</code></span>
        </div>
      </div>

      <div v-if="joins.length" class="cp-group">
        <div class="cp-group-cap">enrichment joins</div>
        <div v-for="(j, i) in joins" :key="i" class="cp-join">
          <div class="cp-join-name">
            <code>{{ j.fact_topic }}</code> ⋈ <code>{{ j.dimension_topic }}</code>
          </div>
          <MeterBar :label="`null-rate ${rateLabel(j.null_rate)}`" :value="pct(j.null_rate)" :max="100" unit="%" />
          <div class="cp-join-counts">
            facts <code>{{ j.fact_count }}</code> · nulls <code>{{ j.null_count }}</code>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.consumer-panel {
  padding: 14px 18px; background: var(--panel);
  border: 1px solid var(--line); border-radius: 12px;
  display: flex; flex-direction: column; gap: 12px;
}
.cp-head {
  font-size: 12px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.06em;
}
.cp-cols { display: flex; gap: 36px; flex-wrap: wrap; }
.cp-group { display: flex; flex-direction: column; gap: 8px; min-width: 260px; flex: 1; }
.cp-group-cap { font-size: 11px; color: var(--muted); }
.cp-row { display: flex; gap: 16px; align-items: baseline; font-size: 12px; color: var(--muted); }
.cp-row-name { color: var(--fg); }
.cp-row-stat code, .cp-join-counts code, .cp-join-name code {
  color: var(--fg); font-variant-numeric: tabular-nums;
}
.cp-join { display: flex; flex-direction: column; gap: 5px; }
.cp-join-name { font-size: 12px; color: var(--muted); }
.cp-join-counts { font-size: 11px; color: var(--muted); }
</style>
