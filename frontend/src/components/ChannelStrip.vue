<script setup lang="ts">
import { computed } from 'vue'
import {
  BOUNDS,
  type ConsumerTopicDials,
  type ConsumerTopicMeter,
  type TopicDials,
  type TopicMeter,
} from '../api'
import MeterBar from './MeterBar.vue'

const props = defineProps<{
  dials: TopicDials
  meter: TopicMeter | undefined
  // Consumer side — present only when the backend ran with --consumer.
  consumerDials: ConsumerTopicDials | undefined
  consumerMeter: ConsumerTopicMeter | undefined
}>()

const emit = defineEmits<{
  (e: 'change', patch: Partial<TopicDials>): void
  (e: 'consumerChange', ingest_rate: number): void
}>()

const edge = computed(() => {
  const w = props.meter?.delivery_edge_sim_time
  if (!w) return '—'
  return w.slice(11, 19) // HH:MM:SS of the ISO instant
})

const watermark = computed(() => {
  const w = props.consumerMeter?.watermark_sim_time
  if (!w) return '—'
  return w.slice(11, 19)
})

function onRate(e: Event) {
  emit('change', { rate: Number((e.target as HTMLInputElement).value) })
}
function onLag(e: Event) {
  emit('change', { lag_ms: Number((e.target as HTMLInputElement).value) })
}
function toggleMute() {
  emit('change', { mute: !props.dials.mute })
}

// ingest_rate fader: log-scale across [0.1, 10000], detent 1.0 at 20% of travel.
// The very bottom snaps to 0 — full consumer stall, the symmetric demo gesture
// to the producer's mute.
const ILOG_MIN = Math.log10(0.1)
const ILOG_SPAN = Math.log10(BOUNDS.ingest_rate.max) - ILOG_MIN // 5 decades
function ingestToPos(rate: number): number {
  if (rate <= 0) return 0
  return Math.min(1, Math.max(0, (Math.log10(rate) - ILOG_MIN) / ILOG_SPAN))
}
function posToIngest(pos: number): number {
  if (pos <= 0) return 0
  return 10 ** (ILOG_MIN + pos * ILOG_SPAN)
}
const ingestPos = computed(() => ingestToPos(props.consumerDials?.ingest_rate ?? 0))
const ingestLabel = computed(() => {
  const r = props.consumerDials?.ingest_rate ?? 0
  return r <= 0 ? 'STALL' : `${Number(r.toPrecision(3))}/s`
})

function onIngest(e: Event) {
  const pos = Number((e.target as HTMLInputElement).value)
  emit('consumerChange', Number(posToIngest(pos).toPrecision(3)))
}
</script>

<template>
  <section class="strip" :class="{ muted: dials.mute }">
    <header class="strip-head">
      <span class="dot" :class="dials.content" />
      <span class="strip-title">{{ dials.topic }}</span>
    </header>
    <span class="content-tag">{{ dials.content }}</span>

    <!-- rate: vertical fader, detent at 1x -->
    <div class="fader-wrap">
      <input
        class="fader"
        type="range"
        :min="BOUNDS.rate.min"
        :max="BOUNDS.rate.max"
        step="0.05"
        :value="dials.rate"
        :aria-label="`${dials.topic} release rate`"
        @input="onRate"
      />
      <div class="fader-readout">{{ dials.rate.toFixed(2) }}×</div>
      <div class="fader-cap">rate</div>
    </div>

    <!-- lag: horizontal slider + numeric (the money knob) -->
    <label class="lag">
      <div class="lag-head"><span>lag</span><span class="lag-val">{{ (dials.lag_ms / 1000).toFixed(1) }} s</span></div>
      <input
        type="range"
        :min="BOUNDS.lag_ms.min"
        :max="BOUNDS.lag_ms.max"
        step="1000"
        :value="dials.lag_ms"
        :aria-label="`${dials.topic} delivery lag`"
        @input="onLag"
      />
    </label>

    <button class="mute" :class="{ on: dials.mute }" @click="toggleMute">
      {{ dials.mute ? 'MUTED' : 'mute' }}
    </button>

    <div class="meters">
      <MeterBar label="backlog" :value="meter?.backlog ?? 0" :max="500" unit="" />
      <MeterBar
        label="deliv. lag"
        :value="(meter?.delivery_lag_ms ?? 0) / 1000"
        :max="BOUNDS.lag_ms.max / 1000"
        unit="s"
      />
      <div class="watermark"><span>deliv. edge</span><code>{{ edge }}</code></div>
    </div>

    <!-- consumer side: only when the backend ran with --consumer -->
    <div v-if="consumerDials" class="consumer">
      <div class="consumer-cap">consumer</div>
      <div class="fader-wrap">
        <input
          class="fader ingest"
          type="range"
          min="0"
          max="1"
          step="0.001"
          :value="ingestPos"
          :aria-label="`${dials.topic} ingest rate`"
          @input="onIngest"
        />
        <div class="fader-readout" :class="{ stall: (consumerDials.ingest_rate ?? 0) <= 0 }">
          {{ ingestLabel }}
        </div>
        <div class="fader-cap">ingest</div>
      </div>
      <MeterBar label="cons. lag" :value="consumerMeter?.consumer_lag ?? 0" :max="500" unit="" />
      <div class="watermark"><span>watermark</span><code>{{ watermark }}</code></div>
    </div>
  </section>
</template>

<style scoped>
.strip {
  display: flex; flex-direction: column; align-items: stretch; gap: 10px;
  width: 150px; padding: 12px; background: var(--panel);
  border: 1px solid var(--line); border-radius: 10px;
}
.strip.muted { opacity: 0.6; }
.strip-head { display: flex; align-items: center; gap: 6px; }
.strip-title { font-weight: 600; font-size: 13px; line-height: 1.2; word-break: break-all; }
.dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
.dot.state-changes { background: #2f81f7; }
.dot.membership-events { background: #3fb950; }
.content-tag { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }

.fader-wrap { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.fader {
  writing-mode: vertical-lr; direction: rtl;
  height: 130px; width: 28px; accent-color: #2f81f7; cursor: pointer;
}
.fader-readout { font-variant-numeric: tabular-nums; font-size: 13px; }
.fader-cap { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }

.lag { display: flex; flex-direction: column; gap: 4px; }
.lag-head { display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); }
.lag-val { color: var(--fg); font-variant-numeric: tabular-nums; }
.lag input { width: 100%; accent-color: #d29922; cursor: pointer; }

.mute {
  padding: 6px; border-radius: 6px; border: 1px solid var(--line);
  background: #161b22; color: var(--muted); cursor: pointer; font-size: 12px;
  text-transform: uppercase; letter-spacing: 0.05em;
}
.mute.on { background: #f85149; color: #fff; border-color: #f85149; }

.meters { display: flex; flex-direction: column; gap: 8px; margin-top: 2px; }
.watermark { display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); }
.watermark code { color: var(--fg); font-variant-numeric: tabular-nums; }

.consumer {
  display: flex; flex-direction: column; align-items: stretch; gap: 8px;
  margin-top: 4px; padding-top: 10px; border-top: 1px dashed var(--line);
}
.consumer-cap {
  font-size: 10px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.06em; text-align: center;
}
.fader.ingest { accent-color: #8957e5; height: 96px; }
.fader-readout.stall { color: #f85149; font-weight: 600; }
</style>
