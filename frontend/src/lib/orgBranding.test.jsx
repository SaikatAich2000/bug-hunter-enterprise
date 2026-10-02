import { describe, expect, it } from "vitest";
import { accentVars, applyOrgAccent, contrast, parseHex, readableOn } from "./orgBranding";

describe("parseHex", () => {
  it("reads 3- and 6-digit colours", () => {
    expect(parseHex("#fff")).toEqual([255, 255, 255]);
    expect(parseHex("#1A9FFF")).toEqual([26, 159, 255]);
  });
  it("rejects anything else", () => {
    for (const bad of ["", null, "red", "#12", "#12345", "#gggggg", "rgb(1,2,3)"]) {
      expect(parseHex(bad)).toBeNull();
    }
  });
});

describe("readableOn", () => {
  it("picks the text colour with the better contrast", () => {
    expect(readableOn([255, 255, 0])).toBe("#000000");
    expect(readableOn([20, 20, 120])).toBe("#ffffff");
  });
  it("always reaches at least 4.5:1 against pure black or white text", () => {
    for (const rgb of [[200, 30, 30], [30, 200, 30], [30, 30, 200], [128, 128, 128], [255, 140, 0]]) {
      const text = readableOn(rgb) === "#000000" ? [0, 0, 0] : [255, 255, 255];
      expect(contrast(rgb, text)).toBeGreaterThanOrEqual(4.5);
    }
  });
});

describe("applyOrgAccent", () => {
  it("sets the fill variables for a valid colour and clears them for an invalid one", () => {
    const root = document.createElement("div");
    applyOrgAccent("#336699", root);
    expect(root.style.getPropertyValue("--accent-fill")).toBe("rgb(51, 102, 153)");
    expect(root.style.getPropertyValue("--on-accent-fill")).toBe("#ffffff");
    applyOrgAccent(null, root);
    expect(root.style.getPropertyValue("--accent-fill")).toBe("");
  });
  it("returns null variables for an invalid colour", () => {
    expect(accentVars("nope")).toBeNull();
  });
});
