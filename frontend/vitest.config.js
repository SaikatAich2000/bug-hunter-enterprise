import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const root = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  plugins: [react()],
  // One worker on Windows: multiple workers surface a Vite module-runner
  // incompatibility with the workspace root. Single-worker runs are fast here
  // (all suites execute in well under a second of test time).
  server: { fs: { strict: false, allow: [fileURLToPath(new URL(".", import.meta.url))] } },
  test: {
    environment: "jsdom",
    pool: "forks",
    poolOptions: {
      forks: { singleFork: true, isolate: false },
    },
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