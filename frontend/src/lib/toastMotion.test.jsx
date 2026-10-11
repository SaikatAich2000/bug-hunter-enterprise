/** Toast exit animation and its reduced-motion path. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./api", () => ({ API: "/api", ApiError: class ApiError extends Error {} }));

let toast;
let el;
const original = window.matchMedia;

function mockMedia(reduce) {
  window.matchMedia = vi.fn((query) => ({ matches: reduce && query.includes("reduce"), media: query }));
}

beforeEach(async () => {
  vi.useFakeTimers();
  vi.resetModules();
  ({ toast } = await import("./toast.js"));
  el = document.createElement("div");
  el.id = "toast";
  el.hidden = true;
  document.body.appendChild(el);
});

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
  window.matchMedia = original;
});

describe("toast motion", () => {
  it("slides out before it hides", () => {
    mockMedia(false);
    toast("Saved");
    vi.advanceTimersByTime(3500);
    expect(el.classList.contains("leaving")).toBe(true);
    expect(el.hidden).toBe(false);
    vi.advanceTimersByTime(250);
    expect(el.hidden).toBe(true);
    expect(el.classList.contains("leaving")).toBe(false);
  });

  it("hides at once under reduced motion", () => {
    mockMedia(true);
    toast("Saved");
    vi.advanceTimersByTime(3500);
    expect(el.hidden).toBe(true);
    expect(el.classList.contains("leaving")).toBe(false);
  });

  it("a new toast while leaving brings it back", () => {
    mockMedia(false);
    toast("One");
    vi.advanceTimersByTime(3500);
    toast("Two");
    vi.advanceTimersByTime(300);
    expect(el.hidden).toBe(false);
    expect(el.textContent).toBe("Two");
  });
});
