/** Behavioral tests for the real fetch wrapper in `./api.js`.
 *
 * Exercises the production module (no re-implementation): request-shaping,
 * FastAPI `{detail}` unwrapping, the 204/empty/non-JSON response shapes, the
 * deduped 401 session redirect, and binary-download filename parsing.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let apiMod;
let replaceSpy;

function jsonResponse(body, { status = 200, headers = {} } = {}) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (name) => headers[name] ?? null },
    text: async () => (body === undefined ? "" : JSON.stringify(body)),
    json: async () => body,
    blob: async () => "blob-bytes",
  };
}

function rawResponse(text, { status = 200, headers = {} } = {}) {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: (name) => headers[name] ?? null },
    text: async () => text,
    json: async () => JSON.parse(text),
    blob: async () => "blob-bytes",
  };
}

beforeEach(async () => {
  vi.resetModules();
  replaceSpy = vi.fn();
  vi.stubGlobal("location", {
    pathname: "/",
    search: "?a=1",
    hash: "#frag",
    replace: replaceSpy,
  });
  apiMod = await import("./api.js");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});


describe("request shaping", () => {
  it("exposes the API prefix and a typed error", () => {
    expect(apiMod.API).toBe("/api");
    const err = new apiMod.ApiError("boom", 422);
    expect(err).toBeInstanceOf(Error);
    expect(err.name).toBe("ApiError");
    expect(err.status).toBe(422);
    expect(err.silent).toBe(false);
    expect(new apiMod.ApiError("x", 401, true).silent).toBe(true);
  });

  it("sends cookies and a JSON content type for json payloads", () => {
    const init = apiMod.requestInit({ method: "POST", json: { a: 1 } });
    expect(init.credentials).toBe("include");
    expect(init.method).toBe("POST");
    expect(init.body).toBe('{"a":1}');
    expect(init.headers["Content-Type"]).toBe("application/json");
  });

  it("keeps an explicit body and caller headers when no json is given", () => {
    const init = apiMod.requestInit({ body: "raw", headers: { "X-Test": "1" } });
    expect(init.body).toBe("raw");
    expect(init.headers["Content-Type"]).toBeUndefined();
    expect(init.headers["X-Test"]).toBe("1");
  });

  it("lets an explicit json=null through as a JSON body", () => {
    const init = apiMod.requestInit({ json: null });
    expect(init.body).toBe("null");
    expect(init.headers["Content-Type"]).toBe("application/json");
  });
});


describe("api() responses", () => {
  it("parses a JSON body from the /api-prefixed URL", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ id: 7 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(apiMod.api("/bugs/7")).resolves.toEqual({ id: 7 });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/bugs/7");
    expect(fetchMock.mock.calls[0][1].credentials).toBe("include");
  });

  it("returns null for 204 and for an empty body", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ...jsonResponse(null), status: 204, ok: true })));
    await expect(apiMod.api("/x")).resolves.toBeNull();
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(undefined)));
    await expect(apiMod.api("/x")).resolves.toBeNull();
  });

  it("returns raw text when the body is not JSON", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => rawResponse("plain text")));
    await expect(apiMod.api("/x")).resolves.toBe("plain text");
  });
});

describe("api() error handling", () => {
  it("unwraps a string detail", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "Not allowed" }, { status: 403 })));
    await expect(apiMod.api("/x")).rejects.toMatchObject({
      message: "Not allowed",
      status: 403,
      silent: false,
    });
  });

  it("joins pydantic validation messages", async () => {
    const body = { detail: [{ msg: "field required" }, { msg: "bad type" }] };
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(body, { status: 422 })));
    await expect(apiMod.api("/x")).rejects.toThrow("field required; bad type");
  });

  it("falls back when detail is unusable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: [{ nope: 1 }] }, { status: 418 })));
    await expect(apiMod.api("/x")).rejects.toThrow("Request failed (418)");
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(null, { status: 500 })));
    await expect(apiMod.api("/x")).rejects.toThrow("Request failed (500)");
  });

  it("redirects to login once on 401 and throws a silent error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "nope" }, { status: 401 })));
    await expect(apiMod.api("/x")).rejects.toMatchObject({ status: 401, silent: true });
    expect(replaceSpy).toHaveBeenCalledTimes(1);
    expect(replaceSpy.mock.calls[0][0]).toBe("/login.html?next=%2F%3Fa%3D1%23frag");
    // A second 401 in the same session must not stack a second navigation.
    await expect(apiMod.api("/y")).rejects.toBeInstanceOf(apiMod.ApiError);
    expect(replaceSpy).toHaveBeenCalledTimes(1);
  });

  it("does not redirect when fetch itself rejects", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network down"); }));
    await expect(apiMod.api("/x")).rejects.toBeInstanceOf(TypeError);
    expect(replaceSpy).not.toHaveBeenCalled();
  });
});


describe("apiBlob() downloads", () => {
  it("decodes an RFC 5987 filename* disposition", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(null, {
      headers: { "Content-Disposition": "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf" },
    })));
    await expect(apiMod.apiBlob("/export")).resolves.toEqual({
      blob: "blob-bytes",
      filename: "résumé.pdf",
    });
  });

  it("prefers filename* over a plain filename", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(null, {
      headers: {
        "Content-Disposition": "attachment; filename=\"fallback.csv\"; filename*=UTF-8''real.csv",
      },
    })));
    await expect(apiMod.apiBlob("/export")).resolves.toMatchObject({ filename: "real.csv" });
  });

  it("reads a quoted plain filename and strips path separators", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(null, {
      headers: { "Content-Disposition": 'attachment; filename="../etc/passwd"' },
    })));
    await expect(apiMod.apiBlob("/export")).resolves.toMatchObject({
      filename: ".._etc_passwd",
    });
  });

  it("returns a null filename when the header is absent", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(null)));
    await expect(apiMod.apiBlob("/export")).resolves.toEqual({
      blob: "blob-bytes",
      filename: null,
    });
  });

  it("surfaces a JSON error detail and the silent 401 redirect", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "Export too large" }, { status: 413 })));
    await expect(apiMod.apiBlob("/export")).rejects.toThrow("Export too large");
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({}, { status: 401 })));
    await expect(apiMod.apiBlob("/export")).rejects.toMatchObject({ status: 401, silent: true });
    expect(replaceSpy).toHaveBeenCalledTimes(1);
  });

  it("keeps the status fallback when the error body is binary", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      status: 500,
      ok: false,
      headers: { get: () => null },
      json: async () => { throw new SyntaxError("not json"); },
    })));
    await expect(apiMod.apiBlob("/export")).rejects.toThrow("Request failed (500)");
  });
});
