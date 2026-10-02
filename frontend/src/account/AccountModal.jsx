// "Account settings": profile and email, security (password, two-factor), notifications, privacy.
import { useEffect, useState } from "react";
import Modal from "../components/Modal";
import { useApp } from "../state/AppContext";
import NotificationsTab from "./NotificationsTab";
import PrivacyTab from "./PrivacyTab";
import ProfileTab from "./ProfileTab";
import SecurityTab from "./SecurityTab";

const TABS = [
  { id: "profile", label: "Profile" },
  { id: "security", label: "Security" },
  { id: "notifications", label: "Notifications" },
  { id: "privacy", label: "Privacy" },
];

export default function AccountModal() {
  const { accountOpen, setAccountOpen } = useApp();
  const [tab, setTab] = useState("profile");

  // Always reopen on the first tab.
  useEffect(() => {
    if (accountOpen) setTab("profile");
  }, [accountOpen]);

  return (
    <Modal
      id="modalAccount"
      open={accountOpen}
      title="Account settings"
      size="lg"
      onClose={() => setAccountOpen(false)}
    >
      <div className="modal-body">
        <div className="ent-tabs" role="tablist" aria-label="Account sections">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              id={`accountTab-${t.id}`}
              aria-selected={tab === t.id}
              aria-controls={`accountPanel-${t.id}`}
              className={`ent-tab${tab === t.id ? " active" : ""}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>
        {accountOpen && (
          <div role="tabpanel" id={`accountPanel-${tab}`} aria-labelledby={`accountTab-${tab}`}>
            {tab === "profile" && <ProfileTab />}
            {tab === "security" && <SecurityTab />}
            {tab === "notifications" && <NotificationsTab />}
            {tab === "privacy" && <PrivacyTab />}
          </div>
        )}
      </div>
    </Modal>
  );
}
