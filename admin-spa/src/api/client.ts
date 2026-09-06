import { getValidIdToken } from "../auth/cognito";
import type {
  ApproveReviewResult,
  DashboardSummary,
  ListProductsParams,
  ListReviewQueueParams,
  ListVideoCandidatesParams,
  Product,
  ReassignVideoResult,
  RejectReviewResult,
  RescrapeResult,
  ReviewQueueListResult,
  VideoCandidateListResult,
} from "./types";

// Unlike consumer-site's PublicApiFunction client, every request here
// needs an Authorization header -- AdminHttpApi is gated by
// admin_api_authorizer's dual-mode check (Cognito ID token here; the 17
// existing automation scripts still use the shared-secret path, see
// src/admin_api_authorizer/app.py). Stripped of a trailing slash for
// the same double-slash reason as consumer-site/src/api/client.ts.
const API_BASE = (import.meta.env.VITE_ADMIN_API_URL ?? "").replace(/\/+$/, "");

if (!API_BASE) {
  // eslint-disable-next-line no-console
  console.error(
    "VITE_ADMIN_API_URL is not set -- copy .env.example to .env.local and fill in the deployed admin API URL.",
  );
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function authHeaders(): Promise<HeadersInit> {
  const token = await getValidIdToken();
  if (!token) {
    // 401 here (rather than letting fetch run without a header) means
    // callers can rely on ApiError.status === 401 to mean "not signed
    // in" uniformly, whether the failure was local (no session) or
    // server-side (session rejected).
    throw new ApiError(401, "Not signed in");
  }
  return { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
}

async function handleResponse<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
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

async function apiGet<T>(path: string, params: Record<string, string | number | boolean | undefined> = {}): Promise<T> {
  const url = new URL(`${API_BASE}${path}`);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  const headers = await authHeaders();
  const resp = await fetch(url.toString(), { headers });
  return handleResponse<T>(resp);
}

async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const headers = await authHeaders();
  const resp = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  return handleResponse<T>(resp);
}

export function getDashboardSummary(): Promise<DashboardSummary> {
  return apiGet<DashboardSummary>("/admin/dashboard");
}

export function listProducts(params: ListProductsParams = {}): Promise<Product[]> {
  return apiGet<{ items: Product[] }>("/products", { ...params }).then((r) => r.items);
}

export function rescrapeProduct(id: string): Promise<RescrapeResult> {
  return apiPost<RescrapeResult>(`/products/${encodeURIComponent(id)}/rescrape`);
}

export function listReviewQueue(params: ListReviewQueueParams = {}): Promise<ReviewQueueListResult> {
  return apiGet<ReviewQueueListResult>("/review-queue", { ...params });
}

// resolved_by is intentionally omitted -- admin_api's ApproveRequest/
// RejectRequest both make it Optional and fall back to the
// authenticated caller's identity (see get_caller in admin_api/app.py)
// when the client doesn't supply one. There's no field for editing
// proposed_value before approving -- the backend always applies the
// row's stored value as-is (see service.py's build_update_plan).
export function approveReviewItem(id: string): Promise<ApproveReviewResult> {
  return apiPost<ApproveReviewResult>(`/review-queue/${encodeURIComponent(id)}/approve`, {});
}

export function rejectReviewItem(id: string, reason?: string): Promise<RejectReviewResult> {
  return apiPost<RejectReviewResult>(`/review-queue/${encodeURIComponent(id)}/reject`, { reason });
}

export function listVideoCandidates(params: ListVideoCandidatesParams = {}): Promise<VideoCandidateListResult> {
  return apiGet<VideoCandidateListResult>("/video-candidates", { ...params });
}

export function approveVideoCandidate(id: string): Promise<{ video_id: string; status: "approved" }> {
  return apiPost(`/video-candidates/${encodeURIComponent(id)}/approve`, {});
}

export function rejectVideoCandidate(id: string, reason?: string): Promise<{ video_id: string; status: "rejected" }> {
  return apiPost(`/video-candidates/${encodeURIComponent(id)}/reject`, { reason });
}

// No resolved_by/reason param at all -- restore just clears back to
// pending (see POST /video-candidates/{id}/restore in admin_api/app.py).
export function restoreVideoCandidate(id: string): Promise<{ video_id: string; status: "pending" }> {
  return apiPost(`/video-candidates/${encodeURIComponent(id)}/restore`);
}

// Works from any status -- tombstones the origin row as rejected and
// copies/merges onto the target product (see reassign_video_candidate
// in admin_api/service.py).
export function reassignVideoCandidate(id: string, targetProductId: string): Promise<ReassignVideoResult> {
  return apiPost<ReassignVideoResult>(`/video-candidates/${encodeURIComponent(id)}/reassign`, {
    product_id: targetProductId,
  });
}
