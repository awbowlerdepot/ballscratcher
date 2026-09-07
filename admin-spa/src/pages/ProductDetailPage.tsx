import { useEffect, useState } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";
import {
  approveArticle,
  approvePriceSource,
  approveVideoCandidate,
  checkPriceForProduct,
  createProductPriceSource,
  deleteProductPriceSource,
  discoverPriceSourcesForProduct,
  discoverVideosForProduct,
  generateArticle,
  getArticle,
  getPriceHistory,
  getProduct,
  getSkuStockHistory,
  listArticleImageCandidates,
  listArticles,
  listPriceSites,
  listProductPriceSources,
  listVideoCandidates,
  reassignVideoCandidate,
  regenerateArticleActionShot,
  regenerateArticleImages,
  regenerateArticleProductShot,
  regenerateArticleText,
  rejectArticle,
  rejectPriceSource,
  rejectVideoCandidate,
  rescrapeProduct,
  resyncArticleNow,
  restorePriceSource,
  restoreVideoCandidate,
  reorderProductImages,
  refreshVideoSummary,
  selectArticleImageCandidate,
  setArticleBigcommerceSync,
  setProductPublished,
  syncArticleNow,
  updateProductImage,
} from "../api/client";
import type {
  Article,
  ArticleImageCandidate,
  ArticleListItem,
  PriceHistoryResult,
  PriceSite,
  ProductDetail,
  ProductImage,
  ProductPriceSource,
  SkuStockHistoryResult,
  VideoCandidate,
} from "../api/types";
import Badge from "../components/Badge";
import Button from "../components/Button";
import PriceHistoryChart from "../components/charts/PriceHistoryChart";
import SkuStockChart from "../components/charts/SkuStockChart";
import type { Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import Modal from "../components/Modal";
import { useToast } from "../components/Toast";
import { ArticlePreview } from "./ArticlesPage";

type DetailTab = "overview" | "videos" | "article" | "pricing" | "skus" | "raw";

// Kept in one place so the ?tab= reader below and the tab buttons/links
// that write it (ProductsPage's new Article-status icon in particular --
// Al: "click the icon it takes you to the article") always agree on what
// a valid value looks like.
const DETAIL_TABS: DetailTab[] = ["overview", "videos", "article", "pricing", "skus", "raw"];

function isDetailTab(value: string | null): value is DetailTab {
  return value !== null && (DETAIL_TABS as string[]).includes(value);
}

function fmtDate(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

function fmtDateOnly(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleDateString() : "—";
}

function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

// Ports admin-site/index.html's loadProductDetailInto -- the one place in
// this project a single product's images/core/description, video
// candidates + rollup, ball-review article, price tracking, and SKU/stock
// data all live together, grouped into sub-tabs (Al: "the admin product
// details are getting a bit cluttered can we clean that up and maybe put
// the different sections in tabs", task #308). admin-spa never had this
// at all -- ProductsPage's list only supported bulk actions, with no way
// to drill into one product -- see task #535. Everything is fetched once
// up front (Promise.all, same as admin-site) rather than per-tab-click,
// so switching tabs is instant and doesn't re-hit the API; a mutation
// (approve a video, select an image candidate, reorder images...)
// re-runs the same load() to keep every tab's view of this product in
// sync, same "reload the whole panel" convention admin-site's own
// loadProductDetailInto call sites use.
export default function ProductDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { show } = useToast();

  // "Back to Products" used to always land on a bare /products, which
  // reset ProductsPage's own URL-search-param-backed filter/sort/page
  // state back to defaults -- the exact "resetting to default over and
  // over" problem 6ab.26 fixed for navigating AWAY from the list, just
  // showing up again on the way BACK. Every Link into this page from
  // ProductsPage now passes the list's current query string along as
  // router state (productsListSearch); we read it back here to rebuild
  // the same URL. Falls back to a bare /products for any other way of
  // reaching this page (direct link, bookmark, browser refresh, which
  // drops router state) -- harmless, just the pre-fix behavior.
  const location = useLocation();
  const productsListSearch = (location.state as { productsListSearch?: string } | null)?.productsListSearch;
  const backToProductsHref = productsListSearch ? `/products?${productsListSearch}` : "/products";

  // ?tab= lets a link land directly on a sub-tab -- e.g. ProductsPage's
  // Article-status icon links to `/products/{id}?tab=article` rather
  // than always dropping onto Overview and making Al click Article
  // himself every time. Falls back to "overview" for a missing or
  // unrecognized value, same "harmless if wrong" convention every other
  // filter/sort value elsewhere in admin-spa already follows.
  const [searchParams, setSearchParams] = useSearchParams();
  const [tab, setTabState] = useState<DetailTab>(() => {
    const fromUrl = searchParams.get("tab");
    return isDetailTab(fromUrl) ? fromUrl : "overview";
  });

  // Keeps the URL in sync with manual tab clicks too (replace, not push,
  // so clicking through Overview -> Videos -> Article doesn't pile up
  // separate back-button stops) -- makes the current tab bookmarkable/
  // shareable/refreshable, not just reachable via an inbound link.
  //
  // REAL INCIDENT (2026-09-07, Al): "we are still losing context when
  // going back from a product detail page. it is a real time suck."
  // The productsListSearch fix above only reads location.state once on
  // mount, but setSearchParams's own navigate() call resets location.
  // state to undefined unless you explicitly pass it back through --
  // and this is the ONLY thing on this page that calls setSearchParams.
  // So the very first tab click after landing (Overview -> Article, or
  // any of them) silently wiped out productsListSearch, long before
  // anyone actually clicked "Back to Products" -- since browsing tabs
  // is the entire point of this page, that made the fix look like it
  // never worked at all. Fixed by forwarding the CURRENT location.state
  // through every setSearchParams call here, so tab clicks stop erasing
  // it.
  function setTab(next: DetailTab) {
    setTabState(next);
    setSearchParams((prev) => {
      const params = new URLSearchParams(prev);
      params.set("tab", next);
      return params;
    }, { replace: true, state: location.state });
  }
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [product, setProduct] = useState<ProductDetail | null>(null);
  const [videos, setVideos] = useState<VideoCandidate[]>([]);
  const [articleItem, setArticleItem] = useState<ArticleListItem | null>(null);
  const [article, setArticle] = useState<Article | null>(null);
  const [articleCandidates, setArticleCandidates] = useState<ArticleImageCandidate[]>([]);
  const [priceSources, setPriceSources] = useState<ProductPriceSource[]>([]);
  const [priceHistory, setPriceHistory] = useState<PriceHistoryResult | null>(null);
  const [skuStockHistory, setSkuStockHistory] = useState<SkuStockHistoryResult | null>(null);
  const [priceSites, setPriceSites] = useState<PriceSite[]>([]);

  const [rejectVideoTarget, setRejectVideoTarget] = useState<VideoCandidate | null>(null);
  const [rejectVideoReason, setRejectVideoReason] = useState("");
  const [reassignTarget, setReassignTarget] = useState<VideoCandidate | null>(null);
  const [reassignProductId, setReassignProductId] = useState("");
  const [rejectArticleOpen, setRejectArticleOpen] = useState(false);
  const [rejectArticleReason, setRejectArticleReason] = useState("");
  const [rejectPriceSourceTarget, setRejectPriceSourceTarget] = useState<ProductPriceSource | null>(null);
  const [rejectPriceSourceReason, setRejectPriceSourceReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [discoverResult, setDiscoverResult] = useState<string | null>(null);
  const [discoverPriceResult, setDiscoverPriceResult] = useState<string | null>(null);
  const [manualSiteId, setManualSiteId] = useState("");
  const [manualUrl, setManualUrl] = useState("");
  const [manualSelector, setManualSelector] = useState("");
  const [addingManualSource, setAddingManualSource] = useState(false);

  // Promise.all, not sequential (REVERTED 2026-09-06 -- see 6ab.27 in
  // DEPLOY_RUNBOOK.md): this page originally fired six separate admin_api
  // calls one at a time specifically because this AWS account's Lambda
  // UnreservedConcurrentExecutions was capped at 10 account-wide (AWS's
  // default low tier for a new/unverified account), and a burst of
  // concurrent invocations against AdminApiFunction had already been seen
  // to exhaust that pool and come back as a bare 503 from AdminHttpApi
  // with nothing in CloudWatch. Al requested and AWS approved a
  // concurrency quota increase to the standard 1000 (see AdminApiFunction's
  // own template.yaml comment, RESOLVED 2026-09-06) -- with that much
  // headroom, six concurrent invocations from one page load is no longer
  // a meaningful risk, so this reverts to the faster parallel load (the
  // max of six round-trips instead of their sum).
  //
  // Each fetch still fails independently rather than aborting the whole
  // load -- a stumble on, say, price-history shouldn't blank a product
  // that loaded fine; it just leaves that one sub-tab empty and reports
  // the failure via the toast instead of the whole-page error view.
  // loadPart already swallows its own errors internally, so Promise.all
  // here is safe -- no individual call rejecting can short-circuit the
  // others or reject the outer Promise.all.
  async function load() {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const p = await getProduct(id);
      setProduct(p);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load product.");
      setLoading(false);
      return;
    }

    async function loadPart<T>(label: string, fn: () => Promise<T>, apply: (result: T) => void) {
      try {
        apply(await fn());
      } catch (err) {
        show(err instanceof Error ? `${label}: ${err.message}` : `Failed to load ${label}.`, "danger");
      }
    }

    await Promise.all([
      loadPart("videos", () => listVideoCandidates({ product_id: id, status: "all", limit: 200 }), (r) =>
        setVideos(r.items),
      ),
      loadPart("article", () => listArticles({ product_id: id, status: "all", limit: 1 }), (r) =>
        setArticleItem(r[0] ?? null),
      ),
      loadPart("price sources", () => listProductPriceSources(id, "all"), setPriceSources),
      loadPart("price history", () => getPriceHistory(id), setPriceHistory),
      loadPart("SKU stock history", () => getSkuStockHistory(id), setSkuStockHistory),
      // Price Sites registry -- feeds the Pricing sub-tab's manual-add
      // dropdown (site name -> id). Same catalog-wide, active-only list
      // PriceSitesPage itself fetches; harmless to re-fetch per product
      // page load since it's small and read-mostly (see PriceSite's own
      // comment in types.ts).
      loadPart("price sites", () => listPriceSites(), setPriceSites),
    ]);
    setLoading(false);
  }

  // Article full detail + image candidates depend on articleItem (set by
  // load() above) rather than living inside load() itself, so this stays
  // its own effect regardless of load()'s parallel/sequential posture.
  // The two calls themselves are independent of each other (neither
  // result feeds the other) -- fired via Promise.all now that this
  // account isn't concurrency-constrained (see 6ab.27 in DEPLOY_RUNBOOK.md
  // and load()'s own comment above).
  useEffect(() => {
    if (!articleItem) {
      setArticle(null);
      setArticleCandidates([]);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const [full, candidates] = await Promise.all([
          getArticle(articleItem.id),
          listArticleImageCandidates(articleItem.id),
        ]);
        if (cancelled) return;
        setArticle(full);
        setArticleCandidates(candidates);
      } catch (err) {
        if (!cancelled) show(err instanceof Error ? `article detail: ${err.message}` : "Failed to load article detail.", "danger");
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [articleItem?.id]);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  if (loading && !product) {
    return <p className="text-sm text-ink-500">Loading…</p>;
  }
  if (error) {
    return <div className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{error}</div>;
  }
  if (!product || !id) {
    return <p className="text-sm text-ink-500">Product not found.</p>;
  }

  const detailTabs: { name: DetailTab; label: string }[] = [
    { name: "overview", label: "Overview" },
    { name: "videos", label: `Videos (${videos.length})` },
    { name: "article", label: `Article${articleItem ? ` (${articleItem.status})` : ""}` },
    { name: "pricing", label: `Pricing (${priceSources.length})` },
    { name: "skus", label: `SKUs & Stock (${product.skus.length})` },
    { name: "raw", label: "Raw Data" },
  ];

  async function handleTogglePublished() {
    if (!product) return;
    try {
      await setProductPublished(product.id, !product.published);
      show(!product.published ? "Published." : "Unpublished.", "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Failed to toggle published.", "danger");
    }
  }

  // --- Images (Overview) ------------------------------------------------

  async function handleMoveImage(images: ProductImage[], idx: number, direction: -1 | 1) {
    const next = [...images];
    const swapWith = idx + direction;
    if (swapWith < 0 || swapWith >= next.length) return;
    [next[idx], next[swapWith]] = [next[swapWith], next[idx]];
    try {
      await reorderProductImages(id!, next.map((img) => img.id));
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Reorder failed.", "danger");
    }
  }

  async function handleSetThumbnail(imageId: string) {
    try {
      await updateProductImage(id!, imageId, { is_thumbnail: true });
      show("Thumbnail updated.", "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Failed to set thumbnail.", "danger");
    }
  }

  async function handleToggleVisibility(imageId: string, currentlyVisible: boolean) {
    try {
      await updateProductImage(id!, imageId, { is_visible: !currentlyVisible });
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Failed to toggle visibility.", "danger");
    }
  }

  // --- Videos -------------------------------------------------------------

  async function handleDiscoverVideos() {
    setDiscoverResult(null);
    try {
      const result = await discoverVideosForProduct(id!);
      setDiscoverResult(
        result.queued ? "Queued -- check back in a bit for new candidates." : (result.reason ?? "Not queued."),
      );
    } catch (err) {
      setDiscoverResult(err instanceof Error ? err.message : "Failed to queue video search.");
    }
  }

  async function handleRefreshRollup() {
    try {
      const result = await refreshVideoSummary(id!);
      show(
        result.rollup_regenerated
          ? `Rollup refreshed from ${result.video_count} video(s).`
          : (result.reason ?? "Nothing to refresh."),
        result.rollup_regenerated ? "ok" : "info",
      );
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Refresh failed.", "danger");
    }
  }

  async function handleApproveVideo(v: VideoCandidate) {
    try {
      await approveVideoCandidate(v.id);
      show("Approved.", "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Approve failed.", "danger");
    }
  }

  async function confirmRejectVideo() {
    if (!rejectVideoTarget) return;
    setSubmitting(true);
    try {
      await rejectVideoCandidate(rejectVideoTarget.id, rejectVideoReason || undefined);
      show("Rejected.", "ok");
      setRejectVideoTarget(null);
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Reject failed.", "danger");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRestoreVideo(v: VideoCandidate) {
    try {
      await restoreVideoCandidate(v.id);
      show("Restored to pending.", "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Restore failed.", "danger");
    }
  }

  async function confirmReassignVideo() {
    if (!reassignTarget || !reassignProductId) return;
    setSubmitting(true);
    try {
      const result = await reassignVideoCandidate(reassignTarget.id, reassignProductId);
      show(
        result.merged_with_existing ? "Reassigned -- merged with an existing candidate." : "Reassigned.",
        "ok",
      );
      setReassignTarget(null);
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Reassign failed.", "danger");
    } finally {
      setSubmitting(false);
    }
  }

  // --- Article --------------------------------------------------------

  async function handleGenerateArticle() {
    try {
      const result = await generateArticle(id!);
      show(result.queued ? "Queued -- reopen this tab in a bit." : (result.reason ?? "Not queued."), result.queued ? "ok" : "danger");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Failed to queue generation.", "danger");
    }
  }

  async function handleRegenerateText() {
    try {
      const result = await regenerateArticleText(id!);
      show(result.queued ? "Queued." : (result.reason ?? "Not queued."), result.queued ? "ok" : "danger");
    } catch (err) {
      show(err instanceof Error ? err.message : "Regenerate failed.", "danger");
    }
  }

  async function handleRegenerateImages() {
    try {
      const result = await regenerateArticleImages(id!);
      show(result.queued ? "Queued." : (result.reason ?? "Not queued."), result.queued ? "ok" : "danger");
    } catch (err) {
      show(err instanceof Error ? err.message : "Regenerate failed.", "danger");
    }
  }

  // Single-variant sibling of handleRegenerateImages -- see ArticlesPage.
  // tsx's own handleRegenerateVariant for the full reasoning (Al:
  // "missing some product shots still...").
  async function handleRegenerateVariant(variant: "action_shot" | "product_shot") {
    try {
      const result = variant === "action_shot" ? await regenerateArticleActionShot(id!) : await regenerateArticleProductShot(id!);
      show(result.queued ? "Queued." : (result.reason ?? "Not queued."), result.queued ? "ok" : "danger");
    } catch (err) {
      show(err instanceof Error ? err.message : "Regenerate failed.", "danger");
    }
  }

  async function handleApproveArticle() {
    if (!articleItem) return;
    try {
      await approveArticle(articleItem.id);
      show("Approved.", "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Approve failed.", "danger");
    }
  }

  async function confirmRejectArticle() {
    if (!articleItem) return;
    setSubmitting(true);
    try {
      await rejectArticle(articleItem.id, rejectArticleReason || undefined);
      show("Rejected.", "ok");
      setRejectArticleOpen(false);
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Reject failed.", "danger");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleToggleSync() {
    if (!articleItem) return;
    try {
      await setArticleBigcommerceSync(articleItem.id, !articleItem.sync_to_bigcommerce);
      show(!articleItem.sync_to_bigcommerce ? "Will sync to BigCommerce." : "Sync turned off.", "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Failed to toggle sync.", "danger");
    }
  }

  async function handleSyncNow() {
    if (!articleItem) return;
    try {
      const result = await syncArticleNow(articleItem.id);
      show(result.queued ? "Sync queued." : (result.reason ?? "Not queued."), result.queued ? "ok" : "danger");
    } catch (err) {
      show(err instanceof Error ? err.message : "Sync failed.", "danger");
    }
  }

  async function handleResyncNow() {
    if (!articleItem) return;
    try {
      const result = await resyncArticleNow(articleItem.id);
      show(result.queued ? "Resync queued." : (result.reason ?? "Not queued."), result.queued ? "ok" : "danger");
    } catch (err) {
      show(err instanceof Error ? err.message : "Resync failed.", "danger");
    }
  }

  async function handleSelectCandidate(candidateId: string) {
    try {
      await selectArticleImageCandidate(candidateId);
      show("Image updated.", "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Failed to select image.", "danger");
    }
  }

  // --- Pricing ------------------------------------------------------------
  // Ported from admin-site's buildPriceTrackingSection -- the buttons Al
  // noticed missing (approve/reject/undo/delete per source, "Find price
  // sources"/"Check price now" triggers, and the manual-add form) were
  // simply never carried over when this tab's first cut only rendered a
  // read-only table.

  async function handleDiscoverPriceSources() {
    setDiscoverPriceResult(null);
    try {
      const result = await discoverPriceSourcesForProduct(id!);
      setDiscoverPriceResult(
        result.queued ? "Queued -- check back in a bit for new sources." : (result.reason ?? "Not queued."),
      );
    } catch (err) {
      setDiscoverPriceResult(err instanceof Error ? err.message : "Failed to queue price-source search.");
    }
  }

  async function handleCheckPriceNow() {
    try {
      const result = await checkPriceForProduct(id!);
      show(result.queued ? "Price check queued." : (result.reason ?? "Not queued."), result.queued ? "ok" : "danger");
    } catch (err) {
      show(err instanceof Error ? err.message : "Price check failed.", "danger");
    }
  }

  async function handleApprovePriceSource(s: ProductPriceSource) {
    try {
      await approvePriceSource(s.id);
      show("Approved.", "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Approve failed.", "danger");
    }
  }

  async function confirmRejectPriceSource() {
    if (!rejectPriceSourceTarget) return;
    setSubmitting(true);
    try {
      await rejectPriceSource(rejectPriceSourceTarget.id, rejectPriceSourceReason || undefined);
      show("Rejected.", "ok");
      setRejectPriceSourceTarget(null);
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Reject failed.", "danger");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRestorePriceSource(s: ProductPriceSource) {
    try {
      await restorePriceSource(s.id);
      show("Restored to pending.", "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Restore failed.", "danger");
    }
  }

  async function handleDeletePriceSource(s: ProductPriceSource) {
    if (!window.confirm(`Delete this price source (${s.site_name})? This also removes its price history.`)) return;
    try {
      await deleteProductPriceSource(s.id);
      show("Deleted.", "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Delete failed.", "danger");
    }
  }

  async function handleAddManualPriceSource() {
    if (!manualSiteId || !manualUrl) return;
    setAddingManualSource(true);
    try {
      await createProductPriceSource(id!, {
        price_site_id: manualSiteId,
        product_url: manualUrl,
        css_selector: manualSelector || undefined,
      });
      show("Added.", "ok");
      setManualSiteId("");
      setManualUrl("");
      setManualSelector("");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Failed to add.", "danger");
    } finally {
      setAddingManualSource(false);
    }
  }

  // --- Raw Data ---------------------------------------------------------

  async function handleRescrape() {
    try {
      const result = await rescrapeProduct(id!);
      show(result.queued ? "Rescrape queued." : (result.reason ?? "Not queued."), result.queued ? "ok" : "danger");
    } catch (err) {
      show(err instanceof Error ? err.message : "Rescrape failed.", "danger");
    }
  }

  const videoColumns: Column<VideoCandidate>[] = [
    {
      key: "title",
      header: "Video",
      stackOnMobile: true,
      render: (v) => (
        <div>
          <a
            href={`https://youtube.com/watch?v=${v.youtube_video_id}`}
            target="_blank"
            rel="noreferrer"
            className="font-medium text-ink-800 hover:text-primary"
          >
            {v.title}
          </a>
          <div className="text-xs text-ink-500">
            {v.channel_title} · {formatDuration(v.duration_seconds)}
            {v.published_at ? ` · published ${fmtDateOnly(v.published_at)}` : ""}
          </div>
        </div>
      ),
    },
    { key: "views", header: "Views", render: (v) => v.view_count ?? "—" },
    {
      key: "match_confidence",
      header: "Match",
      render: (v) =>
        v.match_confidence ? <Badge tone={v.match_confidence === "high" ? "ok" : "pending"}>{v.match_confidence}</Badge> : "—",
    },
    {
      key: "has_summary",
      header: "Summary",
      render: (v) => (v.has_summary ? <Badge tone="ok">yes</Badge> : <Badge tone="muted">no</Badge>),
    },
    {
      key: "status",
      header: "Status",
      render: (v) => (
        <Badge tone={v.status === "approved" ? "ok" : v.status === "rejected" ? "danger" : "pending"}>{v.status}</Badge>
      ),
    },
    {
      key: "actions",
      header: "",
      stackOnMobile: true,
      render: (v) => (
        <div className="flex flex-wrap gap-1.5">
          {v.status === "pending" ? (
            <>
              <Button size="sm" variant="primary" onClick={() => handleApproveVideo(v)}>
                Approve
              </Button>
              <Button
                size="sm"
                variant="danger"
                onClick={() => {
                  setRejectVideoTarget(v);
                  setRejectVideoReason("");
                }}
              >
                Reject
              </Button>
            </>
          ) : (
            <Button size="sm" variant="secondary" onClick={() => handleRestoreVideo(v)}>
              Undo
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setReassignTarget(v);
              setReassignProductId("");
            }}
          >
            Reassign
          </Button>
        </div>
      ),
    },
  ];

  const priceSourceColumns: Column<ProductPriceSource>[] = [
    {
      key: "site_name",
      header: "Site",
      render: (s) => (
        <a
          href={s.product_url.startsWith("http") ? s.product_url : `${s.base_url ?? ""}${s.product_url}`}
          target="_blank"
          rel="noreferrer"
          className="font-medium text-ink-800 hover:text-primary"
        >
          {s.site_name}
        </a>
      ),
    },
    { key: "fetch_method", header: "Method", render: (s) => s.fetch_method },
    {
      key: "status",
      header: "Status",
      render: (s) => <Badge tone={s.status === "approved" ? "ok" : s.status === "rejected" ? "danger" : "pending"}>{s.status}</Badge>,
    },
    { key: "latest_price", header: "Latest price", render: (s) => (s.latest_price !== null ? `$${s.latest_price}` : "—") },
    {
      key: "latest_in_stock",
      header: "In stock",
      render: (s) => (s.latest_in_stock === null ? "—" : s.latest_in_stock ? <Badge tone="ok">yes</Badge> : <Badge tone="danger">no</Badge>),
    },
    { key: "latest_checked_at", header: "Last checked", render: (s) => fmtDate(s.latest_checked_at) },
    {
      key: "actions",
      header: "",
      stackOnMobile: true,
      render: (s) => (
        <div className="flex flex-wrap gap-1.5">
          {s.status === "pending" ? (
            <>
              <Button size="sm" variant="primary" onClick={() => handleApprovePriceSource(s)}>
                Approve
              </Button>
              <Button
                size="sm"
                variant="danger"
                onClick={() => {
                  setRejectPriceSourceTarget(s);
                  setRejectPriceSourceReason("");
                }}
              >
                Reject
              </Button>
            </>
          ) : (
            <Button size="sm" variant="secondary" onClick={() => handleRestorePriceSource(s)}>
              Undo
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={() => handleDeletePriceSource(s)}>
            Delete
          </Button>
        </div>
      ),
    },
  ];

  const sortedImages = [...product.images].sort((a, b) => a.display_order - b.display_order);

  return (
    <div className="flex flex-col gap-4">
      <div>
        <Link to={backToProductsHref} className="text-xs text-primary hover:underline">
          ← Back to Products
        </Link>
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <h1 className="text-xl font-semibold text-ink-800">
            {product.brand_name} {product.name}
          </h1>
          <Badge tone={product.status === "current" ? "ok" : "muted"}>{product.status}</Badge>
          <Badge tone={product.published ? "ok" : "muted"}>{product.published ? "published" : "unpublished"}</Badge>
          <Button size="sm" variant="secondary" onClick={handleTogglePublished}>
            {product.published ? "Unpublish" : "Publish"}
          </Button>
          <a href={product.url} target="_blank" rel="noreferrer" className="text-xs text-primary hover:underline">
            View source page ↗
          </a>
        </div>
      </div>

      <div className="flex flex-wrap gap-1 border-b border-ink-200">
        {detailTabs.map((t) => (
          <button
            key={t.name}
            onClick={() => setTab(t.name)}
            className={`rounded-t-md px-3 py-1.5 text-sm font-medium ${
              tab === t.name ? "border-b-2 border-primary text-primary" : "text-ink-500 hover:text-ink-700"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "overview" && (
        <div className="flex flex-col gap-4">
          <div>
            <p className="mb-2 text-sm font-semibold text-ink-800">Images ({product.images.length})</p>
            {sortedImages.length === 0 ? (
              <p className="text-sm text-ink-500">none pulled down yet</p>
            ) : (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4">
                {sortedImages.map((img, idx) => {
                  const full = img.stored_url || img.source_url;
                  const thumb = img.stored_url ? img.stored_url.replace(/detail\.png$/, "thumbnail.png") : img.source_url;
                  const label = (img.image_type ?? "image") + (img.weight_lbs_context ? ` (${img.weight_lbs_context}lb)` : "");
                  return (
                    <div key={img.id} className={`rounded-md border p-2 ${img.is_visible ? "border-ink-200" : "border-danger/40"}`}>
                      <a href={full} target="_blank" rel="noreferrer">
                        <img src={thumb} loading="lazy" alt={label} className="mb-1.5 h-28 w-full rounded object-cover" />
                      </a>
                      <div className="mb-1.5 text-xs text-ink-500">
                        {label}
                        {!img.stored_url && <div className="text-warn">not processed yet</div>}
                        {img.is_thumbnail && <Badge tone="primary">thumbnail</Badge>}
                        {!img.is_visible && <div className="text-danger">hidden</div>}
                      </div>
                      <div className="flex flex-wrap gap-1">
                        <Button size="sm" variant="ghost" disabled={idx === 0} onClick={() => handleMoveImage(sortedImages, idx, -1)}>
                          Up
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={idx === sortedImages.length - 1}
                          onClick={() => handleMoveImage(sortedImages, idx, 1)}
                        >
                          Down
                        </Button>
                        <Button size="sm" variant="ghost" disabled={img.is_thumbnail} onClick={() => handleSetThumbnail(img.id)}>
                          Set thumbnail
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => handleToggleVisibility(img.id, img.is_visible)}>
                          {img.is_visible ? "Hide" : "Show"}
                        </Button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <p className="text-sm font-semibold text-ink-800">Core</p>
              <p className="text-sm text-ink-600">
                {product.core_name ?? <span className="text-warn">not identified yet</span>}
                {product.core_type ? ` (${product.core_type})` : ""}
              </p>
            </div>
            <div>
              <p className="text-sm font-semibold text-ink-800">Coverstock</p>
              <p className="text-sm text-ink-600">{product.coverstock_name ?? <span className="text-warn">not identified yet</span>}</p>
            </div>
            <div>
              <p className="text-sm font-semibold text-ink-800">Source platform</p>
              <p className="text-sm text-ink-600">{product.source_platform ?? "—"}</p>
            </div>
            <div>
              <p className="text-sm font-semibold text-ink-800">Release date</p>
              <p className="text-sm text-ink-600">{fmtDateOnly(product.release_date)}</p>
            </div>
          </div>

          <div>
            <p className="mb-1 text-sm font-semibold text-ink-800">Description</p>
            <pre className="max-h-64 overflow-y-auto whitespace-pre-wrap rounded-md border border-ink-200 bg-ink-50 p-3 text-xs text-ink-600">
              {product.description || "(none scraped yet)"}
            </pre>
          </div>
        </div>
      )}

      {tab === "videos" && (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="secondary" onClick={handleDiscoverVideos}>
              Search for videos again
            </Button>
            <Button size="sm" variant="secondary" onClick={handleRefreshRollup}>
              Refresh rollup now
            </Button>
            {discoverResult && <span className="text-xs text-ink-500">{discoverResult}</span>}
          </div>
          <DataTable columns={videoColumns} rows={videos} getRowId={(v) => v.id} emptyMessage="No candidates found yet." />
          <div>
            <p className="mb-1 text-sm font-semibold text-ink-800">
              Video review rollup ({product.video_reviews_summary_video_count ?? 0} video
              {product.video_reviews_summary_video_count === 1 ? "" : "s"}, generated{" "}
              {fmtDate(product.video_reviews_summary_generated_at) || "never"})
            </p>
            <pre className="max-h-64 overflow-y-auto whitespace-pre-wrap rounded-md border border-ink-200 bg-ink-50 p-3 text-xs text-ink-600">
              {product.video_reviews_summary || "(none yet)"}
            </pre>
          </div>
        </div>
      )}

      {tab === "article" && (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm">
              <span className="font-semibold text-ink-800">Ball review article:</span>{" "}
              {articleItem ? <Badge tone={articleItem.status === "approved" ? "ok" : articleItem.status === "rejected" ? "danger" : "pending"}>{articleItem.status}</Badge> : <span className="text-ink-500">not generated yet</span>}
            </p>
            {articleItem ? (
              <>
                <Button size="sm" variant="secondary" onClick={handleRegenerateText}>
                  Regen text
                </Button>
                <Button size="sm" variant="secondary" onClick={handleRegenerateImages}>
                  Regen images
                </Button>
              </>
            ) : (
              <Button size="sm" variant="primary" onClick={handleGenerateArticle}>
                Generate article
              </Button>
            )}
          </div>

          {articleItem && (
            <div className="flex flex-wrap items-center gap-2">
              {articleItem.status === "pending" && (
                <>
                  <Button size="sm" variant="primary" onClick={handleApproveArticle}>
                    Approve
                  </Button>
                  <Button size="sm" variant="danger" onClick={() => { setRejectArticleOpen(true); setRejectArticleReason(""); }}>
                    Reject
                  </Button>
                </>
              )}
              <Button size="sm" variant={articleItem.sync_to_bigcommerce ? "primary" : "secondary"} onClick={handleToggleSync}>
                {articleItem.sync_to_bigcommerce ? "Sync on" : "Sync off"}
              </Button>
              {articleItem.sync_to_bigcommerce && !articleItem.bowlerdepot_synced_at && (
                <Button size="sm" variant="ghost" onClick={handleSyncNow}>
                  Sync now
                </Button>
              )}
              {articleItem.bowlerdepot_synced_at && (
                <>
                  <Button size="sm" variant="ghost" onClick={handleResyncNow}>
                    Resync
                  </Button>
                  <span className="text-xs text-ink-400">synced {fmtDate(articleItem.bowlerdepot_synced_at)}</span>
                </>
              )}
            </div>
          )}

          {article && (
            <ArticlePreview
              article={article}
              candidates={articleCandidates}
              onSelectCandidate={handleSelectCandidate}
              onRegenerateVariant={handleRegenerateVariant}
            />
          )}
        </div>
      )}

      {tab === "pricing" && (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="secondary" onClick={handleDiscoverPriceSources}>
              Find price sources
            </Button>
            <Button size="sm" variant="secondary" onClick={handleCheckPriceNow}>
              Check price now
            </Button>
            {discoverPriceResult && <span className="text-xs text-ink-500">{discoverPriceResult}</span>}
          </div>
          <DataTable columns={priceSourceColumns} rows={priceSources} getRowId={(s) => s.id} emptyMessage="No price sources configured yet." />

          <div className="rounded-md border border-ink-200 p-3">
            <p className="mb-2 text-sm font-semibold text-ink-800">Add price source manually</p>
            <div className="flex flex-wrap items-end gap-2">
              <div>
                <label className="mb-1 block text-xs font-medium text-ink-600">Site</label>
                <select
                  value={manualSiteId}
                  onChange={(e) => setManualSiteId(e.target.value)}
                  className="rounded-md border border-ink-300 px-2 py-1.5 text-sm focus:border-primary focus:outline-none"
                >
                  <option value="">Select a site…</option>
                  {priceSites
                    .filter((s) => s.is_active)
                    .map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.name}
                      </option>
                    ))}
                </select>
              </div>
              <div className="min-w-[16rem] flex-1">
                <label className="mb-1 block text-xs font-medium text-ink-600">Product URL</label>
                <input
                  value={manualUrl}
                  onChange={(e) => setManualUrl(e.target.value)}
                  placeholder="https://…"
                  className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm focus:border-primary focus:outline-none"
                />
              </div>
              <div className="min-w-[12rem]">
                <label className="mb-1 block text-xs font-medium text-ink-600">CSS selector override (optional)</label>
                <input
                  value={manualSelector}
                  onChange={(e) => setManualSelector(e.target.value)}
                  className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm focus:border-primary focus:outline-none"
                />
              </div>
              <Button
                size="sm"
                variant="primary"
                onClick={handleAddManualPriceSource}
                disabled={addingManualSource || !manualSiteId || !manualUrl}
              >
                {addingManualSource ? "Adding…" : "Add"}
              </Button>
            </div>
          </div>

          {priceHistory && <PriceHistoryChart data={priceHistory} />}

          {priceHistory && priceHistory.history.length > 0 && (
            <div>
              <p className="mb-1 text-sm font-semibold text-ink-800">Recent price checks</p>
              <div className="max-h-64 overflow-y-auto rounded-md border border-ink-200">
                <table className="w-full text-left text-xs">
                  <thead className="bg-ink-50 text-ink-500">
                    <tr>
                      <th className="px-2 py-1">Site</th>
                      <th className="px-2 py-1">Price</th>
                      <th className="px-2 py-1">In stock</th>
                      <th className="px-2 py-1">Checked</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...priceHistory.history]
                      .reverse()
                      .slice(0, 30)
                      .map((h, i) => {
                        const site = priceHistory.sources.find((s) => s.id === h.price_source_id)?.site_name ?? "—";
                        return (
                          <tr key={i} className="border-t border-ink-200">
                            <td className="px-2 py-1">{site}</td>
                            <td className="px-2 py-1">{h.error ? <span className="text-danger">{h.error}</span> : h.price !== null ? `$${h.price}` : "—"}</td>
                            <td className="px-2 py-1">{h.in_stock === null ? "—" : h.in_stock ? "yes" : "no"}</td>
                            <td className="px-2 py-1">{fmtDate(h.checked_at)}</td>
                          </tr>
                        );
                      })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {tab === "skus" && (
        <div className="flex flex-col gap-3">
          <div className="overflow-x-auto rounded-md border border-ink-200">
            <table className="w-full text-left text-sm">
              <thead className="bg-ink-50 text-xs uppercase text-ink-500">
                <tr>
                  <th className="px-2.5 py-1.5">Weight</th>
                  <th className="px-2.5 py-1.5">RG</th>
                  <th className="px-2.5 py-1.5">Diff</th>
                  <th className="px-2.5 py-1.5">Mass Bias</th>
                  <th className="px-2.5 py-1.5">Source</th>
                  <th className="px-2.5 py-1.5"></th>
                </tr>
              </thead>
              <tbody>
                {product.skus.length === 0 ? (
                  <tr>
                    <td colSpan={6} className="px-2.5 py-4 text-center text-ink-400">
                      none
                    </td>
                  </tr>
                ) : (
                  product.skus.map((s) => (
                    <tr key={s.id} className="border-t border-ink-200">
                      <td className="px-2.5 py-1.5">{s.weight_lbs}lb</td>
                      <td className="px-2.5 py-1.5">{s.rg ?? "—"}</td>
                      <td className="px-2.5 py-1.5">{s.differential ?? "—"}</td>
                      <td className="px-2.5 py-1.5">{s.mass_bias ?? "—"}</td>
                      <td className="px-2.5 py-1.5">{s.source ?? "—"}</td>
                      <td className="px-2.5 py-1.5">{s.needs_review ? "⚠️" : ""}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {skuStockHistory && <SkuStockChart data={skuStockHistory} />}

          {skuStockHistory && skuStockHistory.history.length > 0 && (
            <div>
              <p className="mb-1 text-sm font-semibold text-ink-800">Recent stock readings</p>
              <div className="max-h-64 overflow-y-auto rounded-md border border-ink-200">
                <table className="w-full text-left text-xs">
                  <thead className="bg-ink-50 text-ink-500">
                    <tr>
                      <th className="px-2 py-1">Weight</th>
                      <th className="px-2 py-1">Qty</th>
                      <th className="px-2 py-1">Checked</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...skuStockHistory.history]
                      .reverse()
                      .slice(0, 30)
                      .map((h, i) => {
                        const weight = skuStockHistory.skus.find((s) => s.id === h.product_sku_id)?.weight_lbs ?? "—";
                        return (
                          <tr key={i} className="border-t border-ink-200">
                            <td className="px-2 py-1">{weight}lb</td>
                            <td className="px-2 py-1">{h.quantity ?? "—"}</td>
                            <td className="px-2 py-1">{fmtDate(h.checked_at)}</td>
                          </tr>
                        );
                      })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {tab === "raw" && (
        <div className="flex flex-col gap-3">
          <div>
            <Button size="sm" variant="secondary" onClick={handleRescrape}>
              Rescrape now
            </Button>
          </div>
          <div className="overflow-x-auto rounded-md border border-ink-200">
            <table className="w-full text-left text-xs">
              <tbody>
                {Object.entries(product)
                  .filter(([k]) => !["skus", "images", "discovered_url", "bowlerdepot_matches", "bowwwl_matches"].includes(k))
                  .map(([k, v]) => (
                    <tr key={k} className="border-t border-ink-200">
                      <td className="w-48 px-2.5 py-1 font-medium text-ink-500">{k}</td>
                      <td className="px-2.5 py-1 text-ink-700">{v === null || v === undefined ? "—" : String(v)}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          {product.discovered_url && (
            <div>
              <p className="mb-1 text-sm font-semibold text-ink-800">Discovered URL record</p>
              <pre className="overflow-x-auto rounded-md border border-ink-200 bg-ink-50 p-2 text-xs text-ink-600">
                {JSON.stringify(product.discovered_url, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}

      <Modal
        open={rejectVideoTarget !== null}
        onClose={() => (submitting ? undefined : setRejectVideoTarget(null))}
        title="Reject video"
        footer={
          <>
            <Button variant="secondary" onClick={() => setRejectVideoTarget(null)} disabled={submitting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={confirmRejectVideo} disabled={submitting}>
              {submitting ? "Rejecting…" : "Reject"}
            </Button>
          </>
        }
      >
        <label className="mb-1 block text-xs font-medium text-ink-600">Reason (optional)</label>
        <textarea
          value={rejectVideoReason}
          onChange={(e) => setRejectVideoReason(e.target.value)}
          rows={3}
          className="w-full rounded-md border border-ink-300 px-3 py-2 text-sm focus:border-primary focus:outline-none"
        />
      </Modal>

      <Modal
        open={reassignTarget !== null}
        onClose={() => (submitting ? undefined : setReassignTarget(null))}
        title="Reassign to a different product"
        footer={
          <>
            <Button variant="secondary" onClick={() => setReassignTarget(null)} disabled={submitting}>
              Cancel
            </Button>
            <Button variant="primary" onClick={confirmReassignVideo} disabled={submitting || !reassignProductId}>
              {submitting ? "Reassigning…" : "Reassign"}
            </Button>
          </>
        }
      >
        <label className="mb-1 block text-xs font-medium text-ink-600">Target product ID</label>
        <input
          value={reassignProductId}
          onChange={(e) => setReassignProductId(e.target.value)}
          placeholder="uuid"
          className="w-full rounded-md border border-ink-300 px-3 py-2 text-sm focus:border-primary focus:outline-none"
        />
      </Modal>

      <Modal
        open={rejectArticleOpen}
        onClose={() => (submitting ? undefined : setRejectArticleOpen(false))}
        title="Reject article"
        footer={
          <>
            <Button variant="secondary" onClick={() => setRejectArticleOpen(false)} disabled={submitting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={confirmRejectArticle} disabled={submitting}>
              {submitting ? "Rejecting…" : "Reject"}
            </Button>
          </>
        }
      >
        <label className="mb-1 block text-xs font-medium text-ink-600">Reason (optional)</label>
        <textarea
          value={rejectArticleReason}
          onChange={(e) => setRejectArticleReason(e.target.value)}
          rows={3}
          className="w-full rounded-md border border-ink-300 px-3 py-2 text-sm focus:border-primary focus:outline-none"
        />
      </Modal>

      <Modal
        open={rejectPriceSourceTarget !== null}
        onClose={() => (submitting ? undefined : setRejectPriceSourceTarget(null))}
        title="Reject price source"
        footer={
          <>
            <Button variant="secondary" onClick={() => setRejectPriceSourceTarget(null)} disabled={submitting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={confirmRejectPriceSource} disabled={submitting}>
              {submitting ? "Rejecting…" : "Reject"}
            </Button>
          </>
        }
      >
        <label className="mb-1 block text-xs font-medium text-ink-600">Reason (optional)</label>
        <textarea
          value={rejectPriceSourceReason}
          onChange={(e) => setRejectPriceSourceReason(e.target.value)}
          rows={3}
          className="w-full rounded-md border border-ink-300 px-3 py-2 text-sm focus:border-primary focus:outline-none"
        />
      </Modal>
    </div>
  );
}
