import { useState, useRef, useEffect, useMemo } from 'react';
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
import { Play, Search, Download, AlertCircle, CheckCircle2, Loader2, Image as ImageIcon, History, Video } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle, CardFooter } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
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
    isError: isProofError
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
    isLoading: isHistoryLoading
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
          title: 'Search Failed',
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
      a.download = `sceneit-proof-report-${proof?.id || 'unknown'}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      setDownloadingReport(false);
    } else if (downloadingReport && getReportQuery.isError) {
      toast({
        title: 'Report Download Failed',
        description: 'Could not fetch proof report.',
        variant: 'destructive'
      });
      setDownloadingReport(false);
    }
  }, [downloadingReport, getReportQuery.data, getReportQuery.isError, proof?.id, toast]);

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    if (!query.trim() || proof?.state !== 'ready') return;
    
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
    if (!activeSearchId || !searchHistory) return null;
    if (searchMutation.data?.id === activeSearchId) return searchMutation.data;
    return searchHistory.find(s => s.id === activeSearchId) || null;
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
        <div className="flex flex-col items-center gap-4">
          <Loader2 className="h-8 w-8 animate-spin text-primary" />
          <p className="text-muted-foreground animate-pulse">ESTABLISHING_UPLINK...</p>
        </div>
      </div>
    );
  }

  if (isProofError || !proof) {
    return (
      <div className="min-h-screen p-8 flex flex-col items-center justify-center text-center">
        <AlertCircle className="h-12 w-12 text-destructive mb-4" />
        <h1 className="text-2xl font-bold text-destructive mb-2 font-sans">UPLINK_FAILED</h1>
        <p className="text-muted-foreground max-w-md">
          Unable to retrieve proof state. Ensure the API server is running and accessible.
        </p>
      </div>
    );
  }

  const isReady = proof.state === 'ready';

  return (
    <div className="min-h-screen flex flex-col">
      {/* Header / Status Banner */}
      <header className="border-b p-4 bg-background sticky top-0 z-10 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold font-sans tracking-tight uppercase flex items-center gap-2">
            <Video className="size-6 text-primary" />
            SCENE<span className="text-primary">IT</span>
            <Badge variant="outline" className="ml-2 bg-primary/10 text-primary border-primary">
              ONE-VIDEO PROOF
            </Badge>
          </h1>
          <div className="flex items-center gap-3 mt-2 text-sm text-muted-foreground">
            <span className="flex items-center gap-1">
              <span className="w-2 h-2 rounded-full bg-primary animate-pulse" />
              {proof.id}
            </span>
            <span className="text-border">|</span>
            <span>MODEL: {proof.model}</span>
          </div>
        </div>

        <div className="flex items-center gap-4">
          <div className="text-right">
            <div className="text-xs text-muted-foreground mb-1 uppercase tracking-wider flex items-center justify-end gap-1">
              Index_Status
              {healthData?.status && <span className="w-1.5 h-1.5 rounded-full bg-primary inline-block ml-1" title={`API: ${healthData.status}`} />}
            </div>
            <div className="flex items-center justify-end gap-2">
              {isReady ? (
                <Badge variant="success" className="animate-in fade-in zoom-in duration-300">
                  <CheckCircle2 className="size-3 mr-1" /> READY
                </Badge>
              ) : proof.state === 'failed' ? (
                <Badge variant="destructive">FAILED</Badge>
              ) : (
                <Badge variant="pending" className="animate-pulse">
                  <Loader2 className="size-3 mr-1 animate-spin" /> {proof.state.toUpperCase()}
                </Badge>
              )}
            </div>
          </div>
          
          <div className="text-right border-l pl-4 border-border">
            <div className="text-xs text-muted-foreground mb-1 uppercase tracking-wider">
              Quota
            </div>
            <div className="text-sm">
              <span className="text-primary font-bold">{proof.searchesUsed}</span>
              <span className="text-muted-foreground"> / {proof.searchLimit}</span>
            </div>
          </div>

          <Button 
            variant="outline" 
            size="sm"
            onClick={() => setDownloadingReport(true)}
            disabled={downloadingReport}
            className="ml-2"
          >
            {downloadingReport ? <Loader2 className="size-4 animate-spin mr-2" /> : <Download className="size-4 mr-2" />}
            EVIDENCE.JSON
          </Button>
        </div>
      </header>

      {/* Main Content */}
      <main className="flex-1 p-4 md:p-6 grid grid-cols-1 lg:grid-cols-12 gap-6 max-w-[1600px] mx-auto w-full">
        
        {/* Left Column: Player & Context */}
        <div className="lg:col-span-7 flex flex-col gap-6">
          
          <Card>
            <CardHeader className="p-4 pb-0 border-b border-border/50 bg-muted/20">
              <div className="flex justify-between items-start">
                <div>
                  <CardTitle className="text-lg line-clamp-1 text-foreground" title={proof.title}>
                    {proof.title}
                  </CardTitle>
                  <CardDescription className="mt-1">
                    YouTube: {proof.youtubeVideoId} • {formatTime(proof.durationSeconds)} • {proof.width}x{proof.height}
                  </CardDescription>
                </div>
                <Badge variant="outline" className={
                  proof.timelineStatus === 'verified' ? 'border-primary text-primary' : 
                  proof.timelineStatus === 'mismatch' ? 'border-destructive text-destructive' : ''
                }>
                  TIMELINE: {proof.timelineStatus.toUpperCase()}
                </Badge>
              </div>
            </CardHeader>
            <CardContent className="p-0">
              <YouTubePlayer 
                videoId={proof.youtubeVideoId}
                startSeconds={selectedMatch?.startSeconds}
                endSeconds={selectedMatch?.endSeconds}
                sourcePlaybackUrl={proof.sourcePlaybackUrl}
                className="w-full"
              />
            </CardContent>
            
            {/* Status Message / Processing progress */}
            {!isReady && proof.state !== 'failed' && (
              <CardFooter className="p-4 bg-muted/30 border-t flex flex-col items-start gap-2">
                <div className="text-sm font-bold text-primary flex items-center gap-2">
                  <Loader2 className="size-4 animate-spin" />
                  {proof.statusMessage || "Processing video..."}
                </div>
                <div className="h-1 w-full bg-primary/20 overflow-hidden">
                  <div className="h-full bg-primary w-full animate-pulse" />
                </div>
              </CardFooter>
            )}
            
            {proof.state === 'failed' && (
              <CardFooter className="p-4 bg-destructive/10 border-t border-destructive/30">
                <div className="text-sm font-bold text-destructive flex items-center gap-2">
                  <AlertCircle className="size-4" />
                  {proof.statusMessage || "Ingestion failed."}
                </div>
              </CardFooter>
            )}
          </Card>

          {/* Original Frame Comparison */}
          {selectedMatch && (
            <Card className="border-dashed">
              <CardHeader className="p-4 pb-2 border-b border-border/50">
                <CardTitle className="text-sm flex items-center justify-between">
                  <span className="flex items-center gap-2 text-muted-foreground uppercase tracking-wider">
                    <ImageIcon className="size-4" />
                    Source Frame Evidence
                  </span>
                  <Badge variant="outline" className="text-[10px]">
                    @ {formatTime(selectedMatch.startSeconds + (selectedMatch.endSeconds - selectedMatch.startSeconds) / 2)}
                  </Badge>
                </CardTitle>
              </CardHeader>
              <CardContent className="p-4">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4 items-center">
                  <div className="relative aspect-video bg-muted border overflow-hidden">
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
                      <div className="flex items-center justify-center w-full h-full text-muted-foreground text-xs uppercase text-center p-4">
                        [FRAME_UNAVAILABLE]
                      </div>
                    )}
                  </div>
                  <div className="text-sm text-muted-foreground">
                    <p className="mb-2 text-foreground font-semibold">Compare the original</p>
                    <p className="mb-4 text-xs leading-relaxed">
                      This still was extracted from your original uploaded file at the segment midpoint. It is not an AI-generated image.
                    </p>
                    <div className="p-3 bg-muted/50 border rounded-sm">
                      <p className="text-xs flex items-center gap-2 mb-1">
                        <span className="w-2 h-2 rounded-full bg-yellow-500 inline-block" />
                        Timeline is UNVERIFIED.
                      </p>
                      <p className="text-[10px] opacity-70">
                        Compare this moment with YouTube before treating the timelines as aligned. A matching title or duration alone is not enough.
                      </p>
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>
          )}

          {/* Ingestion Checks */}
          <Card className="bg-transparent border-none shadow-none">
            <CardHeader className="p-0 pb-3">
              <CardTitle className="text-sm uppercase tracking-wider text-muted-foreground">System Checks</CardTitle>
            </CardHeader>
            <CardContent className="p-0 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2">
              {proof.checks.map(check => (
                <div 
                  key={check.id} 
                  className={cn(
                    "p-3 border rounded-sm flex flex-col gap-1 text-xs",
                    check.status === 'passed' ? 'border-primary/50 bg-primary/5' :
                    check.status === 'failed' ? 'border-destructive/50 bg-destructive/5' :
                    'border-border bg-muted/10'
                  )}
                >
                  <div className="flex items-center justify-between font-bold">
                    <span className="truncate pr-2">{check.label}</span>
                    {check.status === 'passed' && <CheckCircle2 className="size-3 text-primary shrink-0" />}
                    {check.status === 'failed' && <AlertCircle className="size-3 text-destructive shrink-0" />}
                    {check.status === 'pending' && <Loader2 className="size-3 text-yellow-500 animate-spin shrink-0" />}
                    {check.status === 'unverified' && <span className="text-[10px] text-muted-foreground shrink-0">[?]</span>}
                  </div>
                  <div className="text-[10px] text-muted-foreground line-clamp-2" title={check.detail}>
                    {check.detail}
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>
        </div>

        {/* Right Column: Search & Results */}
        <div className="lg:col-span-5 flex flex-col gap-6">
          <Card className="border-primary/30 shadow-[0_0_15px_rgba(0,255,65,0.05)]">
            <CardHeader className="p-4 border-b border-border/50">
              <CardTitle className="text-lg font-sans flex items-center gap-2">
                <Search className="size-5 text-primary" />
                QUERY_TERMINAL
              </CardTitle>
            </CardHeader>
            <CardContent className="p-4">
              <form onSubmit={handleSearch} className="flex flex-col gap-3">
                <div className="relative">
                  <div className="absolute left-3 top-3 text-primary">
                    &gt;
                  </div>
                  <Input
                    type="text"
                    placeholder="Enter natural language query..."
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    disabled={!isReady || searchMutation.isPending}
                    className="pl-8 h-12 text-base font-sans bg-black border-primary/50 focus-visible:ring-primary focus-visible:border-primary placeholder:font-mono placeholder:text-muted-foreground/50"
                  />
                </div>
                
                <div className="flex gap-2">
                  <div className="flex bg-muted/20 border border-border/50 rounded-sm overflow-hidden shrink-0">
                    {(['both', 'visual', 'audio'] as const).map((m) => (
                      <button
                        key={m}
                        type="button"
                        onClick={() => setModality(m)}
                        className={cn(
                          "px-3 py-0 h-12 text-xs uppercase tracking-wider font-mono transition-colors",
                          modality === m
                            ? "bg-primary text-black font-bold"
                            : "text-muted-foreground hover:text-primary hover:bg-muted/40"
                        )}
                      >
                        {m}
                      </button>
                    ))}
                  </div>
                  <Button 
                    type="submit" 
                    disabled={!isReady || searchMutation.isPending || !query.trim()}
                    className="flex-1 h-12"
                  >
                    {searchMutation.isPending ? (
                      <><Loader2 className="size-4 mr-2 animate-spin" /> SEARCHING...</>
                    ) : (
                      <>EXECUTE_QUERY</>
                    )}
                  </Button>
                </div>
                <p className="text-[10px] text-muted-foreground mt-1 flex items-start gap-1">
                  <AlertCircle className="size-3 shrink-0" />
                  Queries are saved to this shared proof. Do not enter private information.
                </p>
                
                {/* Suggestions - these are clearly marked as prompts, not precomputed claims */}
                <div className="mt-2">
                  <div className="text-[10px] uppercase text-muted-foreground mb-2 tracking-wider">Suggested Prompts:</div>
                  <div className="flex flex-wrap gap-2">
                    {["someone opening a door", "loud explosion", "creepy hallway"].map((prompt, i) => (
                      <button
                        key={i}
                        type="button"
                        onClick={() => setQuery(prompt)}
                        className="text-xs px-2 py-1 border border-border/50 bg-muted/20 hover:border-primary hover:text-primary transition-colors text-left"
                      >
                        "{prompt}"
                      </button>
                    ))}
                  </div>
                </div>
              </form>
            </CardContent>
          </Card>

          {/* Active Search Results */}
          <Card className="flex-1 flex flex-col min-h-[300px]">
            <CardHeader className="p-4 border-b border-border/50 py-3 bg-muted/10">
              <CardTitle className="text-sm uppercase tracking-wider flex items-center justify-between">
                <span>Results</span>
                {activeSearch && (
                  <span className="text-xs text-muted-foreground font-normal normal-case flex items-center gap-2">
                    <span className="w-1.5 h-1.5 rounded-full bg-primary" />
                    {activeSearch.latencyMs}ms latency
                  </span>
                )}
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0 flex-1 overflow-auto max-h-[500px]">
              {!activeSearch ? (
                <div className="h-full min-h-[200px] flex flex-col items-center justify-center text-muted-foreground p-6 text-center">
                  <Search className="size-8 mb-3 opacity-20" />
                  <p className="text-sm uppercase tracking-widest">Awaiting Input</p>
                </div>
              ) : (
                <div className="flex flex-col">
                  <div className="p-4 border-b bg-black/40">
                    <p className="text-sm font-sans font-medium mb-1 break-words line-clamp-2">"{activeSearch.query}"</p>
                    <div className="flex items-center justify-between text-xs text-muted-foreground">
                      <span>Found {activeSearch.matches.length} matches</span>
                      <span className="uppercase">{activeSearch.provider} • {activeSearch.modality}</span>
                    </div>
                  </div>
                  
                  {activeSearch.matches.length === 0 ? (
                    <div className="p-8 text-center text-muted-foreground">
                      <p className="mb-2">NO_MATCHES_FOUND</p>
                      <p className="text-xs">The model did not find any segments matching this query with sufficient confidence.</p>
                    </div>
                  ) : (
                    <div className="flex flex-col divide-y divide-border/30">
                      {activeSearch.matches.map((match) => (
                        <button
                          key={`${activeSearch.id}-${match.rank}`}
                          onClick={() => setSelectedMatch(match)}
                          className={cn(
                            "flex items-start gap-3 p-3 hover:bg-primary/5 transition-colors text-left group",
                            selectedMatch?.rank === match.rank && selectedMatch?.startSeconds === match.startSeconds 
                              ? "bg-primary/10 border-l-2 border-l-primary" 
                              : "border-l-2 border-l-transparent"
                          )}
                        >
                          <div className="flex flex-col items-center justify-center bg-black border w-12 h-10 shrink-0 font-bold text-xs group-hover:border-primary/50 transition-colors">
                            <span className="text-[10px] text-muted-foreground mb-0.5">RNK</span>
                            {match.rank}
                          </div>
                          
                          <div className="flex-1 min-w-0 flex flex-col justify-center">
                            <div className="flex items-center gap-2 mb-1">
                              <span className="text-sm font-bold text-foreground">
                                {formatTime(match.startSeconds)}
                              </span>
                              <span className="text-muted-foreground text-xs">-</span>
                              <span className="text-sm text-muted-foreground">
                                {formatTime(match.endSeconds)}
                              </span>
                            </div>
                            <div className="flex items-center gap-2">
                              {match.confidenceLabel && (
                                <Badge variant="outline" className="text-[9px] h-4 px-1 py-0 border-border/50 text-muted-foreground bg-transparent">
                                  {match.confidenceLabel}
                                </Badge>
                              )}
                              <span className="text-[10px] text-muted-foreground group-hover:text-primary transition-colors flex items-center">
                                <Play className="size-3 mr-1" /> Play Segment
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
          </Card>

          {/* History */}
          <Card>
            <CardHeader className="p-3 border-b border-border/50 py-2 bg-muted/10">
              <CardTitle className="text-xs uppercase tracking-wider flex items-center gap-2 text-muted-foreground">
                <History className="size-3" />
                History Buffer
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0 overflow-hidden">
              {isHistoryLoading ? (
                <div className="p-4 text-center text-xs text-muted-foreground flex justify-center">
                  <Loader2 className="size-3 animate-spin" />
                </div>
              ) : !searchHistory || searchHistory.length === 0 ? (
                <div className="p-4 text-center text-xs text-muted-foreground opacity-50">
                  Buffer empty
                </div>
              ) : (
                <div className="flex flex-col max-h-[200px] overflow-y-auto divide-y divide-border/20">
                  {searchHistory.map((search) => (
                    <button
                      key={search.id}
                      onClick={() => handleSelectHistory(search)}
                      className={cn(
                        "text-left p-3 text-xs hover:bg-muted/30 transition-colors flex flex-col gap-1",
                        activeSearchId === search.id ? "bg-muted/20" : ""
                      )}
                    >
                      <div className="font-sans font-medium text-foreground line-clamp-1">"{search.query}"</div>
                      <div className="flex justify-between items-center text-muted-foreground text-[10px] font-mono">
                        <span>{search.matches.length} matches</span>
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
