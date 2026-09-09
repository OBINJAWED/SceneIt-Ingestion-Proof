import { test, expect } from '@playwright/test';
import { mkdir, writeFile, unlink } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

test('development filesystem cannot bypass protected media admission', async ({ request }) => {
  const directory = fileURLToPath(new URL('../../../attached_assets/', import.meta.url));
  await mkdir(directory, { recursive: true });
  const file = path.join(directory, `.sceneit-boundary-${randomUUID()}.mp4`);
  const privateMarker = 'controlled fixture: never serve outside the protected API';
  await writeFile(file, privateMarker);
  try {
    for (const suffix of ['', '?raw']) {
      const response = await request.get(`/@fs${file}${suffix}`);
      expect(response.status()).toBe(403);
      expect(await response.text()).not.toContain(privateMarker);
    }
  } finally {
    await unlink(file);
  }
});