// Placeholder shown in LearnIndexPage's grid while listArticles() is in
// flight, sized to match ArticleCard.tsx's real layout (same aspect-[4/3]
// image block + eyebrow/title/hook line heights) so the grid doesn't
// reflow when the real cards pop in.
export default function ArticleCardSkeleton() {
  return (
    <div className="animate-pulse" aria-hidden="true">
      <div className="aspect-[4/3] w-full rounded-md bg-paper-border/60" />
      <div className="mt-3 flex flex-col gap-2">
        <div className="h-3 w-24 rounded bg-paper-border/60" />
        <div className="h-5 w-3/4 rounded bg-paper-border/60" />
        <div className="h-4 w-full rounded bg-paper-border/60" />
        <div className="h-4 w-1/2 rounded bg-paper-border/60" />
      </div>
    </div>
  );
}
