/** Behavioral tests for the real bug-modal helpers (`./helpers.jsx`).
 *
 * Exercises the production module the bug list + modals import: the pure
 * permission/status/label helpers, attachment staging (size cap, dangerous
 * extensions, blob-url lifecycle), the staged tile preview fallback, the
 * attachment card previews, and the activity row.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const toastMock = vi.fn();

vi.mock("../../lib/toast", () => ({
  toast: (...args) => toastMock(...args),
  toastError: vi.fn(),
}));

import {
  ActivityRow,
  AttachmentCard,
  StagedTile,
  canEditItemType,
  errorMessage,
  escapeHtmlText,
  isoToday,
  itemTypeEmoji,
  plainTextFromHtml,
  plainToEditorHtml,
  stagedLabel,
  statusesForType,
  useStagedFiles,
} from "./helpers.jsx";

let container;
let root;

function mount(element) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root.render(element);
  });
  return container;
}

function renderHook(callback) {
  const ref = {};
  function Probe() {
    ref.current = callback();
    return null;
  }
  mount(React.createElement(Probe));
  return ref;
}

beforeEach(() => {
  toastMock.mockClear();
  vi.stubGlobal("URL", {
    createObjectURL: vi.fn((f) => `blob:${f.name}`),
    revokeObjectURL: vi.fn(),
  });
});

afterEach(() => {
  act(() => root?.unmount());
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
});

function fakeFile(name, { size = 10, type = "text/plain" } = {}) {
  return { name, size, type };
}


describe("pure helpers", () => {
  it("maps item types to emoji with a generic fallback", () => {
    expect(itemTypeEmoji("Bug")).toBe("🐞");
    expect(itemTypeEmoji("Task")).toBe("☑️");
    expect(itemTypeEmoji("Sub-task")).toBe("🔹");
    expect(itemTypeEmoji("Epic")).toBe("⚡");
    expect(itemTypeEmoji(undefined)).toBe("📝");
  });

  it("mirrors the backend edit rule (Bug for everyone, others need manager)", () => {
    expect(canEditItemType("user", "Bug")).toBe(true);
    expect(canEditItemType("user", undefined)).toBe(true);
    expect(canEditItemType("user", "Task")).toBe(false);
    expect(canEditItemType("user", "Requirement")).toBe(false);
    expect(canEditItemType("manager", "Task")).toBe(true);
    expect(canEditItemType("admin", "Requirement")).toBe(true);
  });

  it("resolves statuses per type with a global fallback", () => {
    const meta = {
      statuses: ["New", "Done"],
      statuses_by_type: { Task: ["New", "Blocked"] },
    };
    expect(statusesForType(meta, "Task")).toEqual(["New", "Blocked"]);
    expect(statusesForType(meta, "Story")).toEqual(["New", "Done"]);
    expect(statusesForType({ statuses: ["New"], statuses_by_type: {} }, "Bug")).toEqual(["New"]);
    expect(statusesForType({ statuses_by_type: {} }, "Bug")).toEqual([]);
  });

  it("formats today in local YYYY-MM-DD", () => {
    expect(isoToday()).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(isoToday()).toBe(
      new Date().toLocaleDateString("en-CA").replaceAll("/", "-"),
    );
  });

  it("extracts a usable error message", () => {
    expect(errorMessage(new Error("boom"))).toBe("boom");
    expect(errorMessage("plain")).toBe("plain");
  });

  it("falls back to a label for a blank error message", () => {
    const blank = new Error("boom");
    blank.message = "";
    expect(errorMessage(blank)).toBe("Error");
  });

  it("labels staged files with correct pluralisation", () => {
    expect(stagedLabel(0, "Attach files")).toBe("Attach files");
    expect(stagedLabel(1, "Attach files")).toBe("1 file");
    expect(stagedLabel(3, "Attach files")).toBe("3 files");
  });
});

describe("useStagedFiles", () => {
  it("stages allowed files with a blob url each", () => {
    const ref = renderHook(() => useStagedFiles());
    act(() => {
      ref.current.addFiles([fakeFile("a.txt"), fakeFile("b.png")]);
    });
    expect(ref.current.files.map((s) => s.file.name)).toEqual(["a.txt", "b.png"]);
    expect(ref.current.files[0].url).toBe("blob:a.txt");
    expect(toastMock).not.toHaveBeenCalled();
  });

  it("does nothing for an empty selection", () => {
    const ref = renderHook(() => useStagedFiles());
    act(() => {
      ref.current.addFiles([]);
    });
    expect(ref.current.files).toEqual([]);
  });

  it("rejects programs and scripts by extension, naming them", () => {
    const ref = renderHook(() => useStagedFiles());
    act(() => {
      ref.current.addFiles([
        fakeFile("notes.txt"),
        fakeFile("installer.exe"),
        fakeFile("SCRIPT.PS1"),
        fakeFile("dropper.exe."),
      ]);
    });
    expect(ref.current.files.map((s) => s.file.name)).toEqual(["notes.txt"]);
    const msg = toastMock.mock.calls[0][0];
    expect(msg).toContain("installer.exe");
    expect(msg).toContain("SCRIPT.PS1");
    expect(msg).toContain("dropper.exe.");
    expect(toastMock.mock.calls[0][1]).toBe("error");
  });

  it("keeps .js attachments (neutralized on download, not blocked)", () => {
    const ref = renderHook(() => useStagedFiles());
    act(() => {
      ref.current.addFiles([fakeFile("app.js")]);
    });
    expect(ref.current.files).toHaveLength(1);
  });

  it("refuses oversized files with the shared size message", () => {
    const ref = renderHook(() => useStagedFiles());
    act(() => {
      ref.current.addFiles([fakeFile("huge.bin", { size: 51 * 1024 * 1024 })]);
    });
    expect(ref.current.files).toEqual([]);
    expect(toastMock.mock.calls[0][0]).toContain("too large");
  });

  it("removes one staged file and revokes only its url", () => {
    const ref = renderHook(() => useStagedFiles());
    act(() => {
      ref.current.addFiles([fakeFile("a.txt"), fakeFile("b.txt")]);
    });
    act(() => {
      ref.current.removeAt(0);
    });
    expect(ref.current.files.map((s) => s.file.name)).toEqual(["b.txt"]);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:a.txt");
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1);
  });

  it("clears everything and revokes every url", () => {
    const ref = renderHook(() => useStagedFiles());
    act(() => {
      ref.current.addFiles([fakeFile("a.txt"), fakeFile("b.txt")]);
    });
    act(() => {
      ref.current.clear();
    });
    expect(ref.current.files).toEqual([]);
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2);
  });
});


describe("StagedTile", () => {
  it("previews images inline and removes through the callback", () => {
    const onRemove = vi.fn();
    const staged = { file: fakeFile("shot.png", { type: "image/png", size: 2048 }), url: "blob:shot.png" };
    const el = mount(React.createElement(StagedTile, { staged, bucket: "pending", idx: 0, onRemove }));
    expect(el.querySelector("img.attach-staged-thumb")).not.toBeNull();
    expect(el.querySelector(".attach-staged-name").textContent).toBe("shot.png");
    act(() => {
      el.querySelector(".attach-staged-remove").click();
    });
    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(el.querySelector(".attach-staged").dataset.bucket).toBe("pending");
  });

  it("falls back to the file icon when the image cannot render", () => {
    const staged = { file: fakeFile("broken.png", { type: "image/png" }), url: "blob:broken.png" };
    const el = mount(React.createElement(StagedTile, { staged, bucket: "queued", idx: 1, onRemove: vi.fn() }));
    act(() => {
      el.querySelector("img").dispatchEvent(new Event("error"));
    });
    expect(el.querySelector("img")).toBeNull();
    expect(el.querySelector(".attach-staged-link").className).toContain("attach-staged-icon-only");
  });

  it("uses the file icon for non-images", () => {
    const staged = { file: fakeFile("notes.txt", { size: 1024 }), url: "blob:notes.txt" };
    const el = mount(React.createElement(StagedTile, { staged, bucket: "pending", idx: 2, onRemove: vi.fn() }));
    expect(el.querySelector("img")).toBeNull();
    expect(el.querySelector(".attach-staged-link").getAttribute("title")).toBe("Open notes.txt");
  });
});

describe("AttachmentCard", () => {
  const base = { id: 9, filename: "file.bin", content_type: "application/octet-stream", size_bytes: 2048, uploader_name: "Saikat" };

  it("renders a raster image preview that links to the download endpoint", () => {
    const el = mount(React.createElement(AttachmentCard, {
      att: { ...base, filename: "shot.png", content_type: "image/png" },
      bugId: 5,
      deletable: false,
      onDelete: vi.fn(),
    }));
    const img = el.querySelector("img");
    expect(img.getAttribute("src")).toBe("/api/bugs/5/attachments/9/download");
    expect(el.querySelector(".attach-info").textContent).toContain("Saikat");
  });

  it("never renders SVG inline", () => {
    const el = mount(React.createElement(AttachmentCard, {
      att: { ...base, filename: "logo.svg", content_type: "image/svg+xml" },
      bugId: 5,
      deletable: false,
      onDelete: vi.fn(),
    }));
    expect(el.querySelector("img")).toBeNull();
    expect(el.querySelector("a.file-icon")).not.toBeNull();
  });

  it("opens a video in the lightbox instead of a new tab", () => {
    const el = mount(React.createElement(AttachmentCard, {
      att: { ...base, filename: "clip.mp4", content_type: "video/mp4" },
      bugId: 5,
      deletable: false,
      onDelete: vi.fn(),
    }));
    expect(el.querySelector("button[data-act='view-video']")).not.toBeNull();
    expect(el.querySelector("video.attach-video-poster")).not.toBeNull();
    act(() => {
      el.querySelector("button[data-act='view-video']").click();
    });
    const lightbox = document.querySelector(".video-lightbox");
    expect(lightbox).not.toBeNull();
    expect(lightbox.getAttribute("aria-label")).toBe("Video: clip.mp4");
  });

  it("offers Delete only when allowed and reports the id", () => {
    const onDelete = vi.fn();
    let el = mount(React.createElement(AttachmentCard, {
      att: base, bugId: 5, deletable: true, onDelete,
    }));
    act(() => {
      el.querySelector("button[data-act='delete-attachment']").click();
    });
    expect(onDelete).toHaveBeenCalledWith(9);

    el = mount(React.createElement(AttachmentCard, {
      att: base, bugId: 5, deletable: false, onDelete,
    }));
    expect(el.querySelector("button[data-act='delete-attachment']")).toBeNull();
  });
});

describe("ActivityRow", () => {
  it("renders the actor, action, icon and optional detail", () => {
    const el = mount(React.createElement(ActivityRow, {
      activity: { action: "created", actor_name: "Manish", detail: "title set" },
    }));
    expect(el.querySelector(".activity-actor").textContent).toBe("Manish");
    expect(el.querySelector(".activity-action").textContent).toBe("created");
    expect(el.querySelector(".activity-detail").textContent).toBe("title set");
  });

  it("omits the detail line when there is none", () => {
    const el = mount(React.createElement(ActivityRow, {
      activity: { action: "updated", actor_name: "Kunal", detail: "" },
    }));
    expect(el.querySelector(".activity-detail")).toBeNull();
  });
});

describe("plain-text description bridge", () => {
  it("converts editor HTML to plain text with newlines", () => {
    expect(plainTextFromHtml(
      "<div>As a user, I want docs.</div><div><br></div>" +
      "<div>GET / returns <b>HTTP 200</b> &amp; docs.</div>",
    )).toBe("As a user, I want docs.\n\nGET / returns HTTP 200 & docs.");
    expect(plainTextFromHtml("")).toBe("");
    expect(plainTextFromHtml("<p><br></p>")).toBe("");
  });

  it("wraps stored plain-text lines as editor divs, escaped", () => {
    expect(plainToEditorHtml("Line one.\n\nLine two.")).toBe(
      "<div>Line one.</div><div><br></div><div>Line two.</div>",
    );
    expect(plainToEditorHtml("")).toBe("<div><br></div>");
    expect(plainToEditorHtml("a & b <c>")).toBe(
      "<div>a &amp; b &lt;c&gt;</div>",
    );
    expect(escapeHtmlText("a & b <c>")).toBe("a &amp; b &lt;c&gt;");
  });

  it("round-trips plain text through the editor HTML", () => {
    const plain = "First para.\n\nSecond with docs path /docs.";
    expect(plainTextFromHtml(plainToEditorHtml(plain))).toBe(plain);
  });
});
