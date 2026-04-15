/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_TRUSTLAYER_API_URL?: string;
  readonly VITE_DEMO_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
