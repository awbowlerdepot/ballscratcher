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
  // count_products fix below). Optional and backward-compatible on
  // purpose -- every other page using this component (Review Queue,
  // Video Candidates, Articles, Price Sites, Cores, Coverstocks) still
  // hits an endpoint that doesn't return a total, so they keep getting
  // exactly the old behavior: "has a next page" inferred from whether
  // the current page came back full, no page-count display, no First/
  // Last buttons. Only a caller that passes a real `total` gets those --
  // Al, reporting the Products tab specifically: "it doesn't have number
  // of pages and first and last buttons."
  total?: number;
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

  return (
    <div className="mt-3 flex items-center justify-between text-sm text-ink-500">
      <span>
        Showing {itemCount === 0 ? 0 : offset + 1}–{offset + itemCount}
        {hasTotal ? ` of ${total}` : hasNext ? "+" : ""}
        {totalPages != null ? ` (page ${page} of ${totalPages})` : ` (page ${page})`}
      </span>
      <div className="flex gap-2">
        {hasTotal && (
          <Button size="sm" disabled={!hasPrev} onClick={() => onOffsetChange(0)}>
            First
          </Button>
        )}
        <Button size="sm" disabled={!hasPrev} onClick={() => onOffsetChange(Math.max(0, offset - limit))}>
          Previous
        </Button>
        <Button size="sm" disabled={!hasNext} onClick={() => onOffsetChange(offset + limit)}>
          Next
        </Button>
        {hasTotal && (
          <Button size="sm" disabled={!hasNext} onClick={() => onOffsetChange(lastOffset)}>
            Last
          </Button>
        )}
      </div>
    </div>
  );
}
