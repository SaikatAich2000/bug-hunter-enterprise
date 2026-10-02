import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/toast", () => ({ toastError: vi.fn(), toast: vi.fn() }));

import { toastError } from "../../lib/toast";
import { useLatestLoad } from "./useAgileProject";

let root;
let container;
const seen = [];

function Probe({ load, dep }) {
  const { data } = useLatestLoad(() => load(dep), [dep]);
  seen.push([dep, data]);
  return <span>{data ?? "none"}</span>;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

async function flush() {
  await act(async () => { await Promise.resolve(); });
}

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  seen.length = 0;
  vi.clearAllMocks();
});

function mount(element) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(element));
}

describe("useLatestLoad", () => {
  it("never renders data loaded for other inputs", async () => {
    const answers = { a: deferred(), b: deferred() };
    const load = (dep) => answers[dep].promise;
    mount(<Probe load={load} dep="a" />);
    answers.a.resolve("data for a");
    await flush();
    expect(container.textContent).toBe("data for a");

    act(() => root.render(<Probe load={load} dep="b" />));
    // Until b answers, nothing is shown: never a's data under b.
    expect(seen.filter(([dep, data]) => dep === "b" && data === "data for a")).toEqual([]);
    expect(container.textContent).toBe("none");
    answers.b.resolve("data for b");
    await flush();
    expect(container.textContent).toBe("data for b");
  });

  it("lets the newest request win when answers arrive out of order", async () => {
    const answers = { a: deferred(), b: deferred() };
    const load = (dep) => answers[dep].promise;
    mount(<Probe load={load} dep="a" />);
    act(() => root.render(<Probe load={load} dep="b" />));
    answers.b.resolve("b");
    await flush();
    answers.a.resolve("a (late)");
    await flush();
    expect(container.textContent).toBe("b");
  });

  it("keeps the current view when a refresh fails, and reloads on agile:refresh", async () => {
    let calls = 0;
    const load = () => {
      calls += 1;
      return calls === 2 ? Promise.reject(new Error("offline")) : Promise.resolve(`answer ${calls}`);
    };
    mount(<Probe load={load} dep="x" />);
    await flush();
    expect(container.textContent).toBe("answer 1");
    await act(async () => { window.dispatchEvent(new Event("agile:refresh")); });
    await flush();
    expect(container.textContent).toBe("answer 1");
    expect(toastError).toHaveBeenCalledTimes(1);
    await act(async () => { window.dispatchEvent(new Event("agile:refresh")); });
    await flush();
    expect(container.textContent).toBe("answer 3");
  });
});
