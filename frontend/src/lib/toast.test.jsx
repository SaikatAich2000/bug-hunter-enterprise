/** Behavioral tests for the real toast helpers in `./toast.js`.
 *
 * The helpers drive the shell's `#toast` element imperatively, so these tests
 * assert the element state they leave behind (text, class, visibility) plus
 * the silent-401 suppression rule that keeps an expiring session quiet.
 *
 * The suite runs with `isolate: false`, so modules can leak between files;
 * this file therefore defines its own `./api` mock (with a local ApiError)
 * and resolves both modules through `vi.resetModules()` so the `instanceof`
 * identity in `toastError()` always matches what this file constructs.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./api", () => {
  class ApiError extends Error {
    constructor(message, status, silent = false) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.silent = silent;
    }
  }
  return { API: "/api", ApiError };
});

let ApiError;
let toast;
let toastError;
let host;

beforeEach(async () => {
  vi.useFakeTimers();
  vi.resetModules();
  ({ ApiError } = await import("./api.js"));
  ({ toast, toastError } = await import("./toast.js"));
  host = document.createElement("div");
  host.id = "toast";
  document.body.appendChild(host);
});

afterEach(() => {
  vi.useRealTimers();
  document.body.innerHTML = "";
});

describe("toast()", () => {
  it("shows the message with an optional type class", () => {
    toast("Saved");
    expect(host.textContent).toBe("Saved");
    expect(host.className).toBe("toast ");
    expect(host.hidden).toBe(false);

    toast("Nope", "error");
    expect(host.className).toBe("toast error");
  });

  it("auto-hides after the display window", () => {
    toast("Saved");
    expect(host.hidden).toBe(false);
    vi.advanceTimersByTime(3499);
    expect(host.hidden).toBe(false);
    vi.advanceTimersByTime(1);
    expect(host.hidden).toBe(true);
  });

  it("restarts the hide timer on a newer toast", () => {
    toast("first");
    vi.advanceTimersByTime(3000);
    toast("second");
    vi.advanceTimersByTime(3000);
    expect(host.hidden).toBe(false);
    vi.advanceTimersByTime(500);
    expect(host.hidden).toBe(true);
  });

  it("is a no-op when the shell renders no toast host", () => {
    document.body.innerHTML = "";
    expect(() => toast("orphan")).not.toThrow();
  });
});

describe("toastError()", () => {
  it("surfaces an Error message", () => {
    toastError(new Error("Bad request"));
    expect(host.textContent).toBe("Bad request");
    expect(host.className).toBe("toast error");
  });

  it("accepts a plain string and an unknown value", () => {
    toastError("plain failure");
    expect(host.textContent).toBe("plain failure");
    toastError({ weird: true });
    expect(host.textContent).toBe("Something went wrong");
  });

  it("stays silent for the 401 redirect error", () => {
    toastError(new ApiError("Session expired", 401, true));
    expect(host.textContent).toBe("");
    expect(host.className).toBe("");
    // No toast means no auto-hide timer was scheduled either.
    expect(vi.getTimerCount()).toBe(0);
  });

  it("still reports a non-silent ApiError", () => {
    toastError(new ApiError("Conflict", 409));
    expect(host.textContent).toBe("Conflict");
    expect(host.className).toBe("toast error");
  });
});
