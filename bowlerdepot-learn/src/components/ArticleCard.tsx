import { Link } from "react-router-dom";
import { formatArticleDate, resizedImageUrl } from "../api/client";
import type { ArticleCard as ArticleCardType } from "../api/types";

export default function ArticleCard({ article }: { article: ArticleCardType }) {
  // Prefer the article's own AI-generated product shot (a consistent,
  // premium-looking editorial rendering) over the ball's raw scraped
  // photo -- Al: "can we use the product shot for the card in the list
  // of review articles." Falls back to primary_image_url since image
  // generation is independently-fallible and some articles will only
  // ever have the scraped photo (see get_product_article's/
  // list_articles' own docstrings).
  const rawCardImage = article.product_shot_image_url || article.primary_image_url;
  // 640x480 (4:3, matching the aspect-[4/3] box below) -- 2x-retina-sized
  // for this card's ~300-370px rendered width in the 2/3-col grid
  // (LearnIndexPage.tsx), served via img.bowleriq.io instead of the raw
  // full-resolution scraped/generated source.
  const cardImage = rawCardImage ? resizedImageUrl(rawCardImage, { w: 640, h: 480, fit: "cover" }) : null;
  // Al: "add published dates ... to the articles" -- a short date on the
  // index card, same "first_published_at, falling back to reviewed_at"
  // reasoning ArticleDetailPage.tsx uses (see that page's own comment).
  // No "Updated" on the card -- that distinction matters once a reader's
  // already on the article, not while skimming the index.
  const publishedLabel = formatArticleDate(article.first_published_at ?? article.reviewed_at);
  return (
    <div className="group">
      <Link to={`/articles/${article.product_id}`} className="block overflow-hidden rounded-md bg-paper-border/40">
        {cardImage ? (
          <img
            src={cardImage}
            alt={article.product_name}
            loading="lazy"
            className="aspect-[4/3] w-full object-cover"
          />
        ) : (
          <div
            className="aspect-[4/3] w-full bg-gradient-to-br from-secondary to-accent"
            aria-hidden="true"
          />
        )}
      </Link>
      <div className="mt-3 flex flex-col gap-1">
        <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-widest text-muted">
          <span>{article.brand_name}</span>
          {article.article_type_name ? (
            <>
              <span aria-hidden="true">&middot;</span>
              <span>{article.article_type_name}</span>
            </>
          ) : null}
        </div>
        <Link
          to={`/articles/${article.product_id}`}
          className="font-display text-lg font-semibold leading-snug text-ink no-underline group-hover:text-accent"
        >
          {article.title}
        </Link>
        <p className="text-sm text-muted">{article.hook}</p>
        {article.coverstock_type ? (
          <div className="text-xs text-muted">{article.coverstock_type} coverstock</div>
        ) : null}
        {publishedLabel ? <div className="text-xs text-muted">{publishedLabel}</div> : null}
      </div>
    </div>
  );
}
