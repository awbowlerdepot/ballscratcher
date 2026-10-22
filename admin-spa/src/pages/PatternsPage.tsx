import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { createPattern, listPatterns, uploadPatternSheet } from "../api/client";
import type { PatternSummary } from "../api/types";
import Badge from "../components/Badge";
import Button from "../components/Button";
import type { Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import { useToast } from "../components/Toast";

// Oil pattern catalog (runbook 6ej). Al: "pulling in the patterns and using
// OCR is fine but they need approved before they are added to the live
// catalog. similar pattern to the videos. manual uploads of patterns as the
// first way they get pulled in". Upload a sheet PDF -> it's read with Claude
// vision -> review it on the pattern's page -> approve (it goes live in the
// Partner API) or reject. A pattern can also be typed in by hand.

const TABS: { key: string; label: string }[] = [
  { key: "pending", label: "Pending review" },
  { key: "approved", label: "Approved" },
  { key: "rejected", label: "Rejected" },
  { key: "", label: "All" },
];

export function reviewBadge(p: { review_status: string; published: boolean }) {
  if (p.review_status === "approved") return <Badge tone="ok">{p.published ? "live" : "approved"}</Badge>;
  if (p.review_status === "rejected") return <Badge tone="danger">rejected</Badge>;
  return <Badge tone="pending">pending</Badge>;
}

export function readBadge(status: string) {
  if (status === "queued" || status === "running") return <Badge tone="primary">reading sheet…</Badge>;
  if (status === "failed") return <Badge tone="danger">read failed</Badge>;
  if (status === "ready") return <Badge tone="muted">read</Badge>;
  return null;
}

export default function PatternsPage() {
  const { show } = useToast();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "pending";
  const [search, setSearch] = useState("");
  const [rows, setRows] = useState<PatternSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  function load() {
    setLoading(true);
    listPatterns({ review_status: tab || undefined, search: search.trim() || undefined })
      .then(setRows)
      .catch((err) => show(err instanceof Error ? err.message : "Couldn't load patterns.", "danger"))
      .finally(() => setLoading(false));
  }

  useEffect(load, [tab]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const t = setTimeout(load, 300);
    return () => clearTimeout(t);
  }, [search]); // eslint-disable-line react-hooks/exhaustive-deps

  // Keep the list fresh while a sheet is being read.
  useEffect(() => {
    if (!rows.some((r) => r.extraction_status === "queued" || r.extraction_status === "running")) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [rows]); // eslint-disable-line react-hooks/exhaustive-deps

  async function onFile(file: File | undefined) {
    if (!file) return;
    setUploading(true);
    try {
      const id = await uploadPatternSheet(file);
      show("Uploaded. Reading the sheet now (about 15 seconds).", "ok");
      navigate(`/oil-patterns/${id}`);
    } catch (err) {
      show(err instanceof Error ? err.message : "Upload failed.", "danger");
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function typeOneIn() {
    try {
      const p = await createPattern({ name: "New pattern", category: "sport", source: "manual", steps: [] });
      navigate(`/oil-patterns/${p.id}`);
    } catch (err) {
      show(err instanceof Error ? err.message : "Couldn't create a pattern.", "danger");
    }
  }

  const columns: Column<PatternSummary>[] = [
    {
      key: "name",
      header: "Pattern",
      render: (p) => (
        <Link to={`/oil-patterns/${p.id}`} className="font-medium">
          {p.name}
          {p.short_name && <span className="ml-1.5 text-xs text-ink-500">{p.short_name}</span>}
        </Link>
      ),
    },
    { key: "review", header: "Review", render: (p) => <div className="flex flex-wrap gap-1">{reviewBadge(p)}{readBadge(p.extraction_status)}</div> },
    { key: "category", header: "Category", render: (p) => p.category },
    { key: "length", header: "Length", render: (p) => (p.length_feet != null ? `${p.length_feet} ft` : "--") },
    { key: "volume", header: "Volume", render: (p) => (p.volume_ml != null ? `${p.volume_ml} mL` : "--") },
    { key: "ratio", header: "Ratio", render: (p) => (p.ratio != null ? `${p.ratio}:1` : "--") },
    { key: "source", header: "Source", render: (p) => p.source },
    { key: "updated", header: "Updated", render: (p) => new Date(p.updated_at).toLocaleDateString() },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink-800">Oil Patterns</h1>
          <p className="text-sm text-ink-500">
            Patterns for the Partner API. Upload a pattern sheet (PDF): it's read automatically, then reviewed here before it goes
            live.
          </p>
        </div>
        <div className="flex gap-2">
          <input ref={fileInput} type="file" accept="application/pdf,.pdf" className="hidden" onChange={(e) => onFile(e.target.files?.[0])} />
          <Button variant="primary" onClick={() => fileInput.current?.click()} disabled={uploading}>
            {uploading ? "Uploading…" : "Upload sheet"}
          </Button>
          <Button variant="secondary" onClick={typeOneIn}>
            Type one in
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {TABS.map((t) => (
          <button
            key={t.key || "all"}
            type="button"
            onClick={() => setParams(t.key === "pending" ? {} : { tab: t.key }, { replace: true })}
            className={`rounded-md px-3 py-1.5 text-sm font-medium ${tab === t.key ? "bg-primary text-white" : "bg-ink-100 text-ink-600 hover:bg-ink-200"}`}
          >
            {t.label}
          </button>
        ))}
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search patterns…"
          className="ml-auto w-56 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
        />
      </div>

      <DataTable columns={columns} rows={rows} getRowId={(p) => p.id} loading={loading} emptyMessage="No patterns here yet." />
    </div>
  );
}
