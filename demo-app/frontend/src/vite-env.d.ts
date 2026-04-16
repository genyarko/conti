/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_TRUSTLAYER_API_URL?: string;
  readonly VITE_CONTRACT_API_URL?: string;
  readonly VITE_API_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
