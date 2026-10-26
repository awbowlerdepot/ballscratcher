import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { approvePattern, getPattern, readPatternSheet, rejectPattern, updatePattern } from "../api/client";
import { PATTERN_CATEGORIES, PATTERN_SOURCES } from "../api/types";
import type { PatternCheck, PatternDetail, PatternEdit, PatternStep, TrackZone } from "../api/types";
import Button from "../components/Button";
import LanePreview from "../components/LanePreview";
import Modal from "../components/Modal";
import { useToast } from "../components/Toast";
import { readBadge, reviewBadge } from "./PatternsPage";

// Oil pattern review (runbook 6ej). The rendered sheet sits next to the
// fields the reader pulled off it; every save recomputes the steps, board
// totals and the per-foot oil grid server-side (admin_api oil_patterns) and
// re-runs the checks against the sheet's own totals. Approve is disabled
// while any check is an error -- partners only ever get patterns that add up.

type Draft = PatternEdit & { steps: PatternStep[]; track_zones: TrackZone[]; machine: Record<string, unknown> };

const HEADER_FIELDS: { key: keyof PatternEdit; label: string; type: "text" | "number"; unit?: string }[] = [
  { key: "name", label: "Name", type: "text" },
  { key: "short_name", label: "Short name", type: "text" },
  { key: "series", label: "Series", type: "text" },
  { key: "length_feet", label: "Pattern length", type: "number", unit: "ft" },
  { key: "reverse_brush_drop_feet", label: "Reverse brush drop", type: "number", unit: "ft" },
  { key: "oil_per_board_ul", label: "Oil per board", type: "number", unit: "µL" },
  { key: "forward_ml", label: "Forward total", type: "number", unit: "mL" },
  { key: "reverse_ml", label: "Reverse total", type: "number", unit: "mL" },
  { key: "volume_ml", label: "Volume total", type: "number", unit: "mL" },
  { key: "source_url", label: "Source URL", type: "text" },
];

const STEP_COLS: { key: keyof PatternStep; label: string; w: string; num?: boolean }[] = [
  { key: "start", label: "Start", w: "w-14" },
  { key: "stop", label: "Stop", w: "w-14" },
  { key: "loads", label: "Loads", w: "w-14", num: true },
  { key: "mics", label: "Mics", w: "w-14", num: true }, // per step on Pattern Library sheets (runbook 6es)
  { key: "speed_ips", label: "Speed", w: "w-14", num: true },
  { key: "buffer", label: "Buff", w: "w-12", num: true },
  { key: "tank", label: "Tank", w: "w-12" },
  { key: "reported_crossed", label: "Crossed", w: "w-16", num: true },
  { key: "start_feet", label: "Start ft", w: "w-16", num: true },
  { key: "end_feet", label: "End ft", w: "w-16", num: true },
  { key: "reported_oil_ul", label: "Oil µL", w: "w-20", num: true },
];

function toDraft(p: PatternDetail): Draft {
  return {
    name: p.name,
    short_name: p.short_name,
    category: p.category,
    series: p.series,
    source: p.source,
    source_url: p.source_url,
    status: p.status,
    length_feet: p.length_feet,
    reverse_brush_drop_feet: p.reverse_brush_drop_feet,
    oil_per_board_ul: p.oil_per_board_ul,
    forward_ml: p.forward_ml,
    reverse_ml: p.reverse_ml,
    volume_ml: p.volume_ml,
    machine: { ...(p.machine || {}) },
    // The computed zones: the stored ones (or Kegel's standard six) with
    // ratio from the loads and the sheet's value as reported_ratio.
    track_zones: (p.computed.track_zones || p.track_zones || []).map((z) => ({ ...z })),
    // Edit what the SHEET says: reported_* hold its crossed/oil columns.
    steps: (p.steps || []).map((s) => ({
      ...s,
      reported_crossed: s.reported_crossed ?? s.crossed ?? null,
      reported_oil_ul: s.reported_oil_ul ?? s.oil_ul ?? null,
    })),
  };
}

const numOrNull = (v: string) => (v.trim() === "" || Number.isNaN(Number(v)) ? null : Number(v));

export default function PatternDetailPage() {
  const { id = "" } = useParams();
  const { show } = useToast();
  const [p, setP] = useState<PatternDetail | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");

  function load(resetDraft = true) {
    return getPattern(id)
      .then((d) => {
        setP(d);
        if (resetDraft) {
          setDraft(toDraft(d));
          setDirty(false);
        }
        return d;
      })
      .catch((err) => show(err instanceof Error ? err.message : "Couldn't load the pattern.", "danger"));
  }
  useEffect(() => {
    load();
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Poll while the sheet is being read; take the reader's fields when it's done
  // (unless someone has started editing).
  const reading = p?.extraction_status === "queued" || p?.extraction_status === "running";
  useEffect(() => {
    if (!reading) return;
    const t = setInterval(() => load(!dirty), 4000);
    return () => clearInterval(t);
  }, [reading, dirty]); // eslint-disable-line react-hooks/exhaustive-deps

  const set = (patch: Partial<Draft>) => {
    setDraft((d) => (d ? { ...d, ...patch } : d));
    setDirty(true);
  };
  const setStep = (i: number, patch: Partial<PatternStep>) => {
    setDraft((d) => (d ? { ...d, steps: d.steps.map((s, n) => (n === i ? { ...s, ...patch } : s)) } : d));
    setDirty(true);
  };
  const setZone = (i: number, patch: Partial<TrackZone>) => {
    setDraft((d) => (d ? { ...d, track_zones: d.track_zones.map((z, n) => (n === i ? { ...z, ...patch } : z)) } : d));
    setDirty(true);
  };

  async function save() {
    if (!draft) return;
    setBusy("save");
    try {
      const d = await updatePattern(id, draft);
      setP(d);
      setDraft(toDraft(d));
      setDirty(false);
      show(d.checks.some((c) => c.level === "error") ? "Saved. Some checks still fail." : "Saved. All checks pass.", "ok");
    } catch (err) {
      show(err instanceof Error ? err.message : "Save failed.", "danger");
    } finally {
      setBusy(null);
    }
  }

  async function act(kind: "approve" | "read" | "reject") {
    setBusy(kind);
    try {
      if (kind === "approve") {
        const d = await approvePattern(id);
        setP(d);
        show("Approved. It's live in the Partner API within a minute.", "ok");
      } else if (kind === "read") {
        const r = await readPatternSheet(id);
        if (!r.queued) throw new Error(r.reason ?? "Couldn't start the reader.");
        show("Reading the sheet again…", "ok");
        await load(true);
      } else {
        const d = await rejectPattern(id, note);
        setP(d);
        setRejecting(false);
        show("Rejected.", "ok");
      }
    } catch (err) {
      show(err instanceof Error ? err.message : "Request failed.", "danger");
    } finally {
      setBusy(null);
    }
  }

  const checksByStep = useMemo(() => {
    const m = new Map<string, PatternCheck[]>();
    (p?.checks ?? []).forEach((c) => c.step && m.set(c.step, [...(m.get(c.step) ?? []), c]));
    return m;
  }, [p]);

  if (!p || !draft) return <div className="p-6 text-sm text-ink-500">Loading…</div>;
  const errors = p.checks.filter((c) => c.level === "error");
  const computedByKey = new Map(p.computed.steps.map((s) => [`${s.direction} ${s.order}`, s]));

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link to="/oil-patterns" className="text-sm text-ink-500 hover:text-ink-800">
            ‹ Oil patterns
          </Link>
          <h1 className="text-xl font-semibold text-ink-800">{p.name}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            {reviewBadge(p)}
            {readBadge(p.extraction_status)}
            {p.upload_filename && <span className="text-xs text-ink-500">from {p.upload_filename}</span>}
            {p.resolved_by && (
              <span className="text-xs text-ink-500">
                {p.review_status} by {p.resolved_by}
                {p.resolved_at ? ` on ${new Date(p.resolved_at).toLocaleDateString()}` : ""}
              </span>
            )}
          </div>
          {p.extraction_status === "failed" && <p className="mt-1 text-sm text-danger">Reading failed: {p.extraction_error}</p>}
        </div>
        <div className="flex flex-wrap gap-2">
          {p.upload_filename && p.review_status === "pending" && (
            <Button variant="ghost" onClick={() => act("read")} disabled={busy !== null || reading}>
              Read sheet again
            </Button>
          )}
          <Button variant="secondary" onClick={save} disabled={!dirty || busy !== null}>
            {busy === "save" ? "Saving…" : dirty ? "Save changes" : "Saved"}
          </Button>
          {p.review_status !== "rejected" && (
            <Button variant="ghost" className="text-danger" onClick={() => setRejecting(true)} disabled={busy !== null}>
              {p.review_status === "approved" ? "Unpublish" : "Reject"}
            </Button>
          )}
          {p.review_status !== "approved" && (
            <Button
              variant="primary"
              onClick={() => act("approve")}
              disabled={busy !== null || dirty || errors.length > 0 || reading}
              title={dirty ? "Save first" : errors.length ? "Fix the failing checks first" : "Publish to the Partner API"}
            >
              {busy === "approve" ? "Approving…" : "Approve & publish"}
            </Button>
          )}
        </div>
      </div>

      {reading && (
        <div className="rounded-md bg-primary-light px-4 py-3 text-sm text-primary-dark">
          Reading the sheet with OCR… the fields below fill in when it's done (usually under 20 seconds).
        </div>
      )}

      {/* Checks */}
      <div className={`rounded-lg border p-3 text-sm ${errors.length ? "border-danger bg-danger-light" : "border-ink-200 bg-ink-100"}`}>
        {p.checks.length === 0 ? (
          <span className="font-medium text-ok">✓ Every check passes: the steps add up to the sheet's totals.</span>
        ) : (
          <>
            <div className="mb-1 font-medium text-ink-800">
              {errors.length ? `${errors.length} check${errors.length > 1 ? "s" : ""} failing` : "Warnings"}
              {dirty && <span className="ml-2 font-normal text-ink-500">(as of the last save)</span>}
            </div>
            <ul className="list-disc space-y-0.5 pl-5">
              {p.checks.map((c, i) => (
                <li key={i} className={c.level === "error" ? "text-danger" : "text-warn"}>
                  {c.step ? <span className="font-medium">{c.step}: </span> : null}
                  {c.message}
                </li>
              ))}
            </ul>
          </>
        )}
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        {/* Sheet */}
        <div className="flex flex-col gap-2">
          {p.page_image_url ? (
            <a href={p.sheet_pdf_url ?? p.page_image_url} target="_blank" rel="noreferrer" title="Open the original sheet">
              <img src={p.page_image_url} alt="Pattern sheet" className="w-full rounded-md border border-ink-200 bg-white" />
            </a>
          ) : (
            <div className="rounded-md border border-dashed border-ink-300 p-6 text-sm text-ink-500">
              {p.upload_filename ? "The sheet preview appears once it's been read." : "Typed in by hand: no sheet."}
            </div>
          )}
          <LanePreview rows={p.computed.profile.rows} firstLaneRows={p.computed.first_lane_rows} burnLanes={p.computed.profile.burn_lanes} />
        </div>

        {/* Fields */}
        <div className="flex min-w-0 flex-col gap-4">
          <div className="grid grid-cols-2 gap-3 rounded-lg border border-ink-200 p-3 md:grid-cols-3">
            {HEADER_FIELDS.map((f) => (
              <label key={f.key} className={`flex flex-col text-xs font-medium text-ink-600 ${f.key === "name" || f.key === "source_url" ? "col-span-2 md:col-span-3" : ""}`}>
                {f.label}
                {f.unit && <span className="sr-only"> ({f.unit})</span>}
                <div className="mt-1 flex items-center gap-1">
                  <input
                    value={(draft[f.key] as string | number | null | undefined) ?? ""}
                    onChange={(e) => set({ [f.key]: f.type === "number" ? numOrNull(e.target.value) : e.target.value } as Partial<Draft>)}
                    inputMode={f.type === "number" ? "decimal" : undefined}
                    className="w-full rounded-md border border-ink-300 px-2 py-1 text-sm font-normal text-ink-800"
                  />
                  {f.unit && <span className="text-xs text-ink-400">{f.unit}</span>}
                </div>
              </label>
            ))}
            <label className="flex flex-col text-xs font-medium text-ink-600">
              Category
              <select value={draft.category ?? "sport"} onChange={(e) => set({ category: e.target.value })} className="mt-1 rounded-md border border-ink-300 px-2 py-1 text-sm">
                {PATTERN_CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col text-xs font-medium text-ink-600">
              Source
              <select value={draft.source ?? "upload"} onChange={(e) => set({ source: e.target.value })} className="mt-1 rounded-md border border-ink-300 px-2 py-1 text-sm">
                {PATTERN_SOURCES.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col text-xs font-medium text-ink-600">
              Status
              <select value={draft.status ?? "current"} onChange={(e) => set({ status: e.target.value as "current" | "retired" })} className="mt-1 rounded-md border border-ink-300 px-2 py-1 text-sm">
                <option value="current">current</option>
                <option value="retired">retired</option>
              </select>
            </label>
          </div>

          {(["forward", "reverse"] as const).map((dir) => (
            <StepTable
              key={dir}
              direction={dir}
              steps={draft.steps}
              setStep={setStep}
              onAdd={() => {
                const n = draft.steps.filter((s) => s.direction === dir).length + 1;
                set({ steps: [...draft.steps, { direction: dir, order: n, start: "2L", stop: "2R", loads: 0, speed_ips: null, buffer: null, tank: "A", start_feet: null, end_feet: null }] });
              }}
              onRemove={(i) => set({ steps: draft.steps.filter((_, n) => n !== i) })}
              checksByStep={checksByStep}
              computedByKey={computedByKey}
            />
          ))}

          <div className="rounded-lg border border-ink-200 p-3">
            <div className="mb-2 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-ink-800">Track zones</h2>
              {/* Fixed to Kegel's standard six (runbook 6eq): only the sheet's value is editable. */}
              <span className="text-xs text-ink-500">Kegel's standard six</span>
            </div>
            <div className="overflow-x-auto">
              <table className="text-sm">
                <thead>
                  <tr className="text-left text-xs text-ink-500">
                    {["Zone", "Against", "Description", "Sheet", "From loads"].map((h, i) => (
                      <th key={i} className="px-1 pb-1 font-medium">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {draft.track_zones.map((z, i) => (
                    <tr key={i}>
                      <td className="whitespace-nowrap px-1 py-0.5 text-ink-800">
                        {z.from}–{z.to}
                      </td>
                      <td className="whitespace-nowrap px-1 py-0.5 text-ink-800">
                        {z.against_from}–{z.against_to}
                      </td>
                      <td className="whitespace-nowrap px-1 py-0.5 text-ink-600">{z.label}</td>
                      <td className="px-1 py-0.5">
                        <input value={z.reported_ratio ?? ""} onChange={(e) => setZone(i, { reported_ratio: numOrNull(e.target.value) })} className="w-16 rounded border border-ink-300 px-1.5 py-0.5" title="The sheet's ratio, for checking" />
                      </td>
                      <td className="px-1 py-0.5 tabular-nums">
                        {(() => {
                          // Computed from the loads at the last save -- what's published.
                          const comp = p.computed.track_zones?.[i]?.ratio;
                          const sheet = z.reported_ratio;
                          const off = comp != null && sheet != null && Math.abs(comp - sheet) > 0.011;
                          return (
                            <span className={off ? "font-medium text-warn" : "text-ink-800"} title={off ? "Doesn't match the sheet" : undefined}>
                              {comp != null ? comp.toFixed(2) : "--"}
                              {off ? " ⚠" : ""}
                            </span>
                          );
                        })()}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="mt-2 text-xs text-ink-500">
              Ratios are calculated from the loads: each zone's average oil per board, the zone nearer the middle over the other. The zones are fixed;
              the Sheet column is only there to check against. Headline ratio (largest zone): {p.computed.ratio != null ? `${p.computed.ratio}:1` : "--"}
            </p>
          </div>
        </div>
      </div>

      <Modal
        open={rejecting}
        onClose={() => (busy ? undefined : setRejecting(false))}
        title={p.review_status === "approved" ? "Unpublish this pattern?" : "Reject this pattern?"}
        footer={
          <>
            <Button variant="secondary" onClick={() => setRejecting(false)} disabled={busy !== null}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => act("reject")} disabled={busy !== null}>
              {p.review_status === "approved" ? "Unpublish" : "Reject"}
            </Button>
          </>
        }
      >
        <p className="mb-2 text-sm text-ink-600">
          {p.review_status === "approved"
            ? "Partners get a remove for it on their next sync. You can approve it again later."
            : "It stays here, out of the Partner API."}
        </p>
        <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} placeholder="Reason (optional)" className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm" />
      </Modal>
    </div>
  );
}

function StepTable({
  direction,
  steps,
  setStep,
  onAdd,
  onRemove,
  checksByStep,
  computedByKey,
}: {
  direction: "forward" | "reverse";
  steps: PatternStep[];
  setStep: (i: number, patch: Partial<PatternStep>) => void;
  onAdd: () => void;
  onRemove: (i: number) => void;
  checksByStep: Map<string, PatternCheck[]>;
  computedByKey: Map<string, PatternStep>;
}) {
  const rows = steps.map((s, i) => ({ s, i })).filter(({ s }) => s.direction === direction);
  return (
    <div className="rounded-lg border border-ink-200 p-3">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-semibold capitalize text-ink-800">{direction} steps</h2>
        <Button size="sm" variant="ghost" onClick={onAdd}>
          + Step
        </Button>
      </div>
      <div className="overflow-x-auto">
        <table className="text-sm">
          <thead>
            <tr className="text-left text-xs text-ink-500">
              <th className="px-1 pb-1 font-medium">#</th>
              {STEP_COLS.map((c) => (
                <th key={c.key} className="px-1 pb-1 font-medium">
                  {c.label}
                </th>
              ))}
              <th className="px-1 pb-1 font-medium" title="Computed from the loads and boards">
                Boards
              </th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map(({ s, i }) => {
              const key = `${s.direction} ${s.order}`;
              const issues = checksByStep.get(key) ?? [];
              const bad = issues.some((c) => c.level === "error");
              const comp = computedByKey.get(key);
              return (
                <tr key={i} className={bad ? "bg-danger-light" : issues.length ? "bg-warn-light" : ""} title={issues.map((c) => c.message).join("\n") || undefined}>
                  <td className="px-1 py-0.5 text-ink-500">{s.order}</td>
                  {STEP_COLS.map((c) => (
                    <td key={c.key} className="px-1 py-0.5">
                      <input
                        value={(s[c.key] as string | number | null | undefined) ?? ""}
                        onChange={(e) => setStep(i, { [c.key]: c.num ? numOrNull(e.target.value) : e.target.value.toUpperCase() } as Partial<PatternStep>)}
                        inputMode={c.num ? "decimal" : undefined}
                        className={`${c.w} rounded border border-ink-300 px-1.5 py-0.5 tabular-nums`}
                      />
                    </td>
                  ))}
                  <td className="whitespace-nowrap px-1 py-0.5 text-xs text-ink-500 tabular-nums">
                    {comp?.from_board != null ? `${comp.from_board}–${comp.to_board}` : "--"}
                  </td>
                  <td>
                    <button type="button" className="px-1 text-ink-400 hover:text-danger" onClick={() => onRemove(i)} aria-label="Remove step">
                      ✕
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {rows.length === 0 && <p className="text-xs text-ink-500">No {direction} steps yet.</p>}
    </div>
  );
}
