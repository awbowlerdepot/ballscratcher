import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { listProducts, rescrapeProduct } from "../api/client";
import type { ListProductsParams, Product, ProductSort, SourcePlatform } from "../api/types";
import Badge from "../components/Badge";
import type { BulkAction, Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import { IconArticles, IconVideo } from "../components/icons";
import Pagination from "../components/Pagination";
import { useToast } from "../components/Toast";

const LIMIT = 50;

const SORT_OPTIONS: { value: ProductSort; label: string }[] = [
  { value: "popularity", label: "Popularity" },
  { value: "total_daily_movement", label: "Avg Daily Movement" },
  { value: "demand_score", label: "Demand Score" },
  { value: "newest", label: "Newest" },
  { value: "oldest", label: "Oldest" },
  { value: "name_asc", label: "Name A-Z" },
  { value: "name_desc", label: "Name Z-A" },
];

const SOURCE_OPTIONS: SourcePlatform[] = ["netsuite", "shopify", "woocommerce", "commercebuild", "craft_cms"];

// Article-status icon color/tooltip -- Al: "an article icon with state
// so green if approved, yellow if pending, and grey if not generated."
// Al didn't mention rejected explicitly (a product he's actively
// rejected articles for is presumably rare next to pending/approved/
// never-generated), but leaving it lumped in with grey would make a
// rejected article look identical to one that was never even attempted
// -- red, matching ArticlesPage's own approved=ok/anything-else=danger
// badge convention, keeps it visually distinct instead.
function articleIconTone(status: Product["article_status"]): string {
  if (status === "approved") return "text-ok";
  if (status === "pending") return "text-warn";
  if (status === "rejected") return "text-danger";
  return "text-ink-400";
}

function articleIconLabel(status: Product["article_status"]): string {
  if (status === "approved") return "Article approved -- click to view";
  if (status === "pending") return "Article pending review -- click to review";
  if (status === "rejected") return "Article rejected -- click to view";
  return "No article generated yet -- click to open the Article tab";
}

// Video-status icon color/tooltip -- Al's direct follow-up: "can we add
// one similar for videos. similar state. grey if none approved and
// yellow if approve but no summaries and green if approved and
// summaries. maybe a count next to the icon for number of videos."
// Unlike the article icon (one status value), video state is derived
// from two counts admin_api's list_products now returns (see that
// function's own docstring): approved_video_count = 0 is grey
// regardless of how many un-reviewed candidates exist, > 0 with
// approved_summarized_video_count still 0 is yellow, and any
// summarized approved video makes it green.
type VideoCounts = Pick<Product, "video_count" | "approved_video_count" | "approved_summarized_video_count">;

function videoIconTone(p: VideoCounts): string {
  if (p.approved_summarized_video_count > 0) return "text-ok";
  if (p.approved_video_count > 0) return "text-warn";
  return "text-ink-400";
}

function videoIconLabel(p: VideoCounts): string {
  const plural = (n: number) => (n === 1 ? "" : "s");
  if (p.approved_summarized_video_count > 0) {
    return `${p.approved_video_count} approved video${plural(p.approved_video_count)}, summarized -- click to view`;
  }
  if (p.approved_video_count > 0) {
    return `${p.approved_video_count} approved video${plural(p.approved_video_count)}, no summaries yet -- click to review`;
  }
  if (p.video_count > 0) {
    return `${p.video_count} video candidate${plural(p.video_count)} found, none approved yet -- click to review`;
  }
  return "No video candidates found yet -- click to open the Videos tab";
}

// Ports admin-site/index.html's Products tab -- same filter set
// (status/brand/search/source_platform/missing_core/missing_coverstock/
// missing_skus), same bulk rescrape action, now via the DataTable
// component's generic checkbox-select + bulk-action-bar plumbing rather
// than hand-rolled checkbox wiring. Brand filter is a free-text id
// field for now rather than a name dropdown (admin_api's list_products
// takes brand_id, and there's no GET /brands here yet the way
// consumer-site has) -- a real dropdown is a good phase-2 follow-up
// once a brands lookup endpoint exists on the admin side.
// Defaults for every filter/sort control below -- also what a bare
// `/products` (no query string at all) falls back to, so first-ever
// visits and a manually-cleared URL both behave exactly like before
// this feature existed.
const DEFAULT_STATUS: ListProductsParams["status"] = "current";
const DEFAULT_SORT: ProductSort = "popularity";

export default function ProductsPage() {
  const { show } = useToast();
  const [products, setProducts] = useState<Product[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  // Filters/sort/offset all live in the URL query string rather than
  // component state -- Al: "some state management for the filtering and
  // sorting of that list so it doesn't reset to the default over and
  // over." Plain useState reset to its initial value every time this
  // component unmounted (e.g. clicking a product row into
  // ProductDetailPage, or now the new Article icon, then hitting "Back
  // to Products") since React throws local state away on unmount --
  // there was nothing wrong with the values themselves, they just never
  // survived the round trip. Search params live on the router, not this
  // component, so they're still there when ProductsPage remounts; as a
  // bonus the filtered view is now also bookmarkable/shareable and
  // survives a manual page refresh.
  const [searchParams, setSearchParams] = useSearchParams();

  const status = (searchParams.get("status") as ListProductsParams["status"] | null) ?? DEFAULT_STATUS;
  const brandId = searchParams.get("brand_id") ?? "";
  const search = searchParams.get("search") ?? "";
  const sourcePlatform = (searchParams.get("source_platform") as SourcePlatform | null) ?? "";
  const sort = (searchParams.get("sort") as ProductSort | null) ?? DEFAULT_SORT;
  const missingCore = searchParams.get("missing_core") === "1";
  const missingCoverstock = searchParams.get("missing_coverstock") === "1";
  const missingSkus = searchParams.get("missing_skus") === "1";
  const offset = Number(searchParams.get("offset") ?? "0") || 0;

  const filters: ListProductsParams = {
    status,
    brand_id: brandId || undefined,
    search: search || undefined,
    source_platform: sourcePlatform || undefined,
    sort,
    missing_core: missingCore || undefined,
    missing_coverstock: missingCoverstock || undefined,
    missing_skus: missingSkus || undefined,
    limit: LIMIT,
    offset,
  };

  useEffect(() => {
    setLoading(true);
    setError(null);
    listProducts(filters)
      .then(setProducts)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load products."))
      .finally(() => setLoading(false));
    // Re-fetch whenever any filter or the page offset changes. Filters
    // reset offset to 0 via the individual setters below (see
    // updateFilter), so this doesn't need offset in a way that fights
    // that.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, brandId, search, sourcePlatform, sort, missingCore, missingCoverstock, missingSkus, offset]);

  // Single writer for every filter control -- deletes a param entirely
  // rather than writing its "empty" value (blank string, "false") so
  // the URL stays clean and matches DEFAULT_STATUS/DEFAULT_SORT's own
  // "absent means default" reading above. `replace: true` so toggling
  // filters doesn't pile up a back-button stop per keystroke/click --
  // only actual navigation (leaving/returning to this page) should be
  // a history entry.
  function updateFilter(key: string, value: string | boolean) {
    setSelectedIds(new Set());
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        const isEmpty = value === "" || value === false;
        if (isEmpty) {
          next.delete(key);
        } else {
          next.set(key, typeof value === "boolean" ? "1" : value);
        }
        next.delete("offset");
        return next;
      },
      { replace: true },
    );
  }

  function setOffset(next: number) {
    setSearchParams(
      (prev) => {
        const params = new URLSearchParams(prev);
        if (next > 0) params.set("offset", String(next));
        else params.delete("offset");
        return params;
      },
      { replace: true },
    );
  }

  async function handleBulkRescrape(rows: Product[]) {
    const results = await Promise.allSettled(rows.map((p) => rescrapeProduct(p.id)));
    const queued = results.filter((r) => r.status === "fulfilled" && r.value.queued).length;
    const skipped = results.length - queued;
    show(
      skipped === 0
        ? `Queued rescrape for ${queued} product${queued === 1 ? "" : "s"}.`
        : `Queued ${queued}, skipped ${skipped} (no scraper configured or a request failed).`,
      skipped === 0 ? "ok" : "info",
    );
    setSelectedIds(new Set());
  }

  const columns: Column<Product>[] = [
    {
      key: "name",
      header: "Product",
      // Links into ProductDetailPage's sub-tabs (Overview/Videos/Article/
      // Pricing/SKUs & Stock/Raw Data) -- Al: "there is no way to see all
      // the sub 'tabs' for products" (task #535). The old external-site
      // link moves to a separate small "↗" affixed after the name rather
      // than disappearing -- it's still useful (jumping straight to the
      // manufacturer's own page), just no longer the only thing a click
      // here does.
      render: (p) => (
        <span className="font-medium text-ink-800">
          <Link to={`/products/${p.id}`} className="hover:text-primary hover:underline">
            {p.brand_name} {p.name}
          </Link>{" "}
          <a href={p.url} target="_blank" rel="noreferrer" className="text-xs text-ink-400 hover:text-primary" title="View source page">
            ↗
          </a>
        </span>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (p) => <Badge tone={p.status === "current" ? "ok" : "muted"}>{p.status}</Badge>,
    },
    {
      key: "article",
      header: "Article",
      // Al: "add icons to the product list items to click on the
      // different elements that could be associated with them from the
      // list" -- the article icon is the first of these (a per-row
      // shortcut into ProductDetailPage's Article sub-tab), colored by
      // review state so the state is visible without opening the
      // product at all.
      render: (p) => (
        <Link
          to={`/products/${p.id}?tab=article`}
          title={articleIconLabel(p.article_status)}
          className={`inline-flex ${articleIconTone(p.article_status)} hover:opacity-70`}
        >
          <IconArticles className="h-5 w-5" />
        </Link>
      ),
    },
    {
      key: "videos",
      header: "Videos",
      // Same idea as Article above, extended to product_videos -- see
      // videoIconTone/videoIconLabel's own comment for the grey/yellow/
      // green derivation. The count next to the icon is every candidate
      // regardless of status (Al: "a count next to the icon for number
      // of videos"), so a grey icon showing "6" still tells you there's
      // unreviewed work waiting, distinct from a grey icon showing "0".
      render: (p) => (
        <Link
          to={`/products/${p.id}?tab=videos`}
          title={videoIconLabel(p)}
          className={`inline-flex items-center gap-1 ${videoIconTone(p)} hover:opacity-70`}
        >
          <IconVideo className="h-5 w-5" />
          <span className="text-xs font-semibold">{p.video_count}</span>
        </Link>
      ),
    },
    { key: "core_name", header: "Core", render: (p) => p.core_name ?? <span className="text-warn">missing</span> },
    {
      key: "coverstock_name",
      header: "Coverstock",
      render: (p) => p.coverstock_name ?? <span className="text-warn">missing</span>,
    },
    { key: "popularity_score", header: "Popularity", render: (p) => p.popularity_score.toFixed(1) },
    {
      key: "total_daily_movement",
      header: "Avg Daily Movement",
      render: (p) => p.total_daily_movement.toFixed(1),
    },
    {
      key: "demand_score",
      header: "Demand Score",
      // Raw value is a 0-1 percentile blend (see types.ts's own comment
      // on Product.demand_score) -- shown as a friendlier 0-100 score
      // rather than a decimal, same "don't make Al do the math" reasoning
      // as every other formatted column here.
      render: (p) => Math.round(p.demand_score * 100),
    },
    { key: "updated_at", header: "Updated", render: (p) => new Date(p.updated_at).toLocaleDateString() },
  ];

  const bulkActions: BulkAction<Product>[] = [
    { label: "Rescrape selected", onClick: handleBulkRescrape, variant: "primary" },
  ];

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold text-ink-800">Products</h1>

      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-ink-200 bg-ink-100 p-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-ink-600">Status</label>
          <select
            value={status}
            onChange={(e) => updateFilter("status", e.target.value)}
            className="rounded-md border border-ink-300 px-2 py-1.5 text-sm"
          >
            <option value="current">Current</option>
            <option value="retired">Retired</option>
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-ink-600">Brand ID</label>
          <input
            value={brandId}
            onChange={(e) => updateFilter("brand_id", e.target.value)}
            placeholder="uuid"
            className="w-32 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-ink-600">Search</label>
          <input
            value={search}
            onChange={(e) => updateFilter("search", e.target.value)}
            placeholder="Ball name…"
            className="w-40 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-ink-600">Source</label>
          <select
            value={sourcePlatform}
            onChange={(e) => updateFilter("source_platform", e.target.value)}
            className="rounded-md border border-ink-300 px-2 py-1.5 text-sm"
          >
            <option value="">All</option>
            {SOURCE_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-ink-600">Sort</label>
          <select
            value={sort}
            onChange={(e) => updateFilter("sort", e.target.value)}
            className="rounded-md border border-ink-300 px-2 py-1.5 text-sm"
          >
            {SORT_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
        <label className="flex items-center gap-1.5 pb-1.5 text-sm text-ink-600">
          <input type="checkbox" checked={missingCore} onChange={(e) => updateFilter("missing_core", e.target.checked)} />
          Missing core
        </label>
        <label className="flex items-center gap-1.5 pb-1.5 text-sm text-ink-600">
          <input
            type="checkbox"
            checked={missingCoverstock}
            onChange={(e) => updateFilter("missing_coverstock", e.target.checked)}
          />
          Missing coverstock
        </label>
        <label className="flex items-center gap-1.5 pb-1.5 text-sm text-ink-600">
          <input type="checkbox" checked={missingSkus} onChange={(e) => updateFilter("missing_skus", e.target.checked)} />
          Missing SKUs
        </label>
      </div>

      {error && <div className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{error}</div>}

      {/* Desktop/tablet: the real table, every column, unchanged. Hidden
          below md -- see ProductMobileCard below for why this isn't
          just DataTable's generic mobileCollapsible mode anymore. */}
      <div className="hidden md:block">
        <DataTable
          columns={columns}
          rows={products}
          getRowId={(p) => p.id}
          selectable
          selectedIds={selectedIds}
          onSelectionChange={setSelectedIds}
          bulkActions={bulkActions}
          emptyMessage={loading ? "Loading…" : "No products match these filters."}
        />
      </div>

      {/* Mobile: a from-scratch card, not DataTable's generic "collapse
          desktop columns into a card" mode. Al: "the mobile version of
          the list looks like an after thought... focus on showing what
          is important and assume someone will click through to get more
          details. the only thing that i think is important are the
          icons." Every column this table has beyond identity/status/the
          two review-state icons (Core, Coverstock, Popularity, Avg
          Daily Movement, Demand Score, Updated) is exactly the kind of
          "glance at a dense desktop table" data that doesn't matter for
          a quick phone check -- it's all on the product detail page a
          tap away. No selection/bulk-rescrape here either, same
          simplification; that stays a desktop workflow. */}
      <div className="flex flex-col gap-2 md:hidden">
        {loading && (
          <div className="rounded-lg border border-ink-200 bg-ink-100 px-4 py-8 text-center text-sm text-ink-400">
            Loading…
          </div>
        )}
        {!loading && products.length === 0 && (
          <div className="rounded-lg border border-ink-200 bg-ink-100 px-4 py-8 text-center text-sm text-ink-400">
            No products match these filters.
          </div>
        )}
        {products.map((p) => (
          <div key={p.id} className="flex items-center gap-3 rounded-lg border border-ink-200 bg-ink-100 p-3">
            <Link to={`/products/${p.id}`} className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium text-ink-800">
                {p.brand_name} {p.name}
              </div>
              <Badge tone={p.status === "current" ? "ok" : "muted"}>{p.status}</Badge>
            </Link>
            <div className="flex shrink-0 items-center gap-1">
              <Link
                to={`/products/${p.id}?tab=article`}
                title={articleIconLabel(p.article_status)}
                className={`-m-1 flex items-center rounded p-1 ${articleIconTone(p.article_status)} hover:bg-ink-200`}
              >
                <IconArticles className="h-6 w-6" />
              </Link>
              <Link
                to={`/products/${p.id}?tab=videos`}
                title={videoIconLabel(p)}
                className={`-m-1 flex items-center gap-1 rounded p-1 ${videoIconTone(p)} hover:bg-ink-200`}
              >
                <IconVideo className="h-6 w-6" />
                <span className="text-xs font-semibold">{p.video_count}</span>
              </Link>
            </div>
          </div>
        ))}
      </div>

      <Pagination offset={offset} limit={LIMIT} itemCount={products.length} onOffsetChange={setOffset} />
    </div>
  );
}
