import { Fragment, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { categoryAncestors, childCategories, getBrands, getCategories, listArticles, topLevelCategories } from "../api/client";
import type { ArticleCard as ArticleCardType, Category } from "../api/types";
import ArticleCard from "../components/ArticleCard";
import ArticleCardSkeleton from "../components/ArticleCardSkeleton";
import InFeedAd from "../components/InFeedAd";

// One in-feed ad row per this many article cards (Al: "can we do in feed
// ads for the article list?"). MUST be a multiple of both 2 and 3 (i.e.
// of their LCM, 6) -- the grid below is 1/2/3 columns depending on
// viewport (sm:grid-cols-2 lg:grid-cols-3), and the ad's col-span-full
// wrapper forces a row break wherever it lands. Al: "the 8th ball is
// the last and the 9th gets moved down ... in rows of 3 that causes an
// issue because there is a row of 2 then one missing then an entire
// row missing" -- confirmed live: 8 isn't a multiple of 3, so the ad
// landed mid-row, leaving the preceding row short and visually broken
// regardless of whether the ad itself ends up filled, collapsed, or
// still pending. 6 is the smallest interval that always lands exactly
// on a row boundary at every breakpoint.
const IN_FEED_AD_INTERVAL = 6;

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
// (?category_id=&brand_id=&q=&sort=), same shareable-link reasoning as
// consumer-site's BrowsePage.
export default function LearnIndexPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const brandId = searchParams.get("brand_id") || "";
  const search = searchParams.get("q") || "";
  const sort = searchParams.get("sort") || "";
  const categoryParam = searchParams.get("category_id") || "";

  const [searchInput, setSearchInput] = useState(search);
  const [brands, setBrands] = useState<{ id: string; name: string }[]>([]);
  const [articles, setArticles] = useState<ArticleCardType[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Nested categories (migrations 031 + 037). The selected category is
  // ?category_id= (a header tab or a subcategory chip), defaulting to the
  // first top-level category -- the same tab Nav.tsx highlights. Articles
  // are filtered to its whole subtree (public_api's list_articles).
  // categoriesReady gates the article fetch so the default category is
  // known before the first request, instead of loading everything and
  // then re-fetching; if categories fail to load, articles load unfiltered.
  const [categories, setCategories] = useState<Category[]>([]);
  const [categoriesReady, setCategoriesReady] = useState(false);

  useEffect(() => {
    getBrands().then(setBrands).catch(() => setBrands([]));
    getCategories()
      .then(setCategories)
      .catch(() => setCategories([]))
      .finally(() => setCategoriesReady(true));
  }, []);

  const category =
    categories.find((c) => c.id === categoryParam) ?? topLevelCategories(categories)[0] ?? null;
  const categoryId = category?.id || "";
  const ancestors = category ? categoryAncestors(categories, category) : [];
  // Chip row: the selected category's subcategories, or -- when a leaf
  // subcategory is selected -- its siblings, so the reader can hop across
  // without going back up. The "All" chip is that group's parent.
  const chipParent = category
    ? childCategories(categories, category.id).length > 0
      ? category
      : ancestors[ancestors.length - 1] ?? null
    : null;
  const chips = chipParent ? childCategories(categories, chipParent.id) : [];
  // Brand only means something for product-backed categories (Bowling
  // Balls, product_type "ball"); video categories like Bowling Tips have
  // product_type null. undefined (an older API) keeps the filter.
  const rootCategory = ancestors[0] ?? category;
  const showBrandFilter = rootCategory?.product_type !== null;

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
    if (!categoriesReady) return;
    setOffset(0);
    setArticles([]);
    setHasMore(true);
    setError(null);
    setLoading(true);
    listArticles({
      category_id: categoryId || undefined,
      brand_id: (showBrandFilter && brandId) || undefined,
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
  }, [categoriesReady, categoryId, brandId, showBrandFilter, search, sort]);

  function loadMore() {
    const nextOffset = offset + PAGE_SIZE;
    setLoading(true);
    listArticles({
      category_id: categoryId || undefined,
      brand_id: (showBrandFilter && brandId) || undefined,
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

  function categoryHref(id: string): string {
    const next = new URLSearchParams(searchParams);
    next.set("category_id", id);
    return `/?${next.toString()}`;
  }

  // Eyebrow: the path above a subcategory ("Bowling Tips · Spare
  // Shooting"), or for a top-level category its article type ("Ball
  // Review"), as before migration 037.
  const eyebrow = ancestors.length
    ? ancestors.map((a) => a.name).join(" · ")
    : category?.article_types[0]?.name;

  return (
    <div>
      {eyebrow ? (
        <p className="mb-1 text-[11px] font-semibold uppercase tracking-widest text-muted">{eyebrow}</p>
      ) : null}
      <h1 className="mb-6 font-display text-3xl font-semibold text-ink">{category?.name ?? "Articles"}</h1>

      {chipParent && chips.length > 0 ? (
        <div className="mb-6 flex flex-wrap gap-2">
          {[chipParent, ...chips].map((c) => {
            const active = c.id === categoryId;
            return (
              <Link
                key={c.id}
                to={categoryHref(c.id)}
                aria-current={active ? "page" : undefined}
                className={`rounded-full border px-3 py-1 text-sm hover:no-underline ${
                  active ? "border-ink bg-ink text-paper" : "border-paper-border text-ink hover:border-ink"
                }`}
              >
                {c.id === chipParent.id ? `All ${c.name}` : c.name}
              </Link>
            );
          })}
        </div>
      ) : null}

      <div className="mb-8 flex flex-wrap items-center gap-3">
        {showBrandFilter ? (
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
        ) : null}

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
            placeholder="Search articles..."
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
          : // In-feed ad every IN_FEED_AD_INTERVAL cards (Al: "can we do in
            // feed ads for the article list?") -- inserted AFTER a card at
            // that position, never before the first one. Appending more
            // results via loadMore() only adds new entries past whatever
            // was already rendered, so earlier ad placements never remount.
            articles.map((a, i) => (
              // The fragment itself (not just ArticleCard inside it) needs
              // the stable key -- react-router/React reconciles this
              // .map() output as one flat list of top-level elements.
              <Fragment key={a.article_id}>
                <ArticleCard article={a} />
                {(i + 1) % IN_FEED_AD_INTERVAL === 0 ? <InFeedAd key={`ad-${i}`} /> : null}
              </Fragment>
            ))}
      </div>

      {!loading && articles.length === 0 && !error && (
        <p className="py-8 text-center text-muted">No articles match those filters yet.</p>
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
