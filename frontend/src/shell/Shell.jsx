// App shell — TopChrome + SuperNav + Sidebar/main frame. Only the active view is mounted;
// list/analytics re-fetch here because they read shared context, other views fetch on mount.
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { VIEW_MIN_ROLE } from "../types";
import { initPushOnBoot } from "../lib/push";
import TopChrome from "./TopChrome";
import SuperNav from "./SuperNav";
import PageHead from "./PageHead";
import Sidebar from "./Sidebar";
import KpiStrip from "./KpiStrip";
import FilterBar from "./FilterBar";
// ListView is eager for first paint; other views are lazy-split.
import ListView from "../views/ListView";
const EventsView = lazy(() => import("../views/EventsView"));
const SprintsView = lazy(() => import("../views/SprintsView"));
const AnalyticsView = lazy(() => import("../views/AnalyticsView"));
const AuditView = lazy(() => import("../views/AuditView"));
const SessionsView = lazy(() => import("../views/SessionsView"));
const ReportsView = lazy(() => import("../views/ReportsView"));
const OrganizationView = lazy(() => import("../views/OrganizationView"));
import BugModal from "../modals/BugModal";
import ProjectModal from "../modals/ProjectModal";
import UserModal from "../modals/UserModal";
import ChangePasswordModal from "../modals/ChangePasswordModal";
import AccountModal from "../account/AccountModal";
import BulkImportModal from "../modals/BulkImportModal";

export default function Shell() {
  const { view, setView, refreshAll, refreshStats, roleRank, currentUser } = useApp();

  const [mobileOpen, setMobileOpen] = useState(false);

  // Refresh FCM token on boot; no-op when push is unsupported/ungranted.
  useEffect(() => {
    void initPushOnBoot();
  }, []);

  // Re-fetch shared data on view change; boot() covers the initial load.
  const firstView = useRef(true);
  useEffect(() => {
    if (firstView.current) {
      firstView.current = false;
      return;
    }
    if (view === "list") void refreshAll();
    else if (view === "analytics") void refreshStats();
  }, [view, refreshAll, refreshStats]);

  // UX-only role gate (backend is the authority): bounce deep links to gated views.
  const need = VIEW_MIN_ROLE[view];
  const denied = need != null && roleRank(currentUser.role) < roleRank(need);
  useEffect(() => {
    if (denied) setView("list");
  }, [denied, setView]);

  return (
    <>
      <button
        type="button"
        id="sidebarBackdrop"
        className="sidebar-backdrop"
        hidden={!mobileOpen}
        aria-label="Close navigation"
        onClick={() => setMobileOpen(false)}
      ></button>

      <TopChrome onOpenMobile={() => setMobileOpen(true)} />
      <SuperNav />

      <div className="frame">
        <Sidebar mobileOpen={mobileOpen} onNavigate={() => setMobileOpen(false)} />

        <main className="main">
          <PageHead />

          <div className="content">
            <KpiStrip />
            <FilterBar />

            {/* Only the active view is mounted; unmounting is the hide mechanism. */}
            <Suspense fallback={null}>
              {view === "list" && <ListView />}
              {view === "sprints" && <SprintsView />}
              {view === "analytics" && <AnalyticsView />}
              {view === "audit" && !denied && <AuditView />}
              {view === "events" && <EventsView />}
              {view === "reports" && !denied && <ReportsView />}
              {view === "sessions" && !denied && <SessionsView />}
              {view === "organization" && !denied && <OrganizationView />}
            </Suspense>
          </div>
        </main>
      </div>

      {/* Shared modals — context-driven; each renders nothing when closed. */}
      <BugModal />
      <ProjectModal />
      <UserModal />
      <ChangePasswordModal />
      <AccountModal />
      <BulkImportModal />
    </>
  );
}
