import { useEffect, useState } from "react";
import { getCoverstock, listCoverstocks } from "../api/client";
import type { Coverstock, CoverstockDetail, ListCoverstocksParams } from "../api/types";
import Badge from "../components/Badge";
import Button from "../components/Button";
import type { Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import Modal from "../components/Modal";
import Pagination from "../components/Pagination";
import Skeleton from "../components/Skeleton";
import { useToast } from "../components/Toast";

const LIMIT = 50;

function fmtDate(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

// Ports admin-site/index.html's Coverstocks tab -- the exact same "other
// direction" view as Cores (008_coverstocks_table.sql, one migration
// after cores' 007): one row per named coverstock formulation, scoped to
// a brand, with a product_count so a many-products-to-one-coverstock
// case is visible at a glance. Read-only, matching admin_api -- no
// create/update/delete endpoints exist for coverstocks either; rows come
// from the scrapers' own get_or_create_coverstock_id. Structurally this
// is CoresPage.tsx with core_type/release_era swapped for material/type
// -- kept as its own file (not a shared generic component) since that's
// the same choice CoresPage itself made and there's no third "shared
// lookup" tab on the horizon to justify the abstraction yet.
export default function CoverstocksPage() {
  const { show } = useToast();
  const [items, setItems] = useState<Coverstock[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);

  const [search, setSearch] = useState("");
  const [brandId, setBrandId] = useState("");

  const [detailId, setDetailId] = useState<string | null>(null);
  const [detail, setDetail] = useState<CoverstockDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  function load() {
    const params: ListCoverstocksParams = {
      search: search || undefined,
      brand_id: brandId || undefined,
      limit: LIMIT,
      offset,
    };
    setLoading(true);
    setError(null);
    listCoverstocks(params)
      .then(setItems)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load coverstocks."))
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
    getCoverstock(id)
      .then(setDetail)
      .catch((err) => show(err instanceof Error ? err.message : "Failed to load coverstock.", "danger"))
      .finally(() => setDetailLoading(false));
  }

  const columns: Column<Coverstock>[] = [
    { key: "brand_name", header: "Brand", render: (cs) => cs.brand_name },
    { key: "name", header: "Coverstock Name", render: (cs) => cs.name },
    {
      key: "material",
      header: "Material",
      render: (cs) => cs.material ?? <span className="text-ink-400">—</span>,
    },
    {
      key: "type",
      header: "Type",
      render: (cs) => cs.type ?? <span className="text-ink-400">—</span>,
    },
    {
      key: "product_count",
      header: "Products",
      render: (cs) => <Badge tone={cs.product_count > 0 ? "ok" : "muted"}>{cs.product_count}</Badge>,
    },
    { key: "created_at", header: "Created", render: (cs) => fmtDate(cs.created_at) },
    {
      key: "actions",
      header: "",
      render: (cs) => (
        <Button size="sm" variant="ghost" onClick={() => openDetail(cs.id)}>
          Products
        </Button>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold text-ink-800">Coverstocks</h1>

      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-ink-200 bg-ink-100 p-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-ink-600">Search coverstock name</label>
          <input
            value={search}
            onChange={(e) => resetAndSet(setSearch)(e.target.value)}
            placeholder="e.g. pearl reactive"
            className="w-56 rounded-md border border-ink-300 px-2 py-1.5 text-sm"
          />
        </div>
        <div>
          {/* Raw brand-id text field, same as Products/Cores -- no
              GET /brands on the admin API yet for a real dropdown. */}
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

      <DataTable
        columns={columns}
        rows={items}
        getRowId={(cs) => cs.id}
        emptyMessage="No coverstocks match."
        loading={loading}
      />

      <Pagination offset={offset} limit={LIMIT} itemCount={items.length} onOffsetChange={setOffset} />

      <Modal
        open={detailId !== null}
        onClose={() => setDetailId(null)}
        title={detail ? `${detail.brand_name} ${detail.name}` : "Coverstock"}
      >
        {detailLoading && (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-4 w-1/2" />
            <Skeleton className="h-24 w-full" />
          </div>
        )}
        {detail && (
          <div className="flex flex-col gap-3 text-sm">
            <p className="text-ink-600">
              {detail.products.length} product{detail.products.length === 1 ? "" : "s"} using this coverstock
              {detail.material && (
                <>
                  {" "}
                  &middot; material: <span className="font-medium text-ink-800">{detail.material}</span>
                </>
              )}
              {detail.type && (
                <>
                  {" "}
                  &middot; type: <span className="font-medium text-ink-800">{detail.type}</span>
                </>
              )}
            </p>
            {detail.products.length === 0 ? (
              <p className="text-xs text-ink-400">
                None -- likely an orphaned row (every referencing product was reassigned or rescraped under a
                different coverstock name), safe to investigate for cleanup.
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
                    <tr key={p.id} className="border-b border-ink-200 last:border-0">
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
