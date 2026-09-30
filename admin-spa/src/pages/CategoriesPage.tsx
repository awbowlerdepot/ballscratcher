import { useEffect, useMemo, useState } from "react";
import { createCategory, deleteCategory, listCategories, reorderCategories, updateCategory } from "../api/client";
import type { Category } from "../api/types";
import Badge from "../components/Badge";
import Button from "../components/Button";
import Modal from "../components/Modal";
import Skeleton from "../components/Skeleton";
import { useToast } from "../components/Toast";

// Learn-site categories, nested (migration 037) -- Al: "create a category
// 'Bowling Tips' and then that can have sub categories". The API returns
// a flat list; the tree is built here from parent_id. Ordering is per
// sibling group (display_order), moved with the up/down buttons.

interface TreeNode {
  category: Category;
  depth: number;
  siblings: Category[];
}

// Same normalization as admin_api's slugify_category_name, so the preview
// in the form matches what the backend would generate.
function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function childrenOf(categories: Category[], parentId: string | null): Category[] {
  return categories
    .filter((c) => c.parent_id === parentId)
    .sort((a, b) => a.display_order - b.display_order || a.name.localeCompare(b.name));
}

// Depth-first, so each subcategory renders directly under its parent.
function flattenTree(categories: Category[]): TreeNode[] {
  const out: TreeNode[] = [];
  const walk = (parentId: string | null, depth: number) => {
    const siblings = childrenOf(categories, parentId);
    for (const category of siblings) {
      out.push({ category, depth, siblings });
      walk(category.id, depth + 1);
    }
  };
  walk(null, 0);
  return out;
}

function descendantIds(categories: Category[], id: string): Set<string> {
  const ids = new Set<string>([id]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const c of categories) {
      if (c.parent_id && ids.has(c.parent_id) && !ids.has(c.id)) {
        ids.add(c.id);
        grew = true;
      }
    }
  }
  return ids;
}

interface FormState {
  mode: "create" | "edit";
  id?: string;
  name: string;
  slug: string;
  slugTouched: boolean;
  parentId: string;
  description: string;
  productType: string;
}

export default function CategoriesPage() {
  const { show } = useToast();
  const [categories, setCategories] = useState<Category[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const [form, setForm] = useState<FormState | null>(null);
  const [saving, setSaving] = useState(false);

  const [deleteTarget, setDeleteTarget] = useState<Category | null>(null);
  const [deleting, setDeleting] = useState(false);

  function load() {
    setError(null);
    listCategories()
      .then(setCategories)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load categories."))
      .finally(() => setLoading(false));
  }

  useEffect(load, []);

  const tree = useMemo(() => flattenTree(categories), [categories]);
  const byId = useMemo(() => new Map(categories.map((c) => [c.id, c])), [categories]);

  // Parent picker: every category except the one being edited and its own
  // subtree (the backend rejects those moves too), labelled with its path.
  const parentOptions = useMemo(() => {
    const excluded = form?.mode === "edit" && form.id ? descendantIds(categories, form.id) : new Set<string>();
    return tree
      .filter((n) => !excluded.has(n.category.id))
      .map((n) => ({ id: n.category.id, label: `${"— ".repeat(n.depth)}${n.category.name}` }));
  }, [tree, categories, form?.mode, form?.id]);

  function openCreate(parentId: string | null = null) {
    setForm({
      mode: "create",
      name: "",
      slug: "",
      slugTouched: false,
      parentId: parentId ?? "",
      description: "",
      productType: "",
    });
  }

  function openEdit(c: Category) {
    setForm({
      mode: "edit",
      id: c.id,
      name: c.name,
      slug: c.slug,
      slugTouched: true,
      parentId: c.parent_id ?? "",
      description: c.description ?? "",
      productType: c.product_type ?? "",
    });
  }

  async function saveForm() {
    if (!form) return;
    const name = form.name.trim();
    if (!name) {
      show("Name is required.", "danger");
      return;
    }
    const slug = (form.slugTouched ? form.slug : slugify(name)).trim();
    const parentId = form.parentId || null;
    setSaving(true);
    try {
      if (form.mode === "create") {
        await createCategory({
          name,
          slug: slug || undefined,
          parent_id: parentId,
          description: form.description.trim() || undefined,
          product_type: form.productType.trim() || undefined,
        });
        show(`Created "${name}".`, "ok");
      } else if (form.id) {
        const current = byId.get(form.id);
        await updateCategory(form.id, {
          name,
          slug,
          description: form.description.trim(),
          product_type: form.productType.trim(),
          // Only send parent_id when it actually changed -- re-sending the
          // same parent would move the category to the end of its siblings.
          ...(current && (current.parent_id ?? null) !== parentId ? { parent_id: parentId } : {}),
        });
        show(`Saved "${name}".`, "ok");
      }
      setForm(null);
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Save failed.", "danger");
    } finally {
      setSaving(false);
    }
  }

  async function move(node: TreeNode, direction: -1 | 1) {
    const ids = node.siblings.map((c) => c.id);
    const index = ids.indexOf(node.category.id);
    const target = index + direction;
    if (target < 0 || target >= ids.length) return;
    [ids[index], ids[target]] = [ids[target], ids[index]];
    setBusyId(node.category.id);
    try {
      await reorderCategories(node.category.parent_id, ids);
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Reorder failed.", "danger");
    } finally {
      setBusyId(null);
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await deleteCategory(deleteTarget.id);
      show(`Deleted "${deleteTarget.name}".`, "ok");
      setDeleteTarget(null);
      load();
    } catch (err) {
      show(err instanceof Error ? err.message : "Delete failed.", "danger");
    } finally {
      setDeleting(false);
    }
  }

  const deleteBlocker = deleteTarget
    ? categories.some((c) => c.parent_id === deleteTarget.id)
      ? "It still has subcategories. Move or delete them first."
      : deleteTarget.article_count > 0
        ? `It still has ${deleteTarget.article_count} article${deleteTarget.article_count === 1 ? "" : "s"}. Move them to another category first.`
        : null
    : null;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold text-ink-800">Categories</h1>
        <Button variant="primary" onClick={() => openCreate(null)}>
          New category
        </Button>
      </div>
      <p className="text-sm text-ink-500">
        How Learn articles are organized. Top-level categories become tabs in the Learn header once more than one of them
        has published articles; subcategories can nest to any depth. Empty categories stay hidden on the Learn site.
      </p>

      {error && <div className="rounded-md bg-danger-light px-4 py-3 text-sm text-danger">{error}</div>}

      <div className="overflow-hidden rounded-lg border border-ink-200 bg-white">
        {loading ? (
          <div className="flex flex-col gap-3 p-4">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : tree.length === 0 ? (
          <p className="p-6 text-center text-sm text-ink-500">No categories yet.</p>
        ) : (
          <ul className="divide-y divide-ink-200">
            {tree.map((node) => {
              const c = node.category;
              const index = node.siblings.findIndex((s) => s.id === c.id);
              return (
                <li
                  key={c.id}
                  className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3"
                  style={{ paddingLeft: `${1 + node.depth * 1.5}rem` }}
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      {node.depth > 0 && <span className="text-ink-300">└</span>}
                      <span className="font-medium text-ink-800">{c.name}</span>
                      <span className="font-mono text-xs text-ink-400">{c.slug}</span>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-2">
                      <Badge tone={c.article_count > 0 ? "primary" : "muted"}>
                        {c.article_count} article{c.article_count === 1 ? "" : "s"}
                      </Badge>
                      {c.article_types.map((t) => (
                        <Badge key={t.id}>{t.name}</Badge>
                      ))}
                      {c.product_type && (
                        <span className="text-xs text-ink-500">auto-assigns {c.product_type} articles</span>
                      )}
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-1">
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={`Move ${c.name} up`}
                      disabled={index === 0 || busyId !== null}
                      onClick={() => move(node, -1)}
                    >
                      ↑
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={`Move ${c.name} down`}
                      disabled={index === node.siblings.length - 1 || busyId !== null}
                      onClick={() => move(node, 1)}
                    >
                      ↓
                    </Button>
                    <Button size="sm" variant="secondary" onClick={() => openCreate(c.id)}>
                      Add subcategory
                    </Button>
                    <Button size="sm" variant="secondary" onClick={() => openEdit(c)}>
                      Edit
                    </Button>
                    <Button size="sm" variant="ghost" className="text-danger" onClick={() => setDeleteTarget(c)}>
                      Delete
                    </Button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <Modal
        open={form !== null}
        onClose={() => (saving ? undefined : setForm(null))}
        title={form?.mode === "edit" ? "Edit category" : "New category"}
        footer={
          <>
            <Button variant="secondary" onClick={() => setForm(null)} disabled={saving}>
              Cancel
            </Button>
            <Button variant="primary" onClick={saveForm} disabled={saving}>
              {saving ? "Saving…" : form?.mode === "edit" ? "Save" : "Create"}
            </Button>
          </>
        }
      >
        {form && (
          <div className="flex flex-col gap-3">
            <div>
              <label className="mb-1 block text-xs font-medium text-ink-600">Name</label>
              <input
                autoFocus
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="e.g. Bowling Tips"
                className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-ink-600">Slug</label>
              <input
                value={form.slugTouched ? form.slug : slugify(form.name)}
                onChange={(e) => setForm({ ...form, slug: e.target.value, slugTouched: true })}
                placeholder="bowling-tips"
                className="w-full rounded-md border border-ink-300 px-2 py-1.5 font-mono text-sm"
              />
              <p className="mt-1 text-xs text-ink-500">Used in URLs. Must be unique across all categories.</p>
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-ink-600">Parent</label>
              <select
                value={form.parentId}
                onChange={(e) => setForm({ ...form, parentId: e.target.value })}
                className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm"
              >
                <option value="">Top level (Learn header tab)</option>
                {parentOptions.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-ink-600">Description (optional)</label>
              <textarea
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                rows={2}
                className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-ink-600">Auto-assign product type (optional)</label>
              <input
                value={form.productType}
                onChange={(e) => setForm({ ...form, productType: e.target.value })}
                placeholder="e.g. ball"
                className="w-full rounded-md border border-ink-300 px-2 py-1.5 text-sm"
              />
              <p className="mt-1 text-xs text-ink-500">
                New product articles for this product type are filed here automatically. Leave blank for categories
                like Bowling Tips that aren't tied to a product.
              </p>
            </div>
          </div>
        )}
      </Modal>

      <Modal
        open={deleteTarget !== null}
        onClose={() => (deleting ? undefined : setDeleteTarget(null))}
        title={`Delete "${deleteTarget?.name ?? ""}"?`}
        footer={
          <>
            <Button variant="secondary" onClick={() => setDeleteTarget(null)} disabled={deleting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={confirmDelete} disabled={deleting || deleteBlocker !== null}>
              {deleting ? "Deleting…" : "Delete"}
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-600">
          {deleteBlocker ?? "This removes the category and its article types. It can't be undone."}
        </p>
      </Modal>
    </div>
  );
}
