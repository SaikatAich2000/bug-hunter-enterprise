import { describe, expect, it } from "vitest";
import { canSeeProject, projectPeopleItems } from "./people";

const admin = { id: 1, name: "Ada", role: "admin", is_active: true, project_ids: [] };
const member = { id: 2, name: "Mia", role: "user", is_active: true, project_ids: [7] };
const outsider = { id: 3, name: "Oli", role: "user", is_active: true, project_ids: [8] };
const gone = { id: 4, name: "Gus", role: "user", is_active: false, project_ids: [7] };

describe("canSeeProject", () => {
  it("lets admins and members see the project, and nobody else", () => {
    expect(canSeeProject(admin, 7)).toBe(true);
    expect(canSeeProject(member, "7")).toBe(true);
    expect(canSeeProject(outsider, 7)).toBe(false);
  });

  it("filters nobody before a project is chosen", () => {
    expect(canSeeProject(outsider, "")).toBe(true);
    expect(canSeeProject(outsider, null)).toBe(true);
  });
});

describe("projectPeopleItems", () => {
  it("offers active members and admins only", () => {
    const items = projectPeopleItems([admin, member, outsider, gone], 7);
    expect(items.map((i) => i.id)).toEqual([1, 2]);
  });

  it("keeps people already selected so they can be removed", () => {
    const items = projectPeopleItems([admin, member, outsider, gone], 7, [3, 4]);
    expect(items.map((i) => i.id)).toEqual([1, 2, 3, 4]);
    expect(items.find((i) => i.id === 4).label).toBe("Gus (inactive)");
  });
});
