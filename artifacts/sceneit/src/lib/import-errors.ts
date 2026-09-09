export function importError(error: unknown): string {
  const value = error as { data?: { error?: { message?: string } | string; message?: string }; message?: string };
  const detail = value?.data?.error;
  return (typeof detail === 'string' ? detail : detail?.message)
    || value?.data?.message || value?.message || 'The request could not complete. Please retry.';
}