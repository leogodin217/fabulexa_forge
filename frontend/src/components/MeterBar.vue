<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  label: string
  value: number
  max: number
  unit?: string
}>()

const pct = computed(() => Math.min(100, (props.value / props.max) * 100))
const level = computed(() => (pct.value > 80 ? 'hot' : pct.value > 40 ? 'warm' : 'cool'))
</script>

<template>
  <div class="meter">
    <div class="meter-head">
      <span class="meter-label">{{ label }}</span>
      <span class="meter-value">{{ Math.round(value) }}<span class="meter-unit">{{ unit }}</span></span>
    </div>
    <div class="meter-track">
      <div class="meter-fill" :class="level" :style="{ width: pct + '%' }" />
    </div>
  </div>
</template>

<style scoped>
.meter { display: flex; flex-direction: column; gap: 2px; }
.meter-head { display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); }
.meter-value { font-variant-numeric: tabular-nums; color: var(--fg); }
.meter-unit { color: var(--muted); margin-left: 1px; }
.meter-track { height: 8px; background: #0d1117; border-radius: 4px; overflow: hidden; }
.meter-fill { height: 100%; transition: width 120ms linear; border-radius: 4px; }
.meter-fill.cool { background: #2f81f7; }
.meter-fill.warm { background: #d29922; }
.meter-fill.hot { background: #f85149; }
</style>
