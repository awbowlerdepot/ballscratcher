import { useEffect, useRef, useState } from "react";
import {
  createManualSeedUrl,
  deleteManualSeedUrl,
  listBrands,
  listManualSeedUrls,
  listProducts,
  refreshVideoSummary,
  rescrapeProduct,
} from "../api/client";
import type {
  Brand,
  ListProductsParams,
  ManualSeedUrl,
  RefreshRollupResult,
  RescrapeResult,
} from "../api/types";
import Button from "../components/Button";
import type { Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import Modal from "../components/Modal";
import { useToast } from "../components/Toast";

// The subset of ListProductsParams that identify a "products missing
// X" filter -- every batch panel below lists against one of these,
// same six filters the Products tab itself exposes as checkboxes/
// dropdown options. Typed narrowly (rather than `keyof
// ListProductsParams`) so a BatchRunner can only ever be pointed at
// one of these boolean filters, not e.g. `limit` or `sort`.
type BatchFilterParam =
  | "needs_video_summary_refresh"
  | "has_approved_video_summaries"
  | "missing_core"
  | "missing_coverstock"
  | "missing_skus"
  | "html_fallback_skus";

// Mirrors admin-site's runBatch/BATCH_CONFIGS list-then-loop logic
// (itself mirroring scripts/backfill_video_review_rollups.py's run())
// -- list every product matching one GET /products filter, then call
// one POST endpoint per product, sequentially, with a Stop button that
// takes effect between items. `running` is tracked in a ref (not just
// state) because the loop reads it on every iteration of an async
// for-loop -- a plain state closure captured at loop-start would never
// see the click-triggered update.
interface BatchRunnerProps<T> {
  title: string;
  description: string;
  filterParam: BatchFilterParam;
  call: (productId: string) => Promise<T>;
  isSuccess: (result: T) => boolean;
  describe: (result: T) => string;
  danger?: boolean;
  confirmMessage?: string;
}

function BatchRunner<T>({
  title,
  description,
  filterParam,
  call,
  isSuccess,
  describe,
  danger,
  confirmMessage,
}: BatchRunnerProps<T>) {
  const [running, setRunning] = useState(false);
  const runningRef = useRef(false);
  const [log, setLog] = useState<string[]>([]);
  const [stats, setStats] = useState<{ total: number; done: number; errors: number } | null>(null);
  const logRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [log]);

  function appendLog(line: string) {
    setLog((prev) => [...prev, line]);
  }

  async function start() {
    if (runningRef.current) return;
    if (confirmMessage && !window.confirm(confirmMessage)) return;
    runningRef.current = true;
    setRunning(true);
    setLog([]);
    setStats(null);

    let total = 0;
    let done = 0;
    let errors = 0;
    try {
      appendLog("Listing products…");
      let items: { id: string; name: string }[] = [];
      let offset = 0;
      const limit = 200;
      while (runningRef.current) {
        const params: ListProductsParams = { limit, offset };
        params[filterParam] = true;
        const page = await listProducts(params);
        items = items.concat(page);
        if (page.length < limit) break;
        offset += limit;
      }
      if (!runningRef.current) {
        appendLog("Stopped before listing finished.");
        return;
      }
      total = items.length;
      appendLog(`Found ${total} product(s).`);
      setStats({ total, done: 0, errors: 0 });

      for (const product of items) {
        if (!runningRef.current) {
          appendLog("Stopped.");
          break;
        }
        try {
          const result = await call(product.id);
          if (isSuccess(result)) done++;
          appendLog(`${product.name}: ${describe(result)}`);
        } catch (err) {
          errors++;
          appendLog(`ERROR ${product.name}: ${err instanceof Error ? err.message : "failed"}`);
        }
        setStats({ total, done, errors });
      }
      if (runningRef.current) appendLog("Done.");
    } catch (err) {
      appendLog(`Fatal error: ${err instanceof Error ? err.message : "failed"}`);
    } finally {
      runningRef.current = false;
      setRunning(false);
    }
  }

  function stop() {
    runningRef.current = false;
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-ink-200 bg-ink-100 p-4">
      <h3 className="text-sm font-semibold text-ink-800">{title}</h3>
      <p className="text-sm text-ink-500">{description}</p>
      <div className="flex gap-2">
        <Button variant={danger ? "danger" : "primary"} size="sm" onClick={start} disabled={running}>
          Start
        </Button>
        <Button variant="ghost" size="sm" onClick={stop} disabled={!running}>
          Stop
        </Button>
      </div>
      {stats && (
        <div className="font-mono text-xs text-ink-500">
          total: {stats.total} done: {stats.done} errors: {stats.errors}
        </div>
      )}
      {log.length > 0 && (
        <pre
          ref={logRef}
          className="max-h-40 overflow-y-auto whitespace-pre-wrap rounded-md bg-ink-50 p-2 font-mono text-xs text-ink-600"
        >
          {log.join("\n")}
        </pre>
      )}
    </div>
  );
}

function describeRescrape(r: RescrapeResult): string {
  return r.queued ? "queued for rescrape" : (r.reason ?? "not queued");
}

function describeRollupRefresh(r: RefreshRollupResult): string {
  return r.rollup_regenerated ? `refreshed (${r.video_count} videos)` : (r.reason ?? "not refreshed");
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString();
}

// Ports admin-site/index.html's Batch Jobs tab -- the last remaining
// admin-site tab (see README.md's "What's not here yet"). Three parts:
// a single-product rollup refresh, six list-then-loop batch operations
// (BatchRunner above), and the Manual Seed URLs panel (unrelated table,
// but admin-site groups it here too since it's a small, occasionally-
// used admin action rather than something worth its own top-level tab).
export default function BatchJobsPage() {
  const { show } = useToast();

  const [singleId, setSingleId] = useState("");
  const [singleResult, setSingleResult] = useState<string | null>(null);
  const [singleLoading, setSingleLoading] = useState(false);

  async function handleSingleRefresh() {
    const id = singleId.trim();
    if (!id) {
      show("Enter a product_id.", "danger");
      return;
    }
    setSingleLoading(true);
    setSingleResult(null);
    try {
      const result = await refreshVideoSummary(id);
      setSingleResult(describeRollupRefresh(result));
    } catch (err) {
      setSingleResult(`Error: ${err instanceof Error ? err.message : "failed"}`);
    } finally {
      setSingleLoading(false);
    }
  }

  // --- Manual seed URLs ---
  const [brands, setBrands] = useState<Brand[]>([]);
  const [seedUrls, setSeedUrls] = useState<ManualSeedUrl[]>([]);
  const [seedUrlsLoading, setSeedUrlsLoading] = useState(true);
  const [seedUrlsError, setSeedUrlsError] = useState<string | null>(null);
  const [newBrandId, setNewBrandId] = useState("");
  const [newUrl, setNewUrl] = useState("");
  const [newNote, setNewNote] = useState("");
  const [creatingSeedUrl, setCreatingSeedUrl] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ManualSeedUrl | null>(null);
  const [deleteSubmitting, setDeleteSubmitting] = useState(false);

  function loadSeedUrls() {
    setSeedUrlsLoading(true);
    setSeedUrlsError(null);
    listManualSeedUrls()
      .then(setSeedUrls)
      .catch((err) => setSeedUrlsError(err instanceof Error ? err.message : "Failed to load seed URLs."))
      .finally(() => setSeedUrlsLoading(false));
  }

  useEffect(() => {
    listBrands().then(setBrands).catch(() => undefined);
    loadSeedUrls();
  }, []);

  async function handleCreateSeedUrl() {
    if (!newBrandId) {
      show("Select a brand.", "danger");
      return;
    }
    if (!newUrl.trim()) {
      show("Product URL is required.", "danger");
      return;
    }
    setCreatingSeedUrl(true);
    try {
      await createManualSeedUrl({ brand_id: newBrandId, url: newUrl.trim(), note: newNote.trim() || undefined });
      show("Seed URL added -- picked up on the next discovery run.", "ok");
      setNewUrl("");
      setNewNote("");
      loadSeedUrls();
    } catch (err) {
      show(err instanceof Error ? err.message : "Failed to add seed URL.", "danger");
    } finally {
      setCreatingSeedUrl(false);
    }
  }

  async function confirmDeleteSeedUrl() {
    if (!deleteTarget) return;
    setDeleteSubmitting(true);
    try {
      await deleteManualSeedUrl(deleteTarget.id);
      show("Removed.", "ok");
      setDeleteTarget(null);
      loadSeedUrls();
    } catch (err) {
      show(err instanceof Error ? err.message : "Remove failed.", "danger");
    } finally {
      setDeleteSubmitting(false);
    }
  }

  const seedUrlColumns: Column<ManualSeedUrl>[] = [
    { key: "brand_name", header: "Brand", render: (s) => s.brand_name },
    {
      key: "url",
      header: "URL",
      render: (s) => (
        <a href={s.url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
          {s.url}
        </a>
      ),
    },
    { key: "note", header: "Note", render: (s) => s.note ?? <span className="text-ink-400">—</span> },
    { key: "created_at", header: "Added", render: (s) => fmtDate(s.created_at) },
    {
      key: "actions",
      header: "",
      render: (s) => (
        <Button size="sm" variant="ghost" className="text-danger" onClick={() => setDeleteTarget(s)}>
          Remove
        </Button>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-8">
      <h1 className="text-xl font-semibold text-ink-800">Batch Jobs</h1>

      <section className="flex flex-col gap-2 rounded-lg border border-ink-200 bg-ink-100 p-4">
        <h3 className="text-sm font-semibold text-ink-800">Refresh one product&rsquo;s rollup</h3>
        <p className="text-sm text-ink-500">
          On-demand equivalent of POST /products/&#123;id&#125;/refresh-video-summary -- regenerates the "summary of
          summaries" from whatever&rsquo;s currently approved+summarized for that product.
        </p>
        <div className="flex items-end gap-3">
          <input
            value={singleId}
            onChange={(e) => setSingleId(e.target.value)}
            placeholder="product_id (uuid)"
            className="w-72 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
          />
          <Button variant="primary" size="sm" onClick={handleSingleRefresh} disabled={singleLoading}>
            {singleLoading ? "Working…" : "Refresh"}
          </Button>
        </div>
        {singleResult && <p className="text-sm text-ink-500">{singleResult}</p>}
      </section>

      <section className="flex flex-col gap-4">
        <h2 className="text-lg font-semibold text-ink-800">Bulk operations</h2>
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <BatchRunner<RefreshRollupResult>
            title="Refresh stale rollups"
            description="Same filter scripts/backfill_video_review_rollups.py uses by default (needs_video_summary_refresh): products whose rollup is missing, or out of date with their current approved+summarized video count."
            filterParam="needs_video_summary_refresh"
            call={refreshVideoSummary}
            isSuccess={(r) => r.rollup_regenerated}
            describe={describeRollupRefresh}
          />
          <BatchRunner<RefreshRollupResult>
            title="Refresh ALL (catalog-wide)"
            description="Equivalent of REFRESH_ALL=true scripts/backfill_video_review_rollups.py (has_approved_video_summaries filter): regenerates every product with at least one approved+summarized video, regardless of staleness. Real Bedrock cost across the whole catalog -- an occasional deliberate pass, not routine use."
            filterParam="has_approved_video_summaries"
            call={refreshVideoSummary}
            isSuccess={(r) => r.rollup_regenerated}
            describe={describeRollupRefresh}
            danger
            confirmMessage="This regenerates EVERY product with an approved+summarized video, not just stale ones -- real Bedrock cost across the whole catalog. Continue?"
          />
          <BatchRunner<RescrapeResult>
            title="Backfill missing core info"
            description="Equivalent of scripts/backfill_core_ids.py (missing_core filter): queues a rescrape for every product with no core_id set yet. Only enqueues -- re-run later (or re-filter the Products tab) to see how much is left."
            filterParam="missing_core"
            call={rescrapeProduct}
            isSuccess={(r) => r.queued}
            describe={describeRescrape}
          />
          <BatchRunner<RescrapeResult>
            title="Backfill missing coverstock info"
            description="Equivalent of scripts/backfill_coverstock_ids.py (missing_coverstock filter): queues a rescrape for every product with no coverstock_id set yet. Only enqueues -- re-run later to see how much is left."
            filterParam="missing_coverstock"
            call={rescrapeProduct}
            isSuccess={(r) => r.queued}
            describe={describeRescrape}
          />
          <BatchRunner<RescrapeResult>
            title="Backfill missing SKUs"
            description="Equivalent of scripts/rescrape_commercebuild_products.py (missing_skus filter): queues a rescrape for every product with zero product_skus rows, any platform. Only enqueues -- re-run later to see how much is left."
            filterParam="missing_skus"
            call={rescrapeProduct}
            isSuccess={(r) => r.queued}
            describe={describeRescrape}
          />
          <BatchRunner<RescrapeResult>
            title="Backfill 900 Global full weight table (Textract)"
            description="Equivalent of GET /products?html_fallback_skus=true: queues a rescrape for every product whose entire product_skus set is a single source='html' row, distinct from 'missing SKUs' above which only catches zero-row products. A nonzero count after rescraping isn't necessarily still-broken -- spot-check a few."
            filterParam="html_fallback_skus"
            call={rescrapeProduct}
            isSuccess={(r) => r.queued}
            describe={describeRescrape}
          />
        </div>
      </section>

      <section className="flex flex-col gap-4 border-t border-ink-200 pt-6">
        <h2 className="text-lg font-semibold text-ink-800">Manual seed URLs (orphan-page catch)</h2>
        <p className="text-sm text-ink-500">
          For a real, live, in-stock product page a manufacturer's own site has stopped linking to internally -- so it
          never shows up in a category-listing crawl or sitemap, no matter how often discovery runs. Seeding it here
          gets it picked up on Storm/Roto Grip/900 Global's next scheduled discovery run, same as any normally-discovered
          URL.
        </p>

        <div className="flex flex-wrap items-end gap-3 rounded-lg border border-ink-200 bg-ink-100 p-3">
          <div>
            <label className="mb-1 block text-xs font-medium text-ink-600">Brand</label>
            <select
              value={newBrandId}
              onChange={(e) => setNewBrandId(e.target.value)}
              className="w-56 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
            >
              <option value="">-- select brand --</option>
              {brands.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
            </select>
          </div>
          <div className="flex-1">
            <label className="mb-1 block text-xs font-medium text-ink-600">Product URL</label>
            <input
              value={newUrl}
              onChange={(e) => setNewUrl(e.target.value)}
              placeholder="https://www.stormbowling.com/storm-equinox-bowling-ball"
              className="w-full max-w-md rounded-md border border-ink-300 px-2 py-1.5 text-sm"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-ink-600">Note (optional)</label>
            <input
              value={newNote}
              onChange={(e) => setNewNote(e.target.value)}
              placeholder="e.g. orphaned on-site"
              className="w-56 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
            />
          </div>
          <Button variant="primary" onClick={handleCreateSeedUrl} disabled={creatingSeedUrl}>
            {creatingSeedUrl ? "Adding…" : "Add"}
          </Button>
        </div>

        {seedUrlsError && <div className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{seedUrlsError}</div>}

        <DataTable
          columns={seedUrlColumns}
          rows={seedUrls}
          getRowId={(s) => s.id}
          emptyMessage={seedUrlsLoading ? "Loading…" : "No manual seed URLs yet."}
        />
      </section>

      <Modal
        open={deleteTarget !== null}
        onClose={() => (deleteSubmitting ? undefined : setDeleteTarget(null))}
        title="Remove this seed URL?"
        footer={
          <>
            <Button variant="secondary" onClick={() => setDeleteTarget(null)} disabled={deleteSubmitting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={confirmDeleteSeedUrl} disabled={deleteSubmitting}>
              {deleteSubmitting ? "Removing…" : "Remove"}
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-600">
          It stops being force-included in future discovery runs. Any product already scraped from it stays in the
          catalog.
        </p>
      </Modal>
    </div>
  );
}
