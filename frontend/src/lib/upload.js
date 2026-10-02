/** Upload limits mirroring server MAX_FILE_BYTES for client-side rejection. */
import { formatBytes } from "./format";

/** Server-enforced per-attachment cap. */
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

export const MAX_UPLOAD_LABEL = "50 MB";

/** Error message if file exceeds the limit, else null. */
export function fileTooLargeMessage(file) {
  if (file.size <= MAX_UPLOAD_BYTES) return null;
  const name = file.name || "This file";
  return `"${name}" is too large (${formatBytes(file.size)}). The largest file you can attach is ${MAX_UPLOAD_LABEL}.`;
}

/** Split files into allowed/oversized; message lists oversized ones, null if all fit. */
export function partitionBySize(files) {
  const allowed = [];
  const tooLarge = [];
  for (const f of files) {
    if (f.size > MAX_UPLOAD_BYTES) tooLarge.push(f);
    else allowed.push(f);
  }
  if (!tooLarge.length) return { allowed, tooLargeMessage: null };

  let message;
  if (tooLarge.length === 1) {
    message = fileTooLargeMessage(tooLarge[0]);
  } else {
    const names = tooLarge.map((f) => `"${f.name || "a file"}"`).join(", ");
    message = `${names} are too large to attach. The largest file you can attach is ${MAX_UPLOAD_LABEL}.`;
  }
  return { allowed, tooLargeMessage: message };
}
