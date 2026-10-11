/** The Organizations and Security guides: required sections exist and the UI labels they name are real. */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { findGuide } from "./guides.js";
import { headingsOf } from "../lib/markdown";

const src = (rel) => readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", rel), "utf8");
const h2 = (id) => headingsOf(findGuide(id).markdown).filter((h) => h.level === 2).map((h) => h.text);

describe("Organizations guide", () => {
  it("covers every organization topic", () => {
    expect(h2("organizations")).toEqual([
      "General", "Branding", "Invitations", "Members and project leads", "Webhooks", "Custom fields", "Saved views",
    ]);
  });

  it("states the last-lead safeguard with the server's own wording", () => {
    expect(findGuide("organizations").markdown).toContain("Cannot demote the last project lead. Promote another member first.");
  });

  it("names labels that exist in the UI", () => {
    const md = findGuide("organizations").markdown;
    const checks = [
      ["Send invitation", "org/InvitationsTab.jsx"], ["Revoke", "org/InvitationsTab.jsx"],
      ["Join as project lead", "org/InvitationsTab.jsx"], ["Rotate secret", "org/WebhooksTab.jsx"],
      ["Share with everyone", "shell/SavedViews.jsx"], ["Save view", "shell/SavedViews.jsx"],
      ["Email From address", "org/BrandingTab.jsx"], ["Delete organization", "org/DeleteOrganization.jsx"],
      ["Custom fields", "components/ProjectCustomFields.jsx"],
    ];
    for (const [label, file] of checks) {
      expect(md, label).toContain(label);
      expect(src(file), `${label} in ${file}`).toContain(label);
    }
  });
});

describe("Security guide", () => {
  it("covers every security topic", () => {
    expect(h2("security")).toEqual([
      "Two-factor authentication", "Sessions", "Changing your email address", "Downloading your data",
      "Deleting your account", "Deleting an organization",
    ]);
  });

  it("names labels that exist in the UI", () => {
    const md = findGuide("security").markdown;
    const checks = [
      ["Enable 2FA", "account/SecurityTab.jsx"], ["Turn on", "account/SecurityTab.jsx"],
      ["Send code", "account/ProfileTab.jsx"], ["Confirm", "account/ProfileTab.jsx"],
      ["Download my data", "account/PrivacyTab.jsx"], ["Delete my account", "account/PrivacyTab.jsx"],
      ["Show what will be deleted", "org/DeleteOrganization.jsx"], ["Delete organization forever", "org/DeleteOrganization.jsx"],
    ];
    for (const [label, file] of checks) {
      expect(md, label).toContain(label);
      expect(src(file), `${label} in ${file}`).toContain(label);
    }
  });
});
