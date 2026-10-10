/**
 * A background poll that never overlaps itself, skips work while the tab is hidden, runs at
 * once when the tab becomes visible again, and backs off (doubling up to `maxMs`) while the
 * task keeps failing. `onError(err, failures)` and `onRecover()` let the caller tell the user
 * once instead of on every tick. Returns a stop function.
 */
export function startPoller(task, intervalMs, { maxMs = intervalMs * 8, onError, onRecover } = {}) {
  let timer = null;
  let running = false;
  let stopped = false;
  let failures = 0;

  const schedule = () => {
    clearTimeout(timer);
    if (!stopped) timer = setTimeout(run, failures ? Math.min(intervalMs * 2 ** failures, maxMs) : intervalMs);
  };

  async function run() {
    if (stopped || running) return;
    if (document.hidden) {
      schedule();
      return;
    }
    running = true;
    try {
      await task();
      if (failures) onRecover?.();
      failures = 0;
    } catch (err) {
      failures += 1;
      onError?.(err, failures);
    } finally {
      running = false;
      schedule();
    }
  }

  const onVisibility = () => {
    if (!document.hidden) void run();
  };
  document.addEventListener("visibilitychange", onVisibility);
  schedule();
  return () => {
    stopped = true;
    clearTimeout(timer);
    document.removeEventListener("visibilitychange", onVisibility);
  };
}
