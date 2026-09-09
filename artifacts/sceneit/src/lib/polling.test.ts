import assert from 'node:assert/strict';
import test from 'node:test';
import {
  createStatusPoller, isPilotAllowed, protectedStateMessage, STATUS_POLL_BUDGET_MS,
} from './polling.ts';

test('pilot admission fails closed for session fixtures', () => {
  assert.equal(isPilotAllowed(undefined), false);
  assert.equal(isPilotAllowed({ user: null, pilotAdmitted: true }), false);
  assert.equal(isPilotAllowed({ user: { id: 'signed-in' }, pilotAdmitted: false }), false);
  assert.equal(isPilotAllowed({ user: { id: 'admitted' }, pilotAdmitted: true }), true);
});

test('status polling backs off and stops without retrying mutations', () => {
  const poller = createStatusPoller(0);
  let now = 0;
  const waits: number[] = [];
  while (true) {
    const wait = poller.next(false, now);
    if (wait === false) break;
    waits.push(wait);
    now += wait;
  }
  assert.deepEqual(waits, [1_000, 2_000, 4_000, 8_000, 15_000, 20_000, 25_000]);
  assert.equal(now, STATUS_POLL_BUDGET_MS);
  assert.equal(poller.next(false, now), false);
  poller.reset(100_000);
  assert.equal(poller.next(false, 100_000), 1_000);
  assert.equal(poller.next(true, 100_000), false);
});

test('all protected failure fixtures have actionable copy', () => {
  assert.deepEqual(Object.keys(protectedStateMessage).sort(), [
    'admission_required', 'not_found', 'processing', 'quota_exhausted',
    'service_unavailable', 'unauthorized', 'uncertain',
  ]);
});