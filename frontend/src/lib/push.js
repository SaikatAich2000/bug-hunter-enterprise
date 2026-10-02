/** Web push via FCM (foreground side). SDK self-hosted under /static/vendor to keep CSP script-src 'self'. */
import { api } from "./api";
import { toast } from "./toast";

// minimal compat-global shapes (compat SDK ships no types)

let configCache = null;
let messagingCache = null;

// Push failures are non-fatal by design, but silence makes them undiagnosable.
function warn(reason, err = "") {
  console.warn(`[push] ${reason}`, err ?? "");
}

function supported() {
  return (
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "Notification" in window &&
    "PushManager" in window
  );
}

async function getConfig() {
  if (configCache) return configCache;
  try {
    configCache = await api("/push/config");
    return configCache;
  } catch (err) {
    warn("could not load /push/config", err);
    return null;
  }
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`)) {
      resolve();
      return;
    }
    const el = document.createElement("script");
    el.src = src;
    el.async = true;
    el.onload = () => resolve();
    el.onerror = () => {
      // remove failed node so a retry isn't short-circuited by the querySelector guard
      el.remove();
      reject(new Error(`failed to load ${src}`));
    };
    document.head.appendChild(el);
  });
}

async function ensureMessaging(cfg) {
  await loadScript("/static/vendor/firebase-app-compat.js");
  await loadScript("/static/vendor/firebase-messaging-compat.js");
  const fb = window.firebase;
  if (!fb) {
    warn("Firebase SDK did not load (blocked by network or extension)");
    return null;
  }
  if (fb.apps.length === 0) {
    fb.initializeApp({
      apiKey: cfg.api_key,
      authDomain: cfg.auth_domain,
      projectId: cfg.project_id,
      messagingSenderId: cfg.messaging_sender_id,
      appId: cfg.app_id,
    });
  }
  const reg = await navigator.serviceWorker.register("/firebase-messaging-sw.js");
  const messaging = fb.messaging();
  if (!messagingCache) {
    messagingCache = messaging;
    // FCM only auto-pops a system notification for background messages; show one
    // ourselves for foreground messages too, via the same service worker so a
    // click routes through its existing notificationclick handler.
    messaging.onMessage((payload) => {
      const n = payload.notification;
      if (!n?.title) return;
      const url = payload.data?.url || "/";
      reg.showNotification(n.title, {
        body: n.body || "",
        icon: "/static/icon.png?v=" + document.querySelector('meta[name="application-asset-version"]')?.content,
        badge: "/static/icon.png?v=" + document.querySelector('meta[name="application-asset-version"]')?.content,
        data: { url },
        tag: url,
      }).catch((err) => {
        warn("showNotification failed; falling back to toast", err);
        toast(`${n.title}${n.body ? ": " + n.body : ""}`, "info");
      });
    });
  }
  return { messaging, reg };
}

// last token persisted so logout can deregister even if SDK never initialised this tab
const _PUSH_TOKEN_KEY = "bh_push_token";

// The SDK script is on the page but never defined `firebase` (an extension or
// policy blocked it). loadScript skips existing tags, so retrying can't help.
const _SDK_UNAVAILABLE = "sdk-unavailable";

async function subscribeToken(cfg) {
  const m = await ensureMessaging(cfg);
  if (!m) return _SDK_UNAVAILABLE;
  const token = await m.messaging.getToken({
    vapidKey: cfg.vapid_key,
    serviceWorkerRegistration: m.reg,
  });
  if (!token) {
    warn("FCM returned no token");
    return false;
  }
  try {
    localStorage.setItem(_PUSH_TOKEN_KEY, token);
  } catch {
    /* storage unavailable; non-fatal */
  }
  await api("/push/subscribe", {
    method: "POST",
    json: { token, platform: "web", user_agent: navigator.userAgent.slice(0, 400) },
  });
  return true;
}

/** Deregister FCM token on logout; must run before /auth/logout (needs the session). Never throws. */
export async function unsubscribeOnLogout() {
  let token = "";
  try {
    token = localStorage.getItem(_PUSH_TOKEN_KEY) || "";
  } catch {
    /* ignore */
  }
  // drop token client-side so the SDK stops auto-refreshing it
  try {
    if (supported() && messagingCache) {
      await messagingCache.deleteToken();
    }
  } catch {
    /* ignore; server unsubscribe below still runs */
  }
  if (token) {
    try {
      await api("/push/unsubscribe", { method: "POST", json: { token } });
    } catch {
      /* logout proceeds regardless */
    }
  }
  try {
    localStorage.removeItem(_PUSH_TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

/** Show a system notification without FCM — corporate networks often block FCM's
 * own push channel outright, but this only needs the page running (tab may be
 * backgrounded/minimised, just not closed). Tag matches FCM's so a duplicate replaces
 * rather than stacks. */
export async function showLocalNotification(title, body, url) {
  try {
    if (!("Notification" in window) || Notification.permission !== "granted") return false;
    const options = {
      body,
      icon: "/static/icon.png?v=" + document.querySelector('meta[name="application-asset-version"]')?.content,
      badge: "/static/icon.png?v=" + document.querySelector('meta[name="application-asset-version"]')?.content,
      data: { url },
      tag: url,
    };
    if ("serviceWorker" in navigator) {
      // Prefer the SW so a click runs its existing notificationclick handler.
      const reg = await navigator.serviceWorker.getRegistration();
      if (reg) {
        await reg.showNotification(title, options);
        return true;
      }
    }
    new Notification(title, options);
    return true;
  } catch (err) {
    warn("local notification failed", err);
    return false;
  }
}

/** Re-register FCM token on every boot (tokens rotate). Never throws or blocks boot. */
export async function initPushOnBoot() {
  try {
    if (!supported()) {
      warn("this browser has no service worker / push support");
      return;
    }
    const cfg = await getConfig();
    if (!cfg?.enabled) {
      warn("web push is not enabled on the server");
      return;
    }
    let permission = Notification.permission;
    if (permission === "default") {
      try {
        permission = await Notification.requestPermission();
      } catch (err) {
        warn("notification permission prompt failed", err);
        return;
      }
    }
    if (permission !== "granted") {
      warn(`notification permission is "${permission}" — allow it in the browser's site settings`);
      return;
    }
    await subscribeWithRetry(cfg);
  } catch (err) {
    warn("push init failed", err);
  }
}

// A corporate/restrictive network can block Google's push endpoints intermittently
// (proxy hiccup, VPN reconnect) without blocking the site itself. Retry with backoff
// instead of giving up after one failed attempt, and again whenever the browser
// regains connectivity.
const _RETRY_DELAYS_MS = [5_000, 15_000, 60_000, 5 * 60_000];
let _retrying = false;

async function subscribeWithRetry(cfg) {
  if (_retrying) return;
  _retrying = true;
  try {
    for (let attempt = 0; ; attempt++) {
      const ok = await subscribeToken(cfg).catch((err) => {
        warn(`subscribe attempt ${attempt + 1} failed`, err);
        return false;
      });
      if (ok === true || ok === _SDK_UNAVAILABLE) return;
      if (attempt >= _RETRY_DELAYS_MS.length) {
        warn("gave up subscribing to push after all retries");
        return;
      }
      await new Promise((r) => setTimeout(r, _RETRY_DELAYS_MS[attempt]));
    }
  } finally {
    _retrying = false;
  }
}

if (typeof window !== "undefined") {
  // Network dropped and came back (VPN reconnect, Wi-Fi flap, proxy recovered):
  // re-attempt subscription if we still don't have a live token.
  window.addEventListener("online", () => {
    let hasToken = true;
    try {
      hasToken = !!localStorage.getItem(_PUSH_TOKEN_KEY);
    } catch {
      /* storage unavailable; assume no token, retry anyway */
      hasToken = false;
    }
    if (hasToken) return;
    void (async () => {
      const cfg = await getConfig();
      if (cfg?.enabled && Notification.permission === "granted") {
        void subscribeWithRetry(cfg);
      }
    })();
  });
}
