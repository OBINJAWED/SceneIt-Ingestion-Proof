export function uploadFileXHR(
  url: string,
  method: string,
  headers: Record<string, string>,
  file: File,
  onProgress: (percent: number) => void,
  signal?: AbortSignal
): Promise<void> {
  return new Promise((resolve, reject) => {
    const aborted = () => new DOMException('Upload cancelled.', 'AbortError');
    if (signal?.aborted) {
      reject(aborted());
      return;
    }
    const xhr = new XMLHttpRequest();
    let settled = false;
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener('abort', cancel);
      xhr.onload = xhr.onerror = xhr.onabort = xhr.ontimeout = null;
      if (xhr.upload) xhr.upload.onprogress = null;
      if (error) reject(error);
      else resolve();
    };
    const cancel = () => {
      xhr.abort();
      finish(aborted());
    };
    xhr.open(method, url, true);
    xhr.timeout = 15 * 60 * 1000;
    xhr.ontimeout = () => finish(new Error('Upload timed out. Reselect the MP4 and retry this import.'));
    xhr.onabort = () => finish(aborted());
    
    Object.entries(headers).forEach(([key, value]) => {
      xhr.setRequestHeader(key, value);
    });

    if (xhr.upload) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          const percent = Math.round((e.loaded / e.total) * 100);
          if (!settled) onProgress(percent);
        }
      };
    }

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        finish();
      } else {
        finish(new Error(`Upload failed with status ${xhr.status}`));
      }
    };

    xhr.onerror = () => finish(new Error('Network error during upload'));
    signal?.addEventListener('abort', cancel, { once: true });
    try {
      if (signal?.aborted) cancel();
      else xhr.send(file);
    } catch {
      finish(new Error('The file transfer could not start. Please retry.'));
    }
  });
}