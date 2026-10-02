// Filter bar (list view only). Type filter shows on "all" tab; env hidden on Requirement/Task.
import { useCallback, useMemo } from "react";
import { useApp } from "../state/AppContext";
import MsFilter from "../components/MsFilter";
import SavedViews from "./SavedViews";
import { itemTypeIcon } from "../lib/itemTypes";

function toggled(list, v) {
  return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
}

export default function FilterBar() {
  const { view, activeTab, meta, projects, users, filters, setFilters, clearFilters } = useApp();

  const projectOptions = useMemo(
    () => projects.map((p) => [String(p.id), p.name]),
    [projects],
  );
  const itemTypeOptions = useMemo(
    () => meta.item_types.map((t) => [t, `${itemTypeIcon(t)} ${t}`]),
    [meta.item_types],
  );
  const statusOptions = useMemo(
    () => meta.statuses.map((s) => [s, s]),
    [meta.statuses],
  );
  const priorityOptions = useMemo(
    () => meta.priorities.map((s) => [s, s]),
    [meta.priorities],
  );
  const environmentOptions = useMemo(
    () => meta.environments.map((s) => [s, s]),
    [meta.environments],
  );
  const assigneeOptions = useMemo(
    () => users.filter((u) => u.is_active).map((u) => [String(u.id), u.name]),
    [users],
  );

  // Numeric IDs coerced to strings for MsFilter.
  const projectSelected = useMemo(() => filters.project_id.map(String), [filters.project_id]);
  const assigneeSelected = useMemo(() => filters.assignee_id.map(String), [filters.assignee_id]);

  const onToggleProject = useCallback((v) =>
      setFilters((prev) => ({ ...prev, project_id: toggled(prev.project_id, Number(v)) })),
    [setFilters],
  );
  const onToggleItemType = useCallback((v) => setFilters((prev) => ({ ...prev, item_type: toggled(prev.item_type, v) })),
    [setFilters],
  );
  const onToggleStatus = useCallback((v) => setFilters((prev) => ({ ...prev, status: toggled(prev.status, v) })),
    [setFilters],
  );
  const onTogglePriority = useCallback((v) => setFilters((prev) => ({ ...prev, priority: toggled(prev.priority, v) })),
    [setFilters],
  );
  const onToggleEnvironment = useCallback((v) =>
      setFilters((prev) => ({ ...prev, environment: toggled(prev.environment, v) })),
    [setFilters],
  );
  const onToggleAssignee = useCallback((v) =>
      setFilters((prev) => ({ ...prev, assignee_id: toggled(prev.assignee_id, Number(v)) })),
    [setFilters],
  );

  const showTypeFilter = activeTab === "all";
  const showEnvFilter = activeTab !== "Requirement" && activeTab !== "Task";

  return (
    <section className="filter-bar" id="filterBar" hidden={view !== "list"}>
      <MsFilter
        filterKey="project_id"
        label="Projects"
        noun="Projects"
        options={projectOptions}
        selected={projectSelected}
        onToggle={onToggleProject}
      />
      {showTypeFilter && (
        <MsFilter
          filterKey="item_type"
          label="Types"
          noun="Types"
          options={itemTypeOptions}
          selected={filters.item_type}
          onToggle={onToggleItemType}
        />
      )}
      <MsFilter
        filterKey="status"
        label="Statuses"
        noun="Statuses"
        options={statusOptions}
        selected={filters.status}
        onToggle={onToggleStatus}
      />
      <MsFilter
        filterKey="priority"
        label="Priorities"
        noun="Priorities"
        options={priorityOptions}
        selected={filters.priority}
        onToggle={onTogglePriority}
      />
      {showEnvFilter && (
        <MsFilter
          filterKey="environment"
          label="Envs"
          noun="Envs"
          options={environmentOptions}
          selected={filters.environment}
          onToggle={onToggleEnvironment}
        />
      )}
      <MsFilter
        filterKey="assignee_id"
        label="Assignees"
        noun="Assignees"
        options={assigneeOptions}
        selected={assigneeSelected}
        onToggle={onToggleAssignee}
      />
      <button className="btn ghost" id="clearFiltersBtn" onClick={clearFilters}>
        Clear
      </button>
      <SavedViews />
    </section>
  );
}
