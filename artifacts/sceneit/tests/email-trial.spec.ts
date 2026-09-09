import { expect, test, type Page, type Request, type Route } from '@playwright/test';

type SessionOptions = {
  configured?: boolean;
  rollout?: boolean;
  user?: {
    id: string;
    email: string;
    emailVerified: boolean;
  } | null;
  importsUsed?: number;
  searchesUsed?: number;
  importLimit?: number;
  searchLimit?: number;
  privateReason?: string;
  currentImportState?: 'expired';
};

const firebaseConfig = {
  apiKey: 'fixture-api-key',
  authDomain: 'fixture.firebaseapp.com',
  projectId: 'fixture-project',
  appId: '1:123:web:fixture',
};

function token(email: string, verified: boolean) {
  const encode = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url');
  return `${encode({ alg: 'none', typ: 'JWT' })}.${encode({
    aud: firebaseConfig.projectId,
    auth_time: 1_735_689_600,
    email,
    email_verified: verified,
    exp: 4_102_444_800,
    firebase: { sign_in_provider: 'password' },
    iat: 1_735_689_600,
    iss: `https://securetoken.google.com/${firebaseConfig.projectId}`,
    sub: `fixture-${verified ? 'verified' : 'unverified'}`,
    user_id: `fixture-${verified ? 'verified' : 'unverified'}`,
  })}.fixture-signature`;
}

function session(options: SessionOptions = {}) {
  const configured = options.configured ?? true;
  const rollout = options.rollout ?? true;
  const user = options.user ?? null;
  const importLimit = options.importLimit ?? 3;
  const searchLimit = options.searchLimit ?? 50;
  const importsUsed = options.importsUsed ?? 0;
  const searchesUsed = options.searchesUsed ?? 0;
  const allowed = user?.emailVerified === true && rollout && configured;
  return {
    csrfToken: user ? 'application-csrf' : null,
    pilotAdmitted: false,
    user: user && {
      ...user,
      firstName: null,
      provider: 'firebase',
    },
    capabilities: {
      replit: true,
      emailPassword: configured && rollout,
      publicTrialEnabled: rollout,
      unavailableReason: !configured
        ? 'firebase_not_configured'
        : !rollout ? 'public_trial_disabled' : null,
      firebaseConfig: configured ? firebaseConfig : null,
    },
    privateAccess: {
      allowed,
      reason: allowed
        ? 'ready'
        : options.privateReason ?? (!user ? 'authentication_required'
          : !user.emailVerified ? 'verification_required'
            : !configured ? 'firebase_not_configured' : 'public_trial_disabled'),
    },
    usage: user ? {
      importsUsed,
      importLimit,
      importsRemaining: Math.max(0, importLimit - importsUsed),
      searchesUsed,
      searchLimit,
      searchesRemaining: Math.max(0, searchLimit - searchesUsed),
      lifetime: true,
    } : null,
  };
}

async function reply(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function interceptedApp(page: Page, initial: SessionOptions = {}) {
  let current = { ...initial };
  const requests: Request[] = [];
  const unhandled: string[] = [];

  await page.context().route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const isMutation = !['GET', 'HEAD', 'OPTIONS'].includes(request.method());
    if (isMutation || url.hostname !== '127.0.0.1' || url.pathname.startsWith('/api/')) {
      requests.push(request);
    }

    if (url.hostname === 'identitytoolkit.googleapis.com') {
      if (url.pathname.endsWith('/accounts:signUp')) {
        const email = (request.postDataJSON() as { email: string }).email;
        return reply(route, {
          kind: 'identitytoolkit#SignupNewUserResponse',
          localId: 'fixture-unverified',
          email,
          emailVerified: false,
          idToken: token(email, false),
          refreshToken: 'fixture-refresh-token',
          expiresIn: '3600',
        });
      }
      if (url.pathname.endsWith('/accounts:signInWithPassword')) {
        const email = (request.postDataJSON() as { email: string }).email;
        const verified = email !== 'waiting@example.test';
        return reply(route, {
          kind: 'identitytoolkit#VerifyPasswordResponse',
          localId: verified ? 'fixture-verified' : 'fixture-unverified',
          email,
          registered: true,
          idToken: token(email, verified),
          refreshToken: 'fixture-refresh-token',
          expiresIn: '3600',
        });
      }
      if (url.pathname.endsWith('/accounts:sendOobCode')) {
        return reply(route, { kind: 'identitytoolkit#GetOobConfirmationCodeResponse' });
      }
      if (url.pathname.endsWith('/accounts:lookup')) {
        const lookupToken = (request.postDataJSON() as { idToken?: string }).idToken;
        let active = current.user;
        if (lookupToken) {
          const claims = JSON.parse(Buffer.from(lookupToken.split('.')[1], 'base64url').toString()) as {
            email: string;
            email_verified: boolean;
            sub: string;
          };
          active = {
            id: claims.sub,
            email: claims.email,
            emailVerified: claims.email_verified,
          };
        }
        return reply(route, {
          kind: 'identitytoolkit#GetAccountInfoResponse',
          users: active ? [{
            localId: active.id,
            email: active.email,
            emailVerified: active.emailVerified,
            providerUserInfo: [],
          }] : [],
        });
      }
      if (url.pathname.endsWith('/accounts:update')) {
        const body = request.postDataJSON() as { oobCode?: string };
        if (body.oobCode === 'expired-fixture') {
          return reply(route, { error: { code: 400, message: 'EXPIRED_OOB_CODE' } }, 400);
        }
        return reply(route, {
          kind: 'identitytoolkit#SetAccountInfoResponse',
          email: current.user?.email ?? 'verified@example.test',
          emailVerified: true,
        });
      }
      if (url.pathname.endsWith('/accounts:resetPassword')) {
        const body = request.postDataJSON() as { oobCode?: string; newPassword?: string };
        if (body.oobCode === 'expired-fixture') {
          return reply(route, { error: { code: 400, message: 'EXPIRED_OOB_CODE' } }, 400);
        }
        return reply(route, {
          kind: 'identitytoolkit#ResetPasswordResponse',
          email: 'recover@example.test',
          requestType: 'PASSWORD_RESET',
        });
      }
      unhandled.push(`${request.method()} ${request.url()}`);
      return route.abort('blockedbyclient');
    }

    if (url.hostname === 'securetoken.googleapis.com') {
      return reply(route, {
        access_token: token(current.user?.email ?? 'verified@example.test', true),
        expires_in: '3600',
        refresh_token: 'fixture-refresh-token',
        token_type: 'Bearer',
        user_id: 'fixture-verified',
        project_id: firebaseConfig.projectId,
      });
    }

    if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
      return route.abort('blockedbyclient');
    }

    if (url.hostname !== '127.0.0.1') {
      unhandled.push(`${request.method()} ${request.url()}`);
      return route.abort('blockedbyclient');
    }

    if (!url.pathname.startsWith('/api/')) {
      if (['GET', 'HEAD', 'OPTIONS'].includes(request.method())) return route.continue();
      unhandled.push(`${request.method()} ${url.pathname}`);
      return route.abort('blockedbyclient');
    }
    if (url.pathname === '/api/auth/session' || url.pathname === '/api/auth/user') {
      return reply(route, session(current));
    }
    if (url.pathname === '/api/auth/firebase/challenge') {
      return reply(route, { csrfToken: 'exchange-csrf' });
    }
    if (url.pathname === '/api/auth/firebase/session') {
      expect(request.method()).toBe('POST');
      expect(request.headers()['x-csrf-token']).toBe('exchange-csrf');
      const idToken = (request.postDataJSON() as { idToken: string }).idToken;
      expect(idToken).not.toContain('fixture-refresh-token');
      const claims = JSON.parse(Buffer.from(idToken.split('.')[1], 'base64url').toString()) as {
        email: string;
        email_verified: boolean;
        sub: string;
      };
      current = {
        ...current,
        user: { id: claims.sub, email: claims.email, emailVerified: claims.email_verified },
      };
      return reply(route, session(current));
    }
    if (url.pathname === '/api/logout') {
      expect(request.method()).toBe('POST');
      current = { ...current, user: null };
      return reply(route, { success: true });
    }
    if (url.pathname === '/api/imports/config') {
      return reply(route, {
        maxBytes: 200_000_000,
        minDurationSeconds: 4,
        maxDurationSeconds: 1200,
        retentionDays: 7,
        ownerImportLimit: current.importLimit ?? 3,
        appImportLimit: 30,
        ownerSearchLimit: current.searchLimit ?? 50,
        appSearchLimit: 500,
        workerAvailable: true,
      });
    }
    if (url.pathname === '/api/imports/trial-import') {
      return reply(route, {
        id: 'trial-import',
        title: 'Retained trial import',
        entryMethod: 'upload',
        sourceKind: 'file',
        sourceUrl: null,
        externalId: null,
        state: 'ready',
        statusMessage: 'Indexed',
        progressPercent: 100,
        errorCode: null,
        durationSeconds: 30,
        fileSizeBytes: 1024,
        hasAudio: true,
        sourcePlaybackAvailable: false,
        sourcePlaybackUrl: null,
        playbackAuthorized: false,
        timelineStatus: 'not_applicable',
        createdAt: '2025-01-01T00:00:00Z',
        updatedAt: '2025-01-01T00:00:00Z',
        expiresAt: '2030-01-01T00:00:00Z',
        searchesUsed: current.searchesUsed ?? 50,
        searchLimit: current.searchLimit ?? 50,
        importsUsed: current.importsUsed ?? 3,
        importLimit: current.importLimit ?? 3,
        budgetReserved: true,
      });
    }
    if (url.pathname === '/api/imports/private-intent') {
      return reply(route, { error: 'Not found', code: 'NOT_FOUND' }, 404);
    }
    if (url.pathname === '/api/imports/trial-import/searches' && request.method() === 'GET') {
      return reply(route, [{
        id: 'retained-search',
        query: 'saved result remains readable',
        modality: 'visual',
        createdAt: '2025-01-01T00:00:00Z',
        latencyMs: 100,
        provider: 'Twelve Labs',
        partial: false,
        matches: [],
      }]);
    }
    if (url.pathname === '/api/imports/current' && request.method() === 'GET') {
      if (current.currentImportState === 'expired') {
        return reply(route, {
          id: 'expired-import',
          title: 'Expired private import',
          entryMethod: 'upload',
          sourceKind: 'file',
          sourceUrl: null,
          externalId: null,
          state: 'expired',
          statusMessage: 'Expired',
          progressPercent: 100,
          errorCode: null,
          durationSeconds: 30,
          fileSizeBytes: 1024,
          hasAudio: true,
          sourcePlaybackAvailable: false,
          sourcePlaybackUrl: null,
          playbackAuthorized: false,
          timelineStatus: 'not_applicable',
          createdAt: '2025-01-01T00:00:00Z',
          updatedAt: '2025-01-08T00:00:00Z',
          expiresAt: '2025-01-08T00:00:00Z',
          searchesUsed: current.searchesUsed ?? 17,
          searchLimit: current.searchLimit ?? 50,
          importsUsed: current.importsUsed ?? 2,
          importLimit: current.importLimit ?? 3,
          budgetReserved: true,
        });
      }
      return route.fulfill({ status: 404 });
    }

    unhandled.push(`${request.method()} ${url.pathname}`);
    return reply(route, { error: 'Blocked unhandled test request', code: 'TEST_BLOCK' }, 599);
  });

  return {
    requests,
    unhandled,
    setSession(next: SessionOptions) {
      current = { ...next };
    },
  };
}

test('public onboarding is available without reading pilot proof or private work', async ({ page }) => {
  const fixture = await interceptedApp(page);
  await page.goto('/');

  await expect(page.getByRole('heading', { name: 'Find scenes in your videos.' })).toBeVisible();
  await expect(page.getByRole('link', { name: /sign up|create account/i }).or(
    page.getByRole('button', { name: /sign up|create account/i }),
  )).toBeVisible();
  expect(fixture.requests.filter(request => new URL(request.url()).pathname.startsWith('/api/proof'))).toEqual([]);
  expect(fixture.requests.filter(request =>
    new URL(request.url()).pathname === '/api/imports/current',
  )).toEqual([]);
  expect(fixture.requests.filter(request => request.method() !== 'GET')).toEqual([]);
  expect(fixture.unhandled).toEqual([]);
});

test('fixture blocks every unhandled provider and local mutation', async ({ page }) => {
  const fixture = await interceptedApp(page);
  await page.goto('/');
  await page.evaluate(async () => {
    await Promise.allSettled([
      fetch('https://api.twelvelabs.io/v1.3/tasks', {
        method: 'POST',
        mode: 'no-cors',
        body: 'must never leave Playwright',
      }),
      fetch('/api/imports/unexpected', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      }),
    ]);
  });

  expect(fixture.unhandled).toContain('POST https://api.twelvelabs.io/v1.3/tasks');
  expect(fixture.unhandled).toContain('POST /api/imports/unexpected');
});

test('missing Firebase configuration and rollout off preserve Replit entry', async ({ page }) => {
  await interceptedApp(page, { configured: false });
  await page.goto('/auth');
  await expect(page.getByText('Email signup is not currently available.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Continue with Replit' })).toBeVisible();
  await expect(page.getByLabel('Email')).toHaveCount(0);

  await page.context().unroute('**/*');
  await interceptedApp(page, { configured: true, rollout: false });
  await page.goto('/auth');
  await expect(page.getByRole('button', { name: /Replit/ })).toBeVisible();
  await expect(page.getByLabel('Email')).toHaveCount(0);
});

test('signup and resend use only intercepted Firebase endpoints and remain unverified', async ({ page }) => {
  const fixture = await interceptedApp(page);
  await page.goto('/auth');
  await page.getByLabel('Email').fill('new.user@example.test');
  await page.getByLabel('Password').fill('correct horse battery staple');
  await page.getByRole('button', { name: 'Create account' }).click();

  await expect(page.getByTestId('status-verification')).toContainText('new.user@example.test');
  await page.getByRole('button', { name: 'Resend verification email' }).click();
  await expect(page.getByText('A new verification link has been sent.').first()).toBeVisible();

  expect(fixture.requests.filter(request =>
    new URL(request.url()).hostname === 'identitytoolkit.googleapis.com'
    && new URL(request.url()).pathname.endsWith('/accounts:sendOobCode'),
  )).toHaveLength(2);
  expect(fixture.requests.some(request =>
    new URL(request.url()).pathname === '/api/auth/firebase/session',
  )).toBe(false);
  expect(fixture.requests.some(request =>
    new URL(request.url()).pathname.startsWith('/api/imports')
    && request.method() !== 'GET',
  )).toBe(false);
  expect(fixture.unhandled).toEqual([]);
});

test('sign in and generic password reset use intercepted identity requests', async ({ page }) => {
  const fixture = await interceptedApp(page);
  await page.goto('/auth');
  await page.getByTestId('button-mode-signin').click();
  await page.getByLabel('Email').fill('verified@example.test');
  await page.getByLabel('Password').fill('correct horse battery staple');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page).toHaveURL('/');

  fixture.setSession({});
  await page.goto('/auth');
  await page.getByTestId('button-mode-signin').click();
  await page.getByRole('button', { name: 'Forgot password?' }).click();
  await page.getByLabel('Email').fill('unknown@example.test');
  await page.getByRole('button', { name: 'Send reset link' }).click();
  await expect(page.getByRole('heading', { name: 'Check your email' })).toBeVisible();
  await expect(page.getByText(/If an account exists/)).toBeVisible();

  expect(fixture.requests.some(request =>
    new URL(request.url()).pathname.endsWith('/accounts:signInWithPassword'),
  )).toBe(true);
  expect(fixture.requests.some(request =>
    new URL(request.url()).pathname.endsWith('/accounts:sendOobCode'),
  )).toBe(true);
  expect(fixture.unhandled).toEqual([]);
});

test('verification and reset action links have success and expired-link recovery', async ({ page }) => {
  const fixture = await interceptedApp(page);
  await page.goto('/auth/action?mode=verifyEmail&oobCode=valid-fixture');
  await expect(page.getByTestId('status-email-verified')).toContainText(/verified/i);
  await expect(page.getByRole('button', { name: 'Sign in to continue' })).toBeVisible();
  await expect(page).toHaveURL('/auth/action');

  await page.goto('/auth/action?mode=resetPassword&oobCode=valid-fixture');
  await expect(page.getByTestId('text-reset-email')).toContainText('recover@example.test');
  await page.getByTestId('input-new-password').fill('replacement password');
  await page.getByTestId('button-reset-password').click();
  await expect(page.getByTestId('status-password-reset')).toContainText(/sign in/i);

  for (const mode of ['verifyEmail', 'resetPassword']) {
    await page.goto(`/auth/action?mode=${mode}&oobCode=expired-fixture`);
    await expect(page.getByTestId('status-action-error')).toContainText(/expired.*request|request.*new/i);
    await expect(page.getByRole('button', { name: /Sign in to resend|Request a new reset link|Request a new link/i })).toBeVisible();
  }
  expect(fixture.unhandled).toEqual([]);
});

test('PRODUCT-AUTH-RECOVERY-001 expired password reset recovery opens a usable reset form', async ({ page }) => {
  const fixture = await interceptedApp(page);
  await page.goto('/auth/action?mode=resetPassword&oobCode=expired-fixture');
  await page.getByRole('button', { name: /Request a new reset link|Request a new link/i }).click();
  await expect(page).toHaveURL('/auth?mode=reset&returnTo=%2F');
  await expect(page.getByRole('heading', { name: 'Reset password' })).toBeVisible();
  await page.getByLabel('Email').fill('recover@example.test');
  await page.getByRole('button', { name: 'Send reset link' }).click();
  await expect(page.getByRole('heading', { name: 'Check your email' })).toBeVisible();
  expect(fixture.requests.filter(request =>
    new URL(request.url()).pathname.endsWith('/accounts:sendOobCode'),
  )).toHaveLength(1);
  expect(fixture.unhandled).toEqual([]);
});

test('PRODUCT-AUTH-RETURN-002 verification success preserves protected return intent through fresh sign-in', async ({ page }) => {
  const fixture = await interceptedApp(page);
  await page.goto('/auth/action?mode=verifyEmail&oobCode=valid-fixture&returnTo=%2Fimports%2Fprivate-intent');
  await page.getByRole('button', { name: 'Sign in to continue' }).click();
  await expect(page).toHaveURL('/auth?mode=signin&returnTo=%2Fimports%2Fprivate-intent');
  await page.getByLabel('Email').fill('verified@example.test');
  await page.getByLabel('Password').fill('correct horse battery staple');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page).toHaveURL('/imports/private-intent');
  expect(fixture.requests.some(request =>
    request.method() !== 'GET'
    && new URL(request.url()).pathname.startsWith('/api/imports'),
  )).toBe(false);
  expect(fixture.unhandled).toEqual([]);
});

test('unsafe action returnTo is rejected and one-time action credentials are scrubbed', async ({ page }) => {
  const fixture = await interceptedApp(page);
  await page.goto('/auth/action?mode=verifyEmail&oobCode=valid-fixture&returnTo=https%3A%2F%2Fevil.example%2Fsteal');
  await expect(page).toHaveURL('/auth/action');
  await expect(page.getByTestId('status-email-verified')).toBeVisible();
  await page.getByRole('button', { name: 'Sign in to continue' }).click();
  await expect(page).toHaveURL('/auth?mode=signin&returnTo=%2F');
  expect(fixture.requests.some(request => request.url().startsWith('https://evil.example'))).toBe(false);
  expect(fixture.unhandled).toEqual([]);
});

test('expired verification recovery supports fresh sign-in and resend without app access', async ({ page }) => {
  const fixture = await interceptedApp(page);
  await page.goto('/auth/action?mode=verifyEmail&oobCode=expired-fixture&returnTo=%2Fimports%2Fprivate-intent');
  await expect(page.getByTestId('status-action-error')).toContainText(/expired.*request|request.*new/i);
  await page.getByRole('button', { name: /Sign in to resend verification/i }).click();
  await expect(page).toHaveURL('/auth?mode=signin&returnTo=%2Fimports%2Fprivate-intent');
  await page.getByLabel('Email').fill('waiting@example.test');
  await page.getByLabel('Password').fill('correct horse battery staple');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByTestId('status-verification')).toContainText('waiting@example.test');
  await page.getByRole('button', { name: 'Resend verification email' }).click();
  await expect(page.getByText('A new verification link has been sent.').first()).toBeVisible();
  expect(fixture.requests.filter(request =>
    new URL(request.url()).pathname.endsWith('/accounts:sendOobCode'),
  )).toHaveLength(1);
  expect(fixture.requests.some(request =>
    new URL(request.url()).pathname === '/api/auth/firebase/session',
  )).toBe(false);
  expect(fixture.unhandled).toEqual([]);
});

test('successful password reset preserves a safe destination through explicit sign-in', async ({ page }) => {
  const fixture = await interceptedApp(page);
  await page.goto('/auth/action?mode=resetPassword&oobCode=valid-fixture&returnTo=%2Fimports%2Fprivate-intent');
  await expect(page).toHaveURL('/auth/action');
  await page.getByTestId('input-new-password').fill('replacement password');
  await page.getByTestId('button-reset-password').click();
  await expect(page.getByTestId('status-password-reset')).toContainText(/sign in/i);
  await page.getByTestId('button-signin-after-reset').click();
  await expect(page).toHaveURL('/auth?mode=signin&returnTo=%2Fimports%2Fprivate-intent');
  await page.getByLabel('Email').fill('recover@example.test');
  await page.getByLabel('Password').fill('replacement password');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page).toHaveURL('/imports/private-intent');
  expect(fixture.requests.some(request =>
    request.method() !== 'GET'
    && new URL(request.url()).pathname.startsWith('/api/imports'),
  )).toBe(false);
  expect(fixture.unhandled).toEqual([]);
});

test('verification and reset completion close an existing app session before fresh sign-in', async ({ page }) => {
  const existingUser = { id: 'existing-owner', email: 'existing@example.test', emailVerified: true };
  const fixture = await interceptedApp(page, { user: existingUser });

  await page.goto('/auth/action?mode=verifyEmail&oobCode=valid-fixture');
  await expect(page.getByTestId('status-email-verified')).toBeVisible();
  await page.getByRole('button', { name: 'Sign in to continue' }).click();
  await expect(page).toHaveURL('/auth?mode=signin&returnTo=%2F');
  await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible();
  await expect(page.getByLabel('Email')).toBeEnabled();
  await expect(page.getByLabel('Password')).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeDisabled();

  fixture.setSession({ user: existingUser });
  await page.goto('/auth/action?mode=resetPassword&oobCode=valid-fixture');
  await page.getByTestId('input-new-password').fill('replacement password');
  await page.getByTestId('button-reset-password').click();
  await expect(page.getByTestId('status-password-reset')).toBeVisible();
  await page.getByTestId('button-signin-after-reset').click();
  await expect(page).toHaveURL('/auth?mode=signin&returnTo=%2F');
  await expect(page.getByRole('heading', { name: 'Welcome back' })).toBeVisible();
  await expect(page.getByLabel('Email')).toBeEnabled();
  await expect(page.getByLabel('Password')).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeDisabled();

  expect(fixture.requests.filter(request =>
    new URL(request.url()).pathname === '/api/logout',
  )).toHaveLength(2);
  expect(fixture.requests.some(request =>
    request.method() !== 'GET'
    && new URL(request.url()).pathname.startsWith('/api/imports'),
  )).toBe(false);
  expect(fixture.unhandled).toEqual([]);
});

test('signup controls remain keyboard reachable in a narrow layout', async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 640 });
  await interceptedApp(page);
  await page.goto('/auth');
  await page.getByLabel('Email').focus();
  await page.keyboard.type('keyboard@example.test');
  await page.keyboard.press('Tab');
  await page.keyboard.type('correct horse battery staple');
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: 'Create account' })).toBeFocused();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('an unverified email account cannot start processing', async ({ page }) => {
  const fixture = await interceptedApp(page, {
    user: { id: 'unverified-owner', email: 'waiting@example.test', emailVerified: false },
  });
  await page.goto('/');

  await expect(page.getByText(/verify your email/i).first()).toBeVisible();
  await expect(page.getByRole('button', { name: /Continue to upload|Start processing/ })).toBeDisabled();
  expect(fixture.requests.some(request =>
    new URL(request.url()).pathname === '/api/imports/current',
  )).toBe(false);
});

test('verified trial usage remains visible without a current import and after expiry', async ({ page }) => {
  const fixture = await interceptedApp(page, {
    user: { id: 'verified-owner', email: 'verified@example.test', emailVerified: true },
    importsUsed: 2,
    searchesUsed: 17,
  });
  await page.goto('/');

  await expect(page.getByText(/1.*import/i).first()).toBeVisible();
  await expect(page.getByText(/33.*search/i).first()).toBeVisible();
  await expect(page.getByText(/lifetime/i).first()).toBeVisible();
  await expect(page.getByText(/failed.*cancelled|failed and cancelled/i).first()).toBeVisible();
  await expect(page.getByText(/no monthly|never reset/i).first()).toBeVisible();

  fixture.setSession({
    user: { id: 'verified-owner', email: 'verified@example.test', emailVerified: true },
    importsUsed: 2,
    searchesUsed: 17,
    currentImportState: 'expired',
  });
  await page.goto('/');
  await expect(page.getByLabel('Lifetime trial allowance')).toContainText('1 import attempts remaining');
  await expect(page.getByRole('button', { name: 'Resume session' })).toHaveCount(0);
  expect(fixture.unhandled).toEqual([]);
});

test('exhausted trial shows no upgrade and does not implicitly create or search', async ({ page }) => {
  const fixture = await interceptedApp(page, {
    user: { id: 'exhausted-owner', email: 'spent@example.test', emailVerified: true },
    importsUsed: 3,
    searchesUsed: 50,
  });
  await page.goto('/');

  await expect(page.getByText(/no import attempts remaining|import trial.*exhausted|import allowance is used up/i).first()).toBeVisible();
  await expect(page.getByText(/0.*search/i).first()).toBeVisible();
  await expect(page.getByRole('button', { name: /upgrade|checkout|subscribe/i })).toHaveCount(0);
  expect(fixture.requests.some(request =>
    request.method() !== 'GET'
    && /^\/api\/(imports|proof)/.test(new URL(request.url()).pathname),
  )).toBe(false);
  expect(fixture.unhandled).toEqual([]);
});

test('exhaustion preserves an existing import and saved-result reads but disables new search', async ({ page }) => {
  const fixture = await interceptedApp(page, {
    user: { id: 'exhausted-owner', email: 'spent@example.test', emailVerified: true },
    importsUsed: 3,
    searchesUsed: 50,
  });
  await page.goto('/imports/trial-import');

  await expect(page.getByRole('heading', { name: 'Retained trial import' })).toBeVisible();
  await expect(page.getByTestId('button-history-retained-search')).toContainText('saved result remains readable');
  await page.getByRole('textbox', { name: /Describe|scene/i }).fill('must not submit');
  await expect(page.getByTestId('button-search')).toBeDisabled();
  expect(fixture.requests.some(request =>
    request.method() === 'POST'
    && new URL(request.url()).pathname.endsWith('/searches'),
  )).toBe(false);
  expect(fixture.unhandled).toEqual([]);
});

test('Firebase account is denied the independently protected demo', async ({ page }) => {
  const fixture = await interceptedApp(page, {
    user: { id: 'verified-owner', email: 'verified@example.test', emailVerified: true },
  });
  await page.goto('/demo');

  await expect(page.getByRole('heading', { name: 'Pilot access required' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Search scenes' })).toHaveCount(0);
  expect(fixture.requests.filter(request =>
    new URL(request.url()).pathname.startsWith('/api/proof'),
  )).toEqual([]);
});

test('logout and account switch clear private cache without implicit mutations', async ({ page }) => {
  const fixture = await interceptedApp(page, {
    user: { id: 'account-a', email: 'first@example.test', emailVerified: true },
  });
  await page.goto('/');
  await page.evaluate(() => {
    localStorage.setItem('sceneit:selected-result', 'private account A result');
    sessionStorage.setItem('pendingImportLink', 'https://vimeo.com/private-account-a');
  });

  await Promise.all([
    page.waitForEvent('framenavigated', frame => frame === page.mainFrame()),
    page.getByRole('button', { name: /Log out|Sign out/ }).click(),
  ]);
  await expect(page.getByRole('heading', { name: 'Find scenes in your videos.' })).toBeVisible();
  await expect.poll(async () => {
    try { return await page.evaluate(() => localStorage.getItem('sceneit:selected-result')); }
    catch { return 'navigation-in-progress'; }
  }).toBeNull();
  await expect.poll(async () => {
    try { return await page.evaluate(() => sessionStorage.getItem('pendingImportLink')); }
    catch { return 'navigation-in-progress'; }
  }).toBeNull();

  fixture.setSession({
    user: { id: 'account-b', email: 'second@example.test', emailVerified: true },
  });
  await page.goto('/');
  await expect(page.getByText('private account A result')).toHaveCount(0);
  expect(fixture.requests.filter(request =>
    request.method() !== 'GET'
    && /^\/api\/(imports|proof)/.test(new URL(request.url()).pathname),
  )).toEqual([]);
  expect(fixture.unhandled).toEqual([]);
});

test('explicit logout clears a draft originally marked as anonymous', async ({ page }) => {
  const fixture = await interceptedApp(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Paste a Link' }).click();
  await page.getByLabel('Video URL').fill('https://www.youtube.com/watch?v=anonymousDraft');
  await expect.poll(() => page.evaluate(() => ({
    link: sessionStorage.getItem('pendingImportLink'),
    marker: sessionStorage.getItem('pendingImportLink:anonymous'),
  }))).toEqual({
    link: 'https://www.youtube.com/watch?v=anonymousDraft',
    marker: '1',
  });

  await page.goto('/auth?mode=signin&returnTo=%2F');
  await page.getByLabel('Email').fill('verified@example.test');
  await page.getByLabel('Password').fill('correct horse battery staple');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page).toHaveURL('/');
  await expect(page.getByLabel('Video URL')).toHaveValue('https://www.youtube.com/watch?v=anonymousDraft');
  await expect.poll(() => page.evaluate(() =>
    sessionStorage.getItem('pendingImportLink:anonymous'))).toBeNull();
  await page.getByRole('button', { name: /Log out|Sign out/ }).click();
  await expect(page.getByRole('heading', { name: 'Find scenes in your videos.' })).toBeVisible();
  await expect.poll(() => page.evaluate(() => ({
    link: sessionStorage.getItem('pendingImportLink'),
    marker: sessionStorage.getItem('pendingImportLink:anonymous'),
  }))).toEqual({ link: null, marker: null });
  expect(fixture.requests.some(request =>
    request.method() !== 'GET'
    && /^\/api\/(imports|proof)/.test(new URL(request.url()).pathname),
  )).toBe(false);
  expect(fixture.unhandled).toEqual([]);
});

test('account refresh clears an owner draft, consent, and private client cache', async ({ page }) => {
  const fixture = await interceptedApp(page, {
    user: { id: 'account-a', email: 'first@example.test', emailVerified: true },
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Paste a Link' }).click();
  await page.getByLabel('Video URL').fill('https://vimeo.com/private-account-a-draft');
  await page.getByLabel('I confirm I have the right to process this video.').click();
  await page.evaluate(() => localStorage.setItem('sceneit:selected-result', 'private account A result'));
  await expect.poll(() => page.evaluate(() => ({
    link: sessionStorage.getItem('pendingImportLink'),
    marker: sessionStorage.getItem('pendingImportLink:anonymous'),
  }))).toEqual({
    link: 'https://vimeo.com/private-account-a-draft',
    marker: null,
  });

  fixture.setSession({
    user: { id: 'account-b', email: 'second@example.test', emailVerified: true },
  });
  await page.reload();
  await expect.poll(() => page.evaluate(() => ({
    link: sessionStorage.getItem('pendingImportLink'),
    marker: sessionStorage.getItem('pendingImportLink:anonymous'),
    selectedResult: localStorage.getItem('sceneit:selected-result'),
  }))).toEqual({ link: null, marker: null, selectedResult: null });
  await expect(page.getByLabel('I confirm I have the right to process this video.')).not.toBeChecked();
  await page.getByRole('button', { name: 'Paste a Link' }).click();
  await expect(page.getByLabel('Video URL')).toHaveValue('');
  await expect(page.getByText('private account A result')).toHaveCount(0);
  expect(fixture.requests.some(request =>
    request.method() !== 'GET'
    && /^\/api\/(imports|proof)/.test(new URL(request.url()).pathname),
  )).toBe(false);
  expect(fixture.unhandled).toEqual([]);
});

test('identity failure hides the owner draft and guest recovery cannot restore it', async ({ page }) => {
  let failSession = false;
  const fixture = await interceptedApp(page, {
    user: { id: 'account-a', email: 'first@example.test', emailVerified: true },
  });
  await page.route('**/api/auth/session', route =>
    failSession
      ? reply(route, { error: 'Synthetic identity failure', code: 'TEST_AUTH_FAILURE' }, 503)
      : route.fallback());
  await page.goto('/');
  await page.getByRole('button', { name: 'Paste a Link' }).click();
  await page.getByLabel('Video URL').fill('https://vimeo.com/private-owner-draft');
  await page.getByLabel('I confirm I have the right to process this video.').click();
  await page.evaluate(() => localStorage.setItem('sceneit:selected-result', 'owner-only cached result'));
  await expect.poll(() => page.evaluate(() => ({
    link: sessionStorage.getItem('pendingImportLink'),
    marker: sessionStorage.getItem('pendingImportLink:anonymous'),
  }))).toEqual({ link: 'https://vimeo.com/private-owner-draft', marker: null });

  failSession = true;
  await page.reload();
  await expect(page.getByText('Session check unavailable. Processing is closed until your account can be verified.')).toBeVisible();
  await expect(page.getByLabel('Video URL')).toBeHidden();
  await expect(page.getByLabel('Video URL')).toHaveValue('');

  fixture.setSession({});
  failSession = false;
  await page.getByRole('button', { name: 'Retry session check' }).click();
  await expect(page.getByRole('heading', { name: 'Find scenes in your videos.' })).toBeVisible();
  await expect.poll(() => page.evaluate(() => ({
    link: sessionStorage.getItem('pendingImportLink'),
    marker: sessionStorage.getItem('pendingImportLink:anonymous'),
    selectedResult: localStorage.getItem('sceneit:selected-result'),
  }))).toEqual({ link: null, marker: null, selectedResult: null });
  await expect(page.getByLabel('I confirm I have the right to process this video.')).not.toBeChecked();
  await page.getByRole('button', { name: 'Paste a Link' }).click();
  await expect(page.getByLabel('Video URL')).toHaveValue('');
  expect(fixture.requests.some(request =>
    request.method() !== 'GET'
    && /^\/api\/(imports|proof)/.test(new URL(request.url()).pathname),
  )).toBe(false);
  expect(fixture.unhandled).toEqual([]);
});

test('failed logout keeps private access closed across reload until an explicit retry', async ({ page }) => {
  const fixture = await interceptedApp(page, {
    user: { id: 'owner-signout', email: 'signout@example.test', emailVerified: true },
    importsUsed: 2,
  });
  let failLogout = true;
  await page.route('**/api/logout', async route => {
    expect(route.request().headers()['x-csrf-token']).toBe('application-csrf');
    if (failLogout) return reply(route, { error: 'Fixture unavailable' }, 503);
    fixture.setSession({});
    return reply(route, { success: true });
  });
  await page.goto('/');
  await expect(page.getByTestId('trial-allowance')).toBeVisible();
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'Log out' }).click();
  await expect(page.getByRole('button', { name: 'Retry sign out' })).toBeEnabled();
  await expect(page.getByTestId('trial-allowance')).toHaveCount(0);
  const beforeReload = fixture.requests.length;
  await page.reload();
  await expect(page.getByRole('button', { name: 'Retry sign out' })).toBeVisible();
  await expect(page.getByTestId('trial-allowance')).toHaveCount(0);
  expect(fixture.requests.slice(beforeReload).filter(request =>
    new URL(request.url()).pathname.startsWith('/api/imports'))).toHaveLength(0);
  failLogout = false;
  await page.getByRole('button', { name: 'Retry sign out' }).click();
  await expect(page.getByRole('link', { name: 'Email sign in', exact: true })).toBeVisible();
  expect(fixture.unhandled).toEqual([]);
});