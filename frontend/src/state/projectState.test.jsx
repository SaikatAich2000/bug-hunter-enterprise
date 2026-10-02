/** Behavioral tests for the real project-list helpers in
 * `../projectList.js` (the exact module AppContext imports).
 *
 * They exercise production code — normalize/upsert plus the staleness rule —
 * so a drift in the app logic fails these tests.
 */
import { describe, expect, it } from "vitest";
import {
  isStaleProjectResponse,
  normalizeProjects,
  upsertProjectIntoList,
} from "../lib/projectList.js";

describe("project immediate upsert", () => {
  it("inserts a new project exactly once and preserves unrelated rows", () => {
    const current = normalizeProjects([
      { id: 1, name: "Alpha" },
      { id: 2, name: "beta" },
    ]);
    const next = upsertProjectIntoList(current, { id: 9, name: "Zulu" });
    expect(next.filter((p) => p.id === 9)).toHaveLength(1);
    expect(next.map((p) => p.id)).toContain(2);
  });

  it("returns the list unchanged when the project has no id", () => {
    const current = normalizeProjects([{ id: 1, name: "Alpha" }]);
    const kept = upsertProjectIntoList(current, { name: "nameless" });
    expect(kept).toBe(current);
  });

  it("updates an existing project in place without duplicating the id", () => {
    const current = normalizeProjects([{ id: 1, name: "Alpha" }]);
    const next = upsertProjectIntoList(current, { id: 1, name: "Alpha Renamed" });
    expect(next.filter((p) => p.id === 1)).toHaveLength(1);
    expect(next.find((p) => p.id === 1).name).toBe("Alpha Renamed");
    // Immutable: a new array, original untouched.
    expect(next).not.toBe(current);
    expect(current[0].name).toBe("Alpha");
  });

  it("deduplicates by id and sorts case-insensitively", () => {
    const next = normalizeProjects([
      { id: 3, name: "beta" },
      { id: 1, name: "Alpha" },
      { id: 1, name: "Alpha (stale copy)" },
      { id: 2, name: "CHARLIE" },
    ]);
    expect(next.map((p) => p.id)).toEqual([1, 3, 2]);
    // First-seen row wins for a duplicated id.
    expect(next[0].name).toBe("Alpha");
  });
});

describe("project stale-response rejection", () => {
  it("drops a response that started before the latest mutation", () => {
    expect(
      isStaleProjectResponse({ seq: 1, latestSeq: 1, mutatedAt: 0, mutationSeq: 1 }),
    ).toBe(true);
  });

  it("drops a response superseded by a newer request", () => {
    expect(
      isStaleProjectResponse({ seq: 1, latestSeq: 2, mutatedAt: 0, mutationSeq: 0 }),
    ).toBe(true);
  });

  it("accepts the newest response when no mutation intervened", () => {
    expect(
      isStaleProjectResponse({ seq: 2, latestSeq: 2, mutatedAt: 1, mutationSeq: 1 }),
    ).toBe(false);
  });
});