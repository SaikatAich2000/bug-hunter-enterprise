// Accept-invitation page logic.
//
// On load: read ?token=… from URL, fetch a public preview so we can show
// the invitee the org name + role they're joining, then let them set a
// name and password to finalise. On success we have a session cookie
// and bounce into the app.
(() => {
  "use strict";
  const $ = (sel) => document.querySelector(sel);

  // Theme persistence.
  const stored = localStorage.getItem("theme") || "dark";
  document.documentElement.setAttribute("data-theme", stored);

  const params = new URLSearchParams(location.search);
  const token = (params.get("token") || "").trim();

  function showSection(which) {
    $("#loadingState").hidden = which !== "loading";
    $("#errorState").hidden = which !== "error";
    $("#acceptForm").hidden = which !== "form";
  }

  function showFatalError(msg) {
    $("#errorAlert").textContent = msg;
    showSection("error");
  }

  function showAlert(msg, kind = "error") {
    const el = $("#acceptAlert");
    el.textContent = msg;
    el.className = "auth-alert " + kind;
    el.hidden = false;
  }
  function hideAlert() {
    $("#acceptAlert").hidden = true;
  }

  function extractError(data, fallback = "Something went wrong.") {
    if (!data) return fallback;
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) {
      return data.detail.map((e) => {
        const loc = Array.isArray(e.loc) ? e.loc.slice(1).join(".") : "";
        return loc ? `${loc}: ${e.msg}` : e.msg;
      }).join("; ");
    }
    return fallback;
  }

  function clientValidate(form) {
    const name = form.elements.name.value.trim();
    const password = form.elements.password.value;
    if (!name) return "Please enter your name.";
    if (password.length < 8) return "Password needs at least 8 characters.";
    if (!/[A-Za-z]/.test(password) || !/[0-9]/.test(password)) {
      return "Password should mix letters and numbers.";
    }
    return null;
  }

  async function loadPreview() {
    if (!token) {
      showFatalError("This invitation link is missing its token. Ask the person who invited you to send it again.");
      return;
    }
    try {
      const res = await fetch(`/api/invitations/preview/${encodeURIComponent(token)}`, {
        credentials: "include",
      });
      if (!res.ok) {
        let data = null;
        try { data = await res.json(); } catch { /* ignore */ }
        showFatalError(extractError(data, "This invitation isn't valid anymore."));
        return;
      }
      const preview = await res.json();
      $("#orgNameLabel").textContent = preview.organization_name || "Bug Hunter";
      $("#roleLabel").textContent = preview.role || "member";
      $("#emailLabel").textContent = preview.email || "";
      if (preview.invited_by_name) {
        $("#inviterPart").textContent = ` by ${preview.invited_by_name}`;
      }
      showSection("form");
      $("#acceptForm").elements.name.focus();
    } catch (err) {
      showFatalError("Network error. Please check your connection and reload the page.");
    }
  }

  $("#acceptForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    hideAlert();
    const f = e.target;
    const localErr = clientValidate(f);
    if (localErr) {
      showAlert(localErr);
      return;
    }
    const btn = $("#acceptSubmit");
    btn.disabled = true;
    btn.textContent = "Accepting…";
    try {
      const res = await fetch("/api/invitations/accept", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          token,
          name: f.elements.name.value.trim(),
          password: f.elements.password.value,
        }),
      });
      if (!res.ok) {
        let data = null;
        try { data = await res.json(); } catch { /* ignore */ }
        showAlert(extractError(data, "Couldn't accept the invitation."));
        return;
      }
      // We're logged in now.
      location.href = "/";
    } catch (err) {
      showAlert("Network error. Try again.");
    } finally {
      btn.disabled = false;
      btn.textContent = "Accept & sign in";
    }
  });

  loadPreview();
})();
