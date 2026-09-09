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
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { AlertCircle, Link as LinkIcon, Loader2, UploadCloud, AlertTriangle } from 'lucide-react';
import { useToast } from '@/hooks/use-toast';
import { parseSourceKind } from '@/lib/source-utils';
import { UploadPanel } from '@/components/upload-panel';
import { useQueryClient } from '@tanstack/react-query';
import { importError } from '@/lib/import-errors';

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
    request: { headers: { 'X-CSRF-Token': csrfToken || '' } }
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
    <div className="min-h-screen flex flex-col bg-background">
      <AuthHeader />
      <main className="flex-1 p-4 md:p-8 max-w-4xl mx-auto w-full flex flex-col gap-8">
        
        {config && !config.workerAvailable && (
          <div className="bg-yellow-500/10 border border-yellow-500/50 p-4 rounded-sm flex items-start gap-3">
            <AlertTriangle className="size-5 text-yellow-500 shrink-0 mt-0.5" />
            <div className="text-sm text-yellow-500">
              <strong className="block mb-1">Index Worker Unavailable</strong>
              Our indexing servers are currently offline or at capacity. You can still initiate imports, but they will remain in a 'queued' state until capacity frees up.
            </div>
          </div>
        )}

        <div className="text-center space-y-4 mt-4 mb-8">
          <h2 className="text-3xl md:text-5xl font-sans font-bold uppercase tracking-tighter">
            Upload an MP4 or paste a video link.
          </h2>
          <p className="text-muted-foreground text-lg md:text-xl max-w-2xl mx-auto">
            Then describe the scene or event you want to find.
          </p>
        </div>

        {currentImport && currentImport.state !== 'failed' && currentImport.state !== 'cancelled' && currentImport.state !== 'expired' && (
          <Card className="border-primary/50 bg-primary/5">
            <CardContent className="p-4 flex items-center justify-between">
              <div>
                <h3 className="font-bold text-primary mb-1">RESUME_SESSION_DETECTED</h3>
                <p className="text-sm text-muted-foreground">You have an active import session.</p>
              </div>
              <Button onClick={() => setLocation(`/imports/${currentImport.id}`)}>
                RESUME
              </Button>
            </CardContent>
          </Card>
        )}

        <Card className="border-border/50">
          <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as any)} className="w-full">
            <CardHeader className="p-0 border-b border-border/50">
              <TabsList className="w-full h-14 rounded-none bg-transparent p-0 grid grid-cols-2">
                <TabsTrigger 
                  value="upload" 
                  className="rounded-none h-full data-[state=active]:bg-muted/30 data-[state=active]:border-b-2 data-[state=active]:border-primary uppercase tracking-wider font-sans text-sm"
                >
                  <UploadCloud className="size-4 mr-2" /> Upload MP4
                </TabsTrigger>
                <TabsTrigger 
                  value="link" 
                  className="rounded-none h-full data-[state=active]:bg-muted/30 data-[state=active]:border-b-2 data-[state=active]:border-primary uppercase tracking-wider font-sans text-sm"
                >
                  <LinkIcon className="size-4 mr-2" /> Paste a Link
                </TabsTrigger>
              </TabsList>
            </CardHeader>
            <CardContent className="p-6">
              <form id="import-form" onSubmit={handleSubmit} className="flex flex-col gap-6">
                
                <TabsContent value="upload" className="m-0 focus-visible:outline-none">
                  <div className="bg-muted/10 p-6 rounded-sm text-center border border-border/50 flex flex-col items-center justify-center">
                    <UploadCloud className="size-12 text-muted-foreground mb-4 opacity-50" />
                    <p className="text-lg font-bold font-sans">Your MP4, no link required</p>
                    <p className="text-sm text-muted-foreground mt-2 max-w-sm">
                      Confirm your rights, then choose or drop an MP4 on the next screen.
                      Up to 200 MB · H.264 video with AAC audio or silent.
                      You’ll reselect the file if you sign in or reload.
                    </p>
                  </div>
                </TabsContent>

                <TabsContent value="link" className="m-0 focus-visible:outline-none flex flex-col gap-4">
                  <div className="flex flex-col gap-2">
                    <Label htmlFor="linkUrl" className="uppercase text-xs tracking-wider">Video URL</Label>
                    <div className="relative">
                      <div className="absolute left-3 top-3 text-primary">&gt;</div>
                      <Input
                        id="linkUrl"
                        value={linkUrl}
                        onChange={handleLinkChange}
                        placeholder="https://x.com/..."
                        className="pl-8 h-12 text-base bg-black focus-visible:ring-primary font-mono"
                        disabled={isProcessing}
                      />
                    </div>
                  </div>
                  
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                    <button
                      type="button"
                      onClick={() => { setLinkUrl(''); setActiveSourceKind('youtube'); }}
                      className={`p-3 border rounded-sm text-center transition-colors ${activeSourceKind === 'youtube' ? 'border-primary bg-primary/10 text-primary' : 'border-border/50 bg-muted/20 hover:border-primary/50'}`}
                    >
                      <span className="text-sm font-bold block">YouTube</span>
                    </button>
                    <button
                      type="button"
                      onClick={() => { setLinkUrl(''); setActiveSourceKind('x'); }}
                      className={`p-3 border rounded-sm text-center transition-colors ${activeSourceKind === 'x' ? 'border-primary bg-primary/10 text-primary' : 'border-border/50 bg-muted/20 hover:border-primary/50'}`}
                    >
                      <span className="text-sm font-bold block">X (Twitter)</span>
                    </button>
                    <button
                      type="button"
                      onClick={() => { setLinkUrl(''); setActiveSourceKind('tiktok'); }}
                      className={`p-3 border rounded-sm text-center transition-colors ${activeSourceKind === 'tiktok' ? 'border-primary bg-primary/10 text-primary' : 'border-border/50 bg-muted/20 hover:border-primary/50'}`}
                    >
                      <span className="text-sm font-bold block">TikTok</span>
                    </button>
                    <button
                      type="button"
                      onClick={() => { setLinkUrl(''); setActiveSourceKind('vimeo'); }}
                      className={`p-3 border rounded-sm text-center transition-colors ${activeSourceKind === 'vimeo' ? 'border-primary bg-primary/10 text-primary' : 'border-border/50 bg-muted/20 hover:border-primary/50'}`}
                    >
                      <span className="text-sm font-bold block">Vimeo</span>
                    </button>
                  </div>
                  
                  {activeSourceKind === 'youtube' && (
                    <div className="p-3 bg-yellow-500/10 border border-yellow-500/50 rounded-sm flex items-start gap-3">
                      <AlertTriangle className="size-5 text-yellow-500 shrink-0 mt-0.5" />
                      <div className="text-sm text-yellow-500">
                        <strong>YouTube requires an authorized MP4 upload.</strong> 
                        <br/>The link supplies source and official playback context only. Upload an authorized MP4 on the next screen for analysis. We do not download YouTube videos.
                      </div>
                    </div>
                  )}
                  {activeSourceKind && activeSourceKind !== 'youtube' && (
                    <p className="border p-3 text-sm text-muted-foreground">
                      We attempt one authorized, safely retrievable MP4. If restrictions or unsupported delivery prevent import,
                      you can upload an authorized file without losing the link. No login-wall, DRM, or restriction bypass.
                    </p>
                  )}
                  {linkUrl && !activeSourceKind && (
                    <div className="p-3 bg-destructive/10 border border-destructive/50 rounded-sm flex items-start gap-3">
                      <AlertCircle className="size-5 text-destructive shrink-0 mt-0.5" />
                      <div className="text-sm text-destructive">
                        <strong>Unsupported Source</strong> 
                        <br/>The provided URL does not match a supported platform (YouTube, X, TikTok, Vimeo).
                      </div>
                    </div>
                  )}
                </TabsContent>

                <div className="flex flex-col gap-4 bg-muted/20 p-4 border border-border/50 rounded-sm">
                  <div className="flex items-start space-x-3">
                    <Checkbox 
                      id="rights" 
                      checked={rightsAuthorized} 
                      onCheckedChange={(c) => setRightsAuthorized(!!c)} 
                      disabled={isProcessing}
                      className="mt-1 data-[state=checked]:bg-primary data-[state=checked]:text-black"
                    />
                    <div className="space-y-1 leading-none">
                      <Label htmlFor="rights" className="text-sm font-bold cursor-pointer">
                        I confirm I have the right to process this video.
                      </Label>
                      <p className="text-xs text-muted-foreground">
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
                      className="mt-1 data-[state=checked]:bg-primary data-[state=checked]:text-black"
                    />
                    <div className="space-y-1 leading-none">
                      <Label htmlFor="playback" className="text-sm cursor-pointer">
                        Allow owner-only source playback
                      </Label>
                      <p className="text-xs text-muted-foreground">
                        If unchecked, only extracted frames and timestamps will be shown. You can authorize or revoke this later.
                      </p>
                    </div>
                  </div>
                </div>

              </form>
            </CardContent>
            <CardFooter className="p-6 pt-0">
              <Button 
                type="submit" 
                form="import-form" 
                className="w-full h-12 text-base font-bold tracking-wider" 
                disabled={isProcessing || (activeTab === 'link' && !linkUrl) || (activeTab === 'link' && linkUrl && !activeSourceKind) || !rightsAuthorized}
              >
                {isProcessing ? (
                  <><Loader2 className="size-5 animate-spin mr-2" /> PROCESSING...</>
                ) : (
                  <>INITIALIZE_IMPORT</>
                )}
              </Button>
            </CardFooter>
          </Tabs>
        </Card>

        {config && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-center">
            <div className="p-4 border border-border/50 bg-muted/10 rounded-sm">
              <div className="text-2xl font-bold font-sans text-primary">{config.ownerImportLimit}</div>
              <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-1">Lifetime Attempts</div>
            </div>
            <div className="p-4 border border-border/50 bg-muted/10 rounded-sm">
              <div className="text-2xl font-bold font-sans text-primary">{config.ownerSearchLimit}</div>
              <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-1">Lifetime Searches</div>
            </div>
            <div className="p-4 border border-border/50 bg-muted/10 rounded-sm">
              <div className="text-2xl font-bold font-sans text-primary">{config.retentionDays}</div>
              <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-1">Days Retention</div>
            </div>
            <div className="p-4 border border-border/50 bg-muted/10 rounded-sm">
              <div className="text-xl font-bold font-sans text-primary mt-1">{config.minDurationSeconds}s - {Math.round(config.maxDurationSeconds / 60)}m</div>
              <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-1">Video Length</div>
            </div>
          </div>
        )}
        {config && <p className="text-xs text-muted-foreground">
          Account allowances include failed and cancelled attempts and never reset when changing videos.
          Pilot-wide limits: {config.appImportLimit} import attempts and {config.appSearchLimit} searches.
        </p>}

      </main>
    </div>
  );
}