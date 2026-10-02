/** Behavioral tests for the real branding helpers in `./branding.js`.
 *
 * The helpers read `<meta>` tags injected into the served HTML, so these tests
 * drive the DOM the same way the server-rendered pages do.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  getAppName,
  getAppVersion,
  getStaticAssetUrl,
  getWordmarkParts,
} from "./branding.js";

function setMeta(name, content) {
  const el = document.createElement("meta");
  el.setAttribute("name", name);
  if (content !== undefined) el.setAttribute("content", content);
  document.head.appendChild(el);
}

beforeEach(() => {
  document.head.innerHTML = "";
});

afterEach(() => {
  document.head.innerHTML = "";
});

describe("app identity", () => {
  it("reads the name and version meta tags", () => {
    setMeta("application-name", "Bug Hunter");
    setMeta("application-version", "9.9.9");
    expect(getAppName()).toBe("Bug Hunter");
    expect(getAppVersion()).toBe("9.9.9");
  });

  it("returns empty strings when the tags are absent or valueless", () => {
    expect(getAppName()).toBe("");
    expect(getAppVersion()).toBe("");
    setMeta("application-name");
    expect(getAppName()).toBe("");
  });
});

describe("cache-busted static urls", () => {
  it("appends the asset version so redeploys bust caches", () => {
    setMeta("application-asset-version", "abc 123");
    expect(getStaticAssetUrl("logo.png")).toBe("/static/logo.png?v=abc%20123");
  });

  it("omits the query string when no version is published", () => {
    expect(getStaticAssetUrl("logo.png")).toBe("/static/logo.png");
  });
});

describe("wordmark splitting", () => {
  it("returns empty parts when no name is published", () => {
    expect(getWordmarkParts()).toEqual({ first: "", rest: "" });
  });

  it("splits the first word from the accented remainder, uppercased", () => {
    setMeta("application-name", "Bug Hunter");
    expect(getWordmarkParts()).toEqual({ first: "BUG", rest: "HUNTER" });
  });

  it("handles a single-word name", () => {
    setMeta("application-name", "Tracker");
    expect(getWordmarkParts()).toEqual({ first: "TRACKER", rest: "" });
  });

  it("collapses extra whitespace and keeps multi-word remainders", () => {
    setMeta("application-name", "  Bug  Hunter  Pro  ");
    expect(getWordmarkParts()).toEqual({ first: "BUG", rest: "HUNTER PRO" });
  });
});
