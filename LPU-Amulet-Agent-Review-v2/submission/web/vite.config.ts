import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5194,
    strictPort: true,
    proxy: { "/api": { target: "http://127.0.0.1:8094", changeOrigin: false } },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
  build: {
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: (id) =>
          id.includes("@livekit/protocol") || id.includes("@bufbuild")
            ? "livekit-protocol"
            : id.includes("livekit")
              ? "livekit"
              : id.includes("@xyflow")
                ? "flow"
                : undefined,
      },
    },
  },
});
