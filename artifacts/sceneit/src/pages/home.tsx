import { useState, useEffect, useMemo } from 'react';
import { Link } from 'wouter';
import {
  useGetProof,
  useListProofSearches,
  useSearchScenes,
  useGetProofReport,
  useHealthCheck,
  getGetProofQueryKey,
  getListProofSearchesQueryKey,
  getHealthCheckQueryKey
} from '@workspace/api-client-react';
import { useQueryClient } from '@tanstack/react-query';
import { formatDistanceToNow } from 'date-fns';
import { Play, Search, Download, AlertCircle, CheckCircle2, Loader2, Image as ImageIcon, History, Video, Clock } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { SceneSearchForm } from '@/components/scene-search-form';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { YouTubePlayer } from '@/components/youtube-player';
import { useToast } from '@/hooks/use-toast';
import { cn, formatTime } from '@/lib/utils';
import type { SceneSearch, SceneMatch } from '@workspace/api-client-react';

export default function Home() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [query, setQuery] = useState('');
  const [modality, setModality] = useState<'both' | 'visual' | 'audio'>('both');

  // Selected search state - could be the active one just searched, or one from history
  const [activeSearchId, setActiveSearchId] = useState<string | null>(null);

  // Currently selected match to play
  const [selectedMatch, setSelectedMatch] = useState<SceneMatch | null>(null);
  const [frameError, setFrameError] = useState(false);

  useEffect(() => {
    setFrameError(false);
  }, [selectedMatch]);

  // Auto-polling proof while not ready
  const {
    data: proof,
    isLoading: isProofLoading,
    isError: isProofError,
    refetch: refetchProof,
  } = useGetProof({
    query: {
      refetchInterval: (query) => {
        const state = query.state.data?.state;
        return state === 'ready' || state === 'failed' || state === 'needs_review' ? false : 3000;
      },
      queryKey: getGetProofQueryKey()
    }
  });

  const { data: healthData } = useHealthCheck({
    query: {
      refetchInterval: 30000,
      queryKey: getHealthCheckQueryKey()
    }
  });

  const {
    data: searchHistory,
    isLoading: isHistoryLoading,
    isError: isHistoryError,
    refetch: refetchHistory,
  } = useListProofSearches({
    query: {
      enabled: proof?.state === 'ready',
      queryKey: getListProofSearchesQueryKey()
    }
  });

  const searchMutation = useSearchScenes({
    mutation: {
      onSuccess: (data) => {
        setActiveSearchId(data.id);
        // Automatically select the top match if available
        if (data.matches.length > 0) {
          setSelectedMatch(data.matches[0]);
        } else {
          setSelectedMatch(null);
        }
        // Invalidate history to show the new search
        queryClient.invalidateQueries({ queryKey: getListProofSearchesQueryKey() });
        // Invalidate proof to update quota
        queryClient.invalidateQueries({ queryKey: getGetProofQueryKey() });
      },
      onError: (error: any) => {
        toast({
          title: 'Search failed',
          description: error?.data?.error || 'An unexpected error occurred during search.',
          variant: 'destructive',
        });
      }
    }
  });

  const [downloadingReport, setDownloadingReport] = useState(false);
  const getReportQuery = useGetProofReport({
    query: {
      enabled: downloadingReport,
      queryKey: ['report-download'],
    }
  });

  useEffect(() => {
    if (downloadingReport && getReportQuery.data) {
      // Trigger download
      const blob = new Blob([JSON.stringify(getReportQuery.data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `sceneit-evidence-report-${proof?.id || 'unknown'}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      setDownloadingReport(false);
    } else if (downloadingReport && getReportQuery.isError) {
      toast({
        title: 'Evidence download failed',
        description: 'Could not fetch evidence report.',
        variant: 'destructive'
      });
      setDownloadingReport(false);
    }
  }, [downloadingReport, getReportQuery.data, getReportQuery.isError, proof?.id, toast]);

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    if (!query.trim() || proof?.state !== 'ready' || searchMutation.isPending || proof.searchesUsed >= proof.searchLimit) return;

    searchMutation.mutate({
      data: { query: query.trim(), modality }
    });
  };

  const handleSelectHistory = (search: SceneSearch) => {
    setActiveSearchId(search.id);
    setQuery(search.query);
    setModality(search.modality as 'both' | 'visual' | 'audio');
    if (search.matches.length > 0) {
      setSelectedMatch(search.matches[0]);
    } else {
      setSelectedMatch(null);
    }
  };

  const activeSearch = useMemo(() => {
    if (!activeSearchId) return null;
    if (searchMutation.data?.id === activeSearchId) return searchMutation.data;
    return searchHistory?.find(s => s.id === activeSearchId) || null;
  }, [activeSearchId, searchHistory, searchMutation.data]);

  // Handle initial active search selection if history is available and no search is active
  useEffect(() => {
    if (!activeSearchId && searchHistory && searchHistory.length > 0 && !searchMutation.isPending) {
      handleSelectHistory(searchHistory[0]);
    }
  }, [searchHistory, activeSearchId, searchMutation.isPending]);

  if (isProofLoading && !proof) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div role="status" className="flex flex-col items-center gap-4 rounded-2xl border bg-card p-10 text-muted-foreground">
          <Loader2 className="size-8 animate-spin text-primary" />
          <p className="font-medium">Opening the scene search demo…</p>
        </div>
      </div>
    );
  }

  if (isProofError || !proof) {
    return (
      <div className="min-h-screen p-8 flex flex-col items-center justify-center text-center">
        <AlertCircle className="size-12 text-destructive mb-4" />
        <h1 className="text-2xl font-semibold mb-2">The demo couldn’t load</h1>
        <p role="alert" className="text-sm leading-relaxed text-muted-foreground max-w-md">
          We couldn’t retrieve this video’s status. Try again in a moment.
        </p>
        <div className="mt-6 flex flex-wrap justify-center gap-3">
          <Button onClick={() => refetchProof()}>Try again</Button>
          <Button variant="outline" asChild><Link href="/">Back to SceneIt</Link></Button>
        </div>
      </div>
    );
  }

  const isReady = proof.state === 'ready';

  return (
    <div className="min-h-screen flex flex-col bg-background">
      {/* Header / Status Banner */}
      <header className="border-b bg-background/95 px-4 py-4 sm:px-6 flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
        <div>
          <Link href="/" aria-label="SceneIt home" className="text-xl font-semibold tracking-tight flex items-center gap-2.5">
            <Video className="size-5 text-primary" />
            <span>Scene<span className="font-light text-muted-foreground">It</span></span>
            <Badge variant="secondary" className="ml-2 font-medium bg-secondary text-secondary-foreground">
              Demo
            </Badge>
          </Link>
          <div className="flex items-center gap-3 mt-2 text-xs font-medium text-muted-foreground">
            <Link href="/" className="min-h-6 hover:text-foreground">Use your own video</Link>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-4 sm:gap-6">
          <div className="flex flex-col items-end">
            <div className="text-xs text-muted-foreground mb-1">
              Ingestion
            </div>
            <div>
              {isReady ? (
                <Badge variant="outline" className="border-primary/30 text-primary bg-primary/10 font-medium">
                  <CheckCircle2 className="size-3 mr-1.5" /> Ready to search
                </Badge>
              ) : proof.state === 'failed' ? (
                <Badge variant="destructive" className="font-medium">Failed</Badge>
              ) : proof.state === 'needs_review' ? (
                <Badge variant="pending">Review needed</Badge>
              ) : (
                <Badge variant="secondary" className="font-medium">
                  <Loader2 className="size-3 mr-1.5 animate-spin" /> {proof.state.replaceAll('_', ' ')}
                </Badge>
              )}
            </div>
          </div>

          <div className="flex flex-col items-end border-l border-border/60 pl-6">
            <div className="text-xs text-muted-foreground mb-1">
              Searches used
            </div>
            <div className="text-sm font-medium">
              <span className="text-foreground">{proof.searchesUsed}</span>
              <span className="text-muted-foreground"> / {proof.searchLimit}</span>
            </div>
          </div>

          <Button
            variant="secondary"
            size="sm"
            onClick={() => setDownloadingReport(true)}
            disabled={downloadingReport}
            className="font-medium"
          >
            {downloadingReport ? <Loader2 className="size-4 animate-spin mr-2" /> : <Download className="size-4 mr-2" />}
            Download evidence
          </Button>
        </div>
      </header>

      {/* Main Content */}
      <main className="flex-1 p-4 md:p-6 grid grid-cols-1 lg:grid-cols-12 gap-6 max-w-[1440px] mx-auto w-full">
        <div className="min-w-0 lg:col-span-12 pt-3">
          <p className="mb-2 text-xs font-medium text-primary">Scene search demo</p>
          <h1 className="text-3xl sm:text-4xl font-semibold tracking-tight">Less scrubbing. More finding.</h1>
          <p className="mt-3 text-sm text-muted-foreground">Search this video, review ranked moments, and compare the original source.</p>
        </div>
        <div className="min-w-0 lg:col-span-12">
          <SceneSearchForm query={query} onQueryChange={setQuery} modality={modality}
            onModalityChange={setModality} onSubmit={handleSearch} ready={isReady}
            pending={searchMutation.isPending} quotaReached={proof.searchesUsed >= proof.searchLimit} />
        </div>

        {/* Left Column: Player & Context */}
        <div className="min-w-0 lg:col-span-7 flex flex-col gap-6">

          <Card className="overflow-hidden shadow-sm">
            <CardHeader className="p-4 border-b border-border/40 bg-card/60">
              <div className="flex flex-col sm:flex-row justify-between items-start gap-3">
                <div className="min-w-0">
                  <CardTitle className="text-base font-medium break-words leading-relaxed" title={proof.title}>
                    {proof.title}
                  </CardTitle>
                  <CardDescription className="mt-1.5 text-xs">
                    {formatTime(proof.durationSeconds)} · {proof.width} × {proof.height} · Demo video
                  </CardDescription>
                </div>
                <Badge variant="outline" className={cn(
                  "shrink-0 font-medium",
                  proof.timelineStatus === 'verified' ? 'border-primary/30 text-primary bg-primary/5' :
                  proof.timelineStatus === 'mismatch' ? 'border-destructive/30 text-destructive bg-destructive/5' :
                  'bg-secondary text-secondary-foreground border-border/50'
                )}>
                  {proof.timelineStatus === 'verified' ? 'Timeline: sampled verification' : `Timeline: ${proof.timelineStatus}`}
                </Badge>
              </div>
            </CardHeader>
            <CardContent className="p-0 bg-black">
              <YouTubePlayer
                videoId={proof.youtubeVideoId}
                startSeconds={selectedMatch?.startSeconds}
                endSeconds={selectedMatch?.endSeconds}
                sourcePlaybackUrl={proof.sourcePlaybackUrl}
                className="w-full"
              />
            </CardContent>

            {/* Status Message / Processing progress */}
            {!isReady && proof.state !== 'failed' && proof.state !== 'needs_review' && (
              <div className="p-4 bg-secondary/50 border-t flex flex-col items-start gap-3">
                <div className="text-sm font-medium flex items-center gap-2">
                  <Loader2 className="size-4 animate-spin text-primary" />
                  {proof.statusMessage || "Processing video..."}
                </div>
                <div className="h-1 w-full bg-border rounded-full overflow-hidden">
                  <div className="h-full bg-primary w-full animate-pulse rounded-full" />
                </div>
              </div>
            )}

            {(proof.state === 'failed' || proof.state === 'needs_review') && (
              <div role="alert" className="p-4 bg-destructive/10 border-t border-destructive/20 text-sm font-medium text-destructive flex items-start gap-2">
                <AlertCircle className="size-4 mt-0.5 shrink-0" />
                {proof.statusMessage || (proof.state === 'needs_review' ? 'Processing is paused for operator review.' : 'Video processing failed.')}
              </div>
            )}
            {proof.checks.some(check => check.status === 'failed') && <div role="alert" className="border-t border-destructive/20 bg-destructive/5 p-4 text-sm text-destructive">
              Some evidence checks failed. Review the technical evidence below before relying on these results.
            </div>}
          </Card>

          {/* Original Frame Comparison */}
          {selectedMatch && (
            <Card className="shadow-sm">
              <CardHeader className="p-4 py-3 border-b border-border/40 bg-card/60 flex flex-row items-center justify-between gap-4 space-y-0">
                <CardTitle className="text-sm font-semibold flex items-center gap-2">
                  <ImageIcon className="size-4 text-primary" />
                  Source Frame Evidence
                </CardTitle>
                <Badge variant="secondary" className="font-mono text-xs text-muted-foreground px-2 py-0.5">
                  <Clock className="size-3 mr-1.5 inline-block" />
                  {formatTime(selectedMatch.startSeconds + (selectedMatch.endSeconds - selectedMatch.startSeconds) / 2)}
                </Badge>
              </CardHeader>
              <CardContent className="p-4">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-6 items-center">
                  <div className="relative aspect-video bg-black rounded-md overflow-hidden border border-border/50">
                    {selectedMatch.frameUrl && !frameError ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={selectedMatch.frameUrl}
                        alt="Original source frame at the selected segment midpoint"
                        className="w-full h-full object-contain"
                        loading="lazy"
                        onError={() => setFrameError(true)}
                      />
                    ) : (
                      <div className="flex flex-col items-center justify-center w-full h-full text-muted-foreground text-sm gap-2">
                        <ImageIcon className="size-6 opacity-20" />
                        <span>Frame unavailable</span>
                      </div>
                    )}
                  </div>
                  <div className="text-sm space-y-4">
                    <div>
                      <p className="font-semibold text-foreground mb-1">Original source material</p>
                      <p className="text-muted-foreground text-xs leading-relaxed">
                        This still was extracted directly from the uploaded file at the match's midpoint. It is not an AI-generated image.
                      </p>
                    </div>
                    <div className="p-3 bg-secondary/40 rounded-md border border-border/50">
                      <p className="font-medium text-xs flex items-center gap-2 mb-1.5">
                        <span className={cn(
                          "size-2 rounded-full inline-block",
                          proof.timelineStatus === 'verified' ? "bg-primary" :
                          proof.timelineStatus === 'mismatch' ? "bg-destructive" :
                          "bg-yellow-500"
                        )} />
                        Timeline is {proof.timelineStatus}
                      </p>
                      <p className="text-xs text-muted-foreground leading-relaxed">
                        {proof.timelineStatus === 'verified'
                          ? "Paired playback matched the recorded sample scenes at their retained YouTube timestamps. This status covers those samples, not every frame."
                          : proof.timelineStatus === 'mismatch'
                            ? "Paired playback found a difference between the indexed source and the YouTube edit."
                            : "Compare this moment with YouTube before treating the timelines as aligned. A matching title or duration alone is not enough."}
                      </p>
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
          )}

          {/* Ingestion Checks */}
          <details className="rounded-xl border bg-card p-4 sm:p-5" open={proof.checks.some(check => check.status === 'failed')}>
            <summary className="cursor-pointer text-sm font-medium">Technical evidence and system checks</summary>
            <dl className="mt-4 grid gap-3 text-xs text-muted-foreground sm:grid-cols-2">
              <div><dt>Proof identifier</dt><dd className="mt-1 break-all text-foreground">{proof.id}</dd></div>
              <div><dt>Search model</dt><dd className="mt-1 break-all text-foreground">{proof.model}</dd></div>
              <div><dt>YouTube video ID</dt><dd className="mt-1 break-all text-foreground">{proof.youtubeVideoId}</dd></div>
              <div><dt>API status</dt><dd className="mt-1 text-foreground">{healthData?.status || 'Unavailable'}</dd></div>
            </dl>
            <div className="mt-5 grid grid-cols-1 sm:grid-cols-2 gap-3">
              {proof.checks.map(check => (
                <div
                  key={check.id}
                  className={cn(
                    "p-3 rounded-md border flex flex-col gap-1.5 text-xs transition-colors",
                    check.status === 'passed' ? 'border-primary/20 bg-primary/5' :
                    check.status === 'failed' ? 'border-destructive/20 bg-destructive/5' :
                    'border-border/60 bg-card/40'
                  )}
                >
                  <div className="flex items-center justify-between font-semibold">
                    <span className="pr-2 text-foreground">{check.label}</span>
                    {check.status === 'passed' && <CheckCircle2 className="size-4 text-primary shrink-0" />}
                    {check.status === 'failed' && <AlertCircle className="size-4 text-destructive shrink-0" />}
                    {check.status === 'pending' && <Loader2 className="size-4 text-muted-foreground animate-spin shrink-0" />}
                    {check.status === 'unverified' && <span className="text-[10px] text-muted-foreground shrink-0 border border-border rounded-sm px-1">?</span>}
                  </div>
                  <div className="break-words text-muted-foreground leading-relaxed">
                    {check.detail}
                  </div>
                </div>
              ))}
            </div>
          </details>
        </div>

        {/* Right Column: Search & Results */}
        <div className="min-w-0 lg:col-span-5 flex flex-col gap-6">

          {/* Active Search Results */}
          <Card className="flex flex-col overflow-hidden min-h-[350px]" aria-busy={searchMutation.isPending}>
            <CardHeader className="p-4 py-3 border-b border-border/40 bg-card/60">
              <CardTitle className="text-sm font-semibold flex items-center justify-between">
                <span>Scene matches</span>
                {activeSearch && (
                  <span className="text-xs text-muted-foreground font-medium flex items-center gap-1.5">
                    <Clock className="size-3" />
                    {activeSearch.latencyMs}ms
                  </span>
                )}
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0 flex-1 overflow-auto max-h-[450px]">
              {searchMutation.isPending ? (
                <div role="status" className="min-h-64 flex flex-col items-center justify-center gap-3 p-8 text-center text-muted-foreground">
                  <Loader2 className="size-7 animate-spin text-primary" aria-hidden="true" />
                  <p className="text-sm">Finding moments in this video…</p>
                </div>
              ) : !activeSearch ? (
                <div className="h-full min-h-[250px] flex flex-col items-center justify-center text-muted-foreground p-8 text-center space-y-3">
                  <Search className="size-8 opacity-20" />
                  <p className="text-sm font-medium">Your next scene starts with a search</p>
                  <p className="text-xs leading-relaxed">Describe a moment above or reopen a saved search below.</p>
                </div>
              ) : (
                <div className="flex flex-col">
                  <div className="p-4 border-b border-border/60 bg-secondary/20">
                    <p className="text-sm font-medium mb-1.5 break-words line-clamp-2">"{activeSearch.query}"</p>
                    <div className="flex flex-wrap gap-2 items-center justify-between text-xs text-muted-foreground">
                      <span>{activeSearch.matches.length} ranked candidates</span>
                      <span className="capitalize">{activeSearch.provider} • {activeSearch.modality}</span>
                    </div>
                  </div>

                  {activeSearch.matches.length === 0 ? (
                    <div className="p-8 text-center space-y-2">
                      <p className="font-semibold text-foreground">No matches found</p>
                      <p className="text-sm text-muted-foreground">No candidate scenes were returned. Try another description or search mode.</p>
                    </div>
                  ) : (
                    <div className="flex flex-col divide-y divide-border/40">
                      {activeSearch.matches.map((match) => (
                        <button
                          key={`${activeSearch.id}-${match.rank}`}
                          type="button"
                          aria-pressed={selectedMatch?.rank === match.rank && selectedMatch?.startSeconds === match.startSeconds}
                          onClick={() => setSelectedMatch(match)}
                          className={cn(
                            "flex items-start gap-4 p-4 hover:bg-secondary/40 transition-colors text-left group focus-visible:outline-none focus-visible:bg-secondary",
                            selectedMatch?.rank === match.rank && selectedMatch?.startSeconds === match.startSeconds
                              ? "bg-primary/10 relative before:absolute before:inset-y-0 before:left-0 before:w-1 before:bg-primary"
                              : ""
                          )}
                        >
                          <div className={cn(
                            "flex flex-col items-center justify-center rounded-md border w-12 h-12 shrink-0 transition-colors",
                            selectedMatch?.rank === match.rank && selectedMatch?.startSeconds === match.startSeconds
                              ? "border-primary/50 bg-background"
                              : "border-border/60 bg-card group-hover:border-border"
                          )}>
                            <span className="text-[10px] font-medium text-muted-foreground mb-0.5">Rank</span>
                            <span className="font-bold text-foreground">{match.rank}</span>
                          </div>

                          <div className="flex-1 min-w-0 flex flex-col justify-center gap-1.5 mt-0.5">
                            <div className="flex items-center gap-2">
                              <span className="text-sm font-semibold text-foreground font-mono">
                                {formatTime(match.startSeconds)}
                              </span>
                              <span className="text-muted-foreground text-xs">-</span>
                              <span className="text-sm font-semibold text-muted-foreground font-mono">
                                {formatTime(match.endSeconds)}
                              </span>
                            </div>
                            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                              {match.confidenceLabel && (
                                <Badge variant="secondary" className="text-xs px-2 py-0.5 font-medium">
                                  {match.confidenceLabel}
                                </Badge>
                              )}
                              <span className="text-xs font-medium text-muted-foreground group-hover:text-primary transition-colors flex items-center">
                                <Play className="size-3 mr-1" /> Inspect segment
                              </span>
                            </div>
                          </div>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </CardContent>
            <p className="border-t p-4 text-xs leading-relaxed text-muted-foreground">
              Ranked by the search provider. Confidence labels are not guarantees; review the source before drawing conclusions.
            </p>
          </Card>

          {/* History */}
          <Card className="shadow-sm">
            <CardHeader className="p-4 py-3 border-b border-border/40 bg-card/60">
              <CardTitle className="text-sm font-semibold flex items-center gap-2">
                <History className="size-3.5" />
                Search history
              </CardTitle>
              <p className="pt-1 text-xs text-muted-foreground">Reopen a saved search without using your allowance.</p>
            </CardHeader>
            <CardContent className="p-0 overflow-hidden">
              {isHistoryError ? (
                <div role="alert" className="p-5 text-sm text-muted-foreground">
                  Search history couldn’t load.
                  <Button variant="link" className="ml-1" onClick={() => refetchHistory()}>Try again</Button>
                </div>
              ) : isHistoryLoading ? (
                <div className="p-6 text-center flex justify-center">
                  <Loader2 className="size-4 animate-spin text-muted-foreground" />
                </div>
              ) : !searchHistory || searchHistory.length === 0 ? (
                <div className="p-6 text-center text-sm text-muted-foreground font-medium">
                  Saved searches for this demo will appear here.
                </div>
              ) : (
                <div className="flex flex-col max-h-[220px] overflow-y-auto divide-y divide-border/40">
                  {searchHistory.map((search) => (
                    <button
                      key={search.id}
                      type="button"
                      aria-pressed={activeSearchId === search.id}
                      onClick={() => handleSelectHistory(search)}
                      className={cn(
                        "text-left p-4 text-sm hover:bg-secondary/40 transition-colors flex flex-col gap-1.5 focus-visible:outline-none focus-visible:bg-secondary",
                        activeSearchId === search.id ? "bg-secondary/30" : ""
                      )}
                    >
                      <div className="font-medium text-foreground break-words line-clamp-2">"{search.query}"</div>
                      <div className="flex flex-wrap gap-2 justify-between items-center text-muted-foreground text-xs">
                        <span className="font-medium">{search.matches.length} matches</span>
                        <span>{formatDistanceToNow(new Date(search.createdAt), { addSuffix: true })}</span>
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

        </div>
      </main>
    </div>
  );
}