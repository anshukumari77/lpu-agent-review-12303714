// Packaging-only static preview. No backend proxy or agent connection.
import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";

export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  envDir: false,
  preview: {
    host: "127.0.0.1",
    port: 5194,
    strictPort: true,
    proxy: {},
    headers: {
      "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'none'; media-src 'none'; object-src 'none'; frame-ancestors 'none'",
      "Permissions-Policy": "camera=(), microphone=(), display-capture=()",
    },
  },
});
