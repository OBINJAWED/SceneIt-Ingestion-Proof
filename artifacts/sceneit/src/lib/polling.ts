const BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 15_000, 20_000, 25_000] as const;
export const STATUS_POLL_BUDGET_MS = 75_000;

export interface StatusPoller {
  next(terminal: boolean, now?: number): number | false;
  reset(now?: number): void;
}

export function createStatusPoller(startedAt = Date.now()): StatusPoller {
  let start = startedAt;
  let attempt = 0;
  return {
    next(terminal, now = Date.now()) {
      if (terminal || now - start >= STATUS_POLL_BUDGET_MS) return false;
      const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
      attempt += 1;
      return Math.min(delay, STATUS_POLL_BUDGET_MS - (now - start));
    },
    reset(now = Date.now()) {
      start = now;
      attempt = 0;
    },
  };
}

export function isPilotAllowed(session: {
  user: { id: string } | null;
  pilotAdmitted: boolean;
} | null | undefined): boolean {
  return Boolean(session?.user && session.pilotAdmitted === true);
}

export const protectedStateMessage = {
  admission_required: 'Pilot admission is required.',
  quota_exhausted: 'The shared pilot search quota is exhausted.',
  processing: 'The proof is still processing.',
  uncertain: 'The operation needs operator review and will not be retried automatically.',
  service_unavailable: 'The proof service is unavailable. Refresh status before trying again.',
  not_found: 'The protected proof resource was not found.',
  unauthorized: 'Your session is no longer authorized.',
} as const;