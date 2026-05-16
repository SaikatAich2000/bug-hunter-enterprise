// Login page logic.
//
// This file used to be an inline <script> block in login.html. Inline
// scripts are blocked by the app's strict Content Security Policy
// (script-src 'self', no 'unsafe-inline'), so loading the same code
// from /static/ is what lets the page work in production. Same-origin
// served files satisfy the policy without weakening it.
(() => {
  "use strict";
  const $ = (sel) => document.querySelector(sel);

  // Theme persists across pages.
  const stored = localStorage.getItem("theme") || "dark";
  document.documentElement.setAttribute("data-theme", stored);

  function showAlert(id, msg, kind = "error") {
    const el = $(id);
    el.textContent = msg;
    el.className = "auth-alert " + kind;
    el.hidden = false;
  }
  function hideAlerts() {
    $("#loginAlert").hidden = true;
    $("#forgotAlert").hidden = true;
  }

  $("#showForgot").addEventListener("click", (e) => {
    e.preventDefault();
    hideAlerts();
    $("#loginForm").hidden = true;
    $("#forgotForm").hidden = false;
    $("#forgotForm").elements.email.focus();
  });
  $("#backToLogin").addEventListener("click", (e) => {
    e.preventDefault();
    hideAlerts();
    $("#forgotForm").hidden = true;
    $("#loginForm").hidden = false;
    $("#loginForm").elements.email.focus();
  });

  $("#loginThemeBtn").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("theme", next);
  });

  $("#loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    hideAlerts();
    const f = e.target;
    const btn = $("#loginSubmit");
    btn.disabled = true; btn.textContent = "Signing in…";
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          email: f.elements.email.value.trim(),
          password: f.elements.password.value,
        }),
      });
      if (!res.ok) {
        let msg = "Sign in failed";
        try {
          const data = await res.json();
          if (typeof data.detail === "string") {
            msg = data.detail;
          } else if (Array.isArray(data.detail)) {
            msg = data.detail.map(e => e.msg).join(", ");
          }
        } catch {}
        showAlert("#loginAlert", msg);
        return;
      }
      // Success — redirect to home (or `next` query param if provided).
      const params = new URLSearchParams(location.search);
      const next = params.get("next") || "/";
      location.href = next;
    } catch (err) {
      showAlert("#loginAlert", "Network error. Try again.");
    } finally {
      btn.disabled = false; btn.textContent = "Sign in";
    }
  });

  $("#forgotForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    hideAlerts();
    const f = e.target;
    const btn = f.querySelector("button[type=submit]");
    btn.disabled = true; btn.textContent = "Sending…";
    try {
      const res = await fetch("/api/auth/forgot-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: f.elements.email.value.trim() }),
      });
      if (res.status === 204) {
        showAlert("#forgotAlert",
          "If that email is registered, a reset link has been sent. Check your inbox.",
          "success");
      } else {
        let msg = "Request failed";
        try {
          const data = await res.json();
          if (typeof data.detail === "string") {
            msg = data.detail;
          } else if (Array.isArray(data.detail)) {
            msg = data.detail.map(e => e.msg).join(", ");
          }
        } catch {}
        showAlert("#forgotAlert", msg);
      }
    } catch (err) {
      showAlert("#forgotAlert", "Network error. Try again.");
    } finally {
      btn.disabled = false; btn.textContent = "Send reset link";
    }
  });
})();
