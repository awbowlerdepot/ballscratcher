import { useCallback, useEffect, useState } from "react";
import {
  generateAnnouncementImages,
  generateAnnouncementPosts,
  getProductAnnouncement,
  updateAnnouncementPosts,
} from "../api/client";
import type { ArticleSocialPosts, ProductAnnouncement } from "../api/types";
import Badge from "./Badge";
import Button from "./Button";
import { useToast } from "./Toast";

// Announcement posts + images for a new ball (migration 048, runbook 6dd).
// Al: "can we add the ability to generate social posts and images from the
// manufacture description if it exists so we can get social out as soon as
// they are announced before there are videos to generate an article".
// Posts come only from the manufacturer's description + specs; they link
// the ball's bowlerdepot.com listing when we have one, otherwise they say
// "coming soon" with no link. Images: a square themed feed image and a
// square clean product shot, made in the background (~1-2 min) -- this
// polls while they're generating.

const FIELDS: { key: keyof ArticleSocialPosts; label: string; hint: string; rows: number }[] = [
  { key: "facebook", label: "Facebook", hint: "Links the BowlerDepot listing when there is one.", rows: 4 },
  { key: "instagram", label: "Instagram caption", hint: "Pair with the square feed image.", rows: 7 },
  { key: "x", label: "X (Twitter)", hint: "Links count as 23 characters on X.", rows: 3 },
  { key: "tiktok_hook", label: "TikTok / Reels hook", hint: "Say or show this in the first two seconds.", rows: 2 },
  { key: "tiktok_caption", label: "TikTok / Reels caption", hint: "", rows: 3 },
];

const EMPTY: ArticleSocialPosts = { facebook: "", instagram: "", x: "", tiktok_hook: "", tiktok_caption: "" };

function xLength(text: string): number {
  return text.replace(/https?:\/\/\S+/g, "x".repeat(23)).length;
}

export default function AnnouncementSection({ productId }: { productId: string }) {
  const { show } = useToast();
  const [data, setData] = useState<ProductAnnouncement | null>(null);
  const [draft, setDraft] = useState<ArticleSocialPosts>(EMPTY);
  const [writing, setWriting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [queueing, setQueueing] = useState(false);

  const load = useCallback(async () => {
    try {
      const d = await getProductAnnouncement(productId);
      setData(d);
      return d;
    } catch (err) {
      show(err instanceof Error ? err.message : "Couldn't load the announcement.", "danger");
      return null;
    }
  }, [productId, show]);

  useEffect(() => {
    setData(null);
    load().then((d) => setDraft(d?.social_posts ?? EMPTY));
  }, [load]);

  const generatingImages = data?.image_status === "generating";
  useEffect(() => {
    if (!generatingImages) return;
    const t = setInterval(() => {
      load().then((d) => {
        if (d && d.image_status === "ready") show("Announcement images are ready.", "ok");
        if (d && d.image_status === "failed") show(d.image_error ?? "Image generation failed.", "danger");
      });
    }, 5000);
    return () => clearInterval(t);
  }, [generatingImages, load, show]);

  if (!data) return null;
  const saved = data.social_posts ?? null;
  const dirty = saved !== null && FIELDS.some((f) => draft[f.key] !== saved[f.key]);

  async function writePosts() {
    if (saved && !window.confirm("Replace the current announcement posts? Any edits will be lost.")) return;
    setWriting(true);
    try {
      const d = await generateAnnouncementPosts(productId);
      setData(d);
      setDraft(d.social_posts ?? EMPTY);
      show("Announcement posts written.", "ok");
    } catch (err) {
      show(err instanceof Error ? err.message : "Couldn't write posts.", "danger");
    } finally {
      setWriting(false);
    }
  }

  async function savePosts() {
    setSaving(true);
    try {
      const d = await updateAnnouncementPosts(productId, draft);
      setData(d);
      setDraft(d.social_posts ?? EMPTY);
      show("Saved.", "ok");
    } catch (err) {
      show(err instanceof Error ? err.message : "Save failed.", "danger");
    } finally {
      setSaving(false);
    }
  }

  async function makeImages() {
    if ((data?.feed_image_url || data?.product_image_url) && !window.confirm("Make new announcement images? The current ones will be replaced."))
      return;
    setQueueing(true);
    try {
      const r = await generateAnnouncementImages(productId);
      if (!r.queued) throw new Error(r.reason ?? "Not queued");
      show("Making the images -- about 1–2 minutes.", "ok");
      await load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Couldn't start the images.", "danger");
    } finally {
      setQueueing(false);
    }
  }

  async function copy(label: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      show(`Copied ${label}.`, "ok");
    } catch {
      show("Couldn't copy -- select the text and copy it manually.", "danger");
    }
  }

  return (
    <div className="flex flex-col gap-4 border-t border-ink-200 pt-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="font-semibold text-ink-800">New-release announcement</p>
          <p className="text-xs text-ink-500">
            From the manufacturer&rsquo;s description, for before there are videos or an article.
            {data.bowlerdepot_url ? " Links the BowlerDepot listing." : " Not listed on BowlerDepot yet, so the posts say “coming soon” with no link."}
          </p>
        </div>
        {data.article_status ? <Badge tone="muted">Article: {data.article_status}</Badge> : null}
      </div>

      {!data.has_description ? (
        <p className="rounded-md bg-warn-light px-3 py-2 text-xs text-warn">
          The manufacturer hasn&rsquo;t published a description for this ball yet. It fills in automatically on the
          daily scrape once they do.
        </p>
      ) : (
        <>
          {/* Images */}
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant={data.feed_image_url || data.product_image_url ? "secondary" : "primary"}
                disabled={queueing || generatingImages}
                onClick={makeImages}
              >
                {generatingImages ? "Making images…" : data.feed_image_url || data.product_image_url ? "Remake images" : "Make images"}
              </Button>
              <span className="text-xs text-ink-500">
                Square feed image + clean product shot (~$0.30). Each is checked against the official photo and re-made
                if the logo changed.
              </span>
              {data.image_error && data.image_status !== "generating" ? (
                // Failed, or ready with one image rejected for a changed logo (runbook 6de).
                <span className={`text-xs ${data.image_status === "failed" ? "text-danger" : "text-warn"}`}>
                  {data.image_error}
                </span>
              ) : null}
            </div>
            {data.feed_image_url || data.product_image_url ? (
              <div className="flex flex-wrap gap-3">
                {[
                  { url: data.feed_image_url, label: "Feed image (1:1)" },
                  { url: data.product_image_url, label: "Product shot (1:1)" },
                ].map((img) =>
                  img.url ? (
                    <a key={img.label} href={img.url} target="_blank" rel="noreferrer" className="flex w-48 flex-col gap-1 text-xs">
                      <img src={img.url} alt={img.label} className="aspect-square w-full rounded-md border border-ink-200 object-cover" />
                      <span className="font-medium text-ink-700">{img.label} -- open to download</span>
                    </a>
                  ) : null,
                )}
              </div>
            ) : null}
          </div>

          {/* Posts */}
          <div className="flex flex-wrap items-center gap-2">
            {saved && (
              <Button size="sm" variant="primary" onClick={savePosts} disabled={!dirty || saving}>
                {saving ? "Saving…" : dirty ? "Save edits" : "Saved"}
              </Button>
            )}
            <Button size="sm" variant={saved ? "secondary" : "primary"} onClick={writePosts} disabled={writing}>
              {writing ? "Writing posts…" : saved ? "Rewrite posts" : "Write announcement posts"}
            </Button>
            {data.social_posts_generated_at ? (
              <span className="text-xs text-ink-500">Written {new Date(data.social_posts_generated_at).toLocaleString()}</span>
            ) : null}
          </div>

          {saved &&
            FIELDS.map((f) => {
              const value = draft[f.key];
              const xCount = f.key === "x" ? xLength(value) : null;
              return (
                <div key={f.key}>
                  <div className="mb-1 flex items-center justify-between gap-2">
                    <label className="text-xs font-medium text-ink-600">{f.label}</label>
                    <div className="flex items-center gap-2">
                      {xCount !== null && (
                        <span className={`text-xs ${xCount > 280 ? "font-semibold text-danger" : "text-ink-500"}`}>
                          {xCount}/280
                        </span>
                      )}
                      <Button size="sm" variant="ghost" onClick={() => copy(f.label, value)} disabled={!value}>
                        Copy
                      </Button>
                    </div>
                  </div>
                  <textarea
                    value={value}
                    onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })}
                    rows={f.rows}
                    className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm"
                  />
                  {f.hint && <p className="mt-0.5 text-xs text-ink-400">{f.hint}</p>}
                </div>
              );
            })}
        </>
      )}
    </div>
  );
}
