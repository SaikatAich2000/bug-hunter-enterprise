/** Stylesheet contracts for A.5/A.6: the motion rules and AA contrast of the text pairs the new UI uses.
 * jsdom cannot run CSS animations or compute colours, so these read the stylesheets directly. */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const dir = dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(join(dir, name), "utf8").replace(/\r\n/g, "\n");
const motion = read("motion.css");
const onboarding = read("onboarding.css");
const base = read("styles.css");

function block(css, selectorStart) {
  const i = css.indexOf(selectorStart);
  expect(i, `${selectorStart} block`).toBeGreaterThan(-1);
  return css.slice(i, css.indexOf("\n}", i));
}

describe("motion.css", () => {
  it("uses one 150-250ms ease-out timing", () => {
    const ms = Object.fromEntries([...motion.matchAll(/--motion(-fast|-slow)?:\s*(\d+)ms/g)].map((m) => [m[1] ?? "", Number(m[2])]));
    expect(ms[""]).toBeGreaterThanOrEqual(150);
    expect(ms["-slow"]).toBeLessThanOrEqual(250);
    expect(ms["-fast"]).toBeGreaterThanOrEqual(150);
    expect(motion).toMatch(/--motion-ease:\s*cubic-bezier\(0\.16, 1, 0\.3, 1\)/);
  });

  it("keyframes only move transform and opacity", () => {
    const frames = [...motion.matchAll(/@keyframes\s+([\w-]+)\s*\{/g)];
    expect(frames.length).toBeGreaterThan(5);
    for (const m of frames) {
      let depth = 1;
      let i = m.index + m[0].length;
      const start = i;
      while (depth > 0 && i < motion.length) depth += motion[i] === "{" ? 1 : motion[i] === "}" ? -1 : 0, i++;
      const body = motion.slice(start, i - 1);
      for (const decl of body.matchAll(/[{;]\s*([a-z-]+)\s*:/g)) {
        expect(["opacity", "transform"], `${m[1]} animates ${decl[1]}`).toContain(decl[1]);
      }
    }
  });

  it("switches every animation and transition off under reduced motion", () => {
    const reduce = block(motion, "@media (prefers-reduced-motion: reduce)");
    expect(reduce).toMatch(/animation:\s*none\s*!important/);
    expect(reduce).toMatch(/transition:\s*none\s*!important/);
  });

  it("covers modal, toast, menus, rows, skeleton and the sidebar", () => {
    for (const needle of [".modal-card", ".modal[data-closing]", ".toast.leaving", ".profile-menu", ".bug-table tbody tr", ".skeleton", ".sidebar >"]) {
      expect(motion).toContain(needle);
    }
  });

  it("covers row removal, tab switches and the drag-drop settle", () => {
    expect(block(motion, "[data-leaving]")).toMatch(/bh-row-out/);
    expect(motion).toMatch(/\.agile-tab-panel[^{]*\{[^}]*bh-view-in/);
    expect(motion).toMatch(/\.ag-row, \.ag-card \{ transition: opacity/);
    expect(motion).not.toMatch(/\[data-leaving\][^{]*\{[^}]*(color|background)/);
  });

  it("is loaded after the base stylesheets", () => {
    const main = readFileSync(join(dir, "..", "main.jsx"), "utf8");
    expect(main.indexOf("motion.css")).toBeGreaterThan(main.indexOf("styles.css"));
    expect(main.indexOf("onboarding.css")).toBeGreaterThan(-1);
  });

  it("onboarding styles reuse theme tokens, not raw colours for text", () => {
    const textRules = onboarding.split("\n").filter((l) => /(^|\s)color:/.test(l));
    for (const l of textRules) expect(l).toMatch(/var\(--/);
  });
});

function tokens(css, selector) {
  const out = {};
  for (const m of block(css, selector).matchAll(/(--[\w-]+):\s*(#[0-9a-fA-F]{6})\b/g)) out[m[1]] = m[2];
  return out;
}
const dark = tokens(base, ':root,\n[data-theme="dark"] {');
const light = { ...dark, ...tokens(base, '[data-theme="light"] {') };

function luminance(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function ratio(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

describe.each([["dark", dark], ["light", light]])("%s theme contrast (WCAG AA)", (_, t) => {
  it.each([
    ["body text on card", "--text", "--bg-elev"],
    ["body text on inset panel", "--text", "--bg-elev-2"],
    ["body text on hover", "--text", "--bg-elev-3"],
    ["muted text on card", "--text-muted", "--bg-elev"],
    ["muted text on inset panel", "--text-muted", "--bg-elev-2"],
    ["link and accent text on card", "--accent", "--bg-elev"],
    ["text on solid accent fill", "--on-accent-fill", "--accent-fill"],
  ])("%s", (_label, fg, bg) => {
    const f = t[fg] ?? (fg === "--accent-fill" ? t["--accent"] : undefined);
    const b = t[bg] ?? (bg === "--accent-fill" ? t["--accent"] : undefined);
    expect(f, fg).toBeTruthy();
    expect(b, bg).toBeTruthy();
    expect(ratio(f, b)).toBeGreaterThanOrEqual(4.5);
  });
});
