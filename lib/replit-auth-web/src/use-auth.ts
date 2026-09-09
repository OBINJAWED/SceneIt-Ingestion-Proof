import { useCallback, useEffect, useSyncExternalStore } from 'react';
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { getAuthSession, logout as logoutSession, type AuthState } from '@workspace/api-client-react';
import { signOut } from 'firebase/auth';
import { existingFirebaseClientAuth, safeReturnTo } from './firebase-client';
import { clearPendingImportLink } from './pending-import-link';

const SESSION_QUERY_KEY = ['/api/auth/session'] as const;
const AUTH_CHANNEL = 'sceneit:auth-refresh';
const AUTH_TAB_ID = crypto.randomUUID();
const SIGNOUT_LATCH_KEY = 'sceneit:signout-unconfirmed';
const signoutLatchListeners = new Set<() => void>();
let signoutLatchMemory: boolean | undefined;

function readSignoutLatch(): boolean {
  if (signoutLatchMemory !== undefined) return signoutLatchMemory;
  try {
    signoutLatchMemory = sessionStorage.getItem(SIGNOUT_LATCH_KEY) === '1';
  } catch {
    signoutLatchMemory = false;
  }
  return signoutLatchMemory;
}

function subscribeSignoutLatch(listener: () => void) {
  signoutLatchListeners.add(listener);
  return () => signoutLatchListeners.delete(listener);
}

export function setSignoutUnconfirmed(unconfirmed: boolean) {
  signoutLatchMemory = unconfirmed;
  try {
    if (unconfirmed) sessionStorage.setItem(SIGNOUT_LATCH_KEY, '1');
    else sessionStorage.removeItem(SIGNOUT_LATCH_KEY);
  } catch {
    // The in-memory latch still protects this mounted tab.
  }
  signoutLatchListeners.forEach((listener) => listener());
}

const unavailableState = (reason = 'identity_unavailable'): AuthState => ({
  csrfToken: null,
  user: null,
  pilotAdmitted: false,
  capabilities: {
    replit: false,
    emailPassword: false,
    publicTrialEnabled: false,
    unavailableReason: null,
    firebaseConfig: null,
  },
  privateAccess: { allowed: false, reason: reason as AuthState['privateAccess']['reason'] },
  usage: null,
});

function clearBrowserPrivateState(preserveAnonymousImportLink: boolean) {
  clearPendingImportLink(preserveAnonymousImportLink);
  try {
    for (let index = localStorage.length - 1; index >= 0; index -= 1) {
      const key = localStorage.key(index);
      if (key?.startsWith('sceneit:')) localStorage.removeItem(key);
    }
  } catch {
    // Cache removal is best effort; server authorization remains authoritative.
  }
}

export function clearPrivateClientState(
  queryClient: QueryClient,
  { preserveAnonymousImportLink = false }: { preserveAnonymousImportLink?: boolean } = {},
) {
  queryClient.removeQueries({
    predicate: (query) => query.queryKey[0] !== SESSION_QUERY_KEY[0],
  });
  clearBrowserPrivateState(preserveAnonymousImportLink);
}

/** Publish a confirmed closed session before releasing the local access latch. */
export async function closeClientAuthSession(queryClient: QueryClient) {
  await queryClient.cancelQueries({ queryKey: SESSION_QUERY_KEY });
  queryClient.setQueryData<AuthState>(SESSION_QUERY_KEY, (current) => ({
    ...(current ?? unavailableState('authentication_required')),
    csrfToken: null,
    user: null,
    pilotAdmitted: false,
    privateAccess: { allowed: false, reason: 'authentication_required' },
    usage: null,
  }));
}

export function announceAuthRefresh(kind: 'refresh' | 'signout' | 'recovery' = 'refresh') {
  if (typeof BroadcastChannel === 'undefined') return;
  const channel = new BroadcastChannel(AUTH_CHANNEL);
  channel.postMessage({ kind, source: AUTH_TAB_ID });
  channel.close();
}

function sessionIdentity(state: AuthState | undefined): string {
  return JSON.stringify([
    state?.user?.provider, state?.user?.id, state?.user?.email,
    state?.user?.emailVerified, state?.pilotAdmitted,
  ]);
}

export function useAuth() {
  const queryClient = useQueryClient();
  const { data, isLoading, error, refetch } = useQuery<AuthState>({
    queryKey: SESSION_QUERY_KEY,
    queryFn: async ({ signal }) => {
      const next = await getAuthSession({ credentials: 'include', cache: 'no-store', signal });
      const previous = queryClient.getQueryData<AuthState>(SESSION_QUERY_KEY);
      if (sessionIdentity(previous) !== sessionIdentity(next)) {
        // Invalidate the old owner before publishing the new session. Clearing
        // in a render effect can delete newly mounted child queries instead.
        clearPrivateClientState(queryClient, { preserveAnonymousImportLink: true });
      }
      return next;
    },
    staleTime: 30_000,
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
    refetchOnMount: 'always',
    refetchOnWindowFocus: true,
    retry: false,
    // Mounting the error/recovery UI adds auth subscribers. Do not let those
    // mounts restart a failed initial check and replace recovery with a spinner.
    retryOnMount: false,
  });

  const signoutUnconfirmed = useSyncExternalStore(
    subscribeSignoutLatch,
    readSignoutLatch,
    () => false,
  );
  // Keep the latest server state internally so a failed signout can be retried
  // with its CSRF token, but never expose it while the local latch is closed.
  const serverState = error ? unavailableState() : (data ?? unavailableState('authentication_required'));
  const authState = signoutUnconfirmed ? {
    ...serverState,
    csrfToken: null,
    user: null,
    pilotAdmitted: false,
    privateAccess: { allowed: false, reason: 'authentication_required' as const },
    usage: null,
  } : serverState;
  useEffect(() => {
    if (typeof BroadcastChannel === 'undefined') return;
    const channel = new BroadcastChannel(AUTH_CHANNEL);
    channel.onmessage = async (event) => {
      // A page can have several useAuth subscribers. Its own announcement must
      // not reset the session it just exchanged or remount its restored form.
      if (event.data?.source === AUTH_TAB_ID) return;
      const kind = typeof event.data === 'string' ? event.data : event.data?.kind;
      // Recovery closes access just like signout, but an anonymous onboarding
      // draft in the original tab can still be used after explicit fresh login.
      const closesSession = kind === 'signout' || kind === 'recovery';
      clearPrivateClientState(queryClient, {
        preserveAnonymousImportLink: kind === 'refresh' || kind === 'recovery',
      });
      if (closesSession) {
        const providerAuth = existingFirebaseClientAuth();
        if (providerAuth) void signOut(providerAuth).catch(() => undefined);
      }
      await closeClientAuthSession(queryClient);
      if (closesSession) setSignoutUnconfirmed(false);
      void queryClient.invalidateQueries({ queryKey: SESSION_QUERY_KEY });
    };
    return () => channel.close();
  }, [queryClient]);

  const handleLogin = useCallback(() => {
    const current = `${window.location.pathname}${window.location.search}`;
    const returnTo = window.location.pathname === '/auth'
      ? safeReturnTo(new URLSearchParams(window.location.search).get('returnTo'), '/')
      : safeReturnTo(current, '/');
    // Choosing a provider login is an explicit fresh-signin action.
    setSignoutUnconfirmed(false);
    window.location.assign(`/api/login?returnTo=${encodeURIComponent(returnTo)}`);
  }, []);

  const handleLogout = useCallback(async (): Promise<boolean> => {
    // Hide and discard private state as soon as signout is requested, regardless
    // of provider or API availability.
    setSignoutUnconfirmed(true);
    clearPrivateClientState(queryClient);

    const providerAuth = existingFirebaseClientAuth();
    if (providerAuth) {
      try {
        await signOut(providerAuth);
      } catch {
        // Continue with application-session logout.
      }
    }

    try {
      await logoutSession({
        headers: {
          'X-CSRF-Token': serverState.csrfToken ?? '',
        },
        credentials: 'include',
      });
      queryClient.clear();
      setSignoutUnconfirmed(false);
      announceAuthRefresh('signout');
      window.location.assign('/');
      return true;
    } catch {
      // Do not claim that the server session was revoked. A refresh/retry can
      // safely establish its authoritative state again.
      window.alert('We could not confirm sign out. Your private data was cleared from this tab. Please retry.');
      return false;
    }
  }, [queryClient, serverState]);

  return {
    user: authState.user,
    csrfToken: authState.csrfToken,
    pilotAdmitted: authState.pilotAdmitted,
    isAuthenticated: Boolean(authState.user),
    isLoading,
    error,
    signoutUnconfirmed,
    capabilities: authState.capabilities,
    privateAccess: authState.privateAccess,
    usage: authState.usage,
    login: handleLogin,
    logout: handleLogout,
    retrySignout: handleLogout,
    refresh: refetch,
  };
}