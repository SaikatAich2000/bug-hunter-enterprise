/** First-login welcome. Closing it with the X, Escape or "Maybe later" only hides it for now; ticking
 * "Don't show this again" or starting the tour or help is remembered on the server. */
import { useState } from "react";
import Modal from "../components/Modal";
import { getAppName } from "../lib/branding";

export default function WelcomeModal({ open, name, onTour, onHelp, onLater }) {
  const [dontShow, setDontShow] = useState(false);
  const appName = getAppName();
  const first = (name || "").trim().split(/\s+/)[0];
  return (
    <Modal
      id="welcomeModal"
      open={open}
      title={`Welcome to ${appName}${first ? `, ${first}` : ""}`}
      subtitle="Here is the quickest way to get going."
      cardClass="welcome-card"
      onClose={() => onLater(dontShow)}
    >
      <div className="modal-body welcome-body">
        <ul className="welcome-points">
          <li><strong>Track work.</strong> Bugs, requirements, tasks and stories in one list, with filters and bulk updates.</li>
          <li><strong>Plan sprints.</strong> A ranked backlog, a sprint board and reports once a manager turns Scrum on.</li>
          <li><strong>Ask Sleuth.</strong> The assistant answers questions and makes changes after you confirm.</li>
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
        <button type="button" className="btn primary" data-autofocus onClick={() => onTour(dontShow)}>Take the tour</button>
      </div>
    </Modal>
  );
}
