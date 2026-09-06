import { useEffect, useState } from "react";
import { listProducts, rescrapeProduct } from "../api/client";
import type { ListProductsParams, Product, ProductSort, SourcePlatform } from "../api/types";
import Badge from "../components/Badge";
import type { BulkAction, Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import Pagination from "../components/Pagination";
import { useToast } from "../components/Toast";

const LIMIT = 50;

const SORT_OPTIONS: { value: ProductSort; label: string }[] = [
  { value: "popularity", label: "Popularity" },
  { value: "total_adu", label: "Total ADU" },
  { value: "newest", label: "Newest" },
  { value: "oldest", label: "Oldest" },
  { value: "name_asc", label: "Name A-Z" },
  { value: "name_desc", label: "Name Z-A" },
];

const SOURCE_OPTIONS: SourcePlatform[] = ["netsuite", "shopify", "woocommerce", "commercebuild", "craft_cms"];

// Ports admin-site/index.html's Products tab -- same filter set
// (status/brand/search/source_platform/missing_core/missing_coverstock/
// missing_skus), same bulk rescrape action, now via the DataTable
// component's generic checkbox-select + bulk-action-bar plumbing rather
// than hand-rolled checkbox wiring. Brand filter is a free-text id
// field for now rather than a name dropdown (admin_api's list_products
// takes brand_id, and there's no GET /brands here yet the way
// consumer-site has) -- a real dropdown is a good phase-2 follow-up
// once a brands lookup endpoint exists on the admin side.
export default function ProductsPage() {
  const { show } = useToast();
  const [products, setProducts] = useState<Product[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [offset, setOffset] = useState(0);

  const [status, setStatus] = useState<ListProductsParams["status"]>("current");
  const [brandId, setBrandId] = useState("");
  const [search, setSearch] = useState("");
  const [sourcePlatform, setSourcePlatform] = useState<SourcePlatform | "">("");
  const [sort, setSort] = useState<ProductSort>("popularity");
  const [missingCore, setMissingCore] = useState(false);
  const [missingCoverstock, setMissingCoverstock] = useState(false);
  const [missingSkus, setMissingSkus] = useState(false);

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
    // resetAndSet), so this doesn't need offset in a way that fights
    // that.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, brandId, search, sourcePlatform, sort, missingCore, missingCoverstock, missingSkus, offset]);

  function resetAndSet<T>(setter: (v: T) => void) {
    return (value: T) => {
      setOffset(0);
      setSelectedIds(new Set());
      setter(value);
    };
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
      render: (p) => (
        <a href={p.url} target="_blank" rel="noreferrer" className="font-medium text-ink-800 hover:text-primary">
          {p.brand_name} {p.name}
        </a>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (p) => <Badge tone={p.status === "current" ? "ok" : "muted"}>{p.status}</Badge>,
    },
    { key: "core_name", header: "Core", render: (p) => p.core_name ?? <span className="text-warn">missing</span> },
    {
      key: "coverstock_name",
      header: "Coverstock",
      render: (p) => p.coverstock_name ?? <span className="text-warn">missing</span>,
    },
    { key: "popularity_score", header: "Popularity", render: (p) => p.popularity_score.toFixed(1) },
    { key: "total_adu", header: "ADU", render: (p) => p.total_adu.toFixed(1) },
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
            onChange={(e) => resetAndSet(setStatus)(e.target.value as ListProductsParams["status"])}
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
            onChange={(e) => resetAndSet(setBrandId)(e.target.value)}
            placeholder="uuid"
            className="w-32 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-ink-600">Search</label>
          <input
            value={search}
            onChange={(e) => resetAndSet(setSearch)(e.target.value)}
            placeholder="Ball name…"
            className="w-40 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-ink-600">Source</label>
          <select
            value={sourcePlatform}
            onChange={(e) => resetAndSet(setSourcePlatform)(e.target.value as SourcePlatform | "")}
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
            onChange={(e) => resetAndSet(setSort)(e.target.value as ProductSort)}
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
          <input type="checkbox" checked={missingCore} onChange={(e) => resetAndSet(setMissingCore)(e.target.checked)} />
          Missing core
        </label>
        <label className="flex items-center gap-1.5 pb-1.5 text-sm text-ink-600">
          <input
            type="checkbox"
            checked={missingCoverstock}
            onChange={(e) => resetAndSet(setMissingCoverstock)(e.target.checked)}
          />
          Missing coverstock
        </label>
        <label className="flex items-center gap-1.5 pb-1.5 text-sm text-ink-600">
          <input type="checkbox" checked={missingSkus} onChange={(e) => resetAndSet(setMissingSkus)(e.target.checked)} />
          Missing SKUs
        </label>
      </div>

      {error && <div className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{error}</div>}

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

      <Pagination offset={offset} limit={LIMIT} itemCount={products.length} onOffsetChange={setOffset} />
    </div>
  );
}
