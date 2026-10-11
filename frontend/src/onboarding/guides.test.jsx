/** The bundled guides: all topics exist, links resolve, and no banned wording. */
import { describe, expect, it } from "vitest";

import { GUIDES, findGuide, searchGuides } from "./guides.js";

describe("guides", () => {
  it("covers every required topic in order", () => {
    expect(GUIDES.map((g) => g.id)).toEqual(["items", "sprints", "reports", "notifications", "git", "admin", "roles", "organizations", "security"]);
    for (const g of GUIDES) {
      expect(g.title.length).toBeGreaterThan(3);
      expect(g.summary.length).toBeGreaterThan(20);
    }
  });

  it("every in-app guide link points at a real guide", () => {
    for (const g of GUIDES) {
      for (const m of g.markdown.matchAll(/\(#help\/([\w-]+)\)/g)) {
        expect(findGuide(m[1]), `${g.id} -> ${m[1]}`).not.toBeNull();
      }
    }
  });

  it("avoids names of other products", () => {
    for (const g of GUIDES) expect(g.markdown).not.toMatch(new RegExp(["ji" + "ra", "ste" + "am", "moti" + "va"].join("|"), "i"));
  });

  it("search matches every word, case-insensitively", () => {
    expect(searchGuides("")).toHaveLength(GUIDES.length);
    expect(searchGuides("BURNDOWN").map((g) => g.id)).toContain("sprints");
    expect(searchGuides("zzzz-nothing")).toEqual([]);
  });
});
