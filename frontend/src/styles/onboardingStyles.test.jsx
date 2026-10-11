/** Stylesheet contracts for the B.7 additions: step motion, reduced motion on public pages, token-only colours. */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const dir = dirname(fileURLToPath(import.meta.url));
const read = (rel) => readFileSync(join(dir, rel), "utf8").replace(/\r\n/g, "\n");
const motion = read("motion.css");
const auth = read("auth.css");
const onboarding = read("onboarding.css");

describe("step motion", () => {
  it("slides steps with transform and opacity only, forward and back", () => {
    for (const name of ["bh-step-fwd", "bh-step-back"]) {
      const m = new RegExp(`@keyframes ${name}\\s*\\{([^@]*?)\\}\\s*(?=\\n|$)`).exec(motion);
      expect(m, name).toBeTruthy();
      const decls = [...m[1].matchAll(/([a-z-]+)\s*:/g)].map((d) => d[1]);
      expect(decls.every((d) => d === "opacity" || d === "transform"), `${name}: ${decls}`).toBe(true);
    }
    expect(motion).toMatch(/\.wiz-panel\[data-dir="fwd"\][^{]*\{[^}]*bh-step-fwd var\(--motion\)/);
    expect(motion).toMatch(/\.auth-step\[data-dir="back"\][^{]*\{[^}]*bh-step-back var\(--motion\)/);
  });
});

describe("public pages", () => {
  it("load motion.css after auth.css so reduced motion also stops their animation", () => {
    const mount = read("../lib/mountPublicPage.jsx");
    expect(mount).toContain("auth.css");
    expect(mount.indexOf("motion.css")).toBeGreaterThan(mount.indexOf("auth.css"));
  });

  it("auth.css uses theme tokens for every colour", () => {
    for (const line of auth.split("\n").filter((l) => /(^|[\s;{])(color|background|border-color):/.test(l))) {
      const values = [...line.matchAll(/(?:^|[\s;{])(?:color|background|border-color):\s*([^;}]+)/g)].map((m) => m[1]);
      for (const v of values) expect(v, line).toMatch(/var\(--|transparent|none/);
    }
    expect(auth).not.toMatch(/#[0-9a-fA-F]{3,6}\b/);
  });

  it("auth.css defines no infinite or non-ease-out animation of its own", () => {
    expect(auth).not.toMatch(/infinite/);
    expect(auth).not.toMatch(/@keyframes/);
  });
});

describe("wizard styles", () => {
  it("use tokens for text colours and collapse labels on narrow screens", () => {
    for (const l of onboarding.split("\n").filter((x) => /(^|\s)color:/.test(x))) expect(l).toMatch(/var\(--/);
    expect(onboarding).toMatch(/@media \(max-width: 560px\)[^}]*\.wiz-label/);
  });
});
