import Button from "./Button";

interface PaginationProps {
  offset: number;
  limit: number;
  // Count of items actually returned in the current page -- still used
  // as the "has a next page" fallback (and for the "Showing X-Y" range)
  // when `total` isn't available.
  itemCount: number;
  onOffsetChange: (offset: number) => void;
  // Total matching row count across every page, when the caller's API
  // actually returns one (e.g. admin_api's GET /products, since the
  // count_products fix). Optional and backward-compatible on purpose --
  // every other page using this component (Review Queue, Video
  // Candidates, Articles, Price Sites, Cores, Coverstocks) still hits an
  // endpoint that doesn't return a total, so they keep getting exactly
  // the old behavior: "has a next page" inferred from whether the
  // current page came back full, no page-number row, no First/Last
  // buttons. Only a caller that passes a real `total` gets those.
  total?: number;
}

// How many page-number buttons to show on each side of the current page
// before collapsing the rest into a "…" -- Al, second round: "have the
// actual page numbers on the right where previous and next are.... also
// first and last page options when applicable." 1 sibling on each side,
// plus page 1 and the last page always pinned, is the standard pattern
// (same shape GitHub/Google's own pagination uses) -- keeps the control
// a small, fixed width even against a catalog with hundreds of pages,
// rather than rendering one button per page.
const SIBLING_COUNT = 1;

function buildPageWindow(current: number, totalPages: number): (number | "…")[] {
  const first = 1;
  const last = totalPages;
  const start = Math.max(first + 1, current - SIBLING_COUNT);
  const end = Math.min(last - 1, current + SIBLING_COUNT);

  const pages: (number | "…")[] = [first];
  if (start > first + 1) pages.push("…");
  for (let p = start; p <= end; p++) pages.push(p);
  if (end < last - 1) pages.push("…");
  if (last > first) pages.push(last);
  return pages;
}

export default function Pagination({ offset, limit, itemCount, onOffsetChange, total }: PaginationProps) {
  const hasTotal = total != null;
  const hasPrev = offset > 0;
  // With a real total: whether another full page exists past this one.
  // Without one: the old guess (a full page might mean more rows exist,
  // a short page means we've hit the end) -- still the best available
  // signal for every Pagination caller that doesn't have a total.
  const hasNext = hasTotal ? offset + limit < total : itemCount === limit;
  const page = Math.floor(offset / limit) + 1;
  const totalPages = hasTotal ? Math.max(1, Math.ceil(total / limit)) : null;
  const lastOffset = totalPages != null ? Math.max(0, (totalPages - 1) * limit) : 0;
  // Only worth rendering a page-number row once there's more than one
  // page to jump between -- a single-page result already shows
  // everything, First/Last/numbers would all be no-ops.
  const pageWindow = totalPages != null && totalPages > 1 ? buildPageWindow(page, totalPages) : null;

  return (
    <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-sm text-ink-500">
      <span>
        Showing {itemCount === 0 ? 0 : offset + 1}–{offset + itemCount}
        {hasTotal ? ` of ${total}` : hasNext ? "+" : ` (page ${page})`}
      </span>
      <div className="flex items-center gap-1">
        {hasTotal && (
          <Button size="sm" variant="ghost" disabled={!hasPrev} onClick={() => onOffsetChange(0)}>
            First
          </Button>
        )}
        <Button size="sm" variant="ghost" disabled={!hasPrev} onClick={() => onOffsetChange(Math.max(0, offset - limit))}>
          Previous
        </Button>
        {pageWindow?.map((p, i) =>
          p === "…" ? (
            <span key={`ellipsis-${i}`} className="px-1 text-ink-400">
              …
            </span>
          ) : (
            <Button
              key={p}
              size="sm"
              variant={p === page ? "primary" : "ghost"}
              aria-current={p === page ? "page" : undefined}
              onClick={() => onOffsetChange((p - 1) * limit)}
            >
              {p}
            </Button>
          ),
        )}
        <Button size="sm" variant="ghost" disabled={!hasNext} onClick={() => onOffsetChange(offset + limit)}>
          Next
        </Button>
        {hasTotal && (
          <Button size="sm" variant="ghost" disabled={!hasNext} onClick={() => onOffsetChange(lastOffset)}>
            Last
          </Button>
        )}
      </div>
    </div>
  );
}
