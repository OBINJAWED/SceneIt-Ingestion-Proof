import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'wouter';
import { useQueryClient } from '@tanstack/react-query';
import { useAuth } from '@workspace/replit-auth-web';
import {
  useGetImport, useGetImportConfig, useListImportSearches, searchImport,
  cancelImport, authorizeImportPlayback, type ImportMatch, type ImportSearch,
} from '@workspace/api-client-react';
import { AuthHeader } from '@/components/auth-header';
import { UploadPanel } from '@/components/upload-panel';
import { CancelImportDialog } from '@/components/cancel-import-dialog';
import { PrivateSourcePlayer } from '@/components/private-source-player';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { formatTime } from '@/lib/utils';
import { importError } from '@/lib/import-errors';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import {
  AlertTriangle, ArrowLeft, CheckCircle2, Clock3, ExternalLink, Film,
  Loader2, LockKeyhole, Search, ShieldCheck, Trash2,
} from 'lucide-react';

export default function SingleImport() {
  const { id = '' } = useParams();
  const auth = useAuth();
  return <div className="min-h-screen bg-background">
    <AuthHeader />
    {auth.isLoading ? <main className="grid min-h-[70vh] place-items-center p-6">
      <div className="flex items-center gap-3 text-sm text-muted-foreground" role="status">
        <Loader2 className="size-4 animate-spin" aria-hidden="true" /> Checking your private session…
      </div>
    </main>
      : !auth.isAuthenticated ? <main className="mx-auto grid min-h-[70vh] max-w-5xl place-items-center p-4 sm:p-8">
        <Card className="w-full max-w-lg rounded-xl border-border/70">
          <CardHeader className="space-y-4 p-6 sm:p-8">
            <div className="grid size-11 place-items-center rounded-lg border border-primary/30 bg-primary/10 text-primary">
              <LockKeyhole className="size-5" aria-hidden="true" />
            </div>
            <div className="space-y-2">
              <CardTitle className="text-2xl sm:text-3xl">Your private analysis</CardTitle>
              <p className="text-sm leading-6 text-muted-foreground">
                Sign in to access this video, its processing status, and saved search results. Only the owner can continue.
              </p>
            </div>
          </CardHeader>
          <CardContent className="space-y-4 px-6 pb-6 sm:px-8 sm:pb-8">
            {auth.error && <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive" role="alert">
              Sign-in status could not be checked. Refresh the page or try signing in again.
            </div>}
            <Button className="w-full sm:w-auto" onClick={auth.login} data-testid="button-sign-in">
              <ShieldCheck className="mr-2 size-4" aria-hidden="true" /> Sign in with Replit
            </Button>
          </CardContent>
        </Card>
      </main> : <Analysis key={`${auth.user?.id}:${id}`} id={id} token={auth.csrfToken || ''} />}
  </div>;
}

function CandidateFrame({ match }: { match: ImportMatch }) {
  const [unavailable, setUnavailable] = useState(false);
  useEffect(() => setUnavailable(false), [match.frameUrl]);
  if (!match.frameUrl || unavailable) return <div className="grid aspect-video w-full place-items-center rounded-lg border border-dashed bg-background/40 p-6 text-center">
    <div className="space-y-2">
      <Film className="mx-auto size-7 text-muted-foreground" aria-hidden="true" />
      <p className="text-sm font-medium">Frame preview unavailable</p>
      <p className="text-xs leading-5 text-muted-foreground">Use private source playback or the candidate timestamps instead.</p>
    </div>
  </div>;
  return <img key={match.frameUrl} src={match.frameUrl} loading="lazy"
    className="aspect-video w-full rounded-lg bg-black object-contain"
    alt={`Indexed source candidate near ${formatTime(match.startSeconds)}`}
    onError={() => setUnavailable(true)} />;
}
function Analysis({ id, token }: { id: string; token: string }) {
  const cache = useQueryClient();
  const headers = { 'X-CSRF-Token': token };
  const [text, setText] = useState('');
  const [modality, setModality] = useState<'visual' | 'audio' | 'both'>('visual');
  const [result, setResult] = useState<ImportSearch | null>(null);
  const [selected, setSelected] = useState<ImportMatch | null>(null);
  const [selection, setSelection] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [cancelOpen, setCancelOpen] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [cancelAttempted, setCancelAttempted] = useState(false);
  const [uploadCancellation] = useState(() => new AbortController());
  const cancellationInFlight = useRef(false);
  const { data: config } = useGetImportConfig();
  const itemQuery = useGetImport(id, { query: {
    queryKey: ['/api/imports', id],
    refetchInterval: query => {
      const state = query.state.data?.state;
      return state && ['ready', 'failed', 'cancelled', 'expired', 'needs_review'].includes(state) ? false : 3000;
    },
  } });
  const item = itemQuery.data;
  const ready = item?.state === 'ready';
  const historyQuery = useListImportSearches(id, { query: {
    queryKey: ['/api/imports', id, 'searches'], enabled: ready,
  } });
  const history = historyQuery.data || [];
  const refresh = async () => {
    await cache.invalidateQueries({ queryKey: ['/api/imports', id] });
    await cache.invalidateQueries({ queryKey: ['/api/imports/current'] });
  };
  function selectSearch(search: ImportSearch) {
    setResult(search); setText(search.query); setModality(search.modality);
    setSelected(search.matches[0] || null); setSelection(value => value + 1);
  }
  useEffect(() => {
    if (!result && history.length) selectSearch(history[0]);
  }, [historyQuery.data]);
  async function runSearch(event: React.FormEvent) {
    event.preventDefault();
    if (!ready || busy || !text.trim() || (item && item.searchesUsed >= item.searchLimit)) return;
    setBusy(true); setError('');
    try {
      selectSearch(await searchImport(id, { query: text.trim(), modality }, { headers }));
      await refresh();
    } catch (failure) { setError(importError(failure)); }
    finally { setBusy(false); }
  }
  async function cancel() {
    if (cancellationInFlight.current) return;
    cancellationInFlight.current = true;
    setCancelling(true); setBusy(true); setCancelAttempted(true); setError('');
    uploadCancellation.abort();
    try {
      const cancelled = await cancelImport(id, {}, { headers });
      // Discard an older poll before committing the acknowledged cancellation.
      await cache.cancelQueries({ queryKey: ['/api/imports', id], exact: true });
      cache.setQueryData(['/api/imports', id], cancelled);
      setCancelOpen(false);
      await refresh();
    } catch (failure) {
      setCancelOpen(false);
      setError(`Cancellation wasn’t confirmed. ${importError(failure)} Retry cancellation to finish cleanup.`);
    } finally {
      cancellationInFlight.current = false;
      setCancelling(false);
      setBusy(false);
    }
  }
  async function playback(authorized: boolean) {
    setBusy(true); setError('');
    try { await authorizeImportPlayback(id, { authorized }, { headers }); await refresh(); }
    catch (failure) { setError(importError(failure)); }
    finally { setBusy(false); }
  }
  if (itemQuery.isLoading) return <main className="grid min-h-[65vh] place-items-center p-6">
    <div className="flex items-center gap-3 text-sm text-muted-foreground" role="status">
      <Loader2 className="size-4 animate-spin" aria-hidden="true" /> Loading private analysis…
    </div>
  </main>;
  if (!item) return <main className="mx-auto grid min-h-[65vh] max-w-xl place-items-center p-4 sm:p-8">
    <Card className="w-full rounded-xl">
      <CardHeader><CardTitle>Analysis unavailable</CardTitle></CardHeader>
      <CardContent className="space-y-5">
        <p role="alert" className="break-words text-sm leading-6 text-muted-foreground">{importError(itemQuery.error)}</p>
        <div className="flex flex-wrap gap-3">
          <Button onClick={() => itemQuery.refetch()} data-testid="button-retry-analysis">Retry</Button>
          <Button variant="outline" asChild><Link href="/" data-testid="link-imports">Return to imports</Link></Button>
        </div>
      </CardContent>
    </Card>
  </main>;
  const waitingForFile = ['awaiting_upload', 'file_required'].includes(item.state);
  const terminal = ['failed', 'cancelled', 'expired', 'needs_review'].includes(item.state);
  const canCancel = !['cancelled', 'expired', 'cancel_requested'].includes(item.state);
  const sourceName = { file: 'Standalone MP4', youtube: 'YouTube', x: 'X / Twitter', tiktok: 'TikTok', vimeo: 'Vimeo' }[item.sourceKind];
  const stateLabel = item.state.replaceAll('_', ' ');
  return <main className="mx-auto max-w-7xl space-y-6 p-4 sm:p-6 lg:p-8">
    <header className="flex flex-col gap-5 border-b border-border/70 pb-6 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0 space-y-3">
        <Link href="/" className="inline-flex items-center gap-2 text-sm text-muted-foreground transition-colors hover:text-foreground"
          data-testid="link-back-imports">
          <ArrowLeft className="size-4" aria-hidden="true" /> Back to video imports
        </Link>
        <div className="space-y-2">
          <Badge variant="outline" className="rounded-full font-sans normal-case tracking-normal">
            <LockKeyhole className="mr-1.5 size-3" aria-hidden="true" /> Owner-only
          </Badge>
          <h1 className="break-words text-2xl font-semibold leading-tight sm:text-3xl" data-testid="text-import-title">{item.title}</h1>
        </div>
        <p className="text-sm text-muted-foreground">{sourceName} · Private to your account
          {item.durationSeconds != null && ` · ${formatTime(item.durationSeconds)}`}
        </p>
      </div>
      {canCancel && <Button variant="outline" onClick={() => setCancelOpen(true)} disabled={busy || cancelling} className="w-full shrink-0 sm:w-auto"
        data-testid="button-cancel-import">
        <Trash2 className="mr-2 size-4" aria-hidden="true" />
        {cancelling ? 'Requesting cancellation…' : cancelAttempted ? 'Retry cancellation' : ready ? 'Delete import' : waitingForFile ? 'Cancel upload' : 'Cancel import'}
      </Button>}
    </header>
    <CancelImportDialog open={cancelOpen} onOpenChange={setCancelOpen} onConfirm={cancel}
      pending={cancelling} upload={waitingForFile} ready={ready} />
    {error && <div role="alert" className="flex items-start gap-3 rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">
      <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" /><p className="min-w-0 break-words">{error}</p>
    </div>}
    {cancelAttempted && waitingForFile && <div role="status" className="rounded-lg border border-border/70 bg-card p-4 text-sm text-muted-foreground">
      The file transfer is stopped in this browser.
      {cancelling ? ' Requesting cancellation and private media cleanup…' : ' Retry cancellation if it has not been confirmed.'}
    </div>}
    <section aria-live="polite" className="rounded-xl border border-border/70 bg-card p-5 sm:p-6" data-testid="status-import">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            {ready ? <CheckCircle2 className="size-5 text-primary" aria-hidden="true" /> : <Clock3 className="size-5 text-muted-foreground" aria-hidden="true" />}
            <h2 className="font-semibold capitalize">Ingestion: {stateLabel}</h2>
          </div>
          <p className="break-words text-sm leading-6 text-muted-foreground">{item.statusMessage}</p>
        </div>
        <Badge variant={ready ? 'default' : 'outline'} className="w-fit rounded-full font-sans normal-case tracking-normal">
          {ready ? 'Ready to search' : item.state === 'cancel_requested' ? 'Cancellation requested' : waitingForFile ? 'File needed' : terminal ? 'Action needed' : 'Processing'}
        </Badge>
      </div>
      {item.progressPercent != null && !waitingForFile && <div className="mt-5 space-y-2">
        <div className="flex justify-between gap-4 text-xs text-muted-foreground">
          <span>{ready ? 'Ingestion complete' : 'Current processing stage'}</span><span>{Math.round(item.progressPercent)}%</span>
        </div>
        <Progress value={item.progressPercent} className="h-2" />
      </div>}
      {item.state === 'cancel_requested' && <p className="mt-4 text-sm text-muted-foreground">
        Cancellation was received. Private media cleanup runs in the background; you can leave this page.
      </p>}
      {!ready && !terminal && !waitingForFile && item.state !== 'cancel_requested' && <p className="mt-4 text-sm text-muted-foreground">
        Processing continues after you leave this page. You can return at any time.
      </p>}
      {item.state === 'needs_review' && <p className="mt-4 rounded-lg border border-border bg-background/50 p-3 text-sm">
        Processing has stopped for operator reconciliation. It will not be purchased again automatically.
      </p>}
      {config && !config.workerAvailable && <p className="mt-4 rounded-lg border border-border bg-background/50 p-3 text-sm text-foreground">
        The processing worker is unavailable. New jobs cannot advance until it returns.
      </p>}
    </section>
    {waitingForFile && <Card className="rounded-xl"><CardHeader className="pb-3"><CardTitle>Choose your authorized MP4</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm leading-6 text-muted-foreground">The source link, if supplied, stays attached. Re-select your file after a reload or interrupted upload.
          Uploading is not complete until the server validates its contents.</p>
        <UploadPanel importId={id} config={config} onSuccess={refresh}
          cancellationSignal={uploadCancellation.signal} cancellationPending={cancelling}
          onCancel={() => setCancelOpen(true)} />
      </CardContent>
    </Card>}
    <div className="grid gap-6 lg:grid-cols-2">
      <div className="min-w-0 space-y-6">
        <Card className="rounded-xl"><CardHeader className="pb-3"><CardTitle>Private source playback</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-start gap-3 rounded-lg border border-border/70 bg-background/40 p-4">
              <Switch id="private-playback" checked={item.playbackAuthorized}
                disabled={!ready || busy} onCheckedChange={playback} data-testid="switch-private-playback" />
              <label htmlFor="private-playback" className="min-w-0 text-sm font-medium leading-5">
                Allow owner-only playback of the indexed source file
                <span className="mt-1 block font-normal text-muted-foreground">This permission is separate from ingestion and search. You can revoke it here.</span>
              </label>
            </div>
            {ready && item.sourcePlaybackAvailable && item.sourcePlaybackUrl ?
              <PrivateSourcePlayer key={item.id} src={item.sourcePlaybackUrl}
                startSeconds={selected?.startSeconds} endSeconds={selected?.endSeconds}
                matchKey={String(selection)} /> :
              <div className="rounded-lg border border-dashed p-5 text-sm text-muted-foreground">{ready
                ? item.playbackAuthorized
                  ? 'Private playback is authorized, but the source is not currently available. Your search results and timestamps remain accessible.'
                  : 'Ingestion is ready. Enable private source playback above to watch the indexed MP4.'
                : 'Playback availability is separate from ingestion readiness. It can be requested after validation and indexing.'}</div>}
          </CardContent>
        </Card>
        {item.sourceUrl && <Card className="rounded-xl"><CardHeader className="pb-3"><CardTitle>Linked platform source</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <div className="rounded-lg border border-border/70 bg-background/40 p-4 text-sm leading-6"><strong>Timeline not verified.</strong> This link has not been verified as the same edit as the indexed MP4.
              Playback or matching durations do not establish timeline alignment.</div>
            {item.sourceKind === 'youtube' && item.externalId && (
              <iframe className="aspect-video w-full rounded-lg border border-border" title="Official YouTube linked video"
                src={`https://www.youtube.com/embed/${encodeURIComponent(item.externalId)}?playsinline=1`}
                allow="encrypted-media; picture-in-picture; fullscreen" allowFullScreen
                referrerPolicy="strict-origin-when-cross-origin" />
            )}
            <a className="inline-flex max-w-full items-center gap-2 break-all text-sm text-primary underline underline-offset-4" href={item.sourceUrl} target="_blank" rel="noreferrer"
              data-testid="link-external-source">
              <ExternalLink className="size-4 shrink-0" aria-hidden="true" /> <span className="break-all">Open original {sourceName} link</span>
            </a>
            <p className="text-xs text-muted-foreground">Platform playback may be unavailable. No linked-video timestamp alignment is claimed.
              {item.sourceKind !== 'youtube' && ' This platform is offered as a source link, not a fabricated seek control.'}</p>
          </CardContent>
        </Card>}
        {ready && selected && <Card className="rounded-xl"><CardHeader className="pb-3"><CardTitle>Candidate source frame</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            <CandidateFrame match={selected} />
            <p className="text-xs text-muted-foreground">Frame near {formatTime(selected.startSeconds)} in the indexed source.</p>
          </CardContent>
        </Card>}
      </div>
      <div className="min-w-0 space-y-6">
        <Card className="rounded-xl"><CardHeader className="pb-3"><CardTitle>Search this video</CardTitle></CardHeader><CardContent className="space-y-5">
          <form className="space-y-3" onSubmit={runSearch}>
            <label htmlFor="scene-query" className="text-sm font-medium">Describe a scene, action, or spoken moment</label>
            <Input id="scene-query" value={text} maxLength={400} onChange={event => setText(event.target.value)}
              placeholder="For example, someone enters a room" disabled={!ready || busy} className="h-11"
              data-testid="input-scene-query" />
            <label className="flex flex-wrap items-center gap-3 text-sm font-medium">Search mode
              <select aria-label="Search modality" className="h-11 max-w-full rounded-lg border border-border bg-background px-3 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring" value={modality}
                disabled={!ready || busy} onChange={event => setModality(event.target.value as typeof modality)}>
                <option value="visual">Visual scenes</option>
                {item.hasAudio && <><option value="audio">Audio</option><option value="both">Visual and audio</option></>}
              </select>
            </label>
            {item.hasAudio === false && <p className="rounded-lg border border-border/70 bg-background/40 p-3 text-xs text-muted-foreground">
              This video is silent. Visual search remains available; audio search is disabled.
            </p>}
            <Button type="submit" disabled={!ready || busy || !text.trim() || item.searchesUsed >= item.searchLimit} className="w-full sm:w-auto" data-testid="button-search">
              {busy ? <><Loader2 className="mr-2 size-4 animate-spin" aria-hidden="true" /> Please wait…</> : <><Search className="mr-2 size-4" aria-hidden="true" /> Search scenes</>}
            </Button>
          </form>
          <div className="space-y-2">
            <p className="text-xs font-medium text-muted-foreground">Try a suggestion</p>
            <div className="flex flex-wrap gap-2">
            {['Someone enters a room', 'An object moves across the scene'].map(prompt =>
              <Button key={prompt} size="sm" variant="outline" disabled={!ready || busy} onClick={() => setText(prompt)}
                className="h-auto max-w-full whitespace-normal py-2 text-left" data-testid={`button-suggestion-${prompt.startsWith('Someone') ? 'entry' : 'object'}`}>
                {prompt}
              </Button>)}
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            Up to five ranked candidate matches, not exhaustive event detection or guaranteed matches.
            Confidence labels come from the search provider, not a certainty score.
          </p>
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border/70 bg-background/40 p-3 text-sm">
            <span><strong>{item.searchesUsed} of {item.searchLimit}</strong> account searches used</span>
            <span className="text-xs text-muted-foreground">Saved searches use no additional request</span>
          </div>
          {item.searchesUsed >= item.searchLimit && <p role="status" className="rounded-lg border border-amber-400/25 bg-amber-400/5 p-3 text-sm text-amber-300">
            Your account search allowance is used up. Saved results are still available below.
          </p>}
        </CardContent></Card>
        {ready && result && <Card className="rounded-xl"><CardHeader className="space-y-2 pb-3"><CardTitle>Candidate matches</CardTitle>
          <p className="break-words text-sm text-muted-foreground">Results for “{result.query}”</p>
        </CardHeader><CardContent className="space-y-3">
          <p className="text-xs text-muted-foreground">Saved provider result · {result.modality} search</p>
          {result.partial && <p className="rounded-lg border border-border/70 p-3 text-sm">Some invalid or unavailable provider candidates were omitted.</p>}
          {!result.matches.length && <p>No relevant candidates were returned. Try a different description.</p>}
          {result.matches.map(match => <button key={match.rank} type="button"
            className={`w-full rounded-lg border p-4 text-left outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring ${selected?.rank === match.rank ? 'border-primary bg-primary/10' : 'border-border hover:bg-background/60'}`}
            onClick={() => { setSelected(match); setSelection(value => value + 1); }}
            aria-pressed={selected?.rank === match.rank} aria-label={`Candidate ${match.rank}, ${formatTime(match.startSeconds)} to ${formatTime(match.endSeconds)}`}
            data-testid={`button-match-${match.rank}`}>
            <span className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-semibold">Candidate {match.rank}</span>
              {selected?.rank === match.rank && <Badge className="rounded-full font-sans normal-case tracking-normal">Selected</Badge>}
            </span>
            <span className="mt-2 block text-sm">{formatTime(match.startSeconds)} – {formatTime(match.endSeconds)}</span>
            <span className="mt-1 block break-words text-xs text-muted-foreground">{match.confidenceLabel || 'Confidence label unavailable'} · Select to cue the private source segment</span>
          </button>)}
        </CardContent></Card>}
        {ready && !result && <Card className="rounded-xl"><CardContent className="p-6 text-center">
          <Search className="mx-auto mb-3 size-7 text-muted-foreground" aria-hidden="true" />
          <h2 className="text-sm font-medium">Find your first moment</h2>
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">Describe a scene above. Ranked candidates and source timestamps will appear here.</p>
        </CardContent></Card>}
        {ready && <Card className="rounded-xl"><CardHeader className="pb-3"><CardTitle>Search history</CardTitle></CardHeader><CardContent className="space-y-2">
          {historyQuery.isError && <p role="alert">History could not load. <button className="underline" onClick={() => historyQuery.refetch()}>Retry</button></p>}
          {!history.length && <p className="text-sm text-muted-foreground">Your searches for this video will appear here.</p>}
          {history.map(search => <button key={search.id}
            className={`block w-full rounded-lg border p-3 text-left text-sm outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring ${result?.id === search.id ? 'border-primary bg-primary/10' : 'border-border hover:bg-background/60'}`}
            onClick={() => selectSearch(search)} aria-pressed={result?.id === search.id} data-testid={`button-history-${search.id}`}>
            <span className="block break-words font-medium">{search.query}</span>
            <span className="mt-1 block text-xs text-muted-foreground">{search.matches.length} candidates · {search.modality}</span>
          </button>)}
        </CardContent></Card>}
      </div>
    </div>
    <footer className="grid gap-3 border-t border-border/70 pt-5 text-xs leading-5 text-muted-foreground md:grid-cols-3">
      <p><strong className="text-foreground">{item.importsUsed} of {item.importLimit} lifetime import attempts used.</strong><br />Cancellation, failure, deletion, or changing entry method does not reset allowances.</p>
      <p><strong className="text-foreground">Private media access ends {new Date(item.expiresAt).toLocaleString()}.</strong><br />Normal retention: {config?.retentionDays || 7} days.
        Uncertain provider operations may need operator cleanup beyond that deadline.</p>
      <p><strong className="text-foreground">Accepted media</strong><br />Up to 200 MB, H.264 MP4 with AAC audio or silent, 4 seconds–20 minutes.</p>
    </footer>
  </main>;
}
