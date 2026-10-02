// Shared frontend constants that mirror backend policy.

// Minimum role per view, shared by Sidebar + Shell + TopChrome; the backend is
// the real authority (every route re-checks the caller's role).
export const VIEW_MIN_ROLE = {
  reports: "manager",
  audit: "manager",
  sessions: "admin",
  organization: "manager",
};
