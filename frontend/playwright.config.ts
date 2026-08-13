import { defineConfig, devices } from "@playwright/test";

/**
 * E2E config for the platform's critical admin flows (inventory,
 * classification rules, health policies, maintenance).
 *
 * Deliberately targets `vite dev` (the same server started by `npm run
 * dev`), not a `vite preview` production build: `preview` needs its own
 * `preview.proxy` config (Vite's dev-server `server.proxy` — the one this
 * project already relies on and already fixed a real routing bug in,
 * see `vite.config.ts`'s comment — does not apply to `preview`). Testing
 * against the exact server whose proxy behavior is already
 * production-verified is worth more here than testing against a minified
 * bundle; the frontend `build` step is still separately checked in CI
 * (see `.github/workflows/ci.yml`'s `frontend` job).
 *
 * These tests need a real backend + MongoDB + Redis behind that dev
 * server — there is no mocking layer. Locally: `scripts/dev-up.sh`, then
 * the backend (`uv run uvicorn app.main:app --port 8080`) and
 * `npm run dev`, both already running before `npm run test:e2e`. In CI,
 * the `e2e` job in `.github/workflows/ci.yml` starts all of it.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false, // shared backend/DB state; see e2e/README.md
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["html", { open: "never" }], ["list"]] : "list",
  timeout: 30_000,
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
