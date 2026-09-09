import { useEffect, useState } from 'react';
import { Link, useParams } from 'wouter';
import { useQueryClient } from '@tanstack/react-query';
import { useAuth } from '@workspace/replit-auth-web';
import {
  useGetImport, useGetImportConfig, useListImportSearches, searchImport,
  cancelImport, authorizeImportPlayback, type ImportMatch, type ImportSearch,
} from '@workspace/api-client-react';
import { AuthHeader } from '@/components/auth-header';
import { UploadPanel } from '@/components/upload-panel';
import { PrivateSourcePlayer } from '@/components/private-source-player';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { formatTime } from '@/lib/utils';
import { importError } from '@/lib/import-errors';

export default function SingleImport() {
  const { id = '' } = useParams();
  const auth = useAuth();
  return <div className="min-h-screen bg-background">
    <AuthHeader />
    {auth.isLoading ? <p className="p-8" role="status">Checking your private session…</p>
      : !auth.isAuthenticated ? <main className="mx-auto max-w-xl p-8 space-y-4">
        <h1 className="text-2xl font-bold">Sign in to your private analysis</h1>
        <p>Your video and saved results are only available to their owner.</p>
        {auth.error && <p role="alert">Sign-in status could not be checked. Refresh to retry.</p>}
        <Button onClick={auth.login}>Sign in with Replit</Button>
      </main> : <Analysis key={`${auth.user?.id}:${id}`} id={id} token={auth.csrfToken || ''} />}
  </div>;
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
    if (!ready || busy || !text.trim()) return;
    setBusy(true); setError('');
    try {
      selectSearch(await searchImport(id, { query: text.trim(), modality }, { headers }));
      await refresh();
    } catch (failure) { setError(importError(failure)); }
    finally { setBusy(false); }
  }
  async function cancel() {
    if (!window.confirm('Cancel this import and request deletion of its private media? Cumulative allowances will not reset.')) return;
    setBusy(true); setError('');
    try { await cancelImport(id, {}, { headers }); await refresh(); }
    catch (failure) { setError(importError(failure)); }
    finally { setBusy(false); }
  }
  async function playback(authorized: boolean) {
    setBusy(true); setError('');
    try { await authorizeImportPlayback(id, { authorized }, { headers }); await refresh(); }
    catch (failure) { setError(importError(failure)); }
    finally { setBusy(false); }
  }
  if (itemQuery.isLoading) return <p className="p-8" role="status">Loading private analysis…</p>;
  if (!item) return <main className="mx-auto max-w-xl space-y-4 p-8">
    <h1 className="text-2xl font-bold">Analysis unavailable</h1>
    <p role="alert">{importError(itemQuery.error)}</p>
    <Button onClick={() => itemQuery.refetch()}>Retry</Button>
    <Link href="/" className="block text-primary underline">Return to video imports</Link>
  </main>;
  const waitingForFile = ['awaiting_upload', 'file_required'].includes(item.state);
  const terminal = ['failed', 'cancelled', 'expired', 'needs_review'].includes(item.state);
  const canCancel = !['cancelled', 'expired', 'cancel_requested'].includes(item.state);
  const sourceName = item.sourceKind === 'x' ? 'X / Twitter' : item.sourceKind === 'file' ? 'Standalone MP4' : item.sourceKind;
  return <main className="mx-auto max-w-7xl p-4 md:p-8 space-y-6">
    <header className="flex flex-wrap items-start justify-between gap-4 border-b pb-5">
      <div className="min-w-0 space-y-2">
        <Link href="/" className="text-sm text-primary underline">Upload another video / demo</Link>
        <h1 className="break-all text-2xl font-bold">{item.title}</h1>
        <p className="text-sm text-muted-foreground">{sourceName} · Private to your account
          {item.durationSeconds != null && ` · ${formatTime(item.durationSeconds)}`}
        </p>
      </div>
      {canCancel && <Button variant="outline" onClick={cancel} disabled={busy}>Cancel &amp; delete import</Button>}
    </header>
    {error && <p role="alert" className="border border-destructive p-4 text-destructive">{error}</p>}
    <section aria-live="polite" className="border p-4 space-y-2">
      <h2 className="font-bold capitalize">{item.state.replaceAll('_', ' ')}</h2>
      <p>{item.statusMessage}</p>
      {item.progressPercent != null && !waitingForFile && <p>{Math.round(item.progressPercent)}%
        {ready ? ' ready' : ' of the current transfer'}</p>}
      {!ready && !terminal && !waitingForFile && <p className="text-sm text-muted-foreground">
        Processing continues after this page closes. You can return to this analysis.
      </p>}
      {item.state === 'needs_review' && <p className="text-sm">
        Processing has stopped for operator reconciliation. It will not be purchased again automatically.
      </p>}
      {config && !config.workerAvailable && <p className="text-sm text-yellow-400">
        The processing worker is unavailable. New jobs cannot advance until it returns.
      </p>}
    </section>
    {waitingForFile && <Card><CardHeader><CardTitle>Choose your authorized MP4</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <p>The source link, if supplied, stays attached. Re-select your file after a reload or interrupted upload.
          Uploading is not complete until the server validates its contents.</p>
        <UploadPanel importId={id} config={config} onSuccess={refresh} />
      </CardContent>
    </Card>}
    <div className="grid gap-6 lg:grid-cols-2">
      <div className="space-y-6">
        <Card><CardHeader><CardTitle>Indexed source playback</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-start gap-3">
              <Switch id="private-playback" checked={item.playbackAuthorized}
                disabled={!ready || busy} onCheckedChange={playback} />
              <label htmlFor="private-playback" className="text-sm">
                I authorize owner-only playback of this import’s source file.
                <span className="block text-muted-foreground">Separate from analysis permission. You may revoke this here.</span>
              </label>
            </div>
            {ready && item.sourcePlaybackAvailable && item.sourcePlaybackUrl ?
              <PrivateSourcePlayer key={item.id} src={item.sourcePlaybackUrl}
                startSeconds={selected?.startSeconds} endSeconds={selected?.endSeconds}
                matchKey={String(selection)} /> :
              <p className="text-sm text-muted-foreground">{ready
                ? 'Enable private source playback above to watch the indexed MP4.'
                : 'Source playback becomes available after validation and indexing.'}</p>}
          </CardContent>
        </Card>
        {item.sourceUrl && <Card><CardHeader><CardTitle>Linked platform — separate source</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <p className="text-sm">This link has not been verified as the same edit as the indexed MP4.
              Playback or matching durations do not establish timeline alignment.</p>
            {item.sourceKind === 'youtube' && item.externalId && (
              <iframe className="aspect-video w-full" title="Official YouTube linked video"
                src={`https://www.youtube.com/embed/${encodeURIComponent(item.externalId)}?playsinline=1`}
                allow="encrypted-media; picture-in-picture; fullscreen" allowFullScreen
                referrerPolicy="strict-origin-when-cross-origin" />
            )}
            <a className="block break-all text-sm text-primary underline" href={item.sourceUrl} target="_blank" rel="noreferrer">
              Open original {sourceName} link
            </a>
            <p className="text-xs text-muted-foreground">Platform playback may be unavailable. No linked-video timestamp alignment is claimed.
              {item.sourceKind !== 'youtube' && ' This platform is offered as a source link, not a fabricated seek control.'}</p>
          </CardContent>
        </Card>}
        {ready && selected?.frameUrl && <Card><CardHeader><CardTitle>Candidate source frame</CardTitle></CardHeader>
          <CardContent>
            <img key={selected.frameUrl} src={selected.frameUrl} className="aspect-video w-full object-contain bg-black"
              alt={`Indexed source candidate near ${formatTime(selected.startSeconds)}`}
              onError={event => { event.currentTarget.alt = 'Source thumbnail unavailable. Use the private video or timestamps.'; }} />
          </CardContent>
        </Card>}
      </div>
      <div className="space-y-6">
        <Card><CardHeader><CardTitle>Find relevant moments</CardTitle></CardHeader><CardContent className="space-y-4">
          <form className="space-y-3" onSubmit={runSearch}>
            <label htmlFor="scene-query" className="text-sm">Describe a scene or event</label>
            <Input id="scene-query" value={text} maxLength={400} onChange={event => setText(event.target.value)}
              placeholder="Someone enters a room…" disabled={!ready || busy} />
            <label className="flex flex-wrap items-center gap-3 text-sm">Search in
              <select aria-label="Search modality" className="border bg-background p-2" value={modality}
                disabled={!ready || busy} onChange={event => setModality(event.target.value as typeof modality)}>
                <option value="visual">Visual scenes</option>
                {item.hasAudio && <><option value="audio">Audio</option><option value="both">Visual and audio</option></>}
              </select>
            </label>
            {item.hasAudio === false && <p className="text-xs text-muted-foreground">Silent video: visual search is available; audio is disabled.</p>}
            <Button type="submit" disabled={!ready || busy || !text.trim()}>
              {busy ? 'Please wait…' : 'Find moments'}
            </Button>
          </form>
          <div className="flex flex-wrap gap-2">
            {['Someone enters a room', 'An object moves across the scene'].map(prompt =>
              <Button key={prompt} size="sm" variant="outline" disabled={!ready || busy} onClick={() => setText(prompt)}>{prompt}</Button>)}
          </div>
          <p className="text-xs text-muted-foreground">
            Up to five ranked candidate matches, not exhaustive event detection or guaranteed matches.
            Confidence labels come from the search provider, not a certainty score.
          </p>
          <p className="text-sm">{item.searchesUsed} / {item.searchLimit} cumulative account searches used.
            Saved searches can be reopened without a new request.</p>
        </CardContent></Card>
        {ready && result && <Card><CardHeader><CardTitle>Candidate matches</CardTitle></CardHeader><CardContent className="space-y-3">
          <p className="break-words">“{result.query}”</p>
          <p className="text-xs text-muted-foreground">Saved Twelve Labs result · {result.modality}</p>
          {result.partial && <p className="text-sm">Some invalid or unavailable provider candidates were omitted.</p>}
          {!result.matches.length && <p>No relevant candidates were returned. Try a different description.</p>}
          {result.matches.map(match => <button key={match.rank} type="button"
            className={`w-full border p-4 text-left ${selected?.rank === match.rank ? 'border-primary bg-primary/5' : ''}`}
            onClick={() => { setSelected(match); setSelection(value => value + 1); }}>
            <span className="block font-bold">#{match.rank} · {formatTime(match.startSeconds)} – {formatTime(match.endSeconds)}</span>
            <span className="text-sm text-muted-foreground">{match.confidenceLabel || 'Confidence label unavailable'} · Select source segment</span>
          </button>)}
        </CardContent></Card>}
        {ready && <Card><CardHeader><CardTitle>Saved searches</CardTitle></CardHeader><CardContent className="space-y-2">
          {historyQuery.isError && <p role="alert">History could not load. <button className="underline" onClick={() => historyQuery.refetch()}>Retry</button></p>}
          {!history.length && <p className="text-sm text-muted-foreground">Your searches for this video will appear here.</p>}
          {history.map(search => <button key={search.id} className="block w-full break-words border p-3 text-left text-sm"
            onClick={() => selectSearch(search)}>{search.query} · {search.matches.length} candidates</button>)}
        </CardContent></Card>}
      </div>
    </div>
    <footer className="border-t pt-4 text-sm text-muted-foreground space-y-2">
      <p>{item.importsUsed} / {item.importLimit} lifetime account attempts used. Cancellation, failure, deletion, or changing entry method does not reset allowances.</p>
      <p>Private media access ends {new Date(item.expiresAt).toLocaleString()}. Normal retention: {config?.retentionDays || 7} days.
        Uncertain provider operations may need operator cleanup beyond that deadline.</p>
      <p>Limits: 200 MB, H.264 MP4 with AAC audio or silent, 4 seconds–20 minutes.</p>
    </footer>
  </main>;
}