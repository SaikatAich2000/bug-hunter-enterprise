import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const root = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  plugins: [react()],
  server: { fs: { strict: false, allow: [fileURLToPath(new URL(".", import.meta.url))] } },
  test: {
    environment: "jsdom",
    pool: "forks",
    include: ["src/**/*.test.jsx"],
    setupFiles: ["./vitest.setup.js"],
    exclude: ["node_modules/**"],
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      reportsDirectory: "./coverage",
      include: ["src/**/*.js", "src/**/*.jsx"],
    },
  },
  resolve: {
    alias: { "@": resolve(root, "src") },
  },
});