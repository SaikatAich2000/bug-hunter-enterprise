// Signup page logic — creates a new organization + admin user, then
// redirects into the app. Mirrors login.js patterns for consistency.
(() => {
  "use strict";
  const $ = (sel) => document.querySelector(sel);

  // Persist theme across pages.
  const stored = localStorage.getItem("theme") || "dark";
  document.documentElement.setAttribute("data-theme", stored);

  function showAlert(msg, kind = "error") {
    const el = $("#signupAlert");
    el.textContent = msg;
    el.className = "auth-alert " + kind;
    el.hidden = false;
  }
  function hideAlert() {
    $("#signupAlert").hidden = true;
  }

  function extractError(data) {
    if (!data) return "Sign-up failed. Please try again.";
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) {
      // Pydantic field errors — surface the first useful one with its field.
      const parts = data.detail.map((e) => {
        const loc = Array.isArray(e.loc) ? e.loc.slice(1).join(".") : "";
        return loc ? `${loc}: ${e.msg}` : e.msg;
      });
      return parts.join("; ");
    }
    return "Sign-up failed.";
  }

  // Light client-side hint — server is authoritative.
  function clientValidate(form) {
    const orgName = form.elements.organization_name.value.trim();
    const name = form.elements.name.value.trim();
    const email = form.elements.email.value.trim();
    const password = form.elements.password.value;
    if (!orgName) return "Please enter an organization name.";
    if (!name) return "Please enter your name.";
    if (!email || !email.includes("@")) return "Please enter a valid email.";
    if (password.length < 8) return "Password needs at least 8 characters.";
    if (!/[A-Za-z]/.test(password) || !/[0-9]/.test(password)) {
      return "Password should mix letters and numbers.";
    }
    return null;
  }

  $("#signupForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    hideAlert();
    const f = e.target;
    const localErr = clientValidate(f);
    if (localErr) {
      showAlert(localErr);
      return;
    }
    const btn = $("#signupSubmit");
    btn.disabled = true;
    btn.textContent = "Creating…";
    try {
      const res = await fetch("/api/auth/signup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          organization_name: f.elements.organization_name.value.trim(),
          name: f.elements.name.value.trim(),
          email: f.elements.email.value.trim(),
          password: f.elements.password.value,
        }),
      });
      if (res.status === 403) {
        showAlert("Public sign-up is disabled on this server. Ask an admin for an invite.");
        return;
      }
      if (!res.ok) {
        let data = null;
        try { data = await res.json(); } catch { /* ignore */ }
        showAlert(extractError(data));
        return;
      }
      // Logged in — head into the app.
      location.href = "/";
    } catch (err) {
      showAlert("Network error. Please try again.");
    } finally {
      btn.disabled = false;
      btn.textContent = "Create organization";
    }
  });
})();
