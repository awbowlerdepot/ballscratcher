import { useEffect, useState } from "react";
import {
  approveArticle,
  getArticle,
  listArticleImageCandidates,
  listArticles,
  regenerateArticleImages,
  regenerateArticleText,
  rejectArticle,
  resyncArticleNow,
  selectArticleImageCandidate,
  setArticleBigcommerceSync,
  syncArticleNow,
} from "../api/client";
import type { Article, ArticleImageCandidate, ArticleListItem, ArticleStatus, ListArticlesParams } from "../api/types";
import Badge from "../components/Badge";
import Button from "../components/Button";
import type { Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import Modal from "../components/Modal";
import Pagination from "../components/Pagination";
import { useToast } from "../components/Toast";

const LIMIT = 50;

const VARIANT_LABELS: Record<string, string> = { action_shot: "Action shot", product_shot: "Product shot" };

function fmtDate(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

// Ports admin-site/index.html's Articles tab -- AI-generated ball-review
// articles (022_product_articles.sql), reviewed the same pending/
// approved/rejected way as everything else, plus a BigCommerce sync
// toggle and an image-candidate picker (026_product_article_image_
// candidates.sql). No bulk actions here -- admin-site's own Articles tab
// never had them either (article review is inherently one-at-a-time:
// each row's images/text are unique enough that a shared bulk-reject
// reason doesn't make as much sense as it does for Review Queue/Video
// Candidates). No pending_count badge either -- GET /articles doesn't
// return one (checked admin_api/app.py before assuming it did).
export default function ArticlesPage() {
  const { show } = useToast();
  const [items, setItems] = useState<ArticleListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);

  const [status, setStatus] = useState<ArticleStatus>("pending");
  const [productId, setProductId] = useState("");

  const [rejectTarget, setRejectTarget] = useState<ArticleListItem | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const [previewId, setPreviewId] = useState<string | null>(null);
  const [previewArticle, setPreviewArticle] = useState<Article | null>(null);
  const [previewCandidates, setPreviewCandidates] = useState<ArticleImageCandidate[]>([]);
  const [previewLoading, setPreviewLoading] = useState(false);

  function load() {
    const params: ListArticlesParams = { status, product_id: productId || undefined, limit: LIMIT, offset };
    setLoading(true);
    setError(null);
    listArticles(params)
      .then(setItems)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load articles."))
      .finally(() => setLoading(false));
  }

  useEffect(load, [status, productId, offset]);

  function resetAndSet<T>(setter: (v: T) => void) {
    return (value: T) => {
      setOffset(0);
      setter(value);
    };
  }

  async function handleApprove(a: ArticleListItem) {
    try {
      await approveArticle(a.id);
      show("Approved -- now visible via the public API.", "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Approve failed.", "danger");
    }
  }

  function openRejectModal(a: ArticleListItem) {
    setRejectTarget(a);
    setRejectReason("");
  }

  async function confirmReject() {
    if (!rejectTarget) return;
    setSubmitting(true);
    try {
      await rejectArticle(rejectTarget.id, rejectReason || undefined);
      show("Rejected.", "ok");
      setRejectTarget(null);
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Reject failed.", "danger");
    } finally {
      setSubmitting(false);
    }
  }

  // No confirm() before this, same as admin-site -- it only sets what
  // the hourly sync job (or "Sync now" below) will pick up, it doesn't
  // itself publish/unpublish anything.
  async function handleToggleSync(a: ArticleListItem) {
    try {
      await setArticleBigcommerceSync(a.id, !a.sync_to_bigcommerce);
      show(!a.sync_to_bigcommerce ? "Will sync to BigCommerce." : "Sync turned off.", "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Failed to toggle sync.", "danger");
    }
  }

  async function handleSyncNow(id: string) {
    try {
      const result = await syncArticleNow(id);
      show(
        result.queued ? "Sync queued -- refresh in a few seconds to see the result." : (result.reason ?? "Not queued."),
        result.queued ? "ok" : "danger",
      );
    } catch (err) {
      show(err instanceof Error ? err.message : "Sync failed.", "danger");
    }
  }

  async function handleResyncNow(id: string) {
    try {
      const result = await resyncArticleNow(id);
      show(result.queued ? "Resync queued." : (result.reason ?? "Not queued."), result.queued ? "ok" : "danger");
    } catch (err) {
      show(err instanceof Error ? err.message : "Resync failed.", "danger");
    }
  }

  async function handleRegenerateText(productIdForArticle: string) {
    try {
      const result = await regenerateArticleText(productIdForArticle);
      show(
        result.queued ? "Queued -- reopen this article in a bit to see the regenerated text." : (result.reason ?? "Not queued."),
        result.queued ? "ok" : "danger",
      );
    } catch (err) {
      show(err instanceof Error ? err.message : "Regenerate failed.", "danger");
    }
  }

  async function handleRegenerateImages(productIdForArticle: string) {
    try {
      const result = await regenerateArticleImages(productIdForArticle);
      show(
        result.queued ? "Queued -- reopen this article in a bit to see the new candidates." : (result.reason ?? "Not queued."),
        result.queued ? "ok" : "danger",
      );
    } catch (err) {
      show(err instanceof Error ? err.message : "Regenerate failed.", "danger");
    }
  }

  function openPreview(id: string) {
    setPreviewId(id);
    setPreviewArticle(null);
    setPreviewCandidates([]);
    setPreviewLoading(true);
    Promise.all([getArticle(id), listArticleImageCandidates(id)])
      .then(([article, candidates]) => {
        setPreviewArticle(article);
        setPreviewCandidates(candidates);
      })
      .catch((err) => show(err instanceof Error ? err.message : "Failed to load article.", "danger"))
      .finally(() => setPreviewLoading(false));
  }

  async function handleSelectCandidate(candidateId: string) {
    if (!previewId) return;
    try {
      await selectArticleImageCandidate(candidateId);
      show("Image updated.", "ok");
      openPreview(previewId);
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Failed to select image.", "danger");
    }
  }

  const columns: Column<ArticleListItem>[] = [
    {
      key: "title",
      header: "Title",
      stackOnMobile: true,
      render: (a) => (
        // items-start, not inline-block/align-middle -- with a two-line
        // title (common for full ball names) the old inline image drifted
        // to whichever line it happened to sit next to instead of staying
        // pinned to the top of the block. shrink-0 on the image keeps a
        // long title from squeezing the thumbnail down as it wraps.
        <div className="flex items-start gap-2">
          {a.action_shot_image_url ? (
            <img
              src={a.action_shot_image_url}
              alt=""
              title="Action shot"
              className="h-8 w-8 shrink-0 rounded object-cover"
            />
          ) : a.images_generated_at ? (
            <span
              className="mt-1.5 h-8 w-8 shrink-0 text-center text-[10px] leading-tight text-ink-400"
              title="Image generation ran but produced no images"
            >
              no images
            </span>
          ) : (
            <span className="h-8 w-8 shrink-0" aria-hidden="true" />
          )}
          <div className="min-w-0">
            <button className="text-left font-medium text-ink-800 hover:text-primary" onClick={() => openPreview(a.id)}>
              {a.title || "(untitled)"}
            </button>
            <div className="text-xs text-ink-400">
              {a.id.slice(0, 8)}&hellip;{" "}
              <button
                className="text-primary hover:underline"
                onClick={() => navigator.clipboard.writeText(a.id)}
                title="Copy full article ID"
              >
                Copy ID
              </button>
            </div>
          </div>
        </div>
      ),
    },
    {
      key: "product_name",
      header: "Product",
      render: (a) => (
        <div>
          {a.product_name}
          <div className="text-xs text-ink-500">{a.brand_name}</div>
        </div>
      ),
    },
    { key: "generated_at", header: "Generated", render: (a) => fmtDate(a.generated_at) },
    {
      key: "resolved_by",
      header: "Reviewed by",
      render: (a) =>
        a.resolved_by ? (
          <div>
            {a.resolved_by}
            <div className="text-xs text-ink-500">{fmtDate(a.reviewed_at)}</div>
          </div>
        ) : (
          "—"
        ),
    },
    {
      key: "sync",
      header: "BigCommerce",
      stackOnMobile: true,
      render: (a) => (
        <div className="flex flex-col items-start gap-1.5">
          <Button size="sm" variant={a.sync_to_bigcommerce ? "primary" : "secondary"} onClick={() => handleToggleSync(a)}>
            {a.sync_to_bigcommerce ? "Sync on" : "Sync off"}
          </Button>
          {/* Only shown once the flag is on and it hasn't already gone
              out -- same "hidden rather than shown-but-inert" reasoning
              as admin-site's own syncNowBtn condition. */}
          {a.sync_to_bigcommerce && !a.bowlerdepot_synced_at && (
            <Button size="sm" variant="ghost" onClick={() => handleSyncNow(a.id)}>
              Sync now
            </Button>
          )}
          {a.bowlerdepot_synced_at && (
            <div className="flex items-center gap-2">
              <Button size="sm" variant="ghost" onClick={() => handleResyncNow(a.id)}>
                Resync
              </Button>
              <span className="text-xs text-ink-400">synced {fmtDate(a.bowlerdepot_synced_at)}</span>
            </div>
          )}
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      stackOnMobile: true,
      // Two tiers, not one flat row -- the review decision (Approve/
      // Reject, or the resolved-status badge) is the action someone
      // actually came to this row to take; Regen text/images and
      // Preview are secondary maintenance actions that were previously
      // sitting at the same visual weight and blurring together into a
      // five-button wall. Preview duplicates the title's own
      // click-to-open affordance but stays here too since a bare title
      // link isn't always obviously clickable.
      render: (a) => (
        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap gap-1.5">
            {a.status === "pending" ? (
              <>
                <Button size="sm" variant="primary" onClick={() => handleApprove(a)}>
                  Approve
                </Button>
                <Button size="sm" variant="danger" onClick={() => openRejectModal(a)}>
                  Reject
                </Button>
              </>
            ) : (
              <Badge tone={a.status === "approved" ? "ok" : "danger"}>{a.status}</Badge>
            )}
          </div>
          <div className="flex flex-wrap gap-1.5">
            <Button size="sm" variant="secondary" onClick={() => handleRegenerateText(a.product_id)}>
              Regen text
            </Button>
            <Button size="sm" variant="secondary" onClick={() => handleRegenerateImages(a.product_id)}>
              Regen images
            </Button>
            <Button size="sm" variant="ghost" onClick={() => openPreview(a.id)}>
              Preview
            </Button>
          </div>
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold text-ink-800">Articles</h1>

      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-ink-200 bg-ink-100 p-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-ink-600">Status</label>
          <select
            value={status}
            onChange={(e) => resetAndSet(setStatus)(e.target.value as ArticleStatus)}
            className="rounded-md border border-ink-300 px-2 py-1.5 text-sm"
          >
            <option value="pending">Pending</option>
            <option value="approved">Approved</option>
            <option value="rejected">Rejected</option>
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-ink-600">Product ID</label>
          <input
            value={productId}
            onChange={(e) => resetAndSet(setProductId)(e.target.value)}
            placeholder="uuid"
            className="w-64 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
          />
        </div>
      </div>

      {error && <div className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{error}</div>}

      <DataTable
        columns={columns}
        rows={items}
        getRowId={(a) => a.id}
        emptyMessage={loading ? "Loading…" : "Nothing here."}
      />

      <Pagination offset={offset} limit={LIMIT} itemCount={items.length} onOffsetChange={setOffset} />

      <Modal
        open={rejectTarget !== null}
        onClose={() => (submitting ? undefined : setRejectTarget(null))}
        title={`Reject "${rejectTarget?.title || "(untitled)"}"`}
        footer={
          <>
            <Button variant="secondary" onClick={() => setRejectTarget(null)} disabled={submitting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={confirmReject} disabled={submitting}>
              {submitting ? "Rejecting…" : "Reject"}
            </Button>
          </>
        }
      >
        <label className="mb-1 block text-xs font-medium text-ink-600">Reason (optional)</label>
        <textarea
          value={rejectReason}
          onChange={(e) => setRejectReason(e.target.value)}
          rows={3}
          className="w-full rounded-md border border-ink-300 px-3 py-2 text-sm focus:border-primary focus:outline-none"
        />
      </Modal>

      <Modal open={previewId !== null} onClose={() => setPreviewId(null)} title={previewArticle?.title || "Article preview"} wide>
        {previewLoading && <p className="text-sm text-ink-500">Loading…</p>}
        {previewArticle && (
          <ArticlePreview article={previewArticle} candidates={previewCandidates} onSelectCandidate={handleSelectCandidate} />
        )}
      </Modal>
    </div>
  );
}

// Full bowling.com-shaped article render -- mirrors admin-site's
// renderArticlePreviewHtml/renderImageCandidatesHtml as closely as JSX
// allows. candidates may be empty for a pre-v4 article that still has
// plain action_shot_image_url/product_shot_image_url set directly (no
// candidate rows) -- falls back to showing those two images plainly in
// that case, same as admin-site's own imagesBlock fallback.
function ArticlePreview({
  article,
  candidates,
  onSelectCandidate,
}: {
  article: Article;
  candidates: ArticleImageCandidate[];
  onSelectCandidate: (candidateId: string) => void;
}) {
  const listBlock = (label: string, items: string[]) =>
    items.length ? (
      <div>
        <p className="text-sm font-semibold text-ink-800">{label}</p>
        <ul className="list-disc pl-5 text-sm text-ink-600">
          {items.map((s, i) => (
            <li key={i}>{s}</li>
          ))}
        </ul>
      </div>
    ) : null;

  const byVariant: Record<string, ArticleImageCandidate[]> = {};
  candidates.forEach((c) => {
    if (!byVariant[c.variant]) byVariant[c.variant] = [];
    byVariant[c.variant].push(c);
  });

  return (
    <div className="flex flex-col gap-3 text-sm">
      <p className="font-mono text-xs text-ink-400">
        id: {article.id}{" "}
        <button
          className="text-primary hover:underline"
          onClick={() => navigator.clipboard.writeText(article.id)}
          title="Copy full article ID"
        >
          Copy ID
        </button>
      </p>

      {candidates.length > 0 ? (
        Object.keys(byVariant)
          .sort()
          .map((variant) => (
            <div key={variant}>
              <p className="mb-1 font-semibold text-ink-800">{VARIANT_LABELS[variant] ?? variant} candidates:</p>
              {/* flex-col + mt-auto on the trailing badge/button: the
                  outer flex-wrap row already stretches every card in it
                  to the tallest one's height (flex's default
                  align-items: stretch), but without flex-col that extra
                  height just sat below the model_id text -- the
                  button/badge floated at whatever height the image+text
                  happened to add up to, so it didn't line up card to
                  card. mt-auto pins it to the bottom of every card
                  instead, and w-full on the button matches the badge's
                  own full-bleed feel so selected/unselected cards read
                  the same shape. */}
              <div className="flex flex-wrap gap-3">
                {byVariant[variant].map((c) => (
                  <div
                    key={c.id}
                    className={`flex w-40 flex-col rounded-md border p-2 ${c.is_selected ? "border-primary" : "border-ink-200"}`}
                  >
                    <img src={c.image_url} alt="" loading="lazy" className="mb-1.5 h-28 w-full rounded object-cover" />
                    <div className="mb-1.5 truncate text-xs text-ink-500" title={c.model_id}>
                      {c.model_id}
                    </div>
                    <div className="mt-auto flex justify-center">
                      {c.is_selected ? (
                        <Badge tone="ok">selected</Badge>
                      ) : (
                        <Button size="sm" variant="ghost" className="w-full" onClick={() => onSelectCandidate(c.id)}>
                          Use this one
                        </Button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))
      ) : article.action_shot_image_url || article.product_shot_image_url ? (
        // flex-wrap: two 160px figures + gap don't fit this Modal's content
        // width on a narrow phone (see the mobile pass, 2026-09-05) --
        // without it these would force the modal itself to scroll sideways.
        <div className="flex flex-wrap gap-3">
          {article.action_shot_image_url && (
            <figure className="w-40">
              <img src={article.action_shot_image_url} alt="Action shot" className="h-28 w-full rounded object-cover" />
              <figcaption className="text-center text-xs text-ink-500">Action shot</figcaption>
            </figure>
          )}
          {article.product_shot_image_url && (
            <figure className="w-40">
              <img src={article.product_shot_image_url} alt="Product shot" className="h-28 w-full rounded object-cover" />
              <figcaption className="text-center text-xs text-ink-500">Product shot</figcaption>
            </figure>
          )}
        </div>
      ) : article.images_generated_at ? (
        <p className="text-xs text-ink-400">Image generation ran but produced no images for this article.</p>
      ) : null}

      {article.hook && <p className="italic text-ink-700">{article.hook}</p>}
      {article.performance_summary && (
        <div>
          <p className="font-semibold text-ink-800">Performance summary</p>
          <p className="text-ink-600">{article.performance_summary}</p>
        </div>
      )}
      {listBlock("Who should buy this", article.who_should_buy)}
      {listBlock("Who should skip this", article.who_should_skip)}
      {listBlock("Pros", article.pros)}
      {listBlock("Cons", article.cons)}
      {article.buying_tips && (
        <div>
          <p className="font-semibold text-ink-800">Buying tips</p>
          <p className="text-ink-600">{article.buying_tips}</p>
        </div>
      )}
      {article.verdict && (
        <div>
          <p className="font-semibold text-ink-800">Verdict</p>
          <p className="text-ink-600">{article.verdict}</p>
        </div>
      )}
      {article.faq.length > 0 && (
        <div>
          <p className="font-semibold text-ink-800">FAQ</p>
          {article.faq.map((qa, i) => (
            <div key={i} className="mb-1.5">
              <p className="text-xs font-medium text-ink-600">{qa.question}</p>
              <p className="text-ink-600">{qa.answer}</p>
            </div>
          ))}
        </div>
      )}
      {article.comparison_table.length > 0 && (
        <p className="text-xs text-ink-500">Comparison table: {article.comparison_table.length} row(s)</p>
      )}
      {article.sibling_product_ids.length > 0 && (
        <p className="text-xs text-ink-500">
          Inferred siblings (heuristic -- not ground truth): {article.sibling_product_ids.length} product(s)
        </p>
      )}
      <p className="text-xs text-ink-400">
        Source videos: {article.source_video_ids.length} &middot; generated {fmtDate(article.generated_at)}
      </p>
    </div>
  );
}
