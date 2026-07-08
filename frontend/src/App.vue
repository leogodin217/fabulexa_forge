<script setup lang="ts">
import { computed } from 'vue'
import { apiMode, type ConsumerTopicDials, type ConsumerTopicMeter, type TopicMeter } from './api'
import { useControlBoard } from './composables/useControlBoard'
import ChannelStrip from './components/ChannelStrip.vue'
import ConsumerPanel from './components/ConsumerPanel.vue'
import MasterTransport from './components/MasterTransport.vue'

const {
  state,
  meters,
  consumerEnabled,
  consumerState,
  consumerMeters,
  error,
  ready,
  setTransport,
  setTopic,
  setConsumerTopic,
} = useControlBoard()

const meterByTopic = computed(() => {
  const m = new Map<string, TopicMeter>()
  for (const t of meters.value?.topics ?? []) m.set(t.topic, t)
  return m
})

const consumerDialsByTopic = computed(() => {
  const m = new Map<string, ConsumerTopicDials>()
  for (const t of consumerState.value?.topics ?? []) m.set(t.topic, t)
  return m
})

const consumerMeterByTopic = computed(() => {
  const m = new Map<string, ConsumerTopicMeter>()
  for (const t of consumerMeters.value?.topics ?? []) m.set(t.topic, t)
  return m
})
</script>

<template>
  <div class="app">
    <header class="app-head">
      <h1>FabulMixer <span class="sub">live-perform board</span></h1>
      <span class="badge" :class="apiMode">{{ apiMode === 'mock' ? 'MOCK (no backend)' : 'LIVE' }}</span>
    </header>

    <p v-if="error" class="error">{{ error }}</p>

    <template v-if="ready && state">
      <MasterTransport
        :transport="state.transport"
        :meters="meters"
        :consumer-meters="consumerEnabled ? consumerMeters : null"
        @change="setTransport"
      />

      <div class="rack">
        <ChannelStrip
          v-for="t in state.topics"
          :key="t.topic"
          :dials="t"
          :meter="meterByTopic.get(t.topic)"
          :consumer-dials="consumerEnabled ? consumerDialsByTopic.get(t.topic) : undefined"
          :consumer-meter="consumerEnabled ? consumerMeterByTopic.get(t.topic) : undefined"
          @change="(patch) => setTopic(t.topic, patch)"
          @consumer-change="(rate) => setConsumerTopic(t.topic, rate)"
        />
      </div>

      <ConsumerPanel v-if="consumerEnabled && consumerMeters" :meters="consumerMeters" />
    </template>
    <p v-else-if="!error" class="loading">connecting…</p>
  </div>
</template>

<style scoped>
.app { max-width: 1100px; margin: 0 auto; padding: 24px; display: flex; flex-direction: column; gap: 20px; }
.app-head { display: flex; align-items: baseline; gap: 14px; }
h1 { font-size: 22px; margin: 0; }
.sub { color: var(--muted); font-weight: 400; font-size: 15px; }
.badge { font-size: 11px; padding: 3px 8px; border-radius: 6px; letter-spacing: 0.05em; }
.badge.mock { background: #4d3800; color: #ffd666; }
.badge.http { background: #033a16; color: #56d364; }
.rack { display: flex; gap: 14px; flex-wrap: wrap; }
.error { color: #f85149; font-family: monospace; }
.loading { color: var(--muted); }
</style>
