/** Behavioral tests for the real UI-core production modules.
 *
 * They import `../loader.js` (counter-based global loader) and
 * `../sanitize.js` (DOMPurify boundaries) directly: no copied replacement
 * implementations.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { hideLoader, showLoader, withLoader } from "./loader.js";
import { sanitizeHtml, sanitizeSleuth } from "./sanitize.js";
import { isValidEmail } from "./constants.js";

function mountLoaderDom() {
  document.body.innerHTML =
    '<div id="globalLoader" hidden><span id="globalLoaderText"></span></div>';
}

beforeEach(() => {
  mountLoaderDom();
});

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("loader counter behavior (real lib/loader.js)", () => {
  it("stays up until every caller finishes", () => {
    showLoader("One");
    showLoader("Two");
    expect(document.getElementById("globalLoader").hidden).toBe(false);
    hideLoader();
    expect(document.getElementById("globalLoader").hidden).toBe(false);
    hideLoader();
    expect(document.getElementById("globalLoader").hidden).toBe(true);
    expect(document.body.classList.contains("is-loading")).toBe(false);
  });

  it("never drops below zero on unbalanced hides", () => {
    hideLoader();
    hideLoader();
    hideLoader();
    expect(document.getElementById("globalLoader").hidden).toBe(true);
  });

  it("withLoader hides the loader even when the thunk throws", async () => {
    await expect(
      withLoader(async () => {
        throw new Error("boom");
      }),
    ).rejects.toThrow("boom");
    expect(document.getElementById("globalLoader").hidden).toBe(true);
  });
});

describe("sanitization boundaries (real lib/sanitize.js)", () => {
  it("strips script content while keeping safe markup", () => {
    const out = sanitizeHtml('<p>ok</p><script>alert(1)</script>');
    expect(out).not.toContain("<script>");
    expect(out).toContain("<p>ok</p>");
  });

  it("keeps target=_blank emitted by the rich editor", () => {
    const out = sanitizeHtml('<a href="https://x.example" target="_blank">x</a>');
    expect(out).toContain('target="_blank"');
  });

  it("restricts Sleuth chat to its markdown-lite set", () => {
    expect(sanitizeSleuth("<table><tr><td>x</td></tr></table>")).not.toContain("<table>");
    expect(sanitizeSleuth("<strong>hi</strong>")).toContain("<strong>hi</strong>");
  });

  it("preserves legitimately unsaved plain text unchanged", () => {
    expect(sanitizeHtml("plain title 42")).toBe("plain title 42");
  });
});

describe("email format check (real lib/constants.js)", () => {
  it("accepts ordinary addresses", () => {
    expect(isValidEmail("user.name+tag@example.com")).toBe(true);
    expect(isValidEmail("  admin@bughunter.local  ")).toBe(true);
  });

  it("rejects addresses the server would refuse", () => {
    for (const bad of [
      "",
      "no-at-sign",
      "@example.com",
      "user@",
      "user@@example.com",
      "user@localhost",
      "user name@example.com",
      "user@example.",
      null,
      undefined,
    ]) {
      expect(isValidEmail(bad)).toBe(false);
    }
  });

  it("stays linear on hostile input (no catastrophic backtracking)", () => {
    const hostile = `${"a".repeat(20000)}@${"b".repeat(20000)}`;
    const started = Date.now();
    expect(isValidEmail(hostile)).toBe(false);
    expect(Date.now() - started).toBeLessThan(1000);
  });
});