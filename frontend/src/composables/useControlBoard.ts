// The board's single reactive store. Holds operator state + live meters, polls
// /meters at 5 Hz, and pushes dial changes (optimistic local update + throttled
// PUT so dragging a fader doesn't flood the API).
//
// Consumer side: on load it probes /capabilities. When the backend ran with
// --consumer it also holds consumer state + meters, polls them on the same
// cadence, and exposes the consumer ingest dial. When the backend is
// producer-only (consumer_enabled=false) no consumer endpoint is ever called.

import { onScopeDispose, reactive, ref, shallowRef } from 'vue'
import {
  api,
  type ConsumerControlState,
  type ConsumerMeters,
  type ControlState,
  type Meters,
  type TopicDials,
  type Transport,
} from '../api'

const METERS_HZ = 5

export function useControlBoard() {
  const state = shallowRef<ControlState | null>(null)
  const meters = shallowRef<Meters | null>(null)
  const consumerEnabled = ref(false)
  const consumerState = shallowRef<ConsumerControlState | null>(null)
  const consumerMeters = shallowRef<ConsumerMeters | null>(null)
  const error = ref<string | null>(null)
  const ready = ref(false)

  // Trailing throttle per topic so a fader drag coalesces to ~20 PUTs/s. The
  // producer and consumer dials key the same map; their topic names are shared.
  const pending = reactive(new Map<string, ReturnType<typeof setTimeout>>())
  const consumerPending = reactive(new Map<string, ReturnType<typeof setTimeout>>())

  async function load(): Promise<void> {
    try {
      state.value = await api.getState()
      meters.value = await api.getMeters()
      consumerEnabled.value = (await api.getCapabilities()).consumer_enabled
      if (consumerEnabled.value) {
        consumerState.value = await api.getConsumerState()
        consumerMeters.value = await api.getConsumerMeters()
      }
      ready.value = true
      error.value = null
    } catch (e) {
      error.value = String(e)
    }
  }

  async function tickMeters(): Promise<void> {
    try {
      meters.value = await api.getMeters()
      if (consumerEnabled.value) {
        consumerMeters.value = await api.getConsumerMeters()
      }
      error.value = null
    } catch (e) {
      error.value = String(e)
    }
  }

  async function setTransport(patch: Partial<Transport>): Promise<void> {
    if (!state.value) return
    const next: Transport = { ...state.value.transport, ...patch }
    state.value = { ...state.value, transport: next } // optimistic
    try {
      const confirmed = await api.putTransport(next)
      state.value = { ...state.value!, transport: confirmed }
    } catch (e) {
      error.value = String(e)
    }
  }

  function setTopic(topic: string, patch: Partial<TopicDials>): void {
    if (!state.value) return
    const topics = state.value.topics.map((t) =>
      t.topic === topic ? { ...t, ...patch } : t,
    )
    state.value = { ...state.value, topics } // optimistic, immediate

    const existing = pending.get(topic)
    if (existing) clearTimeout(existing)
    pending.set(
      topic,
      setTimeout(() => {
        pending.delete(topic)
        const t = state.value?.topics.find((x) => x.topic === topic)
        if (!t) return
        api
          .putTopic(topic, { rate: t.rate, lag_ms: t.lag_ms, mute: t.mute })
          .then((confirmed) => {
            if (!state.value) return
            state.value = {
              ...state.value,
              topics: state.value.topics.map((x) => (x.topic === topic ? confirmed : x)),
            }
          })
          .catch((e) => (error.value = String(e)))
      }, 50),
    )
  }

  function setConsumerTopic(topic: string, ingest_rate: number): void {
    if (!consumerState.value) return
    const topics = consumerState.value.topics.map((t) =>
      t.topic === topic ? { ...t, ingest_rate } : t,
    )
    consumerState.value = { ...consumerState.value, topics } // optimistic, immediate

    const existing = consumerPending.get(topic)
    if (existing) clearTimeout(existing)
    consumerPending.set(
      topic,
      setTimeout(() => {
        consumerPending.delete(topic)
        const t = consumerState.value?.topics.find((x) => x.topic === topic)
        if (!t) return
        api
          .putConsumerTopic(topic, { ingest_rate: t.ingest_rate })
          .then((confirmed) => {
            if (!consumerState.value) return
            consumerState.value = {
              ...consumerState.value,
              topics: consumerState.value.topics.map((x) =>
                x.topic === topic ? confirmed : x,
              ),
            }
          })
          .catch((e) => (error.value = String(e)))
      }, 50),
    )
  }

  const timer = setInterval(tickMeters, 1000 / METERS_HZ)
  onScopeDispose(() => {
    clearInterval(timer)
    pending.forEach(clearTimeout)
    consumerPending.forEach(clearTimeout)
  })

  void load()

  // Returned refs are read-only by convention — mutate only via setTransport /
  // setTopic / setConsumerTopic, which keep the optimistic local state and the
  // API in sync.
  return {
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
  }
}
