import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getBrands, getCategories, listArticles } from "../api/client";
import type { ArticleCard as ArticleCardType, Category } from "../api/types";
import ArticleCard from "../components/ArticleCard";
import ArticleCardSkeleton from "../components/ArticleCardSkeleton";

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
  // Migration 031 -- read here rather than hardcode "Bowling Balls" /
  // "Ball Review" into this page's markup, so a future second category
  // shows up automatically. Only the first category is used for the
  // eyebrow label today (there's exactly one); this is the seam a future
  // category switcher would hang off of.
  const [category, setCategory] = useState<Category | null>(null);

  useEffect(() => {
    getBrands().then(setBrands).catch(() => setBrands([]));
    getCategories()
      .then((items) => setCategory(items[0] ?? null))
      .catch(() => setCategory(null));
  }, []);

  // ArticleDetailPage.tsx sets document.title to the article's own title on
  // mount (client-side nav between articles never re-runs prerender.ts's
  // build-time title logic). Navigating back to this index page via
  // react-router (e.g. the header logo/nav link) doesn't reload index.html,
  // so without this the tab would keep showing whatever article title was
  // last set. Reset to the site default here.
  useEffect(() => {
    document.title = "Learn | The Bowler Depot";
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

  const articleTypeName = category?.article_types[0]?.name;

  return (
    <div>
      {category ? (
        <p className="mb-1 text-[11px] font-semibold uppercase tracking-widest text-muted">
          {category.name}
          {articleTypeName ? <> &middot; {articleTypeName}</> : null}
        </p>
      ) : null}
      <h1 className="mb-8 font-display text-3xl font-semibold text-ink">Ball Reviews</h1>

      <div className="mb-8 flex flex-wrap items-center gap-3">
        <select
          value={brandId}
          onChange={(e) => updateParam("brand_id", e.target.value)}
          className="rounded-md border border-paper-border bg-transparent px-3 py-2 text-sm text-ink"
        >
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
          className="rounded-md border border-paper-border bg-transparent px-3 py-2 text-sm text-ink"
        >
          {SORT_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>

        <form
          className="flex gap-2"
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
            className="rounded-md border border-paper-border bg-transparent px-3 py-2 text-sm text-ink placeholder:text-muted"
          />
          <button
            type="submit"
            className="rounded-md border border-ink px-4 py-2 text-sm font-medium text-ink hover:bg-ink hover:text-paper"
          >
            Search
          </button>
        </form>
      </div>

      {error && <p className="py-4 text-alert">{error}</p>}

      <div className="grid grid-cols-1 gap-x-8 gap-y-10 sm:grid-cols-2 lg:grid-cols-3">
        {loading && articles.length === 0
          ? // Filter/search/sort change (or first load) -- fill the grid
            // with placeholders instead of leaving it blank, so the page
            // doesn't jump from empty to full height once results land.
            // PAGE_SIZE would overfill the viewport; one row-and-a-half
            // worth is enough to read as "loading" without over-promising
            // a full page of results.
            Array.from({ length: 9 }).map((_, i) => <ArticleCardSkeleton key={i} />)
          : articles.map((a) => <ArticleCard key={a.article_id} article={a} />)}
      </div>

      {!loading && articles.length === 0 && !error && (
        <p className="py-8 text-center text-muted">No reviews match those filters yet.</p>
      )}

      {hasMore && (
        <button
          type="button"
          onClick={loadMore}
          disabled={loading}
          className="mx-auto mt-10 block rounded-full border border-ink px-6 py-2 text-sm font-medium text-ink hover:bg-ink hover:text-paper disabled:opacity-50"
        >
          {loading ? "Loading..." : "Load more"}
        </button>
      )}
    </div>
  );
}
