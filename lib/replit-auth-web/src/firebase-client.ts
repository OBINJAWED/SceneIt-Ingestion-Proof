import { getApp, getApps, initializeApp, type FirebaseOptions } from 'firebase/app';
import {
  getAuth,
  initializeAuth,
  inMemoryPersistence,
  type Auth,
} from 'firebase/auth';

const FIREBASE_APP_NAME = 'sceneit-email-auth';
let clientAuth: Auth | null = null;

/** Cleanup must include provider-only onboarding sessions and need no config. */
export function existingFirebaseClientAuth(): Auth | null {
  return clientAuth;
}

export function getFirebaseClientAuth(config: FirebaseOptions): Auth {
  if (clientAuth) return clientAuth;

  const app = getApps().some((candidate) => candidate.name === FIREBASE_APP_NAME)
    ? getApp(FIREBASE_APP_NAME)
    : initializeApp(config, FIREBASE_APP_NAME);

  try {
    clientAuth = initializeAuth(app, { persistence: inMemoryPersistence });
  } catch (error) {
    // Hot module replacement can preserve the named app and its initialized Auth.
    if (typeof error !== 'object' || error === null || !('code' in error)
      || error.code !== 'auth/already-initialized') {
      throw error;
    }
    clientAuth = getAuth(app);
  }
  return clientAuth;
}

export function safeReturnTo(value: string | null | undefined, fallback = '/'): string {
  if (!value || !value.startsWith('/') || value.startsWith('//') || value.includes('\\')) {
    return fallback;
  }
  try {
    const parsed = new URL(value, window.location.origin);
    if (parsed.origin !== window.location.origin || parsed.pathname === '/auth/action') {
      return fallback;
    }
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return fallback;
  }
}