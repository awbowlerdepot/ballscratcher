// Placeholder for ArticleDetailPage.tsx while getProductArticle() is in
// flight, shaped like the real hero block + a few text sections so the
// page doesn't jump in height once the article loads (same reasoning as
// ArticleCardSkeleton.tsx on the index page).
export default function ArticleDetailSkeleton() {
  return (
    <div className="animate-pulse" aria-hidden="true">
      <div className="mb-6 h-4 w-24 rounded bg-paper-border/60" />

      <div className="relative left-1/2 right-1/2 mb-10 -mx-[50vw] w-screen bg-neutral-900 py-10">
        <div className="mx-auto grid max-w-5xl grid-cols-1 gap-8 px-6 md:grid-cols-[320px_1fr] md:px-8">
          <div className="flex items-center justify-center overflow-visible px-2 md:relative md:px-4">
            <div className="mt-14 mb-0 aspect-[4/3] w-full rounded-sm bg-white/10 md:absolute md:left-0 md:top-1/2 md:my-0 md:w-[325px] md:-translate-y-1/2" />
          </div>
          <div className="flex flex-col justify-center gap-3">
            <div className="h-3 w-32 rounded bg-white/15" />
            <div className="h-6 w-3/4 rounded bg-white/15" />
            <div className="h-4 w-full rounded bg-white/10" />
            <div className="h-4 w-2/3 rounded bg-white/10" />
          </div>
        </div>
      </div>

      {Array.from({ length: 3 }).map((_, i) => (
        <div className="mb-10" key={i}>
          <div className="mb-3 h-6 w-40 rounded bg-paper-border/60" />
          <div className="flex flex-col gap-2">
            <div className="h-4 w-full rounded bg-paper-border/60" />
            <div className="h-4 w-full rounded bg-paper-border/60" />
            <div className="h-4 w-2/3 rounded bg-paper-border/60" />
          </div>
        </div>
      ))}
    </div>
  );
}
