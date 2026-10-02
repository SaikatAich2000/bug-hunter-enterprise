/** Backlog (Jira Scrum): open sprints above the ranked backlog. Drag issues
 * to rank them or to move them between sprints and the backlog; every row
 * also has a "⋯ Move to" menu, the keyboard alternative. */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  DndContext, DragOverlay, KeyboardSensor, MouseSensor, TouchSensor, closestCenter,
  useDroppable, useSensor, useSensors,
} from "@dnd-kit/core";
import {
  SortableContext, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useApp } from "../../state/AppContext";
import BhSelect from "../../components/BhSelect";
import { confirmDialog } from "../../components/ConfirmHost";
import { toast, toastError } from "../../lib/toast";
import { STANDARD_TYPES, itemTypeLabel } from "../../lib/itemTypes";
import { agileApi, notifyAgileChanged } from "./agileApi";
import { useLatestLoad } from "./useAgileProject";
import ActionMenu from "./ActionMenu";
import {
  CategoryTotals, EpicLozenge, EstimateBadge, IssueKey, PriorityIcon, StatusLozenge, SubtaskProgress, TypeIcon,
} from "./IssueBits";
import { AssigneeStack, formatDateRange, localToday } from "./agileShared";
import { CompleteSprintDialog, SprintFormDialog, StartSprintDialog } from "./SprintDialogs";
import {
  BACKLOG_KEY, containerSprintId, daysRemaining, findContainer, matchesIssueFilter, moveLocally, planDrop, sprintKey,
} from "./logic/backlog";
import { categoryTotals, formatNumber, statisticUnit } from "./logic/estimates";

const EMPTY_FILTER = { text: "", epicId: "", assigneeId: "", type: "" };

function buildContainers(planning) {
  const out = {};
  for (const sprint of planning?.sprints || []) out[sprintKey(sprint.id)] = sprint.issues;
  out[BACKLOG_KEY] = planning?.backlog || [];
  return out;
}

function IssueRow({ issue, epicsById, mode, draggable, selected, onToggle, onOpen, menuItems, dragging }) {
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition, isDragging } = useSortable({
    id: issue.id, disabled: !draggable,
  });
  const style = { transform: CSS.Transform.toString(transform), transition };
  // The row is a labelled group that drags with the mouse or a long press.
  // Keyboard users rank with its drag handle (Space to lift, arrows, Space to
  // drop) or its actions menu; its buttons keep their own meaning.
  return (
    <div
      ref={setNodeRef}
      style={style}
      role="group"
      className={`ag-row${selected ? " selected" : ""}${isDragging || dragging ? " dragging" : ""}${issue.flagged ? " flagged" : ""}${draggable ? " ag-draggable" : ""}`}
      data-issue-id={issue.id}
      {...(draggable ? listeners : {})}
      aria-label={`${issue.display_id} ${issue.title}`}
    >
      {draggable && (
        <button type="button" ref={setActivatorNodeRef} className="ag-drag-handle" {...attributes}
          aria-roledescription="drag handle" aria-label={`Move ${issue.display_id}`}>
          <span aria-hidden="true">⠿</span>
        </button>
      )}
      {draggable && (
        <input
          type="checkbox"
          className="ag-row-select"
          checked={selected}
          aria-label={`Select ${issue.display_id}`}
          onChange={() => onToggle(issue.id)}
          onClick={(e) => e.stopPropagation()}
          onPointerDown={(e) => e.stopPropagation()}
          onMouseDown={(e) => e.stopPropagation()}
          onTouchStart={(e) => e.stopPropagation()}
        />
      )}
      <TypeIcon type={issue.item_type} />
      <IssueKey issue={issue} onOpen={onOpen} />
      <button type="button" className="ag-row-title" onClick={(e) => { e.stopPropagation(); onOpen(issue.id); }}>
        {issue.title}
      </button>
      {issue.flagged && <span className="ag-flag" title="Flagged" role="img" aria-label="Flagged">🚩</span>}
      <EpicLozenge epicId={issue.epic_id} epicsById={epicsById} />
      <SubtaskProgress issue={issue} />
      <StatusLozenge status={issue.status} category={issue.status_category} />
      <AssigneeStack assignees={issue.assignees} />
      <EstimateBadge issue={issue} mode={mode} />
      <PriorityIcon priority={issue.priority} />
      <ActionMenu label={`Actions for ${issue.display_id}`} items={menuItems} />
    </div>
  );
}

function Container({ id, children, empty }) {
  // A list is a drop target of its own only while it is empty; otherwise its
  // issues are, so keyboard moves step from issue to issue (not onto the list).
  const { setNodeRef, isOver } = useDroppable({ id, disabled: !empty });
  return (
    <div ref={setNodeRef} className={`ag-list${isOver ? " drop-over" : ""}`}>
      {children}
      {empty}
    </div>
  );
}

export default function BacklogPanel({ canManage, board, onOpenTab }) {
  const { openBugDetail, openBugForm, users } = useApp();
  const mode = board.estimation_mode;
  const planning = useLatestLoad(() => agileApi.planning(board.id), [board.id]);
  const [containers, setContainers] = useState({});
  const [filter, setFilter] = useState(EMPTY_FILTER);
  const [selected, setSelected] = useState(() => new Set());
  const [collapsed, setCollapsed] = useState({});
  const [dialog, setDialog] = useState(null);
  const [activeId, setActiveId] = useState(null);
  // The lists as they were when the current drag began (null when idle).
  const dragStart = useRef(null);
  // Fresh data that arrived during a drag, applied once the drag is over so
  // the preview never jumps under the pointer.
  const pendingData = useRef(null);

  useEffect(() => {
    if (!planning.data) return;
    if (dragStart.current) pendingData.current = planning.data;
    else setContainers(buildContainers(planning.data));
  }, [planning.data]);

  const data = planning.data;
  const epics = useMemo(() => data?.epics || [], [data]);
  const epicsById = useMemo(() => new Map(epics.map((e) => [e.id, e])), [epics]);
  const sprints = useMemo(() => data?.sprints || [], [data]);
  const activeSprints = sprints.filter((s) => s.state === "active");
  const futureSprints = sprints.filter((s) => s.state === "future");
  const filtering = Object.values(filter).some(Boolean);
  const visible = useCallback(
    (key) => (containers[key] || []).filter((issue) => matchesIssueFilter(issue, filter)),
    [containers, filter],
  );
  const allIssues = useMemo(() => Object.values(containers).flat(), [containers]);
  const issueById = useMemo(() => new Map(allIssues.map((i) => [i.id, i])), [allIssues]);

  const sensors = useSensors(
    // Mouse: a 5px move starts a drag. Touch: press and hold, so a swipe
    // still scrolls the page.
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
      keyboardCodes: { start: ["Space"], cancel: ["Escape"], end: ["Space", "Enter"] },
    }),
  );

  const toggle = (id) => setSelected((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  // Which list each issue is in, so issues in a collapsed list count as hidden.
  const listOf = useMemo(() => {
    const map = new Map();
    for (const [key, issues] of Object.entries(containers)) for (const issue of issues) map.set(issue.id, key);
    return map;
  }, [containers]);
  /** On screen: matches the filter and its list is not collapsed. */
  const isShown = (issue) => matchesIssueFilter(issue, filter) && !collapsed[listOf.get(issue.id)];

  /** Issues moving together: the shown selection (in board order) when the
   * dragged one is selected; a selected issue that is not on screen (filtered
   * out, or in a collapsed list) never moves. */
  const movingFor = (id) => {
    if (!selected.has(id) || selected.size < 2) return [id];
    return allIssues.filter((i) => selected.has(i.id) && (i.id === id || isShown(i))).map((i) => i.id);
  };

  const send = async (itemIds, targetKey, neighbours) => {
    try {
      await agileApi.move(board.id, { item_ids: itemIds, sprint_id: containerSprintId(targetKey), ...neighbours });
      setSelected(new Set());
      notifyAgileChanged();
    } catch (err) {
      toastError(err);
      void planning.reload();
    }
  };

  /** Keyboard/menu move: to the top or bottom of a list. */
  const moveTo = (itemIds, targetKey, where) => {
    const list = (containers[targetKey] || []).filter((i) => !itemIds.includes(i.id));
    setContainers((prev) => moveLocally(prev, itemIds, targetKey, where === "top" ? 0 : list.length));
    const neighbours = where === "top"
      ? { before_id: null, after_id: list[0]?.id ?? null }
      : { before_id: list[list.length - 1]?.id ?? null, after_id: null };
    void send(itemIds, targetKey, neighbours);
  };

  const onDragStart = ({ active }) => {
    dragStart.current = containers;
    setActiveId(active.id);
  };

  const containerOf = (id) => (typeof id === "string" && (id === BACKLOG_KEY || id.startsWith("sprint:"))
    ? id : findContainer(containers, id));

  const onDragOver = ({ active, over }) => {
    if (!over) return;
    const from = findContainer(containers, active.id);
    const to = containerOf(over.id);
    if (!from || !to || from === to) return;
    setContainers((prev) => {
      const target = prev[to] || [];
      const overIndex = target.findIndex((i) => i.id === over.id);
      const index = overIndex >= 0 ? overIndex : target.length;
      return moveLocally(prev, [active.id], to, index);
    });
  };

  /** End of a drag: apply data that arrived meanwhile, or restore the start. */
  const settle = (restore) => {
    const latest = pendingData.current;
    const start = dragStart.current;
    pendingData.current = null;
    dragStart.current = null;
    setActiveId(null);
    if (latest) setContainers(buildContainers(latest));
    else if (restore && start) setContainers(start);
  };

  const onDragEnd = ({ active, over }) => {
    const start = dragStart.current;
    const plan = over && start ? planDrop({
      startContainers: start, containers, activeId: active.id, overId: over.id,
      selectedIds: [...selected], isVisible: isShown,
    }) : null;
    if (!plan) {
      settle(true);
      return;
    }
    settle(false);
    setContainers(moveLocally(start, plan.itemIds, plan.targetKey, plan.index));
    void send(plan.itemIds, plan.targetKey, plan.neighbours);
  };

  const onDragCancel = () => settle(true);

  const deleteSprint = async (sprint) => {
    const count = (containers[sprintKey(sprint.id)] || []).length;
    const ok = await confirmDialog(
      `Delete ${sprint.name}?${count ? ` Its ${count} issue${count === 1 ? "" : "s"} move to the top of the backlog.` : ""}`,
      { title: "Delete sprint", okLabel: "Delete", danger: true },
    );
    if (!ok) return;
    try {
      await agileApi.deleteSprint(sprint.id, {
        version: sprint.version, cancellation_note: "Sprint deleted", default_destination: "backlog",
      });
      toast(`${sprint.name} deleted`, "success");
      notifyAgileChanged();
    } catch (err) {
      toastError(err);
    }
  };

  const moveTargets = (issue) => {
    const here = findContainer(containers, issue.id);
    const ids = movingFor(issue.id);
    const items = [{ label: "Open issue", onSelect: () => openBugDetail(issue.id) }];
    if (!canManage) return items;
    items.push({ separator: true });
    for (const sprint of sprints) {
      if (sprintKey(sprint.id) !== here) {
        items.push({ label: `Move to ${sprint.name}`, onSelect: () => moveTo(ids, sprintKey(sprint.id), "bottom") });
      }
    }
    if (here !== BACKLOG_KEY) items.push({ label: "Move to backlog", onSelect: () => moveTo(ids, BACKLOG_KEY, "top") });
    items.push({ label: "Top of this list", onSelect: () => moveTo(ids, here, "top") });
    items.push({ label: "Bottom of this list", onSelect: () => moveTo(ids, here, "bottom") });
    return items;
  };

  const renderList = (key, emptyText) => {
    const shown = visible(key);
    return (
      <SortableContext items={shown.map((i) => i.id)} strategy={verticalListSortingStrategy}>
        <Container
          id={key}
          empty={shown.length === 0 && (
            <p className="ag-empty-drop">{filtering && (containers[key] || []).length ? "No issues match the filter." : emptyText}</p>
          )}
        >
          {shown.map((issue) => (
            <IssueRow
              key={issue.id}
              issue={issue}
              epicsById={epicsById}
              mode={mode}
              draggable={canManage}
              selected={selected.has(issue.id)}
              dragging={activeId != null && selected.has(issue.id) && selected.has(activeId) && issue.id !== activeId}
              onToggle={toggle}
              onOpen={openBugDetail}
              menuItems={moveTargets(issue)}
            />
          ))}
        </Container>
      </SortableContext>
    );
  };

  if (!data) return planning.loading ? <div className="sprints-loading">Loading…</div> : null;

  const assigneeOptions = [
    { value: "", label: "All assignees" },
    { value: "unassigned", label: "Unassigned" },
    ...(users || []).filter((u) => u.is_active).map((u) => ({ value: String(u.id), label: u.name })),
  ];
  const today = localToday();
  const nextName = `${(board.name || "Board").replace(/ Board$/, "")} Sprint ${sprints.length + 1}`;
  const canStartAnother = data.parallel_sprints || activeSprints.length === 0;
  const activeIssue = activeId != null ? issueById.get(activeId) : null;

  return (
    <div className="ag-backlog">
      <div className="ag-filterbar" role="search">
        <input
          className="ag-search"
          type="search"
          placeholder="Search this board"
          aria-label="Search issues"
          value={filter.text}
          onChange={(e) => setFilter((f) => ({ ...f, text: e.target.value }))}
        />
        <BhSelect ariaLabel="Filter by epic" value={filter.epicId} onChange={(v) => setFilter((f) => ({ ...f, epicId: v }))}
          options={[{ value: "", label: "All epics" }, { value: "none", label: "Issues without epic" },
            ...epics.filter((e) => !e.archived).map((e) => ({ value: String(e.id), label: e.title }))]} />
        <BhSelect ariaLabel="Filter by assignee" value={filter.assigneeId}
          onChange={(v) => setFilter((f) => ({ ...f, assigneeId: v }))} options={assigneeOptions} />
        <BhSelect ariaLabel="Filter by type" value={filter.type} onChange={(v) => setFilter((f) => ({ ...f, type: v }))}
          options={[{ value: "", label: "All types" }, ...STANDARD_TYPES.map((t) => ({ value: t, label: itemTypeLabel(t) }))]} />
        {filtering && <button type="button" className="btn ghost btn-sm" onClick={() => setFilter(EMPTY_FILTER)}>Clear filters</button>}
        {canManage && selected.size > 0 && (
          <span className="ag-selection">
            {selected.size} selected
            <button type="button" className="btn ghost btn-sm" onClick={() => setSelected(new Set())}>Clear</button>
          </span>
        )}
      </div>

      <DndContext sensors={sensors} collisionDetection={closestCenter}
        onDragStart={onDragStart} onDragOver={onDragOver} onDragEnd={onDragEnd} onDragCancel={onDragCancel}>
        {sprints.map((sprint) => {
          const key = sprintKey(sprint.id);
          const issues = containers[key] || [];
          const totals = categoryTotals(issues, mode);
          const isCollapsed = collapsed[key];
          const remaining = sprint.state === "active" ? daysRemaining(sprint.end_date, today) : null;
          return (
            <section key={key} className={`ag-sprint ag-sprint-${sprint.state}`} aria-label={sprint.name}>
              <header className="ag-sprint-head">
                <button type="button" className="ag-collapse" aria-expanded={!isCollapsed}
                  aria-label={isCollapsed ? `Expand ${sprint.name}` : `Collapse ${sprint.name}`}
                  onClick={() => setCollapsed((c) => ({ ...c, [key]: !c[key] }))}>{isCollapsed ? "▸" : "▾"}</button>
                <h3 className="ag-sprint-name">{sprint.name}</h3>
                {sprint.state === "active" && <span className="ag-state ag-state-active">Active</span>}
                <span className="muted small">{formatDateRange(sprint.start_date, sprint.end_date)}</span>
                {remaining != null && <span className="muted small">{remaining} day{remaining === 1 ? "" : "s"} remaining</span>}
                <span className="muted small">{totals.count} issue{totals.count === 1 ? "" : "s"}</span>
                <CategoryTotals totals={totals} mode={mode} />
                <span className="ag-spacer" />
                {canManage && sprint.state === "future" && (
                  <button type="button" className="btn primary btn-sm"
                    disabled={!canStartAnother || issues.length === 0}
                    title={!canStartAnother ? "Complete the active sprint first" : issues.length === 0 ? "Add issues first" : "Start this sprint"}
                    onClick={() => setDialog({ kind: "start", sprint })}>Start sprint</button>
                )}
                {canManage && sprint.state === "active" && (
                  <button type="button" className="btn primary btn-sm" onClick={() => setDialog({ kind: "complete", sprint })}>
                    Complete sprint
                  </button>
                )}
                {canManage && (
                  <ActionMenu label={`Sprint actions for ${sprint.name}`} items={[
                    { label: "Edit sprint", onSelect: () => setDialog({ kind: "edit", sprint }) },
                    sprint.state === "future" && { label: "Delete sprint", danger: true, onSelect: () => deleteSprint(sprint) },
                  ]} />
                )}
              </header>
              {!isCollapsed && sprint.goal && <p className="ag-goal">🎯 {sprint.goal}</p>}
              {!isCollapsed && renderList(key, canManage ? "Plan this sprint by dragging issues here from the backlog." : "No issues in this sprint.")}
              {!isCollapsed && canManage && (
                <button type="button" className="ag-create-link"
                  onClick={() => openBugForm({ defaultProjectId: board.project_id, defaultSprintId: sprint.id })}>
                  + Create issue
                </button>
              )}
            </section>
          );
        })}

        <section className="ag-sprint ag-backlog-section" aria-label="Backlog">
          <header className="ag-sprint-head">
            <h3 className="ag-sprint-name">Backlog</h3>
            <span className="muted small">
              {data.backlog_total} issue{data.backlog_total === 1 ? "" : "s"}
            </span>
            <CategoryTotals totals={categoryTotals(containers[BACKLOG_KEY] || [], mode)} mode={mode} />
            <span className="ag-spacer" />
            {canManage && (
              <button type="button" className="btn ghost btn-sm" onClick={() => setDialog({ kind: "create" })}>Create sprint</button>
            )}
          </header>
          {data.backlog_total > (containers[BACKLOG_KEY] || []).length && (
            <p className="muted small ag-note">
              Showing the top {(containers[BACKLOG_KEY] || []).length} issues by rank. Use search to find the rest.
            </p>
          )}
          {renderList(BACKLOG_KEY, "Your backlog is empty.")}
          <button type="button" className="ag-create-link" onClick={() => openBugForm({ defaultProjectId: board.project_id })}>
            + Create issue
          </button>
        </section>

        {createPortal(
          <DragOverlay>
            {activeIssue ? (
              <div className="ag-row ag-row-overlay">
                <TypeIcon type={activeIssue.item_type} />
                <span className="ag-key">{activeIssue.display_id}</span>
                <span className="ag-row-title">{activeIssue.title}</span>
                {movingFor(activeIssue.id).length > 1 && (
                  <span className="ag-count-badge">{movingFor(activeIssue.id).length} issues</span>
                )}
              </div>
            ) : null}
          </DragOverlay>,
          document.body,
        )}
      </DndContext>

      <p className="muted small ag-hint">
        {canManage
          ? `Drag issues to rank them or plan them into a sprint (tick several to move them together; the ⋯ menu moves without dragging). Estimates in ${statisticUnit(mode)}; ${formatNumber(categoryTotals(allIssues, mode).total)} planned in total.`
          : "Only admins and managers can re-rank or plan issues."}
      </p>

      <SprintFormDialog open={dialog?.kind === "create" || dialog?.kind === "edit"} boardId={board.id}
        sprint={dialog?.kind === "edit" ? dialog.sprint : null} defaultName={nextName} onClose={() => setDialog(null)} />
      <StartSprintDialog open={dialog?.kind === "start"} sprint={dialog?.sprint}
        issues={dialog?.sprint ? containers[sprintKey(dialog.sprint.id)] : []} mode={mode}
        onClose={(started) => { setDialog(null); if (started) onOpenTab?.("board"); }} />
      <CompleteSprintDialog open={dialog?.kind === "complete"} sprint={dialog?.sprint}
        issues={dialog?.sprint ? containers[sprintKey(dialog.sprint.id)] : []} futureSprints={futureSprints}
        onClose={() => setDialog(null)} />
    </div>
  );
}
