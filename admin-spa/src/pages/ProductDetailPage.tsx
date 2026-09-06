import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  approveArticle,
  approveVideoCandidate,
  discoverVideosForProduct,
  generateArticle,
  getArticle,
  getPriceHistory,
  getProduct,
  getSkuStockHistory,
  listArticleImageCandidates,
  listArticles,
  listProductPriceSources,
  listVideoCandidates,
  reassignVideoCandidate,
  regenerateArticleImages,
  regenerateArticleText,
  rejectArticle,
  rejectVideoCandidate,
  rescrapeProduct,
  resyncArticleNow,
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
  ProductDetail,
  ProductImage,
  ProductPriceSource,
  SkuStockHistoryResult,
  VideoCandidate,
} from "../api/types";
import Badge from "../components/Badge";
import Button from "../components/Button";
import type { Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import Modal from "../components/Modal";
import { useToast } from "../components/Toast";
import { ArticlePreview } from "./ArticlesPage";

type DetailTab = "overview" | "videos" | "article" | "pricing" | "skus" | "raw";

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

  const [tab, setTab] = useState<DetailTab>("overview");
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

  const [rejectVideoTarget, setRejectVideoTarget] = useState<VideoCandidate | null>(null);
  const [rejectVideoReason, setRejectVideoReason] = useState("");
  const [reassignTarget, setReassignTarget] = useState<VideoCandidate | null>(null);
  const [reassignProductId, setReassignProductId] = useState("");
  const [rejectArticleOpen, setRejectArticleOpen] = useState(false);
  const [rejectArticleReason, setRejectArticleReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [discoverResult, setDiscoverResult] = useState<string | null>(null);

  // Sequential, not Promise.all -- real, confirmed incident on this AWS
  // account (see template.yaml's own comment above AdminApiFunction):
  // Lambda UnreservedConcurrentExecutions is capped at 10 account-wide
  // (AWS's default low tier for a new/unverified account), and a burst
  // of concurrent invocations against this same function has already
  // been seen to exhaust that pool and come back as a bare 503 from
  // AdminHttpApi with nothing in CloudWatch -- HTTP API v2's own way of
  // surfacing a Lambda-service throttle. This page needs six separate
  // admin_api calls (plus two more if an article exists); firing them
  // all via Promise.all was exactly that burst. Awaiting them one at a
  // time keeps this page to a single in-flight AdminApiFunction
  // invocation at once, same "one request, not a burst" posture
  // scripts/backfill_core_ids.py's own retry-with-backoff was written
  // for. Slower wall-clock (roughly the sum of six round-trips instead
  // of the max of six), but that's a fair trade against the page simply
  // failing to load at all.
  //
  // Each fetch also fails independently rather than aborting the whole
  // load -- a stumble on, say, price-history shouldn't blank a product
  // that loaded fine; it just leaves that one sub-tab empty and reports
  // the failure via the toast instead of the whole-page error view.
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

    await loadPart("videos", () => listVideoCandidates({ product_id: id, status: "all", limit: 200 }), (r) =>
      setVideos(r.items),
    );
    await loadPart("article", () => listArticles({ product_id: id, status: "all", limit: 1 }), (r) =>
      setArticleItem(r[0] ?? null),
    );
    await loadPart("price sources", () => listProductPriceSources(id, "all"), setPriceSources);
    await loadPart("price history", () => getPriceHistory(id), setPriceHistory);
    await loadPart("SKU stock history", () => getSkuStockHistory(id), setSkuStockHistory);
    setLoading(false);
  }

  // Article full detail + image candidates depend on articleItem (set by
  // load() above) rather than living inside load() itself -- keeps the
  // same one-request-at-a-time posture without load() needing to know
  // about article-specific follow-up calls.
  useEffect(() => {
    if (!articleItem) {
      setArticle(null);
      setArticleCandidates([]);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const full = await getArticle(articleItem.id);
        if (cancelled) return;
        setArticle(full);
        const candidates = await listArticleImageCandidates(articleItem.id);
        if (!cancelled) setArticleCandidates(candidates);
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
  ];

  const sortedImages = [...product.images].sort((a, b) => a.display_order - b.display_order);

  return (
    <div className="flex flex-col gap-4">
      <div>
        <Link to="/products" className="text-xs text-primary hover:underline">
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

          {article && <ArticlePreview article={article} candidates={articleCandidates} onSelectCandidate={handleSelectCandidate} />}
        </div>
      )}

      {tab === "pricing" && (
        <div className="flex flex-col gap-3">
          <DataTable columns={priceSourceColumns} rows={priceSources} getRowId={(s) => s.id} emptyMessage="No price sources configured yet." />
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
    </div>
  );
}
