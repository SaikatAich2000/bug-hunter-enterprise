/** Canonical project-list helpers shared by AppContext and its tests.
 *
 * Extracted from AppContext so the real normalize/upsert/staleness logic is
 * exercised directly by tests instead of a copied re-implementation.
 */

/** Deduplicate by id and sort case-insensitively by name (id tiebreak). */
export function normalizeProjects(list) {
  const byId = new Map();
  for (const row of list || []) {
    if (row?.id != null && !byId.has(row.id)) byId.set(row.id, row);
  }
  return [...byId.values()].sort(
    (a, b) =>
      String(a.name ?? "")
        .toLowerCase()
        .localeCompare(String(b.name ?? "").toLowerCase()) || a.id - b.id,
  );
}

/** Immutable insert-or-update of one project, keeping the canonical ordering. */
export function upsertProjectIntoList(list, project) {
  if (!project?.id) return list;
  return normalizeProjects([
    ...(list || []).filter((row) => row.id !== project.id),
    project,
  ]);
}

/**
 * True when a GET /projects response must be dropped because either a newer
 * load superseded it or a mutation landed while it was in flight.
 */
export function isStaleProjectResponse({ seq, latestSeq, mutatedAt, mutationSeq }) {
  return seq !== latestSeq || mutatedAt !== mutationSeq;
}
