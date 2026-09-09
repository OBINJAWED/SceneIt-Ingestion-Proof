import { useCallback, useEffect, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

export interface AuthStateUser {
  id: string;
  firstName: string | null;
}

export interface AuthState {
  csrfToken: string | null;
  user: AuthStateUser | null;
}

export function useAuth() {
  const queryClient = useQueryClient();
  
  const { 
    data: authState, 
    isLoading, 
    error 
  } = useQuery<AuthState>({
    queryKey: ['/api/auth/user'],
    queryFn: async () => {
      const res = await fetch('/api/auth/user', {
        headers: { 'Accept': 'application/json' },
        credentials: 'include'
      });
      if (!res.ok) {
        if (res.status === 401) {
          return { user: null, csrfToken: null };
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
    const owner = authState.user?.id || null;
    if (previousOwner.current !== undefined && previousOwner.current !== owner) {
      queryClient.removeQueries({ predicate: query => String(query.queryKey[0]).startsWith('/api/imports') });
    }
    previousOwner.current = owner;
  }, [authState?.user?.id, queryClient]);

  const isAuthenticated = !!authState?.user;
  const user = authState?.user || null;
  const csrfToken = authState?.csrfToken || null;

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
      window.location.href = '/';
    } catch (e) {
      console.error('Logout error:', e);
      alert('Logout failed. Please try again or refresh the page.');
    }
  }, [csrfToken, queryClient]);

  return {
    user,
    csrfToken,
    isAuthenticated,
    isLoading,
    error,
    login: handleLogin,
    logout: handleLogout,
  };
}