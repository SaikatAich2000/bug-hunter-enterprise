// Organization branding: the accent colour tints filled surfaces (primary buttons, active tabs).
// Text accents keep the theme's own colours, so contrast never depends on the chosen colour.

const FILL_VARS = ["--accent-fill", "--on-accent-fill", "--accent-grad", "--accent-grad-hover", "--accent-soft", "--accent-glow"];

/** [r, g, b] of "#rgb" or "#rrggbb", or null. */
export function parseHex(hex) {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(String(hex || "").trim());
  if (!m) return null;
  const h = m[1].length === 3 ? [...m[1]].map((c) => c + c).join("") : m[1];
  return [0, 2, 4].map((i) => Number.parseInt(h.slice(i, i + 2), 16));
}

function luminance([r, g, b]) {
  const lin = (v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

/** WCAG contrast ratio of two [r, g, b] colours. */
export function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** Black or white, whichever reads better on `rgb`. */
export function readableOn(rgb) {
  return contrast(rgb, [0, 0, 0]) >= contrast(rgb, [255, 255, 255]) ? "#000000" : "#ffffff";
}

function shade([r, g, b], factor) {
  const to = (v) => Math.max(0, Math.min(255, Math.round(v * factor)));
  return `rgb(${to(r)}, ${to(g)}, ${to(b)})`;
}

/** The CSS variables for an accent colour, or null when it is not a valid hex colour. */
export function accentVars(hex) {
  const rgb = parseHex(hex);
  if (!rgb) return null;
  const base = `rgb(${rgb.join(", ")})`;
  return {
    "--accent-fill": base,
    "--on-accent-fill": readableOn(rgb),
    "--accent-grad": `linear-gradient(135deg, ${shade(rgb, 1.1)} 0%, ${base} 60%, ${shade(rgb, 0.85)} 100%)`,
    "--accent-grad-hover": `linear-gradient(135deg, ${base} 0%, ${shade(rgb, 0.9)} 60%, ${shade(rgb, 0.75)} 100%)`,
    "--accent-soft": `rgba(${rgb.join(", ")}, 0.14)`,
    "--accent-glow": `rgba(${rgb.join(", ")}, 0.3)`,
  };
}

/** Apply (or clear, for a null/invalid colour) the organization's accent on the page. */
export function applyOrgAccent(hex, root = document.documentElement) {
  const vars = accentVars(hex);
  for (const name of FILL_VARS) {
    if (vars) root.style.setProperty(name, vars[name]);
    else root.style.removeProperty(name);
  }
}
