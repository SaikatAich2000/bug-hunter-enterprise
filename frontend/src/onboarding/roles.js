/** Which onboarding track a person follows. Organization roles are admin, manager and user; "lead"
 * is a plain user who leads at least one project (the server marks those projects can_manage), so
 * they get the member-and-fields tour that a user without a project does not need. */
export function onboardingRole(user, projects = []) {
  if (user?.role === "admin") return "admin";
  if (user?.role === "manager") return "manager";
  return projects.some((p) => p.can_manage) ? "lead" : "user";
}

/** Welcome-screen copy per track: a one-line subtitle and three points. */
export const WELCOME = {
  admin: {
    subtitle: "You run this organization. Set it up in a few minutes.",
    points: [
      ["Set up your organization.", "Name and logo, your first project and the teammates to invite."],
      ["Keep it safe.", "Turn on two-factor authentication, review sessions and the audit trail."],
      ["Track work.", "Bugs, requirements, tasks and stories, with Sprints once you enable Scrum."],
    ],
  },
  manager: {
    subtitle: "You plan the work and bring people in.",
    points: [
      ["Invite your team.", "Organization > Invitations adds people to the projects you lead."],
      ["Plan sprints.", "Turn on Scrum for a project, then rank the backlog and run the board."],
      ["Report on it.", "Build reports from any filter and download them as Excel."],
    ],
  },
  lead: {
    subtitle: "You lead a project. Here is what is yours to manage.",
    points: [
      ["Manage your project.", "Open it from the sidebar to add members and define custom fields."],
      ["Run the work.", "Items, filters and bulk updates, and Sprints when the project uses Scrum."],
      ["Ask Sleuth.", "The assistant answers questions and makes changes after you confirm."],
    ],
  },
  user: {
    subtitle: "Here is the quickest way to get going.",
    points: [
      ["Track work.", "Bugs, requirements, tasks and stories in one list, with filters and bulk updates."],
      ["Plan sprints.", "A ranked backlog, a sprint board and reports once a manager turns Scrum on."],
      ["Ask Sleuth.", "The assistant answers questions and makes changes after you confirm."],
    ],
  },
};
