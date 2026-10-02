/** The agile endpoints the Sprints views use, in one place. */
import { api } from "../../lib/api";

const json = (method, body) => ({ method, json: body });

export const agileApi = {
  settings: (projectId) => api(`/agile/projects/${projectId}/settings`),
  enable: (projectId) => api(`/agile/projects/${projectId}/enable`, json("POST", { feature_flags: {} })),

  board: (boardId) => api(`/agile/boards/${boardId}`),
  updateBoard: (boardId, body) => api(`/agile/boards/${boardId}`, json("PUT", body)),
  saveColumns: (boardId, body) => api(`/agile/boards/${boardId}/columns`, json("PUT", body)),
  boardView: (boardId, sprintId) =>
    api(`/agile/boards/${boardId}/view${sprintId ? `?sprint_id=${sprintId}` : ""}`),
  planning: (boardId) => api(`/agile/boards/${boardId}/planning`),
  move: (boardId, body) => api(`/agile/boards/${boardId}/move`, json("POST", body)),
  transition: (itemId, body) => api(`/agile/work-items/${itemId}/transition`, json("POST", body)),
  flag: (itemId, body) => api(`/agile/work-items/${itemId}/flag`, json("POST", body)),
  hierarchy: (itemId, body) => api(`/agile/work-items/${itemId}/hierarchy`, json("PUT", body)),
  projectHierarchy: (projectId, params = "") => api(`/agile/projects/${projectId}/hierarchy${params}`),

  sprints: (boardId) => api(`/agile/sprints?board_id=${boardId}`),
  createSprint: (boardId, body) => api(`/agile/sprints?board_id=${boardId}`, json("POST", body)),
  updateSprint: (sprintId, body) => api(`/agile/sprints/${sprintId}`, json("PUT", body)),
  startSprint: (sprintId, body) => api(`/agile/sprints/${sprintId}/start`, json("POST", body)),
  completeSprint: (sprintId, body) => api(`/agile/sprints/${sprintId}/complete`, json("POST", body)),
  deleteSprint: (sprintId, body) => api(`/agile/sprints/${sprintId}/cancel`, json("POST", body)),

  epics: (projectId) => api(`/agile/epics?project_id=${projectId}`),
  updateEpic: (epicId, body) => api(`/agile/epics/${epicId}`, json("PUT", body)),
  labels: (projectId) => api(`/agile/labels?project_id=${projectId}`),

  quickFilters: (boardId) => api(`/agile/boards/${boardId}/quick-filters`),
  createQuickFilter: (boardId, body) => api(`/agile/boards/${boardId}/quick-filters`, json("POST", body)),
  updateQuickFilter: (filterId, body) => api(`/agile/quick-filters/${filterId}`, json("PUT", body)),
  deleteQuickFilter: (filterId) => api(`/agile/quick-filters/${filterId}`, { method: "DELETE" }),

  sprintReport: (sprintId) => api(`/agile/reports/sprint/${sprintId}`),
  burndown: (sprintId) => api(`/agile/reports/burndown?sprint_id=${sprintId}`),
  velocity: (boardId, count = 7) => api(`/agile/reports/velocity?board_id=${boardId}&sprint_count=${count}`),
  cumulativeFlow: (boardId, from, to, includeSubtasks = true) =>
    api(`/agile/reports/cumulative-flow?board_id=${boardId}&date_from=${from}&date_to=${to}&include_subtasks=${includeSubtasks}`),
  controlChart: (boardId, from, to) =>
    api(`/agile/reports/control-chart?board_id=${boardId}&date_from=${from}&date_to=${to}`),
  epicReport: (epicId) => api(`/agile/reports/epic/${epicId}`),
  daily: (sprintId, date) => api(`/agile/reports/daily?sprint_id=${sprintId}&report_date=${date}`),
  workload: (sprintId) => api(`/agile/reports/workload?sprint_id=${sprintId}`),
};

/** Tell every open Sprints view (and the item list) that agile data changed. */
export function notifyAgileChanged() {
  window.dispatchEvent(new Event("agile:refresh"));
}

/** Subscribe to agile changes made anywhere in the app; returns the cleanup. */
export function onAgileChanged(handler) {
  window.addEventListener("agile:refresh", handler);
  return () => window.removeEventListener("agile:refresh", handler);
}
