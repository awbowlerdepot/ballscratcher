import { useEffect, useState } from "react";
import { approveReviewItem, listReviewQueue, rejectReviewItem } from "../api/client";
import type { ListReviewQueueParams, ReviewQueueItem, ReviewQueueStatus } from "../api/types";
import Badge from "../components/Badge";
import Button from "../components/Button";
import type { BulkAction, Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import Modal from "../components/Modal";
import Pagination from "../components/Pagination";
import { useToast } from "../components/Toast";

const LIMIT = 50;

// Sequential-with-delay, not Promise.all -- matches admin-site/
// index.html's own bulk approve/reject (task #219), which added the
// 300ms pause specifically to avoid API Gateway/Lambda throttling seen
// in production when a batch of these fired concurrently. Returns how
// many of each outcome so the caller can toast a real summary rather
// than assuming every call succeeded.
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

// Ports admin-site/index.html's Review Queue tab -- the moderation
// queue for scraped-field corrections (a "field_name" is either a
// whitelisted product column or a per-SKU rg/differential/mass_bias
// value, see api/types.ts's ReviewQueueItem comment). No inline-edit of
// proposed_value exists on either the old UI or the backend -- approve
// always applies the value exactly as scraped; a reviewer's only lever
// is approve/reject (with an optional shared reason on reject).
export default function ReviewQueuePage() {
  const { show } = useToast();
  const [items, setItems] = useState<ReviewQueueItem[]>([]);
  const [pendingCount, setPendingCount] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [offset, setOffset] = useState(0);

  const [status, setStatus] = useState<ReviewQueueStatus>("pending");
  const [productId, setProductId] = useState("");

  const [rejectTarget, setRejectTarget] = useState<ReviewQueueItem[] | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [submitting, setSubmitting] = useState(false);

  function load() {
    const params: ListReviewQueueParams = {
      status,
      product_id: productId || undefined,
      limit: LIMIT,
      offset,
    };
    setLoading(true);
    setError(null);
    listReviewQueue(params)
      .then((r) => {
        setItems(r.items);
        setPendingCount(r.pending_count);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load review queue."))
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

  async function handleApprove(rows: ReviewQueueItem[]) {
    const { succeeded, failed } = await sequentialWithDelay(
      rows.map((r) => r.id),
      approveReviewItem,
    );
    show(
      failed === 0
        ? `Approved ${succeeded} item${succeeded === 1 ? "" : "s"}.`
        : `Approved ${succeeded}, ${failed} failed -- see console/network tab.`,
      failed === 0 ? "ok" : "danger",
    );
    setSelectedIds(new Set());
    load();
  }

  function openRejectModal(rows: ReviewQueueItem[]) {
    setRejectTarget(rows);
    setRejectReason("");
  }

  async function confirmReject() {
    if (!rejectTarget) return;
    setSubmitting(true);
    try {
      const { succeeded, failed } = await sequentialWithDelay(
        rejectTarget.map((r) => r.id),
        (id) => rejectReviewItem(id, rejectReason || undefined),
      );
      show(
        failed === 0
          ? `Rejected ${succeeded} item${succeeded === 1 ? "" : "s"}.`
          : `Rejected ${succeeded}, ${failed} failed -- see console/network tab.`,
        failed === 0 ? "ok" : "danger",
      );
      setSelectedIds(new Set());
      setRejectTarget(null);
      load();
    } finally {
      setSubmitting(false);
    }
  }

  const columns: Column<ReviewQueueItem>[] = [
    {
      key: "product_name",
      header: "Product",
      render: (r) => (
        <a href={r.product_url} target="_blank" rel="noreferrer" className="font-medium text-ink-800 hover:text-primary">
          {r.product_name}
        </a>
      ),
    },
    { key: "field_name", header: "Field", render: (r) => <code className="text-xs">{r.field_name}</code> },
    {
      key: "current_value",
      header: "Current",
      render: (r) => <span className="font-mono text-xs text-ink-500">{r.current_value ?? "—"}</span>,
    },
    {
      key: "proposed_value",
      header: "Proposed",
      render: (r) => <span className="font-mono text-xs font-semibold text-primary-dark">{r.proposed_value ?? "—"}</span>,
    },
    { key: "source", header: "Source", render: (r) => r.source ?? "—" },
    {
      key: "status",
      header: "Status",
      render: (r) => (
        <Badge tone={r.status === "approved" ? "ok" : r.status === "rejected" ? "danger" : "pending"}>{r.status}</Badge>
      ),
    },
    { key: "created_at", header: "Created", render: (r) => new Date(r.created_at).toLocaleDateString() },
    ...(status === "pending"
      ? [
          {
            key: "actions",
            header: "",
            render: (r: ReviewQueueItem) => (
              <div className="flex gap-1.5">
                <Button size="sm" variant="primary" onClick={() => handleApprove([r])}>
                  Approve
                </Button>
                <Button size="sm" variant="danger" onClick={() => openRejectModal([r])}>
                  Reject
                </Button>
              </div>
            ),
          } as Column<ReviewQueueItem>,
        ]
      : []),
  ];

  const bulkActions: BulkAction<ReviewQueueItem>[] =
    status === "pending"
      ? [
          { label: "Approve selected", onClick: handleApprove, variant: "primary" },
          { label: "Reject selected", onClick: openRejectModal, variant: "danger" },
        ]
      : [];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-3">
        <h1 className="text-xl font-semibold text-ink-800">Review Queue</h1>
        {pendingCount !== null && <Badge tone="pending">{pendingCount} pending</Badge>}
      </div>

      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-ink-200 bg-ink-100 p-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-ink-600">Status</label>
          <select
            value={status}
            onChange={(e) => resetAndSet(setStatus)(e.target.value as ReviewQueueStatus)}
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
        title={`Reject ${rejectTarget?.length ?? 0} item${(rejectTarget?.length ?? 0) === 1 ? "" : "s"}`}
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
        <label className="mb-1 block text-xs font-medium text-ink-600">Reason (optional, applied to all selected)</label>
        <textarea
          value={rejectReason}
          onChange={(e) => setRejectReason(e.target.value)}
          rows={3}
          className="w-full rounded-md border border-ink-300 px-3 py-2 text-sm focus:border-primary focus:outline-none"
        />
      </Modal>
    </div>
  );
}
