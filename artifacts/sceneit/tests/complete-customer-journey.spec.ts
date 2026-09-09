import { expect, test, type Page, type Request, type Route, type TestInfo } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { createServer, type Server } from 'node:http';

const uploadServers = new Set<Server>();
test.afterEach(async () => {
  await Promise.all([...uploadServers].map(server => new Promise<void>((resolve, reject) => {
    server.closeAllConnections();
    server.close(error => error ? reject(error) : resolve());
    uploadServers.delete(server);
  })));
});

const mediaPath = fileURLToPath(new URL('./assets/synthetic-journey-h264.mp4', import.meta.url));
const importId = 'a67e5608-87f8-4c55-98ea-03c976482aac';
const searchId = '5913f0aa-0112-4262-9d10-ce577da415d7';
const firebaseConfig = {
  apiKey: 'journey-fixture-key',
  authDomain: 'journey-fixture.firebaseapp.com',
  projectId: 'journey-fixture',
  appId: '1:42:web:journey',
};

const png = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
  'base64',
);

function jwt(email: string, verified: boolean, owner = 'owner-a') {
  const part = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url');
  return `${part({ alg: 'none' })}.${part({
    aud: firebaseConfig.projectId, email, email_verified: verified, exp: 4_102_444_800,
    iat: 1_735_689_600, sub: owner, user_id: owner,
    firebase: { sign_in_provider: 'password' },
  })}.fixture`;
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

type HarnessOptions = {
  workerAvailable?: boolean;
  interruptFirstTransfer?: boolean;
  silent?: boolean;
  frameUnavailable?: boolean;
  privateSearchOutcomes?: Array<'empty' | 'failed' | 'uncertain'>;
};

async function journeyHarness(page: Page, options: HarnessOptions = {}) {
  const media = await readFile(mediaPath);
  let owner: { id: string; email: string } | null = null;
  let state = 'file_required';
  let playbackAuthorized = true;
  let playbackAvailable = true;
  let completeCalls = 0;
  let transfers = 0;
  let searches = 0;
  let searchSubmissions = 0;
  let processingReads = 0;
  let importsUsed = 0;
  let creations = 0;
  let entryMethod: 'link' | 'upload' = 'link';
  let sourceKind: 'youtube' | 'vimeo' | 'file' = 'youtube';
  let sourceUrl: string | null = 'https://www.youtube.com/watch?v=fixtureJourney';
  let externalId: string | null = 'fixtureJourney';
  const history: any[] = [];
  const unhandled: string[] = [];
  const requests: Request[] = [];
  const identities = new Map<string, { id: string; email: string; verified: boolean }>();
  function refreshIdentity(id: string, email: string, verified: boolean) {
    const refresh = `fixture-refresh-${id}-${verified}`;
    identities.set(refresh, { id, email, verified });
    return refresh;
  }
  let receivedTransfers = 0;
  const uploadServer = createServer((request, response) => {
    response.setHeader('Access-Control-Allow-Origin', 'http://127.0.0.1:4177');
    response.setHeader('Access-Control-Allow-Methods', 'PUT, OPTIONS');
    response.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    if (request.url !== '/synthetic-upload') {
      response.writeHead(404).end();
      return;
    }
    if (request.method === 'OPTIONS') {
      response.writeHead(204).end();
      return;
    }
    if (request.method !== 'PUT') {
      response.writeHead(405).end();
      return;
    }
    const chunks: Buffer[] = [];
    let bytes = 0;
    request.on('data', (chunk: Buffer) => {
      bytes += chunk.length;
      if (bytes > media.length) request.destroy();
      else chunks.push(chunk);
    });
    request.on('end', () => {
      if (!Buffer.concat(chunks).equals(media)) {
        response.writeHead(422).end('Synthetic fixture byte mismatch');
        return;
      }
      receivedTransfers += 1;
      response.writeHead(200).end();
    });
  });
  uploadServers.add(uploadServer);
  await new Promise<void>(resolve => uploadServer.listen(0, '127.0.0.1', resolve));
  const address = uploadServer.address();
  if (!address || typeof address === 'string') throw new Error('No fixture upload address');
  const uploadUrl = `http://127.0.0.1:${address.port}/synthetic-upload`;

  const item = () => ({
    id: importId,
    title: 'Synthetic journey video',
    entryMethod,
    sourceKind,
    sourceUrl,
    externalId,
    state,
    statusMessage: state === 'ready' ? 'Indexed' : state === 'processing' ? 'Validating and indexing' : state === 'cancelled' ? 'Cancelled' : 'Authorized MP4 required',
    progressPercent: state === 'ready' ? 100 : state === 'processing' ? 55 : 0,
    errorCode: null,
    durationSeconds: state === 'ready' ? 6 : null,
    fileSizeBytes: state === 'ready' ? media.byteLength : null,
    hasAudio: state === 'ready' ? !options.silent : null,
    sourcePlaybackAvailable: state === 'ready' && playbackAuthorized && playbackAvailable,
    sourcePlaybackUrl: state === 'ready' && playbackAuthorized && playbackAvailable ? `/api/imports/${importId}/source` : null,
    playbackAuthorized,
    timelineStatus: 'unverified',
    createdAt: '2025-01-01T00:00:00Z',
    updatedAt: '2025-01-01T00:01:00Z',
    expiresAt: '2030-01-01T00:00:00Z',
    searchesUsed: searches,
    searchLimit: 5,
    importsUsed,
    importLimit: 3,
    quotaMode: 'lifetime',
    budgetReserved: true,
  });

  const session = () => ({
    csrfToken: owner ? 'journey-csrf' : null,
    pilotAdmitted: false,
    user: owner && { ...owner, firstName: null, provider: 'firebase', emailVerified: true },
    capabilities: {
      replit: true, emailPassword: true, publicTrialEnabled: true,
      unavailableReason: null, firebaseConfig,
    },
    privateAccess: owner
      ? { allowed: true, reason: 'ready' }
      : { allowed: false, reason: 'authentication_required' },
    usage: owner ? {
      importsUsed, importLimit: 3, importsRemaining: 3 - importsUsed,
      searchesUsed: searches, searchLimit: 5, searchesRemaining: 5 - searches, lifetime: true,
    } : null,
  });

  await page.context().route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    requests.push(request);

    if (url.href === uploadUrl) {
      if (request.method() === 'OPTIONS') return route.continue();
      if (request.method() !== 'PUT') {
        unhandled.push(`${request.method()} ${request.url()}`);
        return route.abort('blockedbyclient');
      }
      transfers += 1;
      const headers = await request.allHeaders();
      expect(headers['content-type']).toBe('video/mp4');
      if (options.interruptFirstTransfer && transfers === 1) return route.abort('connectionreset');
      // Only this exact disposable loopback sink receives mutation bytes.
      return route.continue();
    }

    if (url.hostname === 'identitytoolkit.googleapis.com') {
      if (url.pathname.endsWith('/accounts:signUp')) {
        const email = (request.postDataJSON() as { email: string }).email;
        return json(route, { localId: 'owner-a', email, emailVerified: false,
          idToken: jwt(email, false), refreshToken: refreshIdentity('owner-a', email, false), expiresIn: '3600' });
      }
      if (url.pathname.endsWith('/accounts:signInWithPassword')) {
        const email = (request.postDataJSON() as { email: string }).email;
        const id = email.startsWith('second') ? 'owner-b' : 'owner-a';
        return json(route, { localId: id, email, emailVerified: true,
          idToken: jwt(email, true, id), refreshToken: refreshIdentity(id, email, true), expiresIn: '3600' });
      }
      if (url.pathname.endsWith('/accounts:sendOobCode')) return json(route, {});
      if (url.pathname.endsWith('/accounts:lookup')) {
        const idToken = (request.postDataJSON() as { idToken?: string }).idToken;
        if (idToken) {
          const claims = JSON.parse(Buffer.from(idToken.split('.')[1], 'base64url').toString()) as {
            sub: string; email: string; email_verified: boolean;
          };
          return json(route, { users: [{
            localId: claims.sub, email: claims.email,
            emailVerified: claims.email_verified, providerUserInfo: [],
          }] });
        }
        return json(route, { users: owner ? [{ localId: owner.id, email: owner.email, emailVerified: true, providerUserInfo: [] }] : [] });
      }
      if (url.pathname.endsWith('/accounts:update')) return json(route, { email: 'journey@example.test', emailVerified: true });
      unhandled.push(`${request.method()} ${request.url()}`);
      return route.abort('blockedbyclient');
    }
    if (url.hostname === 'securetoken.googleapis.com') {
      const refresh = new URLSearchParams(request.postData() || '').get('refresh_token');
      const identity = refresh && identities.get(refresh);
      expect(identity, 'Refresh must preserve the identity that signed in').toBeTruthy();
      if (!identity) return route.abort('blockedbyclient');
      return json(route, { access_token: jwt(identity.email, identity.verified, identity.id),
        expires_in: '3600', refresh_token: refresh, token_type: 'Bearer', user_id: identity.id });
    }
    if (url.hostname === 'www.youtube.com') return route.abort('blockedbyclient');
    if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') return route.abort('blockedbyclient');
    if (url.hostname !== '127.0.0.1') {
      unhandled.push(`${request.method()} ${request.url()}`);
      return route.abort('blockedbyclient');
    }
    if (!url.pathname.startsWith('/api/')) {
      if (['GET', 'HEAD', 'OPTIONS'].includes(request.method())) return route.continue();
      unhandled.push(`${request.method()} ${url.pathname}`);
      return route.abort('blockedbyclient');
    }

    if (url.pathname === '/api/auth/session' || url.pathname === '/api/auth/user') return json(route, session());
    if (url.pathname === '/api/auth/firebase/challenge') return json(route, { csrfToken: 'exchange-csrf' });
    if (url.pathname === '/api/auth/firebase/session') {
      const token = (request.postDataJSON() as { idToken: string }).idToken;
      const claims = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString()) as { sub: string; email: string };
      owner = { id: claims.sub, email: claims.email };
      return json(route, session());
    }
    if (url.pathname === '/api/logout') {
      owner = null;
      return json(route, { success: true });
    }
    if (url.pathname === '/api/imports/config') return json(route, {
      maxBytes: 200_000_000, minDurationSeconds: 4, maxDurationSeconds: 1200, retentionDays: 7,
      ownerImportLimit: 3, appImportLimit: 30, ownerSearchLimit: 5, appSearchLimit: 500,
      workerAvailable: options.workerAvailable ?? true, quotaMode: 'lifetime',
    });
    if (url.pathname.startsWith(`/api/imports/${importId}`) && owner?.id !== 'owner-a') {
      return json(route, { error: 'Not found', code: 'NOT_FOUND' }, 404);
    }
    if (url.pathname === '/api/imports/current') {
      return owner?.id === 'owner-a' && importsUsed ? json(route, item()) : route.fulfill({ status: 404 });
    }
    if (url.pathname === '/api/imports' && request.method() === 'POST') {
      expect(request.headers()['x-csrf-token']).toBe('journey-csrf');
      const creation = request.postDataJSON() as { entryMethod: string; sourceUrl?: string };
      expect(creation).toMatchObject({ analysisAuthorized: true });
      creations += 1;
      entryMethod = creation.entryMethod as 'link' | 'upload';
      if (creation.entryMethod === 'link') {
        sourceUrl = creation.sourceUrl || null;
        if (sourceUrl?.includes('youtube.com')) {
          sourceKind = 'youtube';
          externalId = 'fixtureJourney';
          expect(creation).toMatchObject({ playbackAuthorized: true });
          state = 'file_required';
        } else {
          sourceKind = 'vimeo';
          externalId = 'fixture-vimeo';
          state = 'ready';
          playbackAuthorized = false;
        }
      } else {
        sourceKind = 'file';
        sourceUrl = null;
        externalId = null;
        state = 'file_required';
      }
      importsUsed = 1;
      return json(route, item(), 201);
    }
    if (url.pathname === `/api/imports/${importId}` && request.method() === 'GET') {
      if (owner?.id !== 'owner-a') return json(route, { error: 'Not found', code: 'NOT_FOUND' }, 404);
      if (state === 'processing' && ++processingReads >= 2) state = 'ready';
      return json(route, item());
    }
    if (url.pathname === `/api/imports/${importId}/upload` && request.method() === 'POST') {
      expect(request.headers()['x-csrf-token']).toBe('journey-csrf');
      expect(request.postDataJSON()).toMatchObject({
        fileName: 'synthetic-journey-h264.mp4',
        sizeBytes: media.byteLength,
        contentType: 'video/mp4',
      });
      return json(route, {
        import: item(), uploadURL: uploadUrl,
        method: 'PUT', headers: { 'Content-Type': 'video/mp4' }, expiresAt: '2030-01-01T00:00:00Z',
      });
    }
    if (url.pathname === `/api/imports/${importId}/complete` && request.method() === 'POST') {
      completeCalls += 1;
      if (completeCalls === 1) return json(route, {
        error: 'Completion temporarily unavailable', code: 'SERVICE_UNAVAILABLE',
        state: 'service_unavailable', retryable: true, retryAfterSeconds: null,
      }, 503);
      state = 'processing';
      return json(route, item());
    }
    if (url.pathname === `/api/imports/${importId}/source` && request.method() === 'GET') {
      if (owner?.id !== 'owner-a') return json(route, { error: 'Not found', code: 'NOT_FOUND' }, 404);
      return route.fulfill({ status: 200, contentType: 'video/mp4', body: media });
    }
    if (/^\/api\/imports\/[^/]+\/searches\/[^/]+\/frames\/\d+$/.test(url.pathname)) {
      if (owner?.id !== 'owner-a') return json(route, { error: 'Not found', code: 'NOT_FOUND' }, 404);
      if (options.frameUnavailable) return route.fulfill({ status: 404 });
      return route.fulfill({ status: 200, contentType: 'image/png', body: png });
    }
    if (url.pathname === `/api/imports/${importId}/searches` && request.method() === 'GET') {
      if (owner?.id !== 'owner-a') return json(route, { error: 'Not found', code: 'NOT_FOUND' }, 404);
      return json(route, history);
    }
    if (url.pathname === `/api/imports/${importId}/searches` && request.method() === 'POST') {
      if (owner?.id !== 'owner-a') return json(route, { error: 'Not found', code: 'NOT_FOUND' }, 404);
      const outcome = options.privateSearchOutcomes?.[searchSubmissions++];
      if (outcome === 'failed' || outcome === 'uncertain') {
        return json(route, {
          error: outcome === 'uncertain' ? 'Private search outcome uncertain' : 'Private search failed',
          code: outcome === 'uncertain' ? 'PROVIDER_UNCERTAIN' : 'PROVIDER_FAILED',
          state: outcome, retryable: false, retryAfterSeconds: null,
        }, 503);
      }
      searches += 1;
      const result = {
        id: searchId, query: (request.postDataJSON() as { query: string }).query, modality: 'visual',
        createdAt: '2025-01-01T00:02:00Z', latencyMs: 42, provider: 'Twelve Labs', partial: false,
        matches: outcome === 'empty' ? [] : [
          { rank: 1, startSeconds: 1, endSeconds: 3, confidenceLabel: 'high',
            frameUrl: `/api/imports/${importId}/searches/${searchId}/frames/1`, youtubeUrl: null },
          { rank: 2, startSeconds: 3, endSeconds: 5, confidenceLabel: 'medium',
            frameUrl: `/api/imports/${importId}/searches/${searchId}/frames/2`, youtubeUrl: null },
        ],
      };
      history.unshift(result);
      return json(route, result);
    }
    if (url.pathname === `/api/imports/${importId}/playback` && request.method() === 'POST') {
      playbackAuthorized = (request.postDataJSON() as { authorized: boolean }).authorized;
      return json(route, item());
    }
    if (url.pathname === `/api/imports/${importId}/cancel` && request.method() === 'POST') {
      state = 'cancelled';
      return json(route, item());
    }
    unhandled.push(`${request.method()} ${url.pathname}`);
    return json(route, { error: 'Unknown safe fixture route', code: 'TEST_ROUTE' }, 599);
  });

  return {
    requests, unhandled,
    receivedTransfers: () => receivedTransfers,
    counts: () => ({ completeCalls, transfers, searches, creations }),
    expire() { owner = null; },
    makePlaybackUnavailable() { playbackAvailable = false; },
  };
}

async function snap(page: Page, testInfo: TestInfo, name: string) {
  await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true });
}

async function signIn(page: Page, email = 'journey@example.test') {
  await page.goto('/auth?mode=signin');
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill('correct horse battery staple');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page).toHaveURL('/');
}

test('repeatable complete private customer journey uses only safe synthetic fixtures', async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  const fixture = await journeyHarness(page);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Find scenes in your videos.' })).toBeVisible();
  await page.getByRole('button', { name: 'Paste a Link' }).click();
  await page.getByLabel('Video URL').fill('https://www.youtube.com/watch?v=fixtureJourney');
  await expect(page.getByText('YouTube requires an authorized MP4')).toBeVisible();
  await page.getByLabel('I confirm I have the right to process this video.').click();
  await page.getByRole('button', { name: 'Sign in to continue' }).click();
  await expect(page).toHaveURL(/\/auth\?returnTo=%2F/);
  await expect(page.getByRole('button', { name: 'Create account' })).toBeVisible();
  await snap(page, testInfo, '01-visible-signup');

  await page.getByLabel('Email').fill('journey@example.test');
  await page.getByLabel('Password').fill('correct horse battery staple');
  await page.getByRole('button', { name: 'Create account' }).click();
  await expect(page.getByTestId('status-verification')).toContainText('journey@example.test');
  await page.getByRole('button', { name: 'Resend verification email' }).click();
  await expect(page.getByText('A new verification link has been sent.').first()).toBeVisible();

  // The action URL stands in for clicking the fixture email; no email transport exists.
  await page.goto('/auth/action?mode=verifyEmail&oobCode=journey-valid');
  await expect(page.getByTestId('status-email-verified')).toContainText(/verified/i);
  await page.getByRole('button', { name: 'Sign in to continue' }).click();
  await expect(page).toHaveURL('/auth?mode=signin');
  await page.getByLabel('Email').fill('journey@example.test');
  await page.getByLabel('Password').fill('correct horse battery staple');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page).toHaveURL('/');
  await expect(page.getByRole('button', { name: 'Continue to upload' })).toBeDisabled();
  expect(fixture.counts()).toEqual({ completeCalls: 0, transfers: 0, searches: 0, creations: 0 });
  // PRODUCT-INTENT-004 records the lost intent. Re-enter it to continue the
  // remaining independent journey checks without treating the defect as a pass.
  await page.getByRole('button', { name: 'Paste a Link' }).click();
  await page.getByLabel('Video URL').fill('https://www.youtube.com/watch?v=fixtureJourney');

  await page.getByLabel('I confirm I have the right to process this video.').click();
  await page.getByLabel('Allow owner-only source playback').click();
  await page.getByRole('button', { name: 'Start processing' }).click();
  expect(fixture.counts().creations).toBe(1);
  await expect(page.getByText('Choose your authorized MP4', { exact: true })).toBeVisible();
  await expect(page.getByText(/source link.*stays attached/i)).toBeVisible();
  await snap(page, testInfo, '02-file-fallback');

  const chooser = page.waitForEvent('filechooser');
  await page.getByTestId('dropzone-video-file').press('Enter');
  await (await chooser).setFiles(mediaPath);
  await expect(page.getByTestId('text-selected-filename')).toHaveText('synthetic-journey-h264.mp4');
  await page.getByTestId('button-upload-file').click();
  await expect(page.getByText(/file transfer finished.*completion/i)).toBeVisible();
  expect(fixture.counts()).toMatchObject({ completeCalls: 1, transfers: 1 });
  await page.getByRole('button', { name: 'Retry server confirmation' }).click();
  await expect(page.getByText(/Ingestion: (processing|ready)/i)).toBeVisible();
  expect(fixture.counts()).toMatchObject({ completeCalls: 2, transfers: 1 });
  expect(fixture.receivedTransfers()).toBe(1);

  await page.reload();
  await expect(page.getByText('Ready to search').first()).toBeVisible();
  await snap(page, testInfo, '03-server-ready');
  await page.getByTestId('input-scene-query').fill('moving color bars');
  await page.getByTestId('button-search').click();
  await expect(page.getByRole('button', { name: /Candidate 1, 0:01 to 0:03/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /Candidate 2, 0:03 to 0:05/ })).toBeVisible();
  await expect(page.getByAltText(/Indexed source candidate near 0:01/)).toBeVisible();
  await page.getByTestId('button-match-2').focus();
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('button-match-2')).toHaveAttribute('aria-pressed', 'true');

  const video = page.getByLabel('Private indexed source video');
  await expect(video).toBeVisible();
  await video.evaluate(async element => {
    const mediaElement = element as HTMLVideoElement;
    mediaElement.pause();
    mediaElement.currentTime = 0;
    await mediaElement.play();
  });
  await expect.poll(() => video.evaluate(element => (element as HTMLVideoElement).currentTime)).toBeGreaterThan(0.4);
  await video.evaluate(element => { (element as HTMLVideoElement).currentTime = 4; });
  await expect.poll(() => video.evaluate(element => (element as HTMLVideoElement).currentTime)).toBeGreaterThan(3.8);
  await page.getByTestId('button-match-1').click();
  await expect.poll(() => video.evaluate(element => (element as HTMLVideoElement).currentTime)).toBeLessThan(1.6);
  await snap(page, testInfo, '04-ranked-playback');

  await page.getByTestId('switch-private-playback').click();
  await expect(video).toHaveCount(0);
  await expect(page.getByText(/Enable private source playback/)).toBeVisible();
  fixture.makePlaybackUnavailable();
  await page.getByTestId('switch-private-playback').click();
  await expect(page.getByText(/authorized, but the source is not currently available/)).toBeVisible();

  const mutationsBeforeReload = fixture.requests.filter(request =>
    request.method() !== 'GET' && request.method() !== 'HEAD' && request.method() !== 'OPTIONS').length;
  await page.reload();
  await expect(page.getByTestId(`button-history-${searchId}`)).toContainText('moving color bars');
  await page.getByTestId(`button-history-${searchId}`).focus();
  await page.keyboard.press('Enter');
  await expect(page.getByTestId(`button-history-${searchId}`)).toHaveAttribute('aria-pressed', 'true');
  expect(fixture.counts().searches).toBe(1);
  expect(fixture.requests.filter(request =>
    request.method() !== 'GET' && request.method() !== 'HEAD' && request.method() !== 'OPTIONS').length).toBe(mutationsBeforeReload);

  await page.getByRole('button', { name: 'Log out' }).click();
  await expect(page.getByRole('heading', { name: 'Find scenes in your videos.' })).toBeVisible();
  await signIn(page);
  await page.getByRole('button', { name: 'Resume session' }).click();
  await expect(page).toHaveURL(`/imports/${importId}`);
  await expect(page.getByTestId(`button-history-${searchId}`)).toContainText('moving color bars');
  expect(fixture.counts()).toMatchObject({ searches: 1, transfers: 1 });

  fixture.expire();
  await page.reload();
  await expect(page.getByText('Your private analysis', { exact: true })).toBeVisible();
  await expect(page.getByText('moving color bars')).toHaveCount(0);
  await page.getByTestId('button-sign-in').click();
  await page.getByLabel('Email').fill('second.owner@example.test');
  await page.getByLabel('Password').fill('correct horse battery staple');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByText('Analysis unavailable', { exact: true })).toBeVisible();
  await expect(page.getByText('moving color bars')).toHaveCount(0);
  const exchanges = fixture.requests.filter(request =>
    new URL(request.url()).pathname === '/api/auth/firebase/session');
  const finalClaims = JSON.parse(Buffer.from(exchanges.at(-1)!.postDataJSON().idToken.split('.')[1], 'base64url').toString());
  expect(finalClaims.sub).toBe('owner-b');
  for (const path of [
    `/api/imports/${importId}`,
    `/api/imports/${importId}/searches`,
    `/api/imports/${importId}/searches/${searchId}/frames/1`,
    `/api/imports/${importId}/source`,
  ]) {
    expect(await page.evaluate(async url => (await fetch(url)).status, path)).toBe(404);
  }
  expect(fixture.counts()).toMatchObject({ searches: 1, transfers: 1 });
  expect(fixture.counts().creations).toBe(1);
  expect(fixture.unhandled).toEqual([]);
});

test('PRODUCT-INTENT-004 verification and fresh sign-in preserve pending entry intent', async ({ page }, testInfo) => {
  const fixture = await journeyHarness(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Paste a Link' }).click();
  await page.getByLabel('Video URL').fill('https://www.youtube.com/watch?v=fixtureJourney');
  await page.getByLabel('I confirm I have the right to process this video.').click();
  await page.getByRole('button', { name: 'Sign in to continue' }).click();
  await page.goto('/auth/action?mode=verifyEmail&oobCode=journey-valid');
  await expect(page.getByTestId('status-email-verified')).toBeVisible();
  await page.getByRole('button', { name: 'Sign in to continue' }).click();
  await expect(page).toHaveURL('/auth?mode=signin');
  await page.getByLabel('Email').fill('journey@example.test');
  await page.getByLabel('Password').fill('correct horse battery staple');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page).toHaveURL('/');
  await page.screenshot({ path: testInfo.outputPath('PRODUCT-INTENT-004.png'), fullPage: true });
  expect(fixture.counts().creations).toBe(0);
  test.fail(true, 'PRODUCT-INTENT-004: verification/sign-in loses the pending source link');
  await expect(page.getByLabel('Video URL')).toHaveValue('https://www.youtube.com/watch?v=fixtureJourney');
});

test('worker outage blocks reservation and unknown transports fail closed', async ({ page }) => {
  const fixture = await journeyHarness(page, { workerAvailable: false });
  await signIn(page);
  await expect(page.getByText('Index Worker Unavailable')).toBeVisible();
  await page.getByLabel('I confirm I have the right to process this video.').click();
  await expect(page.getByRole('button', { name: 'Continue to upload' })).toBeDisabled();
  await page.evaluate(() => Promise.allSettled([
    fetch('https://api.twelvelabs.io/v1.3/tasks', { method: 'POST', mode: 'no-cors', body: 'blocked' }),
    fetch('/api/imports/unknown-mutation', { method: 'POST', body: '{}' }),
  ]));
  expect(fixture.unhandled).toEqual(expect.arrayContaining([
    'POST https://api.twelvelabs.io/v1.3/tasks',
    'POST /api/imports/unknown-mutation',
  ]));
  expect(fixture.counts()).toEqual({ completeCalls: 0, transfers: 0, searches: 0, creations: 0 });
});

test('invalid chooser input is rejected and cancellation removes the reserved import', async ({ page }) => {
  const fixture = await journeyHarness(page);
  await signIn(page);
  await page.getByLabel('I confirm I have the right to process this video.').click();
  await page.getByRole('button', { name: 'Continue to upload' }).click();
  await expect(page.getByText('Choose your authorized MP4', { exact: true })).toBeVisible();

  const fileInput = page.locator('input[type=file]');
  await fileInput.setInputFiles({ name: 'not-video.txt', mimeType: 'text/plain', buffer: Buffer.from('not media') });
  await expect(page.getByText('Only MP4 files are supported.')).toBeVisible();
  expect(fixture.counts().transfers).toBe(0);

  const chooser = page.waitForEvent('filechooser');
  await page.getByTestId('dropzone-video-file').press('Enter');
  await (await chooser).setFiles(mediaPath);
  await page.getByTestId('button-cancel-upload').click();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog.getByRole('heading', { name: 'Cancel this upload?' })).toBeVisible();
  await dialog.getByRole('button', { name: 'Cancel upload', exact: true }).click();
  await expect(page.getByText(/Ingestion: cancelled/i)).toBeVisible();
  await expect(page.getByTestId('dropzone-video-file')).toHaveCount(0);
  expect(fixture.counts()).toEqual({ completeCalls: 0, transfers: 0, searches: 0, creations: 1 });
  expect(fixture.unhandled).toEqual([]);
});

test('supported retrievable link can complete without a browser file transfer', async ({ page }) => {
  const fixture = await journeyHarness(page);
  await signIn(page);
  await page.getByRole('button', { name: 'Paste a Link' }).click();
  await page.getByLabel('Video URL').fill('https://vimeo.com/fixture-vimeo');
  await page.getByLabel('I confirm I have the right to process this video.').click();
  await page.getByRole('button', { name: 'Start processing' }).click();
  await expect(page.getByText('Ready to search').first()).toBeVisible();
  await expect(page.getByText('Linked platform source', { exact: true })).toBeVisible();
  await expect(page.getByTestId('link-external-source')).toContainText('Vimeo');
  await expect(page.getByTitle('Official YouTube linked video')).toHaveCount(0);
  expect(fixture.counts()).toEqual({ completeCalls: 0, transfers: 0, searches: 0, creations: 1 });
  await page.getByTestId('button-cancel-import').click();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog.getByRole('heading', { name: 'Delete this import?' })).toBeVisible();
  await dialog.getByRole('button', { name: 'Delete import' }).click();
  await expect(page.getByText(/Ingestion: cancelled/i)).toBeVisible();
  expect(fixture.unhandled).toEqual([]);
});

test('interrupted transfer requires re-selection and retries without a second import', async ({ page }) => {
  const fixture = await journeyHarness(page, { interruptFirstTransfer: true });
  await signIn(page);
  await page.getByLabel('I confirm I have the right to process this video.').click();
  await page.getByRole('button', { name: 'Continue to upload' }).click();
  await page.locator('input[type=file]').setInputFiles(mediaPath);
  await page.getByTestId('button-upload-file').click();
  await expect(page.getByRole('alert')).toContainText(/request|network|upload/i);
  expect(fixture.counts()).toMatchObject({ creations: 1, transfers: 1 });

  await page.reload();
  await expect(page.getByTestId('text-selected-filename')).toHaveCount(0);
  await page.locator('input[type=file]').setInputFiles(mediaPath);
  await page.getByTestId('button-upload-file').click();
  await expect(page.getByText(/file transfer finished.*completion/i)).toBeVisible();
  await page.getByRole('button', { name: 'Retry server confirmation' }).click();
  await expect(page.getByText(/Ingestion: (processing|ready)/i)).toBeVisible();
  expect(fixture.counts()).toMatchObject({ creations: 1, transfers: 2, completeCalls: 2 });
  expect(fixture.receivedTransfers()).toBe(1);
  expect(fixture.unhandled).toEqual([]);
});

test('private empty, failed, and uncertain searches never retry automatically', async ({ page }) => {
  const fixture = await journeyHarness(page, {
    privateSearchOutcomes: ['empty', 'failed', 'uncertain'],
  });
  await signIn(page);
  await page.getByRole('button', { name: 'Paste a Link' }).click();
  await page.getByLabel('Video URL').fill('https://vimeo.com/fixture-vimeo');
  await page.getByLabel('I confirm I have the right to process this video.').click();
  await page.getByRole('button', { name: 'Start processing' }).click();
  const query = page.getByTestId('input-scene-query');
  await query.fill('private empty');
  await page.getByTestId('button-search').click();
  await expect(page.getByText('No relevant candidates were returned. Try a different description.')).toBeVisible();
  await query.fill('private failure');
  await page.getByTestId('button-search').click();
  await expect(page.getByRole('alert')).toContainText('Private search failed');
  await query.fill('private uncertain');
  await page.getByTestId('button-search').click();
  await expect(page.getByRole('alert')).toContainText('Private search outcome uncertain');
  await page.waitForTimeout(1_000);
  expect(fixture.counts()).toMatchObject({ creations: 1, searches: 1 });
  expect(fixture.requests.filter(request =>
    request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/searches'))).toHaveLength(3);
});

test('silent import limits mode and keeps missing frame and media fallbacks readable', async ({ page }) => {
  const fixture = await journeyHarness(page, { silent: true, frameUnavailable: true });
  await signIn(page);
  await page.getByRole('button', { name: 'Paste a Link' }).click();
  await page.getByLabel('Video URL').fill('https://vimeo.com/fixture-vimeo');
  await page.getByLabel('I confirm I have the right to process this video.').click();
  await page.getByRole('button', { name: 'Start processing' }).click();
  await expect(page.getByText('This video is silent. Visual search remains available; audio search is disabled.')).toBeVisible();
  await expect(page.getByRole('option', { name: 'Audio' })).toHaveCount(0);
  fixture.makePlaybackUnavailable();
  await page.getByTestId('switch-private-playback').click();
  await expect(page.getByText(/authorized, but the source is not currently available/)).toBeVisible();
  await page.getByTestId('input-scene-query').fill('missing fixture evidence');
  await page.getByTestId('button-search').click();
  await expect(page.getByText('Frame preview unavailable')).toBeVisible();
  expect(fixture.unhandled).toEqual([]);
});

test('PRODUCT-A11Y-HEADINGS-003 import card titles expose heading semantics', async ({ page }, testInfo) => {
  const fixture = await journeyHarness(page);
  await signIn(page);
  await page.getByLabel('I confirm I have the right to process this video.').click();
  await page.getByRole('button', { name: 'Continue to upload' }).click();
  await expect(page.getByText('Choose your authorized MP4', { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('PRODUCT-A11Y-HEADINGS-003.png'), fullPage: true });
  expect(fixture.counts().creations).toBe(1);
  test.fail(true, 'PRODUCT-A11Y-HEADINGS-003: visible import card titles are divs rather than headings');
  await expect(page.getByRole('heading', { name: 'Choose your authorized MP4' })).toBeVisible();
});