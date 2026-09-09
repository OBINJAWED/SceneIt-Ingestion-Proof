import { useState, useRef, useEffect } from 'react';
import { CheckCircle2, FileVideo, Loader2, ShieldCheck, UploadCloud } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { useToast } from '@/hooks/use-toast';
import { uploadFileXHR } from '@/lib/upload-utils';
import { useAuth } from '@workspace/replit-auth-web';
import { useQueryClient } from '@tanstack/react-query';
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
  reservationAllowed?: boolean;
}

export function UploadPanel({
  importId, config, onSuccess, onError, cancellationSignal, cancellationPending, onCancel, reservationAllowed = true,
}: UploadPanelProps) {
  const { toast } = useToast();
  const { csrfToken } = useAuth();
  const cache = useQueryClient();

  const [file, setFile] = useState<File | null>(null);
  const [isProcessing, setIsProcessing] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [transferred, setTransferred] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState('');

  const fileInputRef = useRef<HTMLInputElement>(null);
  const activeUpload = useRef<AbortController | null>(null);
  const stopped = !!cancellationSignal?.aborted;
  const fileSelectionDisabled = isProcessing || confirmed || stopped || !!cancellationPending || (!reservationAllowed && !transferred);

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
    request: { headers: { 'X-CSRF-Token': csrfToken || '' } },
    mutation: { retry: false },
  });

  const completeUpload = useCompleteImportUpload({
    request: { headers: { 'X-CSRF-Token': csrfToken || '' } },
    mutation: { retry: false },
  });

  const handleFileDrop = (e: React.DragEvent) => {
    e.preventDefault();
    if (fileSelectionDisabled) return;
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
    if (fileSelectionDisabled) return;
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
    if (!file || activeUpload.current || fileSelectionDisabled) return;
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
        cache.setQueryData(['/api/imports', importId], reservation.import);
        void cache.invalidateQueries({ queryKey: ['/api/auth/session'] });
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
      if (!controller.signal.aborted) {
        setConfirmed(true);
        onSuccess();
      }
    } catch (err: any) {
      if (controller.signal.aborted) return;
      setError(importError(err));
      void cache.invalidateQueries({ queryKey: ['/api/auth/session'] });
      if (onError) onError(err);
    } finally {
      if (activeUpload.current === controller) {
        activeUpload.current = null;
        setIsProcessing(false);
      }
    }
  };

  return (
    <div className="flex w-full flex-col gap-5">
      {!reservationAllowed && !transferred && <p role="status" className="rounded-lg border p-4 text-sm text-muted-foreground">
        A new upload cannot be reserved with your current access or remaining allowance. Existing uploads can still be completed or cancelled.
      </p>}
      {error && <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm leading-6 text-destructive">
        <p className="break-words">{error}</p>
        <p className="mt-1">Retry below or select the file again. This import is not complete yet.</p>
      </div>}
      <div
        className={`flex min-h-52 flex-col items-center justify-center rounded-xl border border-dashed p-6 text-center outline-none transition-colors sm:p-10 ${fileSelectionDisabled ? 'cursor-default opacity-70' : 'cursor-pointer'} ${file ? 'border-primary/60 bg-primary/5' : 'border-border bg-background/40 hover:border-primary/50 hover:bg-primary/5'} focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background`}
        onDragOver={(e) => e.preventDefault()}
        onDrop={handleFileDrop}
        onClick={() => { if (!fileSelectionDisabled) fileInputRef.current?.click(); }}
        role="button"
        tabIndex={fileSelectionDisabled ? -1 : 0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            if (!fileSelectionDisabled) fileInputRef.current?.click();
          }
        }}
        aria-label={file
          ? `Selected file ${file.name}.${fileSelectionDisabled ? '' : ' Activate to choose a different MP4.'}`
          : 'Choose an MP4 file'}
        aria-disabled={fileSelectionDisabled}
        data-testid="dropzone-video-file"
      >
        <input
          type="file"
          ref={fileInputRef}
          className="hidden"
          accept="video/mp4,.mp4"
          onChange={handleFileSelect}
          disabled={fileSelectionDisabled}
        />
        {file ? (
          <>
            <div className="mb-4 grid size-12 place-items-center rounded-lg border border-primary/30 bg-primary/10 text-primary">
              <FileVideo className="size-6" aria-hidden="true" />
            </div>
            <p className="max-w-full break-all text-base font-semibold" data-testid="text-selected-filename">{file.name}</p>
            <p className="mt-1 text-sm text-muted-foreground">
              {(file.size / 1000000).toFixed(2)} MB
            </p>
            {!fileSelectionDisabled && <p className="mt-3 text-xs text-muted-foreground">Click or press Enter to choose a different file</p>}
          </>
        ) : (
          <>
            <div className="mb-4 grid size-12 place-items-center rounded-lg border border-border bg-card text-muted-foreground">
              <UploadCloud className="size-6" aria-hidden="true" />
            </div>
            <p className="text-base font-semibold">Drop your MP4 here</p>
            <p className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">
              Or click to browse. Maximum {config ? Math.round(config.maxBytes / 1000000) : 200} MB. H.264 video with AAC audio, or silent.
            </p>
          </>
        )}
      </div>

      <div className="flex items-start gap-3 rounded-lg border border-border/70 bg-background/40 p-4 text-xs leading-5 text-muted-foreground">
        <ShieldCheck className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden="true" />
        <p>Your file remains private to your account. Transfer is followed by server validation; selecting or sending a file does not complete the import.</p>
      </div>

      {isProcessing && (
        <div className="flex flex-col gap-2" role="status" aria-live="polite">
          <div className="flex flex-wrap justify-between gap-2 text-xs">
            <span className="font-medium text-foreground">{uploadProgress === 100 ? 'Transfer sent. Confirming with the server…' : 'Transferring private file…'}</span>
            <span className="tabular-nums text-muted-foreground">{uploadProgress}%</span>
          </div>
          <Progress value={uploadProgress} className="h-2" />
          <p className="text-xs text-muted-foreground">Keep this page open until confirmation finishes.</p>
        </div>
      )}

      {confirmed && !stopped && <div role="status" className="flex items-start gap-3 rounded-lg border border-primary/30 bg-primary/10 p-4 text-sm">
        <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden="true" />
        <p>The server confirmed your upload. Updating the analysis status… You do not need to send the file again.</p>
      </div>}
      {transferred && !isProcessing && !confirmed && !stopped && <div className="flex items-start gap-3 rounded-lg border border-amber-400/25 bg-amber-400/5 p-4 text-sm">
        <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden="true" />
        <p>The file transfer finished, but server completion still needs confirmation. Retrying completion will not send the file again.</p>
      </div>}

      <Button
        onClick={handleUpload}
        disabled={!file || fileSelectionDisabled}
        className="h-11 w-full"
        data-testid="button-upload-file"
      >
        {stopped ? 'Upload stopped' : confirmed ? 'Upload confirmed' : isProcessing ? (
          <><Loader2 className="mr-2 size-4 animate-spin" aria-hidden="true" /> {uploadProgress === 100 ? 'Confirming upload…' : 'Uploading…'}</>
        ) : (
          <>{transferred ? 'Retry server confirmation' : error ? 'Retry upload' : 'Upload private MP4'}</>
        )}
      </Button>
      {onCancel && <Button type="button" variant="outline" onClick={onCancel} disabled={cancellationPending}
        className="h-auto min-h-11 w-full whitespace-normal" data-testid="button-cancel-upload">
        {cancellationPending ? 'Requesting cancellation…' : stopped ? 'Retry cancellation' : 'Cancel upload'}
      </Button>}
    </div>
  );
}
