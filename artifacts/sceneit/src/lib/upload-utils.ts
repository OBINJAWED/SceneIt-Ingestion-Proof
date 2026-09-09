export function uploadFileXHR(
  url: string,
  method: string,
  headers: Record<string, string>,
  file: File,
  onProgress: (percent: number) => void
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(method, url, true);
    xhr.timeout = 15 * 60 * 1000;
    xhr.ontimeout = () => reject(new Error('Upload timed out. Reselect the MP4 and retry this import.'));
    xhr.onabort = () => reject(new Error('Upload interrupted. Reselect the MP4 to retry.'));
    
    Object.entries(headers).forEach(([key, value]) => {
      xhr.setRequestHeader(key, value);
    });

    if (xhr.upload) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          const percent = Math.round((e.loaded / e.total) * 100);
          onProgress(percent);
        }
      };
    }

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve();
      } else {
        reject(new Error(`Upload failed with status ${xhr.status}`));
      }
    };

    xhr.onerror = () => reject(new Error('Network error during upload'));
    
    xhr.send(file);
  });
}