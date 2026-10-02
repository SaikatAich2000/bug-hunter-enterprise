/**
 * Events view — list mode (card grid, debounced search, date filter) and
 * detail mode (one event + filterable item table).
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import MsFilter from "../components/MsFilter";
import EventModal from "../modals/EventModal";
import ItemPicker from "../components/ItemPicker";
import { confirmDialog } from "../components/ConfirmHost";
import { api } from "../lib/api";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { debounce, formatDate, initials } from "../lib/format";
import { DATA_POLL_MS, useApp } from "../state/AppContext";
import { itemTypeIcon as itemTypeEmoji } from "../lib/itemTypes";

/** Button label for "add existing tasks", with count and plural handling. */
function addExistingTasksLabel(count) {
  const shown = count || "";
  const plural = count === 1 ? "" : "s";
  return `Add ${shown} task${plural}`.trim();
}

/* Items-table columns — "All" minus actions (items are deleted via the bug modal). */

const DETAIL_COLS = [
  "id", "title-with-type", "project", "status", "priority", "due", "assignees", "att",
];

const COL_HEAD_LABEL = {
  id: "#",
  "title-with-type": "Title",
  project: "Project",
  status: "Status",
  priority: "Priority",
  due: "Due",
  assignees: "Assignees",
  att: "📎",
};

function matchesEventFilter(ev, q, date) {
  if (date && (ev.scheduled_for || "") !== date) return false;
  if (q) {
    const hay = `${ev.name || ""} ${ev.description || ""}`.toLowerCase();
    if (!hay.includes(q)) return false;
  }
  return true;
}

function eventsSummaryText(rows, totalItems, totalPeople, q, date) {
  if (rows.length === 0) return q || date ? "0 matching events" : "";
  const peopleLabel = rows.length === 1 ? `${totalPeople}` : `~${totalPeople}`;
  const prefix = q || date ? "showing " : "";
  return `${prefix}${rows.length} event${rows.length === 1 ? "" : "s"} · `
       + `${totalItems} item${totalItems === 1 ? "" : "s"} · `
       + `${peopleLabel} ${totalPeople === 1 ? "person" : "people"}`;
}

/** Client-side filter state for the tasks inside an event. */

const EMPTY_DETAIL_FILTER = {
  q: "",
  status: [],
  priority: [],
  assignee_id: [],
};

function itemMatchesAssignees(item, assignees) {
  if (!assignees.size) return true;
  const ids = item.assignees.map((a) => String(a.id));
  return ids.some((id) => assignees.has(id));
}

function itemMatchesQuery(item, q, idExact) {
  if (!q) return true;
  // "#<n>" prefix means exact id match only.
  if (idExact) return String(item.id) === q;
  const hay = `${item.title || ""} ${item.description || ""}`.toLowerCase();
  // Bare number: match by id OR substring.
  if (/^\d+$/.test(q)) return String(item.id) === q || hay.includes(q);
  return hay.includes(q);
}

function filterEventItems(items, f) {
  const raw = (f.q || "").trim().toLowerCase();
  // Strip a leading "#<digits>" id prefix before the rest of the filter runs.
  const idMatch = /^#(\d+)$/.exec(raw);
  const idExact = idMatch !== null;
  const q = idExact ? idMatch[1] : raw.replace(/^#/, "");
  const statuses = new Set(f.status);
  const priorities = new Set(f.priority);
  const assignees = new Set(f.assignee_id.map(String));
  return items.filter(
    (b) =>
      (!statuses.size || statuses.has(b.status)) &&
      (!priorities.size || priorities.has(b.priority)) &&
      itemMatchesAssignees(b, assignees) &&
      itemMatchesQuery(b, q, idExact),
  );
}

/** Render one cell for the detail-column subset. */
function renderCell(col, b) {
  switch (col) {
    case "id":
      return <td key={col} className="col-id">#{b.id}</td>;
    case "title-with-type": {
      const itype = b.item_type || "Bug";
      return (
        <td key={col} className="col-title">
          <div className="title-cell">
            <strong className="title-text" title={b.title}>
              <span className="inline-type" data-type={itype} title={itype}>
                {itemTypeEmoji(itype)}
              </span>{" "}
              {b.title}
            </strong>
            <span className="title-meta">
              {itype} · Updated {formatDate(b.updated_at)}
            </span>
          </div>
        </td>
      );
    }
    case "project":
      return <td key={col} className="col-project">{b.project_name || ""}</td>;
    case "status":
      return (
        <td key={col} className="col-status">
          <span className="badge" data-status={b.status}>{b.status}</span>
        </td>
      );
    case "priority":
      return (
        <td key={col} className="col-priority">
          <span className="badge" data-priority={b.priority}>{b.priority}</span>
        </td>
      );
    case "due":
      return (
        <td key={col} className="col-due">
          {b.due_date ? b.due_date : <span className="muted">—</span>}
        </td>
      );
    case "assignees":
      return (
        <td key={col} className="col-assignees">
          <div className="assignee-stack">
            {b.assignees.length ? (
              b.assignees.map((a) => (
                <span key={a.id} className="assignee-chip" title={a.email}>
                  <span className="avatar">{initials(a.name)}</span>
                  <span className="assignee-chip-name">{a.name}</span>
                </span>
              ))
            ) : (
              <span className="muted">—</span>
            )}
          </div>
        </td>
      );
    case "att":
      return (
        <td key={col} className="col-att">
          {b.attachment_count > 0 ? (
            <span className="att-count">📎 {b.attachment_count}</span>
          ) : (
            <span className="muted">—</span>
          )}
        </td>
      );
  }
}

/** One card in the list-mode grid. */
function EventCard({ ev, onOpen }) {
  const onKeyDown = (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    onOpen(ev.id);
  };

  return (
    <button type="button" className="event-card"
      data-event-id={ev.id}
      aria-label={`Open event ${ev.name}`}
      onClick={() => onOpen(ev.id)}
      onKeyDown={onKeyDown}
    >
      <div className="event-card-head">
        <h3 className="event-card-name">{ev.name}</h3>
        {ev.scheduled_for ? (
          <span className="event-card-sched">📅 {ev.scheduled_for}</span>
        ) : (
          <span className="event-card-sched muted small">No date</span>
        )}
      </div>
      {ev.description ? <p className="event-card-desc">{ev.description}</p> : null}
      <div className="event-card-foot">
        <span className="event-card-count">
          {ev.item_count} item{ev.item_count === 1 ? "" : "s"}
        </span>
        <span className="event-card-people">👥 {ev.assignee_count}</span>
        {ev.project_name ? <span className="muted small">📁 {ev.project_name}</span> : null}
        {ev.created_by_name ? <span className="muted small">by {ev.created_by_name}</span> : null}
      </div>
    </button>
  );
}

/** Detail header meta: bits line, description, manager chips. */
function EventDetailMeta({ detail }) {
  const metaBits = [
    ...(detail.project_name ? [`📁 ${detail.project_name}`] : []),
    ...(detail.scheduled_for ? [`📅 ${detail.scheduled_for}`] : []),
    ...(detail.created_by_name ? [`by ${detail.created_by_name}`] : []),
    `${detail.item_count} item${detail.item_count === 1 ? "" : "s"}`,
    `${detail.assignee_count} people`,
  ];
  return (
    <>
      <div className="event-detail-bits">{metaBits.join(" · ")}</div>
      {detail.description ? <p className="event-detail-desc">{detail.description}</p> : null}
      {detail.managers.length > 0 ? (
        <div className="event-detail-managers">
          <span className="muted small">Managers:</span>{" "}
          {detail.managers.map((m) => (
            <span key={m.id} className="assignee-chip" title={m.email}>
              <span className="avatar">{initials(m.name)}</span>
              <span className="assignee-chip-name">{m.name}</span>
            </span>
          ))}
        </div>
      ) : null}
    </>
  );
}

/** One clickable item row inside the detail-mode table. */
function EventItemRow({ b, onOpen }) {
  const onKeyDown = (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    onOpen(b.id);
  };
  return (
    <tr data-bug-id={b.id} tabIndex={0} onClick={() => onOpen(b.id)} onKeyDown={onKeyDown}>
      {DETAIL_COLS.map((c) => renderCell(c, b))}
    </tr>
  );
}

/* Items table — standard bug-table layout without the actions column. */
function EventItemsTable({ items, onOpen }) {
  return (
    <div className="table-scroll">
      <table className="bug-table">
        <thead>
          <tr>
            {DETAIL_COLS.map((c) => (
              <th key={c} className={`col-${c === "title-with-type" ? "title" : c}`}>
                {COL_HEAD_LABEL[c]}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.map((b) => (
            <EventItemRow key={b.id} b={b} onOpen={onOpen} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function EventsView() {
  const {
    view, meta, canManage, isAdmin, openBugForm, openBugDetail, bugModal, refreshAll,
    eventDeepLinkId, setEventDeepLinkId,
  } = useApp();

  // list-mode state
  const [events, setEvents] = useState(null);
  const [loading, setLoading] = useState(false);
  const [qInput, setQInput] = useState("");
  const [q, setQ] = useState(""); // applied (debounced) search
  const [date, setDate] = useState("");

  // detail-mode state
  const [mode, setMode] = useState("list");
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailQInput, setDetailQInput] = useState("");
  const [detailFilters, setDetailFilters] = useState(EMPTY_DETAIL_FILTER);
  const [existingTasksOpen, setExistingTasksOpen] = useState(false);
  const [existingTasks, setExistingTasks] = useState([]);
  const [existingTasksBusy, setExistingTasksBusy] = useState(false);

  // event create/edit modal (local, mounted by this view)
  const [modal, setModal] = useState({ open: false, event: null });

  // Debounced search appliers (200ms).
  const applyQ = useMemo(() => debounce((v) => setQ(v), 200), []);
  const applyDetailQ = useMemo(
    () => debounce((v) => setDetailFilters((f) => ({ ...f, q: v })), 200),
    [],
  );

  const refreshEvents = useCallback(async () => {
    setLoading(true);
    try {
      setEvents(await api("/events"));
    } catch (err) {
      setEvents(null); // blank the grid on error
      toastError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  const openedEventIdRef = useRef(null);
  const openEventDetail = useCallback(async (eventId) => {
    if (openedEventIdRef.current !== eventId) {
      // Different event: clear filters so previous selections don't bleed in.
      // Same-event refreshes (poll, post-save) intentionally keep filters.
      setDetailFilters(EMPTY_DETAIL_FILTER);
      setDetailQInput("");
    }
    openedEventIdRef.current = eventId;
    setMode("detail");
    setDetail(null); // clears stale data while the fetch is in flight
    setDetailLoading(true);
    try {
      setDetail(await api(`/events/${eventId}`));
    } catch (err) {
      toastError(err);
      setMode("list");
    } finally {
      setDetailLoading(false);
    }
  }, []);

  // A notification or /#event=N link asked for this event.
  useEffect(() => {
    if (eventDeepLinkId == null) return;
    setEventDeepLinkId(null);
    void openEventDetail(eventDeepLinkId);
  }, [eventDeepLinkId, setEventDeepLinkId, openEventDetail]);

  // Background refetch — skips the detailLoading flag so the table doesn't flash.
  const refreshDetailSilently = useCallback(async (id) => {
    try {
      setDetail(await api(`/events/${id}`));
    } catch {
      /* keep last good detail */
    }
  }, []);

  const showListMode = useCallback(() => {
    setMode("list");
    setDetail(null);
    openedEventIdRef.current = null;  // so the next open always resets filters
    setExistingTasksOpen(false);
    setExistingTasks([]);
  }, []);

  const addExistingTasks = useCallback(async () => {
    if (!detail || existingTasks.length === 0) return;
    setExistingTasksBusy(true);
    try {
      await Promise.all(existingTasks.map((task) => api(`/bugs/${task.id}`, {
        method: "PUT", json: { event_id: detail.id },
      })));
      toast(`Added ${existingTasks.length} existing task${existingTasks.length === 1 ? "" : "s"}`, "success");
      setExistingTasks([]);
      setExistingTasksOpen(false);
      await openEventDetail(detail.id);
    } catch (err) {
      toastError(err);
    } finally {
      setExistingTasksBusy(false);
    }
  }, [detail, existingTasks, openEventDetail]);

  // Entering the Events view resets to list mode and refetches.
  const prevView = useRef(null);
  useEffect(() => {
    if (view === "events" && prevView.current !== "events") {
      showListMode();
      void refreshEvents();
    }
    prevView.current = view;
  }, [view, refreshEvents, showListMode]);

  // Live poll — list mode refreshes the grid; detail mode refreshes silently.
  // Tick body lives in a ref so the interval always reads the latest state
  // without being restarted on every render.
  const pollRef = useRef(() => {});
  useEffect(() => {
    pollRef.current = () => {
      if (document.hidden) return;
      if (mode === "detail" && detail) void refreshDetailSilently(detail.id);
      else void refreshEvents();
    };
  });
  useEffect(() => {
    if (view !== "events") return;
    const tick = () => pollRef.current();
    const id = setInterval(tick, DATA_POLL_MS);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [view]);

  // Refetch detail when the bug modal closes — the user may have changed an item.
  const prevBugOpen = useRef(bugModal.open);
  useEffect(() => {
    const was = prevBugOpen.current;
    prevBugOpen.current = bugModal.open;
    if (was && !bugModal.open && mode === "detail" && detail) {
      void openEventDetail(detail.id);
    }
  }, [bugModal.open, mode, detail, openEventDetail]);

  // delete
  const handleDeleteEvent = useCallback(async (event) => {
      const ok = await confirmDialog(
        `Delete event "${event.name}"? Its tasks will be kept but unlinked from this event. Cannot be undone`,
      );
      if (!ok) return;
      try {
        await withLoader(async () => {
          await api(`/events/${event.id}`, { method: "DELETE" });
          showListMode();
          await refreshEvents();
        }, "Deleting event…");
        toast(`Event "${event.name}" deleted`, "success");
      } catch (err) {
        toastError(err);
      }
    },
    [refreshEvents, showListMode],
  );

  // post-save
  const handleSaved = useCallback(async (saved) => {
      const isEdit = detail !== null;
      await refreshEvents();
      // Refresh the bell so manager notifications appear right away.
      void refreshAll();
      if (isEdit) {
        if (mode === "detail" && detail?.id === saved.id) await openEventDetail(saved.id);
      } else {
        await openEventDetail(saved.id);
      }
    },
    [refreshEvents, openEventDetail, mode, detail, refreshAll],
  );

  // derived list-mode data
  const all = events ?? [];
  const qNorm = q.trim().toLowerCase();
  const dateNorm = date.trim();
  const rows = all.filter((ev) => matchesEventFilter(ev, qNorm, dateNorm));
  const totalItems = rows.reduce((n, ev) => n + (ev.item_count || 0), 0);
  const totalPeople = rows.reduce((n, ev) => n + (ev.assignee_count || 0), 0);
  const summary = eventsSummaryText(rows, totalItems, totalPeople, qNorm, dateNorm);
  const showEmptyBanner = !loading && events !== null && all.length === 0;

  const openDetail = useCallback((id) => {
      void openEventDetail(id);
    },
    [openEventDetail],
  );
  const openItem = useCallback((id) => {
      void openBugDetail(id);
    },
    [openBugDetail],
  );

  let gridContent = null;
  if (loading) {
    gridContent = <div className="events-loading muted">Loading events…</div>;
  } else if (events !== null && rows.length === 0) {
    gridContent =
      all.length > 0 ? (
        <div className="events-empty muted">
          <p>No events match the current filter. Try clearing the search or date</p>
        </div>
      ) : null;
  } else if (events !== null) {
    gridContent = rows.map((ev) => <EventCard key={ev.id} ev={ev} onOpen={openDetail} />);
  }

  // derived detail-mode data
  const allItems = detail?.items ?? [];
  const filteredItems = filterEventItems(allItems, detailFilters);

  // Build the assignee dropdown from the event's items so only relevant people appear.
  const assigneeOptions = [];
  {
    const seen = new Set();
    for (const it of allItems) {
      for (const a of it.assignees) {
        if (!seen.has(a.id)) {
          seen.add(a.id);
          assigneeOptions.push([String(a.id), a.name]);
        }
      }
    }
  }
  const statusOptions = meta.statuses.map((s) => [s, s]);
  const priorityOptions = meta.priorities.map((p) => [p, p]);

  // Drop any selected assignee that's no longer in the option pool (their last
  // item left the event) so the filter doesn't hold an unclickable ghost id.
  const assigneeOptionKey = assigneeOptions.map(([v]) => v).join(",");
  useEffect(() => {
    const valid = new Set(assigneeOptions.map(([v]) => v));
    setDetailFilters((f) => {
      if (f.assignee_id.every((id) => valid.has(id))) return f;
      return { ...f, assignee_id: f.assignee_id.filter((id) => valid.has(id)) };
    });
    // assigneeOptionKey is a stable string representation of the option set.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assigneeOptionKey]);

  const toggleDetailFilter = (key) => (value) => {
    setDetailFilters((f) => {
      const cur = f[key];
      return {
        ...f,
        [key]: cur.includes(value) ? cur.filter((v) => v !== value) : [...cur, value],
      };
    });
  };

  let itemsContent = null;
  if (detailLoading || !detail) {
    itemsContent = <div className="muted">Loading…</div>;
  } else if (allItems.length === 0) {
    itemsContent = (
      <div className="event-detail-empty muted">
        <p>
          No items yet. Click <strong>+ Add Task</strong> to create one inside this event, or
          open any existing work item and assign it to this event from its Event field
        </p>
      </div>
    );
  } else if (filteredItems.length === 0) {
    itemsContent = (
      <div className="event-detail-empty muted">
        <p>No tasks in this event match the current filter. Try clearing the search or filters</p>
      </div>
    );
  } else {
    itemsContent = <EventItemsTable items={filteredItems} onOpen={openItem} />;
  }

  return (
    <>
      <section className="view" id="viewEvents" hidden={view !== "events"}>
        {/* list mode: card grid of all events */}
        <div className="events-list-mode" id="eventsListMode" hidden={mode !== "list"}>
          <div className="events-controls">
            {canManage && (
              <button
                className="btn primary"
                id="newEventBtn"
                onClick={() => setModal({ open: true, event: null })}
              >
                + New Event
              </button>
            )}
            <button className="btn ghost" id="eventsRefreshBtn" onClick={() => void refreshEvents()}>
              Refresh
            </button>
            <span className="events-summary" id="eventsSummary">{summary}</span>
          </div>
          <div className="events-filter-bar" id="eventsFilterBar">
            <input
              type="search"
              id="eventsSearch"
              placeholder="Search events by name or description…"
              autoComplete="off"
              value={qInput}
              onChange={(e) => {
                setQInput(e.target.value);
                applyQ(e.target.value);
              }}
            />
            <input
              type="date"
              id="eventsDateFilter"
              aria-label="Filter by scheduled date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
            />
            <button
              className="btn ghost"
              id="eventsClearFiltersBtn"
              onClick={() => {
                setQInput("");
                setQ("");
                setDate("");
              }}
            >
              Clear
            </button>
          </div>
          <div className="events-grid" id="eventsGrid">{gridContent}</div>
          <div className="events-empty" id="eventsEmpty" hidden={!showEmptyBanner}>
            <p>
              No events yet. Click <strong>+ New Event</strong> to create one — a standup, a
              sprint meeting, whatever you want to track
            </p>
          </div>
        </div>

        {/* detail mode: a single event + its items */}
        <div className="events-detail-mode" id="eventsDetailMode" hidden={mode !== "detail"}>
          <div className="events-detail-head">
            <button
              className="btn ghost"
              id="eventBackBtn"
              onClick={() => {
                showListMode();
                void refreshEvents();
              }}
            >
              <span aria-hidden="true">←</span> Back
            </button>
            <h2 id="eventDetailName">{detail ? detail.name : "Event"}</h2>
            <div className="events-detail-actions">
              {canManage && (
                <button
                  className="btn ghost"
                  id="editEventBtn"
                  onClick={() => {
                    if (detail) setModal({ open: true, event: detail });
                  }}
                >
                  ✎ Edit
                </button>
              )}
              {isAdmin && (
                <button
                  className="btn danger"
                  id="deleteEventBtn"
                  onClick={() => {
                    if (detail) void handleDeleteEvent(detail);
                  }}
                >
                  🗑 Delete
                </button>
              )}
            </div>
          </div>
          <div className="events-detail-meta" id="eventDetailMeta">
            {detail ? <EventDetailMeta detail={detail} /> : null}
          </div>
          <div className="events-detail-controls">
            {canManage && (
              <>
                <button
                  className="btn primary"
                  id="addItemToEventBtn"
                  onClick={() => {
                    if (detail) openBugForm({ defaultType: "Task", defaultEventId: detail.id });
                  }}
                >
                  + Add New Task
                </button>
                <button
                  className="btn ghost"
                  id="addExistingTaskBtn"
                  onClick={() => setExistingTasksOpen((open) => !open)}
                >
                  {existingTasksOpen ? "Close" : "+ Add Existing Task"}
                </button>
              </>
            )}
          </div>
          {canManage && existingTasksOpen && detail && (
            <div className="board-add-existing event-add-existing">
              <ItemPicker
                selected={existingTasks}
                onChange={setExistingTasks}
                excludeIds={allItems.map((item) => item.id)}
                fixedType="Task"
                placeholder="Search existing tasks by title or #id…"
                disabled={existingTasksBusy}
              />
              <button
                type="button"
                className="btn primary"
                disabled={existingTasksBusy || existingTasks.length === 0}
                onClick={() => void addExistingTasks()}
              >
                {existingTasksBusy ? "Adding…" : addExistingTasksLabel(existingTasks.length)}
              </button>
            </div>
          )}
          <div className="events-detail-filter-bar" id="eventDetailFilterBar">
            <input
              type="search"
              id="eventDetailSearch"
              placeholder="Search tasks (title or #id)…"
              autoComplete="off"
              value={detailQInput}
              onChange={(e) => {
                setDetailQInput(e.target.value);
                applyDetailQ(e.target.value);
              }}
            />
            <MsFilter
              filterKey="status"
              label="Statuses"
              noun="Statuses"
              options={statusOptions}
              selected={detailFilters.status}
              onToggle={toggleDetailFilter("status")}
            />
            <MsFilter
              filterKey="priority"
              label="Priorities"
              noun="Priorities"
              options={priorityOptions}
              selected={detailFilters.priority}
              onToggle={toggleDetailFilter("priority")}
            />
            <MsFilter
              filterKey="assignee_id"
              label="Assignees"
              noun="Assignees"
              options={assigneeOptions}
              selected={detailFilters.assignee_id}
              onToggle={toggleDetailFilter("assignee_id")}
            />
            <button
              className="btn ghost"
              id="eventDetailClearFiltersBtn"
              onClick={() => {
                setDetailFilters(EMPTY_DETAIL_FILTER);
                setDetailQInput("");
              }}
            >
              Clear
            </button>
          </div>
          <div className="events-detail-items" id="eventDetailItems">{itemsContent}</div>
        </div>
      </section>

      <EventModal
        open={modal.open}
        event={modal.event}
        onClose={() => setModal({ open: false, event: null })}
        onSaved={handleSaved}
      />
    </>
  );
}
