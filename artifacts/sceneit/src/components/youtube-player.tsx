import { useEffect, useRef, useState, useCallback } from 'react';
import { cn, formatTime } from '@/lib/utils';
import { Button } from './ui/button';
import { Play, Pause, ExternalLink, RotateCcw } from 'lucide-react';
import { Tooltip, TooltipContent, TooltipTrigger } from './ui/tooltip';

declare global {
  interface Window {
    YT: any;
    onYouTubeIframeAPIReady: () => void;
  }
}

interface YouTubePlayerProps {
  videoId: string;
  startSeconds?: number;
  endSeconds?: number;
  className?: string;
  onReady?: () => void;
}

export function YouTubePlayer({
  videoId,
  startSeconds,
  endSeconds,
  className,
  onReady
}: YouTubePlayerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const playerRef = useRef<any>(null);
  const timerRef = useRef<number | null>(null);
  
  const [isReady, setIsReady] = useState(false);
  const [isPlaying, setIsPlaying] = useState(false);
  const [isError, setIsError] = useState(false);
  const [errorCode, setErrorCode] = useState<number | null>(null);
  const [loopEnabled, setLoopEnabled] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [localDuration, setLocalDuration] = useState(0);

  // Initialize YT API
  useEffect(() => {
    let mounted = true;

    if (!window.YT) {
      const tag = document.createElement('script');
      tag.src = 'https://www.youtube.com/iframe_api';
      const firstScriptTag = document.getElementsByTagName('script')[0];
      firstScriptTag.parentNode?.insertBefore(tag, firstScriptTag);

      window.onYouTubeIframeAPIReady = () => {
        if (mounted) initPlayer();
      };
    } else if (!playerRef.current) {
      initPlayer();
    }

    function initPlayer() {
      if (!containerRef.current) return;
      
      const playerId = `yt-player-${Math.random().toString(36).substr(2, 9)}`;
      containerRef.current.id = playerId;

      playerRef.current = new window.YT.Player(playerId, {
        videoId,
        playerVars: {
          autoplay: 0,
          controls: 1,
          rel: 0,
          modestbranding: 1,
          playsinline: 1,
          enablejsapi: 1,
          start: Math.floor(startSeconds || 0),
        },
        events: {
          onReady: (event: any) => {
            if (!mounted) return;
            setIsReady(true);
            setLocalDuration(event.target.getDuration());
            if (onReady) onReady();
          },
          onStateChange: (event: any) => {
            if (!mounted) return;
            
            // YT.PlayerState.PLAYING = 1, PAUSED = 2
            if (event.data === window.YT.PlayerState.PLAYING) {
              setIsPlaying(true);
            } else if (event.data === window.YT.PlayerState.PAUSED || event.data === window.YT.PlayerState.ENDED) {
              setIsPlaying(false);
            }
          },
          onError: (event: any) => {
            if (!mounted) return;
            console.error('YouTube Player Error:', event.data);
            setErrorCode(typeof event.data === 'number' ? event.data : null);
            setIsPlaying(false);
            setIsReady(false);
            setIsError(true);
          }
        }
      });
    }

    return () => {
      mounted = false;
      if (timerRef.current !== null) {
        cancelAnimationFrame(timerRef.current);
      }
      if (playerRef.current) {
        try {
          playerRef.current.destroy();
        } catch (e) {
          // ignore destroy errors
        }
        playerRef.current = null;
      }
    };
  }, [videoId]);

  // When segment boundaries change, seek to start if ready, but don't auto-play unless already playing
  useEffect(() => {
    if (isReady && playerRef.current && startSeconds !== undefined) {
      playerRef.current.seekTo(startSeconds, true);
    }
  }, [startSeconds, isReady]);

  // Loop checking loop
  const checkLoop = useCallback(() => {
    if (!playerRef.current || !isPlaying) {
      timerRef.current = requestAnimationFrame(checkLoop);
      return;
    }

    const current = playerRef.current.getCurrentTime();
    setCurrentTime(current);

    if (loopEnabled && endSeconds !== undefined && current >= endSeconds) {
      // Loop back to start
      playerRef.current.seekTo(startSeconds || 0, true);
    }
    timerRef.current = requestAnimationFrame(checkLoop);
  }, [isPlaying, loopEnabled, startSeconds, endSeconds]);

  useEffect(() => {
    timerRef.current = requestAnimationFrame(checkLoop);
    return () => {
      if (timerRef.current !== null) {
        cancelAnimationFrame(timerRef.current);
      }
    };
  }, [checkLoop]);

  const handlePlayPause = () => {
    if (!playerRef.current) return;
    if (isPlaying) {
      playerRef.current.pauseVideo();
    } else {
      playerRef.current.playVideo();
    }
  };

  const handleSeekStart = () => {
    if (!playerRef.current) return;
    playerRef.current.seekTo(startSeconds || 0, true);
  };

  if (isError) {
    return (
      <div className={cn("border border-destructive bg-destructive/10 p-6 flex flex-col items-center justify-center text-center", className)}>
        <p className="text-destructive mb-4" role="alert">
          {errorCode === 101 || errorCode === 150
            ? `YouTube refused embedded playback (error ${errorCode}). Open this segment on YouTube instead.`
            : errorCode === 153
              ? 'YouTube could not validate this player’s referrer (error 153). Open the timestamp directly instead.'
              : `Playback is unavailable in this player${errorCode ? ` (error ${errorCode})` : ''}. Open the segment directly instead.`}
        </p>
        <Button variant="outline" className="border-destructive text-destructive hover:bg-destructive hover:text-black" asChild>
          <a href={`https://youtu.be/${videoId}${startSeconds ? `?t=${Math.floor(startSeconds)}` : ''}`} target="_blank" rel="noreferrer">
            <ExternalLink className="mr-2" /> Open on YouTube
          </a>
        </Button>
      </div>
    );
  }

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <div className="relative w-full aspect-video border bg-black">
        <div ref={containerRef} className="absolute inset-0 w-full h-full" />
        {!isReady && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/80 z-10">
            <span className="animate-pulse text-muted-foreground">INITIALIZING_FEED...</span>
          </div>
        )}
      </div>
      
      <div className="flex flex-wrap items-center justify-between gap-2 px-1">
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="icon"
            onClick={handlePlayPause}
            disabled={!isReady}
            className="h-8 w-8 rounded-none border-primary text-primary hover:bg-primary hover:text-black"
          >
            {isPlaying ? <Pause className="size-4" /> : <Play className="size-4 ml-0.5" />}
          </Button>
          
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="outline"
                size="icon"
                onClick={() => setLoopEnabled(!loopEnabled)}
                disabled={!isReady || endSeconds === undefined}
                className={cn(
                  "h-8 w-8 rounded-none",
                  loopEnabled 
                    ? "bg-primary text-black border-primary hover:bg-primary/80" 
                    : "border-border text-muted-foreground hover:text-primary hover:border-primary"
                )}
              >
                <RotateCcw className="size-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              {loopEnabled ? "Disable segment loop" : "Enable approximate segment loop"}
            </TooltipContent>
          </Tooltip>

          {startSeconds !== undefined && (
            <Button
              variant="ghost"
              size="sm"
              onClick={handleSeekStart}
              disabled={!isReady}
              className="h-8 rounded-none text-xs"
            >
              [ REWIND_TO_START ]
            </Button>
          )}
        </div>

        <div className="flex items-center gap-4 text-xs text-muted-foreground font-mono">
          <span>
            {formatTime(currentTime)} / {formatTime(localDuration)}
          </span>
          <Button variant="ghost" size="sm" className="h-8 text-xs p-0 hover:bg-transparent hover:text-primary" asChild>
            <a href={`https://youtu.be/${videoId}${startSeconds ? `?t=${Math.floor(startSeconds)}` : ''}`} target="_blank" rel="noreferrer">
              <ExternalLink className="size-3 mr-1" />
              YT_FALLBACK
            </a>
          </Button>
        </div>
      </div>
    </div>
  );
}
