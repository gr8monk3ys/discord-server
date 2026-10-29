import { defineConfig, devices } from "@playwright/test";

// End-to-end checks run against the production build (`next build && next start`), because
// the nonce CSP, the headers and the /join redirect only exist there.
const PORT = Number(process.env.PLAYWRIGHT_PORT ?? "4173");
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  timeout: 60000,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: "list",
  use: { baseURL: BASE_URL, trace: "on-first-retry" },
  webServer: {
    command: `bun run build && bun run start -- --hostname 127.0.0.1 --port ${PORT}`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 240000,
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        // Locally, the installed Edge; in CI, Playwright's own Chromium.
        ...(process.env.CI ? {} : { channel: "msedge" }),
      },
    },
  ],
});
