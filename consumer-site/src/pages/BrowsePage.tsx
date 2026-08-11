import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getBrands, listProducts } from "../api/client";
import type { Brand, ProductCard as ProductCardType, ProductStatus } from "../api/types";
import ProductCard from "../components/ProductCard";

const PAGE_SIZE = 24;

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

  const [searchInput, setSearchInput] = useState(search);
  const [brands, setBrands] = useState<Brand[]>([]);
  const [products, setProducts] = useState<ProductCardType[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getBrands().then(setBrands).catch(() => setBrands([]));
  }, []);

  // Re-fetch from scratch whenever a filter changes.
  useEffect(() => {
    setOffset(0);
    setProducts([]);
    setHasMore(true);
    setError(null);
    setLoading(true);
    listProducts({ status, brand_id: brandId || undefined, search: search || undefined, limit: PAGE_SIZE, offset: 0 })
      .then((items) => {
        setProducts(items);
        setHasMore(items.length === PAGE_SIZE);
      })
      .catch(() => setError("Couldn't load balls right now -- try again in a moment."))
      .finally(() => setLoading(false));
  }, [status, brandId, search]);

  function loadMore() {
    const nextOffset = offset + PAGE_SIZE;
    setLoading(true);
    listProducts({
      status,
      brand_id: brandId || undefined,
      search: search || undefined,
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
      </div>

      <h1>{heading}</h1>

      {error && <p className="error-message">{error}</p>}

      {status === "retired" && (
        <p className="hint">
          Looking at a retired ball? Open its page for suggested current balls that play the most alike.
        </p>
      )}

      <div className="product-grid">
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
