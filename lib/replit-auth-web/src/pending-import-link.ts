const LINK_KEY = 'pendingImportLink';
const ANONYMOUS_KEY = 'pendingImportLink:anonymous';

/** Entry text only. Never persist consent, files, results, or access grants. */
export function readPendingImportLink(): string {
  try {
    return sessionStorage.getItem(LINK_KEY) ?? '';
  } catch {
    return '';
  }
}

export function savePendingImportLink(link: string, anonymous: boolean) {
  try {
    // Remove the exemption first so a partial write cannot relabel an owner's
    // draft as anonymous.
    sessionStorage.removeItem(ANONYMOUS_KEY);
    if (!link) {
      sessionStorage.removeItem(LINK_KEY);
      return;
    }
    sessionStorage.setItem(LINK_KEY, link);
    if (anonymous) sessionStorage.setItem(ANONYMOUS_KEY, '1');
  } catch {
    // The mounted form still works when browser storage is unavailable.
  }
}

export function clearPendingImportLink(preserveAnonymous = false) {
  try {
    if (preserveAnonymous && sessionStorage.getItem(ANONYMOUS_KEY) === '1') return;
    sessionStorage.removeItem(ANONYMOUS_KEY);
    sessionStorage.removeItem(LINK_KEY);
  } catch {
    // Storage may be unavailable in hardened browser contexts.
  }
}