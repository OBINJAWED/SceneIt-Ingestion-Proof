import {
  AlertDialog, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
  AlertDialogCancel,
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';

interface CancelImportDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
  pending: boolean;
  upload: boolean;
  ready: boolean;
}

export function CancelImportDialog({
  open, onOpenChange, onConfirm, pending, upload, ready,
}: CancelImportDialogProps) {
  const action = ready ? 'Delete import' : upload ? 'Cancel upload' : 'Cancel import';
  return <AlertDialog open={open} onOpenChange={value => { if (!pending) onOpenChange(value); }}>
    <AlertDialogContent className="w-[calc(100%-2rem)] max-w-lg">
      <AlertDialogHeader>
        <AlertDialogTitle>{ready ? 'Delete this import?' : upload ? 'Cancel this upload?' : 'Cancel this import?'}</AlertDialogTitle>
        <AlertDialogDescription>
          {upload ? 'This stops the file transfer and cancels the import. ' : ''}
          Private media and saved results will be scheduled for deletion.
          In-flight processing may need to finish before cleanup completes.
          Used import and search allowances do not reset.
        </AlertDialogDescription>
      </AlertDialogHeader>
      <AlertDialogFooter>
        <AlertDialogCancel disabled={pending}>Keep {ready ? 'import' : upload ? 'uploading' : 'import'}</AlertDialogCancel>
        <Button variant="destructive" onClick={onConfirm} disabled={pending}>
          {pending ? 'Requesting cancellation…' : action}
        </Button>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>;
}