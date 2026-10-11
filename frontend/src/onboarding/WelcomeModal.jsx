/** First-login welcome, worded for the person's track (admin, manager, project lead, user). Closing it
 * with the X, Escape or "Maybe later" only hides it for now; ticking "Don't show this again" or
 * starting the tour, setup or help is remembered on the server. */
import { useState } from "react";
import Modal from "../components/Modal";
import { getAppName } from "../lib/branding";
import { WELCOME } from "./roles";

export default function WelcomeModal({ open, name, role = "user", onTour, onHelp, onLater, onSetup }) {
  const [dontShow, setDontShow] = useState(false);
  const appName = getAppName();
  const first = (name || "").trim().split(/\s+/)[0];
  const copy = WELCOME[role] ?? WELCOME.user;
  const setup = role === "admin" && onSetup;
  return (
    <Modal
      id="welcomeModal"
      open={open}
      title={`Welcome to ${appName}${first ? `, ${first}` : ""}`}
      subtitle={copy.subtitle}
      cardClass="welcome-card"
      onClose={() => onLater(dontShow)}
    >
      <div className="modal-body welcome-body" data-welcome-role={role}>
        <ul className="welcome-points">
          {copy.points.map(([lead, rest]) => (
            <li key={lead}><strong>{lead}</strong> {rest}</li>
          ))}
        </ul>
        <label className="welcome-check">
          <input type="checkbox" checked={dontShow} onChange={(e) => setDontShow(e.target.checked)} />
          <span>Don&apos;t show this again</span>
        </label>
      </div>
      <div className="modal-foot welcome-foot">
        <button type="button" className="btn ghost" onClick={() => onLater(dontShow)}>Maybe later</button>
        <span className="grow" />
        <button type="button" className="btn" onClick={() => onHelp(dontShow)}>Browse the guides</button>
        <button type="button" className={`btn${setup ? "" : " primary"}`} data-autofocus={setup ? undefined : ""}
          onClick={() => onTour(dontShow)}>Take the tour</button>
        {setup && (
          <button type="button" className="btn primary" data-autofocus onClick={() => onSetup(dontShow)}>
            Set up my organization
          </button>
        )}
      </div>
    </Modal>
  );
}
