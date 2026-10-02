/** Behavioral tests for the real web-push module in `./push.js`.
 *
 * Exercises the production module with mocked transport boundaries only
 * (`api`, `toast`, the Firebase compat SDK and the Notification API) so the
 * boot/retry/logout logic itself is under test: subscription on boot,
 * permission handling, token persistence, logout deregistration and the
 * service-worker-first local notification fallback.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let apiMock;
let push;
let warnSpy;

const CONFIG = {
  enabled: true,
  api_key: "k",
  auth_domain: "a",
  project_id: "p",
  messaging_sender_id: "s",
  app_id: "app",
  vapid_key: "vapid",
};

function installScript(src) {
  const el = document.createElement("script");
  el.src = src;
  document.head.appendChild(el);
  return el;
}

function grantNotifications(permission = "granted") {
  const NotificationStub = vi.fn();
  NotificationStub.permission = permission;
  NotificationStub.requestPermission = vi.fn(async () => permission);
  vi.stubGlobal("Notification", NotificationStub);
  Object.defineProperty(window, "Notification", {
    value: NotificationStub,
    configurable: true,
    writable: true,
  });
  return NotificationStub;
}

function enablePushSupport({ register, getRegistration } = {}) {
  const reg = register ?? { showNotification: vi.fn(async () => undefined) };
  Object.defineProperty(navigator, "serviceWorker", {
    value: {
      register: vi.fn(async () => reg),
      getRegistration: getRegistration ?? vi.fn(async () => reg),
    },
    configurable: true,
    writable: true,
  });
  Object.defineProperty(window, "PushManager", {
    value: function PushManager() {},
    configurable: true,
    writable: true,
  });
  return reg;
}

beforeEach(async () => {
  vi.resetModules();
  localStorage.clear();
  warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
  apiMock = vi.fn(async () => CONFIG);
  vi.doMock("./api", () => ({ api: apiMock, ApiError: class ApiError extends Error {} }));
  vi.doMock("./toast", () => ({ toast: vi.fn(), toastError: vi.fn() }));
  push = await import("./push.js");
});

describe("logout deregistration", () => {
  it("drops the SDK token, unsubscribes the stored token and clears storage", async () => {
    enablePushSupport();
    localStorage.setItem("bh_push_token", "tok-1");
    const fb = { apps: [{}], messaging: () => ({ deleteToken: vi.fn(async () => true) }) };
    vi.stubGlobal("firebase", fb);

    await push.unsubscribeOnLogout();

    expect(apiMock).toHaveBeenCalledWith("/push/unsubscribe", {
      method: "POST",
      json: { token: "tok-1" },
    });
    expect(localStorage.getItem("bh_push_token")).toBeNull();
  });

  it("skips the network call when no token was ever stored", async () => {
    enablePushSupport();
    await push.unsubscribeOnLogout();
    expect(apiMock).not.toHaveBeenCalled();
    expect(localStorage.getItem("bh_push_token")).toBeNull();
  });

  it("still POSTs the unsubscribe when the SDK deleteToken rejects", async () => {
    enablePushSupport();
    localStorage.setItem("bh_push_token", "tok-2");
    vi.stubGlobal("firebase", {
      apps: [{}],
      messaging: () => ({ deleteToken: vi.fn(async () => { throw new Error("sdk down"); }) }),
    });

    await push.unsubscribeOnLogout();

    expect(apiMock).toHaveBeenCalledWith("/push/unsubscribe", {
      method: "POST",
      json: { token: "tok-2" },
    });
  });

  it("never throws when the unsubscribe request fails", async () => {
    apiMock.mockRejectedValueOnce(new Error("offline"));
    localStorage.setItem("bh_push_token", "tok-3");
    await expect(push.unsubscribeOnLogout()).resolves.toBeUndefined();
    expect(localStorage.getItem("bh_push_token")).toBeNull();
  });
});

describe("local notifications", () => {
  it("refuses to notify without granted permission", async () => {
    grantNotifications("denied");
    await expect(push.showLocalNotification("T", "B", "/x")).resolves.toBe(false);
  });

  it("prefers the service worker registration so clicks route through it", async () => {
    grantNotifications("granted");
    const reg = enablePushSupport();
    await expect(push.showLocalNotification("Title", "Body", "/bugs/1")).resolves.toBe(true);
    expect(reg.showNotification).toHaveBeenCalledTimes(1);
    expect(reg.showNotification.mock.calls[0][0]).toBe("Title");
    expect(reg.showNotification.mock.calls[0][1]).toMatchObject({ data: { url: "/bugs/1" } });
  });

  it("falls back to a plain Notification when no registration exists", async () => {
    const NotificationStub = grantNotifications("granted");
    enablePushSupport({ getRegistration: vi.fn(async () => undefined) });
    await expect(push.showLocalNotification("Title", "Body", "/")).resolves.toBe(true);
    expect(NotificationStub).toHaveBeenCalledWith("Title", expect.objectContaining({ tag: "/" }));
  });

  it("reports failure instead of throwing when showing the notification errors", async () => {
    grantNotifications("granted");
    enablePushSupport({ getRegistration: vi.fn(async () => { throw new Error("nope"); }) });
    await expect(push.showLocalNotification("T", "B", "/")).resolves.toBe(false);
    expect(warnSpy).toHaveBeenCalled();
  });
});

describe("boot subscription", () => {
  it("bails out on a browser without service worker / push support", async () => {
    await push.initPushOnBoot();
    expect(apiMock).not.toHaveBeenCalled();
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining("no service worker / push support"),
      "",
    );
  });

  it("bails out when the server reports push disabled", async () => {
    enablePushSupport();
    grantNotifications("granted");
    apiMock.mockResolvedValueOnce({ enabled: false });
    await push.initPushOnBoot();
    expect(apiMock).toHaveBeenCalledWith("/push/config");
    expect(apiMock).toHaveBeenCalledTimes(1);
  });

  it("survives an unreachable /push/config without throwing", async () => {
    enablePushSupport();
    grantNotifications("granted");
    apiMock.mockRejectedValueOnce(new Error("500"));
    await expect(push.initPushOnBoot()).resolves.toBeUndefined();
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining("could not load /push/config"),
      expect.anything(),
    );
  });

  it("stops when the user denies the permission prompt", async () => {
    enablePushSupport();
    const NotificationStub = grantNotifications("default");
    NotificationStub.requestPermission = vi.fn(async () => "denied");
    await push.initPushOnBoot();
    expect(NotificationStub.requestPermission).toHaveBeenCalledTimes(1);
    expect(apiMock).toHaveBeenCalledTimes(1); // only /push/config
  });

  it("registers, subscribes and persists the FCM token", async () => {
    const reg = enablePushSupport();
    grantNotifications("granted");
    installScript("/static/vendor/firebase-app-compat.js");
    installScript("/static/vendor/firebase-messaging-compat.js");
    const getToken = vi.fn(async () => "fcm-token");
    const initializeApp = vi.fn();
    vi.stubGlobal("firebase", {
      apps: [],
      initializeApp,
      messaging: () => ({ getToken, onMessage: vi.fn() }),
    });

    await push.initPushOnBoot();

    expect(initializeApp).toHaveBeenCalledWith({
      apiKey: "k",
      authDomain: "a",
      projectId: "p",
      messagingSenderId: "s",
      appId: "app",
    });
    expect(navigator.serviceWorker.register).toHaveBeenCalledWith("/firebase-messaging-sw.js");
    expect(getToken).toHaveBeenCalledWith({ vapidKey: "vapid", serviceWorkerRegistration: reg });
    expect(localStorage.getItem("bh_push_token")).toBe("fcm-token");
    expect(apiMock).toHaveBeenCalledWith("/push/subscribe", {
      method: "POST",
      json: { token: "fcm-token", platform: "web", user_agent: navigator.userAgent.slice(0, 400) },
    });
  });

  it("does not re-initialise Firebase when an app already exists", async () => {
    enablePushSupport();
    grantNotifications("granted");
    installScript("/static/vendor/firebase-app-compat.js");
    installScript("/static/vendor/firebase-messaging-compat.js");
    const initializeApp = vi.fn();
    vi.stubGlobal("firebase", {
      apps: [{}],
      initializeApp,
      messaging: () => ({ getToken: vi.fn(async () => "t"), onMessage: vi.fn() }),
    });

    await push.initPushOnBoot();

    expect(initializeApp).not.toHaveBeenCalled();
  });

  it("stops cleanly when the FCM SDK is blocked by the network", async () => {
    enablePushSupport();
    grantNotifications("granted");
    installScript("/static/vendor/firebase-app-compat.js");
    installScript("/static/vendor/firebase-messaging-compat.js");
    vi.stubGlobal("firebase", undefined);

    await push.initPushOnBoot();

    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining("Firebase SDK did not load"),
      "",
    );
    expect(apiMock).toHaveBeenCalledTimes(1);
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.doUnmock("./api");
  vi.doUnmock("./toast");
  vi.restoreAllMocks();
  document.querySelectorAll("script").forEach((el) => el.remove());
});
