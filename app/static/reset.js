// Reset-password page logic.
//
// Extracted from an inline <script> in reset.html so it complies with
// the app's strict Content Security Policy (script-src 'self', no
// 'unsafe-inline'). See login.js for the same reasoning.
(() => {
  "use strict";
  const $ = (sel) => document.querySelector(sel);
  document.documentElement.setAttribute("data-theme",
    localStorage.getItem("theme") || "dark");

  const params = new URLSearchParams(location.search);
  const token = params.get("token") || "";

  function showAlert(msg, kind = "error") {
    const el = $("#resetAlert");
    el.textContent = msg;
    el.className = "auth-alert " + kind;
    el.hidden = false;
  }

  if (!token) {
    showAlert("Reset link is missing or malformed. Request a new one from the sign-in page.");
    $("#resetForm").querySelectorAll("input, button").forEach(e => e.disabled = true);
  }

  $("#resetForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const newPw = f.elements.new_password.value;
    const confirmPw = f.elements.confirm_password.value;
    if (newPw !== confirmPw) {
      showAlert("Passwords don't match.");
      return;
    }
    if (newPw.length < 8) {
      showAlert("Password must be at least 8 characters.");
      return;
    }

    const btn = $("#resetSubmit");
    btn.disabled = true; btn.textContent = "Saving…";
    try {
      const res = await fetch("/api/auth/reset-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, new_password: newPw }),
      });
      if (res.status === 204) {
        showAlert("Password updated. Redirecting to sign in…", "success");
        setTimeout(() => { location.href = "/login.html"; }, 1500);
      } else {
        let msg = "Reset failed";
        try { msg = (await res.json()).detail || msg; } catch {}
        showAlert(msg);
      }
    } catch (err) {
      showAlert("Network error. Try again.");
    } finally {
      btn.disabled = false; btn.textContent = "Set new password";
    }
  });
})();
