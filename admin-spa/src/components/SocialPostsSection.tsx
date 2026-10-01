import { useEffect, useState } from "react";
import { generateArticleSocialPosts, updateArticleSocialPosts } from "../api/client";
import type { Article, ArticleSocialPosts } from "../api/types";
import Button from "./Button";
import { useToast } from "./Toast";

// Social media copy for one article (migration 042) -- Al: "in the admin
// ui can we add a social media post section that has copy to put for text
// on social media posts." Generated on demand, every field editable, Save
// persists edits, Copy puts one field on the clipboard.

const FIELDS: { key: keyof ArticleSocialPosts; label: string; hint: string; rows: number }[] = [
  { key: "facebook", label: "Facebook", hint: "Includes the article link.", rows: 4 },
  {
    key: "instagram",
    label: "Instagram caption",
    hint: "Instagram captions can't have clickable links -- put the article URL in your bio.",
    rows: 7,
  },
  { key: "x", label: "X (Twitter)", hint: "Links count as 23 characters on X.", rows: 3 },
  { key: "tiktok_hook", label: "TikTok / Reels hook", hint: "Say or show this in the first two seconds.", rows: 2 },
  { key: "tiktok_caption", label: "TikTok / Reels caption", hint: "", rows: 3 },
];

const EMPTY: ArticleSocialPosts = { facebook: "", instagram: "", x: "", tiktok_hook: "", tiktok_caption: "" };

// Mirrors admin_api's x_post_length: X counts every URL as 23 characters.
function xLength(text: string): number {
  return text.replace(/https?:\/\/\S+/g, "x".repeat(23)).length;
}

export default function SocialPostsSection({ article }: { article: Article }) {
  const { show } = useToast();
  const [saved, setSaved] = useState<ArticleSocialPosts | null>(article.social_posts);
  const [draft, setDraft] = useState<ArticleSocialPosts>(article.social_posts ?? EMPTY);
  const [generatedAt, setGeneratedAt] = useState(article.social_posts_generated_at);
  const [generating, setGenerating] = useState(false);
  const [saving, setSaving] = useState(false);

  // Reset when the preview switches to a different article, or its saved
  // posts really change. Keyed on the posts' CONTENT, not the object: the
  // preview refetches the article after other actions (e.g. picking an
  // image candidate), and a fresh-but-identical object mustn't wipe
  // unsaved edits here.
  const savedKey = JSON.stringify(article.social_posts);
  useEffect(() => {
    setSaved(article.social_posts);
    setDraft(article.social_posts ?? EMPTY);
    setGeneratedAt(article.social_posts_generated_at);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [article.id, savedKey, article.social_posts_generated_at]);

  const dirty = saved !== null && FIELDS.some((f) => draft[f.key] !== saved[f.key]);
  const hasLinkPlaceholder = FIELDS.some((f) => draft[f.key].includes("{LINK}"));

  async function generate() {
    if (saved && !window.confirm("Replace the current posts with new ones? Any edits will be lost.")) return;
    setGenerating(true);
    try {
      const result = await generateArticleSocialPosts(article.id);
      setSaved(result.social_posts);
      setDraft(result.social_posts);
      setGeneratedAt(new Date().toISOString());
      show("Social posts generated.", "ok");
    } catch (err) {
      show(err instanceof Error ? err.message : "Couldn't generate posts.", "danger");
    } finally {
      setGenerating(false);
    }
  }

  async function save() {
    setSaving(true);
    try {
      const result = await updateArticleSocialPosts(article.id, draft);
      setSaved(result.social_posts);
      setDraft(result.social_posts);
      show("Saved.", "ok");
    } catch (err) {
      show(err instanceof Error ? err.message : "Save failed.", "danger");
    } finally {
      setSaving(false);
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
    <div className="flex flex-col gap-3 border-t border-ink-200 pt-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="font-semibold text-ink-800">Social media posts</p>
          {generatedAt && (
            <p className="text-xs text-ink-500">Generated {new Date(generatedAt).toLocaleString()}</p>
          )}
        </div>
        <div className="flex flex-wrap gap-1.5">
          {saved && (
            <Button size="sm" variant="primary" onClick={save} disabled={!dirty || saving}>
              {saving ? "Saving…" : dirty ? "Save edits" : "Saved"}
            </Button>
          )}
          <Button size="sm" variant={saved ? "secondary" : "primary"} onClick={generate} disabled={generating}>
            {generating ? "Writing posts…" : saved ? "Regenerate" : "Generate social posts"}
          </Button>
        </div>
      </div>

      {!saved && !generating && (
        <p className="text-xs text-ink-500">
          Writes ready-to-paste posts for Facebook, Instagram, X, and TikTok/Reels from this article. Takes a few
          seconds.
        </p>
      )}

      {saved && hasLinkPlaceholder && (
        <p className="rounded-md bg-warn-light px-3 py-2 text-xs text-warn">
          This article isn&rsquo;t published yet, so the posts say {"{LINK}"} where the URL goes. Approve the article
          and regenerate, or paste the link in yourself.
        </p>
      )}

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
    </div>
  );
}
