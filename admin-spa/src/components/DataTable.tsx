import type { ReactNode } from "react";
import Button from "./Button";

export interface Column<T> {
  key: string;
  header: string;
  // Omit for a column that can't be sorted client-side (e.g. one that's
  // already driven by a server-side sort param, like Products'
  // popularity/total_daily_movement -- see pages/ProductsPage.tsx).
  sortable?: boolean;
  render: (row: T) => ReactNode;
  className?: string;
  // Card mode (below `md`, see the mobile-pass comment further down):
  // the default puts the label on the left and `render`'s content
  // pinned to the right (fine for a short value like a date or a
  // single badge). A column whose content is itself multi-line or
  // multi-button (a button group, a stacked toggle+timestamp) looks
  // squeezed against the right edge that way -- set this to have the
  // label sit above the content instead, with the content given the
  // card's full width. No effect at `md:`+ either way. See
  // ArticlesPage.tsx's "sync"/"actions" columns for the motivating case.
  stackOnMobile?: boolean;
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
      {/*
        Mobile pass (2026-09-05): below `md`, a real <table> with 6-8
        columns (Review Queue, Video Candidates, Products...) doesn't
        fit a ~375px phone -- the old `overflow-x-auto` wrapper kept the
        page itself from breaking, but sideways-scrolling a table to
        read one row is a bad "quick check on the go" experience. Below
        md this switches to the classic CSS-only responsive-table
        pattern instead: `table`/`thead`/`tbody`/`tr`/`td` all become
        plain flow elements (`block`/`flex`), each row becomes its own
        bordered card, and each cell gets its column header injected as
        an inline label via `data-label` + `before:content-[attr(...)]`
        (Tailwind's arbitrary-value content utility, supported since
        3.3 -- this project pins ^3.4.10). At md+ every one of these
        classes reverts to the normal table display values, so desktop
        is pixel-identical to before. A column with an empty header
        (the actions column on nearly every page) gets `data-label=""`,
        which renders no label -- exactly what's wanted there.

        Card-dial-in pass (2026-09-05, ArticlesPage): each cell defaults
        to label-left/content-right (`justify-between`), which is fine
        for a short value but pins a button group or a multi-line block
        against the card's right edge with less room than it needs. A
        column can opt into `stackOnMobile` (see the `Column` type
        above) to put its label on its own line above the content
        instead, with the content given the full card width.
      */}
      <div className="overflow-x-auto rounded-lg border border-ink-200 bg-ink-100">
        <table className="w-full text-left text-sm">
          <thead className="hidden border-b border-ink-200 bg-ink-50 text-xs uppercase tracking-wide text-ink-500 md:table-header-group">
            <tr className="md:table-row">
              {selectable && (
                <th className="w-10 px-2.5 py-1.5">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    onChange={toggleAll}
                    aria-label="Select all rows"
                  />
                </th>
              )}
              {columns.map((col) => (
                <th key={col.key} className={`px-2.5 py-1.5 font-medium ${col.className ?? ""}`}>
                  {col.sortable && onSortChange ? (
                    <button
                      className="flex items-center gap-1 hover:text-ink-800"
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
          <tbody className="block md:table-row-group">
            {rows.length === 0 && (
              <tr className="block md:table-row">
                <td
                  colSpan={columns.length + (selectable ? 1 : 0)}
                  className="block px-3 py-8 text-center text-ink-400 md:table-cell"
                >
                  {emptyMessage}
                </td>
              </tr>
            )}
            {rows.map((row) => {
              const id = getRowId(row);
              return (
                <tr
                  key={id}
                  className="mb-2 block rounded-md border border-ink-200 last:mb-0 hover:bg-ink-200 md:mb-0 md:table-row md:rounded-none md:border-0 md:border-b md:last:border-0"
                >
                  {selectable && (
                    <td
                      data-label=""
                      className="flex items-center justify-between gap-3 px-2.5 py-1.5 before:font-medium before:uppercase before:tracking-wide before:text-ink-500 before:content-[attr(data-label)] md:table-cell md:before:content-none"
                    >
                      <input
                        type="checkbox"
                        checked={selectedIds?.has(id) ?? false}
                        onChange={() => toggleRow(id)}
                        aria-label={`Select row ${id}`}
                      />
                    </td>
                  )}
                  {columns.map((col) => (
                    <td
                      key={col.key}
                      data-label={col.header}
                      className={`px-2.5 py-1.5 before:text-xs before:font-medium before:uppercase before:tracking-wide before:text-ink-500 before:content-[attr(data-label)] md:table-cell md:before:content-none ${
                        col.stackOnMobile
                          ? "flex flex-col gap-1"
                          : "flex items-start justify-between gap-3 before:shrink-0 before:pt-0.5"
                      } ${col.className ?? ""}`}
                    >
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
