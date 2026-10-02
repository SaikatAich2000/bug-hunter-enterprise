/**
 * Audit Trail view. Paginates GET /api/audit; filter changes replace the list,
 * "Load older entries" appends, a short page signals end-of-history.
 */
import { memo, useCallback, useEffect, useRef, useState } from "react";
import { api, apiBlob } from "../lib/api";
import { saveBlob } from "../lib/download";
import { withLoader } from "../lib/loader";
import { toastError } from "../lib/toast";
import { activityIcon, formatDate } from "../lib/format";
import { DATA_POLL_MS, useApp } from "../state/AppContext";
// 300 rows/page: light first paint, within the backend cap.
const AUDIT_PAGE_SIZE = 300;

// Memoized so typing in the search box doesn't re-render the whole list.
const AuditRowView = memo(function AuditRowView({ r }) {
  return (
    <div className="audit-row">
      <span className="audit-icon">{activityIcon(r.action)}</span>
      <div className="audit-text">
        <div>
          <span className="audit-actor">{r.actor_name}</span>
          <span className="audit-action">{r.action}</span>
          {r.entity_type ? (
            <span className="audit-entity">
              {r.entity_type}
              {r.entity_id ? `#${r.entity_id}` : ""}
            </span>
          ) : null}
        </div>
        {r.detail ? <div className="audit-detail">{r.detail}</div> : null}
      </div>
      <span className="audit-time">{formatDate(r.created_at)}</span>
    </div>
  );
});

export default function AuditView() {
  const { users } = useApp();

  const [entity, setEntity] = useState("");
  const [actor, setActor] = useState("");
  const [q, setQ] = useState("");
  const [rows, setRows] = useState([]);
  /** Short last page means no more history. */
  const [drained, setDrained] = useState(true);
  /** Guards the empty state until the first fetch completes. */
  const [loaded, setLoaded] = useState(false);
  /** Incremented by Refresh/Clear to trigger an immediate re-fetch. */
  const [tick, setTick] = useState(0);

  // Stable ref so fetchAudit always reads the latest filter values (synced after commit).
  const filtersRef = useRef({ entity, actor, q });
  useEffect(() => {
    filtersRef.current = { entity, actor, q };
  }, [entity, actor, q]);
  // Running count of loaded rows, used  next page offset.
  const loadedCountRef = useRef(0);
  // Last-fetched q — lets the debounce skip a request Clear already fired.
  const lastFetchedQRef = useRef(null);

  const fetchAudit = useCallback(async (clean) => {
    const f = filtersRef.current;
    lastFetchedQRef.current = f.q;
    const params = new URLSearchParams();
    const qt = f.q.trim();
    if (f.entity) params.set("entity_type", f.entity);
    if (f.actor) params.set("actor_user_id", f.actor);
    if (qt) params.set("q", qt);
    if (clean) loadedCountRef.current = 0;
    params.set("limit", String(AUDIT_PAGE_SIZE));
    params.set("offset", String(loadedCountRef.current));
    try {
      const page = await api(`/audit?${params.toString()}`);
      loadedCountRef.current += page.length;
      // Offset paging over a growing table can repeat rows — de-dupe by id.
      setRows((prev) => {
        if (clean) return page;
        const seen = new Set(prev.map((r) => r.id));
        return [...prev, ...page.filter((r) => !seen.has(r.id))];
      });
      setDrained(page.length < AUDIT_PAGE_SIZE);
    } catch (err) {
      toastError(err);
      setDrained(true); // don't let "Load older" retry a failed request
    } finally {
      setLoaded(true); // empty-state, not a blank screen, on first-fetch error
    }
  }, []);

  // On mount and whenever entity, actor, or tick changes: clean fetch.
  useEffect(() => {
    void fetchAudit(true);
  }, [entity, actor, tick, fetchAudit]);

  // Search debounced 300 ms; skips mount and q already fetched above.
  const qFirstRef = useRef(true);
  useEffect(() => {
    if (qFirstRef.current) {
      qFirstRef.current = false;
      return;
    }
    const t = setTimeout(() => {
      if (filtersRef.current.q === lastFetchedQRef.current) return;
      void fetchAudit(true);
    }, 300);
    return () => clearTimeout(t);
  }, [q, fetchAudit]);

  // Background poll; paused while hidden.
  useEffect(() => {
    const refresh = () => {
      if (document.hidden) return;
      if (loadedCountRef.current > AUDIT_PAGE_SIZE) return; // user paged back; don't collapse
      void fetchAudit(true);
    };
    const id = setInterval(refresh, DATA_POLL_MS);
    document.addEventListener("visibilitychange", refresh);
    return () => { clearInterval(id); document.removeEventListener("visibilitychange", refresh); };
  }, [fetchAudit]);

  // The same filters as the list, as a CSV file for compliance reviews.
  const onExport = async () => {
    const f = filtersRef.current;
    const params = new URLSearchParams();
    if (f.entity) params.set("entity_type", f.entity);
    if (f.actor) params.set("actor_user_id", f.actor);
    if (f.q.trim()) params.set("q", f.q.trim());
    try {
      const { blob, filename } = await withLoader(
        () => apiBlob(`/audit/export.csv?${params.toString()}`),
        "Preparing export…",
      );
      saveBlob(blob, filename || "audit.csv");
    } catch (err) {
      toastError(err);
    }
  };

  // Reset filters; tick bump triggers the immediate clean fetch.
  const onClear = () => {
    setEntity("");
    setActor("");
    setQ("");
    setTick((t) => t + 1);
  };

  return (
    <section className="view" id="viewAudit">
      <div className="audit-controls">
        <select
          id="auditEntityFilter"
          aria-label="Filter by entity"
          value={entity}
          onChange={(e) => setEntity(e.target.value)}
        >
          <option value="">All entities</option>
          <option value="bug">Bugs</option>
          <option value="user">Users</option>
          <option value="project">Projects</option>
          <option value="attachment">Attachments</option>
          <option value="session">Sessions</option>
          <option value="auth">Auth events</option>
        </select>
        <select
          id="auditActorFilter"
          aria-label="Filter by actor"
          value={actor}
          onChange={(e) => setActor(e.target.value)}
        >
          <option value="">All actors</option>
          {users.map((u) => (
            <option key={u.id} value={String(u.id)}>
              {u.name}
            </option>
          ))}
        </select>
        <input
          id="auditSearch"
          type="search"
          placeholder="Search audit log (try bug #, user name, action…)"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <button type="button" className="btn ghost" id="auditClearBtn" onClick={onClear}>
          Clear
        </button>
        <button
          type="button"
          className="btn ghost"
          id="auditRefreshBtn"
          onClick={() => setTick((t) => t + 1)}
        >
          Refresh
        </button>
        <button type="button" className="btn ghost" id="auditExportBtn" onClick={() => void onExport()}>
          Export CSV
        </button>
      </div>
      <div className="audit-list" id="auditList">
        {loaded && rows.length === 0 ? (
          <p className="no-content">No audit events match</p>
        ) : null}
        {rows.map((r) => <AuditRowView key={r.id} r={r} />)}
        {!drained && rows.length > 0 ? (
          <div className="audit-load-more">
            <button
              type="button"
              className="btn ghost"
              id="auditLoadMoreBtn"
              onClick={() => void fetchAudit(false)}
            >
              Load older entries
            </button>
            <span className="muted small">Showing {rows.length} entries</span>
          </div>
        ) : null}
        {drained && rows.length > 0 ? (
          <div className="audit-load-more muted small">
            — end of history ({rows.length} entries) —
          </div>
        ) : null}
      </div>
    </section>
  );
}
