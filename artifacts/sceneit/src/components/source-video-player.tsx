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
    <div className={cn('flex flex-col gap-3', className)}>
      <div className="border-b border-primary/30 bg-primary/5 px-3 py-2 text-xs">
        <strong className="text-primary">ORIGINAL SOURCE PLAYBACK</strong>
        <span className="text-muted-foreground"> — this is the indexed original, not verification of the YouTube edit.</span>
      </div>
      <video
        ref={videoRef}
        src={src}
        controls
        playsInline
        preload="metadata"
        onTimeUpdate={onTimeUpdate}
        className="aspect-video w-full bg-black"
      />
      <div className="flex flex-wrap items-center justify-between gap-2 px-3 pb-3">
        <div className="flex items-center gap-2">
          <Button
            variant={loopEnabled ? 'default' : 'outline'}
            size="sm"
            disabled={endSeconds === undefined}
            onClick={() => setLoopEnabled(value => !value)}
            className="rounded-none"
          >
            <RotateCcw className="mr-2 size-4" />
            {loopEnabled ? 'SEGMENT LOOP ON' : 'LOOP SEGMENT'}
          </Button>
          {startSeconds !== undefined && (
            <Button variant="ghost" size="sm" className="rounded-none" onClick={() => {
              if (videoRef.current) videoRef.current.currentTime = startSeconds;
            }}>
              REWIND TO {formatTime(startSeconds)}
            </Button>
          )}
        </div>
        <Button variant="ghost" size="sm" asChild>
          <a href={youtubeUrl} target="_blank" rel="noreferrer">
            <ExternalLink className="mr-2 size-3" /> OPEN YOUTUBE TIMESTAMP
          </a>
        </Button>
        {onUseYouTube && (
          <Button variant="outline" size="sm" className="rounded-none" onClick={onUseYouTube}>
            VIEW YOUTUBE PLAYER
          </Button>
        )}
      </div>
      <span className="sr-only">Current source time {formatTime(currentTime)}</span>
    </div>
  );
}