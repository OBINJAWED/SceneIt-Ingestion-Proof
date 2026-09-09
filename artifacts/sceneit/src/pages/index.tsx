import { useState, useRef, useEffect } from 'react';
import { useLocation } from 'wouter';
import { useAuth } from '@workspace/replit-auth-web';
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

export default function ImportsIndex() {
  const [, setLocation] = useLocation();
  const { toast } = useToast();
  const { isAuthenticated, login, csrfToken } = useAuth();
  const queryClient = useQueryClient();

  const { data: config } = useGetImportConfig({
    query: { queryKey: ['/api/imports/config'] }
  });
  const { data: currentImport } = useGetCurrentImport({
    query: {
      queryKey: ['/api/imports/current'],
      enabled: isAuthenticated,
    }
  });

  const createImport = useCreateImport({
    request: { headers: { 'X-CSRF-Token': csrfToken || '' } },
    mutation: { retry: false },
  });

  const [activeTab, setActiveTab] = useState<'upload' | 'link'>('upload');
  const [linkUrl, setLinkUrl] = useState('');
  const [activeSourceKind, setActiveSourceKind] = useState<'youtube' | 'x' | 'tiktok' | 'vimeo' | null>(null);

  const [rightsAuthorized, setRightsAuthorized] = useState(false);
  const [playbackAuthorized, setPlaybackAuthorized] = useState(false);

  const [isProcessing, setIsProcessing] = useState(false);
  const [idempotencyKey] = useState(() => crypto.randomUUID());

  useEffect(() => {
    const pendingLink = sessionStorage.getItem('pendingImportLink');
    if (pendingLink) {
      setActiveTab('link');
      setLinkUrl(pendingLink);
      const restored = parseSourceKind(pendingLink).kind;
      setActiveSourceKind(restored === 'file' ? null : restored);
    }
  }, []);

  useEffect(() => {
    if (linkUrl && activeTab === 'link') sessionStorage.setItem('pendingImportLink', linkUrl);
    else sessionStorage.removeItem('pendingImportLink');
  }, [activeTab, linkUrl]);

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
        sessionStorage.setItem('pendingImportLink', linkUrl);
      }
      login();
      return;
    }

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

        sessionStorage.removeItem('pendingImportLink');
        queryClient.invalidateQueries({ queryKey: ['/api/imports/current'] });
        setLocation(`/imports/${res.id}`);
      } catch (err: any) {
        toast({ title: 'Import Failed', description: importError(err), variant: 'destructive' });
        queryClient.invalidateQueries({ queryKey: ['/api/imports/current'] });
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
        setLocation(`/imports/${res.id}`);
      } catch (err: any) {
        toast({ title: 'Initialization Failed', description: importError(err), variant: 'destructive' });
        queryClient.invalidateQueries({ queryKey: ['/api/imports/current'] });
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
              Our indexing servers are currently offline or at capacity. You can still initiate imports, but they will remain in a 'queued' state until capacity frees up.
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

        {currentImport && currentImport.state !== 'failed' && currentImport.state !== 'cancelled' && currentImport.state !== 'expired' && (
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
              disabled={isProcessing || (activeTab === 'link' && !linkUrl) || (activeTab === 'link' && linkUrl && !activeSourceKind) || !rightsAuthorized}
            >
              {isProcessing ? (
                <><Loader2 className="size-5 animate-spin mr-2" /> Processing...</>
              ) : activeTab === 'upload' ? (
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
              <div className="text-2xl font-bold text-foreground">{config.ownerImportLimit}</div>
              <div className="text-xs text-muted-foreground font-medium mt-1">Lifetime Attempts</div>
            </div>
            <div className="p-4 border border-border/60 bg-card rounded-lg shadow-sm">
              <div className="text-2xl font-bold text-foreground">{config.ownerSearchLimit}</div>
              <div className="text-xs text-muted-foreground font-medium mt-1">Lifetime Searches</div>
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
            Account allowances include failed and cancelled attempts and never reset when changing videos.
            Pilot-wide limits: {config.appImportLimit} import attempts and {config.appSearchLimit} searches.
          </p>
        )}

      </main>
    </div>
  );
}
