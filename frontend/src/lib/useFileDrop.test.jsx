/** Behavioral tests for the real `useFileDrop` hook (`./useFileDrop.js`).
 *
 * Exercises production behavior with Testing Library: highlight on file drag,
 * nested enter/leave depth counting, ignored non-file drags, drop delivery,
 * claimed-drop suppression, and dropEffect assignment.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useFileDrop } from "./useFileDrop.js";

// Minimal renderHook: mounts the hook once via React and exposes .current.
// Single React import shared by the hook harness and the event helpers.
function renderHook(callback) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const ref = {};
  function Probe() {
    ref.current = callback();
    return null;
  }
  const root = createRoot(container);
  act(() => {
    root.render(React.createElement(Probe));
  });
  return {
    result: ref,
    unmount: () => {
      act(() => {
        root.unmount();
      });
      container.remove();
    },
  };
}

afterEach(() => {
  document.body.innerHTML = "";
});

function fileDragEvent({ types = ["Files"], files = [], claimed = false } = {}) {
  const preventDefault = vi.fn();
  return {
    dataTransfer: { types, files, dropEffect: "none" },
    isDefaultPrevented: () => claimed,
    preventDefault,
  };
}

function enter(result, opts) {
  const e = fileDragEvent(opts);
  act(() => {
    result.current.dropProps.onDragEnter(e);
  });
  return e;
}

describe("useFileDrop", () => {
  it("highlights on file drag-enter and clears on matching leave", () => {
    const { result } = renderHook(() => useFileDrop(vi.fn()));
    enter(result);
    expect(result.current.dragging).toBe(true);
    const leave = fileDragEvent();
    act(() => {
      result.current.dropProps.onDragLeave(leave);
    });
    expect(result.current.dragging).toBe(false);
    expect(leave.preventDefault).toHaveBeenCalled();
  });

  it("keeps highlight across nested enter/leave (depth counting)", () => {
    const { result } = renderHook(() => useFileDrop(vi.fn()));
    enter(result);
    enter(result);
    act(() => {
      result.current.dropProps.onDragLeave(fileDragEvent());
    });
    // One nested leave of two enters: still highlighted.
    expect(result.current.dragging).toBe(true);
    act(() => {
      result.current.dropProps.onDragLeave(fileDragEvent());
    });
    expect(result.current.dragging).toBe(false);
  });

  it("ignores non-file drags entirely", () => {
    const onFiles = vi.fn();
    const { result } = renderHook(() => useFileDrop(onFiles));
    const e = fileDragEvent({ types: ["text/plain"] });
    act(() => {
      result.current.dropProps.onDragEnter(e);
      result.current.dropProps.onDragOver(e);
      result.current.dropProps.onDragLeave(e);
      result.current.dropProps.onDrop(e);
    });
    expect(result.current.dragging).toBe(false);
    expect(e.preventDefault).not.toHaveBeenCalled();
    expect(onFiles).not.toHaveBeenCalled();
  });

  it("delivers files on drop and clears highlight", () => {
    const onFiles = vi.fn();
    const { result } = renderHook(() => useFileDrop(onFiles));
    enter(result);
    const files = [{ name: "a.txt" }, { name: "b.txt" }];
    act(() => {
      result.current.dropProps.onDrop(fileDragEvent({ files }));
    });
    expect(onFiles).toHaveBeenCalledTimes(1);
    expect(onFiles).toHaveBeenCalledWith(files);
    expect(result.current.dragging).toBe(false);
  });

  it("suppresses delivery when a nested target already claimed the drop", () => {
    const onFiles = vi.fn();
    const { result } = renderHook(() => useFileDrop(onFiles));
    enter(result);
    const e = fileDragEvent({ files: [{ name: "a.txt" }], claimed: true });
    act(() => {
      result.current.dropProps.onDrop(e);
    });
    expect(e.preventDefault).toHaveBeenCalled();
    expect(onFiles).not.toHaveBeenCalled();
    expect(result.current.dragging).toBe(false);
  });

  it("sets copy dropEffect on drag-over with files", () => {
    const { result } = renderHook(() => useFileDrop(vi.fn()));
    const e = fileDragEvent();
    act(() => {
      result.current.dropProps.onDragOver(e);
    });
    expect(e.preventDefault).toHaveBeenCalled();
    expect(e.dataTransfer.dropEffect).toBe("copy");
  });

  it("ignores an empty file drop without calling back", () => {
    const onFiles = vi.fn();
    const { result } = renderHook(() => useFileDrop(onFiles));
    act(() => {
      result.current.dropProps.onDrop(fileDragEvent({ files: [] }));
    });
    expect(onFiles).not.toHaveBeenCalled();
  });
});
