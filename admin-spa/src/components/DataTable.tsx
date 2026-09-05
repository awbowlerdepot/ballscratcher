import type { ReactNode } from "react";
import Button from "./Button";

export interface Column<T> {
  key: string;
  header: string;
  // Omit for a column that can't be sorted client-side (e.g. one that's
  // already driven by a server-side sort param, like Products'
  // popularity/total_adu -- see pages/ProductsPage.tsx).
  sortable?: boolean;
  render: (row: T) => ReactNode;
  className?: string;
}

export interface BulkAction<T> {
  label: string;
  onClick: (selectedRows: T[]) => void;
  variant?: "primary" | "secondary" | "danger";
}

interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[];
  getRowId: (row: T) => string;
  // Selection/bulk actions are opt-in -- pass all three to get
  // checkboxes + a bulk action bar (Products tab); omit them for a
  // plain read-only table (Dashboard's Top 10 lists).
  selectable?: boolean;
  selectedIds?: Set<string>;
  onSelectionChange?: (ids: Set<string>) => void;
  bulkActions?: BulkAction<T>[];
  sortKey?: string;
  sortDir?: "asc" | "desc";
  onSortChange?: (key: string) => void;
  emptyMessage?: string;
}

export default function DataTable<T>({
  columns,
  rows,
  getRowId,
  selectable = false,
  selectedIds,
  onSelectionChange,
  bulkActions,
  sortKey,
  sortDir,
  onSortChange,
  emptyMessage = "No results.",
}: DataTableProps<T>) {
  const allIds = rows.map(getRowId);
  const allSelected = selectable && allIds.length > 0 && allIds.every((id) => selectedIds?.has(id));
  const selectedRows = rows.filter((row) => selectedIds?.has(getRowId(row)));

  function toggleAll() {
    if (!onSelectionChange) return;
    onSelectionChange(allSelected ? new Set() : new Set(allIds));
  }

  function toggleRow(id: string) {
    if (!onSelectionChange || !selectedIds) return;
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onSelectionChange(next);
  }

  return (
    <div>
      {selectable && bulkActions && selectedRows.length > 0 && (
        <div className="mb-2 flex items-center gap-2 rounded-md bg-primary-light px-3 py-2 text-sm text-primary-dark">
          <span className="font-medium">{selectedRows.length} selected</span>
          <div className="ml-auto flex gap-2">
            {bulkActions.map((action) => (
              <Button
                key={action.label}
                variant={action.variant ?? "secondary"}
                size="sm"
                onClick={() => action.onClick(selectedRows)}
              >
                {action.label}
              </Button>
            ))}
          </div>
        </div>
      )}
      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              {selectable && (
                <th className="w-10 px-3 py-2">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    onChange={toggleAll}
                    aria-label="Select all rows"
                  />
                </th>
              )}
              {columns.map((col) => (
                <th key={col.key} className={`px-3 py-2 font-medium ${col.className ?? ""}`}>
                  {col.sortable && onSortChange ? (
                    <button
                      className="flex items-center gap-1 hover:text-slate-800"
                      onClick={() => onSortChange(col.key)}
                    >
                      {col.header}
                      {sortKey === col.key && <span>{sortDir === "asc" ? "↑" : "↓"}</span>}
                    </button>
                  ) : (
                    col.header
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={columns.length + (selectable ? 1 : 0)} className="px-3 py-8 text-center text-slate-400">
                  {emptyMessage}
                </td>
              </tr>
            )}
            {rows.map((row) => {
              const id = getRowId(row);
              return (
                <tr key={id} className="border-b border-slate-100 last:border-0 hover:bg-slate-50">
                  {selectable && (
                    <td className="px-3 py-2">
                      <input
                        type="checkbox"
                        checked={selectedIds?.has(id) ?? false}
                        onChange={() => toggleRow(id)}
                        aria-label={`Select row ${id}`}
                      />
                    </td>
                  )}
                  {columns.map((col) => (
                    <td key={col.key} className={`px-3 py-2 ${col.className ?? ""}`}>
                      {col.render(row)}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
