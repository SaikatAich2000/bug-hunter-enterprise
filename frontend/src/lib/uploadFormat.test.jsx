/** Behavioral tests for the real upload/format helpers (`./upload.js`,
 * `./format.js`) — the exact modules attachment flows import.
 *
 * They exercise production code (size caps, partition messages, byte/time
 * formatting, debounce cancellation) so a drift in the app logic fails here.
 */
import { describe, expect, it, vi } from "vitest";
import {
  MAX_UPLOAD_BYTES,
  MAX_UPLOAD_LABEL,
  fileTooLargeMessage,
  partitionBySize,
} from "./upload.js";
import {
  activityIcon,
  debounce,
  fileIcon,
  formatBytes,
  formatDate,
  initials,
  timeAgo,
} from "./format.js";

function file(name, size) {
  return { name, size };
}

describe("upload size validation", () => {
  it("accepts a file exactly at the cap", () => {
    expect(fileTooLargeMessage(file("ok.bin", MAX_UPLOAD_BYTES))).toBeNull();
  });

  it("rejects an oversized file with the cap label", () => {
    const msg = fileTooLargeMessage(file("big.bin", MAX_UPLOAD_BYTES + 1));
    expect(msg).toContain("big.bin");
    expect(msg).toContain(MAX_UPLOAD_LABEL);
  });

  it("partitions mixed files and names every oversized one", () => {
    const { allowed, tooLargeMessage } = partitionBySize([
      file("small.txt", 10),
      file("huge-a.bin", MAX_UPLOAD_BYTES + 5),
      file("huge-b.bin", MAX_UPLOAD_BYTES + 6),
    ]);
    expect(allowed.map((f) => f.name)).toEqual(["small.txt"]);
    expect(tooLargeMessage).toContain("huge-a.bin");
    expect(tooLargeMessage).toContain("huge-b.bin");
    expect(tooLargeMessage).toContain(MAX_UPLOAD_LABEL);
  });

  it("returns a null message when every file fits", () => {
    const { allowed, tooLargeMessage } = partitionBySize([file("a.txt", 1)]);
    expect(allowed).toHaveLength(1);
    expect(tooLargeMessage).toBeNull();
  });
});

describe("format helpers", () => {
  it("formats byte counts with units", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(2 * 1024 * 1024)).toBe("2.0 MB");
    expect(formatBytes(-1)).toBe("");
  });

  it("returns empty for blank or invalid dates", () => {
    expect(formatDate(null)).toBe("");
    expect(formatDate("not-a-date")).toBe("");
    expect(formatDate("2026-09-20T10:00:00Z")).toContain("2026");
  });

  it("builds initials from the first two words", () => {
    expect(initials("Ada Lovelace")).toBe("AL");
    expect(initials("  grace  ")).toBe("G");
    expect(initials("")).toBe("");
  });

  it("maps content types to icons", () => {
    expect(fileIcon("image/png")).toBe("🖼️");
    expect(fileIcon("application/pdf")).toBe("📄");
    expect(fileIcon("application/octet-stream")).toBe("📎");
  });

  it("maps audit actions to icons", () => {
    expect(activityIcon("user_login")).toBe("🔑");
    expect(activityIcon("bug_deleted")).toBe("🗑");
    expect(activityIcon("something_unknown_xyz")).toBe("📝");
  });

  it("renders relative times compactly", () => {
    expect(timeAgo(null)).toBe("");
    expect(timeAgo(new Date().toISOString())).toBe("just now");
    const fiveMinAgo = new Date(Date.now() - 5 * 60 * 1000).toISOString();
    expect(timeAgo(fiveMinAgo)).toBe("5m");
    const threeHoursAgo = new Date(Date.now() - 3 * 3600 * 1000).toISOString();
    expect(timeAgo(threeHoursAgo)).toBe("3h");
  });

  it("debounces rapid calls and supports cancellation", () => {
    vi.useFakeTimers();
    try {
      const fn = vi.fn();
      const debounced = debounce(fn, 100);
      debounced("a");
      debounced("b");
      expect(fn).not.toHaveBeenCalled();
      vi.advanceTimersByTime(100);
      expect(fn).toHaveBeenCalledTimes(1);
      expect(fn).toHaveBeenCalledWith("b");

      const fn2 = vi.fn();
      const d2 = debounce(fn2, 100);
      d2("x");
      d2.cancel();
      vi.advanceTimersByTime(200);
      expect(fn2).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});
