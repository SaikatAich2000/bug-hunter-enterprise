// Top-right account dropdown (account settings, change-password, theme, log-out).
// Test contract: id="accountName" is the smoke test's SPA-ready signal; other ids are Playwright selectors.
import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { api } from "../lib/api";
import { initials } from "../lib/format";
import { confirmDialog } from "../components/ConfirmHost";
import { unsubscribeOnLogout } from "../lib/push";

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

export default function ProfileMenu() {
  const { currentUser, setChangePasswordOpen, setAccountOpen } = useApp();

  const [open, setOpen] = useState(false);
  const [theme, setTheme] = useState(currentTheme);
  const rootRef = useRef(null);

  // Close on outside click or Escape
  useEffect(() => {
    if (!open) return;
    const onDown = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false);
    };
    const onKey = (e) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const handleLogout = async () => {
    setOpen(false);
    const ok = await confirmDialog("Log out now?", {
      title: "Log out",
      okLabel: "Log out",
      danger: false,
    });
    if (!ok) return;
    // Deregister push token while the session is still valid (shared-browser safety).
    try {
      await unsubscribeOnLogout();
    } catch {
      /* push not configured or unavailable — proceed */
    }
    try {
      await api("/auth/logout", { method: "POST" });
    } catch {
      /* log out locally even if the server call fails */
    }
    location.replace("/login.html");
  };

  const handleTheme = () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    setTheme(next);
    try {
      localStorage.setItem("theme", next);
    } catch {
      /* private browsing — skip persistence */
    }
  };

  const handleChangePassword = () => {
    setOpen(false);
    setChangePasswordOpen(true);
  };

  const handleAccount = () => {
    setOpen(false);
    setAccountOpen(true);
  };

  const avatar = initials(currentUser.name) || "?";

  return (
    <div className="profile-wrap" ref={rootRef}>
      <button
        type="button"
        className="profile-btn"
        id="profileBtn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Account menu"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="profile-avatar" id="accountAvatar" aria-hidden="true">
          {avatar}
        </span>
        <span className="profile-name" id="accountName">
          {currentUser.name}
        </span>
        <span className="profile-caret" aria-hidden="true">
          ▾
        </span>
      </button>

      {open && (
        <div className="profile-menu" role="menu" aria-label="Account">
          <div className="profile-menu-head">
            <span className="profile-avatar lg" aria-hidden="true">
              {avatar}
            </span>
            <div className="profile-menu-id">
              <div className="profile-menu-name">{currentUser.name}</div>
              <div className="profile-menu-meta">
                <span id="accountRole">{currentUser.role}</span>
                {" · "}
                <span id="accountEmail">{currentUser.email}</span>
              </div>
              <div className="profile-menu-meta" id="accountOrg">
                {currentUser.organization_name}
              </div>
            </div>
          </div>

          <div className="profile-menu-sep" />

          <button
            type="button"
            role="menuitem"
            className="profile-menu-item"
            id="accountSettingsBtn"
            onClick={handleAccount}
          >
            <span className="profile-menu-ic" aria-hidden="true">👤</span>{" "}
            Account settings
          </button>

          <button
            type="button"
            role="menuitem"
            className="profile-menu-item"
            id="changePasswordBtn"
            onClick={handleChangePassword}
          >
            <span className="profile-menu-ic" aria-hidden="true">🔑</span>{" "}
            Change password
          </button>

          <button
            type="button"
            role="menuitem"
            className="profile-menu-item"
            id="themeBtn"
            onClick={handleTheme}
          >
            <span className="profile-menu-ic" aria-hidden="true">{theme === "dark" ? "☀️" : "🌙"}</span>
            {theme === "dark" ? "Light theme" : "Dark theme"}
          </button>

          <div className="profile-menu-sep" />

          <button
            type="button"
            role="menuitem"
            className="profile-menu-item danger"
            id="logoutBtn"
            onClick={() => void handleLogout()}
          >
            <span className="profile-menu-ic" aria-hidden="true">🚪</span>{" "}
            Log out
          </button>
        </div>
      )}
    </div>
  );
}
