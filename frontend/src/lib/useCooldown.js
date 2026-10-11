import { useCallback, useEffect, useState } from "react";

/** Seconds left of a cooldown that `start()` begins (e.g. before another "resend" is allowed). */
export function useCooldown(seconds) {
  const [left, setLeft] = useState(0);
  useEffect(() => {
    if (left <= 0) return undefined;
    const timer = setTimeout(() => setLeft((n) => n - 1), 1000);
    return () => clearTimeout(timer);
  }, [left]);
  const start = useCallback(() => setLeft(seconds), [seconds]);
  return [left, start];
}
