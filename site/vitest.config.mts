import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

export default defineConfig({
  test: {
    // Node: the route handlers use its Request, Response and crypto, and the pages are
    // checked as the server renders them. The browser side is e2e/ (Playwright).
    environment: "node",
    globals: true,
    include: ["src/**/*.{test,spec}.{js,ts,jsx,tsx}"],
  },
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
});
