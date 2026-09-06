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
import { getCatalogDailyMovementHistory, getDashboardSummary } from "../api/client";
import type { CatalogDailyMovementHistoryPoint, DashboardSummary } from "../api/types";
import Card from "../components/Card";
import CatalogDailyMovementChart from "../components/charts/CatalogDailyMovementChart";
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
  // Loaded separately from the rest of the dashboard -- its own endpoint,
  // and there's no reason a slow catalog-history query should block the
  // KPI cards/Top 10 tables from rendering. null while loading, [] once
  // loaded with no history yet.
  const [dailyMovementHistory, setDailyMovementHistory] = useState<CatalogDailyMovementHistoryPoint[] | null>(null);

  useEffect(() => {
    getDashboardSummary()
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load dashboard."));
    getCatalogDailyMovementHistory()
      .then((r) => setDailyMovementHistory(r.items))
      .catch(() => setDailyMovementHistory([]));
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
          <StatCard label="Catalog Avg Daily Movement" value={kpis.total_catalog_daily_movement.toFixed(1)} />
        </div>
      </div>

      <Card title="Avg Daily Movement by brand">
        <div className="chart-wrap">
          <Bar
            data={{
              labels: data.daily_movement_by_brand.map((b) => b.brand_name),
              datasets: [
                {
                  label: "Total Avg Daily Movement",
                  data: data.daily_movement_by_brand.map((b) => b.total_daily_movement),
                  backgroundColor: "#2563eb",
                },
              ],
            }}
            options={{ responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } }}
          />
        </div>
      </Card>

      {dailyMovementHistory && dailyMovementHistory.length > 0 && (
        <Card title="Avg Daily Movement over time">
          <CatalogDailyMovementChart items={dailyMovementHistory} />
        </Card>
      )}

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

        <Card title="Top 10 by Avg Daily Movement">
          <DataTable
            columns={[
              { key: "name", header: "Ball", render: (r) => `${r.brand_name} ${r.name}` },
              {
                key: "total_daily_movement",
                header: "Avg Daily Movement",
                render: (r) => r.total_daily_movement.toFixed(1),
              },
            ]}
            rows={data.top_daily_movement}
            getRowId={(r) => r.id}
          />
        </Card>

        <Card title="Top growing Avg Daily Movement">
          <DataTable
            columns={[
              { key: "name", header: "Ball", render: (r) => `${r.brand_name} ${r.name}` },
              {
                key: "delta_daily_movement",
                header: "Δ Avg Daily Movement",
                render: (r) => `+${r.delta_daily_movement.toFixed(1)}`,
              },
            ]}
            rows={data.top_growing_daily_movement}
            getRowId={(r) => r.product_id}
          />
        </Card>

        <Card title="Top shrinking Avg Daily Movement">
          <DataTable
            columns={[
              { key: "name", header: "Ball", render: (r) => `${r.brand_name} ${r.name}` },
              {
                key: "delta_daily_movement",
                header: "Δ Avg Daily Movement",
                render: (r) => r.delta_daily_movement.toFixed(1),
              },
            ]}
            rows={data.top_shrinking_daily_movement}
            getRowId={(r) => r.product_id}
          />
        </Card>
      </div>
    </div>
  );
}
