import { Link } from "react-router-dom";
import type { ProductCard as ProductCardType } from "../api/types";
import { useCompare, MAX_COMPARE_IDS } from "../context/CompareContext";

export default function ProductCard({ product }: { product: ProductCardType }) {
  const { ids, toggle, isFull } = useCompare();
  const inCompare = ids.includes(product.id);
  const disabled = !inCompare && isFull;

  return (
    <div className="product-card">
      <Link to={`/balls/${product.id}`} className="product-card-media">
        {product.primary_image_url ? (
          <img src={product.primary_image_url} alt={product.name} loading="lazy" />
        ) : (
          <div className="product-card-media-placeholder" aria-hidden="true" />
        )}
      </Link>
      <div className="product-card-body">
        <div className="product-card-brand">{product.brand_name}</div>
        <Link to={`/balls/${product.id}`} className="product-card-name">
          {product.name}
        </Link>
        <div className="product-card-meta">
          {[product.core_type, product.coverstock_type].filter(Boolean).join(" · ") || " "}
        </div>
        {product.video_reviews_summary_video_count ? (
          <div className="product-card-video-count">
            Based on {product.video_reviews_summary_video_count} video review
            {product.video_reviews_summary_video_count === 1 ? "" : "s"}
          </div>
        ) : null}
        <button
          type="button"
          className={inCompare ? "btn btn-compare active" : "btn btn-compare"}
          onClick={() => toggle(product.id)}
          disabled={disabled}
          title={disabled ? `Compare is full (max ${MAX_COMPARE_IDS})` : undefined}
        >
          {inCompare ? "Remove from compare" : "Add to compare"}
        </button>
      </div>
    </div>
  );
}
