import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { uploadFileXHR } from './upload-utils.ts';

const OriginalXHR = globalThis.XMLHttpRequest;
class FakeXHR {
  static instances = [];
  upload = {};
  status = 200;
  aborts = 0;
  sent = false;
  constructor() { FakeXHR.instances.push(this); }
  open() {}
  setRequestHeader() {}
  send() { this.sent = true; }
  abort() { this.aborts++; this.onabort?.(); }
}

function start(signal, progress = () => {}) {
  globalThis.XMLHttpRequest = FakeXHR;
  return uploadFileXHR('https://upload.example.test/session', 'PUT', {},
    new File(['test bytes'], 'test.mp4', { type: 'video/mp4' }), progress, signal);
}

afterEach(() => {
  globalThis.XMLHttpRequest = OriginalXHR;
  FakeXHR.instances = [];
});

test('an already-cancelled operation never starts a transfer', async () => {
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(start(controller.signal), { name: 'AbortError' });
  assert.equal(FakeXHR.instances.length, 0);
});

test('cancellation aborts the actual transfer and ignores late success/progress', async () => {
  const controller = new AbortController();
  const progress = [];
  const promise = start(controller.signal, value => progress.push(value));
  const xhr = FakeXHR.instances[0];
  const lateLoad = xhr.onload;
  const lateProgress = xhr.upload.onprogress;
  lateProgress({ lengthComputable: true, loaded: 20, total: 100 });
  assert.equal(xhr.sent, true);
  controller.abort();
  lateLoad();
  lateProgress({ lengthComputable: true, loaded: 100, total: 100 });
  await assert.rejects(promise, { name: 'AbortError' });
  assert.equal(xhr.aborts, 1);
  assert.deepEqual(progress, [20]);
  assert.equal(xhr.upload.onprogress, null);
});

test('successful transfers remove abort listeners and still complete normally', async () => {
  const controller = new AbortController();
  const promise = start(controller.signal);
  const xhr = FakeXHR.instances[0];
  xhr.onload();
  await promise;
  controller.abort();
  assert.equal(xhr.aborts, 0);
  assert.equal(xhr.onload, null);
});

test('HTTP errors reject without leaving a cancellation listener', async () => {
  const controller = new AbortController();
  const promise = start(controller.signal);
  const xhr = FakeXHR.instances[0];
  xhr.status = 403;
  xhr.onload();
  await assert.rejects(promise, /Upload failed with status 403/);
  controller.abort();
  assert.equal(xhr.aborts, 0);
});

test('timeouts and network failures remain retryable transfer errors', async () => {
  for (const event of ['ontimeout', 'onerror']) {
    const controller = new AbortController();
    const promise = start(controller.signal);
    const xhr = FakeXHR.instances.at(-1);
    xhr[event]();
    await assert.rejects(promise, /Upload timed out|Network error/);
    controller.abort();
    assert.equal(xhr.aborts, 0);
  }
});

test('an external browser abort is reported distinctly from upload failure', async () => {
  const promise = start();
  FakeXHR.instances[0].abort();
  await assert.rejects(promise, { name: 'AbortError' });
});