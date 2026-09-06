import { useEffect, useState } from "react";
import { getCore, listCores } from "../api/client";
import type { Core, CoreDetail, ListCoresParams } from "../api/types";
import Badge from "../components/Badge";
import Button from "../components/Button";
import type { Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import Modal from "../components/Modal";
import Pagination from "../components/Pagination";
import { useToast } from "../components/Toast";

const LIMIT = 50;

function fmtDate(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

// Ports admin-site/index.html's Cores tab -- the "other direction" view
// of products.core_id (007_cores_table.sql): one row per physical core
// (Al's example: DV8's Collision core, used by six differently-named
// balls), with a product_count so a many-products-to-one-core case is
// visible at a glance instead of only inferable by spotting the same
// core name repeated across several Products rows. Read-only, matching
// admin_api -- no create/update/delete endpoints exist for cores at all
// (rows come from the scrapers' own get_or_create_core_id, never by
// hand through this API). Products opens in a Modal here instead of
// admin-site's inline expand-row, matching Articles' own preview
// pattern rather than DataTable's row-toggle shape (which this app's
// component library doesn't support).
export default function CoresPage() {
  const { show } = useToast();
  const [items, setItems] = useState<Core[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);

  const [search, setSearch] = useState("");
  const [brandId, setBrandId] = useState("");

  const [detailId, setDetailId] = useState<string | null>(null);
  const [detail, setDetail] = useState<CoreDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  function load() {
    const params: ListCoresParams = {
      search: search || undefined,
      brand_id: brandId || undefined,
      limit: LIMIT,
      offset,
    };
    setLoading(true);
    setError(null);
    listCores(params)
      .then(setItems)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load cores."))
      .finally(() => setLoading(false));
  }

  useEffect(load, [search, brandId, offset]);

  function resetAndSet<T>(setter: (v: T) => void) {
    return (value: T) => {
      setOffset(0);
      setter(value);
    };
  }

  function openDetail(id: string) {
    setDetailId(id);
    setDetail(null);
    setDetailLoading(true);
    getCore(id)
      .then(setDetail)
      .catch((err) => show(err instanceof Error ? err.message : "Failed to load core.", "danger"))
      .finally(() => setDetailLoading(false));
  }

  const columns: Column<Core>[] = [
    { key: "brand_name", header: "Brand", render: (c) => c.brand_name },
    { key: "name", header: "Core Name", render: (c) => c.name },
    {
      key: "core_type",
      header: "Type",
      render: (c) => c.core_type ?? <span className="text-ink-400">—</span>,
    },
    {
      key: "product_count",
      header: "Products",
      render: (c) => <Badge tone={c.product_count > 0 ? "ok" : "muted"}>{c.product_count}</Badge>,
    },
    { key: "created_at", header: "Created", render: (c) => fmtDate(c.created_at) },
    {
      key: "actions",
      header: "",
      render: (c) => (
        <Button size="sm" variant="ghost" onClick={() => openDetail(c.id)}>
          Products
        </Button>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold text-ink-800">Cores</h1>

      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-ink-200 bg-ink-100 p-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-ink-600">Search core name</label>
          <input
            value={search}
            onChange={(e) => resetAndSet(setSearch)(e.target.value)}
            placeholder="e.g. collision"
            className="w-56 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
          />
        </div>
        <div>
          {/* Raw brand-id text field, same as Products' own filter bar --
              there's no GET /brands on the admin API for a real dropdown
              yet (see admin-spa/README.md's "what's not here yet"). */}
          <label className="mb-1 block text-xs font-medium text-ink-600">Brand ID</label>
          <input
            value={brandId}
            onChange={(e) => resetAndSet(setBrandId)(e.target.value)}
            placeholder="uuid"
            className="w-64 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
          />
        </div>
      </div>

      {error && <div className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{error}</div>}

      <DataTable columns={columns} rows={items} getRowId={(c) => c.id} emptyMessage={loading ? "Loading…" : "No cores match."} />

      <Pagination offset={offset} limit={LIMIT} itemCount={items.length} onOffsetChange={setOffset} />

      <Modal open={detailId !== null} onClose={() => setDetailId(null)} title={detail ? `${detail.brand_name} ${detail.name}` : "Core"}>
        {detailLoading && <p className="text-sm text-ink-500">Loading…</p>}
        {detail && (
          <div className="flex flex-col gap-3 text-sm">
            <p className="text-ink-600">
              {detail.products.length} product{detail.products.length === 1 ? "" : "s"} using this core
              {detail.core_type && (
                <>
                  {" "}
                  &middot; type: <span className="font-medium text-ink-800">{detail.core_type}</span>
                </>
              )}
              {detail.release_era && (
                <>
                  {" "}
                  &middot; era: <span className="font-medium text-ink-800">{detail.release_era}</span>
                </>
              )}
            </p>
            {detail.products.length === 0 ? (
              <p className="text-xs text-ink-400">
                None -- likely an orphaned row (every referencing product was reassigned or rescraped under a different
                core), safe to investigate for cleanup.
              </p>
            ) : (
              <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-ink-200 text-xs uppercase tracking-wide text-ink-500">
                  <tr>
                    <th className="py-1.5 pr-3 font-medium">Name</th>
                    <th className="py-1.5 pr-3 font-medium">Status</th>
                    <th className="py-1.5 pr-3 font-medium">Published</th>
                    <th className="py-1.5 pr-3 font-medium">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.products.map((p) => (
                    <tr key={p.id} className="border-b border-ink-100 last:border-0">
                      <td className="py-1.5 pr-3">
                        <a href={p.url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
                          {p.name}
                        </a>
                      </td>
                      <td className="py-1.5 pr-3">{p.status}</td>
                      <td className="py-1.5 pr-3">
                        <Badge tone={p.published ? "ok" : "muted"}>{p.published ? "published" : "unpublished"}</Badge>
                      </td>
                      <td className="py-1.5 pr-3">{fmtDate(p.updated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </div>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}
