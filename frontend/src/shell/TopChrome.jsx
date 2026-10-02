// Top bar: hamburger, brand, role-gated nav (VIEW_MIN_ROLE, shared with Shell/Sidebar),
// notifications, profile menu.
import { useApp } from "../state/AppContext";
import { getAppName, getStaticAssetUrl, getWordmarkParts } from "../lib/branding";
import NotificationsBell from "./NotificationsBell";
import ProfileMenu from "./ProfileMenu";
import { VIEW_MIN_ROLE } from "../types";
import { NAV_ITEMS } from "./navItems";

export default function TopChrome({ onOpenMobile }) {
  const appName = getAppName();
  const wordmark = getWordmarkParts();
  const { currentUser, view, setView, roleRank, health } = useApp();

  const allowed = (v) => {
    const need = VIEW_MIN_ROLE[v];
    return !need || roleRank(currentUser.role) >= roleRank(need);
  };

  return (
    <header className="chrome">
      <button className="icon-btn menu-btn" id="menuBtn" aria-label="Open menu" onClick={onOpenMobile}>
        ☰
      </button>

      <div className="brandmark" id="brandMark">
        <img className="logo" src={getStaticAssetUrl("icon.png")} alt={appName} />
        <div className="wm">
          <b aria-label={appName}>{wordmark.first}{wordmark.rest && <span>{wordmark.rest}</span>}</b>
          {health && <small id="brandVersion">{`Version ${health.version}`}</small>}
        </div>
      </div>

      {currentUser.organization_name && (
        <div className="org-badge" id="orgBadge" title="Your organization">
          {currentUser.branding?.logo_data_url && (
            <img className="org-badge-logo" src={currentUser.branding.logo_data_url} alt="" />
          )}
          <span>{currentUser.organization_name}</span>
        </div>
      )}

      <nav className="nav" aria-label="Main sections">
        {NAV_ITEMS.filter((item) => allowed(item.view)).map((item) => (
          <button
            key={item.view}
            className={`nav-btn${view === item.view ? " active" : ""}`}
            data-view={item.view}
            onClick={() => setView(item.view)}
          >
            <span className="nav-icon">{item.icon}</span>
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="spacer"></div>

      <div className="chrome-right">
        {/* Forwards to the Sleuth FAB so panel open/close logic stays in one place. */}
        <button
          type="button"
          className="chrome-sleuth-btn"
          aria-label="Ask Sleuth"
          title="Ask Sleuth — the AI assistant"
          onClick={() => document.getElementById("sleuthFab")?.click()}
        >
          <img className="chrome-sleuth-logo" src="/static/sleuth.svg" alt="" draggable={false} />
          <span>Ask Sleuth</span>
        </button>
        <NotificationsBell />
        <ProfileMenu />
      </div>
    </header>
  );
}
