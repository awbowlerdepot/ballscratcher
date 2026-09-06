import { useEffect, useState } from "react";
import {
  approveVideoCandidate,
  listVideoCandidates,
  reassignVideoCandidate,
  rejectVideoCandidate,
  restoreVideoCandidate,
} from "../api/client";
import type { ListVideoCandidatesParams, VideoCandidate, VideoCandidateStatus } from "../api/types";
import Badge from "../components/Badge";
import Button from "../components/Button";
import type { BulkAction, Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import Modal from "../components/Modal";
import Pagination from "../components/Pagination";
import { useToast } from "../components/Toast";

const LIMIT = 50;

// Same sequential-with-300ms-delay pacing as ReviewQueuePage/
// admin-site's own bulk video actions (task #255) -- avoids the API
// Gateway/Lambda throttling this project has hit before with
// concurrent bulk calls.
async function sequentialWithDelay<T>(
  ids: string[],
  fn: (id: string) => Promise<T>,
  delayMs = 300,
): Promise<{ succeeded: number; failed: number }> {
  let succeeded = 0;
  let failed = 0;
  for (const id of ids) {
    try {
      await fn(id);
      succeeded++;
    } catch {
      failed++;
    }
    if (delayMs > 0) await new Promise((resolve) => setTimeout(resolve, delayMs));
  }
  return { succeeded, failed };
}

function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

// Ports admin-site/index.html's Video Candidates tab -- YouTube review
// videos matched to products, approved/rejected by a human. Mirrors
// that tab's exact status choices (pending/approved/rejected -- "all"
// exists server-side but was never exposed as a dropdown option there
// either, only used by the product-detail sub-panel this admin-spa
// doesn't have yet). Reassign is per-row (a video moving to a
// different product, tombstoning the original as rejected) -- there's
// no bulk-reassign in the plain tab, only in product-detail.
export default function VideoCandidatesPage() {
  const { show } = useToast();
  const [items, setItems] = useState<VideoCandidate[]>([]);
  const [pendingCount, setPendingCount] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [offset, setOffset] = useState(0);

  const [status, setStatus] = useState<VideoCandidateStatus>("pending");
  const [productId, setProductId] = useState("");

  const [rejectTarget, setRejectTarget] = useState<VideoCandidate[] | null>(null);
  const [rejectReason, setRejectReason] = useState("");

  const [reassignTarget, setReassignTarget] = useState<VideoCandidate | null>(null);
  const [reassignProductId, setReassignProductId] = useState("");

  const [submitting, setSubmitting] = useState(false);

  function load() {
    const params: ListVideoCandidatesParams = {
      status,
      product_id: productId || undefined,
      limit: LIMIT,
      offset,
    };
    setLoading(true);
    setError(null);
    listVideoCandidates(params)
      .then((r) => {
        setItems(r.items);
        setPendingCount(r.pending_count);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load video candidates."))
      .finally(() => setLoading(false));
  }

  useEffect(load, [status, productId, offset]);

  function resetAndSet<T>(setter: (v: T) => void) {
    return (value: T) => {
      setOffset(0);
      setSelectedIds(new Set());
      setter(value);
    };
  }

  async function handleApprove(rows: VideoCandidate[]) {
    const { succeeded, failed } = await sequentialWithDelay(
      rows.map((r) => r.id),
      approveVideoCandidate,
    );
    show(
      failed === 0 ? `Approved ${succeeded} video${succeeded === 1 ? "" : "s"}.` : `Approved ${succeeded}, ${failed} failed.`,
      failed === 0 ? "ok" : "danger",
    );
    setSelectedIds(new Set());
    load();
  }

  function openRejectModal(rows: VideoCandidate[]) {
    setRejectTarget(rows);
    setRejectReason("");
  }

  async function confirmReject() {
    if (!rejectTarget) return;
    setSubmitting(true);
    try {
      const { succeeded, failed } = await sequentialWithDelay(
        rejectTarget.map((r) => r.id),
        (id) => rejectVideoCandidate(id, rejectReason || undefined),
      );
      show(
        failed === 0 ? `Rejected ${succeeded} video${succeeded === 1 ? "" : "s"}.` : `Rejected ${succeeded}, ${failed} failed.`,
        failed === 0 ? "ok" : "danger",
      );
      setSelectedIds(new Set());
      setRejectTarget(null);
      load();
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRestore(row: VideoCandidate) {
    try {
      await restoreVideoCandidate(row.id);
      show("Restored to pending.", "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Restore failed.", "danger");
    }
  }

  function openReassignModal(row: VideoCandidate) {
    setReassignTarget(row);
    setReassignProductId("");
  }

  async function confirmReassign() {
    if (!reassignTarget || !reassignProductId) return;
    setSubmitting(true);
    try {
      const result = await reassignVideoCandidate(reassignTarget.id, reassignProductId);
      show(
        result.merged_with_existing
          ? "Reassigned -- merged with an existing candidate on the target product."
          : "Reassigned to the target product.",
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

  const columns: Column<VideoCandidate>[] = [
    {
      key: "title",
      header: "Video",
      render: (r) => (
        <div>
          <a
            href={`https://youtube.com/watch?v=${r.youtube_video_id}`}
            target="_blank"
            rel="noreferrer"
            className="font-medium text-slate-800 hover:text-primary"
          >
            {r.title}
          </a>
          <div className="text-xs text-slate-500">
            {r.channel_title} · {formatDuration(r.duration_seconds)}
          </div>
        </div>
      ),
    },
    {
      key: "product_name",
      header: "Product",
      render: (r) => (
        <span>
          {r.brand_name} {r.product_name}
        </span>
      ),
    },
    {
      key: "stats",
      header: "Views / Likes / Comments",
      render: (r) => `${r.view_count ?? "—"} / ${r.like_count ?? "—"} / ${r.comment_count ?? "—"}`,
    },
    {
      key: "match_confidence",
      header: "Match",
      render: (r) => (r.match_confidence !== null ? r.match_confidence.toFixed(2) : "—"),
    },
    {
      key: "has_summary",
      header: "Summary",
      render: (r) => (r.has_summary ? <Badge tone="ok">yes</Badge> : <Badge tone="muted">no</Badge>),
    },
    {
      key: "status",
      header: "Status",
      render: (r) => (
        <Badge tone={r.status === "approved" ? "ok" : r.status === "rejected" ? "danger" : "pending"}>{r.status}</Badge>
      ),
    },
    {
      key: "published_at",
      header: "Published",
      render: (r) => (r.published_at ? new Date(r.published_at).toLocaleDateString() : "—"),
    },
    {
      key: "actions",
      header: "",
      render: (r) => (
        <div className="flex flex-wrap gap-1.5">
          {r.status === "pending" && (
            <>
              <Button size="sm" variant="primary" onClick={() => handleApprove([r])}>
                Approve
              </Button>
              <Button size="sm" variant="danger" onClick={() => openRejectModal([r])}>
                Reject
              </Button>
            </>
          )}
          {r.status !== "pending" && (
            <Button size="sm" variant="secondary" onClick={() => handleRestore(r)}>
              Undo
            </Button>
          )}
          <Button size="sm" variant="secondary" onClick={() => openReassignModal(r)}>
            Reassign
          </Button>
        </div>
      ),
    },
  ];

  const bulkActions: BulkAction<VideoCandidate>[] =
    status === "pending"
      ? [
          { label: "Approve selected", onClick: handleApprove, variant: "primary" },
          { label: "Reject selected", onClick: openRejectModal, variant: "danger" },
        ]
      : [];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold text-slate-800">Video Candidates</h1>
        {pendingCount !== null && <Badge tone="pending">{pendingCount} pending</Badge>}
      </div>

      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-slate-200 bg-white p-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600">Status</label>
          <select
            value={status}
            onChange={(e) => resetAndSet(setStatus)(e.target.value as VideoCandidateStatus)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          >
            <option value="pending">Pending</option>
            <option value="approved">Approved</option>
            <option value="rejected">Rejected</option>
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600">Product ID</label>
          <input
            value={productId}
            onChange={(e) => resetAndSet(setProductId)(e.target.value)}
            placeholder="uuid"
            className="w-64 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </div>
      </div>

      {error && <div className="rounded-md bg-danger-light px-4 py-3 text-sm text-red-800">{error}</div>}

      <DataTable
        columns={columns}
        rows={items}
        getRowId={(r) => r.id}
        selectable={status === "pending"}
        selectedIds={selectedIds}
        onSelectionChange={setSelectedIds}
        bulkActions={bulkActions}
        emptyMessage={loading ? "Loading…" : "Nothing here."}
      />

      <Pagination offset={offset} limit={LIMIT} itemCount={items.length} onOffsetChange={setOffset} />

      <Modal
        open={rejectTarget !== null}
        onClose={() => (submitting ? undefined : setRejectTarget(null))}
        title={`Reject ${rejectTarget?.length ?? 0} video${(rejectTarget?.length ?? 0) === 1 ? "" : "s"}`}
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
        <label className="mb-1 block text-xs font-medium text-slate-600">Reason (optional, applied to all selected)</label>
        <textarea
          value={rejectReason}
          onChange={(e) => setRejectReason(e.target.value)}
          rows={3}
          className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-primary focus:outline-none"
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
            <Button variant="primary" onClick={confirmReassign} disabled={submitting || !reassignProductId}>
              {submitting ? "Reassigning…" : "Reassign"}
            </Button>
          </>
        }
      >
        <p className="mb-2 text-sm text-slate-600">
          Moves "{reassignTarget?.title}" to a different product. The original candidate is tombstoned as rejected on{" "}
          {reassignTarget?.brand_name} {reassignTarget?.product_name}.
        </p>
        <label className="mb-1 block text-xs font-medium text-slate-600">Target product ID</label>
        <input
          value={reassignProductId}
          onChange={(e) => setReassignProductId(e.target.value)}
          placeholder="uuid"
          className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-primary focus:outline-none"
        />
      </Modal>
    </div>
  );
}
