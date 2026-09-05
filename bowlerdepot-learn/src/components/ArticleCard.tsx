import { Link } from "react-router-dom";
import type { ArticleCard as ArticleCardType } from "../api/types";

export default function ArticleCard({ article }: { article: ArticleCardType }) {
  // Prefer the article's own AI-generated product shot (a consistent,
  // premium-looking editorial rendering) over the ball's raw scraped
  // photo -- Al: "can we use the product shot for the card in the list
  // of review articles." Falls back to primary_image_url since image
  // generation is independently-fallible and some articles will only
  // ever have the scraped photo (see get_product_article's/
  // list_articles' own docstrings).
  const cardImage = article.product_shot_image_url || article.primary_image_url;
  return (
    <div className="article-card">
      <Link to={`/articles/${article.product_id}`} className="article-card-media">
        {cardImage ? (
          <img src={cardImage} alt={article.product_name} loading="lazy" />
        ) : (
          <div className="article-card-media-placeholder" aria-hidden="true" />
        )}
      </Link>
      <div className="article-card-body">
        <div className="article-card-brand">{article.brand_name}</div>
        <Link to={`/articles/${article.product_id}`} className="article-card-title">
          {article.title}
        </Link>
        <p className="article-card-hook">{article.hook}</p>
        {article.coverstock_type ? (
          <div className="article-card-meta">{article.coverstock_type} coverstock</div>
        ) : null}
      </div>
    </div>
  );
}
