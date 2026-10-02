/**
 * Shared helpers for the bug list and bug modal: item-type emoji, permission/meta
 * lookups, activity row, attachment card, and staging (useStagedFiles + StagedTile).
 */
import { itemTypeIcon } from "../../lib/itemTypes";
import { useCallback, useState } from "react";
import { activityIcon, fileIcon, formatBytes, formatDate } from "../../lib/format";
import { toast } from "../../lib/toast";
import { partitionBySize } from "../../lib/upload";

// Client-side block mirroring backend _DANGEROUS_UPLOAD_EXTS; trailing dots/spaces stripped first ("evil.exe.").
const _DANGEROUS_EXTS = new Set([
  // .js is intentionally allowed (legitimate source files; neutralized on download).
  "exe", "msi", "bat", "cmd", "com", "scr", "pif", "cpl", "hta", "jar",
  "jse", "vbs", "vbe", "wsf", "wsh", "ps1", "psm1", "sh", "bash",
  "app", "dmg", "pkg", "deb", "rpm", "apk", "msc", "reg", "lnk", "gadget",
  "dll", "sys", "elf",
]);

function isDangerousFile(name) {
  // Linear trailing-trim of dots/spaces: no regex, so no super-linear
  // backtracking (Sonar S8786) and identical behaviour.
  const trimmed = (name || "").trim();
  let end = trimmed.length;
  while (end > 0) {
    const ch = trimmed[end - 1];
    if (ch === "." || /\s/.test(ch)) { end -= 1; continue; }
    break;
  }
  const cleaned = trimmed.slice(0, end);
  const dot = cleaned.lastIndexOf(".");
  if (dot < 0) return false;
  return _DANGEROUS_EXTS.has(cleaned.slice(dot + 1).trim().toLowerCase());
}
import VideoLightbox from "../../components/VideoLightbox";

// --- Plain-text description bridge (API stores plain text, editor is HTML) ---

/** Escape text so it can be embedded as editor HTML text content. */
export function escapeHtmlText(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

/** Stored plain text -> editor HTML: paragraphs break on blank lines. */
export function plainToEditorHtml(text) {
  return String(text || "")
    .split("\n")
    .map((line) => (line.trim() === "" ? "<div><br></div>" : `<div>${escapeHtmlText(line)}</div>`))
    .join("");
}

/** Editor HTML -> plain text with newlines, matching what the API stores. */
export function plainTextFromHtml(html) {
  const div = document.createElement("div");
  div.innerHTML = html || "";
  // Block boundaries and <br> become newlines; inline tags contribute text only.
  div.querySelectorAll("br").forEach((br) => br.replaceWith("\n"));
  div.querySelectorAll("p, div, li, tr, h1, h2, h3, h4, h5, h6, blockquote, pre")
    .forEach((el) => el.after("\n"));
  return (div.textContent ?? "")
    .split("\n")
    .map((line) => line.trim())
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** Icon of a work-item type; one per type, Task and Sub-task included. */
export function itemTypeEmoji(t) {
  return itemTypeIcon(t);
}

/** Mirrors the backend can_edit_bug rule. Tasks and Requirements require manager/admin. */
export function canEditItemType(role, itemType) {
  if (role === "admin" || role === "manager") return true;
  return (itemType || "Bug") === "Bug";
}

/** Allowed statuses for a given item type, falling back to the global list. */
export function statusesForType(meta, itype) {
  const byType = meta.statuses_by_type;
  return byType[itype || "Bug"] ?? meta.statuses ?? [];
}

/** Local-time YYYY-MM-DD, used for the create-mode due-date default. */
export function isoToday() {
  const d = new Date();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}

export function errorMessage(err) {
  return err instanceof Error && err.message ? err.message : String(err);
}

/** Staged-file count label: "Attach files" / "N file(s)". */
export function stagedLabel(count, idle) {
  if (!count) return idle;
  return `${count} file${count > 1 ? "s" : ""}`;
}

export function ActivityRow({ activity }) {
  return (
    <div className="activity-row">
      <span className="activity-icon">{activityIcon(activity.action)}</span>
      <div className="activity-text">
        <div>
          <span className="activity-actor">{activity.actor_name}</span>
          <span className="activity-action">{activity.action}</span>
        </div>
        {activity.detail ? <div className="activity-detail">{activity.detail}</div> : null}
      </div>
      <span className="activity-time">{formatDate(activity.created_at)}</span>
    </div>
  );
}

export function AttachmentCard({ att, bugId, deletable, onDelete }) {
  const url = `/api/bugs/${bugId}/attachments/${att.id}/download`;
  const ct = (att.content_type || "").toLowerCase();
  // SVG can carry inline JS, so it gets the file icon, not inline rendering.
  const isRasterImg = ct.startsWith("image/") && ct !== "image/svg+xml";
  const isVideo = ct.startsWith("video/");
  // Video opens in the themed lightbox, not the native player.
  const [videoOpen, setVideoOpen] = useState(false);
  const openVideo = useCallback(() => setVideoOpen(true), []);

  let preview;
  if (isRasterImg) {
    preview = (
      <a href={url} target="_blank" rel="noopener noreferrer">
        <img src={url} alt={att.filename} loading="lazy" />
      </a>
    );
  } else if (isVideo) {
    // #t=0.1 makes the browser paint the first frame as a poster.
    preview = (
      <button
        type="button"
        className="attach-video-thumb"
        onClick={openVideo}
        aria-label={`Play ${att.filename}`}
      >
        
        <video
          className="attach-video-poster"
          src={`${url}#t=0.1`}
          preload="metadata"
          muted
          playsInline
          tabIndex={-1}
        />
        <span className="attach-video-play" aria-hidden="true">▶</span>
      </button>
    );
  } else {
    preview = (
      <a href={url} target="_blank" rel="noopener noreferrer" className="file-icon">
        {fileIcon(att.content_type, att.filename)}
      </a>
    );
  }
  return (
    <div className="attach-card" data-att-id={att.id}>
      <div className="attach-preview">{preview}</div>
      <div className="attach-meta">
        <div className="attach-name" title={att.filename}>{att.filename}</div>
        <div className="attach-info">
          <span>{formatBytes(att.size_bytes)}</span>
          <span>{att.uploader_name}</span>
        </div>
      </div>
      <div className="attach-actions">
        {isVideo ? (
          <button type="button" data-act="view-video" onClick={openVideo}>
            View
          </button>
        ) : (
          <a href={url} target="_blank" rel="noopener noreferrer">View</a>
        )}
        <a href={url} download={att.filename}>Download</a>
        {deletable && (
          <button
            type="button"
            className="danger"
            data-act="delete-attachment"
            data-id={att.id}
            onClick={() => onDelete(att.id)}
          >
            Delete
          </button>
        )}
      </div>
      {isVideo && videoOpen && (
        <VideoLightbox
          src={url}
          type={att.content_type}
          label={att.filename}
          onClose={() => setVideoOpen(false)}
        />
      )}
    </div>
  );
}

// Attachment staging: FileList is read-only, so selections copy into an array
// with a blob URL per file for preview. Buckets are modal-owned React state.

export function useStagedFiles() {
  const [files, setFiles] = useState([]);

  const addFiles = useCallback((fs) => {
    const list = Array.from(fs);
    if (!list.length) return;
    const { allowed: sized, tooLargeMessage } = partitionBySize(list);
    if (tooLargeMessage) toast(tooLargeMessage, "error");
    // Drop dangerous extensions, naming the blocked files in the toast.
    const blocked = sized.filter((f) => isDangerousFile(f.name));
    const allowed = sized.filter((f) => !isDangerousFile(f.name));
    if (blocked.length) {
      toast(
        `Can't attach ${blocked.map((f) => f.name).join(", ")} — for safety, programs and scripts aren't allowed.`,
        "error",
      );
    }
    if (!allowed.length) return;
    const staged = allowed.map((f) => ({ file: f, url: URL.createObjectURL(f) }));
    setFiles((prev) => [...prev, ...staged]);
  }, []);

  const removeAt = useCallback((idx) => {
    setFiles((prev) => {
      const target = prev[idx];
      if (!target) return prev;
      try {
        URL.revokeObjectURL(target.url);
      } catch {
        /* already revoked */
      }
      return prev.filter((_, i) => i !== idx);
    });
  }, []);

  const clear = useCallback(() => {
    setFiles((prev) => {
      for (const s of prev) {
        try {
          URL.revokeObjectURL(s.url);
        } catch {
          /* already revoked */
        }
      }
      return prev.length ? [] : prev;
    });
  }, []);

  return { files, addFiles, removeAt, clear };
}

/** One pending-upload preview tile. */
export function StagedTile({ staged, bucket, idx, onRemove }) {
  // Fall back to the file icon if the blob URL fails to render.
  const [broken, setBroken] = useState(false);
  const f = staged.file;
  const wasImage = (f.type || "").startsWith("image/");
  const showImg = wasImage && !broken;
  const icon = fileIcon(f.type, f.name);
  return (
    <span className="attach-staged" data-bucket={bucket} data-idx={idx} data-obj-url={staged.url}>
      {showImg ? (
        <a className="attach-staged-link" href={staged.url} target="_blank" rel="noopener noreferrer">
          <img
            className="attach-staged-thumb"
            src={staged.url}
            alt={f.name}
            data-fallback={icon}
            onError={() => setBroken(true)}
          />
        </a>
      ) : (
        <a
          className={`attach-staged-link${wasImage && broken ? " attach-staged-icon-only" : ""}`}
          href={staged.url}
          target="_blank"
          rel="noopener noreferrer"
          title={wasImage ? undefined : `Open ${f.name}`}
        >
          {icon}
        </a>
      )}
      <span className="attach-staged-meta">
        <span className="attach-staged-name" title={f.name}>{f.name}</span>
        <span className="attach-staged-size muted small">{formatBytes(f.size)}</span>
      </span>
      <button
        type="button"
        className="attach-staged-remove"
        aria-label={`Remove ${f.name}`}
        title="Remove (not yet uploaded)"
        onClick={(e) => {
          e.preventDefault();
          onRemove();
        }}
      >
        ✕
      </button>
    </span>
  );
}
