import { defineConfig, devices } from "@playwright/test";

// Browser smoke test (roadmap 6.9) against the real API on a throw-away seeded database, serving the built
// dashboard: `npm run build && npm run e2e`.
const PORT = 8765;

export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: { baseURL: `http://127.0.0.1:${PORT}`, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `uv run python scripts/demo_api.py --port ${PORT}`,
    cwd: "../backend",
    url: `http://127.0.0.1:${PORT}/api/health`,
    env: { DEMO_PASSWORD: "demo password" },
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
