// Organization settings. Admins get every tab; managers can only invite people.
import { useState } from "react";
import BrandingTab from "../org/BrandingTab";
import GeneralTab from "../org/GeneralTab";
import InvitationsTab from "../org/InvitationsTab";
import WebhooksTab from "../org/WebhooksTab";
import { useApp } from "../state/AppContext";

const TABS = [
  { id: "general", label: "General", adminOnly: true, Panel: GeneralTab },
  { id: "branding", label: "Branding", adminOnly: true, Panel: BrandingTab },
  { id: "invitations", label: "Invitations", adminOnly: false, Panel: InvitationsTab },
  { id: "webhooks", label: "Webhooks", adminOnly: true, Panel: WebhooksTab },
];

export default function OrganizationView() {
  const { isAdmin } = useApp();
  const tabs = TABS.filter((t) => isAdmin || !t.adminOnly);
  const [tab, setTab] = useState(tabs[0].id);
  const active = tabs.find((t) => t.id === tab) ?? tabs[0];

  return (
    <section className="view" id="viewOrganization">
      <div className="ent-tabs" role="tablist" aria-label="Organization sections">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            id={`orgTab-${t.id}`}
            aria-selected={active.id === t.id}
            aria-controls={`orgPanel-${t.id}`}
            className={`ent-tab${active.id === t.id ? " active" : ""}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`orgPanel-${active.id}`} aria-labelledby={`orgTab-${active.id}`} className="ent-panel">
        <active.Panel />
      </div>
    </section>
  );
}
