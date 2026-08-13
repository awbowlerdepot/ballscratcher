import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getBrands, listProducts } from "../api/client";
import type { Brand, ProductCard as ProductCardType, ProductStatus } from "../api/types";
import ProductCard from "../components/ProductCard";

const PAGE_SIZE = 24;

type ViewMode = "grid" | "list";
const VIEW_STORAGE_KEY = "bbd_browse_view";

// Common-sense sort options (Al's ask: "lets add some common sense sort
// options for both the admin and consumer UIs") -- mirrors public_api/
// service.py's _SORT_ORDER_BY exactly, values and all, so an unrecognized
// value never reaches the backend from this page. "Featured" (the
// default, value="") keeps the existing updated_at-desc order -- labeled
// differently here than the admin-site's "recently updated" since that
// phrase describes scrape timing, not something a visitor cares about.
const SORT_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "Featured" },
  { value: "popularity", label: "Most Popular" },
  { value: "newest", label: "Newest" },
  { value: "oldest", label: "Oldest" },
  { value: "name_asc", label: "Name (A–Z)" },
  { value: "name_desc", label: "Name (Z–A)" },
];

// A layout preference, not filter data -- unlike status/brand_id/q
// this has no business in the shareable URL (a link to "Brunswick
// balls, current" should look the same whether the person who opens
// it prefers grid or list), so it's plain localStorage instead, same
// reasoning as useCompareList's persistence.
function readStoredView(): ViewMode {
  try {
    return localStorage.getItem(VIEW_STORAGE_KEY) === "list" ? "list" : "grid";
  } catch {
    return "grid";
  }
}

// Al's ask: "a focus on current bowling balls and a way to still view
// retired balls". status defaults to 'current' (matches public_api.
// list_products' own default) -- a visitor has to explicitly switch the
// toggle to see the retired catalog. Filters live in the URL query
// string (?status=&brand_id=&search=) so a filtered view is a real,
// shareable/bookmarkable link, not just client state that resets on
// reload.
export default function BrowsePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const status = (searchParams.get("status") as ProductStatus) || "current";
  const brandId = searchParams.get("brand_id") || "";
  const search = searchParams.get("q") || "";
  const sort = searchParams.get("sort") || "";

  const [searchInput, setSearchInput] = useState(search);
  const [brands, setBrands] = useState<Brand[]>([]);
  const [products, setProducts] = useState<ProductCardType[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<ViewMode>(readStoredView);

  useEffect(() => {
    try {
      localStorage.setItem(VIEW_STORAGE_KEY, view);
    } catch {
      // Private browsing / storage disabled -- the toggle still works
      // for this session, it just won't stick across visits.
    }
  }, [view]);

  useEffect(() => {
    getBrands().then(setBrands).catch(() => setBrands([]));
  }, []);

  // Re-fetch from scratch whenever a filter (or sort) changes.
  useEffect(() => {
    setOffset(0);
    setProducts([]);
    setHasMore(true);
    setError(null);
    setLoading(true);
    listProducts({
      status,
      brand_id: brandId || undefined,
      search: search || undefined,
      sort: sort || undefined,
      limit: PAGE_SIZE,
      offset: 0,
    })
      .then((items) => {
        setProducts(items);
        setHasMore(items.length === PAGE_SIZE);
      })
      .catch(() => setError("Couldn't load balls right now -- try again in a moment."))
      .finally(() => setLoading(false));
  }, [status, brandId, search, sort]);

  function loadMore() {
    const nextOffset = offset + PAGE_SIZE;
    setLoading(true);
    listProducts({
      status,
      brand_id: brandId || undefined,
      search: search || undefined,
      sort: sort || undefined,
      limit: PAGE_SIZE,
      offset: nextOffset,
    })
      .then((items) => {
        setProducts((prev) => [...prev, ...items]);
        setOffset(nextOffset);
        setHasMore(items.length === PAGE_SIZE);
      })
      .catch(() => setError("Couldn't load more balls right now -- try again in a moment."))
      .finally(() => setLoading(false));
  }

  function updateParam(key: string, value: string) {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    setSearchParams(next);
  }

  const heading = useMemo(
    () => (status === "retired" ? "Retired balls" : "Current balls"),
    [status],
  );

  return (
    <div className="page browse-page">
      <div className="browse-controls">
        <div className="status-toggle" role="tablist" aria-label="Ball status">
          <button
            type="button"
            className={status === "current" ? "active" : ""}
            onClick={() => updateParam("status", "current")}
          >
            Current
          </button>
          <button
            type="button"
            className={status === "retired" ? "active" : ""}
            onClick={() => updateParam("status", "retired")}
          >
            Retired
          </button>
        </div>

        <select value={brandId} onChange={(e) => updateParam("brand_id", e.target.value)}>
          <option value="">All brands</option>
          {brands.map((b) => (
            <option key={b.id} value={b.id}>
              {b.name}
            </option>
          ))}
        </select>

        <select
          value={sort}
          onChange={(e) => updateParam("sort", e.target.value)}
          aria-label="Sort"
        >
          {SORT_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>

        <form
          className="search-form"
          onSubmit={(e) => {
            e.preventDefault();
            updateParam("q", searchInput.trim());
          }}
        >
          <input
            type="search"
            placeholder="Search by name..."
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
          />
          <button type="submit" className="btn">
            Search
          </button>
        </form>

        <div className="status-toggle browse-view-toggle" role="tablist" aria-label="Layout">
          <button
            type="button"
            className={view === "grid" ? "active" : ""}
            onClick={() => setView("grid")}
            aria-pressed={view === "grid"}
          >
            Grid
          </button>
          <button
            type="button"
            className={view === "list" ? "active" : ""}
            onClick={() => setView("list")}
            aria-pressed={view === "list"}
          >
            List
          </button>
        </div>
      </div>

      <h1>{heading}</h1>

      {error && <p className="error-message">{error}</p>}

      {status === "retired" && (
        <p className="hint">
          Looking at a retired ball? Open its page for suggested current balls that play the most alike.
        </p>
      )}

      <div className={view === "list" ? "product-list" : "product-grid"}>
        {products.map((p) => (
          <ProductCard key={p.id} product={p} />
        ))}
      </div>

      {!loading && products.length === 0 && !error && <p className="empty-state">No balls match those filters.</p>}

      {hasMore && (
        <button type="button" className="btn btn-load-more" onClick={loadMore} disabled={loading}>
          {loading ? "Loading..." : "Load more"}
        </button>
      )}
    </div>
  );
}
