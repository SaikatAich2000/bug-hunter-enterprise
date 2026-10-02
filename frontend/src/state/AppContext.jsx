/** Global app state: user, meta, directory, bug list + filters, view, shared modals. */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { api } from "../lib/api";
import { getAppName } from "../lib/branding";
import { applyOrgAccent } from "../lib/orgBranding";
import { toast, toastError } from "../lib/toast";
import { showLocalNotification } from "../lib/push";
import { parseDeepLink } from "../lib/deepLink";
import {
  isStaleProjectResponse,
  normalizeProjects,
  upsertProjectIntoList,
} from "../lib/projectList";

/** KPI → status-filter mapping. */
export const KPI_FILTER_MAP = {
  total: [],
  open: ["New", "In Progress", "Reopened"],
  resolved: ["Resolved"],
  closed: ["Closed"],
  resolve_later: ["Resolve Later"],
};

const EMPTY_FILTERS = {
  project_id: [],
  status: [],
  priority: [],
  environment: [],
  assignee_id: [],
  item_type: [],
  reporter_id: null,
  q: "",
};

// Mirrors app/schemas.py (CANONICAL_STATUSES shared by every type) until
// GET /api/meta answers; the server's lists always replace it.
const CANONICAL_STATUSES = [
  "New", "In Progress", "Testing", "In Review", "Approved",
  "Done", "Blocked", "Cancelled", "Resolved", "Closed",
  "Reopened", "Not a Bug", "Resolve Later", "Implemented",
  "Rejected", "Deferred", "Planned", "Completed",
];
const FALLBACK_ITEM_TYPES = ["Bug", "Requirement", "Task", "Epic", "Story", "Sub-task"];

const FALLBACK_META = {
  statuses: CANONICAL_STATUSES,
  statuses_by_type: Object.fromEntries(
    FALLBACK_ITEM_TYPES.map((t) => [t, CANONICAL_STATUSES]),
  ),
  priorities: ["Low", "Medium", "High", "Critical"],
  environments: ["DEV", "UAT", "PROD"],
  item_types: FALLBACK_ITEM_TYPES,
};

const Ctx = createContext(null);

/** Deep-link hash URL for a desktop notification target. */
export function notificationUrl(n) {
  if (n.bug_id) return `/#bug=${n.bug_id}`;
  if (n.event_id) return `/#event=${n.event_id}`;
  return "/";
}

export function useApp() {
  const v = useContext(Ctx);
  if (!v) throw new Error("useApp outside provider");
  return v;
}

const PAGE_SIZE = 20;
const SESSION_POLL_MS = 15_000;
/** Shared poll interval for list, stats, directory, open modal, and per-view pollers. */
export const DATA_POLL_MS = 10_000;
const VERSION_POLL_MS = 5 * 60_000;
/** Cap desktop popups per poll so a bulk import can't flood the notification centre. */
const MAX_DESKTOP_NOTIFS_PER_POLL = 3;

function readLs(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

/** Shallow MeOut compare — unchanged session polls keep object identity (no re-render). */
function sameMe(a, b) {
  return (
    a.id === b.id &&
    a.name === b.name &&
    a.email === b.email &&
    a.role === b.role &&
    a.is_active === b.is_active &&
    a.organization_name === b.organization_name &&
    a.totp_enabled === b.totp_enabled &&
    a.branding?.accent_color === b.branding?.accent_color &&
    a.branding?.logo_data_url === b.branding?.logo_data_url
  );
}

export function AppProvider({
  me,
  children,
}) {
  const [currentUser, setCurrentUser] = useState(me);
  const [accountOpen, setAccountOpen] = useState(false);
  const [meta, setMeta] = useState(FALLBACK_META);
  const [users, setUsers] = useState([]);
  const [projects, setProjects] = useState([]);
  const [stats, setStats] = useState(null);
  const [health, setHealth] = useState(null);

  const [view, setRawView] = useState("list");
  // Set by ReportsView's "View Sprint Reports" link; SprintsView reads and
  // clears this once on mount to open its Sprint Report tab directly.
  const [sprintsDeepLinkTab, setSprintsDeepLinkTab] = useState(null);
  // Event to open once EventsView mounts (notification / #event= deep links).
  const [eventDeepLinkId, setEventDeepLinkId] = useState(null);
  // True once boot has loaded session data (gates notification deep links).
  const [booted, setBooted] = useState(false);
  const [activeTab, setRawActiveTab] = useState(() => {
    const t = readLs("activeTab", "all");
    return ["all", "Bug", "Requirement", "Task"].includes(t)
      ? t
      : "all";
  });
  const [defaultNewType, setRawDefaultNewType] = useState(() => {
    const t = readLs("defaultNewType", "Bug");
    return ["Bug", "Requirement", "Task"].includes(t)
      ? t
      : "Bug";
  });

  const [bugs, setBugs] = useState([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [filters, setRawFilters] = useState(EMPTY_FILTERS);

  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => readLs("sidebarCollapsed", "0") === "1",
  );

  const [bugModal, setBugModal] = useState({ open: false, bug: null });
  const [projectModal, setProjectModal] = useState({ open: false, project: null });
  const [userModal, setUserModal] = useState({ open: false, user: null });
  const [changePasswordOpen, setChangePasswordOpen] = useState(false);
  const [bulkImportOpen, setBulkImportOpen] = useState(false);

  const [notifications, setNotifications] = useState([]);
  const [unreadCount, setUnreadCount] = useState(0);

  // Refs let stable callbacks/pollers read latest values without stale closures.
  const filtersRef = useRef(filters);
  const notificationsRef = useRef(notifications);
  const pageRef = useRef(page);
  const tabRef = useRef(activeTab);
  // Synced after each commit (never during render) so stable callbacks and
  // pollers read the latest values without stale closures.
  useEffect(() => {
    filtersRef.current = filters;
    notificationsRef.current = notifications;
    pageRef.current = page;
    tabRef.current = activeTab;
  });

  // Poll signatures: skip state commits (and re-renders) on unchanged data.
  const lastBugsSig = useRef("");
  const lastStatsSig = useRef("");
  const lastUsersSig = useRef("");
  const lastProjectsSig = useRef("");

  const buildBugParams = useCallback(()=> {
    const f = filtersRef.current;
    const params = new URLSearchParams();
    params.set("page", String(pageRef.current));
    params.set("page_size", String(PAGE_SIZE));
    for (const id of f.project_id) params.append("project_id", String(id));
    for (const s of f.status) params.append("status", s);
    for (const p of f.priority) params.append("priority", p);
    for (const e of f.environment) params.append("environment", e);
    for (const a of f.assignee_id) params.append("assignee_id", String(a));
    for (const t of f.item_type) params.append("item_type", t);
    if (f.reporter_id != null) params.set("reporter_id", String(f.reporter_id));
    if (f.q.trim()) params.set("q", f.q.trim());
    // Implicit tab filter on top.
    const tab = tabRef.current;
    if (tab !== "all" && !f.item_type.includes(tab)) {
      params.append("item_type", tab);
    }
    return params;
  }, []);

  const refreshBugs = useCallback(async () => {
    try {
      const res = await api(`/bugs?${buildBugParams()}`);
      const sig = `${res.total}|${res.pages}|${JSON.stringify(res.items)}`;
      if (sig === lastBugsSig.current) return; // unchanged poll → no re-render
      lastBugsSig.current = sig;
      setBugs(res.items);
      setTotal(res.total);
      setTotalPages(Math.max(1, res.pages));
    } catch (err) {
      toastError(err);
    }
  }, [buildBugParams]);

  const refreshStats = useCallback(async () => {
    try {
      const tab = tabRef.current;
      const params = new URLSearchParams();
      if (tab !== "all") params.set("item_type", tab);
      // Status filter included so Analytics charts react to KPI tile clicks.
      for (const s of filtersRef.current.status) params.append("status", s);
      const qs = params.toString();
      const query = qs ? `?${qs}` : "";
      const res = await api(`/stats${query}`);
      const sig = JSON.stringify(res);
      if (sig === lastStatsSig.current) return; // unchanged poll → no re-render
      lastStatsSig.current = sig;
      setStats(res);
    } catch (err) {
      toastError(err);
    }
  }, []);

  const refreshUnread = useCallback(async () => {
    try {
      const c = await api("/notifications/unread_count");
      setUnreadCount(c.unread);
    } catch {
      /* transient — keep the last known count */
    }
  }, []);

  const refreshAll = useCallback(async () => {
    // Unread badge rides along so mutation sites get a fresh count immediately.
    await Promise.all([refreshBugs(), refreshStats(), refreshUnread()]);
  }, [refreshBugs, refreshStats, refreshUnread]);

  const loadUsers = useCallback(async () => {
    try {
      const res = await api("/users");
      const sig = JSON.stringify(res);
      if (sig === lastUsersSig.current) return; // unchanged → no re-render
      lastUsersSig.current = sig;
      setUsers(res);
    } catch (err) {
      toastError(err);
    }
  }, []);

  // Monotonic ordering for project state: every load gets a sequence number and
  // every optimistic mutation bumps the mutation counter. A load response that
  // started before (or raced with) a mutation is discarded instead of
  // overwriting the fresher mutation result — no reload/delay workaround.
  // The canonical list shape + staleness rule live in lib/projectList.js and are
  // covered directly by frontend/src/lib/projectList.test.jsx.
  const projectLoadSeq = useRef(0);
  const projectMutationSeq = useRef(0);

  const loadProjects = useCallback(async () => {
    const seq = ++projectLoadSeq.current;
    const mutated = projectMutationSeq.current;
    try {
      const res = await api("/projects");
      // Drop a superseded response, or one that raced a mutation.
      if (
        isStaleProjectResponse({
          seq,
          latestSeq: projectLoadSeq.current,
          mutatedAt: mutated,
          mutationSeq: projectMutationSeq.current,
        })
      ) {
        return normalizeProjects(res);
      }
      const next = normalizeProjects(res);
      const sig = JSON.stringify(next);
      if (sig === lastProjectsSig.current) return next; // unchanged → no re-render
      lastProjectsSig.current = sig;
      setProjects(next);
      return next;
    } catch (err) {
      toastError(err);
      return null;
    }
  }, []);

  // Insert or update one project from a captured POST/PUT response so the
  // global list reflects the mutation immediately. Immutable upsert with
  // id-deduplication, case-insensitive ordering and a synchronized signature
  // so an unchanged refetch is still skipped. Returns true when the list
  // changed, false when the payload carried no project (or already matched).
  const upsertProject = useCallback((project) => {
    if (!project?.id) return false;
    projectMutationSeq.current += 1;
    let changed = false;
    setProjects((current) => {
      const next = upsertProjectIntoList(current, project);
      changed = JSON.stringify(next) !== JSON.stringify(current);
      lastProjectsSig.current = JSON.stringify(next);
      return next;
    });
    return changed;
  }, []);

  const notifSeqRef = useRef(0);
  // Monotonic token: discards out-of-order poll responses; bumped by optimistic mutations.
  // Ids already surfaced as a desktop notification. The first load only primes
  // this set, so opening the app doesn't replay a backlog of old notifications.
  const seenNotifIdsRef = useRef(new Set());
  const notifPrimedRef = useRef(false);

  const announceNewNotifications = useCallback((list) => {
    const unseen = list.filter((n) => !n.read_at && !seenNotifIdsRef.current.has(n.id));
    for (const n of list) seenNotifIdsRef.current.add(n.id);
    if (!notifPrimedRef.current) {
      notifPrimedRef.current = true;
      return;
    }
    // Oldest first so the newest ends up on top of the notification stack.
    for (const n of unseen.slice(0, MAX_DESKTOP_NOTIFS_PER_POLL).reverse()) {
      void showLocalNotification(n.title, n.body, notificationUrl(n));
    }
  }, []);

  const loadNotifications = useCallback(async () => {
    const seq = ++notifSeqRef.current;
    try {
      const [list, count] = await Promise.all([
        api("/notifications?limit=50"),
        api("/notifications/unread_count"),
      ]);
      if (seq !== notifSeqRef.current) return; // superseded
      setNotifications(list);
      setUnreadCount(count.unread);
      announceNewNotifications(list);
    } catch (err) {
      toastError(err);
    }
  }, [announceNewNotifications]);

  // Live ref so the session poller calls the latest loadNotifications.
  const loadNotificationsRef = useRef(loadNotifications);
  useEffect(() => {
    loadNotificationsRef.current = loadNotifications;
  }, [loadNotifications]);

  // Boot (auth gate lives in main.tsx).
  const bootedRef = useRef(false);
  useEffect(() => {
    if (bootedRef.current) return;
    bootedRef.current = true;
    (async () => {
      try {
        const [h, m] = await Promise.all([
          api("/health"),
          api("/meta"),
          loadUsers(),
          loadProjects(),
        ]);
        setHealth(h);
        if (m && Array.isArray(m.statuses)) setMeta({ ...FALLBACK_META, ...m });
      } catch (err) {
        toastError(err);
      }
      void loadNotifications();
      await refreshAll();
      setBooted(true);
    })();
  }, [loadUsers, loadProjects, refreshAll, loadNotifications]);

  // Re-fetch the list on page / filter / tab change.
  const firstListEffect = useRef(true);
  useEffect(() => {
    if (firstListEffect.current) {
      // boot() already triggers the initial refreshAll
      firstListEffect.current = false;
      return;
    }
    void refreshBugs();
  }, [page, filters, activeTab, refreshBugs]);

  // Tab change additionally refreshes stats.
  const firstTabEffect = useRef(true);
  useEffect(() => {
    if (firstTabEffect.current) {
      firstTabEffect.current = false;
      return;
    }
    void refreshStats();
  }, [activeTab, refreshStats]);

  // The organization's accent colour tints filled surfaces; cleared when none is set.
  const accent = currentUser.branding?.accent_color;
  useEffect(() => {
    applyOrgAccent(accent);
  }, [accent]);

  const refreshMe = useCallback(async () => {
    const m = await api("/auth/me", { cache: "no-store" });
    setCurrentUser((prev) => (sameMe(prev, m) ? prev : m));
    return m;
  }, []);

  useEffect(() => {
    // Session poll every 15s + on tab focus; unread count rides the same tick.
    const tick = async () => {
      try {
        const m = await api("/auth/me", { cache: "no-store" });
        setCurrentUser((prev) => (sameMe(prev, m) ? prev : m));
      } catch {
        /* 401 already bounced; network errors don't kick the user out */
      }
      try {
        const c = await api("/notifications/unread_count");
        setUnreadCount((prev) => {
          // A rise means something new arrived; pull the list so it can be
          // surfaced as a desktop notification even if FCM never delivered.
          if (c.unread > prev) void loadNotificationsRef.current();
          return c.unread;
        });
      } catch {
        /* transient — keep the last known count */
      }
    };
    const id = setInterval(tick, SESSION_POLL_MS);
    const onVis = () => {
      if (!document.hidden) void tick();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  // Live ref so the interval calls the latest refreshAll without re-registering.
  const refreshAllRef = useRef(refreshAll);
  useEffect(() => {
    refreshAllRef.current = refreshAll;
  }, [refreshAll]);

  useEffect(() => {
    // Poll list + stats; paused while hidden, fires on refocus.
    const refresh = () => {
      if (!document.hidden) void refreshAllRef.current();
    };
    const id = setInterval(refresh, DATA_POLL_MS);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);

  // Directory polls separately so bug saves don't double-fetch users/projects.
  const loadDirRef = useRef({ loadUsers, loadProjects });
  useEffect(() => {
    loadDirRef.current = { loadUsers, loadProjects };
  }, [loadUsers, loadProjects]);
  useEffect(() => {
    const refresh = () => {
      if (document.hidden) return;
      void loadDirRef.current.loadUsers();
      void loadDirRef.current.loadProjects();
    };
    const id = setInterval(refresh, DATA_POLL_MS);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);

  useEffect(() => {
    // Notify once when the deployed asset version changes (new release).
    let warned = false;
    const boot = health?.asset_version;
    if (!boot) return;
    const id = setInterval(async () => {
      try {
        const h = await api("/health");
        if (!warned && h.asset_version && h.asset_version !== boot) {
          warned = true;
          toast(`A new version of ${getAppName()} is available — refresh to update`, "info");
        }
      } catch {
        /* ignore */
      }
    }, VERSION_POLL_MS);
    return () => clearInterval(id);
  }, [health?.asset_version]);

  const setActiveTab = useCallback((t) => {
    setRawActiveTab(t);
    setPage(1);
    try {
      localStorage.setItem("activeTab", t);
    } catch {
      /* private mode */
    }
  }, []);

  const setDefaultNewType = useCallback((t) => {
    setRawDefaultNewType(t);
    try {
      localStorage.setItem("defaultNewType", t);
    } catch {
      /* private mode */
    }
  }, []);

  const setFilters = useCallback((f) => {
      setRawFilters((prev) => {
        const next = typeof f === "function" ? f(prev) : f;
        return next;
      });
      setPage(1);
    },
    [],
  );

  const clearFilters = useCallback(() => {
    setRawFilters(EMPTY_FILTERS);
    setPage(1);
  }, []);

  const toggleSidebarCollapsed = useCallback(() => {
    setSidebarCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("sidebarCollapsed", next ? "1" : "0");
      } catch {
        /* private mode */
      }
      return next;
    });
  }, []);

  // Reflect collapse state on <body> (styles.css keys off body class).
  useEffect(() => {
    document.body.classList.toggle("sidebar-collapsed", sidebarCollapsed);
  }, [sidebarCollapsed]);

  const setView = useCallback((v) => {
    setRawView(v);
  }, []);

  const openBugForm = useCallback(
    (opts) => {
      setBugModal({
        open: true,
        bug: null,
        defaultType: opts?.defaultType,
        defaultEventId: opts?.defaultEventId ?? null,
        defaultProjectId: opts?.defaultProjectId ?? null,
        defaultSprintId: opts?.defaultSprintId ?? null,
        defaultStatus: opts?.defaultStatus ?? null,
        defaultEpicId: opts?.defaultEpicId ?? null,
        defaultParentId: opts?.defaultParentId ?? null,
      });
    },
    [],
  );

  const openBugDetail = useCallback(async (bugId) => {
    try {
      const bug = await api(`/bugs/${bugId}`);
      setBugModal({ open: true, bug });
    } catch (err) {
      toastError(err);
    }
  }, []);

  const reloadBugModal = useCallback(async () => {
    const id = bugModal.bug?.id;
    if (!id) return;
    try {
      const bug = await api(`/bugs/${id}`);
      setBugModal((prev) => ({ ...prev, bug }));
    } catch (err) {
      toastError(err);
    }
  }, [bugModal.bug?.id]);

  const closeBugModal = useCallback(() => {
    setBugModal({ open: false, bug: null });
  }, []);

  // Poll the open bug modal so concurrent edits appear; keyed on bug id.
  const reloadBugModalRef = useRef(reloadBugModal);
  useEffect(() => {
    reloadBugModalRef.current = reloadBugModal;
  }, [reloadBugModal]);
  const bugModalOpenId = bugModal.open && bugModal.bug ? bugModal.bug.id : null;
  useEffect(() => {
    if (bugModalOpenId == null) return;
    const tick = () => {
      if (!document.hidden) void reloadBugModalRef.current();
    };
    const id = setInterval(tick, DATA_POLL_MS);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [bugModalOpenId]);

  const openProjectForm = useCallback((project) => {
    setProjectModal({ open: true, project: project ?? null });
  }, []);
  const closeProjectModal = useCallback(() => {
    setProjectModal({ open: false, project: null });
  }, []);

  const openUserForm = useCallback((user) => {
    setUserModal({ open: true, user: user ?? null });
  }, []);
  const closeUserModal = useCallback(() => {
    setUserModal({ open: false, user: null });
  }, []);

  const markNotificationRead = useCallback(async (id) => {
    // Optimistic: stamp read locally, then persist.
    notifSeqRef.current++; // in-flight poll can't un-read this
    setNotifications((prev) =>
      prev.map((n) =>
        n.id === id && !n.read_at ? { ...n, read_at: new Date().toISOString() } : n,
      ),
    );
    setUnreadCount((c) => Math.max(0, c - 1));
    try {
      await api(`/notifications/${id}/read`, { method: "POST" });
    } catch (err) {
      toastError(err);
      void loadNotifications();
    }
  }, [loadNotifications]);

  const markAllNotificationsRead = useCallback(async () => {
    const now = new Date().toISOString();
    notifSeqRef.current++;
    setNotifications((prev) => prev.map((n) => (n.read_at ? n : { ...n, read_at: now })));
    setUnreadCount(0);
    try {
      await api("/notifications/read-all", { method: "POST" });
    } catch (err) {
      toastError(err);
      void loadNotifications();
    }
  }, [loadNotifications]);

  const deleteNotification = useCallback(async (id) => {
    notifSeqRef.current++;
    // Read unread status outside the updater so the count adjusts exactly once.
    const gone = notificationsRef.current.find((n) => n.id === id);
    const wasUnread = !!gone && !gone.read_at;
    setNotifications((prev) => prev.filter((n) => n.id !== id));
    if (wasUnread) setUnreadCount((c) => Math.max(0, c - 1));
    try {
      await api(`/notifications/${id}`, { method: "DELETE" });
    } catch (err) {
      toastError(err);
      void loadNotifications();
    }
  }, [loadNotifications]);

  const openNotification = useCallback(async (n) => {
      if (!n.read_at) void markNotificationRead(n.id);
      if (n.bug_id != null) {
        await openBugDetail(n.bug_id);
      } else if (n.event_id != null) {
        setEventDeepLinkId(n.event_id);
        setView("events");
      }
    },
    [markNotificationRead, openBugDetail, setView],
  );

  // Notification links (/#bug=N, /#event=N from email, push and desktop
  // notifications): handled once boot has loaded the session, then on every
  // hashchange. The hash is cleared afterwards so a refresh doesn't reopen it
  // and the same link can be followed again.
  useEffect(() => {
    if (!booted) return undefined;
    const follow = () => {
      const link = parseDeepLink(location.hash);
      if (!link) return;
      history.replaceState(null, "", location.pathname + location.search);
      if (link.kind === "bug") {
        void openBugDetail(link.id);
      } else {
        setEventDeepLinkId(link.id);
        setView("events");
      }
    };
    follow();
    window.addEventListener("hashchange", follow);
    return () => window.removeEventListener("hashchange", follow);
  }, [booted, openBugDetail, setView]);

  // Sleuth chat "open bug" deep-link.
  useEffect(() => {
    const onOpen = (e) => {
      const id = e.detail?.bugId;
      if (typeof id === "number") void openBugDetail(id);
    };
    document.addEventListener("sleuth:open-bug", onOpen);
    return () => document.removeEventListener("sleuth:open-bug", onOpen);
  }, [openBugDetail]);

  const roleRank = useCallback((role) => {
    if (role === "admin") return 3;
    if (role === "manager") return 2;
    return 1;
  }, []);

  const value = useMemo(
    () => ({
      currentUser,
      meta,
      users,
      projects,
      stats,
      health,
      view,
      setView,
      sprintsDeepLinkTab,
      eventDeepLinkId,
      setEventDeepLinkId,
      setSprintsDeepLinkTab,
      activeTab,
      setActiveTab,
      defaultNewType,
      setDefaultNewType,
      bugs,
      page,
      setPage,
      pageSize: PAGE_SIZE,
      total,
      totalPages,
      filters,
      setFilters,
      clearFilters,
      sidebarCollapsed,
      toggleSidebarCollapsed,
      refreshBugs,
      refreshStats,
      refreshAll,
      loadUsers,
      loadProjects,
      upsertProject,
      bugModal,
      openBugForm,
      openBugDetail,
      reloadBugModal,
      closeBugModal,
      projectModal,
      openProjectForm,
      closeProjectModal,
      userModal,
      openUserForm,
      closeUserModal,
      changePasswordOpen,
      setChangePasswordOpen,
      accountOpen,
      setAccountOpen,
      refreshMe,
      bulkImportOpen,
      setBulkImportOpen,
      notifications,
      unreadCount,
      loadNotifications,
      markNotificationRead,
      markAllNotificationsRead,
      deleteNotification,
      openNotification,
      roleRank,
      canManage: roleRank(currentUser.role) >= 2,
      isAdmin: currentUser.role === "admin",
    }),
    [
      currentUser, meta, users, projects, stats, health, view, setView,
      sprintsDeepLinkTab, setSprintsDeepLinkTab, eventDeepLinkId,
      activeTab, setActiveTab, defaultNewType, setDefaultNewType, bugs, page,
      total, totalPages, filters, setFilters, clearFilters, sidebarCollapsed,
      toggleSidebarCollapsed, refreshBugs, refreshStats, refreshAll, loadUsers,
      loadProjects, upsertProject, bugModal, openBugForm, openBugDetail, reloadBugModal,
      closeBugModal, projectModal, openProjectForm, closeProjectModal,
      userModal, openUserForm, closeUserModal, changePasswordOpen,
      accountOpen, refreshMe, bulkImportOpen, roleRank,
      notifications, unreadCount, loadNotifications, markNotificationRead,
      markAllNotificationsRead, deleteNotification, openNotification,
    ],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
