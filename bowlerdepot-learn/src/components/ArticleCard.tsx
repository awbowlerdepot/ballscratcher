import { Link } from "react-router-dom";
import type { ArticleCard as ArticleCardType } from "../api/types";

export default function ArticleCard({ article }: { article: ArticleCardType }) {
  return (
    <div className="article-card">
      <Link to={`/articles/${article.product_id}`} className="article-card-media">
        {article.primary_image_url ? (
          <img src={article.primary_image_url} alt={article.product_name} loading="lazy" />
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
