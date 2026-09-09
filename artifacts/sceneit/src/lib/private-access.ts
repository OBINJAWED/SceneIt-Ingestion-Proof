type PrivateSession = {
  error?: unknown;
  isAuthenticated: boolean;
  pilotAdmitted: boolean;
  user: { provider?: string; emailVerified?: boolean } | null;
  privateAccess: { allowed: boolean; reason: string };
};

/** Closing new trial work must not hide valid owners' retained results/cleanup. */
export function canReadPrivate(auth: PrivateSession): boolean {
  if (auth.error || !auth.isAuthenticated) return false;
  if (auth.user?.provider !== 'firebase') return auth.pilotAdmitted;
  return auth.user.emailVerified === true
    && ['ready', 'public_trial_disabled'].includes(auth.privateAccess.reason);
}

export function privateAccessMessage(reason: string): string {
  const messages: Record<string, string> = {
    authentication_required: 'Create an email account or sign in before importing. Email verification is required for processing.',
    verification_required: 'Verify your email before starting an import or a new search.',
    public_trial_disabled: 'Public trials are not open yet. Existing admitted Replit pilots can still sign in.',
    firebase_not_configured: 'Email signup is not configured yet. Replit pilot sign-in is still available.',
    identity_unavailable: 'Your email account could not be verified. Sign in again, or retry when authentication is available.',
    pilot_not_admitted: 'This Replit account is not admitted to the pilot. Email signup does not grant access to the shared demo.',
  };
  return messages[reason] || 'Private processing is currently unavailable.';
}