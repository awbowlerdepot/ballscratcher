import Skeleton from "./Skeleton";

// Al: "can we add a skeleton UI to the admin spa" -- DashboardPage is the
// app's landing page (see its own top comment), so its "Loading
// dashboard…" text was the very first thing anyone saw after signing in,
// every time, with the whole page snapping into place once GET /admin/
// dashboard resolved. This mirrors that page's real layout section-for-
// section (KPI row, transcript-fetcher card, brand chart, four Top 10
// tables) so the swap-in doesn't shift anything -- same reasoning as
// bowlerdepot-learn's ArticleCardSkeleton/ArticleDetailSkeleton (see
// DEPLOY_RUNBOOK.md 6af). Kept as a single-purpose component (not built
// from generic pieces) since this exact shape only appears here.
export default function DashboardSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="mb-3 text-xl font-semibold text-ink-800">Dashboard</h1>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          {Array.from({ length: 10 }).map((_, i) => (
            <div key={i} className="rounded-lg border border-ink-200 bg-ink-100 p-4 shadow-sm">
              <Skeleton className="mb-2 h-3 w-20" />
              <Skeleton className="h-6 w-12" />
            </div>
          ))}
        </div>
      </div>

      <div className="rounded-lg border border-ink-200 bg-ink-100 p-4 shadow-sm">
        <Skeleton className="mb-3 h-4 w-56" />
        <Skeleton className="h-4 w-full max-w-md" />
      </div>

      <div className="rounded-lg border border-ink-200 bg-ink-100 p-4 shadow-sm">
        <Skeleton className="mb-3 h-4 w-48" />
        <Skeleton className="h-48 w-full" />
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="rounded-lg border border-ink-200 bg-ink-100 p-4 shadow-sm">
            <Skeleton className="mb-3 h-4 w-40" />
            <div className="flex flex-col gap-2">
              {Array.from({ length: 5 }).map((_, j) => (
                <Skeleton key={j} className="h-4 w-full" />
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
