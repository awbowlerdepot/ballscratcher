import { useEffect, useState } from "react";
import {
  approvePriceSource,
  createPriceSite,
  deletePriceSite,
  listPriceSites,
  listPriceSources,
  rejectPriceSource,
  restorePriceSource,
  updatePriceSite,
} from "../api/client";
import type {
  ListPriceSourcesParams,
  PriceSite,
  PriceSiteCreateInput,
  PriceSiteFetchMethod,
  PriceSource,
  PriceSourceStatus,
} from "../api/types";
import Badge from "../components/Badge";
import Button from "../components/Button";
import type { BulkAction, Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import Modal from "../components/Modal";
import Pagination from "../components/Pagination";
import { useToast } from "../components/Toast";

const LIMIT = 50;

// Same sequential-with-300ms-delay pacing as Review Queue/Video
// Candidates' own bulk helpers (task #255/#219) -- duplicated here
// rather than shared/extracted, matching this project's established
// per-page convention.
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

function fmtDate(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

// Al: "the href ... is relative ... it needs to be fully qualified" --
// same defensive resolve admin-site's own resolveExternalUrl does,
// since product_url is SUPPOSED to already be absolute by the time it's
// stored but isn't guaranteed to be (see list_product_price_sources'
// own docstring on why base_url is exposed at all).
function resolveExternalUrl(url: string, baseUrl: string | null): string {
  try {
    return new URL(url, baseUrl ?? undefined).toString();
  } catch {
    return url;
  }
}

const EMPTY_SITE_FORM = {
  name: "",
  fetchMethod: "scrape" as PriceSiteFetchMethod,
  searchUrlTemplate: "",
  resultLinkSelector: "",
  defaultCssSelector: "",
  apiProvider: "",
  baseUrl: "",
  notes: "",
};

// Ports admin-site/index.html's two separate top-level tabs -- "Price
// Sources" (the discovered-match review queue, product_price_sources)
// and "Price Sites" (the retailer registry, price_sites) -- into one
// page, per Al's own question about whether combining them made sense.
// They're related (every source points at a site via price_site_id)
// but admin-site never actually combines their UI; nothing about the
// two backend resources conflicts, so this page shows Sources first
// (the higher-churn review queue) with Sites underneath (read-mostly,
// expected to stay small -- see list_price_sites' own docstring).
export default function PriceSitesPage() {
  const { show } = useToast();

  // --- Price Sources (review queue) ---
  const [sources, setSources] = useState<PriceSource[]>([]);
  const [sourcesPendingCount, setSourcesPendingCount] = useState<number | null>(null);
  const [sourcesLoading, setSourcesLoading] = useState(true);
  const [sourcesError, setSourcesError] = useState<string | null>(null);
  const [selectedSourceIds, setSelectedSourceIds] = useState<Set<string>>(new Set());
  const [sourcesOffset, setSourcesOffset] = useState(0);
  const [sourceStatus, setSourceStatus] = useState<PriceSourceStatus>("pending");
  const [sourceProductId, setSourceProductId] = useState("");
  const [rejectTarget, setRejectTarget] = useState<PriceSource[] | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [rejectSubmitting, setRejectSubmitting] = useState(false);

  function loadSources() {
    const params: ListPriceSourcesParams = {
      status: sourceStatus,
      product_id: sourceProductId || undefined,
      limit: LIMIT,
      offset: sourcesOffset,
    };
    setSourcesLoading(true);
    setSourcesError(null);
    listPriceSources(params)
      .then((r) => {
        setSources(r.items);
        setSourcesPendingCount(r.pending_count);
      })
      .catch((err) => setSourcesError(err instanceof Error ? err.message : "Failed to load price sources."))
      .finally(() => setSourcesLoading(false));
  }

  useEffect(loadSources, [sourceStatus, sourceProductId, sourcesOffset]);

  function resetSourcesAndSet<T>(setter: (v: T) => void) {
    return (value: T) => {
      setSourcesOffset(0);
      setSelectedSourceIds(new Set());
      setter(value);
    };
  }

  async function handleApproveSources(rows: PriceSource[]) {
    const { succeeded, failed } = await sequentialWithDelay(
      rows.map((r) => r.id),
      approvePriceSource,
    );
    show(
      failed === 0
        ? `Approved ${succeeded} source${succeeded === 1 ? "" : "s"} -- daily price checking starts for ${succeeded === 1 ? "it" : "them"}.`
        : `Approved ${succeeded}, ${failed} failed.`,
      failed === 0 ? "ok" : "danger",
    );
    setSelectedSourceIds(new Set());
    loadSources();
  }

  function openRejectModal(rows: PriceSource[]) {
    setRejectTarget(rows);
    setRejectReason("");
  }

  async function confirmReject() {
    if (!rejectTarget) return;
    setRejectSubmitting(true);
    try {
      const { succeeded, failed } = await sequentialWithDelay(
        rejectTarget.map((r) => r.id),
        (id) => rejectPriceSource(id, rejectReason || undefined),
      );
      show(
        failed === 0 ? `Rejected ${succeeded} source${succeeded === 1 ? "" : "s"}.` : `Rejected ${succeeded}, ${failed} failed.`,
        failed === 0 ? "ok" : "danger",
      );
      setSelectedSourceIds(new Set());
      setRejectTarget(null);
      loadSources();
    } finally {
      setRejectSubmitting(false);
    }
  }

  async function handleRestoreSource(row: PriceSource) {
    try {
      await restorePriceSource(row.id);
      show("Restored to pending.", "ok");
      loadSources();
    } catch (err) {
      show(err instanceof Error ? err.message : "Restore failed.", "danger");
    }
  }

  const sourceColumns: Column<PriceSource>[] = [
    {
      key: "site",
      header: "Site / URL",
      render: (r) => (
        <div>
          <a
            href={resolveExternalUrl(r.product_url, r.base_url)}
            target="_blank"
            rel="noreferrer"
            className="font-medium text-ink-800 hover:text-primary"
          >
            {r.site_name}
          </a>
          <div className="text-xs text-ink-500">
            {r.source === "manual" ? "manually added" : `auto-matched, ${r.match_confidence ?? "—"} confidence`}
          </div>
        </div>
      ),
    },
    {
      key: "product_name",
      header: "Product",
      render: (r) => (
        <div>
          {r.product_name}
          <div className="text-xs text-ink-500">{r.brand_name}</div>
        </div>
      ),
    },
    { key: "created_at", header: "Found", render: (r) => fmtDate(r.created_at) },
    {
      key: "actions",
      header: "",
      render: (r) => (
        <div className="flex flex-wrap gap-1.5">
          {r.status === "pending" ? (
            <>
              <Button size="sm" variant="primary" onClick={() => handleApproveSources([r])}>
                Approve
              </Button>
              <Button size="sm" variant="danger" onClick={() => openRejectModal([r])}>
                Reject
              </Button>
            </>
          ) : (
            <>
              <Badge tone={r.status === "approved" ? "ok" : "danger"}>{r.status}</Badge>
              <Button size="sm" variant="secondary" onClick={() => handleRestoreSource(r)}>
                Undo
              </Button>
            </>
          )}
        </div>
      ),
    },
  ];

  const sourceBulkActions: BulkAction<PriceSource>[] =
    sourceStatus === "pending"
      ? [
          { label: "Approve selected", onClick: handleApproveSources, variant: "primary" },
          { label: "Reject selected", onClick: openRejectModal, variant: "danger" },
        ]
      : [];

  // --- Price Sites (registry) ---
  const [sites, setSites] = useState<PriceSite[]>([]);
  const [sitesLoading, setSitesLoading] = useState(true);
  const [sitesError, setSitesError] = useState<string | null>(null);
  const [newSite, setNewSite] = useState(EMPTY_SITE_FORM);
  const [creatingSite, setCreatingSite] = useState(false);
  const [editTarget, setEditTarget] = useState<PriceSite | null>(null);
  const [editForm, setEditForm] = useState(EMPTY_SITE_FORM);
  const [editSubmitting, setEditSubmitting] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<PriceSite | null>(null);
  const [deleteSubmitting, setDeleteSubmitting] = useState(false);

  function loadSites() {
    setSitesLoading(true);
    setSitesError(null);
    listPriceSites()
      .then(setSites)
      .catch((err) => setSitesError(err instanceof Error ? err.message : "Failed to load price sites."))
      .finally(() => setSitesLoading(false));
  }

  useEffect(loadSites, []);

  async function handleCreateSite() {
    if (!newSite.name.trim()) {
      show("Name is required.", "danger");
      return;
    }
    if (newSite.fetchMethod === "scrape") {
      if (!newSite.searchUrlTemplate.trim() || !newSite.resultLinkSelector.trim() || !newSite.defaultCssSelector.trim()) {
        show("Search URL template, result link selector, and default price selector are all required for a scrape site.", "danger");
        return;
      }
      if (!newSite.searchUrlTemplate.includes("{query}")) {
        show("Search URL template needs a {query} placeholder.", "danger");
        return;
      }
    } else if (!newSite.apiProvider.trim()) {
      show('API provider is required for an API site (e.g. "bigcommerce").', "danger");
      return;
    }
    const input: PriceSiteCreateInput = {
      name: newSite.name.trim(),
      fetch_method: newSite.fetchMethod,
      notes: newSite.notes.trim() || undefined,
      ...(newSite.fetchMethod === "scrape"
        ? {
            search_url_template: newSite.searchUrlTemplate.trim(),
            result_link_selector: newSite.resultLinkSelector.trim(),
            default_css_selector: newSite.defaultCssSelector.trim(),
          }
        : {
            api_provider: newSite.apiProvider.trim(),
            base_url: newSite.baseUrl.trim() || undefined,
          }),
    };
    setCreatingSite(true);
    try {
      await createPriceSite(input);
      show(`Added ${input.name}.`, "ok");
      setNewSite(EMPTY_SITE_FORM);
      loadSites();
    } catch (err) {
      show(err instanceof Error ? err.message : "Failed to add site.", "danger");
    } finally {
      setCreatingSite(false);
    }
  }

  function openEditModal(site: PriceSite) {
    setEditTarget(site);
    setEditForm({
      name: site.name,
      fetchMethod: site.fetch_method,
      searchUrlTemplate: site.search_url_template ?? "",
      resultLinkSelector: site.result_link_selector ?? "",
      defaultCssSelector: site.default_css_selector ?? "",
      apiProvider: site.api_provider ?? "",
      baseUrl: site.base_url ?? "",
      notes: site.notes ?? "",
    });
  }

  async function confirmEdit() {
    if (!editTarget) return;
    setEditSubmitting(true);
    try {
      await updatePriceSite(editTarget.id, {
        name: editForm.name.trim(),
        notes: editForm.notes.trim(),
        ...(editTarget.fetch_method === "api"
          ? { api_provider: editForm.apiProvider.trim(), base_url: editForm.baseUrl.trim() }
          : {
              search_url_template: editForm.searchUrlTemplate.trim(),
              result_link_selector: editForm.resultLinkSelector.trim(),
              default_css_selector: editForm.defaultCssSelector.trim(),
            }),
      });
      show("Updated.", "ok");
      setEditTarget(null);
      loadSites();
    } catch (err) {
      show(err instanceof Error ? err.message : "Update failed.", "danger");
    } finally {
      setEditSubmitting(false);
    }
  }

  async function handleToggleActive(site: PriceSite) {
    try {
      await updatePriceSite(site.id, { is_active: !site.is_active });
      loadSites();
    } catch (err) {
      show(err instanceof Error ? err.message : "Failed to update.", "danger");
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleteSubmitting(true);
    try {
      await deletePriceSite(deleteTarget.id);
      show("Deleted.", "ok");
      setDeleteTarget(null);
      loadSites();
    } catch (err) {
      show(err instanceof Error ? err.message : "Delete failed.", "danger");
    } finally {
      setDeleteSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-8">
      <section className="flex flex-col gap-4">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-semibold text-ink-800">Price Sources</h1>
          {sourcesPendingCount !== null && <Badge tone="pending">{sourcesPendingCount} pending</Badge>}
        </div>

        <div className="flex flex-wrap items-end gap-3 rounded-lg border border-ink-200 bg-ink-100 p-3">
          <div>
            <label className="mb-1 block text-xs font-medium text-ink-600">Status</label>
            <select
              value={sourceStatus}
              onChange={(e) => resetSourcesAndSet(setSourceStatus)(e.target.value as PriceSourceStatus)}
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
              value={sourceProductId}
              onChange={(e) => resetSourcesAndSet(setSourceProductId)(e.target.value)}
              placeholder="uuid"
              className="w-64 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
            />
          </div>
        </div>

        {sourcesError && <div className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{sourcesError}</div>}

        <DataTable
          columns={sourceColumns}
          rows={sources}
          getRowId={(r) => r.id}
          selectable={sourceStatus === "pending"}
          selectedIds={selectedSourceIds}
          onSelectionChange={setSelectedSourceIds}
          bulkActions={sourceBulkActions}
          emptyMessage={sourcesLoading ? "Loading…" : "Nothing here."}
        />

        <Pagination offset={sourcesOffset} limit={LIMIT} itemCount={sources.length} onOffsetChange={setSourcesOffset} />
      </section>

      <section className="flex flex-col gap-4 border-t border-ink-200 pt-6">
        <h2 className="text-xl font-semibold text-ink-800">Price Sites</h2>
        <p className="text-sm text-ink-500">
          The registry of retailers price_checker's discovery job searches. Adding a site here makes it eligible for the
          next "find price sources" pass on any product -- no new deploy needed.
        </p>

        <div className="flex flex-col gap-3 rounded-lg border border-ink-200 bg-ink-100 p-4">
          <h3 className="text-sm font-semibold text-ink-800">Add a price site</h3>
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <label className="mb-1 block text-xs font-medium text-ink-600">Name</label>
              <input
                value={newSite.name}
                onChange={(e) => setNewSite({ ...newSite, name: e.target.value })}
                placeholder="e.g. BowlingBall.com"
                className="w-56 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-ink-600">Fetch method</label>
              <select
                value={newSite.fetchMethod}
                onChange={(e) => setNewSite({ ...newSite, fetchMethod: e.target.value as PriceSiteFetchMethod })}
                className="rounded-md border border-ink-300 px-2 py-1.5 text-sm"
              >
                <option value="scrape">Scrape (search + CSS selector)</option>
                <option value="api">API (e.g. BowlerDepot/BigCommerce)</option>
              </select>
            </div>
          </div>

          {newSite.fetchMethod === "scrape" ? (
            <div className="flex flex-wrap items-end gap-3">
              <div>
                <label className="mb-1 block text-xs font-medium text-ink-600">Search URL template</label>
                <input
                  value={newSite.searchUrlTemplate}
                  onChange={(e) => setNewSite({ ...newSite, searchUrlTemplate: e.target.value })}
                  placeholder="https://example.com/search?q={query}"
                  className="w-72 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-ink-600">Result link selector</label>
                <input
                  value={newSite.resultLinkSelector}
                  onChange={(e) => setNewSite({ ...newSite, resultLinkSelector: e.target.value })}
                  placeholder=".product-item-link"
                  className="w-48 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-ink-600">Default price selector</label>
                <input
                  value={newSite.defaultCssSelector}
                  onChange={(e) => setNewSite({ ...newSite, defaultCssSelector: e.target.value })}
                  placeholder="[itemprop=price]"
                  className="w-48 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
                />
              </div>
            </div>
          ) : (
            <div className="flex flex-wrap items-end gap-3">
              <div>
                <label className="mb-1 block text-xs font-medium text-ink-600">API provider</label>
                <input
                  value={newSite.apiProvider}
                  onChange={(e) => setNewSite({ ...newSite, apiProvider: e.target.value })}
                  placeholder="bigcommerce"
                  className="w-48 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-ink-600">Storefront base URL</label>
                <input
                  value={newSite.baseUrl}
                  onChange={(e) => setNewSite({ ...newSite, baseUrl: e.target.value })}
                  placeholder="https://www.bowlerdepot.com"
                  className="w-64 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
                />
              </div>
            </div>
          )}

          <div className="flex items-end gap-3">
            <div className="flex-1">
              <label className="mb-1 block text-xs font-medium text-ink-600">Notes (optional)</label>
              <input
                value={newSite.notes}
                onChange={(e) => setNewSite({ ...newSite, notes: e.target.value })}
                className="w-full max-w-md rounded-md border border-ink-300 px-2 py-1.5 text-sm"
              />
            </div>
            <Button variant="primary" onClick={handleCreateSite} disabled={creatingSite}>
              {creatingSite ? "Adding…" : "Add site"}
            </Button>
          </div>
        </div>

        {sitesError && <div className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{sitesError}</div>}

        <DataTable
          columns={[
            {
              key: "name",
              header: "Name",
              render: (s: PriceSite) => (
                <div>
                  {s.name} {!s.is_active && <Badge tone="muted">inactive</Badge>}
                  {s.notes && <div className="text-xs text-ink-500">{s.notes}</div>}
                </div>
              ),
            },
            {
              key: "config",
              header: "Config (search/selectors, or API provider/base URL)",
              render: (s: PriceSite) =>
                s.fetch_method === "api" ? (
                  <div>
                    <Badge tone="muted">api: {s.api_provider ?? "?"}</Badge>
                    {s.base_url && <div className="font-mono text-xs text-ink-500">{s.base_url}</div>}
                  </div>
                ) : (
                  <div className="font-mono text-xs text-ink-500">
                    <div>{s.search_url_template}</div>
                    <div>{s.result_link_selector}</div>
                    <div>{s.default_css_selector}</div>
                  </div>
                ),
            },
            {
              key: "actions",
              header: "",
              render: (s: PriceSite) => (
                <div className="flex flex-wrap gap-1.5">
                  <Button size="sm" variant="secondary" onClick={() => openEditModal(s)}>
                    Edit
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => handleToggleActive(s)}>
                    {s.is_active ? "Deactivate" : "Reactivate"}
                  </Button>
                  <Button size="sm" variant="ghost" className="text-danger" onClick={() => setDeleteTarget(s)}>
                    Delete
                  </Button>
                </div>
              ),
            },
          ]}
          rows={sites}
          getRowId={(s) => s.id}
          emptyMessage={sitesLoading ? "Loading…" : "No price sites configured yet -- add one above."}
        />
      </section>

      <Modal
        open={rejectTarget !== null}
        onClose={() => (rejectSubmitting ? undefined : setRejectTarget(null))}
        title={`Reject ${rejectTarget?.length ?? 0} source${(rejectTarget?.length ?? 0) === 1 ? "" : "s"}`}
        footer={
          <>
            <Button variant="secondary" onClick={() => setRejectTarget(null)} disabled={rejectSubmitting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={confirmReject} disabled={rejectSubmitting}>
              {rejectSubmitting ? "Rejecting…" : "Reject"}
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

      <Modal
        open={editTarget !== null}
        onClose={() => (editSubmitting ? undefined : setEditTarget(null))}
        title={`Edit ${editTarget?.name ?? ""}`}
        footer={
          <>
            <Button variant="secondary" onClick={() => setEditTarget(null)} disabled={editSubmitting}>
              Cancel
            </Button>
            <Button variant="primary" onClick={confirmEdit} disabled={editSubmitting}>
              {editSubmitting ? "Saving…" : "Save"}
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <div>
            <label className="mb-1 block text-xs font-medium text-ink-600">Name</label>
            <input
              value={editForm.name}
              onChange={(e) => setEditForm({ ...editForm, name: e.target.value })}
              className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm"
            />
          </div>
          {/* fetch_method itself isn't editable here -- switching a site between
              'scrape' and 'api' would also mean swapping which of the fields
              below even apply (the DB's own fetch-method check constraint);
              that's a delete-and-recreate, not a quick edit. */}
          {editTarget?.fetch_method === "api" ? (
            <>
              <div>
                <label className="mb-1 block text-xs font-medium text-ink-600">API provider</label>
                <input
                  value={editForm.apiProvider}
                  onChange={(e) => setEditForm({ ...editForm, apiProvider: e.target.value })}
                  className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-ink-600">Storefront base URL</label>
                <input
                  value={editForm.baseUrl}
                  onChange={(e) => setEditForm({ ...editForm, baseUrl: e.target.value })}
                  className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm"
                />
              </div>
            </>
          ) : (
            <>
              <div>
                <label className="mb-1 block text-xs font-medium text-ink-600">Search URL template</label>
                <input
                  value={editForm.searchUrlTemplate}
                  onChange={(e) => setEditForm({ ...editForm, searchUrlTemplate: e.target.value })}
                  className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-ink-600">Result link selector</label>
                <input
                  value={editForm.resultLinkSelector}
                  onChange={(e) => setEditForm({ ...editForm, resultLinkSelector: e.target.value })}
                  className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-ink-600">Default price selector</label>
                <input
                  value={editForm.defaultCssSelector}
                  onChange={(e) => setEditForm({ ...editForm, defaultCssSelector: e.target.value })}
                  className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm"
                />
              </div>
            </>
          )}
          <div>
            <label className="mb-1 block text-xs font-medium text-ink-600">Notes</label>
            <input
              value={editForm.notes}
              onChange={(e) => setEditForm({ ...editForm, notes: e.target.value })}
              className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm"
            />
          </div>
        </div>
      </Modal>

      <Modal
        open={deleteTarget !== null}
        onClose={() => (deleteSubmitting ? undefined : setDeleteTarget(null))}
        title={`Delete ${deleteTarget?.name ?? ""}?`}
        footer={
          <>
            <Button variant="secondary" onClick={() => setDeleteTarget(null)} disabled={deleteSubmitting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={confirmDelete} disabled={deleteSubmitting}>
              {deleteSubmitting ? "Deleting…" : "Delete"}
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-600">
          This permanently deletes every product's price-source rows and history for this site. This cannot be undone.
        </p>
      </Modal>
    </div>
  );
}
