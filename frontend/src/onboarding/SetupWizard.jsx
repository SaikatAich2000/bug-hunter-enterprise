/** First-admin organization setup: name and branding, the first project, teammates, optional extras.
 *
 * Progress is stored by the host on the server (setup_status / setup_step), so a wizard closed halfway
 * resumes at the same step from any browser. Every step is safe to repeat: an existing project is
 * reused instead of created twice, and each invitation reports its own result. Closing the dialog
 * (X, Escape) only hides it; "Skip setup" and "Finish" are remembered. */
import { useEffect, useRef, useState } from "react";
import Modal from "../components/Modal";
import { LOGO_TYPES, MAX_LOGO_BYTES, readAsDataUrl } from "../org/BrandingTab";
import { api } from "../lib/api";
import { isValidEmail } from "../lib/constants";
import { parseHex } from "../lib/orgBranding";
import { useApp } from "../state/AppContext";

const errMessage = (err) => (err instanceof Error ? err.message : String(err || "Something went wrong"));

export const SETUP_STEPS = [
  { id: "organization", label: "Organization" },
  { id: "project", label: "First project" },
  { id: "team", label: "Teammates" },
  { id: "extras", label: "Extras" },
  { id: "done", label: "Done" },
];

const SENDER_RE = /^[^<>@\r\n"]{1,100}$/;

/** "Acme Bugs" + "bugs@acme.com" -> "Acme Bugs <bugs@acme.com>"; the address alone is also valid. */
export function senderFrom(name, address) {
  const n = name.trim();
  const a = address.trim();
  if (!n && !a) return { value: "" };
  if (!a) return { error: "Add the sender's email address, or clear the sender name." };
  if (!isValidEmail(a)) return { error: "Enter a valid sender email address." };
  if (!n) return { value: a };
  if (!SENDER_RE.test(n)) return { error: "The sender name cannot contain < > @ or quotes." };
  return { value: `${n} <${a}>` };
}

/** Email addresses from free text (commas, spaces, semicolons or new lines), without repeats. */
export function parseEmails(text) {
  return [...new Set(text.split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean))];
}

export default function SetupWizard({ open, step, onStep, onClose, onSkip, onFinish, onOpenAccount, onOpenWebhooks, onOpenGuide, onTour }) {
  const { currentUser, projects, loadProjects, refreshMe } = useApp();
  const last = SETUP_STEPS.length - 1;
  const index = Math.min(Math.max(step, 0), last);
  const [dir, setDir] = useState("fwd");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const headingRef = useRef(null);
  const firstRender = useRef(true);

  // Step 1
  const [orgName, setOrgName] = useState(currentUser?.organization_name ?? "");
  const [logo, setLogo] = useState("");
  const [accent, setAccent] = useState("");
  const [senderName, setSenderName] = useState("");
  const [senderAddress, setSenderAddress] = useState("");
  // Step 2
  const [projectName, setProjectName] = useState("");
  const [scrum, setScrum] = useState(false);
  // Step 3
  const [emails, setEmails] = useState("");
  const [role, setRole] = useState("user");
  const [projectIds, setProjectIds] = useState([]);
  const [results, setResults] = useState([]);

  // Prefill from what is already saved, so a resumed wizard shows the earlier answers.
  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    api("/branding")
      .then((b) => {
        if (cancelled || !b) return;
        setLogo(b.logo_data_url || "");
        setAccent(b.accent_color || "");
        const m = /^(?:(.*?)\s*<)?([^<>]+?)>?$/.exec(b.email_from_override || "");
        if (m) {
          setSenderName((m[1] || "").trim());
          setSenderAddress((m[2] || "").trim());
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [open]);

  useEffect(() => {
    // Seed once per opening; re-seeding on every change would refill a field the person just cleared.
    if (open) setOrgName(currentUser?.organization_name ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps -- seeded when the dialog opens only
  }, [open]);

  // Projects invited people join by default: the one just created.
  useEffect(() => {
    setProjectIds((prev) => (prev.length === 0 && projects.length > 0 ? [projects[0].id] : prev));
  }, [projects]);

  // Move focus to the step's heading after a step change so keyboard and screen-reader users follow.
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    headingRef.current?.focus({ preventScroll: true });
  }, [index]);

  const go = (to) => {
    setError("");
    setDir(to >= index ? "fwd" : "back");
    onStep(to);
  };

  async function run(task) {
    setBusy(true);
    setError("");
    try {
      return await task();
    } catch (err) {
      setError(errMessage(err));
      return undefined;
    } finally {
      setBusy(false);
    }
  }

  async function saveOrganization() {
    const name = orgName.trim();
    if (name.length < 2) return setError("Give the organization a name of at least 2 characters.");
    if (accent && !parseHex(accent)) return setError("Enter the accent colour as a hex value such as #6366f1.");
    const sender = senderFrom(senderName, senderAddress);
    if (sender.error) return setError(sender.error);
    const ok = await run(async () => {
      if (name !== currentUser.organization_name) await api("/organization", { method: "PUT", json: { name } });
      await api("/branding", {
        method: "PUT",
        json: { logo_data_url: logo, accent_color: accent, email_from_override: sender.value },
      });
      await refreshMe();
      return true;
    });
    if (ok) go(1);
    return undefined;
  }

  async function pickLogo(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (!LOGO_TYPES.includes(file.type)) return setError("Use a PNG, JPEG, SVG, GIF or WebP image.");
    if (file.size > MAX_LOGO_BYTES) return setError("The logo must be 100 KB or smaller.");
    setError("");
    try {
      setLogo(await readAsDataUrl(file));
    } catch (err) {
      setError(errMessage(err));
    }
    return undefined;
  }

  async function createProject() {
    const name = projectName.trim();
    // Every new organization starts with a default project, so adding another is optional.
    if (name === "" && projects.length > 0) return go(2);
    if (name.length < 2) return setError("Give the project a name of at least 2 characters.");
    const ok = await run(async () => {
      await api("/projects", { method: "POST", json: { name, agile_enabled: scrum } });
      await loadProjects({ quiet: true });
      return true;
    });
    if (ok) go(2);
    return undefined;
  }

  async function sendInvitations() {
    const list = parseEmails(emails);
    if (list.length === 0) return go(3);
    const bad = list.filter((a) => !isValidEmail(a));
    if (bad.length) return setError(`Not a valid email address: ${bad.join(", ")}`);
    const out = await run(async () => {
      const done = [];
      for (const email of list) {
        try {
          await api("/invitations", { method: "POST", json: { email, role, project_ids: projectIds, as_lead: false } });
          done.push({ email, ok: true });
        } catch (err) {
          done.push({ email, ok: false, reason: errMessage(err) });
        }
      }
      return done;
    });
    if (!out) return undefined;
    setResults(out);
    const failed = out.filter((r) => !r.ok);
    setEmails(failed.map((r) => r.email).join("\n"));
    if (failed.length === 0) go(3);
    return undefined;
  }

  const toggleProject = (id) =>
    setProjectIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));

  const swatch = parseHex(accent) ? accent : "#6366f1";
  const title = `Set up ${currentUser?.organization_name || "your organization"}`;

  return (
    <Modal id="setupWizard" open={open} title={title} subtitle="A few minutes now saves a lot of explaining later." size="lg"
      cardClass="wiz-card" onClose={onClose}>
      <div className="modal-body wiz-body">
        <ol className="wiz-steps" aria-label="Setup progress">
          {SETUP_STEPS.map((s, i) => (
            <li key={s.id} className={`wiz-dot${i === index ? " current" : ""}${i < index ? " past" : ""}`}
              aria-current={i === index ? "step" : undefined}>
              <span className="wiz-num" aria-hidden="true">{i < index ? "✓" : i + 1}</span>
              <span className="wiz-label">{s.label}</span>
              <span className="sr-only">{i < index ? " (done)" : ""}</span>
            </li>
          ))}
        </ol>

        <div className="wiz-panel" key={index} data-dir={dir}>
          <h3 className="wiz-heading" tabIndex={-1} ref={headingRef}>
            Step {index + 1} of {SETUP_STEPS.length}: {SETUP_STEPS[index].label}
          </h3>

          {index === 0 && (
            <form noValidate onSubmit={(e) => { e.preventDefault(); void saveOrganization(); }} id="wizOrgForm">
              <label className="field">
                <span>Organization name <em>*</em></span>
                <input value={orgName} onChange={(e) => setOrgName(e.target.value)} maxLength={120} data-autofocus />
              </label>
              <div className="field">
                <span>Logo (optional)</span>
                <div className="ent-logo-row">
                  {logo ? <img className="ent-logo-preview" src={logo} alt="Current logo" /> : <span className="hint">No logo yet</span>}
                  <label className="btn">
                    Choose image
                    <input type="file" accept={LOGO_TYPES.join(",")} hidden onChange={pickLogo} aria-label="Logo file" />
                  </label>
                  {logo && <button type="button" className="btn ghost" onClick={() => setLogo("")}>Remove</button>}
                </div>
              </div>
              <label className="field" htmlFor="wizAccent">
                <span>Accent colour (optional)</span>
                <span className="ent-color-row">
                  <input type="color" aria-label="Pick accent colour" value={swatch} onChange={(e) => setAccent(e.target.value)} />
                  <input id="wizAccent" value={accent} onChange={(e) => setAccent(e.target.value)} placeholder="#6366f1" maxLength={9} />
                </span>
              </label>
              <div className="wiz-pair">
                <label className="field">
                  <span>Email sender name (optional)</span>
                  <input value={senderName} onChange={(e) => setSenderName(e.target.value)} placeholder="Acme Bugs" maxLength={100} />
                </label>
                <label className="field">
                  <span>Sender email address</span>
                  <input type="email" value={senderAddress} onChange={(e) => setSenderAddress(e.target.value)} placeholder="bugs@acme.com" maxLength={150} />
                </label>
              </div>
              <p className="hint">Notification emails for this organization are sent as this sender. Your mail server must be allowed to send as it.</p>
            </form>
          )}

          {index === 1 && (
            <form noValidate onSubmit={(e) => { e.preventDefault(); void createProject(); }} id="wizProjectForm">
              {projects.length > 0 && (
                <p className="wiz-note" role="status">
                  You already have {projects.length === 1 ? "a project" : `${projects.length} projects`}:{" "}
                  <strong>{projects[0].name}</strong>. Add another below, or continue to invite your team.
                </p>
              )}
              <p className="hint">Projects hold work items. You can add more later from the sidebar.</p>
              <label className="field">
                <span>Project name {projects.length === 0 && <em>*</em>}</span>
                <input value={projectName} onChange={(e) => setProjectName(e.target.value)} maxLength={120} data-autofocus />
              </label>
              <label className="ent-check">
                <input type="checkbox" checked={scrum} onChange={(e) => setScrum(e.target.checked)} />
                <span>Use Scrum (sprints, a backlog and a board) for this project</span>
              </label>
            </form>
          )}

          {index === 2 && (
            <form noValidate onSubmit={(e) => { e.preventDefault(); void sendInvitations(); }} id="wizTeamForm">
              <label className="field">
                <span>Email addresses (optional)</span>
                <textarea rows={3} value={emails} onChange={(e) => setEmails(e.target.value)}
                  placeholder="ana@acme.com, ben@acme.com" data-autofocus />
              </label>
              <label className="field">
                <span>Role</span>
                <select value={role} onChange={(e) => setRole(e.target.value)}>
                  <option value="user">User</option>
                  <option value="manager">Manager</option>
                  <option value="admin">Admin</option>
                </select>
              </label>
              {projects.length > 0 && (
                <fieldset className="field">
                  <legend>Projects to join</legend>
                  <div className="ent-check-grid">
                    {projects.map((p) => (
                      <label key={p.id} className="ent-check">
                        <input type="checkbox" checked={projectIds.includes(p.id)} onChange={() => toggleProject(p.id)} />
                        <span>{p.name}</span>
                      </label>
                    ))}
                  </div>
                </fieldset>
              )}
              {results.length > 0 && (
                <ul className="wiz-results" aria-live="polite">
                  {results.map((r) => (
                    <li key={r.email} className={r.ok ? "ok" : "bad"}>
                      {r.ok ? `Invitation sent to ${r.email}` : `${r.email}: ${r.reason}`}
                    </li>
                  ))}
                </ul>
              )}
              <p className="hint">Each person gets a link by email. You can also invite people later under Organization &gt; Invitations.</p>
            </form>
          )}

          {index === 3 && (
            <ul className="wiz-extras">
              <li>
                <div>
                  <strong>Two-factor authentication</strong>
                  <p className="hint">Protect your own sign-in with an authenticator app.</p>
                </div>
                <button type="button" className="btn" onClick={onOpenAccount}>Open security settings</button>
              </li>
              <li>
                <div>
                  <strong>Webhooks</strong>
                  <p className="hint">Send item events to your own systems, signed with a secret.</p>
                </div>
                <button type="button" className="btn" onClick={onOpenWebhooks}>Open Organization</button>
              </li>
              <li>
                <div>
                  <strong>Custom fields</strong>
                  <p className="hint">Add your own fields to a project&apos;s items from the project&apos;s edit dialog.</p>
                </div>
                <button type="button" className="btn" onClick={() => onOpenGuide("organizations")}>Read how</button>
              </li>
            </ul>
          )}

          {index === 4 && (
            <div className="wiz-done">
              <p>Your organization is ready. Take the short tour to see where everything is, or start working.</p>
              <div className="ent-actions">
                <button type="button" className="btn" onClick={onTour}>Take the tour</button>
              </div>
            </div>
          )}

          <p className="wiz-error" role="alert" hidden={!error}>{error}</p>
        </div>
      </div>

      <div className="modal-foot wiz-foot">
        {index < last && <button type="button" className="btn ghost" onClick={onSkip}>Skip setup</button>}
        <span className="grow" />
        {index > 0 && index < last && <button type="button" className="btn" disabled={busy} onClick={() => go(index - 1)}>Back</button>}
        {index === 0 && (
          <button type="submit" form="wizOrgForm" className="btn primary" disabled={busy}>
            {busy ? "Saving..." : "Save and continue"}
          </button>
        )}
        {index === 1 && (
          <button type="submit" form="wizProjectForm" className="btn primary" disabled={busy}>
            {busy ? "Creating..." : projectName.trim() ? "Create project" : projects.length > 0 ? "Continue" : "Create project"}
          </button>
        )}
        {index === 2 && (
          <button type="submit" form="wizTeamForm" className="btn primary" disabled={busy}>
            {busy ? "Sending..." : parseEmails(emails).length ? "Send invitations" : "Skip for now"}
          </button>
        )}
        {index === 3 && <button type="button" className="btn primary" onClick={() => go(4)}>Continue</button>}
        {index === last && <button type="button" className="btn primary" data-autofocus onClick={onFinish}>Finish</button>}
      </div>
    </Modal>
  );
}
