import { useEffect, useMemo, useState } from "react";
import {
  createLearnVideo,
  deleteLearnVideo,
  getLearnVideo,
  listCategories,
  listLearnVideos,
  retryLearnVideoTranscript,
  setManualLearnVideoTranscript,
  updateLearnVideo,
} from "../api/client";
import type { Category, LearnVideo, LearnVideoDetail, LearnVideoTranscriptStatus } from "../api/types";
import { LEARN_VIDEO_MAX_TRANSCRIPT_ATTEMPTS } from "../api/types";
import Badge from "../components/Badge";
import Button from "../components/Button";
import type { Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import Modal from "../components/Modal";
import Pagination from "../components/Pagination";
import { useToast } from "../components/Toast";
import { categoryOptionLabel, flattenCategoryTree } from "../lib/categoryTree";

const LIMIT = 50;

// Learn videos (migration 038) -- phase 2 of Bowling Tips. Al: "we can add
// videos from youtube there and then using a similar workflow that we have
// for balls it will generate an article with that video inline." Paste a
// YouTube link, pick a category; admin_api fills in the details from
// YouTube, and the Pi transcript fetcher picks the video up on its next
// daily run. Article generation from these is phase 3.

const STATUS_LABEL: Record<LearnVideoTranscriptStatus, string> = {
  awaiting: "Awaiting transcript",
  retrying: "Will retry",
  ready: "Transcript ready",
  unavailable: "No transcript",
};

const STATUS_TONE: Record<LearnVideoTranscriptStatus, "pending" | "ok" | "danger"> = {
  awaiting: "pending",
  retrying: "pending",
  ready: "ok",
  unavailable: "danger",
};

// What the transcript column says under the badge.
function transcriptDetail(v: LearnVideo): string {
  switch (v.transcript_status) {
    case "ready":
      return `${(v.transcript_chars ?? 0).toLocaleString()} characters${v.transcript_source === "manual" ? ", pasted" : ""}`;
    case "retrying":
      return `YouTube didn't serve it to the Pi (${v.transcript_attempts} of ${LEARN_VIDEO_MAX_TRANSCRIPT_ATTEMPTS} tries). Retrying on the next daily run.`;
    case "unavailable":
      return v.transcript_note === "video_player_error_transcript_unavailable"
        ? "YouTube didn't serve it to the Pi after several tries. Paste the transcript instead."
        : (v.transcript_note ?? "");
    default:
      return "Picked up on the Pi's next daily run";
  }
}

function fmtDuration(seconds: number | null): string {
  if (seconds == null) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function fmtDate(iso: string | null): string {
  return iso ? new Date(iso).toLocaleDateString() : "";
}

export default function LearnVideosPage() {
  const { show } = useToast();
  const [categories, setCategories] = useState<Category[]>([]);
  const [items, setItems] = useState<LearnVideo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);

  const [filterCategory, setFilterCategory] = useState("");
  const [filterStatus, setFilterStatus] = useState<LearnVideoTranscriptStatus | "">("");

  const [newUrl, setNewUrl] = useState("");
  const [newCategory, setNewCategory] = useState("");
  const [adding, setAdding] = useState(false);

  const [transcriptFor, setTranscriptFor] = useState<LearnVideoDetail | null>(null);
  const [transcriptLoadingId, setTranscriptLoadingId] = useState<string | null>(null);
  const [pasteFor, setPasteFor] = useState<LearnVideo | null>(null);
  const [pasteText, setPasteText] = useState("");
  const [pasting, setPasting] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<LearnVideo | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const categoryOptions = useMemo(
    () => flattenCategoryTree(categories).map((n) => ({ id: n.category.id, label: categoryOptionLabel(n) })),
    [categories],
  );

  useEffect(() => {
    listCategories()
      .then(setCategories)
      .catch(() => setCategories([]));
  }, []);

  function load() {
    setLoading(true);
    setError(null);
    listLearnVideos({
      category_id: filterCategory || undefined,
      transcript_status: filterStatus || undefined,
      limit: LIMIT,
      offset,
    })
      .then(setItems)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load videos."))
      .finally(() => setLoading(false));
  }

  useEffect(load, [filterCategory, filterStatus, offset]);

  async function handleAdd() {
    const url = newUrl.trim();
    if (!url) {
      show("Paste a YouTube link first.", "danger");
      return;
    }
    if (!newCategory) {
      show("Pick a category for the video.", "danger");
      return;
    }
    setAdding(true);
    try {
      const video = await createLearnVideo(url, newCategory);
      show(`Added "${video.title ?? video.youtube_video_id}". The Pi will fetch its transcript on its next run.`, "ok");
      setNewUrl("");
      if (offset === 0) load();
      else setOffset(0);
    } catch (err) {
      show(err instanceof Error ? err.message : "Couldn't add that video.", "danger");
    } finally {
      setAdding(false);
    }
  }

  async function moveVideo(video: LearnVideo, categoryId: string) {
    if (categoryId === video.category_id) return;
    setBusyId(video.id);
    try {
      await updateLearnVideo(video.id, categoryId);
      show("Moved.", "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Move failed.", "danger");
    } finally {
      setBusyId(null);
    }
  }

  async function retry(video: LearnVideo) {
    setBusyId(video.id);
    try {
      await retryLearnVideoTranscript(video.id);
      show("Queued. The Pi will try again on its next run.", "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Retry failed.", "danger");
    } finally {
      setBusyId(null);
    }
  }

  async function openTranscript(video: LearnVideo) {
    setTranscriptLoadingId(video.id);
    try {
      setTranscriptFor(await getLearnVideo(video.id));
    } catch (err) {
      show(err instanceof Error ? err.message : "Couldn't load the transcript.", "danger");
    } finally {
      setTranscriptLoadingId(null);
    }
  }

  function openPaste(video: LearnVideo) {
    setPasteText("");
    setPasteFor(video);
  }

  async function savePaste() {
    if (!pasteFor) return;
    setPasting(true);
    try {
      const result = await setManualLearnVideoTranscript(pasteFor.id, pasteText);
      show(`Saved transcript (${result.transcript_chars.toLocaleString()} characters).`, "ok");
      setPasteFor(null);
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Couldn't save the transcript.", "danger");
    } finally {
      setPasting(false);
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await deleteLearnVideo(deleteTarget.id);
      show("Deleted.", "ok");
      setDeleteTarget(null);
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Delete failed.", "danger");
    } finally {
      setDeleting(false);
    }
  }

  const columns: Column<LearnVideo>[] = [
    {
      key: "video",
      header: "Video",
      primary: true,
      render: (v) => (
        <div className="flex items-start gap-3">
          <a
            href={`https://www.youtube.com/watch?v=${v.youtube_video_id}`}
            target="_blank"
            rel="noreferrer"
            className="shrink-0"
          >
            {v.thumbnail_url ? (
              <img src={v.thumbnail_url} alt="" className="aspect-video w-28 rounded object-cover" loading="lazy" />
            ) : (
              <div className="aspect-video w-28 rounded bg-ink-200" />
            )}
          </a>
          <div className="min-w-0">
            <a
              href={`https://www.youtube.com/watch?v=${v.youtube_video_id}`}
              target="_blank"
              rel="noreferrer"
              className="line-clamp-2 font-medium text-ink-800 hover:underline"
            >
              {v.title ?? v.youtube_video_id}
            </a>
            <div className="mt-0.5 text-xs text-ink-500">
              {[v.channel_title, fmtDuration(v.duration_seconds), fmtDate(v.published_at)].filter(Boolean).join(" · ")}
            </div>
          </div>
        </div>
      ),
    },
    {
      key: "category",
      header: "Category",
      render: (v) => (
        <select
          value={v.category_id}
          disabled={busyId === v.id}
          onChange={(e) => moveVideo(v, e.target.value)}
          aria-label={`Category for ${v.title ?? v.youtube_video_id}`}
          className="max-w-[12rem] rounded-md border border-ink-300 px-2 py-1 text-sm"
        >
          {categoryOptions.map((o) => (
            <option key={o.id} value={o.id}>
              {o.label}
            </option>
          ))}
        </select>
      ),
    },
    {
      key: "transcript",
      header: "Transcript",
      render: (v) => (
        <div className="flex flex-col items-start gap-1">
          <Badge tone={STATUS_TONE[v.transcript_status]}>{STATUS_LABEL[v.transcript_status]}</Badge>
          <span className="max-w-[16rem] text-xs text-ink-500">{transcriptDetail(v)}</span>
        </div>
      ),
    },
    {
      key: "added",
      header: "Added",
      render: (v) => (
        <div className="text-sm">
          {fmtDate(v.created_at)}
          {v.added_by && <div className="text-xs text-ink-500">{v.added_by}</div>}
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      render: (v) => (
        <div className="flex flex-wrap gap-1">
          {v.transcript_status === "ready" && (
            <Button
              size="sm"
              variant="secondary"
              disabled={transcriptLoadingId === v.id}
              onClick={() => openTranscript(v)}
            >
              {transcriptLoadingId === v.id ? "Loading…" : "View transcript"}
            </Button>
          )}
          <Button
            size="sm"
            variant={v.transcript_status === "unavailable" ? "primary" : "ghost"}
            onClick={() => openPaste(v)}
          >
            {v.transcript_status === "ready" ? "Replace transcript" : "Paste transcript"}
          </Button>
          {(v.transcript_status === "ready" || v.transcript_status === "unavailable") && (
            <Button size="sm" variant="ghost" disabled={busyId === v.id} onClick={() => retry(v)}>
              Retry on Pi
            </Button>
          )}
          <Button size="sm" variant="ghost" className="text-danger" onClick={() => setDeleteTarget(v)}>
            Delete
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold text-ink-800">Learn Videos</h1>
      <p className="text-sm text-ink-500">
        YouTube videos that Learn articles will be written from, like Bowling Tips. Add a video and its details come from
        YouTube. The Pi transcript fetcher picks it up on its next daily run.
      </p>

      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-ink-200 bg-ink-100 p-3">
        <div className="w-full sm:w-auto sm:flex-1">
          <label className="mb-1 block text-xs font-medium text-ink-600">YouTube link</label>
          <input
            value={newUrl}
            onChange={(e) => setNewUrl(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleAdd();
            }}
            placeholder="https://www.youtube.com/watch?v=..."
            className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm"
          />
        </div>
        <div className="w-full sm:w-auto">
          <label className="mb-1 block text-xs font-medium text-ink-600">Category</label>
          <select
            value={newCategory}
            onChange={(e) => setNewCategory(e.target.value)}
            className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm sm:w-56"
          >
            <option value="">Choose a category…</option>
            {categoryOptions.map((o) => (
              <option key={o.id} value={o.id}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <Button variant="primary" onClick={handleAdd} disabled={adding} className="w-full sm:w-auto">
          {adding ? "Adding…" : "Add video"}
        </Button>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <select
          value={filterCategory}
          onChange={(e) => {
            setOffset(0);
            setFilterCategory(e.target.value);
          }}
          aria-label="Filter by category"
          className="rounded-md border border-ink-300 px-2 py-1.5 text-sm"
        >
          <option value="">All categories</option>
          {categoryOptions.map((o) => (
            <option key={o.id} value={o.id}>
              {o.label}
            </option>
          ))}
        </select>
        <select
          value={filterStatus}
          onChange={(e) => {
            setOffset(0);
            setFilterStatus(e.target.value as LearnVideoTranscriptStatus | "");
          }}
          aria-label="Filter by transcript status"
          className="rounded-md border border-ink-300 px-2 py-1.5 text-sm"
        >
          <option value="">Any transcript status</option>
          <option value="awaiting">{STATUS_LABEL.awaiting}</option>
          <option value="retrying">{STATUS_LABEL.retrying}</option>
          <option value="ready">{STATUS_LABEL.ready}</option>
          <option value="unavailable">{STATUS_LABEL.unavailable}</option>
        </select>
      </div>

      {error && <div className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{error}</div>}

      <DataTable
        columns={columns}
        rows={items}
        getRowId={(v) => v.id}
        emptyMessage={filterCategory || filterStatus ? "No videos match those filters." : "No videos yet. Add one above."}
        loading={loading}
      />

      <Pagination offset={offset} limit={LIMIT} itemCount={items.length} onOffsetChange={setOffset} />

      <Modal
        open={transcriptFor !== null}
        onClose={() => setTranscriptFor(null)}
        title={transcriptFor?.title ?? "Transcript"}
        wide
        footer={
          <Button variant="secondary" onClick={() => setTranscriptFor(null)}>
            Close
          </Button>
        }
      >
        <div className="max-h-[60vh] overflow-y-auto whitespace-pre-wrap text-sm leading-relaxed text-ink-700">
          {transcriptFor?.transcript}
        </div>
      </Modal>

      <Modal
        open={pasteFor !== null}
        onClose={() => (pasting ? undefined : setPasteFor(null))}
        title={`Paste transcript: ${pasteFor?.title ?? pasteFor?.youtube_video_id ?? ""}`}
        wide
        footer={
          <>
            <Button variant="secondary" onClick={() => setPasteFor(null)} disabled={pasting}>
              Cancel
            </Button>
            <Button variant="primary" onClick={savePaste} disabled={pasting || !pasteText.trim()}>
              {pasting ? "Saving…" : "Save transcript"}
            </Button>
          </>
        }
      >
        {pasteFor && (
          <div className="flex flex-col gap-3">
            <ol className="list-decimal pl-5 text-sm text-ink-600">
              <li>
                <a
                  href={`https://www.youtube.com/watch?v=${pasteFor.youtube_video_id}`}
                  target="_blank"
                  rel="noreferrer"
                  className="text-primary hover:underline"
                >
                  Open the video on YouTube
                </a>
                , expand the description, and click <strong>Show transcript</strong>.
              </li>
              <li>Select all of the transcript text in the panel and copy it.</li>
              <li>Paste it below. Timestamps are removed automatically.</li>
            </ol>
            <textarea
              autoFocus
              value={pasteText}
              onChange={(e) => setPasteText(e.target.value)}
              rows={12}
              placeholder={"0:00\nso today we're going to talk about...\n0:04\n..."}
              className="w-full rounded-md border border-ink-300 px-2 py-1.5 font-mono text-xs"
            />
            {pasteFor.transcript_status === "ready" && (
              <p className="text-xs text-ink-500">This replaces the current transcript.</p>
            )}
          </div>
        )}
      </Modal>

      <Modal
        open={deleteTarget !== null}
        onClose={() => (deleting ? undefined : setDeleteTarget(null))}
        title={`Delete "${deleteTarget?.title ?? deleteTarget?.youtube_video_id ?? ""}"?`}
        footer={
          <>
            <Button variant="secondary" onClick={() => setDeleteTarget(null)} disabled={deleting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={confirmDelete} disabled={deleting}>
              {deleting ? "Deleting…" : "Delete"}
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-600">
          Removes the video and its transcript from BowlerIQ. Nothing on YouTube is affected. You can add it again later.
        </p>
      </Modal>
    </div>
  );
}
