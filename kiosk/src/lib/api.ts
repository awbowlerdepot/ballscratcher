// Kiosk data layer (runbook 6eh). Everything comes from the public BowlerIQ
// API (api.bowleriq.io, CORS "*", read-only) -- the same catalog, plotter,
// Learn articles and review videos the Learn site uses. Types are copied
// from bowlerdepot-learn/src/api/types.ts (each frontend owns its copy,
// the same convention the two APIs follow), trimmed to what the kiosk uses.
// No prices anywhere (Al: "No prices").

const API = (import.meta.env.VITE_PUBLIC_API_URL ?? "https://api.bowleriq.io").replace(/\/+$/, "");
const IMAGE_RESIZER_ORIGIN = "https://img.bowleriq.io";
export const LEARN_SITE = "https://learn.bowlerdepot.com";

// --- types -----------------------------------------------------------------

export interface PlotterNeighbor {
  id: string;
  distance: number;
}

export interface PlotterNeighbors {
  twins?: PlotterNeighbor[];
  closest?: PlotterNeighbor[];
  more_oil: PlotterNeighbor[];
  less_oil: PlotterNeighbor[];
  more_angular: PlotterNeighbor[];
  smoother: PlotterNeighbor[];
}

export interface LearnPlotterPoint {
  id: string;
  name: string;
  brand_name: string;
  primary_image_url?: string | null;
  oil: number;
  motion: number;
  oil_motion_source: "chart" | "estimated" | "manual";
  ecommerce_url: string | null;
  article_slug: string | null;
  coverstock_name: string | null;
  coverstock_material: string | null;
  finish_category: string | null;
  recommendable: boolean;
  status?: "current" | "retired";
  neighbors: PlotterNeighbors;
}

export interface PlotterLookupItem {
  id: string;
  name: string;
  brand_name: string;
  status: "current" | "retired";
}

export interface ProductCard {
  id: string;
  name: string;
  brand_name: string;
  color: string | null;
  status: string;
  primary_image_url: string | null;
  release_date: string | null;
  popularity_score: number | null;
  coverstock_name: string | null;
  coverstock_type: string | null;
  coverstock_material: string | null;
  core_name: string | null;
  core_type: string | null;
  video_reviews_summary_video_count: number | null;
}

export interface ProductVideo {
  youtube_video_id: string;
  title: string;
  channel_title: string | null;
  published_at: string | null;
  thumbnail_url: string | null;
  summary: string | null;
}

export interface Sku {
  weight_lbs: number;
  rg: number | null;
  differential: number | null;
  mass_bias: number | null;
}

export interface ProductDetail {
  id: string;
  name: string;
  brand_name: string;
  color: string | null;
  status: string;
  url: string | null;
  description: string | null;
  release_date: string | null;
  primary_image_url: string | null;
  images: { id: string; stored_url: string | null; image_type: string | null; is_thumbnail: boolean; display_order: number }[];
  coverstock_name: string | null;
  coverstock_full_name: string | null;
  coverstock_type: string | null;
  coverstock_material: string | null;
  core_name: string | null;
  core_type: string | null;
  factory_finish: string | null;
  has_particle: boolean | null;
  skus: Sku[];
  video_reviews_summary: string | null;
  video_reviews_summary_video_count: number | null;
  videos: ProductVideo[] | null;
}

export interface ArticleCard {
  article_id: string;
  slug: string | null;
  product_id: string | null;
  title: string;
  hook: string | null;
  article_kind?: string | null; // "video" for a video article (migration 040)
  category_name?: string | null;
  product_name?: string | null;
  brand_name?: string | null;
  primary_image_url?: string | null;
  product_shot_image_url?: string | null;
  video_thumbnail_url?: string | null;
  reviewed_at?: string | null;
}

export interface ArticleSection {
  heading: string;
  body: string;
}

export interface ArticleDetail {
  id: string;
  slug: string | null;
  title: string;
  hook: string | null;
  kind?: string | null;
  performance_summary: string | null;
  pros: string[] | null;
  cons: string[] | null;
  who_should_buy: string[] | null;
  who_should_skip: string[] | null;
  buying_tips: string | null;
  verdict: string | null;
  faq: { question: string; answer: string }[] | null;
  sections?: ArticleSection[] | null;
  key_takeaways?: string[] | null;
  action_shot_image_url: string | null;
  product_shot_image_url: string | null;
  author_name?: string | null;
  product_id?: string | null;
}

export interface SignageSpec {
  clip_url: string;
  ball_x: number;
  ball_y: number;
  ball_r: number;
  tagline: string | null;
}

// --- fetch -------------------------------------------------------------------

async function get<T>(path: string, params: Record<string, string | number | undefined> = {}): Promise<T> {
  const url = new URL(`${API}${path}`);
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") url.searchParams.set(k, String(v));
  const resp = await fetch(url.toString());
  if (!resp.ok) throw new Error(`${resp.status} ${path}`);
  return (await resp.json()) as T;
}

// Small in-memory cache: the kiosk re-visits the same few endpoints all day,
// and the catalog changes slowly. Cleared on a full reload (the idle reset
// doesn't reload the page, so a session's data stays warm for the next one).
const cache = new Map<string, { at: number; value: Promise<unknown> }>();
const TTL_MS = 30 * 60 * 1000;
function cached<T>(key: string, load: () => Promise<T>): Promise<T> {
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < TTL_MS) return hit.value as Promise<T>;
  const value = load().catch((err) => {
    cache.delete(key);
    throw err;
  });
  cache.set(key, { at: Date.now(), value });
  return value;
}

export const getPlotter = () =>
  cached("plotter", () => get<{ items: LearnPlotterPoint[] }>("/learn/plotter").then((r) => r.items));

export const getPlotterBall = (id: string) => cached(`pball:${id}`, () => get<LearnPlotterPoint>(`/learn/plotter/ball/${id}`));

export const lookupBalls = (q: string) =>
  get<{ items: PlotterLookupItem[] }>("/learn/plotter/lookup", { q }).then((r) => r.items);

// Every current ball as a card (two pages of 100), for release dates,
// popularity and cover type, which the plotter list doesn't carry.
export const getCurrentCards = () =>
  cached("cards", async () => {
    const out: ProductCard[] = [];
    for (let offset = 0; offset < 1000; offset += 100) {
      const page = await get<{ items: ProductCard[] }>("/products", { status: "current", limit: 100, offset, sort: "newest" });
      out.push(...page.items);
      if (page.items.length < 100) break;
    }
    return out;
  });

export const getProduct = (id: string) => cached(`product:${id}`, () => get<ProductDetail>(`/products/${id}`));

export const getProductArticle = (id: string) =>
  cached(`particle:${id}`, () => get<{ article: ArticleDetail | null }>(`/products/${id}/article`).then((r) => r.article));

export const getArticles = (params: { search?: string; limit?: number; offset?: number } = {}) =>
  get<{ items: ArticleCard[] }>("/articles", { limit: params.limit ?? 48, offset: params.offset, search: params.search }).then(
    (r) => r.items,
  );

export const getArticle = (slug: string) =>
  cached(`article:${slug}`, () =>
    get<{ article: ArticleDetail; product_id: string | null }>(`/articles/${slug}`).then((r) => ({ ...r.article, product_id: r.product_id })),
  );

export const getSignage = (slug: string) => get<SignageSpec>(`/learn/signage/${slug}`);

// --- helpers -----------------------------------------------------------------

export interface ResizeOptions {
  w?: number;
  h?: number;
  fit?: "cover" | "contain" | "inside";
  fmt?: "webp" | "avif" | "jpeg" | "png";
  q?: number;
}

// Same rewrite as the Learn site: ImageBucket S3 URL -> img.bowleriq.io.
export function resizedImageUrl(rawUrl: string, options: ResizeOptions): string {
  let key: string;
  try {
    key = new URL(rawUrl).pathname.replace(/^\/+/, "");
  } catch {
    return rawUrl;
  }
  if (!key.startsWith("product-images/") && !key.startsWith("article-images/")) return rawUrl;
  const url = new URL(`${IMAGE_RESIZER_ORIGIN}/${key}`);
  if (options.w) url.searchParams.set("w", String(options.w));
  if (options.h) url.searchParams.set("h", String(options.h));
  if (options.fit) url.searchParams.set("fit", options.fit);
  if (options.fmt) url.searchParams.set("fmt", options.fmt);
  if (options.q) url.searchParams.set("q", String(options.q));
  return url.toString();
}

export function img(url: string | null | undefined, w: number, h?: number, fit: ResizeOptions["fit"] = "contain") {
  return url ? resizedImageUrl(url, { w, h, fit }) : null;
}

export function articleHref(item: { slug?: string | null; product_id?: string | null }): string {
  return `/articles/${item.slug || item.product_id}`;
}

export const MATERIAL_LABEL: Record<string, string> = {
  reactive_resin: "Reactive resin",
  urethane: "Urethane",
  polyester_plastic: "Polyester",
};

export function titleCase(s: string | null | undefined) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "";
}

// --- oil patterns (runbook 6eu) -------------------------------------------------

export interface PatternSummary {
  id: string;
  name: string;
  category: string; // house | sport | challenge | recreational | tournament
  series: string | null;
  source: string;
  status: string;
  length_feet: number | null;
  volume_ml: number | null;
  ratio: number | null;
  reverse_brush_drop_feet: number | null;
}

export interface PatternZone {
  from: string;
  to: string;
  against_from: string;
  against_to: string;
  label: string | null;
  ratio: number | null;
}

export interface PatternDetail extends PatternSummary {
  short_name: string | null;
  source_url: string | null;
  oil_per_board_ul: number | null;
  forward_ml: number | null;
  reverse_ml: number | null;
  description: string | null;
  track_zones: PatternZone[];
  machine: Record<string, unknown>;
  board_totals: { forward_ul: number[]; reverse_ul: number[] } | null;
  profile: { foot_step: number; units: string; model: string | null; burn_lanes: number | null; rows: number[][] } | null;
}

export interface SuggestedBall {
  id: string;
  name: string;
  brand_name: string;
  primary_image_url: string | null;
  oil: number;
  motion: number;
  coverstock_name: string | null;
  finish_category: string | null;
  article_slug: string | null;
}

export interface PatternSuggestions {
  pattern_id: string;
  target: { oil: number; motion: number };
  summary: string;
  note: string;
  groups: { key: "urethane" | "best" | "lighter" | "heavier"; title: string; blurb: string; balls: SuggestedBall[] }[];
}

export const getPatterns = () =>
  cached("patterns", () => get<{ items: PatternSummary[] }>("/patterns").then((r) => r.items));

export const getPattern = (id: string) => cached(`pattern:${id}`, () => get<PatternDetail>(`/patterns/${id}`));

export const getPatternSuggestions = (id: string) =>
  cached(`pattern-sugg:${id}`, () => get<PatternSuggestions>(`/patterns/${id}/suggestions`));
