import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { listBrands, listProductsPage, rescrapeProduct } from "../api/client";
import type { Brand, ListProductsParams, Product, ProductSort, SourcePlatform } from "../api/types";
import Badge from "../components/Badge";
import type { BulkAction, Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import { IconArticles, IconVideo } from "../components/icons";
import Pagination from "../components/Pagination";
import Skeleton from "../components/Skeleton";
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

// Filter bar tidy-up (runbook 6fb). Al: "can we improve the filtering on the
// products page. the types of filters are good but it just needs tidied up
// and we haven't revisited it since they were created". Source shows which
// brands each scraper platform covers instead of the platform's internal
// name; the value sent to the API is unchanged.
const SOURCE_OPTIONS: { value: SourcePlatform; label: string }[] = [
  { value: "craft_cms", label: "Brunswick · Radical · DV8" },
  { value: "commercebuild", label: "Storm · Roto Grip · 900 Global" },
  { value: "netsuite", label: "Motiv" },
  { value: "shopify", label: "Hammer · Track · Ebonite · Columbia 300" },
  { value: "woocommerce", label: "Swag" },
];
const MISSING_CHIPS: { key: "missing_core" | "missing_coverstock" | "missing_skus"; label: string }[] = [
  { key: "missing_core", label: "Core" },
  { key: "missing_coverstock", label: "Coverstock" },
  { key: "missing_skus", label: "SKUs" },
];

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
  // Total matching row count across every page, not just this one --
  // Al: "it doesn't have number of pages and first and last buttons."
  // null until the first response comes back (or on error), same
  // "don't show a real number until we actually know one" convention
  // this page already used before for products itself.
  const [total, setTotal] = useState<number | null>(null);
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

  // "all" in the URL = no status filter (the API treats a missing status as any).
  const statusParam = searchParams.get("status");
  const status = statusParam === "all" ? undefined : ((statusParam as ListProductsParams["status"] | null) ?? DEFAULT_STATUS);
  const brandId = searchParams.get("brand_id") ?? "";
  const search = searchParams.get("search") ?? "";
  const sourcePlatform = (searchParams.get("source_platform") as SourcePlatform | null) ?? "";
  const sort = (searchParams.get("sort") as ProductSort | null) ?? DEFAULT_SORT;
  const missingCore = searchParams.get("missing_core") === "1";
  const missingCoverstock = searchParams.get("missing_coverstock") === "1";
  const missingSkus = searchParams.get("missing_skus") === "1";
  const offset = Number(searchParams.get("offset") ?? "0") || 0;
  const [brands, setBrands] = useState<Brand[]>([]);
  useEffect(() => {
    listBrands()
      .then((b) => setBrands([...b].sort((x, y) => x.name.localeCompare(y.name))))
      .catch(() => undefined);
  }, []);
  // Search box: local text, written to the URL after a short pause, so the
  // list doesn't refetch on every keystroke.
  const [searchText, setSearchText] = useState(search);
  const searchTimer = useRef<number | undefined>(undefined);
  useEffect(() => setSearchText(search), [search]);
  // Al: "can we add something to the products list view that shows if they
  // are published or not". "published" in the URL is "yes" or "no";
  // absent means any. The API's published filter already existed.
  const publishedParam = searchParams.get("published");
  const published = publishedParam === "yes" ? true : publishedParam === "no" ? false : undefined;

  const filters: ListProductsParams = {
    status,
    published,
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
    listProductsPage(filters)
      .then((r) => {
        setProducts(r.items);
        setTotal(r.total);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load products."))
      .finally(() => setLoading(false));
    // Re-fetch whenever any filter or the page offset changes. Filters
    // reset offset to 0 via the individual setters below (see
    // updateFilter), so this doesn't need offset in a way that fights
    // that.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, published, brandId, search, sourcePlatform, sort, missingCore, missingCoverstock, missingSkus, offset]);

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
          <Link
            to={`/products/${p.id}`}
            state={{ productsListSearch: searchParams.toString() }}
            className="hover:text-primary hover:underline"
          >
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
      // Whether the ball is published, i.e. shown on the consumer site and
      // returned by the public and partner APIs.
      key: "published",
      header: "Published",
      render: (p) =>
        p.published ? <Badge tone="ok">published</Badge> : <Badge tone="pending">not published</Badge>,
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
          state={{ productsListSearch: searchParams.toString() }}
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
          state={{ productsListSearch: searchParams.toString() }}
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

  // Anything but the defaults (current, any, all, no missing-data chips,
  // no search); sort isn't a filter, so it stays when clearing.
  const FILTER_KEYS = ["status", "published", "brand_id", "search", "source_platform", "missing_core", "missing_coverstock", "missing_skus"];
  const filtersActive = FILTER_KEYS.some((k) => searchParams.get(k));
  function clearFilters() {
    setSelectedIds(new Set());
    setSearchText("");
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams();
        if (prev.get("sort")) next.set("sort", prev.get("sort")!);
        return next;
      },
      { replace: true },
    );
  }

  const bulkActions: BulkAction<Product>[] = [
    { label: "Rescrape selected", onClick: handleBulkRescrape, variant: "primary" },
  ];

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold text-ink-800">Products</h1>

      <div className="flex flex-col gap-3 rounded-lg border border-ink-200 bg-ink-100 p-3">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[14rem] flex-1">
            <label className="mb-1 block text-xs font-medium text-ink-600">Search</label>
            <div className="relative">
              <input
                value={searchText}
                onChange={(e) => {
                  const v = e.target.value;
                  setSearchText(v);
                  window.clearTimeout(searchTimer.current);
                  searchTimer.current = window.setTimeout(() => updateFilter("search", v.trim()), 300);
                }}
                placeholder="Ball name…"
                className="w-full rounded-md border border-ink-300 px-2 py-1.5 pr-7 text-sm"
              />
              {searchText && (
                <button
                  type="button"
                  onClick={() => {
                    setSearchText("");
                    updateFilter("search", "");
                  }}
                  className="absolute right-1.5 top-1/2 -translate-y-1/2 px-1 text-ink-400 hover:text-ink-700"
                  aria-label="Clear search"
                >
                  ✕
                </button>
              )}
            </div>
          </div>
          <FilterSelect label="Brand" value={brandId} onChange={(v) => updateFilter("brand_id", v)}>
            <option value="">All brands</option>
            {brands.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
            {brandId && !brands.some((b) => b.id === brandId) && <option value={brandId}>{brandId}</option>}
          </FilterSelect>
          <FilterSelect
            label="Status"
            value={statusParam === "all" ? "all" : (status ?? "current")}
            onChange={(v) => updateFilter("status", v === DEFAULT_STATUS ? "" : v)}
          >
            <option value="current">Current</option>
            <option value="retired">Retired</option>
            <option value="all">All</option>
          </FilterSelect>
          <FilterSelect
            label="Published"
            value={publishedParam === "yes" || publishedParam === "no" ? publishedParam : ""}
            onChange={(v) => updateFilter("published", v)}
          >
            <option value="">Any</option>
            <option value="yes">Published</option>
            <option value="no">Not published</option>
          </FilterSelect>
          <FilterSelect label="Source" value={sourcePlatform} onChange={(v) => updateFilter("source_platform", v)}>
            <option value="">All sources</option>
            {SOURCE_OPTIONS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </FilterSelect>
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t border-ink-200 pt-3">
          <span className="text-xs font-medium text-ink-600">Missing data</span>
          {MISSING_CHIPS.map((c) => {
            const on = searchParams.get(c.key) === "1";
            return (
              <button
                key={c.key}
                type="button"
                onClick={() => updateFilter(c.key, !on)}
                aria-pressed={on}
                className={`rounded-full border px-3 py-1 text-xs font-medium ${
                  on ? "border-warn bg-warn-light text-warn" : "border-ink-300 text-ink-600 hover:border-ink-400"
                }`}
              >
                {on ? "✓ " : ""}
                {c.label}
              </button>
            );
          })}
          <div className="ml-auto flex flex-wrap items-center gap-3">
            <span className="text-sm text-ink-500">
              {total != null ? `${total.toLocaleString()} product${total === 1 ? "" : "s"}` : ""}
            </span>
            {filtersActive && (
              <button type="button" onClick={clearFilters} className="text-sm font-medium text-primary hover:underline">
                Clear filters
              </button>
            )}
            <label className="flex items-center gap-2 text-xs font-medium text-ink-600">
              Sort
              <select
                value={sort}
                onChange={(e) => updateFilter("sort", e.target.value === DEFAULT_SORT ? "" : e.target.value)}
                className="rounded-md border border-ink-300 px-2 py-1.5 text-sm font-normal"
              >
                {SORT_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </div>
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
          emptyMessage="No products match these filters."
          loading={loading}
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
        {loading &&
          Array.from({ length: 6 }).map((_, i) => (
            <div key={`skeleton-${i}`} className="flex items-center gap-3 rounded-lg border border-ink-200 bg-ink-100 p-3">
              <div className="min-w-0 flex-1">
                <Skeleton className="mb-1.5 h-4 w-3/4" />
                <Skeleton className="h-4 w-12" />
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Skeleton className="h-6 w-6 rounded-full" />
                <Skeleton className="h-6 w-6 rounded-full" />
              </div>
            </div>
          ))}
        {!loading && products.length === 0 && (
          <div className="rounded-lg border border-ink-200 bg-ink-100 px-4 py-8 text-center text-sm text-ink-400">
            No products match these filters.
          </div>
        )}
        {products.map((p) => (
          <div key={p.id} className="flex items-center gap-3 rounded-lg border border-ink-200 bg-ink-100 p-3">
            <Link
              to={`/products/${p.id}`}
              state={{ productsListSearch: searchParams.toString() }}
              className="min-w-0 flex-1"
            >
              <div className="truncate text-sm font-medium text-ink-800">
                {p.brand_name} {p.name}
              </div>
              <Badge tone={p.status === "current" ? "ok" : "muted"}>{p.status}</Badge>
            </Link>
            <div className="flex shrink-0 items-center gap-1">
              <Link
                to={`/products/${p.id}?tab=article`}
                state={{ productsListSearch: searchParams.toString() }}
                title={articleIconLabel(p.article_status)}
                className={`-m-1 flex items-center rounded p-1 ${articleIconTone(p.article_status)} hover:bg-ink-200`}
              >
                <IconArticles className="h-6 w-6" />
              </Link>
              <Link
                to={`/products/${p.id}?tab=videos`}
                state={{ productsListSearch: searchParams.toString() }}
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

      <Pagination
        offset={offset}
        limit={LIMIT}
        itemCount={products.length}
        onOffsetChange={setOffset}
        total={total ?? undefined}
      />
    </div>
  );
}

function FilterSelect({
  label,
  value,
  onChange,
  children,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  children: ReactNode;
}) {
  return (
    <div>
      <label className="mb-1 block text-xs font-medium text-ink-600">{label}</label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={`max-w-[16rem] rounded-md border px-2 py-1.5 text-sm ${value ? "border-primary text-ink-800" : "border-ink-300 text-ink-600"}`}
      >
        {children}
      </select>
    </div>
  );
}
