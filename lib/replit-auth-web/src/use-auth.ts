import { useCallback, useEffect, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

export interface AuthStateUser {
  id: string;
  firstName: string | null;
}

export interface AuthState {
  csrfToken: string | null;
  user: AuthStateUser | null;
  pilotAdmitted: boolean;
}

export function useAuth() {
  const queryClient = useQueryClient();
  
  const { 
    data: authState, 
    isLoading, 
    error 
  } = useQuery<AuthState>({
    queryKey: ['/api/auth/session'],
    queryFn: async () => {
      const res = await fetch('/api/auth/session', {
        headers: { 'Accept': 'application/json' },
        credentials: 'include',
        cache: 'no-store',
      });
      if (!res.ok) {
        if (res.status === 401) {
          return { user: null, csrfToken: null, pilotAdmitted: false };
        }
        throw new Error('Failed to fetch auth state');
      }
      return res.json();
    },
    staleTime: 30000,
    refetchOnWindowFocus: true,
    retry: false,
  });
  const previousOwner = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (!authState) return;
    const owner = `${authState.user?.id || 'anonymous'}:${authState.pilotAdmitted}`;
    if (previousOwner.current !== undefined && previousOwner.current !== owner) {
      queryClient.removeQueries({
        predicate: query => {
          const key = String(query.queryKey[0]);
          return key.startsWith('/api/imports') || key.startsWith('/api/proof');
        },
      });
      sessionStorage.removeItem('pendingImportLink');
      for (let index = localStorage.length - 1; index >= 0; index -= 1) {
        const key = localStorage.key(index);
        if (key?.startsWith('sceneit:')) localStorage.removeItem(key);
      }
    }
    previousOwner.current = owner;
  }, [authState?.user?.id, authState?.pilotAdmitted, queryClient]);

  const isAuthenticated = !!authState?.user;
  const user = authState?.user || null;
  const csrfToken = authState?.csrfToken || null;
  const pilotAdmitted = authState?.pilotAdmitted === true;

  const handleLogin = useCallback(() => {
    const returnTo = window.location.pathname + window.location.search;
    window.location.href = `/api/login?returnTo=${encodeURIComponent(returnTo)}`;
  }, []);

  const handleLogout = useCallback(async () => {
    try {
      const res = await fetch('/api/logout', {
        method: 'POST',
        headers: { 
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrfToken || '' 
        },
        credentials: 'include',
        body: '{}'
      });
      if (!res.ok) {
        throw new Error('Logout failed with status ' + res.status);
      }
      queryClient.clear();
      sessionStorage.removeItem('pendingImportLink');
      for (let index = localStorage.length - 1; index >= 0; index -= 1) {
        const key = localStorage.key(index);
        if (key?.startsWith('sceneit:')) localStorage.removeItem(key);
      }
      window.location.href = '/';
    } catch (e) {
      console.error('Logout error:', e);
      alert('Logout failed. Please try again or refresh the page.');
    }
  }, [csrfToken, queryClient]);

  return {
    user,
    csrfToken,
    pilotAdmitted,
    isAuthenticated,
    isLoading,
    error,
    login: handleLogin,
    logout: handleLogout,
  };
}