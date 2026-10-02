/**
 * EventModal — create/edit form for an Event (create defaults scheduled_for to today).
 * Manager picker is limited to active admin/manager users (backend rejects others).
 */
import { useEffect, useRef, useState } from "react";
import Modal from "../components/Modal";
import BhDateInput from "../components/BhDateInput";
import ChipPicker from "../components/ChipPicker";
import { api } from "../lib/api";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";
import { isoToday } from "./bug/helpers";

export default function EventModal({ open, event, onClose, onSaved }) {
  const { users, projects } = useApp();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [scheduledFor, setScheduledFor] = useState("");
  const [managerIds, setManagerIds] = useState([]);
  // "" = nothing picked yet; dropdown is scoped to projects the actor can see.
  const [projectId, setProjectId] = useState("");
  const nameRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    if (event) {
      setName(event.name || "");
      setDescription(event.description || "");
      setScheduledFor(event.scheduled_for || "");
      setManagerIds(event.managers.map((m) => m.id));
      setProjectId(event.project_id ?? "");
    } else {
      setName("");
      setDescription("");
      setScheduledFor(isoToday()); // default to today (local calendar date)
      setManagerIds([]);
      setProjectId(projects[0]?.id ?? ""); // projects loaded at app boot

    }
    const t = setTimeout(() => nameRef.current?.focus(), 50);
    return () => clearTimeout(t);
  }, [open, event, projects]);

  // Backend rejects non-admin/manager users  managers, so exclude them.
  const eligibleManagers = users.filter(
    (u) => u.is_active && (u.role === "admin" || u.role === "manager"),
  );
  // Keep already-assigned-but-now-ineligible managers so they can be deselected (else silently re-submitted).
  const managerPickerItems = eligibleManagers.map((u) => ({ id: u.id, label: u.name, title: u.role }));
  const eligibleManagerIds = new Set(managerPickerItems.map((i) => i.id));
  for (const m of event?.managers ?? []) {
    if (managerIds.includes(m.id) && !eligibleManagerIds.has(m.id)) {
      managerPickerItems.push({ id: m.id, label: `${m.name} (inactive)`, title: m.role });
    }
  }

  const submit = async (e) => {
    e.preventDefault();
    const id = event?.id ?? null;
    const trimmedName = name.trim();
    if (!trimmedName) {
      toast("Event name is required", "error");
      return;
    }
    if (projectId === "") {
      toast("Pick a project for this event", "error");
      return;
    }
    const payload = {
      name: trimmedName,
      description,
      scheduled_for: scheduledFor || null,
      project_id: projectId,
      manager_ids: managerIds,
    };
    try {
      await withLoader(async () => {
        const result = id
          ? await api(`/events/${id}`, { method: "PUT", json: payload })
          : await api("/events", { method: "POST", json: payload });
        onClose();
        await onSaved(result, id != null);
        return result;
      }, id ? "Saving event…" : "Creating event…");
      toast(id ? "Event updated" : "Event created", "success");
    } catch (err) {
      toastError(err);
    }
  };

  return (
    <Modal
      id="modalEvent"
      open={open}
      title={
        <span id="modalEventTitle">
          {event ? `Edit "${event.name}"` : "New Event"}
        </span>
      }
      onClose={onClose}
    >
      <form id="formEvent" className="modal-body" onSubmit={submit}>
        <input type="hidden" name="id" value={event ? String(event.id) : ""} readOnly />
        <label className="field">
          <span>
            Name <em>*</em>
          </span>
          <input
            name="name"
            required
            minLength={2}
            maxLength={200}
            placeholder="Morning standup · 2026-05-28"
            ref={nameRef}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label className="field">
          <span>
            Project <em>*</em>
          </span>
          <select
            name="project_id"
            required
            value={projectId === "" ? "" : String(projectId)}
            onChange={(e) => setProjectId(e.target.value ? Number(e.target.value) : "")}
          >
            <option value="" disabled>
              Select a project…
            </option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <small className="hint">
            Only people with access to this project will see the event and its items.
          </small>
        </label>
        <label className="field" htmlFor="eventScheduledFor">
          <span>Scheduled for</span>
          <BhDateInput id="eventScheduledFor" name="scheduled_for" value={scheduledFor} onChange={setScheduledFor} />
        </label>
        <fieldset className="field">
          <legend>Managers</legend>
          <ChipPicker
            id="eventManagerPicker"
            items={managerPickerItems}
            selected={managerIds}
            onToggle={(uid) =>
              setManagerIds((prev) =>
                prev.includes(uid) ? prev.filter((x) => x !== uid) : [...prev, uid],
              )
            }
          />
          <small className="hint">
            Managers get notified when the event is created, edited or deleted — but not for
            individual tasks inside it. Only admin/manager users can be event managers
          </small>
        </fieldset>
        <label className="field">
          <span>Description</span>
          <textarea
            name="description"
            rows={3}
            maxLength={10000}
            placeholder="Agenda, attendees, notes — whatever's useful"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          ></textarea>
        </label>
        <div className="modal-foot">
          <button type="button" className="btn ghost" data-close-modal="" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn primary">
            Save
          </button>
        </div>
      </form>
    </Modal>
  );
}
