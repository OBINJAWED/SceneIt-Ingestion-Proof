import { expect, test, type Page, type Route } from '@playwright/test';

const proof = {
  id: 'shared-proof',
  title: 'Controlled proof fixture',
  youtubeVideoId: 'fixture-video',
  youtubeUrl: 'https://youtu.be/fixture-video',
  durationSeconds: 90,
  width: 1280,
  height: 720,
  fileSizeBytes: 1024,
  hasAudio: true,
  state: 'ready',
  statusMessage: 'Ready',
  model: 'fixture-model',
  timelineStatus: 'unverified',
  checks: [],
  sourcePlaybackAvailable: false,
  sourcePlaybackUrl: null,
  searchesUsed: 1,
  searchLimit: 5,
  updatedAt: '2025-01-01T00:00:00Z',
};

const savedSearch = {
  id: 'b8b31700-2bb5-42b7-95d7-d1c39ff8ccad',
  query: 'shared saved doorway',
  modality: 'visual',
  createdAt: '2025-01-01T00:00:00Z',
  latencyMs: 123,
  provider: 'Twelve Labs',
  partial: false,
  matches: [{
    rank: 1,
    startSeconds: 12,
    endSeconds: 18,
    confidenceLabel: 'high',
    frameUrl: '/api/proof/searches/b8b31700-2bb5-42b7-95d7-d1c39ff8ccad/frames/1',
    youtubeUrl: 'https://youtu.be/fixture-video?t=12',
  }],
};

const importFixture = {
  id: '3b4d10cf-9cc1-4d80-a90d-d09a57bb68fa',
  title: 'Private import fixture',
  entryMethod: 'upload',
  sourceKind: 'file',
  sourceUrl: null,
  externalId: null,
  state: 'ready',
  statusMessage: 'Indexed',
  progressPercent: 100,
  errorCode: null,
  durationSeconds: 30,
  fileSizeBytes: 2048,
  hasAudio: true,
  sourcePlaybackAvailable: false,
  sourcePlaybackUrl: null,
  playbackAuthorized: false,
  timelineStatus: 'not_applicable',
  createdAt: '2025-01-01T00:00:00Z',
  updatedAt: '2025-01-01T00:00:00Z',
  expiresAt: '2030-01-01T00:00:00Z',
  searchesUsed: 0,
  searchLimit: 5,
  importsUsed: 1,
  importLimit: 2,
};

const config = {
  maxBytes: 200000000,
  minDurationSeconds: 4,
  maxDurationSeconds: 1200,
  retentionDays: 7,
  ownerImportLimit: 2,
  appImportLimit: 20,
  ownerSearchLimit: 5,
  appSearchLimit: 50,
  workerAvailable: true,
};

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function fixtureApi(
  page: Page,
  options: {
    admitted?: boolean;
    onSearch?: (route: Route) => Promise<void>;
    onHistory?: (route: Route) => Promise<void>;
    onProof?: (route: Route) => Promise<void>;
    onOperations?: (route: Route) => Promise<void>;
    onFrame?: (route: Route) => Promise<void>;
    onLogout?: () => void;
  } = {},
) {
  let signedOut = false;
  await page.route('https://www.youtube.com/**', route => route.abort());
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/auth/session' || url.pathname === '/api/auth/user') {
      if (signedOut) {
        return json(route, { user: null, csrfToken: null, pilotAdmitted: false });
      }
      return json(route, {
        user: { id: 'pilot-fixture', firstName: 'Pilot' },
        csrfToken: 'fixture-csrf',
        pilotAdmitted: options.admitted ?? true,
      });
    }
    if (url.pathname === '/api/logout') {
      signedOut = true;
      options.onLogout?.();
      return json(route, { success: true });
    }
    if (url.pathname === '/api/proof') {
      if (options.onProof) return options.onProof(route);
      return json(route, proof);
    }
    if (url.pathname === '/api/proof/readiness') {
      return json(route, {
        state: 'ready', proofState: 'ready', searchAvailable: true,
        searchesUsed: 1, searchLimit: 5, detail: null, retryAfterSeconds: null,
      });
    }
    if (url.pathname === '/api/proof/search-operations') {
      if (options.onOperations) return options.onOperations(route);
      return json(route, []);
    }
    if (/^\/api\/proof\/searches\/[^/]+\/frames\/\d+$/.test(url.pathname)) {
      if (options.onFrame) return options.onFrame(route);
      return route.fulfill({ status: 404 });
    }
    if (url.pathname === '/api/proof/searches' && request.method() === 'GET') {
      if (options.onHistory) return options.onHistory(route);
      return json(route, [savedSearch]);
    }
    if (url.pathname === '/api/proof/searches' && request.method() === 'POST') {
      if (options.onSearch) return options.onSearch(route);
      return json(route, savedSearch);
    }
    if (url.pathname === '/api/healthz') return json(route, { status: 'ok' });
    if (url.pathname === '/api/imports/config') return json(route, config);
    if (url.pathname === `/api/imports/${importFixture.id}`) return json(route, importFixture);
    if (url.pathname === `/api/imports/${importFixture.id}/searches`) return json(route, []);
    return json(route, { error: 'Unmatched fixture route', code: 'TEST_ROUTE' }, 500);
  });
}

test('denies a signed-in account without pilot admission', async ({ page }) => {
  await fixtureApi(page, { admitted: false });
  await page.goto('/demo');
  await expect(page.getByRole('heading', { name: 'Pilot access required' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Search scenes' })).toHaveCount(0);
});

test('renders the admitted shared proof and saved result', async ({ page }) => {
  await fixtureApi(page);
  await page.goto('/demo');
  await expect(page.getByRole('heading', { name: 'Less scrubbing. More finding.' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Search scenes' })).toBeVisible();
  await expect(page.getByText('shared saved doorway', { exact: false }).first()).toBeVisible();
  await expect(page.getByText('Searches are saved to this shared demo. Do not enter private information.')).toBeVisible();
});

test('retains a selected successful result through a failed read and explicit refresh', async ({ page }) => {
  let historyReads = 0;
  let paidSearches = 0;
  let frameReads = 0;
  let historyAvailable = true;
  let frameAvailable = false;
  await fixtureApi(page, {
    onHistory: async route => {
      historyReads += 1;
      if (!historyAvailable) {
        return json(route, {
          error: 'History unavailable', code: 'SERVICE', state: 'service_unavailable',
          retryable: false, retryAfterSeconds: null,
        }, 503);
      }
      return json(route, [savedSearch]);
    },
    onSearch: async route => {
      paidSearches += 1;
      historyAvailable = false;
      expect(route.request().headers()['x-csrf-token']).toBe('fixture-csrf');
      return json(route, { ...savedSearch, id: '793922b7-792c-4fd0-a5b5-2578ff2e0fe6', query: 'new cached result' });
    },
    onFrame: async route => {
      frameReads += 1;
      if (!frameAvailable) return route.fulfill({ status: 503 });
      return route.fulfill({
        status: 200,
        contentType: 'image/png',
        body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64'),
      });
    },
  });
  await page.goto('/demo');
  await expect(page.getByText('shared saved doorway', { exact: false }).first()).toBeVisible();
  expect(historyReads).toBe(1);
  await page.getByRole('searchbox', { name: 'Describe a scene or event' }).fill('new cached result');
  await page.getByRole('button', { name: 'Search scenes' }).click();
  await expect(page.getByText('"new cached result"')).toBeVisible();
  await expect(page.getByRole('alert').filter({ hasText: 'A shared proof read failed.' })).toBeVisible();
  await expect(page.getByText('Source Frame Evidence')).toBeVisible();
  frameAvailable = true;
  await page.getByRole('button', { name: 'Refresh frame' }).click();
  await expect.poll(() => frameReads).toBeGreaterThanOrEqual(2);
  await expect(page.getByRole('button', { name: 'Refresh frame' })).toHaveCount(0);
  historyAvailable = true;
  const readsBeforeRefresh = historyReads;
  await page.getByRole('alert').getByRole('button', { name: 'Refresh status', exact: true }).click();
  await expect.poll(() => historyReads).toBeGreaterThan(readsBeforeRefresh);
  await expect(page.getByRole('alert').filter({ hasText: 'A shared proof read failed.' })).toHaveCount(0);
  await expect(page.getByText('"new cached result"')).toBeVisible();
  expect(paidSearches).toBe(1);
});

test('shows uncertain paid search failure without automatic retry', async ({ page }) => {
  let paidSearches = 0;
  let proofReads = 0;
  let operationReads = 0;
  await fixtureApi(page, {
    onProof: route => {
      proofReads += 1;
      return json(route, proof);
    },
    onOperations: route => {
      operationReads += 1;
      return json(route, []);
    },
    onSearch: async route => {
      paidSearches += 1;
      return json(route, {
        error: 'Outcome uncertain', code: 'PROVIDER_UNCERTAIN', state: 'uncertain',
        retryable: false, retryAfterSeconds: null,
      }, 503);
    },
  });
  await page.goto('/demo');
  await page.getByRole('searchbox', { name: 'Describe a scene or event' }).fill('uncertain request');
  await page.getByRole('button', { name: 'Search scenes' }).click();
  await expect(page.getByText(
    'The operation needs operator review and will not be retried automatically.',
    { exact: true },
  )).toBeVisible();
  await page.waitForTimeout(1_500);
  expect(paidSearches).toBe(1);
  expect(proofReads).toBeGreaterThanOrEqual(2);
  expect(operationReads).toBeGreaterThanOrEqual(2);
});

test('logout clears SceneIt storage and selected proof UI', async ({ page }) => {
  let loggedOut = false;
  await fixtureApi(page, { onLogout: () => { loggedOut = true; } });
  await page.goto('/demo');
  await expect(page.getByText('Source Frame Evidence')).toBeVisible();
  await page.evaluate(() => localStorage.setItem('sceneit:selected-result', 'stale'));
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect.poll(() => loggedOut).toBe(true);
  await expect(page.getByRole('heading', { name: 'SceneIt controlled pilot' })).toBeVisible();
  await expect(page.getByText('Source Frame Evidence')).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => localStorage.getItem('sceneit:selected-result'))).toBeNull();
});

test('preserves the private import analysis surface', async ({ page }) => {
  await fixtureApi(page);
  await page.goto(`/imports/${importFixture.id}`);
  await expect(page.getByRole('heading', { name: 'Private import fixture' })).toBeVisible();
  await expect(page.getByText('Private source playback', { exact: true })).toBeVisible();
  await expect(page.getByText('Search this video', { exact: true })).toBeVisible();
});