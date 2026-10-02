import { describe, expect, it } from "vitest";

import { parseDeepLink } from "./deepLink.js";
import { notificationUrl } from "../state/AppContext.jsx";

describe("parseDeepLink", () => {
  it("reads bug and event links", () => {
    expect(parseDeepLink("#bug=12")).toEqual({ kind: "bug", id: 12 });
    expect(parseDeepLink("#event=3")).toEqual({ kind: "event", id: 3 });
  });

  it.each(["", "#", "#bug=", "#bug=0", "#bug=-1", "#bug=1.5", "#bug=12abc", "#bug=12&x=1",
    "#user=4", "bug=4", "#bug=99999999999"])("ignores %j", (hash) => {
    expect(parseDeepLink(hash)).toBeNull();
  });

  it("round-trips the URLs the app itself generates", () => {
    expect(parseDeepLink(new URL(notificationUrl({ bug_id: 7 }), "http://x").hash))
      .toEqual({ kind: "bug", id: 7 });
    expect(parseDeepLink(new URL(notificationUrl({ event_id: 9 }), "http://x").hash))
      .toEqual({ kind: "event", id: 9 });
  });
});
