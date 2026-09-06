import Button from "./Button";

interface PaginationProps {
  offset: number;
  limit: number;
  // Count of items actually returned in the current page -- admin_api's
  // list_products doesn't return a total count, so "has a next page" is
  // inferred the usual limit/offset way: a full page might mean more
  // rows exist, a short page means we've hit the end.
  itemCount: number;
  onOffsetChange: (offset: number) => void;
}

export default function Pagination({ offset, limit, itemCount, onOffsetChange }: PaginationProps) {
  const hasPrev = offset > 0;
  const hasNext = itemCount === limit;
  const page = Math.floor(offset / limit) + 1;

  return (
    <div className="mt-3 flex items-center justify-between text-sm text-ink-500">
      <span>
        Showing {itemCount === 0 ? 0 : offset + 1}–{offset + itemCount}
        {hasNext ? "+" : ""} (page {page})
      </span>
      <div className="flex gap-2">
        <Button size="sm" disabled={!hasPrev} onClick={() => onOffsetChange(Math.max(0, offset - limit))}>
          Previous
        </Button>
        <Button size="sm" disabled={!hasNext} onClick={() => onOffsetChange(offset + limit)}>
          Next
        </Button>
      </div>
    </div>
  );
}
