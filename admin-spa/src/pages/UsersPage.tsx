import { useEffect, useState } from "react";
import { createUser, deleteUser, listUsers, setUserEnabled, setUserGroup } from "../api/client";
import type { AdminUser } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import Badge from "../components/Badge";
import Button from "../components/Button";
import type { Column } from "../components/DataTable";
import DataTable from "../components/DataTable";
import Modal from "../components/Modal";
import { useToast } from "../components/Toast";

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString();
}

// Al: "can we add user management and a user group that has no access
// to user managment" -- see admin_api/service.py's own "User management
// (Cognito)" section header comment for the full design: Admins get
// this page, Editors get a 403 from every one of these routes (this
// page also never appears in Editors' own nav -- see Layout.tsx's
// NAV_ITEMS filter -- but that's a UX nicety, the backend gate is the
// real boundary). Admins/Editors are the only two groups; there's no
// "invite" flow beyond an Admin picking an email + a group here.
export default function UsersPage() {
  const { user: me } = useAuth();
  const { show } = useToast();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [newEmail, setNewEmail] = useState("");
  const [newGroup, setNewGroup] = useState<"Admins" | "Editors">("Editors");
  const [creating, setCreating] = useState(false);
  // Shown exactly once, right after creation -- create_user's own
  // password is never persisted or re-fetchable server-side (see
  // CreateUserResult's own comment in types.ts), so this is the only
  // chance to hand it to whoever's setting the new account up.
  const [createdCredentials, setCreatedCredentials] = useState<{ email: string; password: string } | null>(null);

  const [deleteTarget, setDeleteTarget] = useState<AdminUser | null>(null);
  const [deleteSubmitting, setDeleteSubmitting] = useState(false);

  const [busyUsername, setBusyUsername] = useState<string | null>(null);

  function load() {
    setLoading(true);
    setError(null);
    listUsers()
      .then(setUsers)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load users."))
      .finally(() => setLoading(false));
  }

  useEffect(load, []);

  async function handleCreate() {
    const email = newEmail.trim();
    if (!email) {
      show("Email is required.", "danger");
      return;
    }
    setCreating(true);
    try {
      const result = await createUser({ email, group: newGroup });
      setCreatedCredentials({ email: result.email, password: result.password });
      setNewEmail("");
      setNewGroup("Editors");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Failed to create user.", "danger");
    } finally {
      setCreating(false);
    }
  }

  async function handleGroupChange(u: AdminUser, group: "Admins" | "Editors") {
    setBusyUsername(u.username);
    try {
      await setUserGroup(u.username, group);
      show(`${u.email} moved to ${group}.`, "ok");
      load();
    } catch (err) {
      // Most likely the last-Admin lockout guard (service.set_user_group)
      // -- surfaced verbatim, it's already a plain-English explanation.
      show(err instanceof Error ? err.message : "Failed to change group.", "danger");
    } finally {
      setBusyUsername(null);
    }
  }

  async function handleToggleEnabled(u: AdminUser) {
    setBusyUsername(u.username);
    try {
      await setUserEnabled(u.username, !u.enabled);
      show(`${u.email} ${u.enabled ? "disabled" : "enabled"}.`, "ok");
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Failed to update account.", "danger");
    } finally {
      setBusyUsername(null);
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleteSubmitting(true);
    try {
      await deleteUser(deleteTarget.username);
      show(`${deleteTarget.email} deleted.`, "ok");
      setDeleteTarget(null);
      load();
    } catch (err) {
      // Most likely the last-Admin lockout guard (service.delete_user).
      show(err instanceof Error ? err.message : "Failed to delete user.", "danger");
    } finally {
      setDeleteSubmitting(false);
    }
  }

  const columns: Column<AdminUser>[] = [
    {
      key: "email",
      header: "Email",
      render: (u) => (
        <span className="font-medium text-ink-800">
          {u.email}
          {u.email === me?.email && <span className="ml-1.5 text-xs text-ink-400">(you)</span>}
        </span>
      ),
    },
    {
      key: "group",
      header: "Group",
      render: (u) =>
        u.group === null ? (
          <Badge tone="danger">No group -- no access</Badge>
        ) : (
          <select
            value={u.group}
            disabled={busyUsername === u.username}
            onChange={(e) => handleGroupChange(u, e.target.value as "Admins" | "Editors")}
            className="rounded-md border border-ink-300 px-2 py-1 text-sm"
          >
            <option value="Admins">Admins</option>
            <option value="Editors">Editors</option>
          </select>
        ),
    },
    {
      key: "status",
      header: "Status",
      render: (u) => <Badge tone={u.enabled ? "muted" : "danger"}>{u.enabled ? u.status : "disabled"}</Badge>,
    },
    { key: "created_at", header: "Created", render: (u) => fmtDate(u.created_at) },
    {
      key: "actions",
      header: "",
      render: (u) => (
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="ghost" disabled={busyUsername === u.username} onClick={() => handleToggleEnabled(u)}>
            {u.enabled ? "Disable" : "Enable"}
          </Button>
          <Button size="sm" variant="ghost" className="text-danger" onClick={() => setDeleteTarget(u)}>
            Delete
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold text-ink-800">Users</h1>
      <p className="text-sm text-ink-500">
        Admins can do anything the shared automation token can; Editors get routine review work (approve/reject,
        regenerate) without the more destructive actions -- and neither group can reach this page except Admins. At
        least one Admin must always remain: you can't move or delete the last one.
      </p>

      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-ink-200 bg-ink-100 p-3">
        <div className="w-full sm:w-auto">
          <label className="mb-1 block text-xs font-medium text-ink-600">Email</label>
          <input
            value={newEmail}
            onChange={(e) => setNewEmail(e.target.value)}
            placeholder="name@example.com"
            className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm sm:w-64"
          />
        </div>
        <div className="w-full sm:w-auto">
          <label className="mb-1 block text-xs font-medium text-ink-600">Group</label>
          <select
            value={newGroup}
            onChange={(e) => setNewGroup(e.target.value as "Admins" | "Editors")}
            className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm sm:w-40"
          >
            <option value="Editors">Editors</option>
            <option value="Admins">Admins</option>
          </select>
        </div>
        <Button variant="primary" onClick={handleCreate} disabled={creating} className="w-full sm:w-auto">
          {creating ? "Creating…" : "Create user"}
        </Button>
      </div>

      {error && <div className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{error}</div>}

      <DataTable columns={columns} rows={users} getRowId={(u) => u.username} emptyMessage="No users yet." loading={loading} />

      <Modal
        open={createdCredentials !== null}
        onClose={() => setCreatedCredentials(null)}
        title="User created"
        footer={<Button variant="primary" onClick={() => setCreatedCredentials(null)}>Done</Button>}
      >
        <p className="mb-3 text-sm text-ink-600">
          Share this password with <span className="font-medium text-ink-800">{createdCredentials?.email}</span> now --
          it's set as their permanent sign-in password and won't be shown again.
        </p>
        <code className="block select-all rounded-md bg-ink-200 px-3 py-2 font-mono text-sm text-ink-800">
          {createdCredentials?.password}
        </code>
      </Modal>

      <Modal
        open={deleteTarget !== null}
        onClose={() => (deleteSubmitting ? undefined : setDeleteTarget(null))}
        title={`Delete ${deleteTarget?.email ?? ""}?`}
        footer={
          <>
            <Button variant="secondary" onClick={() => setDeleteTarget(null)} disabled={deleteSubmitting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={confirmDelete} disabled={deleteSubmitting}>
              {deleteSubmitting ? "Deleting…" : "Delete"}
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-600">
          This is irreversible. Consider Disable instead if you might need to restore access later.
        </p>
      </Modal>
    </div>
  );
}
