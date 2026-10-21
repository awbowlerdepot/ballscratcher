import { useEffect, useState } from "react";
import { createPartner, getPartnerUsage, listPartners, revokePartner, rotatePartnerKey, updatePartner } from "../api/client";
import type { Partner, PartnerUsage, PartnerWithKey } from "../api/types";
import Badge from "../components/Badge";
import Button from "../components/Button";
import type { Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import Modal from "../components/Modal";
import { useToast } from "../components/Toast";

// Partner API access (runbook 6eg). Al: "is the access management and keys
// something we can add to the admin.bowleriq.com". Admins only -- the nav
// item is adminOnly and the route is wrapped in AdminRoute, but the real
// boundary is admin_api's _require_admin on every /partners route.
//
// A partner is active exactly when its key is in the Secrets Manager secret
// the partner API authorizer reads; admin_api keeps a record (who/when/
// notes/last 4 of the key) alongside it. New and rotated keys come back
// from the API ONCE and are never stored, so the "key issued" dialog is the
// only chance to copy one -- same as a new user's password on UsersPage.
// Changes take effect within ~5 minutes (the authorizer's cache).

const DOCS_URL = "https://docs.bowleriq.io";

function fmtDate(iso: string | null): string {
  return iso ? new Date(iso).toLocaleDateString() : "--";
}

function fmtAgo(iso: string | null): string {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400 * 2) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

type UsageState = { status: "loading" } | { status: "error"; message: string } | { status: "ok"; data: Record<string, PartnerUsage> };

export default function PartnersPage() {
  const { show } = useToast();
  const [partners, setPartners] = useState<Partner[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [usage, setUsage] = useState<UsageState>({ status: "loading" });

  const [newName, setNewName] = useState("");
  const [newContact, setNewContact] = useState("");
  const [newNotes, setNewNotes] = useState("");
  const [creating, setCreating] = useState(false);

  // The one-time key dialog (create or rotate).
  const [issued, setIssued] = useState<{ partner: PartnerWithKey; rotated: boolean } | null>(null);
  const [copied, setCopied] = useState(false);

  const [confirm, setConfirm] = useState<{ kind: "rotate" | "revoke"; partner: Partner } | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);

  const [editing, setEditing] = useState<Partner | null>(null);
  const [editContact, setEditContact] = useState("");
  const [editNotes, setEditNotes] = useState("");
  const [editBusy, setEditBusy] = useState(false);

  function load() {
    setLoading(true);
    setError(null);
    listPartners()
      .then(setPartners)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load partners."))
      .finally(() => setLoading(false));
  }

  function loadUsage() {
    setUsage({ status: "loading" });
    getPartnerUsage()
      .then((data) => setUsage({ status: "ok", data }))
      .catch((err) => setUsage({ status: "error", message: err instanceof Error ? err.message : "Usage unavailable." }));
  }

  useEffect(() => {
    load();
    loadUsage();
  }, []);

  async function handleCreate() {
    const name = newName.trim();
    if (!name) {
      show("Partner name is required.", "danger");
      return;
    }
    setCreating(true);
    try {
      const partner = await createPartner({ name, contact: newContact.trim() || undefined, notes: newNotes.trim() || undefined });
      setCopied(false);
      setIssued({ partner, rotated: false });
      setNewName("");
      setNewContact("");
      setNewNotes("");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Failed to create partner.", "danger");
    } finally {
      setCreating(false);
    }
  }

  async function handleConfirm() {
    if (!confirm) return;
    setConfirmBusy(true);
    try {
      if (confirm.kind === "rotate") {
        const partner = await rotatePartnerKey(confirm.partner.name);
        setCopied(false);
        setIssued({ partner, rotated: true });
      } else {
        await revokePartner(confirm.partner.name);
        show(`${confirm.partner.name}'s key revoked. Access ends within about 5 minutes.`, "ok");
      }
      setConfirm(null);
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Request failed.", "danger");
    } finally {
      setConfirmBusy(false);
    }
  }

  function startEdit(p: Partner) {
    setEditing(p);
    setEditContact(p.contact ?? "");
    setEditNotes(p.notes ?? "");
  }

  async function saveEdit() {
    if (!editing) return;
    setEditBusy(true);
    try {
      await updatePartner(editing.name, { contact: editContact.trim() || null, notes: editNotes.trim() || null });
      show("Saved.", "ok");
      setEditing(null);
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Save failed.", "danger");
    } finally {
      setEditBusy(false);
    }
  }

  async function copyKey() {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.partner.key);
      setCopied(true);
    } catch {
      show("Couldn't copy -- select the key and copy it manually.", "danger");
    }
  }

  function renderUsage(p: Partner) {
    if (usage.status === "loading") return <span className="text-xs text-ink-400">Loading…</span>;
    if (usage.status === "error") return <span className="text-xs text-ink-400">Unavailable</span>;
    const u = usage.data[p.name];
    if (!u) return <span className="text-xs text-ink-400">No calls in 30 days</span>;
    return (
      <div className="text-xs">
        <div className="text-ink-800">
          {u.calls_7d.toLocaleString()} <span className="text-ink-500">/ 7d</span> · {u.calls_30d.toLocaleString()}{" "}
          <span className="text-ink-500">/ 30d</span>
        </div>
        <div className="text-ink-500">
          Last call {fmtAgo(u.last_call_at)}
          {u.errors_7d > 0 && <span className="ml-1 font-medium text-danger">· {u.errors_7d} errors (7d)</span>}
        </div>
      </div>
    );
  }

  const columns: Column<Partner>[] = [
    {
      key: "name",
      header: "Partner",
      render: (p) => (
        <div>
          <div className="font-medium text-ink-800">{p.name}</div>
          {p.contact && <div className="text-xs text-ink-500">{p.contact}</div>}
          {p.notes && <div className="max-w-xs truncate text-xs text-ink-400" title={p.notes}>{p.notes}</div>}
        </div>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (p) =>
        p.status === "active" ? <Badge tone="ok">active</Badge> : <Badge tone="muted">revoked</Badge>,
    },
    {
      key: "key",
      header: "Key",
      render: (p) =>
        p.status !== "active" ? (
          <span className="text-xs text-ink-400">--</span>
        ) : p.key_last4 ? (
          <code className="font-mono text-xs text-ink-700">…{p.key_last4}</code>
        ) : (
          <span className="text-xs text-ink-400" title="Issued with the script before admin tracking; rotate to start tracking it.">
            not recorded
          </span>
        ),
    },
    { key: "usage", header: "Usage", render: renderUsage },
    {
      key: "history",
      header: "History",
      render: (p) => (
        <div className="text-xs text-ink-500">
          <div>
            Created {fmtDate(p.created_at)}
            {p.created_by && <span className="text-ink-400"> by {p.created_by}</span>}
          </div>
          {p.rotated_at && <div>Rotated {fmtDate(p.rotated_at)}{p.rotated_by && <span className="text-ink-400"> by {p.rotated_by}</span>}</div>}
          {p.status === "revoked" && p.revoked_at && (
            <div>Revoked {fmtDate(p.revoked_at)}{p.revoked_by && <span className="text-ink-400"> by {p.revoked_by}</span>}</div>
          )}
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      render: (p) => (
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={() => startEdit(p)} disabled={p.created_at === null && p.status === "active"}>
            Edit
          </Button>
          {p.status === "active" ? (
            <>
              <Button size="sm" variant="ghost" onClick={() => setConfirm({ kind: "rotate", partner: p })}>
                Rotate key
              </Button>
              <Button size="sm" variant="ghost" className="text-danger" onClick={() => setConfirm({ kind: "revoke", partner: p })}>
                Revoke
              </Button>
            </>
          ) : (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setNewName(p.name);
                window.scrollTo({ top: 0, behavior: "smooth" });
              }}
            >
              Re-issue…
            </Button>
          )}
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold text-ink-800">Partners</h1>
          <p className="text-sm text-ink-500">
            Keys for the Partner API (<a className="underline" href={DOCS_URL} target="_blank" rel="noreferrer">docs.bowleriq.io</a>).
            One key per platform. New keys are shown once and never stored; changes take effect within about 5 minutes.
          </p>
        </div>
        <Button size="sm" variant="secondary" onClick={loadUsage} disabled={usage.status === "loading"}>
          {usage.status === "loading" ? "Loading usage…" : "Refresh usage"}
        </Button>
      </div>

      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-ink-200 bg-ink-100 p-3">
        <div className="w-full sm:w-auto">
          <label className="mb-1 block text-xs font-medium text-ink-600">Partner name</label>
          <input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="Acme_Pro_Shop"
            className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm sm:w-52"
          />
        </div>
        <div className="w-full sm:w-auto">
          <label className="mb-1 block text-xs font-medium text-ink-600">Contact (optional)</label>
          <input
            value={newContact}
            onChange={(e) => setNewContact(e.target.value)}
            placeholder="Name, email or phone"
            className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm sm:w-56"
          />
        </div>
        <div className="w-full sm:flex-1">
          <label className="mb-1 block text-xs font-medium text-ink-600">Notes (optional)</label>
          <input
            value={newNotes}
            onChange={(e) => setNewNotes(e.target.value)}
            placeholder="What they use it for"
            className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm"
          />
        </div>
        <Button variant="primary" onClick={handleCreate} disabled={creating} className="w-full sm:w-auto">
          {creating ? "Issuing…" : "Issue key"}
        </Button>
        <p className="w-full text-xs text-ink-500">
          The name can't be changed later: it's how this partner's calls are labeled in the logs. Letters, digits, - and _ only.
        </p>
      </div>

      {error && <div className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{error}</div>}

      <DataTable columns={columns} rows={partners} getRowId={(p) => p.name} emptyMessage="No partners yet." loading={loading} />

      {/* One-time key dialog (create or rotate). */}
      <Modal
        open={issued !== null}
        onClose={() => setIssued(null)}
        title={issued?.rotated ? `New key for ${issued.partner.name}` : `Key issued for ${issued?.partner.name ?? ""}`}
        footer={
          <Button variant="primary" onClick={() => setIssued(null)}>
            I've copied it
          </Button>
        }
      >
        <p className="mb-3 text-sm text-ink-600">
          Copy this key now and send it to the partner privately. <span className="font-medium text-ink-800">It won't be shown again</span>{" "}
          and isn't stored anywhere; if it's lost, rotate to issue a new one.
          {issued?.rotated && " The previous key stops working within about 5 minutes."}
        </p>
        <div className="flex items-stretch gap-2">
          <code className="block min-w-0 flex-1 select-all break-all rounded-md bg-ink-200 px-3 py-2 font-mono text-sm text-ink-800">
            {issued?.partner.key}
          </code>
          <Button variant="secondary" onClick={copyKey}>
            {copied ? "Copied" : "Copy"}
          </Button>
        </div>
        <p className="mt-3 text-xs text-ink-500">
          Send with it: <span className="font-mono">{DOCS_URL}</span> (paste the key into the page's Authentication box to try calls). Their
          server sends it as <span className="font-mono">Authorization: Bearer &lt;key&gt;</span>. It works within about 5 minutes.
        </p>
      </Modal>

      {/* Rotate / revoke confirmation. */}
      <Modal
        open={confirm !== null}
        onClose={() => (confirmBusy ? undefined : setConfirm(null))}
        title={confirm?.kind === "rotate" ? `Rotate ${confirm.partner.name}'s key?` : `Revoke ${confirm?.partner.name ?? ""}?`}
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirm(null)} disabled={confirmBusy}>
              Cancel
            </Button>
            <Button variant={confirm?.kind === "revoke" ? "danger" : "primary"} onClick={handleConfirm} disabled={confirmBusy}>
              {confirmBusy ? "Working…" : confirm?.kind === "rotate" ? "Rotate key" : "Revoke access"}
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-600">
          {confirm?.kind === "rotate"
            ? "A new key is issued and shown once. Their current key stops working within about 5 minutes, so send them the new one first if they're live."
            : "Their key stops working within about 5 minutes. Their record stays here, and you can issue them a new key later."}
        </p>
      </Modal>

      {/* Edit contact / notes. */}
      <Modal
        open={editing !== null}
        onClose={() => (editBusy ? undefined : setEditing(null))}
        title={`Edit ${editing?.name ?? ""}`}
        footer={
          <>
            <Button variant="secondary" onClick={() => setEditing(null)} disabled={editBusy}>
              Cancel
            </Button>
            <Button variant="primary" onClick={saveEdit} disabled={editBusy}>
              {editBusy ? "Saving…" : "Save"}
            </Button>
          </>
        }
      >
        <label className="mb-1 block text-xs font-medium text-ink-600">Contact</label>
        <input
          value={editContact}
          onChange={(e) => setEditContact(e.target.value)}
          className="mb-3 w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm"
        />
        <label className="mb-1 block text-xs font-medium text-ink-600">Notes</label>
        <textarea
          value={editNotes}
          onChange={(e) => setEditNotes(e.target.value)}
          rows={3}
          className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm"
        />
      </Modal>
    </div>
  );
}
