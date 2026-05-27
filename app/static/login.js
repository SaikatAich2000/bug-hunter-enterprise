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

  // Holds the short-lived token returned by step-1 (password) so step-2
  // (TOTP) can echo it back.
  let pendingTotpToken = "";

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
      // Step-1 may return `{requires_totp: true, pending_token}` which
      // means the password was right but the user has 2FA on. Surface
      // the 6-digit code form instead of redirecting.
      const data = await res.json().catch(() => ({}));
      if (data && data.requires_totp && data.pending_token) {
        pendingTotpToken = data.pending_token;
        $("#loginForm").hidden = true;
        const totpForm = $("#totpForm");
        if (totpForm) {
          totpForm.hidden = false;
          setTimeout(() => totpForm.elements.code?.focus(), 50);
        }
        return;
      }
      // No 2FA — straight to home / next.
      const params = new URLSearchParams(location.search);
      const next = params.get("next") || "/";
      location.href = next;
    } catch (err) {
      showAlert("#loginAlert", "Network error. Try again");
    } finally {
      btn.disabled = false; btn.textContent = "Sign in";
    }
  });

  // 2FA step-2 handler — fires only when the page contains a #totpForm.
  const totpForm = $("#totpForm");
  if (totpForm) {
    totpForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      hideAlerts();
      const code = (totpForm.elements.code.value || "").trim();
      if (!code) {
        showAlert("#totpAlert", "Enter the 6-digit code from your authenticator app");
        return;
      }
      const btn = totpForm.querySelector("button[type=submit]");
      btn.disabled = true; btn.textContent = "Verifying…";
      try {
        const res = await fetch("/api/auth/login/totp", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ pending_token: pendingTotpToken, code }),
        });
        if (!res.ok) {
          let msg = "Invalid code";
          try {
            const data = await res.json();
            if (typeof data.detail === "string") msg = data.detail;
          } catch {}
          showAlert("#totpAlert", msg);
          return;
        }
        const params = new URLSearchParams(location.search);
        const next = params.get("next") || "/";
        location.href = next;
      } catch (err) {
        showAlert("#totpAlert", "Network error. Try again");
      } finally {
        btn.disabled = false; btn.textContent = "Verify";
      }
    });

    $("#totpBack")?.addEventListener("click", (e) => {
      e.preventDefault();
      pendingTotpToken = "";
      totpForm.hidden = true;
      $("#loginForm").hidden = false;
      $("#loginForm").elements.password.value = "";
      $("#loginForm").elements.email.focus();
    });
  }

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
        // Email exists — the server queued the reset link. Be specific
        // about what happens next so the user knows to check their inbox.
        showAlert("#forgotAlert",
          "Reset link sent. Check your inbox — the link expires in 30 minutes",
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
      showAlert("#forgotAlert", "Network error. Try again");
    } finally {
      btn.disabled = false; btn.textContent = "Send reset link";
    }
  });
})();
