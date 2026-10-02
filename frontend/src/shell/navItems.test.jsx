/** Contract tests for the navigation model (`./navItems.js` + `../types.js`).
 *
 * TopChrome and the mobile Sidebar render from the same NAV_ITEMS list, and
 * restricted views defer to VIEW_MIN_ROLE — these tests pin the shared
 * source of truth so a renamed view cannot orphan the sidebar links.
 */
import { describe, expect, it } from "vitest";

import { VIEW_MIN_ROLE } from "../types.js";
import { NAV_ITEMS } from "./navItems.js";

describe("navigation model", () => {
  it("lists every main view with an icon and a label", () => {
    expect(NAV_ITEMS.map((item) => item.view)).toEqual([
      "list",
      "sprints",
      "events",
      "analytics",
      "reports",
      "audit",
      "sessions",
      "organization",
    ]);
    for (const item of NAV_ITEMS) {
      expect(item.icon).toBeTruthy();
      expect(item.label).toBeTruthy();
    }
  });

  it("has no duplicate views", () => {
    const views = NAV_ITEMS.map((item) => item.view);
    expect(new Set(views).size).toBe(views.length);
  });

  it("gates the restricted views through shared role minimums", () => {
    expect(VIEW_MIN_ROLE.reports).toBe("manager");
    expect(VIEW_MIN_ROLE.audit).toBe("manager");
    expect(VIEW_MIN_ROLE.sessions).toBe("admin");
    expect(VIEW_MIN_ROLE.organization).toBe("manager");
    for (const view of Object.keys(VIEW_MIN_ROLE)) {
      expect(NAV_ITEMS.some((item) => item.view === view)).toBe(true);
    }
  });
});
