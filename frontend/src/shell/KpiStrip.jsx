// KPI strip — tile numbers + click-to-filter toggle. List and analytics views only.
import { useApp, KPI_FILTER_MAP } from "../state/AppContext";

function arraysEqualAsSets(a, b) {
  if (a.length !== b.length) return false;
  const sa = new Set(a);
  for (const x of b) if (!sa.has(x)) return false;
  return true;
}

const TILES = [
  { key: "total", numId: "kpiBugs", numClass: "kpi-num", label: "Total", aria: "Show all bugs" },
  { key: "open", numId: "kpiOpen", numClass: "kpi-num kpi-open", label: "Open", aria: "Filter to open bugs" },
  { key: "resolved", numId: "kpiResolved", numClass: "kpi-num kpi-resolved", label: "Resolved", aria: "Filter to resolved bugs" },
  { key: "closed", numId: "kpiClosed", numClass: "kpi-num kpi-closed", label: "Closed", aria: "Filter to closed bugs" },
  { key: "resolve_later", numId: "kpiResolveLater", numClass: "kpi-num kpi-later", label: "Resolve Later", aria: "Filter to resolve-later bugs" },
];

export default function KpiStrip() {
  const { view, stats, filters, setFilters } = useApp();

  const show = view === "list" || view === "analytics";

  const numbers = {
    total: stats?.bugs ?? 0,
    open: stats?.open ?? 0,
    resolved: stats?.resolved ?? 0,
    closed: stats?.closed ?? 0,
    resolve_later: stats?.resolve_later ?? 0,
  };

  const isActive = (key) => {
    const target = KPI_FILTER_MAP[key];
    if (!target) return false;
    return key === "total"
      ? filters.status.length === 0
      : arraysEqualAsSets(filters.status, target);
  };

  // Active tile toggles the filter off; intentionally no navigation.
  const onKpiClick = (key) => {
    const target = KPI_FILTER_MAP[key];
    if (!target) return;
    const next =
      arraysEqualAsSets(filters.status, target) && target.length > 0 ? [] : [...target];
    setFilters((prev) => ({ ...prev, status: next }));
  };

  return (
    <section className="kpi-strip" id="kpiStrip" style={{ display: show ? undefined : "none" }}>
      {TILES.map((t) => (
        <button
          key={t.key}
          type="button"
          className={`kpi${isActive(t.key) ? " active" : ""}`}
          data-kpi={t.key}
          aria-label={t.aria}
          onClick={() => onKpiClick(t.key)}
        >
          <div className={t.numClass} id={t.numId}>
            {numbers[t.key] ?? 0}
          </div>
          <div className="kpi-label">{t.label}</div>
        </button>
      ))}
    </section>
  );
}
