import Skeleton from "./Skeleton";

// Al: "can we add a skeleton UI to the admin spa" -- mirrors
// ProductDetailPage's real header (back link + title + status badge)
// and its six-tab strip so the loading state doesn't collapse the page
// down to one line of text and then jump back out once `load()`
// resolves (see that page's own `if (loading && !product)` guard this
// replaces). The tab strip in particular used to disappear entirely
// while loading, which was disorienting on a page reached by clicking
// a specific tab's icon (ProductsPage's Article/Video status icons
// link straight to `?tab=article`/`?tab=videos`) -- rendering it as
// six inert bars keeps that structure visible immediately.
export default function ProductDetailSkeleton() {
  return (
    <div className="flex flex-col gap-4">
      <div>
        <Skeleton className="mb-2 h-4 w-32" />
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <Skeleton className="h-6 w-64" />
          <Skeleton className="h-5 w-16 rounded-full" />
        </div>
      </div>

      <div className="flex flex-wrap gap-1 border-b border-ink-200 pb-1">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} className="h-8 w-24 rounded-t" />
        ))}
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="aspect-square w-full" />
        ))}
      </div>

      <div className="flex flex-col gap-2">
        <Skeleton className="h-4 w-full max-w-md" />
        <Skeleton className="h-4 w-full max-w-sm" />
        <Skeleton className="h-4 w-full max-w-lg" />
        <Skeleton className="h-4 w-full max-w-xs" />
      </div>
    </div>
  );
}
