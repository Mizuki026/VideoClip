import { defineConfig, devices } from '@playwright/test';

const browserMode = process.env.MVP_WEB_MODE ?? 'preview';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: 'list',
  outputDir: './test-results',
  webServer: {
    command: browserMode === 'dev' ? 'npm run dev' : 'npm run preview',
    url: 'http://127.0.0.1:5174',
    reuseExistingServer: false,
    timeout: 30_000,
  },
  use: {
    baseURL: process.env.MVP_WEB_URL ?? 'http://127.0.0.1:5174',
    ...devices['Desktop Chrome'],
    headless: true,
    screenshot: 'off',
    trace: 'off',
    video: 'off',
    serviceWorkers: 'block',
  },
});
