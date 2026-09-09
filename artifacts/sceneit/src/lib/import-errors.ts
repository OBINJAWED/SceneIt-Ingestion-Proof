export function importError(error: unknown): string {
  const value = error as { data?: { code?: string; error?: { message?: string } | string; message?: string }; message?: string };
  const messages: Record<string, string> = {
    owner_import_limit: 'Your lifetime import attempts are used up. Failed and cancelled reserved attempts count; there is no monthly reset.',
    owner_search_limit: 'Your lifetime new searches are used up. Saved results are still available without spending a search.',
    app_import_limit: 'SceneIt has reached its shared import capacity. This is separate from your account allowance.',
    app_search_limit: 'SceneIt has reached its shared search capacity. Saved results remain available.',
    verification_required: 'Verify your email and sign in again before starting new processing.',
    public_trial_disabled: 'Public trial processing is not open. Existing results and cleanup remain available.',
    firebase_not_configured: 'Email processing is not configured. Replit pilot login remains available.',
    identity_unavailable: 'Your email account could not be verified. Sign in again or retry when authentication is available.',
  };
  if (value?.data?.code && messages[value.data.code]) return messages[value.data.code];
  const detail = value?.data?.error;
  return (typeof detail === 'string' ? detail : detail?.message)
    || value?.data?.message || value?.message || 'The request could not complete. Please retry.';
}