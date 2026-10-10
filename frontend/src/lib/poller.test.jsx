import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { startPoller } from "./poller";

describe("startPoller", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("never runs the task twice at once", async () => {
    let release;
    const task = vi.fn(() => new Promise((r) => { release = r; }));
    const stop = startPoller(task, 1000);
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(5000);
    expect(task).toHaveBeenCalledTimes(1);
    release();
    await vi.advanceTimersByTimeAsync(1000);
    expect(task).toHaveBeenCalledTimes(2);
    stop();
  });

  it("backs off while failing and reports once per failure streak", async () => {
    const onError = vi.fn();
    const onRecover = vi.fn();
    let fail = true;
    const task = vi.fn(async () => { if (fail) throw new Error("down"); });
    const stop = startPoller(task, 1000, { maxMs: 4000, onError, onRecover });
    await vi.advanceTimersByTimeAsync(1000); // 1st try fails
    await vi.advanceTimersByTimeAsync(1999);
    expect(task).toHaveBeenCalledTimes(1); // next try waits 2 s
    await vi.advanceTimersByTimeAsync(1);
    expect(task).toHaveBeenCalledTimes(2);
    fail = false;
    await vi.advanceTimersByTimeAsync(4000);
    expect(task).toHaveBeenCalledTimes(3);
    expect(onError.mock.calls.map((c) => c[1])).toEqual([1, 2]);
    expect(onRecover).toHaveBeenCalledTimes(1);
    stop();
  });

  it("stops cleanly", async () => {
    const task = vi.fn(async () => {});
    const stop = startPoller(task, 1000);
    stop();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(task).not.toHaveBeenCalled();
  });
});
