import { useEffect, useState } from "react";
import { createBlockedChannel, deleteBlockedChannel, listBlockedChannels } from "../api/client";
import type { BlockedChannel } from "../api/types";
import Button from "../components/Button";
import type { Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import Modal from "../components/Modal";
import { useToast } from "../components/Toast";

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString();
}

// Ports admin-site/index.html's Blocked Channels panel -- there it lives
// inside the Video Candidates tab (021_blocked_video_channels.sql's own
// comment: "small blocklist tightly coupled to this tab's own
// channel_title column"), but gets its own top-level nav item here like
// Cores/Coverstocks did, matching the rest of this SPA's one-page-per-
// resource pattern. A quick "Block channel" action also lives on each
// row of VideoCandidatesPage for the same in-context flow admin-site
// offered. No update endpoint -- a row here IS the block (see
// BlockedChannel's own comment in types.ts); create/delete only.
export default function BlockedChannelsPage() {
  const { show } = useToast();
  const [items, setItems] = useState<BlockedChannel[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [newTitle, setNewTitle] = useState("");
  const [newNote, setNewNote] = useState("");
  const [creating, setCreating] = useState(false);

  const [deleteTarget, setDeleteTarget] = useState<BlockedChannel | null>(null);
  const [deleteSubmitting, setDeleteSubmitting] = useState(false);

  function load() {
    setLoading(true);
    setError(null);
    listBlockedChannels()
      .then(setItems)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load blocked channels."))
      .finally(() => setLoading(false));
  }

  useEffect(load, []);

  async function handleCreate() {
    const title = newTitle.trim();
    if (!title) {
      show("Channel name is required.", "danger");
      return;
    }
    setCreating(true);
    try {
      await createBlockedChannel(title, newNote.trim() || undefined);
      show(`Blocked "${title}".`, "ok");
      setNewTitle("");
      setNewNote("");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Failed to block channel.", "danger");
    } finally {
      setCreating(false);
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleteSubmitting(true);
    try {
      await deleteBlockedChannel(deleteTarget.id);
      show("Unblocked.", "ok");
      setDeleteTarget(null);
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Unblock failed.", "danger");
    } finally {
      setDeleteSubmitting(false);
    }
  }

  const columns: Column<BlockedChannel>[] = [
    { key: "channel_title", header: "Channel", render: (c) => c.channel_title },
    {
      key: "note",
      header: "Note",
      render: (c) => c.note ?? <span className="text-ink-400">—</span>,
    },
    { key: "created_at", header: "Blocked", render: (c) => fmtDate(c.created_at) },
    {
      key: "actions",
      header: "",
      render: (c) => (
        <Button size="sm" variant="ghost" className="text-danger" onClick={() => setDeleteTarget(c)}>
          Unblock
        </Button>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold text-ink-800">Blocked Channels</h1>
      <p className="text-sm text-ink-500">
        Videos from a blocked channel stay approved and still feed the aggregate video summary, but are never pushed to
        BigCommerce's Product Videos feature on bowlerdepot.com. Match is case-insensitive on the channel's display name.
      </p>

      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-ink-200 bg-ink-100 p-3">
        <div className="w-full sm:w-auto">
          <label className="mb-1 block text-xs font-medium text-ink-600">Channel name</label>
          <input
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            placeholder="e.g. Bowling.com"
            className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm sm:w-56"
          />
        </div>
        <div className="w-full sm:w-auto">
          <label className="mb-1 block text-xs font-medium text-ink-600">Note (optional)</label>
          <input
            value={newNote}
            onChange={(e) => setNewNote(e.target.value)}
            placeholder="e.g. competitor retailer"
            className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm sm:w-56"
          />
        </div>
        <Button variant="primary" onClick={handleCreate} disabled={creating} className="w-full sm:w-auto">
          {creating ? "Blocking…" : "Block"}
        </Button>
      </div>

      {error && <div className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{error}</div>}

      <DataTable
        columns={columns}
        rows={items}
        getRowId={(c) => c.id}
        emptyMessage={loading ? "Loading…" : "No blocked channels yet."}
      />

      <Modal
        open={deleteTarget !== null}
        onClose={() => (deleteSubmitting ? undefined : setDeleteTarget(null))}
        title={`Unblock "${deleteTarget?.channel_title ?? ""}"?`}
        footer={
          <>
            <Button variant="secondary" onClick={() => setDeleteTarget(null)} disabled={deleteSubmitting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={confirmDelete} disabled={deleteSubmitting}>
              {deleteSubmitting ? "Unblocking…" : "Unblock"}
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-600">Its videos become eligible for the BigCommerce push again on the next sync run.</p>
      </Modal>
    </div>
  );
}
