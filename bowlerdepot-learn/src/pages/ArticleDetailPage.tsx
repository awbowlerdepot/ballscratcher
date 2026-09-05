import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, bowlerDepotSearchUrl, getProductArticle } from "../api/client";
import type { ProductArticleResponse } from "../api/types";

// Real BowlerDepot price for a Similar Balls card (Al: "include links
// and pricing for it using the bowlerdepot.com pricing data") -- null
// in, null out (no "$0.00"/"Call for price" placeholder); a sibling
// price_checker hasn't priced yet just shows no price, same as every
// other null-means-omit convention this page already follows.
function formatPrice(price: number | null | undefined, currency: string | null | undefined): string | null {
  if (price == null) return null;
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency: currency || "USD" }).format(price);
  } catch {
    return `$${price.toFixed(2)}`;
  }
}

export default function ArticleDetailPage() {
  const { productId } = useParams<{ productId: string }>();
  const [data, setData] = useState<ProductArticleResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!productId) return;
    setLoading(true);
    setNotFound(false);
    setError(null);
    getProductArticle(productId)
      .then(setData)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 404) {
          setNotFound(true);
        } else {
          setError("Couldn't load this review right now -- try again in a moment.");
        }
      })
      .finally(() => setLoading(false));
  }, [productId]);

  if (loading) return <p className="empty-state">Loading...</p>;
  if (notFound) return <p className="empty-state">That ball isn't in our catalog.</p>;
  if (error) return <p className="error-message">{error}</p>;

  const article = data?.article;
  if (!article) return <p className="empty-state">No review is published for this ball yet.</p>;

  const product = article.product;
  const heroImage = article.action_shot_image_url || product?.primary_image_url;
  // Al: "would it be possible to link to the ecommerce product page for
  // some balls inline too" -- ecommerce_url (from public_api, resolved
  // out of price_checker's own BigCommerce price-tracking data) is a
  // REAL storefront product page when price_checker has matched and
  // approved a BowlerDepot source for this product; otherwise fall back
  // to the search-results link every ball has always had.
  const shopUrl = product?.ecommerce_url || (product ? bowlerDepotSearchUrl(product.name) : null);

  return (
    <div className="page">
      <Link to="/" className="back-link">
        &larr; All reviews
      </Link>

      <div className="article-detail-hero">
        <div className="article-detail-image">
          {heroImage ? <img src={heroImage} alt={product?.name || article.title} /> : null}
        </div>
        <div>
          {product ? <div className="article-detail-eyebrow">{product.core_type || product.coverstock_type}</div> : null}
          <h1>{article.title}</h1>
          <p className="article-detail-hook">{article.hook}</p>
          <div className="article-detail-actions">
            {shopUrl ? (
              <a className="btn btn-primary" href={shopUrl} target="_blank" rel="noreferrer">
                Shop this ball at BowlerDepot
              </a>
            ) : null}
          </div>
        </div>
      </div>

      {article.performance_summary ? (
        <div className="article-section">
          <h2>Performance</h2>
          <p>{article.performance_summary}</p>
        </div>
      ) : null}

      {(article.pros?.length || article.cons?.length) ? (
        <div className="article-section">
          <h2>Pros &amp; Cons</h2>
          <div className="pros-cons-grid">
            <div>
              <div className="pros-title">Pros</div>
              <ul>
                {(article.pros || []).map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ul>
            </div>
            <div>
              <div className="cons-title">Cons</div>
              <ul>
                {(article.cons || []).map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      ) : null}

      {article.who_should_buy?.length ? (
        <div className="article-section">
          <h2>Who Should Buy This</h2>
          <ul>
            {article.who_should_buy.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {article.who_should_skip?.length ? (
        <div className="article-section">
          <h2>Who Should Skip This</h2>
          <ul>
            {article.who_should_skip.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {article.buying_tips ? (
        <div className="article-section">
          <h2>Buying Tips</h2>
          <p>{article.buying_tips}</p>
        </div>
      ) : null}

      {article.verdict ? (
        <div className="article-section">
          <h2>Verdict</h2>
          <p>{article.verdict}</p>
        </div>
      ) : null}

      {product?.skus?.length ? (
        <div className="article-section">
          <h2>Specs</h2>
          <div className="comparison-table-wrap">
            <table className="comparison-table">
              <thead>
                <tr>
                  <th>Weight</th>
                  <th>RG</th>
                  <th>Differential</th>
                  <th>Mass Bias</th>
                </tr>
              </thead>
              <tbody>
                {product.skus.map((sku) => (
                  <tr key={sku.weight_lbs}>
                    <td>{sku.weight_lbs} lb</td>
                    <td>{sku.rg ?? "--"}</td>
                    <td>{sku.differential ?? "--"}</td>
                    <td>{sku.mass_bias ?? "--"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {article.comparison_table?.length ? (
        <div className="article-section">
          <h2>Similar Balls</h2>
          <div className="article-grid">
            {article.comparison_table.map((c) => {
              const price = formatPrice(c.ecommerce_price, c.ecommerce_price_currency);
              return (
                <a
                  key={c.id}
                  className="article-card"
                  href={c.ecommerce_url || bowlerDepotSearchUrl(c.name)}
                  target="_blank"
                  rel="noreferrer"
                >
                  <div className="article-card-media">
                    {c.primary_image_url ? (
                      <img src={c.primary_image_url} alt={c.name} loading="lazy" />
                    ) : (
                      <div className="article-card-media-placeholder" aria-hidden="true" />
                    )}
                  </div>
                  <div className="article-card-body">
                    <div className="article-card-title">{c.name}</div>
                    <div className="article-card-meta">
                      {[c.core_name, c.coverstock_name].filter(Boolean).join(" · ")}
                    </div>
                    {price ? (
                      <div className="article-card-price">
                        {price}
                        {c.ecommerce_in_stock === false ? (
                          <span className="article-card-stock-badge"> · Out of stock</span>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                </a>
              );
            })}
          </div>
        </div>
      ) : null}

      {article.faq?.length ? (
        <div className="article-section">
          <h2>FAQ</h2>
          {article.faq.map((item, i) => (
            <div className="faq-item" key={i}>
              <p className="faq-question">{item.question}</p>
              <p className="faq-answer">{item.answer}</p>
            </div>
          ))}
        </div>
      ) : null}

      {article.related_reviews?.length ? (
        <div className="article-section">
          <h2>Related Reviews</h2>
          <div className="article-grid">
            {article.related_reviews.map((r) => (
              <Link key={r.product_id} className="article-card" to={`/articles/${r.product_id}`}>
                <div className="article-card-media">
                  {r.primary_image_url ? (
                    <img src={r.primary_image_url} alt={r.product_name} loading="lazy" />
                  ) : (
                    <div className="article-card-media-placeholder" aria-hidden="true" />
                  )}
                </div>
                <div className="article-card-body">
                  <div className="article-card-title">{r.title}</div>
                  <div className="article-card-meta">{r.product_name}</div>
                </div>
              </Link>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
