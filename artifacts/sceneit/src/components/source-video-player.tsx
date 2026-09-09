import { useEffect, useRef, useState } from 'react';
import { RotateCcw, ExternalLink } from 'lucide-react';
import { Button } from './ui/button';
import { cn, formatTime } from '@/lib/utils';

interface SourceVideoPlayerProps {
  src: string;
  youtubeUrl: string;
  startSeconds?: number;
  endSeconds?: number;
  className?: string;
  onUseYouTube?: () => void;
}

export function SourceVideoPlayer({
  src, youtubeUrl, startSeconds, endSeconds, className, onUseYouTube,
}: SourceVideoPlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [loopEnabled, setLoopEnabled] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [error, setError] = useState(false);

  useEffect(() => { setError(false); }, [src]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || startSeconds === undefined) return;
    const seek = () => { video.currentTime = startSeconds; };
    if (video.readyState >= 1) seek();
    else video.addEventListener('loadedmetadata', seek, { once: true });
  }, [startSeconds]);

  const onTimeUpdate = () => {
    const video = videoRef.current;
    if (!video) return;
    setCurrentTime(video.currentTime);
    // timeupdate only fires while time advances, so a paused video stays paused.
    if (loopEnabled && endSeconds !== undefined && video.currentTime >= endSeconds) {
      video.currentTime = startSeconds ?? 0;
      if (!video.paused) void video.play();
    }
  };

  return (
    <div className={cn('flex min-w-0 flex-col gap-3', className)}>
      <div className="border-b bg-muted/30 px-4 py-3 text-xs leading-relaxed">
        <strong className="font-medium text-foreground">Original source playback</strong>
        <span className="text-muted-foreground"> — this is the indexed original, not verification of the YouTube edit.</span>
      </div>
      <video
        ref={videoRef}
        src={src}
        controls
        playsInline
        preload="metadata"
        onTimeUpdate={onTimeUpdate}
        aria-label="Indexed original video"
        onError={() => setError(true)}
        className="aspect-video w-full bg-black"
      />
      {error && <p role="alert" className="mx-3 rounded-lg border border-destructive/25 bg-destructive/5 p-3 text-sm text-destructive">
        The original video couldn’t play here. Your scene results and source frames are still available.
        You can open the timestamp on YouTube below; its edit is a separate source.
      </p>}
      <div className="flex flex-wrap items-center justify-between gap-2 px-3 pb-3">
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant={loopEnabled ? 'default' : 'outline'}
            size="sm"
            disabled={endSeconds === undefined}
            onClick={() => setLoopEnabled(value => !value)}
            aria-pressed={loopEnabled}
            className="min-h-11"
          >
            <RotateCcw className="mr-2 size-4" />
            {loopEnabled ? 'Loop enabled' : 'Loop segment'}
          </Button>
          {startSeconds !== undefined && (
            <Button variant="ghost" size="sm" className="min-h-11" onClick={() => {
              if (videoRef.current) videoRef.current.currentTime = startSeconds;
            }}>
              Restart at {formatTime(startSeconds)}
            </Button>
          )}
        </div>
        <Button variant="ghost" size="sm" className="min-h-11" asChild>
          <a href={youtubeUrl} target="_blank" rel="noreferrer">
            <ExternalLink className="size-3" /> Open on YouTube
          </a>
        </Button>
        {onUseYouTube && (
          <Button variant="outline" size="sm" className="min-h-11" onClick={onUseYouTube}>
            View YouTube player
          </Button>
        )}
      </div>
      <span className="sr-only">Current source time {formatTime(currentTime)}</span>
    </div>
  );
}