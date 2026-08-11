import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { getCompareProducts, listProducts } from "../api/client";
import type { ProductCard, ProductDetail } from "../api/types";
import { useCompare, MAX_COMPARE_IDS } from "../context/CompareContext";

// Al's ask: "an intuitive way to populate a ball comparison page". Two
// ways in: the Browse/Detail pages' "Add to compare" buttons (shared
// localStorage state via useCompareList), or typing directly into this
// page's own search box. Either way funnels into the same ids list,
// which this page also mirrors into ?ids= in the URL -- so a compare
// set built by clicking around is still a real, shareable link.
export default function ComparePage() {
  const { ids, remove, add, clear, isFull, setAll } = useCompare();
  const [searchParams, setSearchParams] = useSearchParams();
  const [products, setProducts] = useState<ProductDetail[]>([]);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ProductCard[]>([]);

  // URL <-> localStorage sync, one direction at a time to avoid a loop:
  // on first mount, adopt ?ids= from the URL if present (a shared link);
  // after that, keep the URL in sync with whatever the shared compare
  // list becomes.
  const [adoptedFromUrl, setAdoptedFromUrl] = useState(false);
  useEffect(() => {
    if (adoptedFromUrl) return;
    const fromUrl = searchParams.get("ids");
    if (fromUrl) {
      setAll(fromUrl.split(",").filter(Boolean));
    }
    setAdoptedFromUrl(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!adoptedFromUrl) return;
    const next = new URLSearchParams(searchParams);
    if (ids.length > 0) next.set("ids", ids.join(","));
    else next.delete("ids");
    setSearchParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ids, adoptedFromUrl]);

  useEffect(() => {
    if (ids.length === 0) {
      setProducts([]);
      return;
    }
    setLoading(true);
    getCompareProducts(ids)
      .then(setProducts)
      .finally(() => setLoading(false));
  }, [ids]);

  useEffect(() => {
    if (!query.trim()) {
      setResults([]);
      return;
    }
    const handle = setTimeout(() => {
      listProducts({ search: query.trim(), limit: 8 }).then(setResults);
    }, 250);
    return () => clearTimeout(handle);
  }, [query]);

  // Every spec field that shows up on ANY product in the set -- rows a
  // particular ball doesn't have just render blank in that column,
  // rather than only showing fields common to all of them.
  const specRows: { label: string; get: (p: ProductDetail) => string }[] = [
    { label: "Brand", get: (p) => p.brand_name },
    { label: "Status", get: (p) => p.status },
    { label: "Core", get: (p) => [p.core_name, p.core_type].filter(Boolean).join(" / ") },
    { label: "Coverstock", get: (p) => p.coverstock_full_name || p.coverstock_name || "" },
    { label: "Coverstock type", get: (p) => p.coverstock_type || "" },
    { label: "Coverstock material", get: (p) => p.coverstock_material || "" },
    { label: "Color", get: (p) => p.color || "" },
    { label: "Weights available", get: (p) => p.weights_available || "" },
    {
      label: "RG / DIFF (15lb)",
      get: (p) => {
        const sku = p.skus.find((s) => s.weight_lbs === 15) || p.skus[0];
        return sku ? `${sku.rg ?? "--"} / ${sku.differential ?? "--"}` : "";
      },
    },
    { label: "Release date", get: (p) => p.release_date || "" },
  ];

  return (
    <div className="page compare-page">
      <h1>Compare balls</h1>
      <p className="hint">
        Add up to {MAX_COMPARE_IDS} balls to compare side by side. Selections are shared with the Browse page and
        saved in this link.
      </p>

      <div className="compare-search">
        <input
          type="search"
          placeholder="Search for a ball to add..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          disabled={isFull}
        />
        {results.length > 0 && (
          <ul className="compare-search-results">
            {results.map((r) => (
              <li key={r.id}>
                <span>
                  {r.brand_name} {r.name}
                </span>
                <button
                  type="button"
                  className="btn"
                  disabled={ids.includes(r.id) || isFull}
                  onClick={() => {
                    add(r.id);
                    setQuery("");
                    setResults([]);
                  }}
                >
                  {ids.includes(r.id) ? "Added" : "Add"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {ids.length === 0 ? (
        <p className="empty-state">
          Nothing to compare yet. <Link to="/">Browse balls</Link> and add a few, or search above.
        </p>
      ) : (
        <>
          <button type="button" className="btn" onClick={clear}>
            Clear all
          </button>

          {loading ? (
            <p>Loading...</p>
          ) : (
            <div className="compare-table-wrap">
              <table className="compare-table">
                <thead>
                  <tr>
                    <th />
                    {products.map((p) => (
                      <th key={p.id}>
                        <Link to={`/balls/${p.id}`}>
                          {p.primary_image_url && <img src={p.primary_image_url} alt={p.name} />}
                          <div>{p.name}</div>
                        </Link>
                        <button type="button" className="btn btn-remove" onClick={() => remove(p.id)}>
                          Remove
                        </button>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {specRows.map((row) => (
                    <tr key={row.label}>
                      <th scope="row">{row.label}</th>
                      {products.map((p) => (
                        <td key={p.id}>{row.get(p) || "--"}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
