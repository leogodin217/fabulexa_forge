/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 'mock' (default, no backend) | 'http' (real FabulMixer driver). */
  readonly VITE_API_MODE?: 'mock' | 'http'
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
