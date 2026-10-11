/** Placeholder shapes shown while data loads, instead of a "Loading…" line. The shimmer is CSS
 * (styles/motion.css) and is switched off under prefers-reduced-motion. Screen readers get one
 * polite "Loading" status instead of the decorative bars. */
export default function Skeleton({ lines = 3, label = "Loading", className = "" }) {
  const n = Math.max(1, Math.min(12, lines));
  return (
    <div className={`skeleton-block ${className}`.trim()} role="status" aria-live="polite" aria-busy="true">
      <span className="sr-only">{label}</span>
      {Array.from({ length: n }, (_, i) => (
        <span
          key={i}
          className="skeleton"
          aria-hidden="true"
          style={{ width: `${i === n - 1 && n > 1 ? 62 : 100 - ((i * 7) % 21)}%` }}
        />
      ))}
    </div>
  );
}
