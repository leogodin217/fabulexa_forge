// Selects the API implementation. Default is the mock (runs with no backend);
// set VITE_API_MODE=http to talk to the real FabulMixer driver.

import { createHttpApi } from './httpApi'
import { createMockApi } from './mockApi'
import type { FabulMixerApi } from './types'

const mode = import.meta.env.VITE_API_MODE ?? 'mock'

export const api: FabulMixerApi = mode === 'http' ? createHttpApi() : createMockApi()
export const apiMode = mode

export * from './types'
