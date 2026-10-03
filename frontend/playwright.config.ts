import { defineConfig, devices } from '@playwright/test';
import { randomUUID } from 'node:crypto';

const browserMode = process.env.MVP_WEB_MODE ?? 'preview';
const webPort = Number(process.env.VIDEOCLIP_WEB_PORT ?? '5174');
const defaultWebUrl = `http://127.0.0.1:${webPort}`;
const outputDir = process.env.MVP_PLAYWRIGHT_OUTPUT_DIR
  ?? `../data/mvp-stage-2/acceptance/playwright-run-${Date.now()}-${process.pid}-${randomUUID()}`;

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: 'list',
  outputDir,
  webServer: {
    command: browserMode === 'dev' ? `npm run dev -- --port ${webPort}` : `npm run preview -- --port ${webPort}`,
    url: defaultWebUrl,
    reuseExistingServer: false,
    timeout: 30_000,
  },
  use: {
    baseURL: process.env.MVP_WEB_URL ?? defaultWebUrl,
    ...devices['Desktop Chrome'],
    headless: true,
    screenshot: 'off',
    trace: 'off',
    video: 'off',
    serviceWorkers: 'block',
  },
});
