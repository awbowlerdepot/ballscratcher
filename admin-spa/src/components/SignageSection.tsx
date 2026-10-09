import { useCallback, useEffect, useState } from "react";
import {
  animateArticleSignage,
  generateArticleSignage,
  getArticleSignage,
  renderArticleSignage,
  updateArticleSignage,
} from "../api/client";
import type { Article, ArticleSignage, SignageStatus, SignageStillSource } from "../api/types";
import Badge from "./Badge";
import Button from "./Button";
import { useToast } from "./Toast";

// In-store signage for one ball article (migration 045, runbook 6cn) -- Al:
// "can there just be a generate button on the ball on the admin site so we
// can pick and choose". Generate (3 vertical stills + tagline ideas) ->
// pick a still and tagline -> Animate (Veo clip, seamless loop) -> preview
// -> Approve (the live signage URL serves it) -> Render MP4 (for piSignage).
// Generate/Animate/Render run in the background; this polls while one runs.
//
// Runbook 6co: lives in its own Signage tab on the product page now ("I
// would prefer the social posts and this Signage Video stuff all in the
// product page with a tab for each"), and Generate starts from a source
// image Al picks -- "Can we select an existing image action or product shot
// as input to the Signage Video still as part of the workflow?" The stills
// are that image extended to 9:16 (straight 9:16 generation letterboxed).

const RUNNING: SignageStatus[] = ["generating", "animating", "rendering"];
const STATUS_LABEL: Record<SignageStatus, string> = {
  idle: "Not started",
  generating: "Generating stills…",
  stills_ready: "Pick a still",
  animating: "Animating (4–6 min)…",
  rendering: "Rendering MP4 (1–3 min)…",
  ready: "Ready",
  failed: "Failed",
};
const SOURCE_LABEL: Record<SignageStillSource, string> = {
  action_shot: "Action shot",
  product_shot: "Product shot",
  new: "New scene",
};
const PREVIEW_SCALE = 0.25; // 1080x1920 page shown at 270x480

function money(n?: number | null) {
  return n == null ? null : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(n);
}

export default function SignageSection({ article }: { article: Article }) {
  const { show } = useToast();
  const [data, setData] = useState<ArticleSignage | null>(null);
  const [busy, setBusy] = useState(false);
  const [taglineDraft, setTaglineDraft] = useState("");
  const [source, setSource] = useState<SignageStillSource>("action_shot");

  const load = useCallback(async () => {
    try {
      const d = await getArticleSignage(article.id);
      setData(d);
      return d;
    } catch (err) {
      show(err instanceof Error ? err.message : "Couldn't load signage.", "danger");
      return null;
    }
  }, [article.id, show]);

  useEffect(() => {
    setData(null);
    load().then((d) => {
      setTaglineDraft(d?.tagline ?? d?.tagline_options?.[0] ?? "");
      // Default to the action shot (a real scene extends best), else the
      // product shot, else a fresh scene.
      setSource(d?.source_images.action_shot ? "action_shot" : d?.source_images.product_shot ? "product_shot" : "new");
    });
  }, [load]);

  const running = !!data && RUNNING.includes(data.status);
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => {
      load().then((d) => {
        if (d && !RUNNING.includes(d.status) && !d.tagline) setTaglineDraft(d.tagline_options?.[0] ?? "");
      });
    }, 5000);
    return () => clearInterval(t);
  }, [running, load]);

  async function act(fn: () => Promise<unknown>, okMsg: string) {
    setBusy(true);
    try {
      const r = (await fn()) as { queued?: boolean; reason?: string } | ArticleSignage;
      if (r && "queued" in r && r.queued === false) throw new Error(r.reason ?? "Not queued");
      show(okMsg, "ok");
      await load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Something went wrong.", "danger");
    } finally {
      setBusy(false);
    }
  }

  if (!data) return null;
  const hasStills = data.still_candidates.length > 0;
  const previewUrl =
    data.clip_url && data.signage_url
      ? `${data.signage_url}?${new URLSearchParams({
          video: data.clip_url,
          bx: String(data.ball_x ?? 0.5),
          by: String(data.ball_y ?? 0.62),
          br: String(data.ball_r ?? 0.27),
          ...(data.tagline ? { cta: data.tagline } : {}),
        }).toString()}`
      : null;
  const mp4Stale = data.mp4_url && data.mp4_price != null && data.drilled_price != null && data.mp4_price !== data.drilled_price;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="font-semibold text-ink-800">In-store signage</p>
          <p className="text-xs text-ink-500">
            9:16 animated loop for the store screens · drilled price {money(data.drilled_price) ?? "unknown"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {data.approved ? <Badge tone="ok">Approved</Badge> : null}
          <Badge tone={data.status === "failed" ? "danger" : running ? "pending" : "muted"}>{STATUS_LABEL[data.status]}</Badge>
        </div>
      </div>

      {data.status === "failed" && data.error ? (
        <p className="rounded-md bg-danger-light px-3 py-2 text-xs text-danger">{data.error}</p>
      ) : null}

      {/* Step 1: pick a source image, then stills + taglines */}
      <div className="flex flex-col gap-2">
        <p className="text-xs font-medium text-ink-600">Start from</p>
        <div className="flex flex-wrap gap-3">
          {(["action_shot", "product_shot", "new"] as SignageStillSource[]).map((s) => {
            const img = s === "new" ? null : data.source_images[s];
            const available = s === "new" || !!img;
            const picked = s === source;
            return (
              <button
                key={s}
                type="button"
                disabled={!available || busy || running}
                onClick={() => setSource(s)}
                className={`flex w-36 flex-col gap-1 rounded-md border p-1.5 text-left text-xs disabled:opacity-40 ${picked ? "border-primary ring-2 ring-primary" : "border-ink-200"}`}
              >
                {img ? (
                  <img src={img} alt="" className="aspect-video w-full rounded object-cover" />
                ) : (
                  <div className="flex aspect-video w-full items-center justify-center rounded bg-ink-100 px-2 text-center text-[11px] text-ink-500">
                    {s === "new" ? "Fresh themed scene from the article" : "None yet"}
                  </div>
                )}
                <span className="font-medium text-ink-700">{SOURCE_LABEL[s]}</span>
              </button>
            );
          })}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant={hasStills ? "secondary" : "primary"}
            disabled={busy || running}
            onClick={() => {
              if (hasStills && !window.confirm("Generate new stills and taglines? The current candidates will be replaced.")) return;
              act(() => generateArticleSignage(article.id, source), "Generating 3 stills and tagline ideas -- about 1–3 minutes.");
            }}
          >
            {hasStills ? "Regenerate stills" : "Generate signage"}
          </Button>
          <span className="text-xs text-ink-500">
            3 vertical stills extended from the {SOURCE_LABEL[source].toLowerCase()} + tagline ideas (~$0.50
            {source === "new" ? ", a bit more for the new scene" : ""}).
          </span>
        </div>
      </div>

      {hasStills ? (
        <div className="flex flex-wrap gap-3">
          {data.still_candidates.map((c) => {
            const selected = c.key === data.selected_still_key;
            return (
              <div
                key={c.key}
                className={`flex w-32 flex-col gap-1 rounded-md border p-1.5 ${selected ? "border-primary ring-2 ring-primary" : "border-ink-200"}`}
              >
                <img src={c.url} alt="" className="aspect-[9/16] w-full rounded object-cover" />
                {selected ? (
                  <Badge tone="ok">selected</Badge>
                ) : (
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy || running}
                    onClick={() => {
                      if (data.clip_url && !window.confirm("Switching stills discards the current animation. Continue?")) return;
                      act(() => updateArticleSignage(article.id, { selected_still_key: c.key }), "Still selected.");
                    }}
                  >
                    Use this one
                  </Button>
                )}
              </div>
            );
          })}
        </div>
      ) : null}

      {data.tagline_options.length || data.tagline ? (
        <div className="flex flex-col gap-1.5">
          <label className="text-xs font-medium text-ink-600">Tagline (under the price)</label>
          <div className="flex flex-wrap gap-1.5">
            {data.tagline_options.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTaglineDraft(t)}
                className={`rounded-full border px-2.5 py-1 text-xs ${t === taglineDraft ? "border-primary bg-primary-light text-ink-800" : "border-ink-200 text-ink-600"}`}
              >
                {t}
              </button>
            ))}
          </div>
          <div className="flex gap-2">
            <input
              value={taglineDraft}
              onChange={(e) => setTaglineDraft(e.target.value)}
              maxLength={60}
              className="w-full max-w-sm rounded-md border border-ink-300 px-2 py-1.5 text-sm"
            />
            <Button
              size="sm"
              variant="secondary"
              disabled={busy || running || !taglineDraft.trim() || taglineDraft.trim() === data.tagline}
              onClick={() => act(() => updateArticleSignage(article.id, { tagline: taglineDraft }), "Tagline saved.")}
            >
              {data.tagline && taglineDraft.trim() === data.tagline ? "Saved" : "Save tagline"}
            </Button>
          </div>
        </div>
      ) : null}

      {/* Step 2: animate + preview */}
      {data.selected_still_url ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant={data.clip_url ? "secondary" : "primary"}
            disabled={busy || running}
            onClick={() => {
              if (data.clip_url && !window.confirm("Make a new animation? It replaces the current one and needs re-approval.")) return;
              act(() => animateArticleSignage(article.id), "Animating -- about 4–6 minutes.");
            }}
          >
            {data.clip_url ? "Re-animate" : "Animate"}
          </Button>
          <span className="text-xs text-ink-500">15 s Veo video from the selected still, camera locked (~$2).</span>
        </div>
      ) : null}

      {previewUrl ? (
        <div className="flex flex-wrap items-start gap-4">
          <div
            className="overflow-hidden rounded-md border border-ink-200 bg-black"
            style={{ width: 1080 * PREVIEW_SCALE, height: 1920 * PREVIEW_SCALE }}
          >
            <iframe
              title="Signage preview"
              src={previewUrl}
              style={{ width: 1080, height: 1920, border: 0, transform: `scale(${PREVIEW_SCALE})`, transformOrigin: "0 0" }}
            />
          </div>
          <div className="flex max-w-xs flex-col gap-2 text-xs text-ink-600">
            <p>Live preview with the current drilled price and the saved tagline.</p>
            {data.approved ? (
              <>
                <p>
                  Approved{data.approved_by ? ` by ${data.approved_by}` : ""}
                  {data.approved_at ? ` · ${new Date(data.approved_at).toLocaleString()}` : ""}
                </p>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy || running}
                  onClick={() => act(() => updateArticleSignage(article.id, { approved: false }), "Unapproved.")}
                >
                  Unapprove
                </Button>
              </>
            ) : (
              <Button
                size="sm"
                variant="primary"
                disabled={busy || running}
                onClick={() => act(() => updateArticleSignage(article.id, { approved: true }), "Approved -- the signage URL now shows it.")}
              >
                Approve
              </Button>
            )}
            {data.approved && data.signage_url ? (
              <div>
                <p className="font-medium text-ink-700">piSignage web link</p>
                <code className="break-all text-[11px]">{data.signage_url}</code>
              </div>
            ) : null}
            <div className="mt-1 flex flex-col gap-1.5 border-t border-ink-200 pt-2">
              <Button
                size="sm"
                variant="secondary"
                disabled={busy || running}
                onClick={() => act(() => renderArticleSignage(article.id), "Rendering the MP4 -- about 1–3 minutes.")}
              >
                {data.mp4_url ? "Re-render MP4" : "Render MP4"}
              </Button>
              {data.mp4_url ? (
                <a href={data.mp4_url} target="_blank" rel="noreferrer" className="font-semibold">
                  Download MP4 ({money(data.mp4_price) ?? "no price"}
                  {data.mp4_rendered_at ? `, ${new Date(data.mp4_rendered_at).toLocaleDateString()}` : ""})
                </a>
              ) : null}
              {mp4Stale ? (
                <p className="text-warn">The price changed since this MP4 was rendered -- re-render it.</p>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
