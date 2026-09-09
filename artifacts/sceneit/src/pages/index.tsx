import { useState, useRef, useEffect } from 'react';
import { Link, useLocation } from 'wouter';
import { clearPendingImportLink, readPendingImportLink, savePendingImportLink, useAuth } from '@workspace/replit-auth-web';
import {
  useGetImportConfig,
  useGetCurrentImport,
  useCreateImport
} from '@workspace/api-client-react';
import { AuthHeader } from '@/components/auth-header';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardHeader, CardTitle, CardDescription, CardFooter } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { AlertCircle, Link as LinkIcon, Loader2, UploadCloud, AlertTriangle, FileVideo } from 'lucide-react';
import { useToast } from '@/hooks/use-toast';
import { parseSourceKind } from '@/lib/source-utils';
import { useQueryClient } from '@tanstack/react-query';
import { importError } from '@/lib/import-errors';
import { cn } from '@/lib/utils';
import { TrialAllowance } from '@/components/trial-allowance';
import { canReadPrivate, privateAccessMessage } from '@/lib/private-access';

export default function ImportsIndex() {
  const auth = useAuth();
  // Resolve identity (and clear the previous owner's drafts) before restoring
  // entry text. Otherwise an initial anonymous render can relabel private state.
  if (auth.isLoading) {
    return <main className="min-h-screen grid place-items-center p-6 bg-background"><Loader2 aria-label="Loading sign-in status" className="animate-spin text-primary size-8" /></main>;
  }
  const identity = auth.error ? 'unavailable' : auth.signoutUnconfirmed ? 'signout-unconfirmed' : auth.user?.id || 'anonymous';
  return <ImportEntry key={`${identity}:${auth.csrfToken || ''}`} />;
}

function ImportEntry() {
  const [, setLocation] = useLocation();
  const { toast } = useToast();
  const auth = useAuth();
  const { isAuthenticated, csrfToken } = auth;
  const privateReadable = canReadPrivate(auth);
  const queryClient = useQueryClient();

  const { data: config } = useGetImportConfig({
    query: { queryKey: ['/api/imports/config'], enabled: privateReadable }
  });
  const lifetime = auth.usage?.lifetime ?? (auth.user?.provider === 'firebase' || config?.quotaMode !== 'monthly');
  const { data: currentImport } = useGetCurrentImport({
    query: {
      queryKey: ['/api/imports/current'],
      enabled: privateReadable,
    }
  });

  const createImport = useCreateImport({
    request: { headers: { 'X-CSRF-Token': csrfToken || '' } },
    mutation: { retry: false },
  });

  const [pendingLink] = useState(() =>
    auth.error || auth.signoutUnconfirmed ? '' : readPendingImportLink());
  const [activeTab, setActiveTab] = useState<'upload' | 'link'>(pendingLink ? 'link' : 'upload');
  const [linkUrl, setLinkUrl] = useState(pendingLink);
  const [activeSourceKind, setActiveSourceKind] = useState<'youtube' | 'x' | 'tiktok' | 'vimeo' | null>(() => {
    const restored = parseSourceKind(pendingLink).kind;
    return restored === 'file' ? null : restored;
  });

  const [rightsAuthorized, setRightsAuthorized] = useState(false);
  const [playbackAuthorized, setPlaybackAuthorized] = useState(false);

  const [isProcessing, setIsProcessing] = useState(false);
  const [idempotencyKey] = useState(() => crypto.randomUUID());

  useEffect(() => {
    // Do not grant an anonymous exemption while identity is uncertain.
    if (auth.error || auth.signoutUnconfirmed) return;
    savePendingImportLink(activeTab === 'link' ? linkUrl : '', !isAuthenticated);
  }, [activeTab, linkUrl, isAuthenticated, auth.error, auth.signoutUnconfirmed]);

  const handleLinkChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value;
    setLinkUrl(val);
    if (!val) {
      setActiveSourceKind(null);
      return;
    }
    const { kind } = parseSourceKind(val);
    if (kind !== 'file') {
      setActiveSourceKind(kind as any);
    } else {
      setActiveSourceKind(null);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!rightsAuthorized) {
      toast({ title: 'Authorization Required', description: 'You must check the rights analysis checkbox.', variant: 'destructive' });
      return;
    }

    if (!isAuthenticated) {
      if (activeTab === 'link' && linkUrl) {
        savePendingImportLink(linkUrl, true);
      }
      setLocation('/auth?returnTo=%2F');
      return;
    }
    if (auth.error || !auth.privateAccess.allowed || !auth.usage
      || auth.usage.importsRemaining <= 0 || !config?.workerAvailable) return;

    if (activeTab === 'link') {
      if (!linkUrl) return;
      const { kind, error } = parseSourceKind(linkUrl);

      if (error) {
        toast({ title: 'Unsupported Source', description: error, variant: 'destructive' });
        return;
      }

      try {
        setIsProcessing(true);
        const res = await createImport.mutateAsync({
          data: {
            entryMethod: 'link',
            sourceUrl: linkUrl,
            analysisAuthorized: true,
            playbackAuthorized,
            idempotencyKey
          }
        });

        clearPendingImportLink();
        queryClient.invalidateQueries({ queryKey: ['/api/imports/current'] });
        queryClient.invalidateQueries({ queryKey: ['/api/auth/session'] });
        setLocation(`/imports/${res.id}`);
      } catch (err: any) {
        toast({ title: 'Import Failed', description: importError(err), variant: 'destructive' });
        queryClient.invalidateQueries({ queryKey: ['/api/imports/current'] });
        queryClient.invalidateQueries({ queryKey: ['/api/auth/session'] });
        setIsProcessing(false);
      }
    } else {
      // Create import first, then show upload panel
      try {
        setIsProcessing(true);
        const res = await createImport.mutateAsync({
          data: {
            entryMethod: 'upload',
            analysisAuthorized: true,
            playbackAuthorized,
            idempotencyKey
          }
        });
        queryClient.invalidateQueries({ queryKey: ['/api/imports/current'] });
        queryClient.invalidateQueries({ queryKey: ['/api/auth/session'] });
        setLocation(`/imports/${res.id}`);
      } catch (err: any) {
        toast({ title: 'Initialization Failed', description: importError(err), variant: 'destructive' });
        queryClient.invalidateQueries({ queryKey: ['/api/imports/current'] });
        queryClient.invalidateQueries({ queryKey: ['/api/auth/session'] });
        setIsProcessing(false);
      }
    }
  };

  return (
    <div className="min-h-[100dvh] flex flex-col bg-background">
      <AuthHeader />
      <main className="flex-1 p-4 md:p-8 max-w-4xl mx-auto w-full flex flex-col gap-10">

        {config && !config.workerAvailable && (
          <div className="bg-yellow-500/10 border border-yellow-600/30 p-4 rounded-md flex items-start gap-3 mt-4">
            <AlertTriangle className="size-5 text-yellow-500 shrink-0 mt-0.5" />
            <div className="text-sm text-yellow-500 leading-relaxed">
              <strong className="block mb-1 font-semibold text-yellow-500/90">Index Worker Unavailable</strong>
              The processing worker is unavailable. New imports are paused here until it returns. This is separate from your remaining account allowance.
            </div>
          </div>
        )}

        <div className="text-center space-y-4 mt-8 mb-4">
          <h2 className="text-3xl md:text-5xl font-semibold tracking-tight">
            Find scenes in your videos.
          </h2>
          <p className="text-muted-foreground text-lg md:text-xl max-w-2xl mx-auto">
            Upload an MP4 or paste a link, then describe the moment you want to find.
          </p>
        </div>

        <TrialAllowance />
        {(auth.error || !auth.privateAccess.allowed) && <section className="rounded-xl border border-border bg-card p-5 space-y-3" role="status">
          <p className="text-sm leading-6">{auth.error ? 'Session check unavailable. Processing is closed until your account can be verified.'
            : privateAccessMessage(!isAuthenticated && auth.capabilities.unavailableReason ? auth.capabilities.unavailableReason : auth.privateAccess.reason)}</p>
          <div className="flex flex-wrap gap-3">
            {!isAuthenticated && <Button asChild><Link href="/auth">Create account or sign in</Link></Button>}
            {auth.user?.provider === 'firebase' && <Button asChild variant="outline"><Link href="/auth?mode=signin">Verify email or sign in again</Link></Button>}
            {auth.error && <Button variant="outline" onClick={() => queryClient.invalidateQueries({ queryKey: ['/api/auth/session'] })}>Retry session check</Button>}
          </div>
          <p className="text-xs text-muted-foreground">A small lifetime trial. No payment required. Signing in never starts processing automatically.</p>
        </section>}

        {privateReadable && currentImport && currentImport.state !== 'failed' && currentImport.state !== 'cancelled' && currentImport.state !== 'expired' && (
          <Card className="border-primary/30 bg-primary/5 shadow-sm">
            <CardContent className="p-5 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
              <div>
                <h3 className="font-semibold text-primary mb-1">Resume current video</h3>
                <p className="text-sm text-muted-foreground">You have a video in progress or ready for review.</p>
              </div>
              <Button onClick={() => setLocation(`/imports/${currentImport.id}`)} className="h-11 px-6 font-medium w-full sm:w-auto">
                Resume session
              </Button>
            </CardContent>
          </Card>
        )}

        <Card className="shadow-md border-border/60 overflow-hidden bg-card">
          <div className="grid grid-cols-2 border-b border-border/60">
            <button
              onClick={() => setActiveTab('upload')}
              aria-pressed={activeTab === 'upload'}
              className={cn(
                "py-4 px-2 flex items-center justify-center gap-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:bg-secondary min-h-[52px]",
                activeTab === 'upload'
                  ? "bg-background text-foreground shadow-[inset_0_-2px_0_hsl(var(--primary))]"
                  : "bg-secondary/30 text-muted-foreground hover:bg-secondary/60 hover:text-foreground"
              )}
            >
              <UploadCloud className="size-4" /> Upload MP4
            </button>
            <button
              onClick={() => setActiveTab('link')}
              aria-pressed={activeTab === 'link'}
              className={cn(
                "py-4 px-2 flex items-center justify-center gap-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:bg-secondary min-h-[52px]",
                activeTab === 'link'
                  ? "bg-background text-foreground shadow-[inset_0_-2px_0_hsl(var(--primary))]"
                  : "bg-secondary/30 text-muted-foreground hover:bg-secondary/60 hover:text-foreground border-l border-border/60"
              )}
            >
              <LinkIcon className="size-4" /> Paste a Link
            </button>
          </div>

          <CardContent className="p-6 md:p-8">
            <form id="import-form" onSubmit={handleSubmit} className="flex flex-col gap-8">

              <div className={cn("transition-opacity", activeTab === 'upload' ? 'block' : 'hidden')}>
                <div className="bg-secondary/30 p-6 sm:p-8 rounded-lg text-center border border-dashed border-border/80 flex flex-col items-center justify-center gap-4">
                  <div className="p-4 bg-background rounded-full shadow-sm border border-border">
                    <FileVideo className="size-8 text-primary" />
                  </div>
                  <div>
                    <p className="text-base font-semibold">Your MP4, no link required</p>
                    <p className="text-sm text-muted-foreground mt-2 max-w-md mx-auto leading-relaxed">
                      Confirm your rights, then choose or drop an MP4 on the next screen.
                      Up to 200 MB, H.264 video with AAC audio or silent.
                      You’ll reselect the file if you sign in or reload.
                    </p>
                  </div>
                </div>
              </div>

              <div className={cn("flex flex-col gap-6", activeTab === 'link' ? 'block' : 'hidden')}>
                <div className="flex flex-col gap-2.5">
                  <Label htmlFor="linkUrl" className="text-sm font-semibold">Video URL</Label>
                  <Input
                    id="linkUrl"
                    value={linkUrl}
                    onChange={handleLinkChange}
                    placeholder="https://youtube.com/... or https://x.com/..."
                    className="h-12 px-4 text-base bg-background shadow-inner focus-visible:ring-primary"
                    disabled={isProcessing}
                  />
                </div>

                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                  {[
                    { id: 'youtube', label: 'YouTube' },
                    { id: 'x', label: 'X (Twitter)' },
                    { id: 'tiktok', label: 'TikTok' },
                    { id: 'vimeo', label: 'Vimeo' }
                  ].map(platform => (
                    <button
                      key={platform.id}
                      type="button"
                      aria-pressed={activeSourceKind === platform.id}
                      onClick={() => { setLinkUrl(''); setActiveSourceKind(platform.id as any); }}
                      className={cn(
                        "p-3 min-h-[44px] rounded-md border text-center transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                        activeSourceKind === platform.id
                          ? "border-primary bg-primary/5 text-primary shadow-sm"
                          : "border-border/60 bg-secondary/20 hover:border-primary/40 hover:bg-secondary/50 text-foreground"
                      )}
                    >
                      <span className="text-sm font-medium">{platform.label}</span>
                    </button>
                  ))}
                </div>

                <div className="p-4 bg-secondary/30 rounded-md border border-border/60 text-sm text-muted-foreground leading-relaxed">
                  <strong className="block font-semibold text-foreground mb-1">Link import availability</strong>
                  YouTube links require an authorized MP4 for analysis. X (Twitter), TikTok, and Vimeo can be imported only when an authorized MP4 is safely retrievable.
                  Otherwise, upload an authorized file without losing the link. No login-wall, DRM, or restriction bypass.
                </div>

                {activeSourceKind === 'youtube' && (
                  <div className="p-4 bg-yellow-500/10 border border-yellow-600/30 rounded-md flex items-start gap-3">
                    <AlertTriangle className="size-5 text-yellow-500 shrink-0 mt-0.5" />
                    <div className="text-sm text-yellow-500 leading-relaxed">
                      <strong className="font-semibold text-yellow-500/90 block mb-1">YouTube requires an authorized MP4</strong>
                      The link supplies source and official playback context only. Upload an authorized MP4 on the next screen for analysis. We do not download YouTube videos.
                    </div>
                  </div>
                )}

                {linkUrl && !activeSourceKind && (
                  <div className="p-4 bg-destructive/10 border border-destructive/30 rounded-md flex items-start gap-3">
                    <AlertCircle className="size-5 text-destructive shrink-0 mt-0.5" />
                    <div className="text-sm text-destructive leading-relaxed">
                      <strong className="font-semibold block mb-1">Unsupported Source</strong>
                      The provided URL does not match a supported platform (YouTube, X, TikTok, Vimeo).
                    </div>
                  </div>
                )}
              </div>

              <div className="flex flex-col gap-5 bg-secondary/30 p-5 md:p-6 border border-border/60 rounded-lg">
                <div className="flex items-start space-x-3">
                  <Checkbox
                    id="rights"
                    checked={rightsAuthorized}
                    onCheckedChange={(c) => setRightsAuthorized(!!c)}
                    disabled={isProcessing}
                    className="mt-1"
                  />
                  <div className="space-y-1.5 leading-none">
                    <Label htmlFor="rights" className="text-sm font-semibold cursor-pointer text-foreground block">
                      I confirm I have the right to process this video.
                    </Label>
                    <p className="text-xs text-muted-foreground leading-relaxed">
                      Required for private analysis. Access expires after {config?.retentionDays || 7} days.
                      Uncertain provider cleanup may require operator review beyond that deadline.
                    </p>
                  </div>
                </div>

                <div className="flex items-start space-x-3">
                  <Checkbox
                    id="playback"
                    checked={playbackAuthorized}
                    onCheckedChange={(c) => setPlaybackAuthorized(!!c)}
                    disabled={isProcessing}
                    className="mt-1"
                  />
                  <div className="space-y-1.5 leading-none">
                    <Label htmlFor="playback" className="text-sm font-semibold cursor-pointer text-foreground block">
                      Allow owner-only source playback
                    </Label>
                    <p className="text-xs text-muted-foreground leading-relaxed">
                      If unchecked, only extracted frames and timestamps will be shown. You can authorize or revoke this later.
                    </p>
                  </div>
                </div>
              </div>

            </form>
          </CardContent>
          <CardFooter className="p-6 md:p-8 pt-0">
            <Button
              type="submit"
              form="import-form"
              className="w-full h-12 text-base font-semibold shadow-sm"
              disabled={isProcessing || auth.isLoading || !!auth.error || (isAuthenticated && (!auth.privateAccess.allowed || !auth.usage || auth.usage.importsRemaining <= 0 || !config?.workerAvailable)) || (activeTab === 'link' && !linkUrl) || (activeTab === 'link' && linkUrl && !activeSourceKind) || !rightsAuthorized}
            >
              {isProcessing ? (
                <><Loader2 className="size-5 animate-spin mr-2" /> Processing...</>
              ) : !isAuthenticated ? 'Sign in to continue' : activeTab === 'upload' ? (
                <>Continue to upload</>
              ) : (
                <>Start processing</>
              )}
            </Button>
          </CardFooter>
        </Card>

        {config && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-center">
            <div className="p-4 border border-border/60 bg-card rounded-lg shadow-sm">
              <div className="text-2xl font-bold text-foreground">{auth.usage?.importLimit ?? config.ownerImportLimit}</div>
              <div className="text-xs text-muted-foreground font-medium mt-1">{lifetime ? 'Lifetime' : 'Current Window'} Attempts</div>
            </div>
            <div className="p-4 border border-border/60 bg-card rounded-lg shadow-sm">
              <div className="text-2xl font-bold text-foreground">{auth.usage?.searchLimit ?? config.ownerSearchLimit}</div>
              <div className="text-xs text-muted-foreground font-medium mt-1">{lifetime ? 'Lifetime' : 'Current Window'} Searches</div>
            </div>
            <div className="p-4 border border-border/60 bg-card rounded-lg shadow-sm">
              <div className="text-2xl font-bold text-foreground">{config.retentionDays}</div>
              <div className="text-xs text-muted-foreground font-medium mt-1">Days Retention</div>
            </div>
            <div className="p-4 border border-border/60 bg-card rounded-lg shadow-sm">
              <div className="text-xl font-bold text-foreground mt-1">
                {config.minDurationSeconds}s - {Math.round(config.maxDurationSeconds / 60)}m
              </div>
              <div className="text-xs text-muted-foreground font-medium mt-1">Video Length</div>
            </div>
          </div>
        )}
        {config && (
          <p className="text-xs text-muted-foreground text-center max-w-2xl mx-auto leading-relaxed">
            {lifetime ? 'Lifetime account allowances include failed and cancelled reserved attempts. No monthly refresh and no payment required.'
              : 'Current subscription allowances include failed and cancelled reserved attempts. Deletion does not restore consumed usage.'}{' '}
            App-wide limits: {config.appImportLimit} import attempts and {config.appSearchLimit} searches.
            These are operation allowances, not a guaranteed dollar spending ceiling.
          </p>
        )}

      </main>
    </div>
  );
}
