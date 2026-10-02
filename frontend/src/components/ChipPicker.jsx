/** Controlled selectable chip list; class names must match styles.css. */

export default function ChipPicker({ items, selected, onToggle, disabled, id }) {
  const selectedSet = new Set(selected);

  return (
    <div className={`chip-picker${disabled ? " locked" : ""}`} id={id}>
      {items.length === 0 ? (
        <span className="chip-empty">— none available —</span>
      ) : (
        items.map((item) => (
          <span
            key={item.id}
            role="button"
            tabIndex={disabled ? -1 : 0}
            aria-pressed={selectedSet.has(item.id)}
            aria-disabled={disabled || undefined}
            className={`chip${selectedSet.has(item.id) ? " selected" : ""}`}
            data-id={item.id}
            onClick={() => {
              if (!disabled) onToggle(item.id);
            }}
            onKeyDown={(e) => {
              if (!disabled && (e.key === "Enter" || e.key === " ")) {
                e.preventDefault();
                onToggle(item.id);
              }
            }}
          >
            {item.label}
            {item.title ? (
              <>
                {" "}
                <span className="chip-sub">· {item.title}</span>
              </>
            ) : null}
          </span>
        ))
      )}
    </div>
  );
}
