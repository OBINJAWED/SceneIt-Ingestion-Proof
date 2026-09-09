import { useEffect, useRef, useState } from 'react';
import { useLocation } from 'wouter';
import {
  announceAuthRefresh,
  clearPrivateClientState,
  getFirebaseClientAuth,
  setSignoutUnconfirmed,
  useAuth,
} from '@workspace/replit-auth-web';
import { logout as logoutSession } from '@workspace/api-client-react';
import {
  applyActionCode,
  confirmPasswordReset,
  signOut,
  verifyPasswordResetCode,
  type AuthError,
} from 'firebase/auth';
import { useQueryClient } from '@tanstack/react-query';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useToast } from '@/hooks/use-toast';
import { Loader2, CheckCircle2, AlertCircle, KeyRound, ArrowRight } from 'lucide-react';

type ActionStatus = 'pending' | 'success' | 'error' | 'input';

function captureActionParameters() {
  const parameters = new URLSearchParams(window.location.search);
  return {
    mode: parameters.get('mode'),
    oobCode: parameters.get('oobCode'),
  };
}

export default function AuthActionPage() {
  const [, setLocation] = useLocation();
  const { toast } = useToast();
  const auth = useAuth();
  const queryClient = useQueryClient();
  const parameters = useRef(captureActionParameters());
  const actionStarted = useRef(false);
  const [status, setStatus] = useState<ActionStatus>('pending');
  const [isProcessing, setIsProcessing] = useState(true);
  const [errorMessage, setErrorMessage] = useState('');
  const [expired, setExpired] = useState(false);
  const [resetEmail, setResetEmail] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const { mode, oobCode } = parameters.current;

  useEffect(() => {
    // Remove one-time credentials before any network request, reload, copy, or
    // referrer can reuse or disclose them.
    window.history.replaceState(window.history.state, '', window.location.pathname);
  }, []);

  useEffect(() => {
    if (auth.isLoading || actionStarted.current) return;
    actionStarted.current = true;

    if (auth.error) {
      setStatus('error');
      setErrorMessage('Authentication is temporarily unavailable. Please retry from the original email link.');
      setIsProcessing(false);
      return;
    }
    if (!auth.capabilities.emailPassword || !auth.capabilities.publicTrialEnabled || !auth.capabilities.firebaseConfig) {
      setStatus('error');
      setErrorMessage('Email account actions are not currently available.');
      setIsProcessing(false);
      return;
    }
    if (!mode || !oobCode) {
      setStatus('error');
      setErrorMessage('This link is missing required information.');
      setIsProcessing(false);
      return;
    }

    const firebaseAuth = getFirebaseClientAuth(auth.capabilities.firebaseConfig);
    const handleAction = async () => {
      try {
        if (mode === 'verifyEmail') {
          await applyActionCode(firebaseAuth, oobCode);
          // Verification may happen in a new tab where in-memory provider auth
          // is intentionally empty. A fresh explicit sign-in creates app access.
          setSignoutUnconfirmed(true);
          clearPrivateClientState(queryClient);
          try {
            await signOut(firebaseAuth);
          } catch {
            // There may be no in-memory provider session in this tab.
          }
          let sessionClosed = !auth.csrfToken;
          if (auth.csrfToken) {
            try {
              await logoutSession({
                credentials: 'include',
                headers: { 'X-CSRF-Token': auth.csrfToken },
              });
              sessionClosed = true;
            } catch {
              // Local access is already closed; fresh sign-in is still required.
            }
          }
          if (sessionClosed) {
            setSignoutUnconfirmed(false);
            announceAuthRefresh('signout');
          }
          setStatus('success');
        } else if (mode === 'resetPassword') {
          setResetEmail(await verifyPasswordResetCode(firebaseAuth, oobCode));
          setStatus('input');
        } else {
          setStatus('error');
          setErrorMessage('This type of account action is not supported.');
        }
      } catch (error) {
        const code = (error as Partial<AuthError>)?.code;
        if (code === 'auth/expired-action-code') {
          setExpired(true);
          setErrorMessage('This link has expired. Request a new link to continue.');
        } else if (code === 'auth/invalid-action-code') {
          setErrorMessage('This link is invalid or has already been used. Request a new link if needed.');
        } else if (code === 'auth/network-request-failed') {
          setErrorMessage('The authentication service could not be reached. Check your connection and reopen the email link.');
        } else {
          setErrorMessage('We could not verify this link. Request a new link and try again.');
        }
        setStatus('error');
      } finally {
        setIsProcessing(false);
      }
    };
    void handleAction();
  }, [
    auth.capabilities.emailPassword,
    auth.capabilities.firebaseConfig,
    auth.capabilities.publicTrialEnabled,
    auth.capabilities,
    auth.csrfToken,
    auth.error,
    auth.isLoading,
    mode,
    oobCode,
    queryClient,
  ]);

  const clearAccessAfterReset = async () => {
    const config = auth.capabilities.firebaseConfig;
    setSignoutUnconfirmed(true);
    clearPrivateClientState(queryClient);
    if (config) {
      try {
        await signOut(getFirebaseClientAuth(config));
      } catch {
        // Local application access is already fail-closed.
      }
    }
    let sessionClosed = !auth.csrfToken;
    if (auth.csrfToken) {
      try {
        await logoutSession({
          credentials: 'include',
          headers: {
            'X-CSRF-Token': auth.csrfToken,
          },
        });
        sessionClosed = true;
      } catch {
        // Password-reset completion still requires an explicit fresh sign-in.
      }
    }
    if (sessionClosed) {
      setSignoutUnconfirmed(false);
      announceAuthRefresh('signout');
    }
  };

  const handlePasswordReset = async (event: React.FormEvent) => {
    event.preventDefault();
    const config = auth.capabilities.firebaseConfig;
    if (!newPassword || newPassword.length < 6 || !oobCode || !config) return;
    setIsProcessing(true);
    try {
      await confirmPasswordReset(getFirebaseClientAuth(config), oobCode, newPassword);
      setNewPassword('');
      await clearAccessAfterReset();
      setStatus('success');
      toast({ title: 'Password updated', description: 'Sign in again with your new password.' });
    } catch (error) {
      const code = (error as Partial<AuthError>)?.code;
      if (code === 'auth/weak-password') {
        toast({ title: 'Weak password', description: 'Use at least 6 characters.', variant: 'destructive' });
      } else {
        setExpired(code === 'auth/expired-action-code' || code === 'auth/invalid-action-code');
        setErrorMessage(code === 'auth/network-request-failed'
          ? 'The authentication service could not be reached. Check your connection and retry.'
          : 'This reset link is invalid or expired. Request a new reset link.');
        setStatus('error');
      }
    } finally {
      setIsProcessing(false);
    }
  };

  if (auth.isLoading || (isProcessing && status === 'pending')) {
    return <main className="min-h-screen grid place-items-center p-6 bg-background"><Loader2 className="animate-spin text-primary size-8" /></main>;
  }

  return (
    <main className="min-h-screen flex items-center justify-center p-6 bg-background">
      <Card className="w-full max-w-md shadow-lg border-border/60">
        {status === 'success' && mode === 'verifyEmail' && (
          <>
            <CardHeader className="text-center">
              <CheckCircle2 className="mx-auto mb-3 size-10 text-green-500" />
              <CardTitle role="heading" aria-level={1}>Email verified</CardTitle>
              <CardDescription data-testid="status-email-verified">Your address is verified. Sign in again to create a fresh secure session.</CardDescription>
            </CardHeader>
            <CardContent>
              <Button data-testid="button-signin-after-action" className="w-full" onClick={() => setLocation('/auth?mode=signin', { replace: true })}>
                Sign in to continue<ArrowRight className="ml-2 size-4" />
              </Button>
            </CardContent>
          </>
        )}

        {status === 'success' && mode === 'resetPassword' && (
          <>
            <CardHeader className="text-center">
              <CheckCircle2 className="mx-auto mb-3 size-10 text-green-500" />
              <CardTitle role="heading" aria-level={1}>Password reset complete</CardTitle>
              <CardDescription data-testid="status-password-reset">Provider and app access were cleared. Sign in explicitly with your new password.</CardDescription>
            </CardHeader>
            <CardContent><Button data-testid="button-signin-after-reset" className="w-full" onClick={() => setLocation('/auth?mode=signin', { replace: true })}>Go to sign in</Button></CardContent>
          </>
        )}

        {status === 'input' && mode === 'resetPassword' && (
          <>
            <CardHeader className="text-center">
              <KeyRound className="mx-auto mb-3 size-10 text-primary" />
              <CardTitle role="heading" aria-level={1}>Set a new password</CardTitle>
              <CardDescription data-testid="text-reset-email">For <strong>{resetEmail}</strong></CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handlePasswordReset} className="space-y-5">
                <div className="space-y-2">
                  <Label htmlFor="new-password">New password</Label>
                  <Input data-testid="input-new-password" id="new-password" type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} required minLength={6} disabled={isProcessing} autoComplete="new-password" />
                </div>
                <Button data-testid="button-reset-password" type="submit" className="w-full" disabled={isProcessing || newPassword.length < 6}>
                  {isProcessing && <Loader2 className="mr-2 size-4 animate-spin" />}Reset password
                </Button>
              </form>
            </CardContent>
          </>
        )}

        {status === 'error' && (
          <>
            <CardHeader className="text-center">
              <AlertCircle className="mx-auto mb-3 size-10 text-destructive" />
              <CardTitle role="heading" aria-level={1}>{expired ? 'Link expired' : 'Link unavailable'}</CardTitle>
              <CardDescription data-testid="status-action-error">{errorMessage}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <Button data-testid="button-recover-action" className="w-full" onClick={() => setLocation('/auth', { replace: true })}>
                {mode === 'verifyEmail' ? 'Sign in to resend verification' : 'Request a new reset link'}
              </Button>
              {auth.capabilities.replit && <Button data-testid="button-replit-login" variant="outline" className="w-full" onClick={auth.login}>Continue with Replit</Button>}
            </CardContent>
          </>
        )}
      </Card>
    </main>
  );
}