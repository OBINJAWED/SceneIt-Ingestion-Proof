import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  retries: 0,
  reporter: 'line',
  use: {
    baseURL: 'http://127.0.0.1:4177',
    serviceWorkers: 'block',
    launchOptions: process.env.SCENEIT_TEST_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.SCENEIT_TEST_CHROMIUM_EXECUTABLE }
      : undefined,
  },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 900 } } },
    // Mobile viewport fixture, not a claim of real-device Safari verification.
    { name: 'mobile', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
  ],
  webServer: {
    command: 'PORT=4177 BASE_PATH=/ pnpm exec vite --config vite.config.ts --host 127.0.0.1',
    port: 4177,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});