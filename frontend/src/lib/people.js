/** True when `user` can open project `projectId`: admins see every project, everyone else only
 * the projects they belong to. With no project chosen yet, nobody is filtered out. */
export function canSeeProject(user, projectId) {
  const pid = Number.parseInt(projectId, 10);
  if (!Number.isFinite(pid)) return true;
  return user.role === "admin" || (user.project_ids || []).includes(pid);
}

/** Picker items for people who can be attached to work in `projectId`: active project members,
 * plus anyone already selected so they stay visible and can be removed. */
export function projectPeopleItems(users, projectId, selectedIds = []) {
  const selected = new Set(selectedIds);
  return users
    .filter((u) => selected.has(u.id) || (u.is_active && canSeeProject(u, projectId)))
    .map((u) => ({ id: u.id, label: u.is_active ? u.name : `${u.name} (inactive)`, title: u.role }));
}
