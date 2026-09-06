import {
  BarElement,
  CategoryScale,
  Chart as ChartJS,
  Legend,
  LinearScale,
  Tooltip,
} from "chart.js";
import { useEffect, useState } from "react";
import { Bar } from "react-chartjs-2";
import { getDashboardSummary } from "../api/client";
import type { DashboardSummary } from "../api/types";
import Card from "../components/Card";
import DataTable from "../components/DataTable";
import StatCard from "../components/StatCard";

ChartJS.register(CategoryScale, LinearScale, BarElement, Tooltip, Legend);

// Elevated to the app's landing page per Al's "dashboards more
// prominent" priority (see the admin-SPA design doc in
// DEPLOY_RUNBOOK.md) -- this is "/" in App.tsx's routes, not a tab you
// have to click into. Ports the same GET /admin/dashboard payload the
// old admin-site/index.html Dashboard tab renders (see
// src/admin_api/service.py's get_dashboard_summary), just with Chart.js
// via react-chartjs-2 instead of the old page's raw Chart.js DOM calls.
export default function DashboardPage() {
  const [data, setData] = useState<DashboardSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getDashboardSummary()
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load dashboard."));
  }, []);

  if (error) {
    return <div className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{error}</div>;
  }
  if (!data) {
    return <div className="text-sm text-ink-500">Loading dashboard…</div>;
  }

  const { kpis } = data;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="mb-3 text-xl font-semibold text-ink-800">Dashboard</h1>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          <StatCard label="Total products" value={kpis.total_products} />
          <StatCard label="Current" value={kpis.current_products} />
          <StatCard label="Retired" value={kpis.retired_products} />
          <StatCard label="Missing core" value={kpis.missing_core} tone={kpis.missing_core > 0 ? "warn" : "default"} />
          <StatCard
            label="Missing coverstock"
            value={kpis.missing_coverstock}
            tone={kpis.missing_coverstock > 0 ? "warn" : "default"}
          />
          <StatCard label="Missing SKUs" value={kpis.missing_skus} tone={kpis.missing_skus > 0 ? "warn" : "default"} />
          <StatCard label="With video" value={kpis.products_with_video} />
          <StatCard label="With price tracking" value={kpis.products_with_price_tracking} />
          <StatCard label="Catalog ADU" value={kpis.total_catalog_adu.toFixed(1)} />
        </div>
      </div>

      <Card title="ADU by brand">
        <div className="chart-wrap">
          <Bar
            data={{
              labels: data.adu_by_brand.map((b) => b.brand_name),
              datasets: [
                {
                  label: "Total ADU",
                  data: data.adu_by_brand.map((b) => b.total_adu),
                  backgroundColor: "#2563eb",
                },
              ],
            }}
            options={{ responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } }}
          />
        </div>
      </Card>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card title="Top 10 by popularity">
          <DataTable
            columns={[
              { key: "name", header: "Ball", render: (r) => `${r.brand_name} ${r.name}` },
              { key: "popularity_score", header: "Score", render: (r) => r.popularity_score.toFixed(1) },
            ]}
            rows={data.top_popularity}
            getRowId={(r) => r.id}
          />
        </Card>

        <Card title="Top 10 by ADU">
          <DataTable
            columns={[
              { key: "name", header: "Ball", render: (r) => `${r.brand_name} ${r.name}` },
              { key: "total_adu", header: "ADU", render: (r) => r.total_adu.toFixed(1) },
            ]}
            rows={data.top_adu}
            getRowId={(r) => r.id}
          />
        </Card>

        <Card title="Top growing ADU">
          <DataTable
            columns={[
              { key: "name", header: "Ball", render: (r) => `${r.brand_name} ${r.name}` },
              { key: "delta_adu", header: "Δ ADU", render: (r) => `+${r.delta_adu.toFixed(1)}` },
            ]}
            rows={data.top_growing_adu}
            getRowId={(r) => r.product_id}
          />
        </Card>

        <Card title="Top shrinking ADU">
          <DataTable
            columns={[
              { key: "name", header: "Ball", render: (r) => `${r.brand_name} ${r.name}` },
              { key: "delta_adu", header: "Δ ADU", render: (r) => r.delta_adu.toFixed(1) },
            ]}
            rows={data.top_shrinking_adu}
            getRowId={(r) => r.product_id}
          />
        </Card>
      </div>
    </div>
  );
}
