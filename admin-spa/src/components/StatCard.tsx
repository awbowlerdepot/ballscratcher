interface StatCardProps {
  label: string;
  value: string | number;
  tone?: "default" | "warn";
}

// Small single-number KPI tile -- the Dashboard tab's top row (see
// pages/DashboardPage.tsx) renders one of these per key in
// DashboardKpis. "warn" tone is for counts that represent a data gap
// (missing_core/missing_coverstock/missing_skus) -- nonzero there is
// worth a glance of orange, not an error-red (it's backlog, not
// breakage).
export default function StatCard({ label, value, tone = "default" }: StatCardProps) {
  return (
    <div className="rounded-lg border border-ink-200 bg-ink-100 p-4 shadow-sm">
      <div className="text-xs font-medium uppercase tracking-wide text-ink-500">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${tone === "warn" ? "text-warn" : "text-ink-900"}`}>
        {value}
      </div>
    </div>
  );
}
