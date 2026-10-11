/** Role tracks: who gets which welcome copy and tour, and that every tour step points at a real element. */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { VIEW_MIN_ROLE } from "../types";
import { NAV_ITEMS } from "../shell/navItems";
import { WELCOME, onboardingRole } from "./roles";
import { TOUR_STEPS, TRACKS, stepsFor } from "./tourSteps";

const src = (rel) => readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", rel), "utf8");
const RANK = { user: 1, manager: 2, admin: 3 };
const opener = (role) => (view) => RANK[role === "lead" ? "user" : role] >= RANK[VIEW_MIN_ROLE[view] ?? "user"];

describe("onboardingRole", () => {
  it("maps organization roles, and plain users who lead a project, to a track", () => {
    expect(onboardingRole({ role: "admin" }, [])).toBe("admin");
    expect(onboardingRole({ role: "manager" }, [{ can_manage: true }])).toBe("manager");
    expect(onboardingRole({ role: "user" }, [{ can_manage: false }])).toBe("user");
    expect(onboardingRole({ role: "user" }, [{ can_manage: false }, { can_manage: true }])).toBe("lead");
    expect(onboardingRole({ role: "user" })).toBe("user");
  });
});

describe("tour tracks", () => {
  it("has welcome copy and a tour for each track", () => {
    for (const role of ["admin", "manager", "lead", "user"]) {
      expect(WELCOME[role].points).toHaveLength(3);
      expect(stepsFor(opener(role), role).length).toBeGreaterThan(3);
    }
  });

  it("only lists steps that exist, once each", () => {
    const ids = TOUR_STEPS.map((s) => s.id);
    for (const [role, list] of Object.entries(TRACKS)) {
      expect(new Set(list).size, role).toBe(list.length);
      for (const id of list) expect(ids, `${role}: ${id}`).toContain(id);
    }
  });

  it("never shows a step for a section the role cannot open", () => {
    const views = (role) => stepsFor(opener(role), role).filter((s) => s.view).map((s) => s.view);
    expect(views("admin")).toEqual(["organization", "reports", "audit", "sessions"]);
    expect(views("manager")).toEqual(["reports", "organization"]);
    expect(views("lead")).toEqual([]);
    expect(views("user")).toEqual([]);
  });

  it("differs by role: the admin tour covers organization, audit and sessions, a user's does not", () => {
    const ids = (role) => stepsFor(opener(role), role).map((s) => s.id);
    expect(ids("admin")).toEqual(expect.arrayContaining(["organization", "audit", "sessions"]));
    expect(ids("lead")).toContain("my-projects");
    expect(ids("user")).toEqual(["items", "sprints", "events", "sleuth", "settings"]);
    expect(stepsFor(opener("admin"), "admin").find((s) => s.id === "settings").body).toMatch(/Organization setup/);
    expect(stepsFor(opener("user"), "user").find((s) => s.id === "settings").body).not.toMatch(/Organization setup/);
  });

  it("anchors every step to an element the app really renders", () => {
    const views = new Set(NAV_ITEMS.map((n) => n.view));
    const sidebar = src("shell/Sidebar.jsx");
    const chrome = src("shell/TopChrome.jsx") + src("shell/ProfileMenu.jsx") + src("App.jsx");
    for (const step of TOUR_STEPS) {
      const nav = /^\.nav-btn\[data-view="(\w+)"\]$/.exec(step.selector);
      if (nav) expect(views.has(nav[1]), `${step.id}: ${step.selector}`).toBe(true);
      else if (step.selector.startsWith("#")) {
        const id = step.selector.slice(1);
        expect(sidebar + chrome, `${step.id}: ${step.selector}`).toContain(`id="${id}"`);
      } else if (step.selector.startsWith(".")) {
        const all = chrome + src("sleuth/SleuthPanel.jsx") + sidebar;
        for (const part of step.selector.split(",").map((s) => s.trim())) {
          const needle = part.startsWith("#") ? `id="${part.slice(1)}"` : part.slice(1);
          expect(all, `${step.id}: ${part}`).toContain(needle);
        }
      } else throw new Error(`unexpected selector ${step.selector}`);
    }
  });
});
