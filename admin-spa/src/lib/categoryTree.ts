import type { Category } from "../api/types";

// Category tree helpers (migration 037). GET /categories is a flat list;
// these build the tree from parent_id. Shared by the Categories and Learn
// Videos pages.

export interface CategoryTreeNode {
  category: Category;
  depth: number;
  siblings: Category[];
}

export function childrenOf(categories: Category[], parentId: string | null): Category[] {
  return categories
    .filter((c) => c.parent_id === parentId)
    .sort((a, b) => a.display_order - b.display_order || a.name.localeCompare(b.name));
}

// Depth-first, so each subcategory comes directly after its parent.
export function flattenCategoryTree(categories: Category[]): CategoryTreeNode[] {
  const out: CategoryTreeNode[] = [];
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

// <option> labels that show nesting, e.g. "— — Corner Pins".
export function categoryOptionLabel(node: CategoryTreeNode): string {
  return `${"— ".repeat(node.depth)}${node.category.name}`;
}
