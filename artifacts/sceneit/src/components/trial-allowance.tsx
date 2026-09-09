import { useAuth } from '@workspace/replit-auth-web';

export function TrialAllowance() {
  const { usage, error } = useAuth();
  if (!usage || error) return null;
  const lifetime = usage.lifetime;
  return <section aria-label={lifetime ? 'Lifetime trial allowance' : 'Current subscription allowance'} className="rounded-xl border border-primary/25 bg-primary/5 p-5 space-y-3" data-testid="trial-allowance">
    <h2 className="text-sm font-semibold">{lifetime ? 'Your lifetime allowance' : 'Your current subscription allowance'}</h2>
    <div className="flex flex-wrap gap-x-8 gap-y-3" aria-live="polite">
      <p><strong className="text-xl text-primary">{usage.importsRemaining}</strong> import attempts remaining
        <span className="block text-xs text-muted-foreground">{usage.importsUsed} of {usage.importLimit} used</span></p>
      <p><strong className="text-xl text-primary">{usage.searchesRemaining}</strong> new searches remaining
        <span className="block text-xs text-muted-foreground">{usage.searchesUsed} of {usage.searchLimit} used</span></p>
    </div>
    <p className="text-xs leading-5 text-muted-foreground">{lifetime
      ? 'Lifetime attempts include reserved failed and cancelled attempts. Deletion and expiry do not restore them. No monthly refresh. No payment required.'
      : 'These allowances belong to your current subscription usage window. Reserved failed and cancelled attempts remain consumed; deletion and expiry do not restore them.'}</p>
    {usage.importsRemaining === 0 && <p role="status" className="text-sm text-amber-300">Your {lifetime ? 'lifetime' : 'current'} import allowance is used up.
      Already-reserved work can finish; retained results and cleanup remain available.</p>}
    {usage.searchesRemaining === 0 && <p role="status" className="text-sm text-amber-300">Your {lifetime ? 'lifetime' : 'current'} new-search allowance is used up.
      Opening saved results does not spend a search.</p>}
  </section>;
}