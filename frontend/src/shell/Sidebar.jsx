// Sidebar — project/team rail. Swatch/avatar click = filter, name click = edit.
import { useApp } from "../state/AppContext";
import { api } from "../lib/api";
import { toast, toastError } from "../lib/toast";
import { initials } from "../lib/format";
import { getAppName, getStaticAssetUrl, getWordmarkParts } from "../lib/branding";
import { withLoader } from "../lib/loader";
import { confirmDialog } from "../components/ConfirmHost";
import { VIEW_MIN_ROLE } from "../types";
import { NAV_ITEMS } from "./navItems";

// Enter/Space activation for interactive spans (spans keep the grid layout intact).
function keyActivate(fn) {
  return (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fn();
    }
  };
}

export default function Sidebar({ mobileOpen, onNavigate }) {
  const appName = getAppName();
  const wordmark = getWordmarkParts();
  const {
    projects,
    users,
    filters,
    setFilters,
    openProjectForm,
    openUserForm,
    loadProjects,
    loadUsers,
    refreshAll,
    canManage,
    isAdmin,
    view,
    setView,
    roleRank,
    currentUser,
    sidebarCollapsed,
    toggleSidebarCollapsed,
    health,
  } = useApp();

  // Mirrors the TopChrome nav gate.
  const allowed = (v) => {
    const need = VIEW_MIN_ROLE[v];
    return !need || roleRank(currentUser.role) >= roleRank(need);
  };

  const goTo = (v) => {
    setView(v);
    onNavigate?.();
  };

  const toggleProjectFilter = (id) => {
    setFilters((prev) => ({
      ...prev,
      project_id: prev.project_id.includes(id)
        ? prev.project_id.filter((x) => x !== id)
        : [...prev.project_id, id],
    }));
  };

  const toggleAssigneeFilter = (id) => {
    setFilters((prev) => ({
      ...prev,
      assignee_id: prev.assignee_id.includes(id)
        ? prev.assignee_id.filter((x) => x !== id)
        : [...prev.assignee_id, id],
    }));
  };

  const handleDeleteProject = async (id) => {
    const project = projects.find((p) => p.id === id);
    const name = project ? project.name : `#${id}`;
    const ok = await confirmDialog(`Delete project "${name}"?\nThis only works if it has no bugs`);
    if (!ok) return;
    try {
      await withLoader(async () => {
        await api(`/projects/${id}`, { method: "DELETE" });
        // Remove the deleted project from any active filter.
        setFilters((prev) => ({
          ...prev,
          project_id: prev.project_id.filter((v) => v !== id),
        }));
        await loadProjects();
        await refreshAll();
      }, "Deleting project…");
      toast(`Project "${name}" deleted`, "success");
    } catch (err) {
      toastError(err);
    }
  };

  const handleDeleteUser = async (id) => {
    const user = users.find((u) => u.id === id);
    const name = user ? user.name : `#${id}`;
    const ok = await confirmDialog(
      `Delete user "${name}"?\nThis user will be removed from all bug assignments.\nReports they filed will become "unassigned reporter"`,
    );
    if (!ok) return;
    try {
      await withLoader(async () => {
        await api(`/users/${id}`, { method: "DELETE" });
        // Remove the deleted user from any active assignee filter.
        setFilters((prev) => ({
          ...prev,
          assignee_id: prev.assignee_id.filter((v) => v !== id),
        }));
        await loadUsers();
        await refreshAll();
      }, "Deleting user…");
      toast(`User "${name}" deleted`, "success");
    } catch (err) {
      toastError(err);
    }
  };

  const activeProjectIds = new Set(filters.project_id);
  const activeUsers = users.filter((u) => u.is_active);

  return (
    <aside
      className={`sidebar${mobileOpen ? " open" : ""}`}
      id="sidebar"
      aria-label="Projects and team"
    >
      {/* Mobile-only brand header (desktop brand is in TopChrome). */}
      <div className="sidebar-brand">
        <img className="logo" src={getStaticAssetUrl("icon.png")} alt={appName} />
        <div className="wm">
          <b aria-label={appName}>{wordmark.first}{wordmark.rest && <span>{wordmark.rest}</span>}</b>
          {health && <small>{`Version ${health.version}`}</small>}
          {currentUser.organization_name && <small className="sidebar-org">{currentUser.organization_name}</small>}
        </div>
      </div>

      {/* Drawer-only view nav (hidden above 1180px via CSS). */}
      <nav className="sidebar-nav" aria-label="Main sections">
        {NAV_ITEMS.filter((item) => allowed(item.view)).map((item) => (
          <button
            key={item.view}
            type="button"
            className={`nav-btn${view === item.view ? " active" : ""}`}
            data-view={item.view}
            onClick={() => goTo(item.view)}
          >
            <span className="nav-icon">{item.icon}</span>
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      <section className="side-section">
        <div className="side-section-header">
          <span>Projects</span>
          {canManage && (
            <button
              className="icon-btn"
              id="newProjectBtn"
              title="New project"
              aria-label="New project"
              onClick={() => openProjectForm()}
            >
              +
            </button>
          )}
        </div>
        <ul className="side-list" id="projectList">
          {projects.length === 0 ? (
            <li className="side-item muted no-cursor">No projects — click + to add</li>
          ) : (
            projects.map((p) => (
              <li
                key={p.id}
                className={`side-item${activeProjectIds.has(p.id) ? " active" : ""}`}
                data-project-id={p.id}
                title={p.name}
              >
                <span
                  className="swatch"
                  data-act="filter"
                  style={{ background: p.color }}
                  title="Toggle filter"
                  role="button"
                  tabIndex={0}
                  aria-label={`Toggle filter: ${p.name}`}
                  onClick={() => toggleProjectFilter(p.id)}
                  onKeyDown={keyActivate(() => toggleProjectFilter(p.id))}
                ></span>
                <span
                  className="label-text"
                  data-act="open-project"
                  title={canManage || p.can_manage ? `${p.name} — click to edit` : p.name}
                  role="button"
                  tabIndex={0}
                  onClick={() => (canManage || p.can_manage ? openProjectForm(p) : toggleProjectFilter(p.id))}
                  onKeyDown={keyActivate(() =>
                    canManage || p.can_manage ? openProjectForm(p) : toggleProjectFilter(p.id),
                  )}
                >
                  {p.name}
                </span>
                <span className="row-actions">
                  {(canManage || p.can_manage) && (
                    <button
                      className="icon-btn"
                      data-act="edit-project"
                      data-id={p.id}
                      title="Edit"
                      onClick={() => openProjectForm(p)}
                    >
                      ✎
                    </button>
                  )}
                  {isAdmin && (
                    <button
                      className="icon-btn danger"
                      data-act="delete-project"
                      data-id={p.id}
                      title="Delete"
                      onClick={() => void handleDeleteProject(p.id)}
                    >
                      🗑
                    </button>
                  )}
                </span>
              </li>
            ))
          )}
        </ul>
      </section>

      {canManage && (
        <section className="side-section">
          <div className="side-section-header">
            <span>Team</span>
            <button
              className="icon-btn"
              id="newUserBtn"
              title="New user"
              aria-label="New user"
              onClick={() => openUserForm()}
            >
              +
            </button>
          </div>
          <ul className="side-list" id="userList">
            {activeUsers.length === 0 ? (
              <li className="side-item muted no-cursor">No users yet — click + to add</li>
            ) : (
              activeUsers.map((u) => (
                <li
                  key={u.id}
                  className="side-item"
                  data-user-id={u.id}
                  title={`${u.email}${u.role ? " — " + u.role : ""}`}
                >
                  <span
                    className="avatar"
                    data-act="filter-user"
                    title="Toggle filter"
                    role="button"
                    tabIndex={0}
                    aria-label={`Toggle filter: ${u.name}`}
                    onClick={() => toggleAssigneeFilter(u.id)}
                    onKeyDown={keyActivate(() => toggleAssigneeFilter(u.id))}
                  >
                    {initials(u.name)}
                  </span>
                  <span
                    className="label-text"
                    data-act="open-user"
                    title={canManage ? `${u.name} — click to edit` : u.name}
                    role="button"
                    tabIndex={0}
                    onClick={() => (canManage ? openUserForm(u) : toggleAssigneeFilter(u.id))}
                    onKeyDown={keyActivate(() =>
                      canManage ? openUserForm(u) : toggleAssigneeFilter(u.id),
                    )}
                  >
                    {u.name}
                    {u.role ? <span className="meta"> · {u.role}</span> : null}
                  </span>
                  <span className="row-actions">
                    {canManage && (
                      <button
                        className="icon-btn"
                        data-act="edit-user"
                        data-id={u.id}
                        title="Edit"
                        onClick={() => openUserForm(u)}
                      >
                        ✎
                      </button>
                    )}
                    {isAdmin && (
                      <button
                        className="icon-btn danger"
                        data-act="delete-user"
                        data-id={u.id}
                        title="Delete"
                        onClick={() => void handleDeleteUser(u.id)}
                      >
                        🗑
                      </button>
                    )}
                  </span>
                </li>
              ))
            )}
          </ul>
        </section>
      )}

      {/* Desktop collapse toggle; AppContext persists the state. */}
      <button
        type="button"
        className="sidebar-collapse-btn"
        onClick={toggleSidebarCollapsed}
        aria-controls="sidebar"
        aria-expanded={!sidebarCollapsed}
        title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
      >
        <svg
          className="collapse-icon"
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M15 18l-6-6 6-6" />
        </svg>
        <span className="collapse-label">Collapse sidebar</span>
      </button>

      {/* Mobile-only close button. */}
      <button
        type="button"
        className="sidebar-close-btn"
        onClick={() => onNavigate?.()}
        aria-controls="sidebar"
        title="Close menu"
      >
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M18 6 6 18M6 6l12 12" />
        </svg>
        <span>Close menu</span>
      </button>
    </aside>
  );
}
