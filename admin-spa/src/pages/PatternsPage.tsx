import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { createPattern, listPatterns, uploadPatternSheet, type PatternListResult } from "../api/client";
import { PATTERN_CATEGORIES, PATTERN_SOURCES } from "../api/types";
import type { PatternSummary } from "../api/types";
import Badge from "../components/Badge";
import Button from "../components/Button";
import type { Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import Pagination from "../components/Pagination";
import { useToast } from "../components/Toast";

// Oil pattern catalog (runbook 6ej). Al: "pulling in the patterns and using
// OCR is fine but they need approved before they are added to the live
// catalog. similar pattern to the videos. manual uploads of patterns as the
// first way they get pulled in". Upload a sheet PDF -> it's read with Claude
// vision -> review it on the pattern's page -> approve (it goes live in the
// Partner API) or reject. A pattern can also be typed in by hand.

// Filters (runbook 6ev). Al: "in the bowleriq admin can we add the proper
// filtering for these" once the Kegel import brought ~1,340 patterns in.
// All server-side and kept in the URL, so a filtered view can be
// bookmarked or shared; bands match the kiosk's chips.
const LIMIT = 100;
const FILTERS: { key: string; label: string; options: [string, string][] }[] = [
  { key: "source", label: "Source", options: PATTERN_SOURCES.map((s) => [s, s === "kegel" ? "Kegel library" : s === "upload" ? "Uploaded sheet" : s]) },
  { key: "category", label: "Type", options: PATTERN_CATEGORIES.map((c) => [c, c]) },
  { key: "length", label: "Length", options: [["short", "Short (≤38 ft)"], ["medium", "Medium (39–44 ft)"], ["long", "Long (45+ ft)"]] },
  { key: "oil", label: "Oil", options: [["light", "Light (<22 mL)"], ["medium", "Medium (22–28 mL)"], ["heavy", "Heavy (>28 mL)"]] },
  { key: "ratio", label: "Ratio", options: [["flat", "Flat (<4:1)"], ["medium", "Medium (4–7:1)"], ["house", "House (7:1+)"]] },
  {
    key: "flag",
    label: "Why pending",
    options: [
      ["forward_drop", "Forward drop brush"],
      ["raise_brush", "Raise brush at end"],
      ["check_errors", "Check errors"],
      ["read_failed", "Sheet read failed"],
      ["no_note", "Waiting on review (no note)"],
    ],
  },
];
const SORTS: [string, string][] = [
  ["attention", "Needs attention first"],
  ["updated", "Recently updated"],
  ["name", "Name A–Z"],
  ["length", "Longest"],
  ["volume", "Most oil"],
  ["ratio", "Highest ratio"],
];

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
  const [search, setSearch] = useState(params.get("q") ?? "");
  const [result, setResult] = useState<PatternListResult | null>(null);
  const rows = result?.items ?? [];
  const [loading, setLoading] = useState(true);
  const offset = Number(params.get("offset") ?? 0) || 0;
  const filterValue = (k: string) => params.get(k) ?? "";
  // Single writer for the URL state: an empty value removes the param, and
  // any filter change goes back to the first page.
  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "offset") next.delete("offset");
    setParams(next, { replace: true });
  };
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  function load() {
    setLoading(true);
    listPatterns({
      review_status: tab || undefined,
      search: params.get("q") || undefined,
      ...Object.fromEntries(FILTERS.map((f) => [f.key, filterValue(f.key) || undefined])),
      series: filterValue("series") || undefined,
      sort: filterValue("sort") || undefined,
      limit: LIMIT,
      offset,
    })
      .then(setResult)
      .catch((err) => show(err instanceof Error ? err.message : "Couldn't load patterns.", "danger"))
      .finally(() => setLoading(false));
  }

  useEffect(load, [params.toString()]); // eslint-disable-line react-hooks/exhaustive-deps
  // Search box -> ?q= after a short pause.
  useEffect(() => {
    const t = setTimeout(() => {
      if ((params.get("q") ?? "") !== search.trim()) setParam("q", search.trim());
    }, 300);
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
    { key: "category", header: "Type", render: (p) => p.category },
    { key: "series", header: "Series", render: (p) => p.series ?? "--" },
    { key: "length", header: "Length", render: (p) => (p.length_feet != null ? `${p.length_feet} ft` : "--") },
    { key: "volume", header: "Volume", render: (p) => (p.volume_ml != null ? `${p.volume_ml} mL` : "--") },
    { key: "ratio", header: "Ratio", render: (p) => (p.ratio != null ? `${p.ratio}:1` : "--") },
    { key: "source", header: "Source", render: (p) => p.source },
    {
      key: "note",
      header: "Note",
      render: (p) =>
        p.review_status === "pending" && p.review_note ? (
          <span className="line-clamp-2 max-w-xs text-xs text-ink-500" title={p.review_note}>
            {p.review_note.replace(/^Not bulk-approved: /, "")}
          </span>
        ) : null,
    },
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
            onClick={() => setParam("tab", t.key === "pending" ? "" : t.key || "all")}
            className={`rounded-md px-3 py-1.5 text-sm font-medium ${tab === t.key ? "bg-primary text-white" : "bg-ink-100 text-ink-600 hover:bg-ink-200"}`}
          >
            {t.label}
            {result && t.key && (
              <span className="ml-1.5 text-xs opacity-75">{result.counts[t.key as "pending" | "approved" | "rejected"]}</span>
            )}
          </button>
        ))}
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search patterns…"
          className="ml-auto w-56 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
        />
      </div>

      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-ink-200 p-3">
        {FILTERS.map((f) => (
          <select
            key={f.key}
            value={filterValue(f.key)}
            onChange={(e) => setParam(f.key, e.target.value)}
            className={`rounded-md border px-2 py-1.5 text-sm ${filterValue(f.key) ? "border-primary text-ink-800" : "border-ink-300 text-ink-600"}`}
            aria-label={f.label}
          >
            <option value="">{f.label}: any</option>
            {f.options.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
        ))}
        <select
          value={filterValue("series")}
          onChange={(e) => setParam("series", e.target.value)}
          className={`max-w-[16rem] rounded-md border px-2 py-1.5 text-sm ${filterValue("series") ? "border-primary text-ink-800" : "border-ink-300 text-ink-600"}`}
          aria-label="Series"
        >
          <option value="">Series: any</option>
          {(result?.series ?? []).map((s) => (
            <option key={s.series} value={s.series}>
              {s.series} ({s.count})
            </option>
          ))}
        </select>
        <span className="mx-1 h-6 w-px bg-ink-200" />
        <select
          value={filterValue("sort")}
          onChange={(e) => setParam("sort", e.target.value === "attention" ? "" : e.target.value)}
          className="rounded-md border border-ink-300 px-2 py-1.5 text-sm text-ink-600"
          aria-label="Sort"
        >
          {SORTS.map(([v, l]) => (
            <option key={v} value={v}>
              Sort: {l}
            </option>
          ))}
        </select>
        {[...FILTERS.map((f) => f.key), "series", "q"].some((k) => params.get(k)) && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              const next = new URLSearchParams();
              if (params.get("tab")) next.set("tab", params.get("tab")!);
              setSearch("");
              setParams(next, { replace: true });
            }}
          >
            Clear filters
          </Button>
        )}
        <span className="ml-auto text-sm text-ink-500">{result ? `${result.total} pattern${result.total === 1 ? "" : "s"}` : ""}</span>
      </div>

      <DataTable columns={columns} rows={rows} getRowId={(p) => p.id} loading={loading} emptyMessage="No patterns match these filters." />
      <Pagination offset={offset} limit={LIMIT} itemCount={rows.length} total={result?.total} onOffsetChange={(o) => setParam("offset", o ? String(o) : "")} />
    </div>
  );
}
