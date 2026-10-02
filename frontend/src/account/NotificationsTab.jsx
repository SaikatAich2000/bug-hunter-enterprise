// Which kinds of push notification this user wants (mentions, assignments, other activity).
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { toastError } from "../lib/toast";

const CHANNELS = [
  { key: "assignments", label: "Assignments", help: "When an item is assigned to you" },
  { key: "mentions", label: "Mentions", help: "When a comment mentions someone with @" },
  { key: "activity", label: "Other activity", help: "Updates, comments and changes on your items" },
];

export default function NotificationsTab() {
  const [prefs, setPrefs] = useState(null);

  useEffect(() => {
    api("/notifications/preferences").then(setPrefs).catch(toastError);
  }, []);

  async function toggle(key) {
    const wanted = !prefs[key];
    const flip = (p) => ({ ...p, [key]: !p[key] });
    setPrefs(flip);
    try {
      setPrefs(await api("/notifications/preferences", { method: "PUT", json: { [key]: wanted } }));
    } catch (err) {
      setPrefs(flip);
      toastError(err);
    }
  }

  return (
    <div className="ent-section">
      <h3>Push notifications</h3>
      <p className="hint">
        Applies to browsers and the mobile app. In-app notifications and email are not affected.
      </p>
      {prefs === null && <p className="hint">Loading…</p>}
      {prefs && (
        <ul className="ent-toggle-list">
          {CHANNELS.map((c) => (
            <li key={c.key}>
              <label className="ent-toggle">
                <input type="checkbox" checked={prefs[c.key]} aria-describedby={`prefHelp-${c.key}`}
                  onChange={() => void toggle(c.key)} />
                <strong>{c.label}</strong>
              </label>
              <small id={`prefHelp-${c.key}`} className="hint ent-toggle-help">{c.help}</small>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
