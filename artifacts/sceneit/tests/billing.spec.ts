import { expect, test, type Page, type Request, type Route } from '@playwright/test';

const user = {
  id: 'billing-owner',
  email: 'owner@example.test',
  emailVerified: true,
  firstName: 'Pilot',
  provider: 'replit',
};

const metrics = {
  imports: { limit: 12, used: 3, remaining: 9 },
  upload_attempts: { limit: 12, used: 3, remaining: 9 },
  analysis_seconds: { limit: 3600, used: 420, remaining: 3180 },
  searches: { limit: 200, used: 21, remaining: 179 },
  media_bytes: { limit: 5_000_000_000, used: 900_000_000, remaining: 4_100_000_000 },
  frames: { limit: 500, used: 42, remaining: 458 },
};

const offer = {
  tier: 'reviewed_plus',
  name: 'Synthetic reviewed Plus — test only',
  rank: 20,
  cadence: 'monthly',
  currency: 'usd',
  capabilities: ['imports', 'searches'],
  limits: {
    imports: 12, upload_attempts: 12, analysis_seconds: 3600, searches: 200,
    media_bytes: 5_000_000_000, frames: 500, storage_bytes: 5_000_000_000,
  },
  unitAmount: 2400,
  taxBehavior: 'exclusive',
};

const annualOffer = {
  ...offer,
  tier: 'reviewed_annual',
  name: 'Synthetic reviewed Annual — test only',
  rank: 30,
  cadence: 'yearly',
  unitAmount: 24_000,
};

function session(pilotAdmitted = true) {
  return {
    csrfToken: 'billing-csrf',
    pilotAdmitted,
    user,
    capabilities: {
      replit: true,
      emailPassword: false,
      publicTrialEnabled: false,
      unavailableReason: 'public_trial_disabled',
      firebaseConfig: null,
    },
    privateAccess: { allowed: pilotAdmitted, reason: pilotAdmitted ? 'ready' : 'pilot_not_admitted' },
    usage: null,
  };
}

type BillingFixtureOptions = {
  pilotAdmitted?: boolean;
  paymentProblem?: 'expired_card' | 'payment_authentication_required' | null;
  pendingChange?: {
    changeId: string;
    tier: string;
    cadence: 'monthly' | 'yearly';
    currency: string;
    effectiveAt: string | null;
    state: 'confirming' | 'payment_pending' | 'scheduled' | 'uncertain';
  } | null;
  enabled?: boolean;
  membership?: 'active' | 'inactive';
  effectiveTier?: string | null;
  cadence?: 'monthly' | 'yearly' | null;
  currency?: string | null;
  offers?: Array<typeof offer>;
  mutationFailure?: { code: string; state: string; error: string };
  mutationFailureAt?: 'preview' | 'confirm' | 'checkout' | 'withdraw';
  withdrawResultState?: string;
  portalExpiresAt?: string;
  previewExpiresAt?: string;
  checkoutExpiresAt?: string;
  previewKind?: 'upgrade' | 'scheduled';
  hostedReturnFixture?: boolean;
  operationStates?: Array<{
    idempotencyKey: string;
    kind: 'checkout' | 'portal' | 'upgrade' | 'schedule' | 'withdraw';
    state: 'creating' | 'created' | 'confirming' | 'confirmed' | 'scheduled' | 'payment_pending' | 'completed' | 'uncertain' | 'withdrawn' | 'expired' | 'failed';
  }>;
};

function status(options: Omit<BillingFixtureOptions, 'pilotAdmitted' | 'mutationFailure'> = {}) {
  if (options.enabled === false) {
    return {
      enabled: false, environment: null, membership: 'disabled',
      effectiveTier: null, cadence: null, currency: null, paidThrough: null,
      cancelAtPeriodEnd: false, usage: null, pendingChange: null,
      paymentProblem: null, managementEligible: false, offers: [],
    };
  }
  const paymentProblem = options.paymentProblem ?? null;
  return {
    enabled: options.enabled ?? true,
    environment: 'test',
    membership: options.membership ?? 'active',
    effectiveTier: options.effectiveTier === undefined ? 'reviewed_core' : options.effectiveTier,
    cadence: options.cadence === undefined ? 'monthly' : options.cadence,
    currency: options.currency === undefined ? 'usd' : options.currency,
    paidThrough: '2030-02-01T00:00:00Z',
    cancelAtPeriodEnd: false,
    usage: {
      windowStart: '2030-01-01T00:00:00Z',
      windowEnd: '2030-02-01T00:00:00Z',
      metrics,
      storage: { limit: 5_000_000_000, used: 900_000_000, remaining: 4_100_000_000 },
      workStopped: false,
    },
    pendingChange: options.pendingChange ?? null,
    paymentProblem: paymentProblem ? {
      code: paymentProblem,
      state: 'open',
      invoiceId: 'in_safe_fixture',
      tier: 'reviewed_core',
      cadence: 'monthly',
      currency: 'usd',
      amountDue: 1900,
      nextAction: paymentProblem === 'payment_authentication_required' ? 'authenticate_payment' : 'manage_billing',
      occurredAt: '2030-01-15T00:00:00Z',
      resolvedAt: null,
    } : null,
    managementEligible: true,
    offers: options.offers ?? [offer, annualOffer],
    ...(options.operationStates ? { operationStates: options.operationStates } : {}),
  };
}

async function reply(route: Route, body: unknown, statusCode = 200) {
  await route.fulfill({ status: statusCode, contentType: 'application/json', body: JSON.stringify(body) });
}

async function billingApp(page: Page, options: BillingFixtureOptions = {}) {
  const mutations: Request[] = [];
  const external: Request[] = [];
  await page.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
      return route.abort('blockedbyclient');
    }
    if (url.hostname !== '127.0.0.1') {
      external.push(request);
      if (options.hostedReturnFixture && url.hostname === 'billing-provider.invalid') {
        return route.fulfill({
          contentType: 'text/html',
          body: '<!doctype html><title>Intercepted hosted billing</title><a href="http://127.0.0.1:4177/billing">Return to SceneIt</a>',
        });
      }
      return route.abort('blockedbyclient');
    }
    if (!url.pathname.startsWith('/api/')) return route.continue();
    if (request.method() !== 'GET') mutations.push(request);
    if (url.pathname === '/api/auth/session' || url.pathname === '/api/auth/user') return reply(route, session(options.pilotAdmitted ?? true));
    if (url.pathname === '/api/imports/config') return reply(route, {
      maxBytes: 1, minDurationSeconds: 1, maxDurationSeconds: 2, retentionDays: 1,
      ownerImportLimit: 1, appImportLimit: 1, ownerSearchLimit: 1, appSearchLimit: 1,
      quotaMode: 'monthly', workerAvailable: true,
    });
    if (url.pathname === '/api/billing/status') return reply(route, status(options));
    if (url.pathname === '/api/billing/checkout' && options.mutationFailure && options.mutationFailureAt === 'checkout') {
      return reply(route, options.mutationFailure, 503);
    }
    if (url.pathname === '/api/billing/checkout') return reply(route, {
      operationId: '99999999-9999-4999-8999-999999999999',
      url: 'https://billing-provider.invalid/checkout',
      expiresAt: options.checkoutExpiresAt ?? '2099-01-01T00:00:00Z',
    });
    if (url.pathname === '/api/billing/change/preview' && options.mutationFailure && options.mutationFailureAt === 'preview') {
      return reply(route, options.mutationFailure, 503);
    }
    if (url.pathname === '/api/billing/change/preview') return reply(route, {
      previewId: '11111111-1111-4111-8111-111111111111',
      kind: options.previewKind ?? 'upgrade',
      effectiveAt: '2030-01-16T00:00:00Z',
      expiresAt: options.previewExpiresAt ?? '2030-01-16T00:10:00Z',
      currency: 'usd',
      subtotal: 430,
      tax: 35,
      total: 465,
    });
    if (url.pathname === '/api/billing/change/confirm' && options.mutationFailure && options.mutationFailureAt === 'confirm') {
      return reply(route, options.mutationFailure, 503);
    }
    if (url.pathname === '/api/billing/change/confirm') return reply(route, {
      changeId: '22222222-2222-4222-8222-222222222222',
      state: 'payment_pending',
      effectiveAt: '2030-01-16T00:00:00Z',
      hostedAction: null,
    });
    if (url.pathname === '/api/billing/portal') return reply(route, {
      operationId: '33333333-3333-4333-8333-333333333333',
      url: 'https://billing-provider.invalid/session',
      expiresAt: options.portalExpiresAt ?? '2099-01-01T00:00:00Z',
    });
    if (url.pathname === '/api/billing/change/withdraw' && options.mutationFailure && options.mutationFailureAt === 'withdraw') {
      return reply(route, options.mutationFailure, 503);
    }
    if (url.pathname === '/api/billing/change/withdraw') return reply(route, {
      changeId: '55555555-5555-4555-8555-555555555555',
      state: options.withdrawResultState ?? 'withdrawn',
    });
    return reply(route, { error: 'Unexpected API request' }, 500);
  });
  await page.goto('/billing');
  return { mutations, external };
}

test('desktop previews exact authoritative upgrade money before confirmation', async ({ page }) => {
  const { mutations, external } = await billingApp(page);
  await expect(page.getByRole('heading', { name: 'Membership & billing' })).toBeVisible();
  await page.getByTestId('button-select-reviewed_plus-monthly-usd').click();
  await expect(page.getByTestId('text-preview-total')).toContainText('$4.65');
  await page.getByTestId('button-confirm-change').click();

  const previewRequest = mutations.find(request => new URL(request.url()).pathname.endsWith('/change/preview'));
  expect(previewRequest?.headers()['x-csrf-token']).toBe('billing-csrf');
  expect(previewRequest?.postDataJSON()).toEqual({
    tier: 'reviewed_plus',
    cadence: 'monthly',
    currency: 'usd',
    idempotencyKey: expect.stringMatching(/^[0-9a-f-]{36}$/),
  });
  const confirmRequest = mutations.find(request => new URL(request.url()).pathname.endsWith('/change/confirm'));
  expect(confirmRequest?.postDataJSON()).toEqual({
    previewId: '11111111-1111-4111-8111-111111111111',
    idempotencyKey: expect.stringMatching(/^[0-9a-f-]{36}$/),
  });
  await expect(page.getByTestId('text-membership-tier')).toHaveText('reviewed_core');
  expect(external).toHaveLength(0);
});

test('mobile expired-card journey uses direct hosted management and cancellation', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { mutations, external } = await billingApp(page, {
    pilotAdmitted: false,
    paymentProblem: 'expired_card',
  });
  await expect(page.getByText('Your card has expired')).toBeVisible();
  await expect(page.getByText(/Updating a card .* does not itself confirm payment/)).toBeVisible();
  await page.getByTestId('button-cancel-membership').click();

  await expect.poll(() => mutations.filter(request => new URL(request.url()).pathname === '/api/billing/portal').length).toBe(1);
  const portalRequest = mutations.find(request => new URL(request.url()).pathname === '/api/billing/portal');
  expect(portalRequest?.headers()['x-csrf-token']).toBe('billing-csrf');
  expect(portalRequest?.postDataJSON()).toEqual({
    action: 'cancel',
    idempotencyKey: expect.stringMatching(/^[0-9a-f-]{36}$/),
  });
  await expect.poll(() => external.length).toBe(1);
  expect(new URL(external[0].url()).hostname).toBe('billing-provider.invalid');
});

test('scheduled annual change shows renewal date and is the only withdrawable state', async ({ page }) => {
  const pendingChange = {
    changeId: '55555555-5555-4555-8555-555555555555',
    tier: 'reviewed_annual',
    cadence: 'yearly' as const,
    currency: 'usd',
    effectiveAt: '2030-02-01T00:00:00Z',
    state: 'scheduled' as const,
  };
  const { mutations } = await billingApp(page, { pendingChange });
  await expect(page.getByTestId('text-pending-change-state')).toContainText('next renewal');
  await expect(page.getByTestId('text-pending-change-state')).toContainText('current tier, currency, and usage remain unchanged');
  await page.getByTestId('button-withdraw-change').click();
  const request = mutations.find(item => new URL(item.url()).pathname.endsWith('/change/withdraw'));
  expect(request?.postDataJSON()).toEqual({
    changeId: pendingChange.changeId,
    idempotencyKey: expect.stringMatching(/^[0-9a-f-]{36}$/),
  });
  await expect(page.getByText('Scheduled change withdrawn', { exact: true })).toBeVisible();
});

test('malformed withdrawal success does not claim the schedule was withdrawn', async ({ page }) => {
  const pendingChange = {
    changeId: '56565656-5656-4656-8656-565656565656',
    tier: 'reviewed_annual',
    cadence: 'yearly' as const,
    currency: 'usd',
    effectiveAt: '2030-02-01T00:00:00Z',
    state: 'scheduled' as const,
  };
  await billingApp(page, { pendingChange, withdrawResultState: 'scheduled' });
  await page.getByTestId('button-withdraw-change').click();
  await expect(page.getByText('Scheduled change withdrawn', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Withdrawal is still being checked', { exact: true })).toBeVisible();
  await expect(page.getByTestId('status-operation-uncertain')).toBeVisible();
});

for (const failure of [
  {
    name: 'unknown',
    response: {
      code: 'billing_outcome_unknown',
      state: 'uncertain',
      error: 'The withdrawal outcome is being reconciled.',
    },
    operationState: 'uncertain' as const,
  },
  {
    name: 'concurrent',
    response: {
      code: 'withdrawal_in_progress',
      state: 'creating',
      error: 'The withdrawal is already in progress.',
    },
    operationState: 'creating' as const,
  },
]) {
  test(`${failure.name} withdrawal never claims success and preserves its key across reload`, async ({ page }) => {
    const pendingChange = {
      changeId: '57575757-5757-4757-8757-575757575757',
      tier: 'reviewed_annual',
      cadence: 'yearly' as const,
      currency: 'usd',
      effectiveAt: '2030-02-01T00:00:00Z',
      state: 'scheduled' as const,
    };
    const options: BillingFixtureOptions = {
      pendingChange,
      mutationFailureAt: 'withdraw',
      mutationFailure: failure.response,
    };
    const { mutations } = await billingApp(page, options);
    await page.getByTestId('button-withdraw-change').click();
    await expect(page.getByText('Scheduled change withdrawn', { exact: true })).toHaveCount(0);
    await expect(page.getByTestId('status-operation-uncertain')).toBeVisible();

    const request = mutations.find(item => new URL(item.url()).pathname.endsWith('/change/withdraw'));
    const idempotencyKey = (request?.postDataJSON() as { idempotencyKey: string }).idempotencyKey;
    options.operationStates = [{ idempotencyKey, kind: 'withdraw', state: failure.operationState }];
    await page.reload();

    await expect(page.getByTestId('status-operation-uncertain')).toBeVisible();
    await expect(page.getByTestId('button-withdraw-change')).toBeDisabled();
    expect(await page.evaluate(key => {
      for (let index = 0; index < sessionStorage.length; index += 1) {
        const storageKey = sessionStorage.key(index);
        if (storageKey && sessionStorage.getItem(storageKey) === key) return true;
      }
      return false;
    }, idempotencyKey)).toBe(true);
    expect(mutations.filter(item => new URL(item.url()).pathname.endsWith('/change/withdraw'))).toHaveLength(1);
  });
}

for (const unresolvedState of ['scheduled', 'created'] as const) {
  test(`${unresolvedState} withdrawal status stays blocked until verified completion`, async ({ page }) => {
    const options: BillingFixtureOptions = {
      pendingChange: {
        changeId: '56565656-5656-4656-8656-565656565656',
        tier: 'reviewed_annual',
        cadence: 'yearly',
        currency: 'usd',
        effectiveAt: '2030-02-01T00:00:00Z',
        state: 'scheduled',
      },
      mutationFailureAt: 'withdraw',
      mutationFailure: {
        code: 'withdrawal_outcome_unknown',
        state: 'uncertain',
        error: 'The withdrawal has not been verified.',
      },
    };
    const { mutations, external } = await billingApp(page, options);
    await page.getByTestId('button-withdraw-change').click();
    await expect(page.getByTestId('status-operation-uncertain')).toBeVisible();
    const request = mutations.find(item => new URL(item.url()).pathname.endsWith('/change/withdraw'));
    const idempotencyKey = (request?.postDataJSON() as { idempotencyKey: string }).idempotencyKey;
    const keyIsRetained = () => page.evaluate(key =>
      Object.values(sessionStorage).includes(key), idempotencyKey);

    for (const state of [unresolvedState, 'uncertain'] as const) {
      options.operationStates = [{ idempotencyKey, kind: 'withdraw', state }];
      await page.reload();
      await expect(page.getByTestId('status-operation-uncertain')).toBeVisible();
      await expect(page.getByTestId('button-withdraw-change')).toBeDisabled();
      await expect.poll(keyIsRetained).toBe(true);
      await expect(page.getByText('Scheduled change withdrawn', { exact: true })).toHaveCount(0);
    }

    options.operationStates = [{ idempotencyKey, kind: 'withdraw', state: 'completed' }];
    options.pendingChange = null;
    await page.reload();
    await expect(page.getByTestId('status-operation-uncertain')).toHaveCount(0);
    await expect(page.getByTestId('button-withdraw-change')).toHaveCount(0);
    await expect.poll(keyIsRetained).toBe(false);
    expect(mutations.filter(item => new URL(item.url()).pathname.endsWith('/change/withdraw'))).toHaveLength(1);
    expect(external).toHaveLength(0);
  });
}

test('authoritative failed withdrawal retires its key before a fresh retry', async ({ page }) => {
  const pendingChange = {
    changeId: '58585858-5858-4858-8858-585858585858',
    tier: 'reviewed_annual',
    cadence: 'yearly' as const,
    currency: 'usd',
    effectiveAt: '2030-02-01T00:00:00Z',
    state: 'scheduled' as const,
  };
  const options: BillingFixtureOptions = {
    pendingChange,
    mutationFailureAt: 'withdraw',
    mutationFailure: {
      code: 'provider_rejected',
      state: 'failed',
      error: 'The provider rejected the withdrawal.',
    },
  };
  const { mutations } = await billingApp(page, options);
  await page.getByTestId('button-withdraw-change').click();
  await expect(page.getByText('Scheduled change withdrawn', { exact: true })).toHaveCount(0);
  const first = mutations.find(item => new URL(item.url()).pathname.endsWith('/change/withdraw'));
  const firstKey = (first?.postDataJSON() as { idempotencyKey: string }).idempotencyKey;

  options.operationStates = [{ idempotencyKey: firstKey, kind: 'withdraw', state: 'failed' }];
  options.mutationFailureAt = undefined;
  await page.reload();
  await expect.poll(() => page.evaluate(key => {
    for (let index = 0; index < sessionStorage.length; index += 1) {
      const storageKey = sessionStorage.key(index);
      if (storageKey && sessionStorage.getItem(storageKey) === key) return true;
    }
    return false;
  }, firstKey)).toBe(false);
  await page.getByTestId('button-withdraw-change').click();
  await expect(page.getByText('Scheduled change withdrawn', { exact: true })).toBeVisible();

  const withdrawals = mutations.filter(item => new URL(item.url()).pathname.endsWith('/change/withdraw'));
  expect(withdrawals).toHaveLength(2);
  expect((withdrawals[1].postDataJSON() as { idempotencyKey: string }).idempotencyKey).not.toBe(firstKey);
});

test('payment-pending state never offers withdrawal or implies access', async ({ page }) => {
  await billingApp(page, {
    pendingChange: {
      changeId: '66666666-6666-4666-8666-666666666666',
      tier: 'reviewed_plus',
      cadence: 'monthly',
      currency: 'usd',
      effectiveAt: null,
      state: 'payment_pending',
    },
  });
  await expect(page.getByTestId('text-pending-change-state')).toContainText('still being verified');
  await expect(page.getByTestId('text-pending-change-state')).toContainText('current paid tier and limits remain unchanged');
  await expect(page.getByTestId('button-withdraw-change')).toHaveCount(0);
  await expect(page.getByTestId('text-membership-tier')).toHaveText('reviewed_core');
});

test('confirming change is processing without a second submission action', async ({ page }) => {
  await billingApp(page, {
    pendingChange: {
      changeId: '67676767-6767-4767-8767-676767676767',
      tier: 'reviewed_plus',
      cadence: 'monthly',
      currency: 'usd',
      effectiveAt: null,
      state: 'confirming',
    },
  });
  await expect(page.getByTestId('text-pending-change-state')).toContainText('still processing');
  await expect(page.getByTestId('text-pending-change-state')).toContainText('do not submit another change');
  await expect(page.getByTestId('button-withdraw-change')).toHaveCount(0);
});

test('uncertain pending change names reconciliation and preserves current tier', async ({ page }) => {
  await billingApp(page, {
    pendingChange: {
      changeId: '68686868-6868-4868-8868-686868686868',
      tier: 'reviewed_plus',
      cadence: 'monthly',
      currency: 'usd',
      effectiveAt: null,
      state: 'uncertain',
    },
  });
  await expect(page.getByTestId('text-pending-change-state')).toContainText('outcome is unknown');
  await expect(page.getByTestId('text-pending-change-state')).toContainText('current paid tier and limits remain unchanged');
  await expect(page.getByTestId('button-withdraw-change')).toHaveCount(0);
  await expect(page.getByTestId('text-membership-tier')).toHaveText('reviewed_core');
});

test('disabled billing exposes no fixture offers', async ({ page }) => {
  await billingApp(page, { enabled: false, offers: [], membership: 'inactive', effectiveTier: null, cadence: null, currency: null });
  await expect(page.getByTestId('status-billing-disabled')).toBeVisible();
  await expect(page.locator('[data-testid^="card-offer-"]')).toHaveCount(0);
  await expect(page.getByText(/test only/)).toHaveCount(0);
});

test('unknown confirmation stays blocked until matching operation authoritatively settles', async ({ page }) => {
  const options: BillingFixtureOptions = {
    mutationFailureAt: 'confirm',
    mutationFailure: {
      code: 'billing_outcome_unknown',
      state: 'uncertain',
      error: 'The billing outcome is being reconciled.',
    },
  };
  const { mutations } = await billingApp(page, options);
  await page.getByTestId('button-select-reviewed_plus-monthly-usd').click();
  await page.getByTestId('button-confirm-change').click();
  await expect(page.getByTestId('status-operation-uncertain')).toBeVisible();
  const confirmation = mutations.find(item => new URL(item.url()).pathname.endsWith('/change/confirm'));
  const idempotencyKey = (confirmation?.postDataJSON() as { idempotencyKey: string }).idempotencyKey;
  const storedConfirmationKey = await page.evaluate(key => {
    for (let index = 0; index < sessionStorage.length; index += 1) {
      const storageKey = sessionStorage.key(index);
      if (storageKey && sessionStorage.getItem(storageKey) === key) return storageKey;
    }
    return null;
  }, idempotencyKey);
  expect(storedConfirmationKey).toBeTruthy();
  options.operationStates = [{ idempotencyKey, kind: 'upgrade', state: 'uncertain' }];

  await page.reload();
  await expect(page.getByTestId('status-operation-uncertain')).toBeVisible();
  await expect(page.getByTestId('button-select-reviewed_annual-yearly-usd')).toBeDisabled();
  expect(mutations.filter(item => new URL(item.url()).pathname.endsWith('/change/confirm'))).toHaveLength(1);
  expect(await page.evaluate(key => sessionStorage.getItem(key!), storedConfirmationKey)).toBe(idempotencyKey);

  options.operationStates = [{ idempotencyKey, kind: 'upgrade', state: 'failed' }];
  await page.reload();
  await expect(page.getByTestId('status-operation-uncertain')).toHaveCount(0);
  await expect(page.getByTestId('button-select-reviewed_annual-yearly-usd')).toBeEnabled();
  await expect(page.getByTestId('text-membership-tier')).toHaveText('reviewed_core');
});

for (const device of [
  { name: 'desktop', viewport: { width: 1280, height: 800 } },
  { name: 'mobile', viewport: { width: 390, height: 844 } },
]) {
  test(`${device.name} matching scheduled confirmation recovery enables withdrawal`, async ({ page }) => {
    await page.setViewportSize(device.viewport);
    const options: BillingFixtureOptions = {
      previewKind: 'scheduled',
      mutationFailureAt: 'confirm',
      mutationFailure: {
        code: 'billing_outcome_unknown',
        state: 'uncertain',
        error: 'The scheduled-change confirmation timed out.',
      },
    };
    const { mutations } = await billingApp(page, options);
    await page.getByTestId('button-select-reviewed_annual-yearly-usd').click();
    await page.getByTestId('button-confirm-change').click();
    await expect(page.getByTestId('status-operation-uncertain')).toBeVisible();

    const confirmation = mutations.find(item => new URL(item.url()).pathname.endsWith('/change/confirm'));
    const idempotencyKey = (confirmation?.postDataJSON() as { idempotencyKey: string }).idempotencyKey;
    options.operationStates = [{ idempotencyKey, kind: 'schedule', state: 'scheduled' }];
    options.pendingChange = {
      changeId: '59595959-5959-4959-8959-595959595959',
      tier: 'reviewed_annual',
      cadence: 'yearly',
      currency: 'usd',
      effectiveAt: '2030-02-01T00:00:00Z',
      state: 'scheduled',
    };
    options.mutationFailureAt = undefined;
    await page.reload();

    await expect(page.getByTestId('status-operation-uncertain')).toHaveCount(0);
    await expect(page.getByTestId('button-withdraw-change')).toBeEnabled();
    await expect(page.getByTestId('button-select-reviewed_plus-monthly-usd')).toBeDisabled();
    await page.getByTestId('button-withdraw-change').click();
    await expect(page.getByText('Scheduled change withdrawn', { exact: true })).toBeVisible();
  });
}

for (const recovery of ['absent', 'mismatched'] as const) {
  test(`${recovery} scheduled operation row does not clear an unknown confirmation`, async ({ page }) => {
    const options: BillingFixtureOptions = {
      previewKind: 'scheduled',
      mutationFailureAt: 'confirm',
      mutationFailure: {
        code: 'billing_outcome_unknown',
        state: 'uncertain',
        error: 'The scheduled-change confirmation timed out.',
      },
    };
    const { mutations } = await billingApp(page, options);
    await page.getByTestId('button-select-reviewed_annual-yearly-usd').click();
    await page.getByTestId('button-confirm-change').click();
    await expect(page.getByTestId('status-operation-uncertain')).toBeVisible();
    const confirmation = mutations.find(item => new URL(item.url()).pathname.endsWith('/change/confirm'));
    const idempotencyKey = (confirmation?.postDataJSON() as { idempotencyKey: string }).idempotencyKey;
    options.operationStates = recovery === 'absent' ? [] : [{
      idempotencyKey: '60606060-6060-4060-8060-606060606060',
      kind: 'schedule',
      state: 'scheduled',
    }];
    options.pendingChange = {
      changeId: '61616161-6161-4161-8161-616161616161',
      tier: 'reviewed_annual',
      cadence: 'yearly',
      currency: 'usd',
      effectiveAt: '2030-02-01T00:00:00Z',
      state: 'scheduled',
    };
    await page.reload();

    await expect(page.getByTestId('status-operation-uncertain')).toBeVisible();
    await expect(page.getByTestId('button-withdraw-change')).toBeDisabled();
    expect(await page.evaluate(key => {
      for (let index = 0; index < sessionStorage.length; index += 1) {
        const storageKey = sessionStorage.key(index);
        if (storageKey && sessionStorage.getItem(storageKey) === key) return true;
      }
      return false;
    }, idempotencyKey)).toBe(true);
  });
}

test('preview-only provider outage remains retryable with the same stable key', async ({ page }) => {
  const options: BillingFixtureOptions = {
    mutationFailureAt: 'preview',
    mutationFailure: {
      code: 'billing_provider_unavailable',
      state: 'service_unavailable',
      error: 'The billing provider is temporarily unavailable.',
    },
  };
  const { mutations } = await billingApp(page, options);
  await page.getByTestId('button-select-reviewed_plus-monthly-usd').click();
  await expect(page.getByTestId('status-operation-uncertain')).toHaveCount(0);

  options.mutationFailureAt = undefined;
  await page.getByTestId('button-select-reviewed_plus-monthly-usd').click();
  await expect(page.getByTestId('dialog-change-preview')).toBeVisible();
  const previews = mutations.filter(item => new URL(item.url()).pathname.endsWith('/change/preview'));
  expect(previews).toHaveLength(2);
  expect((previews[0].postDataJSON() as { idempotencyKey: string }).idempotencyKey)
    .toBe((previews[1].postDataJSON() as { idempotencyKey: string }).idempotencyKey);
});

test('expired hosted portal session gets a fresh key on an explicit same-tab attempt', async ({ page }) => {
  const now = Date.now();
  const options: BillingFixtureOptions = {
    hostedReturnFixture: true,
    portalExpiresAt: new Date(now + 60_000).toISOString(),
  };
  const { mutations } = await billingApp(page, options);
  await page.getByTestId('button-manage-billing').click();
  await expect.poll(() => mutations.filter(item => new URL(item.url()).pathname === '/api/billing/portal').length).toBe(1);
  await page.getByRole('link', { name: 'Return to SceneIt' }).click();
  await expect(page.getByRole('heading', { name: 'Membership & billing' })).toBeVisible();

  await page.evaluate(later => { Date.now = () => later; }, now + 120_000);
  options.portalExpiresAt = new Date(now + 180_000).toISOString();
  await page.getByTestId('button-manage-billing').click();
  await expect.poll(() => mutations.filter(item => new URL(item.url()).pathname === '/api/billing/portal').length).toBe(2);

  const portals = mutations.filter(item => new URL(item.url()).pathname === '/api/billing/portal');
  expect((portals[0].postDataJSON() as { idempotencyKey: string }).idempotencyKey)
    .not.toBe((portals[1].postDataJSON() as { idempotencyKey: string }).idempotencyKey);
});

test('expired hosted checkout gets a fresh key on an explicit same-tab attempt', async ({ page }) => {
  const now = Date.now();
  const options: BillingFixtureOptions = {
    hostedReturnFixture: true,
    membership: 'inactive',
    effectiveTier: null,
    cadence: null,
    checkoutExpiresAt: new Date(now + 60_000).toISOString(),
  };
  const { mutations } = await billingApp(page, options);
  await page.getByTestId('button-select-reviewed_plus-monthly-usd').click();
  await expect.poll(() => mutations.filter(item => new URL(item.url()).pathname === '/api/billing/checkout').length).toBe(1);
  await page.getByRole('link', { name: 'Return to SceneIt' }).click();
  await expect(page.getByRole('heading', { name: 'Membership & billing' })).toBeVisible();

  await page.evaluate(later => { Date.now = () => later; }, now + 120_000);
  options.checkoutExpiresAt = new Date(now + 180_000).toISOString();
  await page.getByTestId('button-select-reviewed_plus-monthly-usd').click();
  await expect.poll(() => mutations.filter(item => new URL(item.url()).pathname === '/api/billing/checkout').length).toBe(2);

  const checkouts = mutations.filter(item => new URL(item.url()).pathname === '/api/billing/checkout');
  expect((checkouts[0].postDataJSON() as { idempotencyKey: string }).idempotencyKey)
    .not.toBe((checkouts[1].postDataJSON() as { idempotencyKey: string }).idempotencyKey);
});

test('expired abandoned preview gets a fresh key on an explicit same-tab attempt', async ({ page }) => {
  const now = Date.now();
  const options: BillingFixtureOptions = {
    previewExpiresAt: new Date(now + 60_000).toISOString(),
  };
  const { mutations } = await billingApp(page, options);
  await page.getByTestId('button-select-reviewed_plus-monthly-usd').click();
  await expect(page.getByTestId('dialog-change-preview')).toBeVisible();
  await page.keyboard.press('Escape');

  await page.evaluate(later => { Date.now = () => later; }, now + 120_000);
  options.previewExpiresAt = new Date(now + 180_000).toISOString();
  await page.getByTestId('button-select-reviewed_plus-monthly-usd').click();
  await expect(page.getByTestId('dialog-change-preview')).toBeVisible();

  const previews = mutations.filter(item => new URL(item.url()).pathname.endsWith('/change/preview'));
  expect(previews).toHaveLength(2);
  expect((previews[0].postDataJSON() as { idempotencyKey: string }).idempotencyKey)
    .not.toBe((previews[1].postDataJSON() as { idempotencyKey: string }).idempotencyKey);
});

test('confirmed preview retires its key before the next same-tab preview', async ({ page }) => {
  const { mutations } = await billingApp(page);
  await page.getByTestId('button-select-reviewed_plus-monthly-usd').click();
  await page.getByTestId('button-confirm-change').click();
  await page.getByTestId('button-select-reviewed_plus-monthly-usd').click();

  const previews = mutations.filter(item => new URL(item.url()).pathname.endsWith('/change/preview'));
  expect(previews).toHaveLength(2);
  expect((previews[0].postDataJSON() as { idempotencyKey: string }).idempotencyKey)
    .not.toBe((previews[1].postDataJSON() as { idempotencyKey: string }).idempotencyKey);
});

test('authentication-required management remains available without pilot purchase offers', async ({ page }) => {
  await billingApp(page, {
    pilotAdmitted: false,
    paymentProblem: 'payment_authentication_required',
  });
  await expect(page.getByText('Payment authentication required')).toBeVisible();
  await expect(page.getByTestId('button-recover-payment')).toBeVisible();
  await expect(page.locator('[data-testid^="card-offer-"]')).toHaveCount(0);
  await expect(page.getByTestId('text-membership-tier')).toHaveText('reviewed_core');
});

test('formats zero and three-decimal currencies from integer minor units', async ({ page }) => {
  const jpyOffer = { ...offer, tier: 'fixture_jpy', name: 'Synthetic reviewed JPY — test only', currency: 'jpy', unitAmount: 2400 };
  const kwdOffer = { ...annualOffer, tier: 'fixture_kwd', name: 'Synthetic reviewed KWD annual — test only', currency: 'kwd', unitAmount: 1234 };
  await billingApp(page, {
    membership: 'inactive',
    effectiveTier: null,
    cadence: null,
    currency: null,
    offers: [jpyOffer, kwdOffer],
    pendingChange: {
      changeId: '77777777-7777-4777-8777-777777777777',
      tier: 'fixture_kwd',
      cadence: 'yearly',
      currency: 'kwd',
      effectiveAt: '2031-04-05T12:00:00Z',
      state: 'scheduled',
    },
  });
  await expect(page.getByTestId('text-price-fixture_jpy-monthly')).toContainText('2,400');
  await expect(page.getByTestId('text-price-fixture_kwd-yearly')).toContainText('1.234');
  await expect(page.getByTestId('text-pending-change-state')).toContainText('next renewal');
  await expect(page.getByTestId('card-pending-change')).toContainText('KWD');
  await expect(page.getByTestId('text-pending-change-state')).toContainText('2031');
});
