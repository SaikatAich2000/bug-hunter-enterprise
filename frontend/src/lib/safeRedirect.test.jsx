import { describe, expect, it } from "vitest";

import { loginTarget, safeNextPath } from "./safeRedirect.js";

const ORIGIN = "https://bugs.example.com";

describe("safeNextPath", () => {
  it("keeps same-origin paths with query and hash", () => {
    expect(safeNextPath("/bugs/12?tab=comments#c3", ORIGIN)).toBe("/bugs/12?tab=comments#c3");
    expect(safeNextPath("/", ORIGIN)).toBe("/");
  });

  it.each([
    ["protocol-relative", "//evil.example/x"],
    ["backslash host", "/\\evil.example"],
    ["tab-smuggled host", "/\t/evil.example"],
    ["newline-smuggled host", "/\n/evil.example"],
    ["absolute URL", "https://evil.example/"],
    ["javascript URL", "javascript:alert(1)"],
    ["relative without slash", "bugs/1"],
    ["empty", ""],
  ])("falls back to / for %s", (_label, value) => {
    expect(safeNextPath(value, ORIGIN)).toBe("/");
  });

  it("falls back to / for a missing value", () => {
    expect(safeNextPath(null, ORIGIN)).toBe("/");
    expect(safeNextPath(undefined, ORIGIN)).toBe("/");
  });
});

describe("loginTarget", () => {
  it("prefers an explicit next", () => {
    expect(loginTarget("?next=%2Fx%23bug%3D2", "#bug=9", ORIGIN)).toBe("/x#bug=2");
  });

  it("keeps a deep-link fragment carried across the login redirect", () => {
    expect(loginTarget("", "#bug=5", ORIGIN)).toBe("/#bug=5");
  });

  it("defaults to the home page", () => {
    expect(loginTarget("", "", ORIGIN)).toBe("/");
  });

  it("still refuses an off-site next", () => {
    expect(loginTarget("?next=%2F%09%2Fevil.example", "", ORIGIN)).toBe("/");
  });
});
