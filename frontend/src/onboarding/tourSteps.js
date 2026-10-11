/** Product tour steps. `selector` is the control the coach mark points at; when it is not on screen
 * (a narrow window hides the top bar, say) the step is shown centred instead. `view` ties a step to a
 * restricted section so people who cannot open it never see it. */
export const TOUR_STEPS = [
  {
    id: "items",
    selector: '.nav-btn[data-view="list"]',
    title: "Work items",
    body: "Every bug, requirement, task and story lives here. Use + New Item to add one, the filters to find it, and the checkboxes to update many at once.",
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
    id: "sleuth",
    selector: ".chrome-sleuth-btn",
    title: "Ask Sleuth",
    body: "The assistant answers questions about your work and can make changes after you confirm. Press Ctrl+/ (or Cmd+/) to open it from anywhere.",
  },
  {
    id: "settings",
    selector: "#profileBtn",
    title: "Settings and help",
    body: "Your account menu holds the theme, Change password, the Help center and this tour, which you can restart whenever you like.",
  },
];

/** Steps this person can actually use. `canOpen(view)` is the role check the nav already uses. */
export function stepsFor(canOpen) {
  return TOUR_STEPS.filter((s) => !s.view || canOpen(s.view));
}
