/** Active sprint board (Jira Scrum): columns mapped to statuses, swimlanes,
 * quick filters, drag a card to another column to transition it. */
import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import {
  DndContext, DragOverlay, MouseSensor, TouchSensor, closestCorners, pointerWithin,
  useDraggable, useDroppable, useSensor, useSensors,
} from "@dnd-kit/core";
import { useApp } from "../../state/AppContext";
import BhSelect from "../../components/BhSelect";
import { confirmDialog } from "../../components/ConfirmHost";
import { toast, toastError } from "../../lib/toast";
import { agileApi, notifyAgileChanged } from "./agileApi";
import { useLatestLoad } from "./useAgileProject";
import ActionMenu from "./ActionMenu";
import {
  EpicLozenge, EstimateBadge, IssueKey, PriorityIcon, SubtaskProgress, TypeIcon,
} from "./IssueBits";
import { AssigneeStack, formatDateRange, localToday } from "./agileShared";
import { CompleteSprintDialog } from "./SprintDialogs";
import {
  BUILTIN_QUICK_FILTERS, SWIMLANE_MODES, applyQuickFilters, buildSwimlanes, dropStatus, wipState,
} from "./logic/board";
import { daysRemaining } from "./logic/backlog";
import { categoryTotals, formatNumber, statisticUnit } from "./logic/estimates";

const WIP_WARNING = /acknowledge to proceed/i;

// What screen readers hear on a card: there is no keyboard drag on the board.
const DND_ACCESSIBILITY = {
  screenReaderInstructions: {
    draggable: "Open an issue with its title. Use its actions menu to move it to another column.",
  },
};

// A card lands in the cell under the pointer, like Jira; keyboard drags (no
// pointer) fall back to the nearest cell.
function dropUnderPointer(args) {
  const hits = pointerWithin(args);
  return hits.length ? hits : closestCorners(args);
}

function Card({ card, epicsById, mode, columns, onOpen, onMove, onFlag }) {
  const { listeners, setNodeRef, isDragging } = useDraggable({
    id: `card-${card.id}`, data: { card }, disabled: !card.can_edit,
  });
  const menu = [
    { label: "Open issue", onSelect: () => onOpen(card.id) },
    card.can_edit && { separator: true },
    ...(card.can_edit ? columns.flatMap((col) => col.statuses
      .filter((status) => status !== card.status)
      .map((status) => ({ label: `Move to ${status} (${col.name})`, onSelect: () => onMove(card, col, status) }))) : []),
    card.can_edit && { separator: true },
    card.can_edit && { label: card.flagged ? "Remove flag" : "Add flag", onSelect: () => onFlag(card) },
  ];
  // The card is a labelled group that drags with the mouse or a long press;
  // its own buttons (key, title, actions) are what keyboard and screen-reader
  // users work with, as on Jira boards: the actions menu moves the card.
  return (
    <div
      ref={setNodeRef}
      role="group"
      className={`ag-card${card.flagged ? " flagged" : ""}${isDragging ? " dragging" : ""}${card.item_type === "Sub-task" ? " ag-card-sub" : ""}${card.can_edit ? " ag-draggable" : ""}`}
      data-issue-id={card.id}
      {...listeners}
      aria-roledescription="card"
      aria-label={`${card.display_id} ${card.title}, ${card.status}`}
    >
      <div className="ag-card-top">
        <TypeIcon type={card.item_type} />
        <IssueKey issue={card} onOpen={onOpen} />
        {card.flagged && <span className="ag-flag" role="img" aria-label="Flagged" title="Flagged">🚩</span>}
        <span className="ag-spacer" />
        <ActionMenu label={`Actions for ${card.display_id}`} items={menu} />
      </div>
      <button type="button" className="ag-card-title" onClick={() => onOpen(card.id)}>
        {card.title}
      </button>
      {card.parent_id != null && (
        <span className="ag-card-parent" title={`Sub-task of ${card.parent_display_id} ${card.parent_title || ""}`}>
          ↳ {card.parent_display_id}
        </span>
      )}
      <EpicLozenge epicId={card.epic_id} epicsById={epicsById} />
      <div className="ag-card-bottom">
        <PriorityIcon priority={card.priority} />
        <SubtaskProgress issue={card} />
        <span className="ag-spacer" />
        {card.item_type !== "Sub-task" && <EstimateBadge issue={card} mode={mode} />}
        <AssigneeStack assignees={card.assignees} />
      </div>
    </div>
  );
}

function Cell({ id, column, children, dimmed }) {
  const { setNodeRef, isOver } = useDroppable({ id, data: { column } });
  return (
    <div ref={setNodeRef} className={`ag-cell${isOver ? " drop-over" : ""}${dimmed ? " dimmed" : ""}`}
      role="group" data-category={column.category} aria-label={`${column.name} column`}>
      {children}
    </div>
  );
}

export default function BoardPanel({ canManage, board, onOpenTab }) {
  const { openBugDetail, currentUser } = useApp();
  const mode = board.estimation_mode;
  const [sprintId, setSprintId] = useState(null);
  const view = useLatestLoad(() => agileApi.boardView(board.id, sprintId), [board.id, sprintId]);
  const filters = useLatestLoad(() => agileApi.quickFilters(board.id), [board.id]);
  const [active, setActive] = useState(() => new Set());
  const [lanesMode, setLanesMode] = useState(board.swimlane_mode || "none");
  const [collapsedLanes, setCollapsedLanes] = useState({});
  const [dragCard, setDragCard] = useState(null);
  const [completing, setCompleting] = useState(false);
  const [futureSprints, setFutureSprints] = useState([]);
  const [cards, setCards] = useState([]);

  useEffect(() => setLanesMode(board.swimlane_mode || "none"), [board.swimlane_mode]);
  const data = view.data;
  useEffect(() => {
    setCards((data?.columns || []).flatMap((col) => col.cards));
  }, [data]);

  const columns = useMemo(() => data?.columns || [], [data]);
  const epics = useMemo(() => data?.epics || [], [data]);
  const epicsById = useMemo(() => new Map(epics.map((e) => [e.id, e])), [epics]);
  const customFilters = useMemo(() => filters.data || [], [filters.data]);
  const shown = useMemo(
    () => applyQuickFilters(cards, active, { userId: currentUser.id, customFilters }),
    [cards, active, currentUser.id, customFilters],
  );
  const lanes = useMemo(
    () => buildSwimlanes(shown, lanesMode, { epics, currentUserId: currentUser.id }),
    [shown, lanesMode, epics, currentUser.id],
  );
  const countByColumn = useMemo(() => {
    const out = {};
    for (const card of cards) out[card.column_id] = (out[card.column_id] || 0) + 1;
    return out;
  }, [cards]);

  const sensors = useSensors(
    // Mouse: a 5px move starts a drag. Touch: press and hold, so a swipe
    // still scrolls the page.
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
  );

  const transition = async (card, column, status) => {
    // Undo only this card's optimistic move: other cards may be moving too.
    const revert = () => setCards((list) => list.map((c) => (
      c.id === card.id && c.status === status ? { ...c, status: card.status, column_id: card.column_id } : c
    )));
    setCards((list) => list.map((c) => (c.id === card.id ? { ...c, status, column_id: column.id } : c)));
    const send = (acknowledged) => agileApi.transition(card.id, {
      to_status: status, version: card.version, override_wip: false, override_reason: "", acknowledged,
    });
    try {
      try {
        await send(false);
      } catch (err) {
        if (!(err?.status === 400 && WIP_WARNING.test(err.message || ""))) throw err;
        const ok = await confirmDialog(`${err.message.replace(/;\s*acknowledge to proceed\.?$/i, "")}. Move it anyway?`,
          { title: "Column is at its limit", okLabel: "Move anyway", danger: false });
        if (!ok) {
          revert();
          return;
        }
        await send(true);
      }
      notifyAgileChanged();
    } catch (err) {
      revert();
      toastError(err);
    }
  };

  const flag = async (card) => {
    try {
      await agileApi.flag(card.id, { flagged: !card.flagged, version: card.version });
      notifyAgileChanged();
    } catch (err) {
      toastError(err);
    }
  };

  const onDragEnd = ({ active: dragged, over }) => {
    setDragCard(null);
    if (!over) return;
    const card = dragged.data.current?.card;
    const column = over.data.current?.column;
    if (!card || !column) return;
    const status = dropStatus(column, card);
    if (status === undefined) {
      toast(`${column.name} has no status mapped to it. Map one in Board settings.`, "error");
      return;
    }
    if (status === null) return;
    void transition(card, column, status);
  };

  if (!data) return view.loading ? <div className="sprints-loading">Loading…</div> : null;

  const sprint = data.sprint;
  if (!sprint) {
    return (
      <div className="empty-state">
        <h2>No active sprint</h2>
        <p className="muted">Plan a sprint in the backlog and start it to see its board here.</p>
        <button type="button" className="btn primary" onClick={() => onOpenTab("backlog")}>Go to backlog</button>
      </div>
    );
  }

  const today = localToday();
  const remaining = daysRemaining(sprint.end_date, today);
  // Every issue of the sprint, including those whose status is on no column
  // (not drawn, but still part of the sprint when it is completed).
  const sprintIssues = [...cards, ...(data.unmapped || [])].filter((c) => c.item_type !== "Sub-task");
  const totals = categoryTotals(sprintIssues, mode);
  const toggleFilter = (key) => setActive((prev) => {
    const next = new Set(prev);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });
  const hidden = cards.length - shown.length;

  return (
    <div className="ag-board">
      <header className="ag-board-head">
        <div>
          <h2 className="ag-board-title">{sprint.name}</h2>
          <p className="muted small">
            {formatDateRange(sprint.start_date, sprint.end_date)}
            {remaining != null && ` · ${remaining} day${remaining === 1 ? "" : "s"} remaining`}
            {` · ${formatNumber(totals.done)} of ${formatNumber(totals.total)} ${statisticUnit(mode)} done`}
          </p>
          {sprint.goal && <p className="ag-goal">🎯 {sprint.goal}</p>}
        </div>
        <span className="ag-spacer" />
        {data.active_sprints.length > 1 && (
          <BhSelect ariaLabel="Active sprint" value={String(sprint.id)} onChange={(v) => setSprintId(Number(v))}
            options={data.active_sprints.map((s) => ({ value: String(s.id), label: s.name }))} />
        )}
        {canManage && sprint.state === "active" && (
          <button type="button" className="btn primary btn-sm" onClick={async () => {
            try {
              setFutureSprints((await agileApi.sprints(board.id)).filter((s) => s.state === "future"));
            } catch (err) {
              toastError(err);
            }
            setCompleting(true);
          }}>Complete sprint</button>
        )}
      </header>

      <div className="ag-quickfilters" role="toolbar" aria-label="Quick filters">
        <span className="muted small">Quick filters:</span>
        {[...BUILTIN_QUICK_FILTERS.map((f) => ({ id: f.key, name: f.name })), ...customFilters].map((f) => (
          <button key={f.id} type="button" className={`ag-chip${active.has(f.id) ? " active" : ""}`}
            aria-pressed={active.has(f.id)} onClick={() => toggleFilter(f.id)}>{f.name}</button>
        ))}
        {active.size > 0 && (
          <button type="button" className="btn ghost btn-sm" onClick={() => setActive(new Set())}>Clear</button>
        )}
        <span className="ag-spacer" />
        <label className="muted small" htmlFor="ag-lanes">Group by</label>
        <BhSelect id="ag-lanes" ariaLabel="Group by" value={lanesMode} onChange={setLanesMode} options={SWIMLANE_MODES} />
      </div>

      {data.unmapped.length > 0 && (
        <p className="ag-notice" role="status">
          {data.unmapped.length} issue{data.unmapped.length === 1 ? " is" : "s are"} not shown because
          {data.unmapped.length === 1 ? " its" : " their"} status ({[...new Set(data.unmapped.map((c) => c.status))].join(", ")})
          {" "}is not in any column.
          {canManage && <> <button type="button" className="ag-link" onClick={() => onOpenTab("settings")}>Map it in Board settings</button>.</>}
        </p>
      )}
      {hidden > 0 && <p className="muted small">{hidden} card{hidden === 1 ? "" : "s"} hidden by quick filters.</p>}

      <DndContext sensors={sensors} collisionDetection={dropUnderPointer} accessibility={DND_ACCESSIBILITY}
        onDragStart={({ active: a }) => setDragCard(a.data.current?.card ?? null)}
        onDragEnd={onDragEnd} onDragCancel={() => setDragCard(null)}>
        <div className="ag-columns" style={{ "--ag-cols": columns.length }}>
          <div className="ag-columns-head">
            {columns.map((col) => {
              const count = countByColumn[col.id] || 0;
              const state = wipState(col, count);
              return (
                <div key={col.id} className={`ag-col-head ag-wip-${state}`} data-category={col.category}>
                  <span className="ag-col-name">{col.name}</span>
                  <span className="ag-col-count" title={state === "over" ? "Over the column's maximum" : state === "under" ? "Under the column's minimum" : ""}>
                    {count}
                    {col.min_cards != null && ` · min ${col.min_cards}`}
                    {col.wip_limit != null && ` · max ${col.wip_limit}`}
                  </span>
                </div>
              );
            })}
          </div>
          {lanes.map((lane) => {
            const isCollapsed = collapsedLanes[lane.key];
            return (
              <section key={lane.key} className="ag-lane" aria-label={lane.title || "Board"}>
                {lane.title && (
                  <button type="button" className="ag-lane-head" aria-expanded={!isCollapsed}
                    onClick={() => setCollapsedLanes((c) => ({ ...c, [lane.key]: !c[lane.key] }))}>
                    <span aria-hidden="true">{isCollapsed ? "▸" : "▾"}</span>
                    {lane.color && <span className="ag-lane-dot" style={{ background: lane.color }} />}
                    <span>{lane.title}</span>
                    {lane.parent && <span className="ag-lozenge ag-cat-todo">{lane.parent.status}</span>}
                    <span className="muted small">{lane.cards.length} issue{lane.cards.length === 1 ? "" : "s"}</span>
                  </button>
                )}
                {!isCollapsed && (
                  <div className="ag-lane-grid">
                    {columns.map((col) => (
                      <Cell key={col.id} id={`${lane.key}|${col.id}`} column={col}
                        dimmed={dragCard != null && dropStatus(col, dragCard) === undefined}>
                        {lane.cards.filter((c) => c.column_id === col.id).map((card) => (
                          <Card key={card.id} card={card} epicsById={epicsById} mode={mode} columns={columns}
                            onOpen={openBugDetail} onMove={transition} onFlag={flag} />
                        ))}
                      </Cell>
                    ))}
                  </div>
                )}
              </section>
            );
          })}
        </div>
        {createPortal(
          <DragOverlay>
            {dragCard ? (
              <div className="ag-card ag-card-overlay">
                <div className="ag-card-top"><TypeIcon type={dragCard.item_type} /><span className="ag-key">{dragCard.display_id}</span></div>
                <div className="ag-card-title">{dragCard.title}</div>
              </div>
            ) : null}
          </DragOverlay>,
          document.body,
        )}
      </DndContext>

      <CompleteSprintDialog open={completing} sprint={sprint} issues={sprintIssues}
        futureSprints={futureSprints} onClose={(done) => { setCompleting(false); if (done) onOpenTab("backlog"); }} />
    </div>
  );
}
