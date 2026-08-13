import type {
  Brand,
  PlotterPoint,
  ProductCard,
  ProductDetail,
  ProductStatus,
  SimilarProduct,
} from "./types";

// PublicApiFunction is unauthenticated by design (see public_api/
// service.py's module docstring in the main repo) -- no bearer token,
// no credentials, just a plain fetch. Base URL comes from the build-time
// env var (see .env.example / DEPLOY_RUNBOOK.md's consumer-site section)
// -- template.yaml's PublicApiUrl stack output is what this should point
// at in production.
// Stripped of any trailing slash -- template.yaml's PublicApiUrl output
// itself ends in "/" (see !Sub "https://${PublicHttpApi}.../"), and
// leaving it in would build a double-slash path (".../\/brands") when
// concatenated with a leading-slash path below. Some API Gateway HTTP
// API route configs don't normalize that away cleanly.
const API_BASE = (import.meta.env.VITE_PUBLIC_API_URL ?? "").replace(/\/+$/, "");

if (!API_BASE) {
  // Fails loud in dev rather than silently issuing relative-path
  // requests that 404 in a confusing way -- see README.md for setup.
  // eslint-disable-next-line no-console
  console.error(
    "VITE_PUBLIC_API_URL is not set -- copy .env.example to .env.local and fill in the deployed PublicApiUrl.",
  );
}

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function apiGet<T>(path: string, params: Record<string, string | number | undefined> = {}): Promise<T> {
  const url = new URL(`${API_BASE}${path}`);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  const resp = await fetch(url.toString());
  if (!resp.ok) {
    // 404 is a real, expected outcome for a bad/unpublished product id
    // (see public_api.get_product's identical-404 guarantee) -- callers
    // check err.status rather than this throwing being treated as
    // always-unexpected.
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail ?? detail;
    } catch {
      // body wasn't JSON -- keep statusText
    }
    throw new ApiError(resp.status, detail);
  }
  return resp.json() as Promise<T>;
}

export { ApiError };

export function getBrands(): Promise<Brand[]> {
  return apiGet<{ items: Brand[] }>("/brands").then((r) => r.items);
}

export interface ListProductsParams {
  status?: ProductStatus;
  brand_id?: string;
  search?: string;
  limit?: number;
  offset?: number;
}

export function listProducts(params: ListProductsParams = {}): Promise<ProductCard[]> {
  return apiGet<{ items: ProductCard[] }>("/products", {
    status: params.status ?? "current",
    brand_id: params.brand_id,
    search: params.search,
    limit: params.limit,
    offset: params.offset,
  }).then((r) => r.items);
}

export function getProduct(id: string): Promise<ProductDetail> {
  return apiGet<ProductDetail>(`/products/${encodeURIComponent(id)}`);
}

export function getSimilarProducts(id: string, limit = 5): Promise<SimilarProduct[]> {
  return apiGet<{ items: SimilarProduct[] }>(`/products/${encodeURIComponent(id)}/similar`, { limit }).then(
    (r) => r.items,
  );
}

// Capped at 6 server-side (public_api.MAX_COMPARE_IDS) -- the comparison
// page's own useCompareList hook enforces the same cap client-side so a
// visitor gets immediate feedback rather than a silently-truncated list.
export function getCompareProducts(ids: string[]): Promise<ProductDetail[]> {
  if (ids.length === 0) return Promise.resolve([]);
  return apiGet<{ items: ProductDetail[] }>("/products/compare", { ids: ids.join(",") }).then((r) => r.items);
}

// ids (optional): when given, overrides status entirely -- backs the
// plotter page's Compare tab, which wants positions for exactly the
// visitor's current compare-list ids (which may mix current/retired),
// not the whole status-filtered catalog. Mirrors getCompareProducts'
// comma-joined ids shape above; server-side cap/order/drop-missing
// behavior lives in public_api.list_plotter_positions, not duplicated
// here.
export function getPlotterPositions(status: ProductStatus = "current", ids?: string[]): Promise<PlotterPoint[]> {
  const params: Record<string, string | number | undefined> =
    ids && ids.length > 0 ? { ids: ids.join(",") } : { status };
  return apiGet<{ items: PlotterPoint[] }>("/products/plotter", params).then((r) => r.items);
}
