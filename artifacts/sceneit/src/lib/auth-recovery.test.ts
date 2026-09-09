import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { QueryClient } from '@tanstack/react-query';
// This workspace library exposes a CommonJS TypeScript entry to Node; Vite
// consumes its source as ESM. Exercise that entry without changing its packaging.
const {
  clearPendingImportLink,
  clearPrivateClientState,
  closeClientAuthSession,
  readPendingImportLink,
  safeReturnTo,
  savePendingImportLink,
}: typeof import('@workspace/replit-auth-web') = createRequire(import.meta.url)('@workspace/replit-auth-web');

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() { return values.size; },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => { values.delete(key); },
    setItem: (key, value) => { values.set(key, value); },
  };
}

test('recovery preserves only anonymous entry text, never private caches or owner drafts', () => {
  const descriptors = ['sessionStorage', 'localStorage', 'window'].map(key =>
    [key, Object.getOwnPropertyDescriptor(globalThis, key)] as const);
  const session = memoryStorage();
  const local = memoryStorage();
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: session });
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: local });
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { location: { origin: 'https://sceneit.example.test' } } });
  const queryClient = new QueryClient();
  try {
    queryClient.setQueryData(['/api/auth/session'], { user: null });
    queryClient.setQueryData(['/api/imports/private-owner'], { secret: 'owner result' });
    local.setItem('sceneit:private-search', 'private query');
    local.setItem('unrelated-setting', 'retained');
    savePendingImportLink('https://vimeo.com/123456', true);
    clearPrivateClientState(queryClient, { preserveAnonymousImportLink: true });
    assert.equal(readPendingImportLink(), 'https://vimeo.com/123456');
    assert.equal(queryClient.getQueryData(['/api/imports/private-owner']), undefined);
    assert.deepEqual(queryClient.getQueryData(['/api/auth/session']), { user: null });
    assert.equal(local.getItem('sceneit:private-search'), null);
    assert.equal(local.getItem('unrelated-setting'), 'retained');

    // Once restored under an authenticated owner, there is no exemption.
    savePendingImportLink('https://vimeo.com/123456', false);
    clearPrivateClientState(queryClient, { preserveAnonymousImportLink: true });
    assert.equal(readPendingImportLink(), '');
    assert.equal(session.length, 0);

    // Explicit logout clears even an unfinished anonymous onboarding draft.
    savePendingImportLink('https://vimeo.com/123456', true);
    clearPrivateClientState(queryClient);
    assert.equal(session.length, 0);

    // Legacy/unscoped drafts cannot bypass account-switch cleanup.
    session.setItem('pendingImportLink', 'https://vimeo.com/legacy-owner');
    clearPendingImportLink(true);
    assert.equal(session.length, 0);
    savePendingImportLink('https://vimeo.com/123456', true);
    savePendingImportLink('', true);
    assert.equal(session.length, 0);

    assert.equal(safeReturnTo('/imports/private-intent?view=results#scene'), '/imports/private-intent?view=results#scene');
    for (const unsafe of [
      'https://other.example.test/imports/1', '//other.example.test/imports/1',
      '/\\other.example.test', '/\t/other.example.test', 'javascript:alert(1)',
      '/auth/action?oobCode=fixture', '/imports/../auth/action?oobCode=fixture',
    ]) assert.equal(safeReturnTo(unsafe), '/', unsafe);
  } finally {
    queryClient.clear();
    for (const [key, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else Reflect.deleteProperty(globalThis, key);
    }
  }
});

test('confirmed recovery closes cached access before a fresh explicit sign-in', async () => {
  const queryClient = new QueryClient();
  queryClient.setQueryData(['/api/auth/session'], {
    user: { id: 'previous-owner' },
    csrfToken: 'synthetic-only',
    pilotAdmitted: true,
    privateAccess: { allowed: true, reason: 'ready' },
    usage: { importsRemaining: 3 },
    capabilities: { emailPassword: true },
  });
  await closeClientAuthSession(queryClient);
  assert.deepEqual(queryClient.getQueryData(['/api/auth/session']), {
    user: null,
    csrfToken: null,
    pilotAdmitted: false,
    privateAccess: { allowed: false, reason: 'authentication_required' },
    usage: null,
    capabilities: { emailPassword: true },
  });
  queryClient.clear();
});

test('blocked browser storage does not prevent recovery or require consent persistence', () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage');
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    get() { throw new Error('Storage blocked'); },
  });
  try {
    assert.equal(readPendingImportLink(), '');
    assert.doesNotThrow(() => savePendingImportLink('https://vimeo.com/123456', true));
    assert.doesNotThrow(() => clearPendingImportLink(true));
    assert.doesNotThrow(() => clearPendingImportLink());
  } finally {
    if (descriptor) Object.defineProperty(globalThis, 'sessionStorage', descriptor);
    else Reflect.deleteProperty(globalThis, 'sessionStorage');
  }
});