import assert from 'node:assert/strict';
import test from 'node:test';
import { canReadPrivate } from './private-access';
import { importError } from './import-errors';

const emailOwner = {
  isAuthenticated: true, pilotAdmitted: false,
  user: { provider: 'firebase', emailVerified: true },
  privateAccess: { allowed: true, reason: 'ready' },
};

test('verified private owners keep retained reads when new public work is closed', () => {
  assert.equal(canReadPrivate(emailOwner), true);
  assert.equal(canReadPrivate({ ...emailOwner, privateAccess: { allowed: false, reason: 'public_trial_disabled' } }), true);
});

test('guests, unverified identities and uncertain cached sessions cannot render private data', () => {
  assert.equal(canReadPrivate({ ...emailOwner, isAuthenticated: false, user: null }), false);
  assert.equal(canReadPrivate({ ...emailOwner, user: { provider: 'firebase', emailVerified: false } }), false);
  assert.equal(canReadPrivate({ ...emailOwner, error: new Error('session unavailable') }), false);
  assert.equal(canReadPrivate({ ...emailOwner, privateAccess: { allowed: false, reason: 'identity_unavailable' } }), false);
});

test('legacy owners require exact pilot admission independently of Firebase', () => {
  assert.equal(canReadPrivate({ ...emailOwner, user: { provider: 'replit' } }), false);
  assert.equal(canReadPrivate({ ...emailOwner, user: { provider: 'replit' }, pilotAdmitted: true }), true);
});

test('account exhaustion, app capacity and verification have distinct recovery copy', () => {
  const message = (code: string) => importError({ data: { code, error: 'Generic allowance denial' } });
  assert.match(message('owner_import_limit'), /lifetime import attempts/);
  assert.match(message('owner_search_limit'), /Saved results/);
  assert.match(message('app_import_limit'), /shared import capacity/);
  assert.match(message('app_search_limit'), /shared search capacity/);
  assert.match(message('verification_required'), /Verify your email/);
});