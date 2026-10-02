/**
 * Unified create/edit/view modal for work items (bug == null → create).
 * Test contracts: #bugSubmitBtn inside #formBug, reporter_id select disabled, #bugCommentsSection visible in edit/view, #commentPostBtn type="button", no nested <form> in #formBug (composer is a <div>).
 */
import {
  useEffect,
  useRef,
  useState,
} from "react";
import { useApp } from "../state/AppContext";
import { api } from "../lib/api";
import { toast, toastError } from "../lib/toast";
import { withLoader } from "../lib/loader";
import {
  AGILE_ITEM_TYPES,
  expectedVersionForSave,
  itemTypeLabel,
  itemTypeOptions,
  itemTypeSelectionForCreate,
  itemTypeSelectionForEdit,
  resolveCreateItemType,
} from "../lib/itemTypes";
import { formatDate, initials } from "../lib/format";
import { fileTooLargeMessage, MAX_UPLOAD_BYTES } from "../lib/upload";
import { sanitizeHtml } from "../lib/sanitize";
import { confirmDialog } from "../components/ConfirmHost";
import BhSelect from "../components/BhSelect";
import BhDateInput from "../components/BhDateInput";
import BugCustomFields, { missingRequiredFields, saveCustomValues } from "../components/BugCustomFields";
import ChipPicker from "../components/ChipPicker";
import ItemPicker from "../components/ItemPicker";
import { useFileDrop } from "../lib/useFileDrop";
import RichEditor from "../components/RichEditor";
import GitBranchesPanel from "../components/GitBranchesPanel";
import {
  ActivityRow,
  AttachmentCard,
  StagedTile,
  canEditItemType,
  errorMessage,
  isoToday,
  itemTypeEmoji,
  plainTextFromHtml,
  plainToEditorHtml,
  stagedLabel,
  statusesForType,
  useStagedFiles,
} from "./bug/helpers";
const NO_EVENT_OPTION = { value: "", label: "— No event —" };
const SELECT_PLACEHOLDER = { value: "", label: "— select —" };

/** Mirrors ALLOWED_LINK_TYPES in app/schemas.py. */
const LINK_TYPE_OPTS = [
  { value: "relates", label: "relates to" },
  { value: "blocks", label: "blocks" },
  { value: "duplicate", label: "duplicates" },
];

async function uploadCreateBugFiles(createdId, files) {
  let done = 0;
  let failed = 0;
  for (const f of files) {
    const fd = new FormData();
    fd.append("file", f);
    try {
      await api(`/bugs/${createdId}/attachments`, { method: "POST", body: fd });
      done++;
    } catch (err) {
      failed++;
      toast(`Attachment ${f.name}: ${errorMessage(err)}`, "error");
    }
  }
  return { done, failed };
}

async function toastAfterCreate(created, ctype, files) {
  if (!(files.length && created?.id)) {
    toast(`${ctype} created`, "success");
    return;
  }
  const { done, failed } = await uploadCreateBugFiles(created.id, files);
  if (done) toast(`${ctype} #${created.id} created · ${done} file(s) attached`, "success");
  else if (failed) toast(`${ctype} #${created.id} created (no attachments saved)`, "info");
  else toast(`${ctype} created`, "success");
}

/** Board/Backlog/Hierarchy each keep their own fetched state (not wired to
 * the global refreshAll()) — this tells them to re-fetch after any mutation
 * here so a newly-created/moved item actually shows up without a reload. */
function notifyAgileChanged() {
  window.dispatchEvent(new Event("agile:refresh"));
}

function commentSubmitLabel(body, fileCount) {
  if (body && fileCount) return "Posting comment and uploading…";
  if (body) return "Posting comment…";
  return "Uploading file(s)…";
}

export default function BugModal() {
  const {
    bugModal,
    closeBugModal,
    reloadBugModal,
    refreshAll,
    refreshBugs,
    openBugDetail,
    currentUser,
    meta,
    users,
    projects,
    isAdmin,
    canManage,
    defaultNewType,
    setDefaultNewType,
  } = useApp();

  const {
    open, bug, defaultType, defaultEventId, defaultProjectId, defaultSprintId, defaultStatus,
    defaultEpicId, defaultParentId,
  } = bugModal;
  const isEdit = bug != null;
  const bugId = bug?.id ?? null;

  const [title, setTitle] = useState("");
  const [projectId, setProjectId] = useState("");
  const [itemType, setItemType] = useState("Bug");
  const [status, setStatus] = useState("New");
  const [priority, setPriority] = useState("Medium");
  const [environment, setEnvironment] = useState("DEV");
  const [eventId, setEventId] = useState("");
  const [eventOptions, setEventOptions] = useState([NO_EVENT_OPTION]);
  const [dueDate, setDueDate] = useState("");
  const [assigneeIds, setAssigneeIds] = useState([]);
  const [editingCommentId, setEditingCommentId] = useState(null);
  // Items chosen in the picker, not raw IDs.
  const [linkTargets, setLinkTargets] = useState([]);
  const [linkType, setLinkType] = useState("relates");
  // Sprint assignment: any Bug/Requirement/Task can be added to
  // a Sprint directly from its detail modal, tracked like a Story would be.
  const [availableSprints, setAvailableSprints] = useState([]);
  const [availableEpics, setAvailableEpics] = useState([]);
  const [sprintPick, setSprintPick] = useState("");
  const [sprintBusy, setSprintBusy] = useState(false);
  // Jira hierarchy links: a Sub-task's parent issue (required) and a standard
  // issue's Epic (optional).
  const [parentPick, setParentPick] = useState("");
  const [parentOptions, setParentOptions] = useState([]);
  const [parentQuery, setParentQuery] = useState("");
  // Every parent seen in a search (and the current one), so the chosen parent
  // keeps its label when a later search does not list it.
  const [knownParents, setKnownParents] = useState({});
  const [epicPick, setEpicPick] = useState("");
  // null = the selected project's Agile setting isn't loaded yet. Agile-only
  // types are only creatable once we know Agile is on for that project.
  const [projectAgile, setProjectAgile] = useState(null);
  const [enableAgileBusy, setEnableAgileBusy] = useState(false);

  const descRef = useRef(null);
  const commentRef = useRef(null);
  const editCommentRef = useRef(null);
  const titleRef = useRef(null);
  // Version frozen at open time so the server can detect concurrent edits on save.
  const expectedVersionRef = useRef(null);
  const customFieldsRef = useRef(null);

  // Three staging buckets: new-item, comment, and post-creation attachments.
  const createStaged = useStagedFiles();
  const commentStaged = useStagedFiles();
  const bugAttachStaged = useStagedFiles();

  const createDrop = useFileDrop((files) => createStaged.addFiles(files));
  const bugAttachDrop = useFileDrop((files) => bugAttachStaged.addFiles(files));
  const commentDrop = useFileDrop((files) => commentStaged.addFiles(files));

  // Create is always editable; edit prefers server can_edit, falling back to the role heuristic.
  const canEditThis = isEdit ? (bug.can_edit ?? canEditItemType(currentUser.role, bug.item_type)) : true;
  const readOnly = isEdit && !canEditThis;

  function seedEditForm() {
    // edit / view
    setTitle(bug.title || "");
    setProjectId(bug.project_id ? String(bug.project_id) : "");
    setItemType(bug.item_type || "Bug");
    setStatus(bug.status || "New");
    setPriority(bug.priority || "Medium");
    setEnvironment(bug.environment || "DEV");
    setEventId(bug.event_id ? String(bug.event_id) : "");
    setDueDate(bug.due_date || "");
    setAssigneeIds(bug.assignees ? bug.assignees.map((a) => a.id) : []);
    descRef.current?.setHtml(plainToEditorHtml(bug.description || ""));
    expectedVersionRef.current = bug.version ?? null;
    setEpicPick(bug.epic_id ? String(bug.epic_id) : "");
    setParentPick(bug.parent_id ? String(bug.parent_id) : "");
    setParentQuery("");
    // A sprint picked in an earlier session must never carry over.
    setSprintPick("");
  }

  function defaultProjectChoice() {
    if (defaultProjectId) return String(defaultProjectId);
    if (projects[0]?.id) return String(projects[0].id);
    return "";
  }

  function seedCreateForm() {
    // create
    setTitle("");
    setProjectId(defaultProjectChoice());
    setItemType(defaultType || defaultNewType || "Bug");
    setStatus(defaultStatus || "New");
    setPriority("Medium");
    setEnvironment("DEV");
    setEventId(defaultEventId ? String(defaultEventId) : "");
    setDueDate(isoToday()); // default to today
    setAssigneeIds([]);
    descRef.current?.setHtml("");
    expectedVersionRef.current = null;
    setSprintPick(defaultSprintId ? String(defaultSprintId) : "");
    setEpicPick(defaultEpicId ? String(defaultEpicId) : "");
    setParentPick(defaultParentId ? String(defaultParentId) : "");
    setParentQuery("");
  }

  function seedFormFields() {
    if (bug) seedEditForm();
    else seedCreateForm();
  }

  function eventOptionLabel(ev) {
    if (ev.scheduled_for) return `${ev.name} · ${ev.scheduled_for}`;
    return ev.name;
  }

  function seedEventOptions() {
    // Seed the current event immediately; replace with the full list once /events loads.
    const seed = [NO_EVENT_OPTION];
    if (bug?.event_id && bug.event_name) {
      seed.push({ value: String(bug.event_id), label: bug.event_name });
    }
    setEventOptions(seed);
    let cancelled = false;
    api("/events")
      .then((events) => {
        if (cancelled) return;
        setEventOptions([
          NO_EVENT_OPTION,
          ...(events || []).map((ev) => ({
            value: String(ev.id),
            label: eventOptionLabel(ev),
          })),
        ]);
      })
      .catch(() => {
        /* leave the placeholder if /events fails */
      });
    return () => { cancelled = true; };
  }

  function focusTitleOnOpen() {
    // Delay focus so the modal finishes mounting first.
    const ro = readOnly;
    if (ro) return undefined;
    const timer = window.setTimeout(() => titleRef.current?.focus(), 50);
    return () => window.clearTimeout(timer);
  }

  // Seed form fields on open or item switch; reloadBugModal() keeps the id so half-typed fields survive.
  useEffect(() => {
    if (!open) return undefined;
    setEditingCommentId(null);
    // Don't carry staged files across items.
    createStaged.clear();
    commentStaged.clear();
    bugAttachStaged.clear();
    commentRef.current?.setHtml("");
    setLinkTargets([]);
    setLinkType("relates");

    seedFormFields();
    const cancelEvents = seedEventOptions();
    const cancelFocus = focusTitleOnOpen();
    return () => {
      cancelEvents?.();
      cancelFocus?.();
    };
    // Keyed on open + bug id only — see seeding note above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, bugId]);

  // Focus the inline editor when it appears.
  useEffect(() => {
    if (editingCommentId != null) editCommentRef.current?.focus();
  }, [editingCommentId]);

  // Load the current project's Agile settings in both modes: open Sprints for
  // create/edit ("any item created can be added to a sprint") and
  // the Agile-enabled flag that gates the Agile-only item types.
  useEffect(() => {
    let forProjectId = null;
    if (isEdit) {
      forProjectId = bug?.project_id;
    } else if (projectId) {
      forProjectId = Number(projectId);
    }
    setProjectAgile(null);
    if (!open || !forProjectId) { setAvailableSprints([]); setAvailableEpics([]); return; }
    let cancelled = false;
    (async () => {
      try {
        const settings = await api(`/agile/projects/${forProjectId}/settings`);
        if (cancelled) return;
        setProjectAgile(Boolean(settings.agile_enabled));
        if (!settings.agile_enabled || !settings.board_id) { setAvailableSprints([]); setAvailableEpics([]); return; }
        const rows = await api(`/agile/sprints?board_id=${settings.board_id}`);
        const epicRows = await api(`/agile/epics?project_id=${forProjectId}`);
        if (cancelled) return;
        const open_ = rows.filter((s) => s.state === "active" || s.state === "future");
        setAvailableSprints(open_);
        setAvailableEpics(epicRows);
        // New issues go to the backlog unless the caller (a sprint's
        // "Create issue") asked for a sprint, as in Jira.
      } catch {
        if (!cancelled) setAvailableSprints([]);
      }
    })();
    return () => { cancelled = true; };
  }, [open, isEdit, bugId, bug?.project_id, projectId]);

  // Parent candidates for a Sub-task: the project's standard issues matching
  // the search (server-side, so large projects are fully reachable).
  useEffect(() => {
    if (!open || itemType !== "Sub-task" || !projectId) {
      setParentOptions([]);
      return undefined;
    }
    let cancelled = false;
    const timer = setTimeout(() => {
      const params = new URLSearchParams({ project_id: String(projectId), page_size: "50" });
      for (const t of ["Story", "Requirement", "Task", "Bug"]) params.append("item_type", t);
      if (parentQuery.trim()) params.set("q", parentQuery.trim());
      api(`/bugs?${params}`)
        .then((res) => {
          if (cancelled) return;
          const items = res?.items || [];
          setParentOptions(items);
          setKnownParents((prev) => ({ ...prev, ...Object.fromEntries(items.map((p) => [String(p.id), p])) }));
        })
        .catch(() => {
          if (!cancelled) setParentOptions([]);
        });
    }, parentQuery ? 250 : 0);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [open, isEdit, itemType, projectId, parentQuery]);

  // The current (or preset) parent's title.
  useEffect(() => {
    const parentId = isEdit ? bug?.parent_id : defaultParentId;
    if (!open || !parentId) return undefined;
    let cancelled = false;
    api(`/bugs/${parentId}`)
      .then((p) => { if (!cancelled && p) setKnownParents((prev) => ({ ...prev, [String(p.id)]: p })); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [open, isEdit, bug?.parent_id, defaultParentId]);

  // Epic and parent belong to one project: switching the new item's project
  // drops a pick from the previous one.
  useEffect(() => {
    if (!open || isEdit) return;
    setEpicPick((prev) => (prev && String(defaultEpicId || "") === prev && String(defaultProjectId || "") === String(projectId) ? prev : ""));
    setParentPick((prev) => (prev && String(defaultParentId || "") === prev && String(defaultProjectId || "") === String(projectId) ? prev : ""));
    setSprintPick((prev) => (prev && String(defaultSprintId || "") === prev && String(defaultProjectId || "") === String(projectId) ? prev : ""));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, isEdit, projectId]);

  // Keep a new item's type creatable for the selected project: a remembered
  // default type (or a list view's active tab) can name an Agile-only type while
  // the project has Agile disabled, which the API can only refuse.
  useEffect(() => {
    if (!open || isEdit) return;
    setItemType((prev) => resolveCreateItemType({
      requested: prev,
      values: itemTypeSelectionForCreate(meta.item_types, { agileEnabled: projectAgile }),
    }));
  }, [open, isEdit, meta.item_types, projectAgile]);

  const enableAgileForProject = async () => {
    const pid = Number.parseInt(projectId, 10);
    if (!pid) return;
    setEnableAgileBusy(true);
    try {
      await api(`/agile/projects/${pid}/enable`, { method: "POST", json: { feature_flags: {} } });
      setProjectAgile(true);
      toast("Agile enabled for this project", "success");
      notifyAgileChanged();
    } catch (err) {
      toastError(err);
    } finally {
      setEnableAgileBusy(false);
    }
  };

  const addToSprint = async () => {
    if (!sprintPick || !bugId) return;
    setSprintBusy(true);
    try {
      await api(`/agile/sprints/${sprintPick}/items`, { method: "POST", json: { item_ids: [bugId] } });
      toast("Added to sprint", "success");
      setSprintPick("");
      notifyAgileChanged();
      await reloadBugModal();
    } catch (err) {
      toastError(err);
    } finally {
      setSprintBusy(false);
    }
  };

  const removeFromSprint = async () => {
    if (!bug?.sprint_id || !bugId) return;
    setSprintBusy(true);
    try {
      await api(`/agile/sprints/${bug.sprint_id}/items/${bugId}`, { method: "DELETE" });
      toast("Removed from sprint", "success");
      notifyAgileChanged();
      await reloadBugModal();
    } catch (err) {
      toastError(err);
    } finally {
      setSprintBusy(false);
    }
  };

  // Bail when closed so poll ticks don't recompute options / rebuild RichEditors. All hooks run above.
  if (!open) return null;

  // Keep any status the item already holds even if invalid for the type — flagged "(legacy)".
  const validStatuses = statusesForType(meta, itemType);
  const statusOpts = [
    SELECT_PLACEHOLDER,
    ...validStatuses.map((s) => ({ value: s, label: s })),
  ];
  if (status && !validStatuses.includes(status)) {
    statusOpts.push({ value: status, label: `${status} (legacy)` });
  }

  // Same-table conversions only. Edit mode offers exactly the types the backend
  // accepts for a reclassification (Bug/Requirement/Task/User Story) and keeps
  // the item's own type visible when it is a legacy hierarchy type; create mode
  // keeps the full set because creation paths differ per type.
  // The option source + Story label live in lib/itemTypes.js (covered by tests).
  const itemTypeValues = isEdit
    ? itemTypeSelectionForEdit(itemType)
    : itemTypeSelectionForCreate(meta.item_types, { agileEnabled: projectAgile });
  const itemTypeOpts = [
    SELECT_PLACEHOLDER,
    ...itemTypeOptions(itemTypeValues),
  ];
  const projectOpts = [
    SELECT_PLACEHOLDER,
    ...projects.map((p) => ({ value: String(p.id), label: p.name })),
  ];
  // Agile-only types (Epic / Story / Sub-task) have no
  // create path while the selected project has Agile disabled — say so instead of
  // letting the request fail, and offer the switch to permitted roles.
  const agileTypesBlocked = !isEdit && projectAgile === false;
  const priorityOpts = [
    SELECT_PLACEHOLDER,
    ...meta.priorities.map((s) => ({ value: s, label: s })),
  ];
  const environmentOpts = [
    { value: "DEV", label: "DEV" },
    { value: "UAT", label: "UAT" },
    { value: "PROD", label: "PROD" },
  ];

  // Show the current event by name if the list hasn't loaded yet.
  const eventOpts =
    eventId && bug?.event_name && !eventOptions.some((o) => o.value === eventId)
      ? [...eventOptions, { value: eventId, label: bug.event_name }]
      : eventOptions;

  // Reporter is fixed — inject the original reporter when it isn't the current user.
  const me = currentUser;
  const reporterOption =
    isEdit && bug.reporter && bug.reporter.id !== me.id
      ? bug.reporter
      : { id: me.id, name: me.name, email: me.email };
  const reporterId = isEdit && bug.reporter ? bug.reporter.id : me.id;

  const assigneeItems = users
    .filter((u) => u.is_active)
    .map((u) => ({ id: u.id, label: u.name, title: u.role }));

  const headType = isEdit ? bug.item_type || "Bug" : defaultType || defaultNewType || "Bug";
  const headTitle = isEdit
    ? `${itemTypeEmoji(headType)} ${headType} #${bug.id}`
    : "New Item";
  const headSubtitle = isEdit ? bug.title || "" : "";

  // Edit: pasted images upload immediately as bug-level attachments. Create: staged until save.
  const onDescPaste = async (f) => {
    if (bugId) {
      const tooBig = fileTooLargeMessage(f);
      if (tooBig) {
        toast(tooBig, "error");
        return;
      }
      const fd = new FormData();
      fd.append("file", f);
      await api(`/bugs/${bugId}/attachments`, { method: "POST", body: fd });
      await reloadBugModal();
      toast(`Attached: ${f.name || "pasted file"}`, "success");
    } else {
      createStaged.addFiles([f]); // addFiles() checks the size limit
      if (f.size <= MAX_UPLOAD_BYTES) toast(`Staged: ${f.name || "pasted file"}`, "info");
    }
  };

  // Pasted files stage in the comment bucket, uploaded with comment_id once the comment posts.
  const onComposerPaste = (f) => {
    commentStaged.addFiles([f]);
    toast(`Staged: ${f.name || "pasted file"}`, "info");
  };

  // Inline-editor comment is already saved, so pastes attach at the bug level (no comment_id to backfill).
  const onEditCommentPaste = async (f) => {
    if (!bugId) return;
    const tooBig = fileTooLargeMessage(f);
    if (tooBig) {
      toast(tooBig, "error");
      return;
    }
    const fd = new FormData();
    fd.append("file", f);
    await api(`/bugs/${bugId}/attachments`, { method: "POST", body: fd });
    toast(`Attached: ${f.name || "pasted file"}`, "success");
  };

  const onDeleteBug = async ()=> {
    if (!bugId) return;
    const itype = bug?.item_type || "Bug";
    const noun = itype.toLowerCase();
    const ok = await confirmDialog(
      `Delete ${noun} #${bugId}? This will also delete its comments and attachments. Cannot be undone`,
    );
    if (!ok) return;
    try {
      await withLoader(async () => {
        await api(`/bugs/${bugId}`, { method: "DELETE" });
        closeBugModal();
        await refreshAll();
      }, `Deleting ${noun}…`);
      toast(`${itype} #${bugId} deleted`, "success");
      notifyAgileChanged();
    } catch (err) {
      toastError(err);
    }
  };

  const onDeleteAttachment = async (attId) => {
    const ok = await confirmDialog("Delete this attachment?");
    if (!ok || !bugId) return;
    try {
      await withLoader(async () => {
        await api(`/bugs/${bugId}/attachments/${attId}`, { method: "DELETE" });
        await reloadBugModal();
        await refreshBugs();
      }, "Deleting attachment…");
      toast("Attachment deleted", "success");
    } catch (err) {
      toastError(err);
    }
  };

  const onDeleteComment = async (commentId) => {
    const ok = await confirmDialog(
      "Delete this comment? Its attachments will be removed too. Cannot be undone",
    );
    if (!ok || !bugId) return;
    try {
      await withLoader(async () => {
        await api(`/bugs/${bugId}/comments/${commentId}`, { method: "DELETE" });
        await reloadBugModal();
        await refreshBugs();
      }, "Deleting comment…");
      toast("Comment deleted", "success");
    } catch (err) {
      toastError(err);
    }
  };

  const onSaveEditComment = async (commentId) => {
    if (!bugId) return;
    const body = (editCommentRef.current?.getHtml() ?? "").trim();
    if (!body) {
      toast("Comment body can't be empty", "error");
      return;
    }
    try {
      await withLoader(async () => {
        await api(`/bugs/${bugId}/comments/${commentId}`, { method: "PUT", json: { body } });
        setEditingCommentId(null);
        await reloadBugModal();
      }, "Saving comment…");
      toast("Comment updated", "success");
    } catch (err) {
      toastError(err);
    }
  };

  const onCancelEditComment = ()=> {
    setEditingCommentId(null);
    void reloadBugModal();
  };

  // Composer files always attach to the comment, not the bug.
  const onPostComment = async ()=> {
    if (!bugId) return;
    const body = (commentRef.current?.getHtml() ?? "").trim();
    const files = commentStaged.files;
    if (!body && files.length === 0) {
      toast("Add a comment or attach a file", "error");
      commentRef.current?.focus();
      return;
    }
    if (!body && files.length > 0) {
      toast(
        "Add some comment text — files attached here become comment " +
          "attachments. For a bug-level file, use the 📎 Add attachment " +
          "button above.",
        "error",
      );
      commentRef.current?.focus();
      return;
    }
    try {
      await withLoader(async () => {
        const comment = await api(`/bugs/${bugId}/comments`, {
          method: "POST",
          json: { body },
        });
        let failed = 0;
        for (const s of files) {
          const fd = new FormData();
          fd.append("file", s.file);
          fd.append("comment_id", String(comment.id));
          try {
            await api(`/bugs/${bugId}/attachments`, { method: "POST", body: fd });
          } catch (err) {
            failed++;
            toast(`Attachment ${s.file.name}: ${errorMessage(err)}`, "error");
          }
        }
        if (body) toast("Comment posted", "success");
        else if (files.length && !failed) {
          toast(`${files.length} file${files.length > 1 ? "s" : ""} attached`, "success");
        }
        // Reset for the next post.
        commentRef.current?.setHtml("");
        commentStaged.clear();
        await reloadBugModal();
        await refreshBugs();
      }, commentSubmitLabel(body, files.length));
    } catch (err) {
      toastError(err);
    }
  };

  // Flush staged bug-level attachments on "Upload N file(s)".
  const flushBugAttach = async ()=> {
    if (!bugId) return;
    const files = bugAttachStaged.files;
    if (!files.length) return;
    let done = 0;
    for (const s of files) {
      const fd = new FormData();
      fd.append("file", s.file);
      try {
        await api(`/bugs/${bugId}/attachments`, { method: "POST", body: fd });
        done++;
      } catch (err) {
        toast(`Attachment ${s.file.name}: ${errorMessage(err)}`, "error");
      }
    }
    bugAttachStaged.clear();
    if (done) toast(`${done} file${done > 1 ? "s" : ""} attached`, "success");
    // Refresh so the new attachment_count shows in the table cell too.
    await reloadBugModal();
    await refreshBugs();
  };

  // Link this item to every item chosen in the picker.
  const onAddLink = async ()=> {
    if (!bugId) return;
    if (linkTargets.length === 0) {
      toast("Pick one or more items to link", "error");
      return;
    }
    try {
      await withLoader(async () => {
        let done = 0;
        let failed = 0;
        for (const t of linkTargets) {
          try {
            await api(`/bugs/${bugId}/links`, {
              method: "POST",
              json: { target_bug_id: t.id, link_type: linkType },
            });
            done++;
          } catch (err) {
            failed++;
            toast(`Link to #${t.id}: ${errorMessage(err)}`, "error");
          }
        }
        setLinkTargets([]);
        await reloadBugModal();
        if (done) {
          toast(
            `Linked ${done} item${done > 1 ? "s" : ""}${failed ? " (" + failed + " failed)" : ""}`,
            "success",
          );
        }
      }, linkTargets.length > 1 ? "Linking items…" : "Linking…");
    } catch (err) {
      toastError(err);
    }
  };

  // Planning work into or out of sprints is a manager's job (the server
  // enforces it too); members see the sprint but cannot change it.
  function renderSprintRemoveButton() {
    if (readOnly || !canManage) return null;
    return (
      <button type="button" className="btn ghost btn-sm" disabled={sprintBusy} onClick={removeFromSprint}>
        Remove
      </button>
    );
  }

  function renderSprintCurrent() {
    return (
      <div className="bug-sprint-current">
        <span className="badge" data-status="In Progress">{bug.sprint_name || `Sprint #${bug.sprint_id}`}</span>
        {renderSprintRemoveButton()}
      </div>
    );
  }

  function renderSprintPicker() {
    return (
      <div className="bug-sprint-picker">
        <BhSelect
          ariaLabel="Sprint"
          value={sprintPick}
          onChange={setSprintPick}
          options={[
            { value: "", label: "Not in a sprint — pick one…" },
            ...availableSprints.map((s) => ({ value: String(s.id), label: `${s.name} (${s.state})` })),
          ]}
          disabled={readOnly || sprintBusy}
        />
        {!readOnly && (
          <button type="button" className="btn ghost btn-sm" disabled={!sprintPick || sprintBusy} onClick={addToSprint}>
            Add to sprint
          </button>
        )}
      </div>
    );
  }

  function renderEditSprintField() {
    if (bug.sprint_id) return renderSprintCurrent();
    if (!canManage) return <span className="muted small">Not in a sprint (managers plan sprints)</span>;
    if (availableSprints.length > 0) return renderSprintPicker();
    return (
      <span className="muted small">Not in a sprint (set up the Scrum board for this project to plan it)</span>
    );
  }

  function renderCreateSprintField() {
    if (availableSprints.length === 0 || !canManage) return null;
    return (
      <div className="field bug-sprint-field">
        <span>Sprint</span>
        <BhSelect
          ariaLabel="Sprint"
          value={sprintPick}
          onChange={setSprintPick}
          options={[
            { value: "", label: "No sprint (Backlog)" },
            ...availableSprints.map((s) => ({ value: String(s.id), label: `${s.name} (${s.state})` })),
          ]}
        />
      </div>
    );
  }

  function parentLabel(p) {
    return `${p.display_id || `#${p.id}`} ${p.title}`;
  }

  /** Jira's hierarchy fields: Parent for a Sub-task, Epic for a standard issue. */
  function renderParentField() {
    if (itemType === "Epic") return null;
    if (itemType === "Sub-task") {
      const options = parentOptions.filter((p) => p.id !== bugId);
      if (parentPick && !options.some((p) => String(p.id) === parentPick)) {
        options.unshift(knownParents[parentPick] || { id: Number(parentPick), title: "", display_id: `#${parentPick}` });
      }
      return (
        <div className="field" aria-labelledby="bugParentLabel">
          <span id="bugParentLabel">Parent issue <em>*</em></span>
          {!readOnly && (
            <input type="search" className="bug-parent-search" value={parentQuery}
              onChange={(e) => setParentQuery(e.target.value)} placeholder="Search issues by title or key…"
              aria-label="Search for the parent issue" maxLength={200} />
          )}
          <BhSelect
            ariaLabel="Parent issue"
            value={parentPick}
            onChange={setParentPick}
            disabled={readOnly}
            options={[
              { value: "", label: options.length ? "— select the parent issue —" : (parentQuery ? "No matching issues" : "No issues in this project yet") },
              ...options.map((p) => ({ value: String(p.id), label: parentLabel(p) })),
            ]}
          />
          <small className="hint">A Sub-task belongs to one Story, Task, Bug or Requirement and follows its sprint and epic.</small>
        </div>
      );
    }
    const epics = availableEpics.filter((e) => !e.archived || (isEdit && e.id === bug.epic_id));
    if (!epics.length && !(isEdit && bug.epic_id)) return null;
    return (
      <div className="field" aria-labelledby="bugEpicLabel">
        <span id="bugEpicLabel">Epic</span>
        <BhSelect
          ariaLabel="Epic"
          value={epicPick}
          onChange={setEpicPick}
          disabled={readOnly}
          options={[
            { value: "", label: "— No epic —" },
            ...epics.map((ep) => ({ value: String(ep.id), label: `${ep.display_id || `#${ep.id}`} ${ep.title}` })),
          ]}
        />
      </div>
    );
  }

  function renderSprintField() {
    // Epics are not planned into sprints; a Sub-task follows its parent.
    if (itemType === "Epic") return null;
    if (itemType === "Sub-task") {
      return (
        <div className="field bug-sprint-field">
          <span>Sprint</span>
          <span className="muted small">
            {isEdit && bug.sprint_name ? `${bug.sprint_name} (from the parent issue)` : "Follows the parent issue's sprint"}
          </span>
        </div>
      );
    }
    if (!isEdit) return renderCreateSprintField();
    return (
      <div className="field bug-sprint-field">
        <span>Sprint</span>
        {renderEditSprintField()}
      </div>
    );
  }

  const onRemoveLink = async (linkId) => {
    if (!bugId) return;
    try {
      await withLoader(async () => {
        await api(`/bugs/${bugId}/links/${linkId}`, { method: "DELETE" });
        await reloadBugModal();
      }, "Removing link…");
      toast("Link removed", "success");
    } catch (err) {
      toastError(err);
    }
  };

  const onSubmit = async (e) => {
    e.preventDefault();
    const id = bugId;
    // New items use the current user ; edits preserve the original.
    const reporterFromForm = isEdit ? bug.reporter?.id ?? null : null;
    const reporterFromMe = currentUser.id || null;
    const payload = {
      project_id: Number.parseInt(projectId, 10),
      title: title.trim(),
      description: plainTextFromHtml(descRef.current?.getHtml() ?? ""),
      reporter_id: id ? reporterFromForm || reporterFromMe : reporterFromMe,
      item_type: itemType || "Bug",
      status,
      priority,
      environment,
      due_date: dueDate || null,
      // Empty or "0" → null so the server treats it  explicit unlink.
      event_id: eventId && eventId !== "0" ? Number.parseInt(eventId, 10) : null,
      assignee_ids: assigneeIds,
      // Optimistic concurrency: send the opened version so the server 409s on a concurrent edit.
      ...(expectedVersionForSave({ isEdit: Boolean(id), version: expectedVersionRef.current }) != null
        ? { expected_version: expectedVersionRef.current }
        : {}),
    };
    // Persist the chosen type so the next new-item modal defaults to it.
    if (!id) setDefaultNewType(payload.item_type || "Bug");
    if (!payload.project_id) {
      toast("Please pick a project", "error");
      return;
    }
    if (!payload.title) {
      toast("Title is required", "error");
      return;
    }
    if (!payload.reporter_id) {
      toast("Reporter is required", "error");
      return;
    }
    const missingFields = missingRequiredFields(customFieldsRef.current);
    if (missingFields.length > 0) {
      toast(`Fill in the required field${missingFields.length > 1 ? "s" : ""}: ${missingFields.join(", ")}`, "error");
      return;
    }

    // Guard staged-but-never-uploaded files (edit mode only; create uploads them in-flow).
    if (id) {
      if (bugAttachStaged.files.length > 0) {
        const n = bugAttachStaged.files.length;
        toast(
          `You have ${n} attachment${n > 1 ? "s" : ""} that ${n > 1 ? "haven't" : "hasn't"} been uploaded yet. ` +
            'Click "Upload" in the Attachments section, or remove ' +
            `${n > 1 ? "them" : "it"}, before saving.`,
          "error",
        );
        return;
      }
      if (commentStaged.files.length > 0) {
        const n = commentStaged.files.length;
        toast(
          `You have ${n} file${n > 1 ? "s" : ""} attached to a comment you haven't posted yet. ` +
            'Click "Post" to add the comment, or remove ' +
            `${n > 1 ? "them" : "it"}, before saving.`,
          "error",
        );
        return;
      }
    }

    // Only a sprint offered for this item's project counts (never a stale pick).
    const sprintPickValid = Boolean(sprintPick) && availableSprints.some((sp) => String(sp.id) === String(sprintPick));
    // refreshAll() keeps the Work Items list in sync after the modal closes.
    try {
      if (id) {
        const result = await withLoader(async () => {
          // Each step returns the new version; keep it, so if a later step
          // fails the dialog stays open and Save can be retried without a
          // version conflict on the part that already went through.
          let updated = await api(`/bugs/${id}`, { method: "PUT", json: payload });
          expectedVersionRef.current = updated?.version ?? expectedVersionRef.current;
          const standard = !["Epic", "Sub-task"].includes(updated?.item_type || payload.item_type);
          if (standard && String(epicPick || "") !== String(bug?.epic_id || "")) {
            updated = await api(`/agile/work-items/${id}/hierarchy`, {
              method: "PUT", json: { epic_id: epicPick ? Number(epicPick) : null, version: updated.version },
            });
            expectedVersionRef.current = updated?.version ?? expectedVersionRef.current;
          }
          if (itemType === "Sub-task" && parentPick && String(parentPick) !== String(bug?.parent_id || "")) {
            updated = await api(`/agile/work-items/${id}/hierarchy`, {
              method: "PUT", json: { parent_id: Number(parentPick), version: updated.version },
            });
            expectedVersionRef.current = updated?.version ?? expectedVersionRef.current;
          }
          if (canManage && standard && sprintPickValid && String(sprintPick) !== String(bug?.sprint_id || "")) {
            await api(`/agile/sprints/${sprintPick}/items`, {
              method: "POST", json: { item_ids: [id] },
            });
          }
          await saveCustomValues(id, customFieldsRef.current);
          closeBugModal();
          await refreshAll();
          return updated;
        }, "Saving changes…");
        const utype = result?.item_type || payload.item_type || "Bug";
        toast(`${utype} #${id} updated`, "success");
        notifyAgileChanged();
      } else {
        // Warn (don't block) if the due date falls outside the picked Sprint's
        // date range before creating — matches the same guard used elsewhere
        // when assigning existing items to a Sprint.
        if (sprintPick) {
          const sprint = availableSprints.find((s) => String(s.id) === String(sprintPick));
          const due = payload.due_date;
          const outOfRange = sprint && due && (
            (sprint.start_date && due < sprint.start_date) || (sprint.end_date && due > sprint.end_date)
          );
          if (outOfRange) {
            const ok = await confirmDialog(
              `This item's due date (${due}) falls outside "${sprint.name}"'s sprint window ` +
              `(${sprint.start_date || "?"} – ${sprint.end_date || "?"}). Add it to this sprint anyway?`,
              { title: "Due date outside Sprint range", okLabel: "Add anyway", danger: false },
            );
            if (!ok) return;
          }
        }
        if (itemType === "Sub-task" && !parentPick) {
          toast("Pick the parent issue this Sub-task belongs to", "error");
          return;
        }
        // Only standard issues are planned into sprints (Sub-tasks follow
        // their parent, Epics span sprints).
        const sprintIdForCreate = canManage && sprintPickValid && !["Epic", "Sub-task"].includes(itemType)
          ? Number(sprintPick) : null;
        const epicIdForCreate = epicPick && !["Epic", "Sub-task"].includes(itemType) ? Number(epicPick) : null;
        // POST the item, then upload staged files; per-file failures toast but don't roll back the create.
        await withLoader(async () => {
          let created;
          // Agile create paths place the item in the sprint themselves; only
          // the legacy /bugs create needs the follow-up sprint assignment.
          let needsSprintAdd = false;
          if (["Epic", "Story", "Sub-task"].includes(itemType)) {
            created = await api("/agile/work-items", { method: "POST", json: {
              project_id: payload.project_id, title: payload.title, description: payload.description,
              item_type: itemType, sprint_id: sprintIdForCreate, status: payload.status,
              priority: payload.priority, end_date: payload.due_date,
              assignee_ids: payload.assignee_ids,
              parent_id: itemType === "Sub-task" ? Number(parentPick) : null,
              epic_id: epicIdForCreate,
            }});
          } else {
            created = await api("/bugs", { method: "POST", json: payload });
            needsSprintAdd = Boolean(sprintIdForCreate);
            if (epicIdForCreate && created?.id) {
              await api(`/agile/work-items/${created.id}/hierarchy`, {
                method: "PUT", json: { epic_id: epicIdForCreate, version: created.version },
              });
            }
          }
          const ctype = created?.item_type || payload.item_type || "Bug";
          const files = createStaged.files.map((s) => s.file);
          await toastAfterCreate(created, ctype, files);
          if (needsSprintAdd && created?.id) {
            try {
              await api(`/agile/sprints/${sprintIdForCreate}/items`, { method: "POST", json: { item_ids: [created.id] } });
            } catch (err) {
              toastError(err);
            }
          }
          try {
            await saveCustomValues(created?.id, customFieldsRef.current);
          } catch (err) {
            toastError(err);
          }
          createStaged.clear();
          closeBugModal();
          await refreshAll();
          notifyAgileChanged();
        }, "Creating item…");
      }
    } catch (err) {
      // An Agile-only create is refused while the project's Agile surface is off;
      // surface the reason plus the switch instead of a bare "not found".
      if (!id && AGILE_ITEM_TYPES.includes(itemType) && err?.status === 404) {
        setProjectAgile(false);
        toast(
          `Agile is not enabled for this project, so ${itemTypeLabel(itemType)} items `
            + "cannot be created yet."
            + (canManage ? " Use “Enable Agile” above." : " Ask a manager or admin to enable it."),
          "error",
        );
        return;
      }
      toastError(err);
    }
  };

  function renderCommentEditButton(c) {
    return (
      <button
        type="button"
        className="icon-btn"
        data-act="edit-comment"
        data-id={c.id}
        title="Edit comment"
        onClick={() => setEditingCommentId(c.id)}
      >
        ✎
      </button>
    );
  }

  function renderCommentAdminActions(c, hasBody) {
    if (!isAdmin) return null;
    return (
      <span className="comment-admin-actions">
        {hasBody ? renderCommentEditButton(c) : null}
        <button
          type="button"
          className="icon-btn danger"
          data-act="delete-comment"
          data-id={c.id}
          title="Delete comment"
          onClick={() => void onDeleteComment(c.id)}
        >
          🗑
        </button>
      </span>
    );
  }

  function renderCommentAttachments(c) {
    if (c.attachments.length === 0) return null;
    return (
      <div className="comment-attachments">
        <div className="attachment-grid">
          {c.attachments.map((a) => (
            <AttachmentCard
              key={a.id}
              att={a}
              bugId={c.bug_id}
              deletable={isAdmin}
              onDelete={(attId) => void onDeleteAttachment(attId)}
            />
          ))}
        </div>
      </div>
    );
  }

  function renderCommentBody(c, hasBody) {
    if (!hasBody) return null;
    // Server sanitizes on write; sanitizeHtml() here is a defensive second pass.
    return (
      <div
        className="comment-body bh-rich-content"
        data-comment-body={c.id}
        dangerouslySetInnerHTML={{ __html: sanitizeHtml(c.body) }}
      />
    );
  }

  function renderCommentEditRow(c) {
    return (
      <div className="comment-edit-row">
        <RichEditor
          key={`comment-edit-${c.id}`}
          ref={editCommentRef}
          initialHtml={c.body || ""}
          placeholder="Edit comment…"
          ariaLabel="Edit comment"
          textareaId={`commentEditBody-${c.id}`}
          onPasteFile={onEditCommentPaste}
        />
        <div className="comment-edit-actions">
          <button
            type="button"
            className="btn ghost"
            data-act="cancel-edit-comment"
            data-id={c.id}
            onClick={onCancelEditComment}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn primary"
            data-act="save-edit-comment"
            data-id={c.id}
            onClick={() => void onSaveEditComment(c.id)}
          >
            Save
          </button>
        </div>
      </div>
    );
  }

  function renderCommentContent(c, isEditing, hasBody) {
    if (isEditing) return renderCommentEditRow(c);
    return renderCommentBody(c, hasBody);
  }

  const renderComment = (c) => {
    const hasBody = (c.body || "").trim().length > 0;
    const isEditing = editingCommentId === c.id;
    return (
      <div className="comment" data-comment-id={c.id} key={c.id}>
        <div className="comment-head">
          <div className="comment-head-left">
            <span className="avatar">{initials(c.author_name)}</span>
            <span className="comment-author">{c.author_name}</span>
          </div>
          <span className="comment-head-right">
            <span className="comment-time">{formatDate(c.created_at)}</span>
            {renderCommentAdminActions(c, hasBody)}
          </span>
        </div>
        {renderCommentContent(c, isEditing, hasBody)}
        {renderCommentAttachments(c)}
      </div>
    );
  };

  return (
    <div className="modal" id="modalBug" hidden={!open}>
      <div className="modal-card xxl">
        <div className="modal-head">
          <div className="bug-modal-head-left">
            <h2 id="modalBugTitle">{headTitle}</h2>
            <span className="bug-modal-subtitle muted small" id="modalBugSubtitle">
              {headSubtitle}
            </span>
          </div>
          <div className="bug-modal-head-actions">
            <button
              type="button"
              className="btn danger"
              id="bugDeleteBtn"
              data-needs-role="admin"
              hidden={!(isEdit && isAdmin) || readOnly}
              onClick={() => void onDeleteBug()}
            >
              🗑 Delete
            </button>
            <button
              type="button"
              className="icon-btn modal-close"
              data-close-modal
              aria-label="Close"
              onClick={closeBugModal}
            >
              ✕
            </button>
          </div>
        </div>
        <form
          id="formBug"
          className="modal-body bug-modal-body"
          noValidate
          data-read-only={readOnly ? "1" : ""}
          onSubmit={(e) => void onSubmit(e)}
        >
          {readOnly && (
            <div className="bug-readonly-banner">
              {`Read-only — only admins and managers can edit ${(bug?.item_type || "item").toLowerCase()}s.`}
            </div>
          )}
          <input type="hidden" name="id" value={bugId != null ? String(bugId) : ""} readOnly />

          {/* Title — full-width headline */}
          <label className="field f-grow bug-title-field">
            <span>
              Title <em>*</em>
            </span>
            <input
              name="title"
              required
              minLength={3}
              maxLength={200}
              placeholder="Short summary…"
              ref={titleRef}
              value={title}
              disabled={readOnly}
              onChange={(e) => setTitle(e.target.value)}
            />
          </label>

          <div className="bug-modal-grid">
            {/* Main column */}
            <div className="bug-modal-main">
              {/* div not label: a <label> would route clicks to the hidden native textarea, not the surface. */}
              <div className="field">
                <span>Description</span>
                <RichEditor
                  ref={descRef}
                  name="description"
                  ariaLabel="Description"
                  // Descriptions are stored as plain text; formatting would be discarded.
                  formatting={false}
                  placeholder="Context, acceptance criteria, repro steps — whatever's useful…"
                  disabled={readOnly}
                  onPasteFile={onDescPaste}
                />
              </div>

              {/* Attachments for new items — staged and uploaded after the POST. */}
              <section
                className="bug-section bug-create-attach-section"
                id="bugCreateAttachSection"
                hidden={isEdit}
              >
                <div
                  className={`comment-form attach-dropzone${createDrop.dragging ? " is-dragging" : ""}`}
                  {...createDrop.dropProps}
                >
                  <div className="attach-drop-hint">Drop files to attach</div>
                  <div className="comment-form-row">
                    <label className="comment-attach-btn" title="Attach files">
                      📎 <span id="createFileLabel">{stagedLabel(createStaged.files.length, "Attach files")}</span>
                      <input
                        type="file"
                        multiple
                        id="createBugFiles"
                        aria-label="Attachments for new bug"
                        onChange={(e) => {
                          // Reset value so re-picking the same file fires onChange again.
                          createStaged.addFiles(e.currentTarget.files ?? []);
                          e.currentTarget.value = "";
                        }}
                      />
                    </label>
                    <div className="attach-staged-list" id="createFilePreview">
                      {createStaged.files.map((s, i) => (
                        <StagedTile
                          key={s.url}
                          staged={s}
                          bucket="createBug"
                          idx={i}
                          onRemove={() => createStaged.removeAt(i)}
                        />
                      ))}
                    </div>
                  </div>
                  <p className="muted small create-attach-hint">
                    Files attach to the item itself. Add more later via the Add-attachment button or via comments
                  </p>
                </div>
              </section>

              {/* Bug-level attachments (edit mode); drop files here to stage. */}
              <section
                className={`bug-section${canEditThis && bugAttachDrop.dragging ? " attach-dropzone is-dragging" : ""}`}
                id="bugAttachmentsSection"
                hidden={!isEdit}
                {...(canEditThis ? bugAttachDrop.dropProps : {})}
              >
                {canEditThis && <div className="attach-drop-hint">Drop files to attach</div>}
                <div className="bug-section-head">
                  <h3>
                    Attachments{" "}
                    <span className="muted small" id="attachmentsCount">
                      {bug?.attachments.length ? `(${bug.attachments.length})` : ""}
                    </span>
                  </h3>
                  <label
                    className="comment-attach-btn bug-attach-add-btn"
                    id="bugAttachAddLabel"
                    title="Add an attachment to this item"
                    hidden={!isEdit || !canEditThis}
                  >
                    📎 <span id="bugAttachAddText">{stagedLabel(bugAttachStaged.files.length, "Add attachment")}</span>
                    <input
                      type="file"
                      multiple
                      id="bugAttachAddInput"
                      aria-label="Add an attachment to this item"
                      onChange={(e) => {
                        bugAttachStaged.addFiles(e.currentTarget.files ?? []);
                        e.currentTarget.value = "";
                      }}
                    />
                  </label>
                </div>
                <div className="attach-staged-list" id="bugAttachAddPreview">
                  {bugAttachStaged.files.map((s, i) => (
                    <StagedTile
                      key={s.url}
                      staged={s}
                      bucket="bugAttach"
                      idx={i}
                      onRemove={() => bugAttachStaged.removeAt(i)}
                    />
                  ))}
                  {bugAttachStaged.files.length > 0 && (
                    <div className="attach-staged-actions">
                      <button
                        type="button"
                        className="btn primary attach-staged-upload"
                        onClick={() => {
                          withLoader(flushBugAttach, "Uploading attachment(s)…").catch(toastError);
                        }}
                      >
                        Upload {bugAttachStaged.files.length} file
                        {bugAttachStaged.files.length > 1 ? "s" : ""}
                      </button>
                      <button
                        type="button"
                        className="btn ghost attach-staged-cancel"
                        onClick={() => bugAttachStaged.clear()}
                      >
                        Cancel
                      </button>
                    </div>
                  )}
                </div>
                <div className="attachment-grid" id="bugAttachmentsGrid">
                  {bug?.attachments.map((a) => (
                    <AttachmentCard
                      key={a.id}
                      att={a}
                      bugId={bug.id}
                      deletable={isAdmin}
                      onDelete={(attId) => void onDeleteAttachment(attId)}
                    />
                  ))}
                </div>
                <div
                  className="bug-attach-empty muted small"
                  id="bugAttachEmpty"
                  hidden={!bug || bug.attachments.length > 0 || !canEditThis}
                >
                  No attachments yet. Click <strong>📎 Add attachment</strong> above to upload one
                </div>
              </section>

              {/* Comments — visible in edit and read-only view. Composer is a <div>, not a <form> (HTML5 forbids nesting). */}
              <section className="bug-section" id="bugCommentsSection" hidden={!isEdit}>
                <h3>
                  Comments{" "}
                  <span className="muted small" id="commentsCount">
                    {bug ? `(${bug.comments.length})` : ""}
                  </span>
                </h3>
                <div
                  id="bugCommentsList"
                  className="bug-comments-list"
                  hidden={!bug || bug.comments.length === 0}
                >
                  {bug?.comments.map((c) => renderComment(c))}
                </div>
                <div
                  className={`comment-form attach-dropzone${commentDrop.dragging ? " is-dragging" : ""}`}
                  id="commentForm"
                  hidden={readOnly}
                  aria-label="Add a comment"
                  {...commentDrop.dropProps}
                >
                  <div className="attach-drop-hint">Drop files to attach</div>
                  <RichEditor
                    ref={commentRef}
                    textareaId="commentBody"
                    ariaLabel="New comment"
                    placeholder="Add a comment, attach a file, or both (Ctrl/Cmd+Enter to post)"
                    onCtrlEnter={(e) => {
                      e.preventDefault();
                      void onPostComment();
                    }}
                    onPasteFile={onComposerPaste}
                  />
                  <div className="comment-form-row">
                    <label className="comment-attach-btn" title="Attach files">
                      📎 <span id="fileLabel">{stagedLabel(commentStaged.files.length, "Attach files")}</span>
                      <input
                        type="file"
                        multiple
                        id="commentFiles"
                        onChange={(e) => {
                          commentStaged.addFiles(e.currentTarget.files ?? []);
                          e.currentTarget.value = "";
                        }}
                      />
                    </label>
                    <div className="attach-staged-list" id="filePreview">
                      {commentStaged.files.map((s, i) => (
                        <StagedTile
                          key={s.url}
                          staged={s}
                          bucket="comment"
                          idx={i}
                          onRemove={() => commentStaged.removeAt(i)}
                        />
                      ))}
                    </div>
                    {commentStaged.files.length > 0 && (
                      <button
                        type="button"
                        className="btn ghost attach-staged-cancel"
                        style={{ marginLeft: "auto" }}
                        onClick={() => commentStaged.clear()}
                      >
                        Cancel
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn primary"
                      id="commentPostBtn"
                      style={commentStaged.files.length > 0 ? undefined : { marginLeft: "auto" }}
                      onClick={() => void onPostComment()}
                    >
                      Post
                    </button>
                  </div>
                </div>
              </section>

              {/* Linked items — inverse phrasing computed server-side. */}
              <section className="bug-section bug-links-section" id="bugLinksSection" hidden={!isEdit}>
                <h3>
                  Linked items{" "}
                  <span className="muted small" id="linksCount">
                    {bug ? `(${bug.links.length})` : ""}
                  </span>
                </h3>
                {bug && bug.links.length > 0 ? (
                  <div className="bug-links-list" id="bugLinksList">
                    {bug.links.map((link) => (
                      <div className="bug-link-row" data-link-id={link.id} key={link.id}>
                        <span className="bug-link-rel">{link.label}</span>
                        <button
                          type="button"
                          className="bug-link-target"
                          title={`Open #${link.other_bug_id}`}
                          onClick={() => void openBugDetail(link.other_bug_id)}
                        >
                          <span className="inline-type" data-type={link.other_bug_item_type}>
                            {itemTypeEmoji(link.other_bug_item_type)}
                          </span>{" "}
                          #{link.other_bug_id} {link.other_bug_title}
                        </button>
                        <span className="badge" data-status={link.other_bug_status}>
                          {link.other_bug_status}
                        </span>
                        {canEditThis && (
                          <button
                            type="button"
                            className="icon-btn danger bug-link-remove"
                            title="Remove link"
                            aria-label="Remove link"
                            onClick={() => void onRemoveLink(link.id)}
                          >
                            ✕
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="no-content">No linked items yet</p>
                )}
                {canEditThis && (
                  <div className="bug-link-add" id="bugLinkAdd">
                    <div className="bug-link-add-type">
                      <BhSelect
                        name="link_type"
                        ariaLabel="Link type"
                        value={linkType}
                        onChange={setLinkType}
                        options={LINK_TYPE_OPTS}
                      />
                    </div>
                    <div className="bug-link-add-target">
                      <ItemPicker
                        selected={linkTargets}
                        onChange={setLinkTargets}
                        excludeIds={[
                          ...(bugId != null ? [bugId] : []),
                          ...(bug?.links.map((l) => l.other_bug_id) ?? []),
                        ]}
                      />
                    </div>
                    <button
                      type="button"
                      className="btn primary"
                      disabled={linkTargets.length === 0}
                      onClick={() => void onAddLink()}
                    >
                      Link{linkTargets.length > 1 ? ` ${linkTargets.length}` : ""}
                    </button>
                  </div>
                )}
              </section>

              <GitBranchesPanel targetType="work-item" targetId={bugId} visible={isEdit} itemType={bug?.item_type || itemType} />

              {/* Activity — collapsible */}
              <section
                className="bug-section bug-activity-section"
                id="bugActivitySection"
                hidden={!isEdit}
              >
                <details>
                  <summary>
                    Activity history{" "}
                    <span className="muted small" id="activityCount">
                      {bug ? `(${bug.activities.length})` : ""}
                    </span>
                  </summary>
                  <div id="bugActivityList" className="bug-activity-list">
                    {bug && bug.activities.length > 0 ? (
                      bug.activities.map((a) => <ActivityRow key={a.id} activity={a} />)
                    ) : (
                      <p className="no-content">No activity yet</p>
                    )}
                  </div>
                </details>
              </section>
            </div>

            {/* Side column — metadata */}
            <aside className="bug-modal-side" aria-label="Work item metadata">
              <div className="field" aria-labelledby="bugTypeLabel">
                <span id="bugTypeLabel">
                  Type <em>*</em>
                </span>
                <BhSelect
                  name="item_type"
                  ariaLabel="Type"
                  value={itemType}
                  onChange={(v) => setItemType(v)}
                  options={itemTypeOpts}
                  disabled={readOnly}
                />
                {agileTypesBlocked && (
                  <small className="hint" id="bugTypeAgileHint">
                    Agile is not enabled for this project, so{" "}
                    {AGILE_ITEM_TYPES.map(itemTypeLabel).join(", ")} items cannot be created yet.{" "}
                    {canManage ? (
                      <button
                        type="button"
                        className="btn ghost"
                        id="bugEnableAgileBtn"
                        onClick={enableAgileForProject}
                        disabled={enableAgileBusy}
                      >
                        {enableAgileBusy ? "Enabling…" : "Enable Agile"}
                      </button>
                    ) : (
                      "Ask a manager or admin to enable Agile for it."
                    )}
                  </small>
                )}
              </div>
              <div className="field" aria-labelledby="bugProjectLabel">
                <span id="bugProjectLabel">
                  Project <em>*</em>
                </span>
                <BhSelect
                  name="project_id"
                  ariaLabel="Project"
                  value={projectId}
                  onChange={setProjectId}
                  options={projectOpts}
                  disabled={readOnly}
                />
              </div>
              {renderParentField()}
              <div className="field" aria-labelledby="bugStatusLabel">
                <span id="bugStatusLabel">Status</span>
                <BhSelect
                  name="status"
                  ariaLabel="Status"
                  value={status}
                  onChange={setStatus}
                  options={statusOpts}
                  disabled={readOnly}
                />
              </div>
              <div className="field" aria-labelledby="bugPriorityLabel">
                <span id="bugPriorityLabel">Priority</span>
                <BhSelect
                  name="priority"
                  ariaLabel="Priority"
                  value={priority}
                  onChange={setPriority}
                  options={priorityOpts}
                  disabled={readOnly}
                />
              </div>
              <div className="field" aria-labelledby="bugEnvLabel">
                <span id="bugEnvLabel">Environment</span>
                <BhSelect
                  name="environment"
                  ariaLabel="Environment"
                  value={environment}
                  onChange={setEnvironment}
                  options={environmentOpts}
                  disabled={readOnly}
                />
              </div>
              <div className="field" aria-labelledby="bugReporterLabel">
                <span id="bugReporterLabel">Reporter</span>
                {/* Fixed to current user (or original reporter on edit); disabled, id comes from state. Hand-rolled so the reporter-select class survives (BhSelect can't add classes). */}
                <div className="bh-sel-wrap">
                  <select
                    className="bh-sel-native reporter-select"
                    name="reporter_id"
                    aria-labelledby="bugReporterLabel"
                    disabled
                    value={String(reporterId)}
                    onChange={() => undefined}
                  >
                    <option value="">— select —</option>
                    <option value={String(reporterOption.id)} title={reporterOption.email}>
                      {reporterOption.name}
                    </option>
                  </select>
                  <button
                    type="button"
                    className="bh-sel-btn is-disabled"
                    aria-haspopup="listbox"
                    aria-expanded={false}
                    disabled
                  >
                    <span className="bh-sel-label">{reporterOption.name}</span>
                  </button>
                </div>
              </div>
              <fieldset className="field">
                <legend>Assignees</legend>
                <ChipPicker
                  id="assigneePicker"
                  items={assigneeItems}
                  selected={assigneeIds}
                  disabled={readOnly}
                  onToggle={(uid) =>
                    setAssigneeIds((prev) =>
                      prev.includes(uid) ? prev.filter((x) => x !== uid) : [...prev, uid],
                    )
                  }
                />
              </fieldset>
              <div className="field" aria-labelledby="bugEventLabel">
                <span id="bugEventLabel">Event</span>
                <BhSelect
                  name="event_id"
                  ariaLabel="Event"
                  value={eventId}
                  onChange={setEventId}
                  options={eventOpts}
                  disabled={readOnly}
                />
              </div>
              <div className="field" aria-labelledby="bugDueLabel">
                <span id="bugDueLabel">Due date</span>
                <BhDateInput
                  name="due_date"
                  ariaLabel="Due date"
                  value={dueDate}
                  onChange={setDueDate}
                  disabled={readOnly}
                />
              </div>

              {/* Sprint tracking — any Bug/Requirement/Task can be added to a
                  Sprint here, not just Stories. Create-mode
                  defaults to the project's active Sprint and
                  applies the pick right after the item is created. */}
              {renderSprintField()}

              {/* Timestamps — visible in edit mode only */}
              <div className="bug-side-meta" id="bugSideMeta" hidden={!isEdit}>
                <div className="bug-side-meta-row">
                  <span className="k">Created</span>
                  <span className="v" id="bugMetaCreated">
                    {bug ? formatDate(bug.created_at) : "—"}
                  </span>
                </div>
                <div className="bug-side-meta-row">
                  <span className="k">Updated</span>
                  <span className="v" id="bugMetaUpdated">
                    {bug ? formatDate(bug.updated_at) : "—"}
                  </span>
                </div>
              </div>

              <BugCustomFields projectId={Number.parseInt(projectId, 10) || null} bugId={bugId}
                readOnly={readOnly} handleRef={customFieldsRef} />
            </aside>
          </div>

          <div className="modal-foot">
            <button type="button" className="btn ghost" data-close-modal onClick={closeBugModal}>
              Cancel
            </button>
            {/* #bugSubmitBtn must stay inside #formBug — tests depend on it. */}
            <button type="submit" className="btn primary" id="bugSubmitBtn" hidden={readOnly}>
              {isEdit ? "Save changes" : "Create"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
