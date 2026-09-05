import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getBrands, listArticles } from "../api/client";
import type { ArticleCard as ArticleCardType } from "../api/types";
import ArticleCard from "../components/ArticleCard";

const PAGE_SIZE = 24;

// Mirrors public_api/service.py's _ARTICLE_SORT_ORDER_BY exactly (values
// and all), same reasoning as consumer-site/src/pages/BrowsePage.tsx's
// own SORT_OPTIONS -- an unrecognized value never reaches the backend
// from this page.
const SORT_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "Newest" },
  { value: "oldest", label: "Oldest" },
  { value: "title_asc", label: "Title (A–Z)" },
  { value: "title_desc", label: "Title (Z–A)" },
];

// Browse/index page for the Learn section (Al's ask: "a sophisticated
// learn section with articles that are displayed in a way that is best
// from a UI/UX perspective"). Filters live in the URL query string
// (?brand_id=&q=&sort=), same shareable-link reasoning as consumer-
// site's BrowsePage.
export default function LearnIndexPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const brandId = searchParams.get("brand_id") || "";
  const search = searchParams.get("q") || "";
  const sort = searchParams.get("sort") || "";

  const [searchInput, setSearchInput] = useState(search);
  const [brands, setBrands] = useState<{ id: string; name: string }[]>([]);
  const [articles, setArticles] = useState<ArticleCardType[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getBrands().then(setBrands).catch(() => setBrands([]));
  }, []);

  useEffect(() => {
    setOffset(0);
    setArticles([]);
    setHasMore(true);
    setError(null);
    setLoading(true);
    listArticles({
      brand_id: brandId || undefined,
      search: search || undefined,
      sort: sort || undefined,
      limit: PAGE_SIZE,
      offset: 0,
    })
      .then((items) => {
        setArticles(items);
        setHasMore(items.length === PAGE_SIZE);
      })
      .catch(() => setError("Couldn't load articles right now -- try again in a moment."))
      .finally(() => setLoading(false));
  }, [brandId, search, sort]);

  function loadMore() {
    const nextOffset = offset + PAGE_SIZE;
    setLoading(true);
    listArticles({
      brand_id: brandId || undefined,
      search: search || undefined,
      sort: sort || undefined,
      limit: PAGE_SIZE,
      offset: nextOffset,
    })
      .then((items) => {
        setArticles((prev) => [...prev, ...items]);
        setOffset(nextOffset);
        setHasMore(items.length === PAGE_SIZE);
      })
      .catch(() => setError("Couldn't load more articles right now -- try again in a moment."))
      .finally(() => setLoading(false));
  }

  function updateParam(key: string, value: string) {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    setSearchParams(next);
  }

  return (
    <div className="page">
      <h1>Ball Reviews</h1>

      <div className="learn-controls">
        <select value={brandId} onChange={(e) => updateParam("brand_id", e.target.value)}>
          <option value="">All brands</option>
          {brands.map((b) => (
            <option key={b.id} value={b.id}>
              {b.name}
            </option>
          ))}
        </select>

        <select value={sort} onChange={(e) => updateParam("sort", e.target.value)} aria-label="Sort">
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
            placeholder="Search reviews..."
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
          />
          <button type="submit" className="btn">
            Search
          </button>
        </form>
      </div>

      {error && <p className="error-message">{error}</p>}

      <div className="article-grid">
        {articles.map((a) => (
          <ArticleCard key={a.article_id} article={a} />
        ))}
      </div>

      {!loading && articles.length === 0 && !error && (
        <p className="empty-state">No reviews match those filters yet.</p>
      )}

      {hasMore && (
        <button type="button" className="btn btn-load-more" onClick={loadMore} disabled={loading}>
          {loading ? "Loading..." : "Load more"}
        </button>
      )}
    </div>
  );
}
