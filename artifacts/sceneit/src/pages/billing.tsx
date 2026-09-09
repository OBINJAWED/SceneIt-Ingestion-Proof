import { useEffect, useMemo, useState } from 'react';
import { useAuth } from '@workspace/replit-auth-web';
import {
  getGetBillingStatusQueryKey,
  useConfirmBillingChange,
  useCreateBillingCheckout,
  useCreateBillingPortal,
  useGetBillingStatus,
  usePreviewBillingChange,
  useWithdrawBillingChange,
  type BillingChangePreview,
  type BillingOffer,
} from '@workspace/api-client-react';
import { useQueryClient } from '@tanstack/react-query';
import { AlertCircle, CalendarClock, Check, CreditCard, ExternalLink, Loader2, ReceiptText, ShieldCheck, ShieldAlert } from 'lucide-react';
import { AuthHeader } from '@/components/auth-header';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle, CardFooter } from '@/components/ui/card';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Progress } from '@/components/ui/progress';
import { useToast } from '@/hooks/use-toast';
import { cn } from '@/lib/utils';

type OperationKind = 'checkout' | 'manage' | 'cancel' | 'preview' | 'confirm' | 'withdraw';

function retireOperation(storageKey: string) {
  sessionStorage.removeItem(storageKey);
  sessionStorage.removeItem(`${storageKey}:expiresAt`);
  sessionStorage.removeItem(`${storageKey}:uncertain`);
}

function operationKey(owner: string, kind: OperationKind, identity: string) {
  const key = `sceneit:billing:${owner}:${kind}:${identity}`;
  let value = sessionStorage.getItem(key);
  const knownExpiry = sessionStorage.getItem(`${key}:expiresAt`);
  if (
    value
    && !sessionStorage.getItem(`${key}:uncertain`)
    && knownExpiry
    && Date.parse(knownExpiry) <= Date.now()
  ) {
    retireOperation(key);
    value = null;
  }
  if (!value) {
    value = crypto.randomUUID();
    sessionStorage.setItem(key, value);
  }
  return { storageKey: key, value };
}

function savedUncertainOperation(owner: string) {
  for (let index = 0; index < sessionStorage.length; index += 1) {
    const key = sessionStorage.key(index);
    if (key?.startsWith(`sceneit:billing:${owner}:`) && key.endsWith(':uncertain')) return key.slice(0, -10);
  }
  return null;
}

const UNCERTAINTY_CLEARING_STATES = new Set(['created', 'scheduled', 'completed', 'withdrawn', 'expired', 'failed']);
const TERMINAL_OPERATION_STATES = new Set(['completed', 'expired', 'failed']);

function money(amount: number, currency: string) {
  const formatter = new Intl.NumberFormat(undefined, { style: 'currency', currency: currency.toUpperCase() });
  const digits = formatter.resolvedOptions().maximumFractionDigits ?? 2;
  return formatter.format(amount / (10 ** digits));
}

function dateTime(value: string | null | undefined) {
  return value ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : 'Not available';
}

function apiMessage(error: unknown) {
  const candidate = error as { data?: { error?: string; state?: string }; message?: string };
  return candidate.data?.error || candidate.message || 'Billing could not be updated. Please try again.';
}

function apiCode(error: unknown) {
  return (error as { data?: { code?: string } }).data?.code;
}

function isUncertain(error: unknown) {
  const candidate = error as { data?: { code?: string; state?: string } };
  const code = candidate.data?.code;
  return candidate.data?.state === 'uncertain'
    || candidate.data?.state === 'creating'
    || candidate.data?.state === 'confirming'
    || code === 'provider_outcome_unknown'
    || code === 'provider_deadline_exceeded'
    || code === 'billing_provider_unavailable'
    || code?.endsWith('_outcome_unknown') === true;
}

function pendingChangeMessage(state: 'confirming' | 'payment_pending' | 'scheduled' | 'uncertain', effectiveAt: string | null) {
  if (state === 'scheduled') return `Scheduled for the next renewal on ${dateTime(effectiveAt)}. Your current tier, currency, and usage remain unchanged until then.`;
  if (state === 'payment_pending') return 'Upgrade payment is still being verified. Your current paid tier and limits remain unchanged until a verified billing event confirms payment.';
  if (state === 'confirming') return 'Confirmation is still processing. Your current paid tier and limits remain unchanged; do not submit another change.';
  return 'The provider outcome is unknown and is being reconciled. Your current paid tier and limits remain unchanged; SceneIt has blocked another billing change.';
}

function Metric({ label, used, limit }: { label: string; used: number; limit: number }) {
  const percent = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
  return (
    <div className="space-y-2.5" data-testid={`metric-${label.toLowerCase().replaceAll(' ', '-')}`}>
      <div className="flex justify-between gap-4 text-sm font-medium">
        <span className="text-foreground">{label}</span>
        <span className="text-muted-foreground">{used.toLocaleString()} / {limit.toLocaleString()}</span>
      </div>
      <Progress value={percent} className="h-2" />
    </div>
  );
}

export default function BillingPage() {
  const auth = useAuth();
  const owner = auth.user?.id || '';
  return <BillingAccount key={`${owner}:${auth.csrfToken || ''}`} owner={owner} />;
}

function BillingAccount({ owner }: { owner: string }) {
  const auth = useAuth();
  const csrfToken = auth.csrfToken || '';
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [preview, setPreview] = useState<BillingChangePreview | null>(null);
  const [previewOperationKey, setPreviewOperationKey] = useState<string | null>(null);
  const [blockedOperation, setBlockedOperation] = useState<string | null>(() => savedUncertainOperation(owner));

  useEffect(() => {
    if (auth.isLoading || auth.error) return;
    for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
      const key = sessionStorage.key(index);
      if (key?.startsWith('sceneit:billing:') && !key.startsWith(`sceneit:billing:${owner}:`)) sessionStorage.removeItem(key);
    }
  }, [auth.isLoading, auth.error, owner]);

  const statusQuery = useGetBillingStatus({
    query: {
      queryKey: getGetBillingStatusQueryKey(),
      enabled: auth.isAuthenticated,
      staleTime: 0,
      refetchOnMount: 'always',
    },
  });
  const mutationOptions = { request: { headers: { 'X-CSRF-Token': csrfToken } }, mutation: { retry: false } } as const;
  const checkout = useCreateBillingCheckout(mutationOptions);
  const portal = useCreateBillingPortal(mutationOptions);
  const previewChange = usePreviewBillingChange(mutationOptions);
  const confirmChange = useConfirmBillingChange(mutationOptions);
  const withdrawChange = useWithdrawBillingChange(mutationOptions);
  const status = statusQuery.data;

  useEffect(() => {
    if (!status?.operationStates) return;
    let reconciledBlockedOperation = false;
    for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
      const storageKey = sessionStorage.key(index);
      if (
        !storageKey?.startsWith(`sceneit:billing:${owner}:`)
        || storageKey.endsWith(':expiresAt')
        || storageKey.endsWith(':uncertain')
      ) continue;
      const idempotencyKey = sessionStorage.getItem(storageKey);
      const matching = status.operationStates.find(operation => operation.idempotencyKey === idempotencyKey);
      if (!matching) continue;
      if (TERMINAL_OPERATION_STATES.has(matching.state)) retireOperation(storageKey);
      const withdrawalResolved = TERMINAL_OPERATION_STATES.has(matching.state) || matching.state === 'withdrawn';
      const uncertaintyCleared = matching.kind === 'withdraw'
        ? withdrawalResolved
        : UNCERTAINTY_CLEARING_STATES.has(matching.state);
      if (uncertaintyCleared) {
        sessionStorage.removeItem(`${storageKey}:uncertain`);
        if (blockedOperation === storageKey) reconciledBlockedOperation = true;
      }
    }
    if (reconciledBlockedOperation) setBlockedOperation(null);
  }, [blockedOperation, status?.operationStates]);

  const offers = useMemo(() => {
    if (!status?.enabled || !auth.pilotAdmitted) return [];
    return status.offers.filter(offer => !status.currency || offer.currency === status.currency);
  }, [auth.pilotAdmitted, status]);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: getGetBillingStatusQueryKey() });
  const rememberExpiration = (storageKey: string, expiresAt: string) => {
    if (!Number.isNaN(Date.parse(expiresAt))) sessionStorage.setItem(`${storageKey}:expiresAt`, expiresAt);
  };
  const handleFailure = (error: unknown, storageKey: string, blockUnknownOutcome = true) => {
    if (blockUnknownOutcome && isUncertain(error)) {
      setBlockedOperation(storageKey);
      sessionStorage.setItem(`${storageKey}:uncertain`, '1');
      toast({ title: 'Billing outcome is being checked', description: 'Do not submit this purchase again. Refresh later for the authoritative status.', variant: 'destructive' });
    } else {
      toast({ title: 'Billing action unavailable', description: apiMessage(error), variant: 'destructive' });
    }
  };
  const openHosted = (url: string, expiresAt: string) => {
    if (Date.parse(expiresAt) <= Date.now()) {
      toast({ title: 'Hosted session expired', description: 'Refresh billing status before trying again.', variant: 'destructive' });
      invalidate();
      return;
    }
    window.location.assign(url);
  };

  const startPortal = async (action: 'manage' | 'cancel') => {
    const operation = operationKey(owner, action, action);
    if (blockedOperation === operation.storageKey) return;
    try {
      const result = await portal.mutateAsync({ data: { action, idempotencyKey: operation.value } });
      rememberExpiration(operation.storageKey, result.expiresAt);
      await invalidate();
      openHosted(result.url, result.expiresAt);
    } catch (error) {
      handleFailure(error, operation.storageKey);
      await invalidate();
    }
  };

  const chooseOffer = async (offer: BillingOffer) => {
    if (status?.pendingChange) return;
    const identity = `${offer.tier}:${offer.cadence}:${offer.currency}`;
    if (status?.membership !== 'active') {
      const operation = operationKey(owner, 'checkout', identity);
      if (blockedOperation === operation.storageKey) return;
      try {
        const result = await checkout.mutateAsync({ data: { tier: offer.tier, cadence: offer.cadence, currency: offer.currency, idempotencyKey: operation.value } });
        rememberExpiration(operation.storageKey, result.expiresAt);
        await invalidate();
        openHosted(result.url, result.expiresAt);
      } catch (error) {
        if (apiCode(error) === 'checkout_completed') retireOperation(operation.storageKey);
        handleFailure(error, operation.storageKey);
        await invalidate();
      }
      return;
    }
    const operation = operationKey(owner, 'preview', identity);
    if (blockedOperation === operation.storageKey) return;
    try {
      const result = await previewChange.mutateAsync({ data: { tier: offer.tier, cadence: offer.cadence, currency: offer.currency, idempotencyKey: operation.value } });
      rememberExpiration(operation.storageKey, result.expiresAt);
      await invalidate();
      setPreview(result);
      setPreviewOperationKey(operation.storageKey);
    } catch (error) {
      if (apiCode(error) === 'preview_expired') retireOperation(operation.storageKey);
      handleFailure(error, operation.storageKey, false);
    }
  };

  const confirmPreview = async () => {
    if (!preview) return;
    const operation = operationKey(owner, 'confirm', preview.previewId);
    if (blockedOperation === operation.storageKey) return;
    try {
      const result = await confirmChange.mutateAsync({ data: { previewId: preview.previewId, idempotencyKey: operation.value } });
      setPreview(null);
      if (previewOperationKey) retireOperation(previewOperationKey);
      setPreviewOperationKey(null);
      await invalidate();
      toast({ title: result.state === 'effective' ? 'Membership updated' : 'Change submitted', description: `Authoritative status: ${result.state.replaceAll('_', ' ')}.` });
      if (result.hostedAction) openHosted(result.hostedAction.url, result.hostedAction.expiresAt);
    } catch (error) {
      handleFailure(error, operation.storageKey);
      await invalidate();
    }
  };

  const withdrawPending = async () => {
    if (!status?.pendingChange || blockedOperation) return;
    const operation = operationKey(owner, 'withdraw', status.pendingChange.changeId);
    try {
      const result = await withdrawChange.mutateAsync({ data: { changeId: status.pendingChange.changeId, idempotencyKey: operation.value } });
      if ((result as { state?: unknown }).state !== 'withdrawn') {
        setBlockedOperation(operation.storageKey);
        sessionStorage.setItem(`${operation.storageKey}:uncertain`, '1');
        toast({
          title: 'Withdrawal is still being checked',
          description: 'The server did not confirm withdrawal. Do not submit it again until billing status is authoritative.',
          variant: 'destructive',
        });
        await invalidate();
        return;
      }
      await invalidate();
      toast({ title: 'Scheduled change withdrawn' });
    } catch (error) {
      handleFailure(error, operation.storageKey);
      await invalidate();
    }
  };

  const busy = checkout.isPending || portal.isPending || previewChange.isPending || confirmChange.isPending || withdrawChange.isPending;

  if (auth.isLoading) return (
    <main className="min-h-[100dvh] flex items-center justify-center bg-background" role="status">
      <div className="flex flex-col items-center gap-4 text-muted-foreground">
        <Loader2 className="size-8 animate-spin text-primary" />
        <span className="font-medium text-sm">Checking account...</span>
      </div>
    </main>
  );

  if (!auth.isAuthenticated) return (
    <div className="min-h-[100dvh] flex flex-col bg-background">
      <AuthHeader />
      <main className="flex-1 flex items-center justify-center p-6">
        <Card className="w-full max-w-md shadow-sm border-border/60">
          <CardHeader className="text-center pb-2">
            <div className="mx-auto bg-primary/10 w-12 h-12 rounded-full flex items-center justify-center mb-4">
              <ShieldCheck className="size-6 text-primary" />
            </div>
            <CardTitle className="text-2xl font-semibold tracking-tight">Sign in to manage billing</CardTitle>
            <CardDescription className="text-base mt-2">
              Billing details are private to your SceneIt account.
            </CardDescription>
          </CardHeader>
          <CardContent className="pt-6">
            <Button size="lg" className="w-full font-medium" onClick={auth.login} data-testid="button-sign-in-billing">
              Sign in with Replit
            </Button>
          </CardContent>
        </Card>
      </main>
    </div>
  );

  return (
    <div className="min-h-[100dvh] bg-background flex flex-col">
      <AuthHeader />
      <main className="mx-auto w-full max-w-5xl space-y-10 p-4 py-8 md:py-12 md:p-8 flex-1">
        
        <section className="flex flex-col justify-between gap-6 border-b border-border/60 pb-8 md:flex-row md:items-end">
          <div className="space-y-3">
            <Badge variant="secondary" className="bg-secondary text-secondary-foreground font-medium rounded-full px-3 py-1 text-xs">
              <ShieldCheck className="mr-1.5 size-3.5 text-primary" /> Secure hosted billing
            </Badge>
            <h1 className="text-3xl font-semibold tracking-tight md:text-4xl text-foreground">Membership & billing</h1>
            <p className="max-w-2xl text-base text-muted-foreground leading-relaxed">
              See verified access and usage here. Card details, receipts, and cancellation confirmation stay with Stripe.
            </p>
          </div>
          {status?.managementEligible && (
            <Button size="lg" variant="outline" className="font-medium shrink-0 shadow-sm" onClick={() => startPortal('manage')} disabled={busy} data-testid="button-manage-billing">
              <CreditCard className="mr-2.5 size-4 text-muted-foreground" /> Manage card & invoices <ExternalLink className="ml-2 size-3.5 text-muted-foreground" />
            </Button>
          )}
        </section>

        {statusQuery.isLoading && (
          <Card className="shadow-sm border-border/60" data-testid="status-billing-loading">
            <CardContent className="flex items-center gap-4 p-8 text-muted-foreground font-medium">
              <Loader2 className="size-5 animate-spin text-primary" /> Loading verified billing status…
            </CardContent>
          </Card>
        )}
        
        {statusQuery.error && (
          <Alert variant="destructive" className="border-destructive/30 bg-destructive/5 text-destructive" data-testid="status-billing-error">
            <AlertCircle className="size-5" />
            <AlertTitle className="text-base font-semibold">Billing status unavailable</AlertTitle>
            <AlertDescription className="mt-2 text-sm leading-relaxed text-destructive/90">
              {apiMessage(statusQuery.error)} 
              <Button className="mt-4 block font-medium" variant="destructive" onClick={() => statusQuery.refetch()} data-testid="button-retry-billing">
                Try again
              </Button>
            </AlertDescription>
          </Alert>
        )}

        {status && <>
          {status.paymentProblem && (
            <Alert variant="destructive" className="border-destructive/30 bg-destructive/5 text-destructive" data-testid="status-payment-problem">
              <AlertCircle className="size-5" />
              <AlertTitle className="text-base font-semibold">
                {status.paymentProblem.code === 'expired_card' ? 'Your card has expired' : status.paymentProblem.code === 'payment_authentication_required' ? 'Payment authentication required' : 'Payment needs attention'}
              </AlertTitle>
              <AlertDescription className="mt-2 text-sm leading-relaxed text-destructive/90">
                Already-paid access remains available only through <span className="font-semibold">{dateTime(status.paidThrough)}</span>. Updating a card or returning from Stripe does not itself confirm payment.
                <Button className="mt-4 block font-medium" variant="destructive" onClick={() => startPortal('manage')} disabled={busy} data-testid="button-recover-payment">
                  Open secure payment recovery
                </Button>
              </AlertDescription>
            </Alert>
          )}
          
          {!status.enabled && (
            <Alert className="border-border/60 shadow-sm" data-testid="status-billing-disabled">
              <ShieldCheck className="size-5 text-primary" />
              <AlertTitle className="text-base font-semibold text-foreground">Paid memberships are not available</AlertTitle>
              <AlertDescription className="mt-1 text-sm leading-relaxed text-muted-foreground">
                No reviewed offers are configured for purchase. Your existing private account access is unchanged.
              </AlertDescription>
            </Alert>
          )}
          
          {blockedOperation && (
            <Alert variant="destructive" className="border-destructive/30 bg-destructive/5 text-destructive" data-testid="status-operation-uncertain">
              <AlertCircle className="size-5" />
              <AlertTitle className="text-base font-semibold">Purchase blocked while outcome is checked</AlertTitle>
              <AlertDescription className="mt-2 text-sm leading-relaxed text-destructive/90">
                SceneIt will not issue another financial operation. Refresh later; support can reconcile the saved operation safely.
              </AlertDescription>
            </Alert>
          )}

          <div className="grid gap-6 lg:grid-cols-[1.2fr_1fr]">
            <Card className="flex flex-col h-full shadow-sm border-border/60" data-testid="card-current-membership">
              <CardHeader className="pb-4">
                <CardDescription className="text-xs uppercase tracking-wider font-semibold">Verified membership</CardDescription>
                <CardTitle className="text-3xl font-semibold tracking-tight" data-testid="text-membership-tier">
                  {status.effectiveTier || (status.membership === 'disabled' ? 'Billing unavailable' : 'No paid tier')}
                </CardTitle>
              </CardHeader>
              <CardContent className="grid gap-6 sm:grid-cols-2 flex-1">
                <div className="space-y-1.5">
                  <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Status</p>
                  <div className="flex items-center gap-2">
                    <div className={cn("size-2 rounded-full", status.membership === 'active' ? "bg-green-500" : "bg-muted-foreground")} />
                    <p className="font-medium capitalize text-sm text-foreground" data-testid="text-membership-status">{status.membership}</p>
                  </div>
                </div>
                <div className="space-y-1.5">
                  <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Cadence</p>
                  <p className="font-medium capitalize text-sm text-foreground" data-testid="text-membership-cadence">{status.cadence || '—'}</p>
                </div>
                <div className="space-y-1.5">
                  <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Paid through</p>
                  <p className="font-medium text-sm text-foreground" data-testid="text-paid-through">{dateTime(status.paidThrough)}</p>
                </div>
                <div className="space-y-1.5">
                  <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Renewal</p>
                  <p className="font-medium text-sm text-foreground" data-testid="text-cancellation-state">{status.cancelAtPeriodEnd ? 'Cancels at period end' : 'Continues until changed'}</p>
                </div>
              </CardContent>
              {status.managementEligible && !status.cancelAtPeriodEnd && (
                <CardFooter className="pt-4 border-t border-border/40 mt-auto">
                  <Button variant="ghost" size="sm" className="w-full sm:w-auto text-muted-foreground hover:text-foreground font-medium" onClick={() => startPortal('cancel')} disabled={busy} data-testid="button-cancel-membership">
                    <CalendarClock className="mr-2 size-4" /> Cancel at period end in Stripe
                  </Button>
                </CardFooter>
              )}
            </Card>
            
            <Card className="flex flex-col h-full shadow-sm border-border/60" data-testid="card-usage">
              <CardHeader className="pb-4">
                <CardDescription className="text-xs uppercase tracking-wider font-semibold">Current allowance window</CardDescription>
                <CardTitle className="text-3xl font-semibold tracking-tight">Monthly usage</CardTitle>
              </CardHeader>
              <CardContent className="space-y-6 flex-1">
                {status.usage ? <>
                  <Metric label="Imports" used={status.usage.metrics.imports.used} limit={status.usage.metrics.imports.limit} />
                  <Metric label="Searches" used={status.usage.metrics.searches.used} limit={status.usage.metrics.searches.limit} />
                  <Metric label="Storage" used={status.usage.storage.used} limit={status.usage.storage.limit} />
                </> : <div className="h-full flex flex-col items-center justify-center text-center space-y-3 py-6">
                  <ShieldAlert className="size-8 text-muted-foreground/30" />
                  <p className="text-sm text-muted-foreground">No paid usage window is active.</p>
                </div>}
              </CardContent>
              {status.usage && (
                <CardFooter className="pt-4 border-t border-border/40 mt-auto">
                  <p className="text-xs text-muted-foreground">Window ends <span className="font-medium text-foreground">{dateTime(status.usage.windowEnd)}</span>. Tier changes do not reset usage.</p>
                </CardFooter>
              )}
            </Card>
          </div>

          {status.pendingChange && (
            <Card className="border-primary/40 bg-primary/5 shadow-sm" data-testid="card-pending-change">
              <CardContent className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-6 p-6">
                <div>
                  <div className="flex items-center gap-3 mb-2">
                    <Badge variant="outline" className="border-primary/30 text-primary bg-primary/10">Pending change</Badge>
                    <h3 className="text-lg font-semibold">{status.pendingChange.tier} &middot; {status.pendingChange.cadence} &middot; {status.pendingChange.currency.toUpperCase()}</h3>
                  </div>
                  <p className="text-sm text-muted-foreground leading-relaxed max-w-xl">
                    <span data-testid="text-pending-change-state">{pendingChangeMessage(status.pendingChange.state, status.pendingChange.effectiveAt)}</span>
                  </p>
                </div>
                {status.pendingChange.state === 'scheduled' && (
                  <Button variant="outline" className="shrink-0 font-medium" onClick={withdrawPending} disabled={busy || Boolean(blockedOperation)} data-testid="button-withdraw-change">
                    Withdraw scheduled change
                  </Button>
                )}
              </CardContent>
            </Card>
          )}

          {offers.length > 0 && (
            <section aria-labelledby="offers-heading" className="pt-4">
              <div className="mb-6">
                <h2 id="offers-heading" className="text-2xl font-semibold tracking-tight">Reviewed membership offers</h2>
                <p className="mt-2 text-base text-muted-foreground">
                  {status.currency ? `Your subscription remains in ${status.currency.toUpperCase()}.` : 'Choose a reviewed tier, cadence, and currency.'} Taxes are calculated in the secure hosted flow.
                </p>
              </div>
              <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
                {offers.map(offer => {
                  const current = offer.tier === status.effectiveTier && offer.cadence === status.cadence;
                  return (
                    <Card 
                      key={`${offer.tier}:${offer.cadence}:${offer.currency}`} 
                      className={cn(
                        "flex flex-col transition-all duration-200", 
                        current ? 'border-primary shadow-sm relative overflow-hidden' : 'hover:border-primary/50 shadow-sm border-border/60'
                      )} 
                      data-testid={`card-offer-${offer.tier}-${offer.cadence}-${offer.currency}`}
                    >
                      {current && <div className="absolute top-0 inset-x-0 h-1 bg-primary" />}
                      <CardHeader className="pb-4">
                        <div className="flex items-start justify-between gap-3">
                          <div>
                            <CardTitle className="text-xl">{offer.name}</CardTitle>
                            <CardDescription className="mt-1.5 font-medium capitalize text-muted-foreground">
                              {offer.cadence} &middot; {offer.taxBehavior} tax
                            </CardDescription>
                          </div>
                          {current && (
                            <Badge variant="secondary" className="bg-primary/10 text-primary border-primary/20 hover:bg-primary/15 shrink-0">
                              <Check className="mr-1 size-3" /> Current
                            </Badge>
                          )}
                        </div>
                      </CardHeader>
                      <CardContent className="flex-1 flex flex-col pt-0">
                        <div className="mb-6">
                          <p className="text-4xl font-bold tracking-tight text-foreground" data-testid={`text-price-${offer.tier}-${offer.cadence}`}>
                            {money(offer.unitAmount, offer.currency)}
                            <span className="text-base font-medium text-muted-foreground"> / {offer.cadence === 'monthly' ? 'mo' : 'yr'}</span>
                          </p>
                        </div>
                        
                        <ul className="mb-8 space-y-3 text-sm text-muted-foreground flex-1">
                          {offer.capabilities.map(capability => (
                            <li key={capability} className="flex gap-3 items-start">
                              <Check className="mt-0.5 size-4 text-primary shrink-0" />
                              <span className="leading-snug">{capability.replaceAll('_', ' ')}</span>
                            </li>
                          ))}
                        </ul>
                        
                        <Button 
                          className="w-full font-medium mt-auto" 
                          size="lg"
                          variant={current ? 'secondary' : 'default'} 
                          disabled={current || busy || !!blockedOperation || !!status.pendingChange}
                          onClick={() => chooseOffer(offer)} 
                          data-testid={`button-select-${offer.tier}-${offer.cadence}-${offer.currency}`}
                        >
                          {current ? 'Current membership' : status.membership === 'active' ? 'Preview change' : 'Continue securely'}
                        </Button>
                      </CardContent>
                    </Card>
                  );
                })}
              </div>
            </section>
          )}
        </>}
      </main>

      <Dialog open={!!preview} onOpenChange={open => { if (!open && !confirmChange.isPending) setPreview(null); }}>
        <DialogContent className="sm:max-w-md" data-testid="dialog-change-preview">
          <DialogHeader>
            <DialogTitle className="text-xl">
              {preview?.kind === 'upgrade' ? 'Confirm paid upgrade' : 'Schedule membership change'}
            </DialogTitle>
            <DialogDescription className="text-base mt-2">
              {preview?.kind === 'upgrade' 
                ? 'The higher tier begins only after Stripe verifies payment.' 
                : `This change is scheduled for ${dateTime(preview?.effectiveAt)}.`}
            </DialogDescription>
          </DialogHeader>
          
          {preview && (
            <div className="my-4 space-y-4 rounded-xl border border-border/50 bg-secondary/30 p-5 text-sm">
              <div className="flex justify-between items-center text-muted-foreground">
                <span>Subtotal</span>
                <span className="font-medium text-foreground">{money(preview.subtotal, preview.currency)}</span>
              </div>
              <div className="flex justify-between items-center text-muted-foreground">
                <span>Tax</span>
                <span className="font-medium text-foreground">{money(preview.tax, preview.currency)}</span>
              </div>
              <div className="flex justify-between items-center border-t border-border/60 pt-4 text-base font-semibold text-foreground" data-testid="text-preview-total">
                <span>Total</span>
                <span>{money(preview.total, preview.currency)}</span>
              </div>
              <p className="text-xs text-muted-foreground mt-4 pt-4 border-t border-border/40">
                Preview expires {dateTime(preview.expiresAt)}.
              </p>
            </div>
          )}
          
          <DialogFooter className="gap-2 sm:gap-0 mt-2">
            <Button variant="outline" onClick={() => setPreview(null)} disabled={confirmChange.isPending} data-testid="button-close-preview">
              Not now
            </Button>
            <Button onClick={confirmPreview} disabled={confirmChange.isPending || !!blockedOperation} data-testid="button-confirm-change">
              {confirmChange.isPending ? <Loader2 className="mr-2 size-4 animate-spin" /> : <ReceiptText className="mr-2 size-4" />}
              Confirm change
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
