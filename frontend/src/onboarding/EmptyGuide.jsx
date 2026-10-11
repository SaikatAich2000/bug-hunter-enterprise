/** Call-to-action row for empty lists: one primary action (when the person may take it) and a link
 * to the matching guide. Sits under the existing empty-state text so that text stays the source of truth. */
import { useOpenGuide } from "./useOpenGuide";

export default function EmptyGuide({ guide, action, hint }) {
  const openGuide = useOpenGuide();
  return (
    <div className="empty-guide">
      {hint && <p className="empty-guide-hint">{hint}</p>}
      <div className="empty-guide-actions">
        {action && (
          <button type="button" className="btn primary" onClick={action.onClick}>{action.label}</button>
        )}
        {guide && (
          <button type="button" className="btn ghost" onClick={() => openGuide(guide)}>Read the guide</button>
        )}
      </div>
    </div>
  );
}
