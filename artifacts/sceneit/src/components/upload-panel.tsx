import { useState, useRef, useEffect } from 'react';
import { UploadCloud, FileVideo, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { useToast } from '@/hooks/use-toast';
import { uploadFileXHR } from '@/lib/upload-utils';
import { useAuth } from '@workspace/replit-auth-web';
import { importError } from '@/lib/import-errors';
import { 
  useReserveImportUpload, 
  useCompleteImportUpload,
  type ImportConfig 
} from '@workspace/api-client-react';

interface UploadPanelProps {
  importId: string;
  config?: ImportConfig;
  onSuccess: () => void;
  onError?: (err: any) => void;
  cancellationSignal?: AbortSignal;
  cancellationPending?: boolean;
  onCancel?: () => void;
}

export function UploadPanel({
  importId, config, onSuccess, onError, cancellationSignal, cancellationPending, onCancel,
}: UploadPanelProps) {
  const { toast } = useToast();
  const { csrfToken } = useAuth();
  
  const [file, setFile] = useState<File | null>(null);
  const [isProcessing, setIsProcessing] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [transferred, setTransferred] = useState(false);
  const [error, setError] = useState('');
  
  const fileInputRef = useRef<HTMLInputElement>(null);
  const activeUpload = useRef<AbortController | null>(null);
  const stopped = !!cancellationSignal?.aborted;

  useEffect(() => {
    const stop = () => {
      activeUpload.current?.abort();
      setIsProcessing(false);
      setError('');
    };
    cancellationSignal?.addEventListener('abort', stop);
    if (cancellationSignal?.aborted) stop();
    return () => {
      cancellationSignal?.removeEventListener('abort', stop);
      const active = activeUpload.current;
      activeUpload.current = null;
      active?.abort();
    };
  }, [cancellationSignal, importId]);

  const reserveUpload = useReserveImportUpload({
    request: { headers: { 'X-CSRF-Token': csrfToken || '' } }
  });
  
  const completeUpload = useCompleteImportUpload({
    request: { headers: { 'X-CSRF-Token': csrfToken || '' } }
  });

  const handleFileDrop = (e: React.DragEvent) => {
    e.preventDefault();
    if (isProcessing || stopped) return;
    if (e.dataTransfer.files.length !== 1) {
      toast({ title: 'Choose one MP4', description: 'Batch uploads are not supported.', variant: 'destructive' });
      return;
    }
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      validateAndSetFile(e.dataTransfer.files[0]);
    }
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      validateAndSetFile(e.target.files[0]);
    }
  };

  const validateAndSetFile = (f: File) => {
    if (isProcessing || stopped) return;
    // Some browsers on Linux may provide an empty string for type
    if (f.type && f.type !== 'video/mp4') {
      toast({ title: 'Invalid format', description: 'Only MP4 files are supported.', variant: 'destructive' });
      return;
    }
    if (!f.name.toLowerCase().endsWith('.mp4')) {
      toast({ title: 'Invalid format', description: 'Only files ending in .mp4 are supported.', variant: 'destructive' });
      return;
    }

    const maxBytes = config?.maxBytes || 200000000;
    if (!f.size || f.size > maxBytes) {
      toast({ title: 'File too large', description: `Maximum size is ${Math.round(maxBytes / 1000000)}MB.`, variant: 'destructive' });
      return;
    }
    setFile(f);
    setTransferred(false);
    setError('');
  };

  const handleUpload = async () => {
    if (!file || activeUpload.current || stopped || cancellationPending) return;
    const controller = new AbortController();
    activeUpload.current = controller;
    try {
      setIsProcessing(true);
      setError('');
      if (!transferred) {
        setUploadProgress(0);
        const reservation = await reserveUpload.mutateAsync({
          importId,
          data: {
            fileName: file.name,
            sizeBytes: file.size,
            contentType: 'video/mp4'
          }
        });
        // Reservation writes may finish after Cancel. Never start their transfer.
        if (controller.signal.aborted) return;
        await uploadFileXHR(
          reservation.uploadURL, 
          reservation.method, 
          reservation.headers, 
          file, 
          (percent) => setUploadProgress(percent),
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setTransferred(true);
      }
      if (controller.signal.aborted) return;
      await completeUpload.mutateAsync({ importId, data: {} });
      if (!controller.signal.aborted) onSuccess();
    } catch (err: any) {
      if (controller.signal.aborted) return;
      setError(importError(err));
      if (onError) onError(err);
    } finally {
      if (activeUpload.current === controller) {
        activeUpload.current = null;
        setIsProcessing(false);
      }
    }
  };

  return (
    <div className="flex flex-col gap-6 w-full">
      {error && <p role="alert" className="text-destructive">{error} Retry this upload or reselect the file; the import is not complete.</p>}
      <div 
        className={`border-2 border-dashed ${file ? 'border-primary bg-primary/5' : 'border-border'} rounded-sm p-8 md:p-12 text-center transition-colors flex flex-col items-center justify-center cursor-pointer hover:border-primary/50 hover:bg-muted/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary`}
        onDragOver={(e) => e.preventDefault()}
        onDrop={handleFileDrop}
        onClick={() => { if (!isProcessing && !stopped) fileInputRef.current?.click(); }}
        role="button"
        tabIndex={isProcessing || stopped ? -1 : 0}
        aria-disabled={isProcessing || stopped}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            if (!isProcessing && !stopped) fileInputRef.current?.click();
          }
        }}
        aria-label="Upload MP4 file"
      >
        <input 
          type="file" 
          ref={fileInputRef} 
          className="hidden" 
          accept="video/mp4,.mp4" 
          onChange={handleFileSelect}
          disabled={isProcessing || stopped}
        />
        {file ? (
          <>
            <FileVideo className="size-12 text-primary mb-4" />
            <p className="text-lg font-bold font-sans">{file.name}</p>
            <p className="text-sm text-muted-foreground mt-1">
              {(file.size / 1000000).toFixed(2)} MB
            </p>
          </>
        ) : (
          <>
            <UploadCloud className="size-12 text-muted-foreground mb-4 opacity-50" />
            <p className="text-lg font-bold font-sans">DRAG_AND_DROP_MP4</p>
            <p className="text-sm text-muted-foreground mt-2">
              or click to browse. Max {config ? Math.round(config.maxBytes / 1000000) : 200}MB. (H264 AAC or silent)
            </p>
          </>
        )}
      </div>

      {isProcessing && (
        <div className="flex flex-col gap-2" role="status">
          <div className="flex justify-between text-xs font-mono">
            <span>{uploadProgress === 100 ? 'TRANSFER SENT · AWAITING SERVER VALIDATION' : 'UPLOADING FILE…'}</span>
            <span>{uploadProgress}%</span>
          </div>
          <Progress value={uploadProgress} className="h-2 rounded-none bg-muted" />
        </div>
      )}

      <Button 
        onClick={handleUpload} 
        disabled={!file || isProcessing || stopped || cancellationPending}
        className="w-full h-12 font-bold tracking-wider"
      >
        {stopped ? 'UPLOAD STOPPED' : isProcessing ? (
          <><Loader2 className="size-5 animate-spin mr-2" /> PROCESSING UPLOAD...</>
        ) : (
          <>{transferred ? 'RETRY COMPLETION' : error ? 'RETRY UPLOAD' : 'UPLOAD FILE'}</>
        )}
      </Button>
      {onCancel && <Button type="button" variant="outline" onClick={onCancel} disabled={cancellationPending}>
        {cancellationPending ? 'Requesting cancellation…' : stopped ? 'Retry cancellation' : 'Cancel upload'}
      </Button>}
    </div>
  );
}