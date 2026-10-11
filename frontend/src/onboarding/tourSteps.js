/** Product tour steps. `selector` is the control the coach mark points at; when it is not on screen
 * (a narrow window hides the top bar, say) the step is shown centred instead. `view` ties a step to a
 * restricted section so people who cannot open it never see it. `body` is a string, or an object
 * keyed by track when the wording differs ("default" covers the rest).
 *
 * Every track lists the ids it walks through, in order (TRACKS). Each selector is an element the
 * app really renders for that track: nav buttons come from NAV_ITEMS, the project controls from the
 * sidebar. */
export const TOUR_STEPS = [
  {
    id: "items",
    selector: '.nav-btn[data-view="list"]',
    title: "Work items",
    body: "Every bug, requirement, task and story lives here. Use + New Item to add one, the filters to find it, and the checkboxes to update many at once.",
  },
  {
    id: "organization",
    view: "organization",
    selector: '.nav-btn[data-view="organization"]',
    title: "Organization",
    body: {
      admin: "Your organization's name and branding, invitations, webhooks and the organization-wide settings. Start with Invitations to bring your team in.",
      default: "Invite people to the projects you lead. Admins manage the rest of the organization settings.",
    },
  },
  {
    id: "new-project",
    selector: "#newProjectBtn",
    title: "Projects",
    body: "Click + to create a project. Tick Enable Agile to use Sprints, and add members and custom fields once it exists.",
  },
  {
    id: "my-projects",
    selector: "#projectList",
    title: "Your projects",
    body: "Click a project's name to edit it. As a project lead you can add or remove its members and define its custom fields. A project always keeps at least one lead.",
  },
  {
    id: "sprints",
    selector: '.nav-btn[data-view="sprints"]',
    title: "Sprints",
    body: "Plan a backlog, run a sprint on a board and review it with reports. A manager sets Scrum up once per project.",
  },
  {
    id: "events",
    selector: '.nav-btn[data-view="events"]',
    title: "Events",
    body: "Group items into a standup, a review or a release, and track them together.",
  },
  {
    id: "reports",
    view: "reports",
    selector: '.nav-btn[data-view="reports"]',
    title: "Reports",
    body: "Build a report from any filters, preview it, then download the full result as an Excel file.",
  },
  {
    id: "audit",
    view: "audit",
    selector: '.nav-btn[data-view="audit"]',
    title: "Audit trail",
    body: "Every create, update, delete and sign-in is recorded here for incident review.",
  },
  {
    id: "sessions",
    view: "sessions",
    selector: '.nav-btn[data-view="sessions"]',
    title: "Sessions",
    body: "See which devices are signed in and revoke one without touching the others.",
  },
  {
    id: "sleuth",
    selector: ".chrome-sleuth-btn",
    title: "Ask Sleuth",
    body: "The assistant answers questions about your work and can make changes after you confirm. Press Ctrl+/ (or Cmd+/) to open it from anywhere.",
  },
  {
    id: "settings",
    selector: "#profileBtn",
    title: "Settings and help",
    body: {
      admin: "Your account menu holds Account settings (two-factor authentication, data export), the theme, the Help center, this tour and Organization setup.",
      default: "Your account menu holds the theme, Change password, the Help center and this tour, which you can restart whenever you like.",
    },
  },
];

export const TRACKS = {
  admin: ["items", "organization", "new-project", "sprints", "reports", "audit", "sessions", "sleuth", "settings"],
  manager: ["items", "new-project", "sprints", "events", "reports", "organization", "sleuth", "settings"],
  lead: ["items", "my-projects", "sprints", "events", "sleuth", "settings"],
  user: ["items", "sprints", "events", "sleuth", "settings"],
};

/** Steps this person can actually use, in their track's order. `canOpen(view)` is the role check
 * the nav already uses; `role` is the track from onboardingRole(). */
export function stepsFor(canOpen, role = "user") {
  const byId = new Map(TOUR_STEPS.map((s) => [s.id, s]));
  return (TRACKS[role] ?? TRACKS.user)
    .map((id) => byId.get(id))
    .filter((s) => s && (!s.view || canOpen(s.view)))
    .map((s) => (typeof s.body === "string" ? s : { ...s, body: s.body[role] ?? s.body.default }));
}
