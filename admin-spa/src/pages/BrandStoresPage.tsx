import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  addBallToBrandLine,
  getBrandStore,
  listBrandStores,
  removeBallFromBrandLine,
  rerunBrandStore,
  updateBrandStore,
} from "../api/client";
import type { BrandArtSlot, BrandStoreDetail, BrandStoreSummary } from "../api/types";
import Badge from "../components/Badge";
import Button from "../components/Button";
import Card from "../components/Card";
import type { Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import { useToast } from "../components/Toast";

// Brand stores (runbook 6fq, migration 058). Al: BowlerIQ owns each brand's
// lineups, and a partner's brand-store art is a standing task BowlerIQ
// reruns by itself when a trigger rule fires (a release, a line change, a
// new photo, a companion selling out, the weekly sweep), each image at a
// stable URL. "Adding the next brand should only mean loading its lineups
// and creating its task" -- scripts/load_brand_lineups.py does both; this
// page is for looking at the result, filing a ball by hand, editing the
// palette/heroes, and "Rerun now".

const fmt = (d: string | null) => (d ? new Date(d).toLocaleString() : "--");

const STATUS_TONE: Record<string, "ok" | "pending" | "danger" | "muted"> = {
  idle: "muted",
  queued: "pending",
  running: "pending",
  failed: "danger",
  succeeded: "ok",
  partial: "pending",
};

export default function BrandStoresPage() {
  const { id } = useParams();
  return id ? <BrandStoreDetailView taskId={id} /> : <BrandStoreList />;
}

function BrandStoreList() {
  const { show } = useToast();
  const [rows, setRows] = useState<BrandStoreSummary[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    listBrandStores()
      .then(setRows)
      .catch((err) => show(err instanceof Error ? err.message : "Couldn't load brand stores.", "danger"))
      .finally(() => setLoading(false));
  }, [show]);

  const columns: Column<BrandStoreSummary>[] = [
    {
      key: "brand",
      header: "Brand",
      render: (r) => (
        <Link to={`/brand-stores/${r.task_id}`} className="font-medium">
          {r.brand}
        </Link>
      ),
    },
    { key: "owner", header: "For", render: (r) => r.owner },
    { key: "status", header: "Status", render: (r) => <Badge tone={STATUS_TONE[r.status] ?? "muted"}>{r.status}</Badge> },
    {
      key: "last",
      header: "Last run",
      render: (r) => (
        <span>
          {fmt(r.last_run_at)}{" "}
          {r.last_run_status ? <Badge tone={STATUS_TONE[r.last_run_status] ?? "muted"}>{r.last_run_status}</Badge> : null}
        </span>
      ),
    },
    { key: "images", header: "Images", render: (r) => `${r.images}${r.failed_slots ? ` (${r.failed_slots} failed)` : ""}` },
    {
      key: "unplaced",
      header: "Unplaced balls",
      render: (r) => (r.unplaced ? <Badge tone="pending">{r.unplaced}</Badge> : <span className="text-xs text-ink-400">none</span>),
    },
  ];

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold text-ink-900">Brand stores</h1>
        <p className="text-sm text-ink-500">
          Each brand's lineups and the brand-store art BowlerIQ draws for a partner. A run happens daily when something
          changed, weekly in full, and on "Rerun now".
        </p>
      </div>
      <DataTable columns={columns} rows={rows} loading={loading} getRowId={(r) => r.task_id} emptyMessage="No brand stores yet." />
    </div>
  );
}

function SlotCard({ slot }: { slot: BrandArtSlot }) {
  return (
    <div className="space-y-1">
      {slot.url ? (
        <a href={slot.url} target="_blank" rel="noreferrer">
          <img src={`${slot.url}?w=480&fmt=webp`} alt={slot.alt ?? ""} className="w-full rounded border border-ink-200" />
        </a>
      ) : (
        <div className="flex aspect-[4/3] items-center justify-center rounded border border-dashed border-ink-200 text-xs text-ink-400">
          not drawn yet
        </div>
      )}
      <div className="flex flex-wrap items-center gap-1 text-xs">
        <span className="font-mono text-ink-700">{slot.slot_key}</span>
        {slot.hero_type ? <Badge tone="muted">{slot.hero_type}</Badge> : null}
        {slot.method ? <Badge tone="muted">{slot.method}</Badge> : null}
        {slot.url_16x9 ? (
          <a href={slot.url_16x9} target="_blank" rel="noreferrer" className="text-primary">
            16:9
          </a>
        ) : null}
      </div>
      <p className="text-xs text-ink-600">{slot.featured.map((f) => f.name).join(" + ")}</p>
      {slot.error ? <p className="text-xs text-danger">{slot.error}</p> : null}
      <p className="text-xs text-ink-400">{fmt(slot.generated_at)}</p>
    </div>
  );
}

function BrandStoreDetailView({ taskId }: { taskId: string }) {
  const { show } = useToast();
  const [data, setData] = useState<BrandStoreDetail | null>(null);
  const [paramsText, setParamsText] = useState("");
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState<Record<string, string>>({});

  const load = useCallback(() => {
    getBrandStore(taskId)
      .then((d) => {
        setData(d);
        setParamsText(JSON.stringify(d.params, null, 2));
      })
      .catch((err) => show(err instanceof Error ? err.message : "Couldn't load the brand store.", "danger"));
  }, [taskId, show]);

  useEffect(load, [load]);

  // Poll while a run is queued or running (a full run with Gemini heroes takes minutes).
  useEffect(() => {
    if (!data || (data.status !== "queued" && data.status !== "running")) return;
    const t = window.setTimeout(load, 10000);
    return () => window.clearTimeout(t);
  }, [data, load]);

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    try {
      await fn();
      show(ok, "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "That didn't work.", "danger");
    } finally {
      setBusy(false);
    }
  };

  if (!data) return <p className="text-sm text-ink-500">Loading...</p>;

  const heroes = data.slots.filter((s) => s.kind === "hero");
  const lines = data.slots.filter((s) => s.kind === "line");
  const ballOptions = Array.from(
    new Map(data.groupings.flatMap((g) => g.lines.flatMap((l) => l.balls)).map((b) => [b.product_id, b])).values(),
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <Link to="/brand-stores" className="text-sm text-ink-500">
            Brand stores
          </Link>
          <h1 className="text-xl font-semibold text-ink-900">
            {data.brand} <span className="text-ink-400">for {data.owner}</span>
          </h1>
          <p className="text-sm text-ink-500">
            <Badge tone={STATUS_TONE[data.status] ?? "muted"}>{data.status}</Badge> last run {fmt(data.last_run_at)}, last
            full run {fmt(data.last_full_run_at)}
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="secondary"
            disabled={busy}
            onClick={() => act(() => updateBrandStore(taskId, { enabled: !data.enabled }), data.enabled ? "Paused." : "Enabled.")}
          >
            {data.enabled ? "Pause" : "Enable"}
          </Button>
          <Button
            disabled={busy || data.status === "queued" || data.status === "running"}
            onClick={() => act(() => rerunBrandStore(taskId), "Rerun queued. It takes a few minutes.")}
          >
            Rerun now
          </Button>
        </div>
      </div>

      <Card title="Heroes">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          {heroes.length ? heroes.map((s) => <SlotCard key={s.slot_key} slot={s} />) : <p className="text-sm text-ink-500">None drawn yet.</p>}
        </div>
      </Card>

      <Card title="Line images">
        <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
          {lines.length ? lines.map((s) => <SlotCard key={s.slot_key} slot={s} />) : <p className="text-sm text-ink-500">None drawn yet.</p>}
        </div>
      </Card>

      {data.unplaced.length ? (
        <Card title="Balls no line rule placed">
          <ul className="space-y-1 text-sm">
            {data.unplaced.map((u) => (
              <li key={u.review_id}>
                <Link to={`/products/${u.product_id}`}>{u.name}</Link>{" "}
                <span className="text-ink-500">({u.grouping}): file it into a line below.</span>
              </li>
            ))}
          </ul>
        </Card>
      ) : null}

      {data.groupings.map((g) => (
        <Card key={g.key} title={`${g.label} lineup`}>
          {g.source ? <p className="mb-2 text-xs text-ink-500">Source: {g.source}</p> : null}
          <div className="space-y-3">
            {g.lines.map((l) => (
              <div key={l.line_id} className="border-t border-ink-100 pt-2 first:border-0 first:pt-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-ink-800">{l.label}</span>
                  <Badge tone="muted">{l.rule === "manual" ? "filed by hand" : `rule: ${l.rule}`}</Badge>
                </div>
                <div className="mt-1 flex flex-wrap gap-2">
                  {l.balls.map((b) => (
                    <span key={b.product_id} className="flex items-center gap-1 rounded border border-ink-200 px-2 py-1 text-xs">
                      {b.image_url ? <img src={b.image_url} alt="" className="h-6 w-6 rounded object-cover" /> : null}
                      <Link to={`/products/${b.product_id}`}>{b.name}</Link>
                      {b.status !== "current" ? <Badge tone="muted">{b.status}</Badge> : null}
                      <span className="text-ink-400">{b.added_by}</span>
                      <button
                        type="button"
                        className="text-ink-400 hover:text-danger"
                        title="Remove from this line"
                        disabled={busy}
                        onClick={() => act(() => removeBallFromBrandLine(l.line_id, b.product_id), "Removed.")}
                      >
                        x
                      </button>
                    </span>
                  ))}
                  {!l.balls.length ? <span className="text-xs text-ink-400">no balls</span> : null}
                </div>
                <div className="mt-1 flex items-center gap-2">
                  <select
                    className="rounded border border-ink-200 px-2 py-1 text-xs"
                    value={adding[l.line_id] ?? ""}
                    onChange={(e) => setAdding({ ...adding, [l.line_id]: e.target.value })}
                  >
                    <option value="">Add a ball...</option>
                    {[...data.unplaced.map((u) => ({ product_id: u.product_id, name: u.name })), ...ballOptions]
                      .filter((b, i, all) => all.findIndex((x) => x.product_id === b.product_id) === i)
                      .filter((b) => !l.balls.some((x) => x.product_id === b.product_id))
                      .map((b) => (
                        <option key={b.product_id} value={b.product_id}>
                          {b.name}
                        </option>
                      ))}
                  </select>
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={busy || !adding[l.line_id]}
                    onClick={() => act(() => addBallToBrandLine(l.line_id, adding[l.line_id]), "Added.")}
                  >
                    Add
                  </Button>
                </div>
              </div>
            ))}
          </div>
        </Card>
      ))}

      <Card title="Task settings">
        <p className="mb-2 text-xs text-ink-500">
          palette (background, accent), line_groupings (which groupings get line images), heroes (up to 3: product
          heroes pick newest_ball or newest_companion; lineup heroes take a line or ball_ids). Saving a change redraws
          the affected images at the next daily run, or now with Rerun.
        </p>
        <textarea
          className="h-56 w-full rounded border border-ink-200 p-2 font-mono text-xs"
          value={paramsText}
          onChange={(e) => setParamsText(e.target.value)}
        />
        <div className="mt-2">
          <Button
            variant="secondary"
            disabled={busy}
            onClick={() => {
              let params: Record<string, unknown>;
              try {
                params = JSON.parse(paramsText);
              } catch {
                show("That isn't valid JSON.", "danger");
                return;
              }
              act(() => updateBrandStore(taskId, { params }), "Saved.");
            }}
          >
            Save settings
          </Button>
        </div>
      </Card>

      <Card title="Recent runs">
        <ul className="space-y-1 text-xs">
          {data.runs.map((r) => (
            <li key={r.run_id}>
              <Badge tone={STATUS_TONE[r.status] ?? "muted"}>{r.status}</Badge> {fmt(r.started_at)} - {r.trigger}
              {r.slots.length ? ` (${r.slots.join(", ")})` : " (all slots)"}
              {r.detail?.error ? <span className="text-danger"> {r.detail.error}</span> : null}
            </li>
          ))}
          {!data.runs.length ? <li className="text-ink-400">No runs yet.</li> : null}
        </ul>
      </Card>
    </div>
  );
}
