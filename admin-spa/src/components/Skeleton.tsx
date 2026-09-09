// Al: "can we add a skeleton UI to the admin spa" -- same motivation as
// bowlerdepot-learn's ArticleCardSkeleton/ArticleDetailSkeleton (see
// DEPLOY_RUNBOOK.md 6af): a shimmering placeholder that already occupies
// the space real content will take up reduces layout shift while an
// admin_api call is in flight, instead of a page snapping from "Loading…"
// text (or an empty table) to full content all at once.
//
// Single primitive, not a family of shaped components -- every skeleton
// need in this app (a table cell, a KPI number, a card's body, a tab
// strip) is just one or more rectangles of the right size, so callers
// compose `<Skeleton />` with width/height/className rather than this
// file growing a bespoke component per shape. `bg-ink-200` picks up the
// dense-dark theme's own border/surface color one step lighter than
// `bg-ink-100` card backgrounds, so the shimmer reads as "content not in
// yet" rather than a stray dark box.
interface SkeletonProps {
  className?: string;
  // Inline width/height for cases Tailwind's arbitrary-value classes
  // would be more awkward for (a table cell's width varying by column,
  // e.g.) -- prefer className's w-*/h-* utilities when a fixed size is
  // fine.
  width?: string | number;
  height?: string | number;
}

export default function Skeleton({ className = "", width, height }: SkeletonProps) {
  return (
    <div
      className={`animate-pulse rounded bg-ink-200 ${className}`}
      style={{
        width: width !== undefined ? width : undefined,
        height: height !== undefined ? height : undefined,
      }}
    />
  );
}
