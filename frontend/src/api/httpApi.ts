// Real HTTP implementation of the FabulMixer control API. Talks to the backend
// at `/api` (Vite proxies it in dev — see vite.config.ts). Selected over the mock
// by VITE_API_MODE=http. Identical interface, so nothing else in the app changes.

import type {
  Capabilities,
  ConsumerControlState,
  ConsumerMeters,
  ConsumerTopicDials,
  ConsumerTopicDialsInput,
  ControlState,
  FabulMixerApi,
  Meters,
  Transport,
  TopicDials,
  TopicDialsInput,
} from './types'

const BASE = '/api'

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? detail
    } catch {
      // body was not JSON; keep statusText
    }
    throw new Error(`${res.status} ${detail}`)
  }
  return res.json() as Promise<T>
}

export function createHttpApi(): FabulMixerApi {
  return {
    getState: () => fetch(`${BASE}/state`).then((r) => json<ControlState>(r)),

    getMeters: () => fetch(`${BASE}/meters`).then((r) => json<Meters>(r)),

    putTransport: (transport: Transport) =>
      fetch(`${BASE}/transport`, {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(transport),
      }).then((r) => json<Transport>(r)),

    putTopic: (topic: string, dials: TopicDialsInput) =>
      fetch(`${BASE}/topics/${encodeURIComponent(topic)}`, {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(dials),
      }).then((r) => json<TopicDials>(r)),

    getCapabilities: () =>
      fetch(`${BASE}/capabilities`).then((r) => json<Capabilities>(r)),

    getConsumerState: () =>
      fetch(`${BASE}/consumer/state`).then((r) => json<ConsumerControlState>(r)),

    getConsumerMeters: () =>
      fetch(`${BASE}/consumer/meters`).then((r) => json<ConsumerMeters>(r)),

    putConsumerTopic: (topic: string, dials: ConsumerTopicDialsInput) =>
      fetch(`${BASE}/consumer/topics/${encodeURIComponent(topic)}`, {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(dials),
      }).then((r) => json<ConsumerTopicDials>(r)),
  }
}
