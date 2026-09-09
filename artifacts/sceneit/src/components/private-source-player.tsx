import { useEffect, useRef, useState } from 'react';
import { RotateCcw, ExternalLink } from 'lucide-react';
import { Button } from './ui/button';
import { cn, formatTime } from '@/lib/utils';

interface PrivateSourcePlayerProps {
  src: string;
  sourceUrl?: string | null;
  platformName?: string;
  startSeconds?: number;
  endSeconds?: number;
  className?: string;
  matchKey?: string; // Used to trigger rewind when selecting the same match again
}

export function PrivateSourcePlayer({
  src, sourceUrl, platformName, startSeconds, endSeconds, className, matchKey
}: PrivateSourcePlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [loopEnabled, setLoopEnabled] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [error, setError] = useState(false);
  const [unsupported, setUnsupported] = useState(false);
  const playIntent = useRef(false);

  useEffect(() => {
    const probe = document.createElement('video');
    setUnsupported(probe.canPlayType('video/mp4; codecs="avc1.42E01E"') === '');
    setError(false);
    playIntent.current = false;
    setLoopEnabled(false);
  }, [src]);

  function seekToStart() {
    const video = videoRef.current;
    if (!video || !Number.isFinite(video.duration) || !Number.isFinite(startSeconds)
      || startSeconds! < 0 || startSeconds! >= video.duration) return;
    video.currentTime = startSeconds!;
  }

  useEffect(() => {
    const video = videoRef.current;
    if (!video || startSeconds === undefined) return;
    
    const seek = seekToStart;
    
    if (video.readyState >= 1) {
      seek();
    } else {
      video.addEventListener('loadedmetadata', seek, { once: true });
    }
    return () => video.removeEventListener('loadedmetadata', seek);
  }, [src, startSeconds, matchKey]);

  const onTimeUpdate = () => {
    const video = videoRef.current;
    if (!video) return;
    setCurrentTime(video.currentTime);
    
    // timeupdate only fires while time advances, so a paused video stays paused.
    if (loopEnabled && !video.paused && !video.seeking && Number.isFinite(endSeconds)
        && endSeconds! > (startSeconds ?? 0) && endSeconds! <= video.duration
        && video.currentTime >= endSeconds!) {
      seekToStart();
    }
  };

  if (unsupported) return <div className="space-y-3 border p-4">
    <p role="alert">This browser does not support H.264 MP4 playback. Try a current Safari, Chrome, or Edge browser.
      Your analysis and timestamps are still available.</p>
    <a className="text-sm text-primary underline" href={src} download="private-source.mp4">Download your authorized private MP4</a>
  </div>;

  return (
    <div className={cn('flex flex-col gap-3', className)}>
      <div className="border-b border-primary/30 bg-primary/5 px-3 py-2 flex items-center justify-between">
        <div className="text-xs">
          <strong className="text-primary tracking-wider font-mono">PRIVATE_SOURCE_PLAYBACK</strong>
          <span className="text-muted-foreground ml-2 hidden sm:inline">— Indexed original file</span>
        </div>
      </div>
      
      <video
        ref={videoRef}
        src={src}
        controls
        playsInline
        preload="metadata"
        onTimeUpdate={onTimeUpdate}
        onPlay={() => { playIntent.current = true; }}
        onPause={() => {
          if (!videoRef.current?.ended) playIntent.current = false;
        }}
        onEnded={() => {
          const video = videoRef.current;
          if (video && loopEnabled && playIntent.current && Number.isFinite(startSeconds)) {
            seekToStart();
            void video.play().catch(() => { playIntent.current = false; setError(true); });
          }
        }}
        className="aspect-video w-full bg-black border border-border/30 shadow-inner"
        onError={() => setError(true)}
      />
      {error && <p role="alert" className="px-3 text-sm text-destructive">
        Private playback could not load or decode this MP4. Try a supported browser or check your session and permission.
        <a className="ml-2 underline" href={src} download="private-source.mp4">Download your authorized source</a>
      </p>}
      
      <div className="flex flex-wrap items-center justify-between gap-2 px-3 pb-3">
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant={loopEnabled ? 'default' : 'outline'}
            size="sm"
            disabled={endSeconds === undefined}
            onClick={() => setLoopEnabled(value => !value)}
            className="rounded-none font-mono text-[10px] tracking-wider h-8"
          >
            <RotateCcw className="mr-2 size-3" />
            {loopEnabled ? 'LOOP: ON' : 'LOOP_SEGMENT'}
          </Button>
          
          {startSeconds !== undefined && (
            <Button 
              type="button"
              variant="ghost" 
              size="sm" 
              className="rounded-none font-mono text-[10px] tracking-wider h-8 border border-transparent hover:border-primary/30" 
              onClick={seekToStart}
            >
              REWIND
            </Button>
          )}
        </div>
        
        {sourceUrl && platformName && (
          <Button type="button" variant="ghost" size="sm" className="h-8 text-[10px] font-mono tracking-wider hover:text-primary" asChild>
            <a href={sourceUrl} target="_blank" rel="noreferrer">
              <ExternalLink className="mr-2 size-3" /> OPEN {platformName.toUpperCase()}
            </a>
          </Button>
        )}
      </div>
      <span className="sr-only">Current source time {formatTime(currentTime)}</span>
    </div>
  );
}