import { useEffect, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  exchangeFirebaseSession,
  getFirebaseChallenge,
} from '@workspace/api-client-react';
import {
  announceAuthRefresh,
  clearPrivateClientState,
  getFirebaseClientAuth,
  safeReturnTo,
  setSignoutUnconfirmed,
  useAuth,
} from '@workspace/replit-auth-web';
import {
  createUserWithEmailAndPassword,
  sendEmailVerification,
  sendPasswordResetEmail,
  signInWithEmailAndPassword,
  signOut,
  type AuthError,
} from 'firebase/auth';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { useToast } from '@/hooks/use-toast';
import { Loader2, ArrowLeft, Mail, KeyRound, AlertCircle, Film } from 'lucide-react';
import { useLocation } from 'wouter';

type Mode = 'signin' | 'signup' | 'reset';

function authMessage(error: unknown, fallback: string): string {
  const code = (error as Partial<AuthError>)?.code;
  if (code === 'auth/email-already-in-use') return 'An account with this email already exists. Sign in or reset your password.';
  if (code === 'auth/invalid-email') return 'Please enter a valid email address.';
  if (code === 'auth/weak-password') return 'Password must be at least 6 characters.';
  if (code === 'auth/user-not-found' || code === 'auth/wrong-password' || code === 'auth/invalid-credential') return 'Invalid email or password.';
  if (code === 'auth/too-many-requests') return 'Too many attempts. Wait a few minutes, then try again or reset your password.';
  if (code === 'auth/network-request-failed') return 'The authentication service could not be reached. Check your connection and try again.';
  return fallback;
}

export default function AuthPage() {
  const [, setLocation] = useLocation();
  const { toast } = useToast();
  const auth = useAuth();
  const queryClient = useQueryClient();
  const returnTo = useMemo(
    () => safeReturnTo(new URLSearchParams(window.location.search).get('returnTo'), '/'),
    [],
  );

  const [mode, setMode] = useState<Mode>(() => {
    const requested = new URLSearchParams(window.location.search).get('mode');
    return requested === 'signin' || requested === 'reset' ? requested : 'signup';
  });
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [isProcessing, setIsProcessing] = useState(false);
  const [verificationSent, setVerificationSent] = useState(false);
  const [verificationDeliveryFailed, setVerificationDeliveryFailed] = useState(false);
  const [resetSent, setResetSent] = useState(false);

  useEffect(() => {
    if (!auth.isLoading && auth.isAuthenticated && auth.user?.emailVerified) {
      setLocation(returnTo, { replace: true });
    }
  }, [auth.isAuthenticated, auth.isLoading, auth.user?.emailVerified, returnTo, setLocation]);

  const emailFlowEnabled = auth.capabilities.emailPassword
    && auth.capabilities.publicTrialEnabled
    && Boolean(auth.capabilities.firebaseConfig);

  const showError = (error: unknown, fallback: string, title = 'Authentication failed') => {
    toast({ title, description: authMessage(error, fallback), variant: 'destructive' });
  };

  const exchangeSession = async (idToken: string) => {
    const challenge = await getFirebaseChallenge({ credentials: 'include', cache: 'no-store' });
    const nextState = await exchangeFirebaseSession(
      { idToken },
      {
        credentials: 'include',
        headers: { 'X-CSRF-Token': challenge.csrfToken },
      },
    );
    await queryClient.cancelQueries({ queryKey: ['/api/auth/session'] });
    clearPrivateClientState(queryClient);
    queryClient.setQueryData(['/api/auth/session'], nextState);
    setSignoutUnconfirmed(false);
    announceAuthRefresh();
  };

  const handleSignup = async (event: React.FormEvent) => {
    event.preventDefault();
    const config = auth.capabilities.firebaseConfig;
    if (!emailFlowEnabled || !config || !email || !password) return;
    setIsProcessing(true);
    setVerificationDeliveryFailed(false);
    try {
      const credential = await createUserWithEmailAndPassword(
        getFirebaseClientAuth(config),
        email.trim(),
        password,
      );
      setVerificationSent(true);
      try {
        await sendEmailVerification(credential.user);
      } catch (error) {
        setVerificationDeliveryFailed(true);
        showError(error, 'Your account was created, but the verification email could not be sent. Use Resend below.', 'Verification email not sent');
      }
    } catch (error) {
      showError(error, 'We could not create your account.');
    } finally {
      setIsProcessing(false);
    }
  };

  const handleSignin = async (event: React.FormEvent) => {
    event.preventDefault();
    const config = auth.capabilities.firebaseConfig;
    if (!emailFlowEnabled || !config || !email || !password) return;
    setIsProcessing(true);
    try {
      const credential = await signInWithEmailAndPassword(
        getFirebaseClientAuth(config),
        email.trim(),
        password,
      );
      if (!credential.user.emailVerified) {
        setVerificationSent(true);
        return;
      }
      await exchangeSession(await credential.user.getIdToken(true));
      setPassword('');
      setLocation(returnTo, { replace: true });
    } catch (error) {
      const configForCleanup = auth.capabilities.firebaseConfig;
      if (configForCleanup) {
        try {
          await signOut(getFirebaseClientAuth(configForCleanup));
        } catch {
          // The application session remains closed because exchange did not complete.
        }
      }
      showError(error, 'We could not sign you in.');
    } finally {
      setIsProcessing(false);
    }
  };

  const handleReset = async (event: React.FormEvent) => {
    event.preventDefault();
    const config = auth.capabilities.firebaseConfig;
    if (!emailFlowEnabled || !config || !email) return;
    setIsProcessing(true);
    try {
      await sendPasswordResetEmail(getFirebaseClientAuth(config), email.trim());
      setResetSent(true);
    } catch (error) {
      const code = (error as Partial<AuthError>)?.code;
      if (code === 'auth/user-not-found' || code === 'auth/invalid-credential') {
        setResetSent(true);
      } else {
        showError(error, 'The reset request failed. Check your connection and try again.', 'Reset email not sent');
      }
    } finally {
      setIsProcessing(false);
    }
  };

  const resendVerification = async () => {
    const config = auth.capabilities.firebaseConfig;
    if (!config) return;
    const currentUser = getFirebaseClientAuth(config).currentUser;
    if (!currentUser) {
      setVerificationSent(false);
      setMode('signin');
      toast({
        title: 'Sign in again',
        description: 'For your security, sign in again before requesting another verification email.',
      });
      return;
    }
    setIsProcessing(true);
    try {
      await sendEmailVerification(currentUser);
      setVerificationDeliveryFailed(false);
      toast({ title: 'Email sent', description: 'A new verification link has been sent.' });
    } catch (error) {
      showError(error, 'The verification email could not be sent. Try again shortly.', 'Email not sent');
    } finally {
      setIsProcessing(false);
    }
  };

  const signInAfterVerification = async () => {
    const config = auth.capabilities.firebaseConfig;
    if (config) {
      try {
        await signOut(getFirebaseClientAuth(config));
      } catch {
        // Continue to the explicit fresh-signin form.
      }
    }
    setPassword('');
    setVerificationSent(false);
    setMode('signin');
    toast({ title: 'Sign in again', description: 'After verification, sign in again to start a fresh session.' });
  };

  if (auth.isLoading) {
    return <main className="min-h-screen grid place-items-center p-6 bg-background"><Loader2 className="animate-spin text-primary size-8" /></main>;
  }

  if (auth.error) {
    return (
      <main className="min-h-screen grid place-items-center p-6 bg-background">
        <Card className="w-full max-w-md">
          <CardHeader className="text-center">
            <AlertCircle className="mx-auto mb-3 size-8 text-destructive" />
            <CardTitle role="heading" aria-level={1}>Sign-in status unavailable</CardTitle>
            <CardDescription data-testid="status-auth-unavailable">We could not safely load authentication. Retry before continuing.</CardDescription>
          </CardHeader>
          <CardContent><Button data-testid="button-retry-auth" className="w-full" onClick={() => void auth.refresh()}>Retry</Button></CardContent>
        </Card>
      </main>
    );
  }

  if (!emailFlowEnabled) {
    return (
      <main className="min-h-screen flex items-center justify-center p-6 bg-background">
        <Card className="w-full max-w-md shadow-lg border-border/60">
          <CardHeader className="text-center">
            <Film className="mx-auto mb-3 size-8 text-primary" />
            <CardTitle role="heading" aria-level={1}>Sign in</CardTitle>
            <CardDescription data-testid="status-email-auth-closed">Email signup is not currently available.</CardDescription>
          </CardHeader>
          <CardContent>
            {auth.capabilities.replit ? (
              <Button data-testid="button-replit-login" className="w-full h-12" onClick={auth.login}>Continue with Replit</Button>
            ) : (
              <p className="text-center text-sm text-muted-foreground">No sign-in method is currently configured.</p>
            )}
          </CardContent>
        </Card>
      </main>
    );
  }

  if (verificationSent || (auth.isAuthenticated && !auth.user?.emailVerified)) {
    return (
      <main className="min-h-screen flex items-center justify-center p-6 bg-background">
        <Card className="w-full max-w-md shadow-lg border-border/60">
          <CardHeader className="text-center">
            <Mail className="mx-auto mb-3 size-8 text-primary" />
            <CardTitle role="heading" aria-level={1}>Verify your email</CardTitle>
            <CardDescription data-testid="status-verification">
              Check the verification link sent to <strong>{auth.user?.email || email}</strong>.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {verificationDeliveryFailed && <p className="text-sm text-destructive">The first email was not sent. Retry below.</p>}
            <Button data-testid="button-resend-verification" variant="outline" className="w-full" onClick={resendVerification} disabled={isProcessing}>
              {isProcessing && <Loader2 className="mr-2 size-4 animate-spin" />}Resend verification email
            </Button>
            <Button data-testid="button-signin-after-verification" className="w-full" onClick={signInAfterVerification} disabled={isProcessing}>
              I've verified — sign in again
            </Button>
          </CardContent>
          <CardFooter className="justify-center">
            <Button data-testid="button-cancel-verification" variant="ghost" onClick={() => void auth.logout()}>Sign out</Button>
          </CardFooter>
        </Card>
      </main>
    );
  }

  if (resetSent) {
    return (
      <main className="min-h-screen grid place-items-center p-6 bg-background">
        <Card className="w-full max-w-md">
          <CardHeader className="text-center">
            <Mail className="mx-auto mb-3 size-8 text-primary" />
            <CardTitle role="heading" aria-level={1}>Check your email</CardTitle>
            <CardDescription data-testid="status-reset-sent">If an account exists for <strong>{email}</strong>, reset instructions have been sent.</CardDescription>
          </CardHeader>
          <CardFooter className="justify-center">
            <Button data-testid="button-back-signin" variant="ghost" onClick={() => { setResetSent(false); setMode('signin'); }}><ArrowLeft className="mr-2 size-4" />Back to sign in</Button>
          </CardFooter>
        </Card>
      </main>
    );
  }

  return (
    <main className="min-h-screen flex items-center justify-center p-6 bg-background">
      <div className="w-full max-w-md">
        <div className="mb-8 text-center">
          <Film className="mx-auto mb-4 size-10 text-primary" />
          <h1 className="text-3xl font-semibold">{mode === 'signup' ? 'Create an account' : mode === 'signin' ? 'Welcome back' : 'Reset password'}</h1>
          <p className="mt-2 text-muted-foreground">{mode === 'reset' ? 'Enter your email to receive a reset link.' : 'Use your verified email to access private videos.'}</p>
        </div>
        <Card className="shadow-lg border-border/60">
          <CardContent className="pt-6">
            <form onSubmit={mode === 'signup' ? handleSignup : mode === 'signin' ? handleSignin : handleReset} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="email">Email</Label>
                <Input data-testid="input-email" id="email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} required disabled={isProcessing} autoComplete="email" />
              </div>
              {mode !== 'reset' && (
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <Label htmlFor="password">Password</Label>
                    {mode === 'signin' && <button data-testid="button-forgot-password" type="button" onClick={() => setMode('reset')} className="text-xs font-medium text-primary hover:underline">Forgot password?</button>}
                  </div>
                  <div className="relative">
                    <KeyRound className="absolute left-3 top-3 size-4 text-muted-foreground" />
                    <Input data-testid="input-password" id="password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} className="pl-10" required minLength={6} disabled={isProcessing} autoComplete={mode === 'signup' ? 'new-password' : 'current-password'} />
                  </div>
                </div>
              )}
              <Button data-testid="button-submit-auth" type="submit" className="w-full" disabled={isProcessing || !email || (mode !== 'reset' && !password)}>
                {isProcessing && <Loader2 className="mr-2 size-4 animate-spin" />}
                {mode === 'signup' ? 'Create account' : mode === 'signin' ? 'Sign in' : 'Send reset link'}
              </Button>
            </form>
            {auth.capabilities.replit && <Button data-testid="button-replit-login" variant="outline" className="mt-4 w-full" onClick={auth.login} disabled={isProcessing}>Continue with Replit</Button>}
          </CardContent>
          <CardFooter className="justify-center">
            {mode === 'signup' ? (
              <button data-testid="button-mode-signin" type="button" onClick={() => setMode('signin')} className="text-sm text-primary hover:underline">Already have an account? Sign in</button>
            ) : mode === 'signin' ? (
              <button data-testid="button-mode-signup" type="button" onClick={() => setMode('signup')} className="text-sm text-primary hover:underline">Create an account</button>
            ) : (
              <button data-testid="button-mode-signin" type="button" onClick={() => setMode('signin')} className="text-sm text-primary hover:underline"><ArrowLeft className="mr-1 inline size-4" />Back to sign in</button>
            )}
          </CardFooter>
        </Card>
      </div>
    </main>
  );
}