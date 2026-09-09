import { VideoImportSourceKind } from '@workspace/api-client-react';

export function parseSourceKind(url: string): { kind: VideoImportSourceKind, error?: string } {
  try {
    const parsed = new URL(url);
    if (!['https:', 'http:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.port)
      return { kind: 'file', error: 'Use a public video URL without credentials or custom ports.' };
    const host = parsed.hostname.toLowerCase().replace(/^www\./, '');
    
    if (host === 'youtube.com' || host === 'm.youtube.com' || host === 'youtu.be') {
      return { kind: 'youtube' };
    }
    if (host === 'x.com' || host === 'twitter.com' || host === 'mobile.twitter.com') {
      return { kind: 'x' };
    }
    if (host === 'tiktok.com' || host === 'vm.tiktok.com' || host === 'vt.tiktok.com' || host === 'm.tiktok.com') {
      return { kind: 'tiktok' };
    }
    if (host === 'vimeo.com') {
      return { kind: 'vimeo' };
    }
    
    return { kind: 'file', error: 'Unsupported URL. Only YouTube, X, TikTok, and Vimeo are supported.' };
  } catch (e) {
    return { kind: 'file', error: 'Invalid URL format.' };
  }
}
