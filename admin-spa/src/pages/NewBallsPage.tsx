import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { listNewBallsForSocial } from "../api/client";
import type { NewBallForSocial } from "../api/types";
import Badge from "../components/Badge";
import type { Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import { useToast } from "../components/Toast";

// New balls (runbook 6dd) -- recently discovered current balls with no
// approved article yet, so announcement posts + images can go out as soon
// as a ball is announced (Al: "so we can get social out as soon as they are
// announced before there are videos to generate an article"). Each row
// opens the ball's Social tab, where the announcement is made.

const WINDOWS = [30, 60, 90];

export default function NewBallsPage() {
  const { show } = useToast();
  const [days, setDays] = useState(60);
  const [rows, setRows] = useState<NewBallForSocial[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    listNewBallsForSocial(days)
      .then(setRows)
      .catch((err) => show(err instanceof Error ? err.message : "Couldn't load new balls.", "danger"))
      .finally(() => setLoading(false));
  }, [days, show]);

  const columns: Column<NewBallForSocial>[] = [
    {
      key: "ball",
      header: "Ball",
      render: (r) => (
        <Link to={`/products/${r.product_id}?tab=social`} className="flex items-center gap-2 font-medium">
          {r.image_url ? <img src={r.image_url} alt="" className="h-10 w-10 rounded object-cover" /> : null}
          <span>
            {r.brand} {r.name}
          </span>
        </Link>
      ),
    },
    {
      key: "discovered",
      header: "Discovered",
      render: (r) => new Date(r.created_at).toLocaleDateString(),
    },
    {
      key: "description",
      header: "Description",
      render: (r) => (r.has_description ? <Badge tone="ok">yes</Badge> : <Badge tone="muted">not yet</Badge>),
    },
    {
      key: "posts",
      header: "Posts",
      render: (r) => (r.has_posts ? <Badge tone="ok">written</Badge> : <span className="text-xs text-ink-400">--</span>),
    },
    {
      key: "images",
      header: "Images",
      render: (r) =>
        r.image_status === "ready" ? (
          <Badge tone="ok">ready</Badge>
        ) : r.image_status === "generating" ? (
          <Badge tone="pending">making</Badge>
        ) : r.image_status === "failed" ? (
          <Badge tone="danger">failed</Badge>
        ) : (
          <span className="text-xs text-ink-400">--</span>
        ),
    },
    {
      key: "article",
      header: "Article",
      render: (r) => (r.article_status ? <Badge tone="muted">{r.article_status}</Badge> : <span className="text-xs text-ink-400">none</span>),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink-900">New Balls</h1>
          <p className="text-sm text-ink-500">
            Recently discovered balls without an approved article. Open one to write announcement posts and make
            images from the manufacturer&rsquo;s description.
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm text-ink-600">
          Discovered in the last
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="rounded-md border border-ink-300 px-2 py-1 text-sm"
          >
            {WINDOWS.map((d) => (
              <option key={d} value={d}>
                {d} days
              </option>
            ))}
          </select>
        </label>
      </div>
      <DataTable
        columns={columns}
        rows={rows}
        getRowId={(r) => r.product_id}
        loading={loading}
        emptyMessage="No new balls in this window."
      />
    </div>
  );
}
