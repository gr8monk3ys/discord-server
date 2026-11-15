import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

export default defineConfig({
  test: {
    // Node: the route handlers use its Request, Response and crypto, and the pages are
    // checked as the server renders them. The browser side is e2e/ (Playwright).
    environment: "node",
    globals: true,
    include: ["src/**/*.{test,spec}.{js,ts,jsx,tsx}"],
    coverage: {
      provider: "v8",
      reporter: ["text-summary", "json-summary"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/*.{test,spec}.{ts,tsx}", "**/*.d.ts"],
      // Ratchet, as on lscaturchio.xyz: set just below the measured coverage so CI fails on a
      // regression. Raise these as coverage improves; never lower them. The client islands
      // (components/) are covered by e2e/, which this number does not see.
      thresholds: {
        statements: 68,
        branches: 68,
        functions: 54,
        lines: 68,
      },
    },
  },
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
});
