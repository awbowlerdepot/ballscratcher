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

  if (loading) return <p className="py-8 text-center text-muted">Loading...</p>;
  if (notFound) return <p className="py-8 text-center text-muted">That ball isn't in our catalog.</p>;
  if (error) return <p className="py-8 text-center text-alert">{error}</p>;

  const article = data?.article;
  if (!article) return <p className="py-8 text-center text-muted">No review is published for this ball yet.</p>;

  const product = article.product;
  const heroImage = article.action_shot_image_url || product?.primary_image_url;
  // Migration 031 -- "Bowling Balls · Ball Review" eyebrow, read straight
  // off the article row rather than hardcoded (see ArticleCard's own
  // comment on the same fields). Null for a pre-migration article.
  const taxonomyLabel = [article.category_name, article.article_type_name].filter(Boolean).join(" · ");

  return (
    <div>
      <Link to="/" className="mb-6 inline-block text-sm font-medium text-muted hover:text-ink">
        &larr; All reviews
      </Link>

      <div className="relative left-1/2 right-1/2 mb-10 -mx-[50vw] w-screen bg-neutral-900 py-10">
        <div className="mx-auto grid max-w-5xl grid-cols-1 gap-8 px-6 md:grid-cols-[320px_1fr] md:px-8">
          <div className="flex items-center justify-center overflow-visible px-2 md:relative md:px-4">
            <div className="-my-[0.6rem] aspect-[4/3] w-full -rotate-[8deg] rounded-sm bg-white p-2 shadow-[0_12px_24px_-6px_rgba(0,0,0,0.55)] md:absolute md:left-0 md:top-1/2 md:my-0 md:w-[325px] md:-translate-y-1/2">
              {heroImage ? (
                <img
                  src={heroImage}
                  alt={product?.name || article.title}
                  className="h-full w-full rounded-[2px] object-cover"
                />
              ) : null}
            </div>
          </div>
          <div className="flex flex-col justify-center">
            <p className="mb-1 text-[11px] font-semibold uppercase tracking-widest text-white/60">
              {taxonomyLabel || product?.core_type || product?.coverstock_type}
            </p>
            <h1 className="font-display text-xl font-semibold text-white">{article.title}</h1>
            <p className="mt-2 text-base text-white/80">{article.hook}</p>
          </div>
        </div>
      </div>

      {article.performance_summary ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Performance</h2>
          <p className="text-ink/90">{article.performance_summary}</p>
        </div>
      ) : null}

      {article.pros?.length || article.cons?.length ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Pros &amp; Cons</h2>
          <div className="grid grid-cols-1 gap-6 sm:grid-cols-2">
            <div>
              <div className="mb-2 text-sm font-semibold text-green-700">Pros</div>
              <ul className="list-disc space-y-1 pl-5 text-sm text-ink/90">
                {(article.pros || []).map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ul>
            </div>
            <div>
              <div className="mb-2 text-sm font-semibold text-alert">Cons</div>
              <ul className="list-disc space-y-1 pl-5 text-sm text-ink/90">
                {(article.cons || []).map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      ) : null}

      {article.who_should_buy?.length ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Who Should Buy This</h2>
          <ul className="list-disc space-y-1 pl-5 text-sm text-ink/90">
            {article.who_should_buy.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {article.who_should_skip?.length ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Who Should Skip This</h2>
          <ul className="list-disc space-y-1 pl-5 text-sm text-ink/90">
            {article.who_should_skip.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {article.buying_tips ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Buying Tips</h2>
          <p className="text-ink/90">{article.buying_tips}</p>
        </div>
      ) : null}

      {article.verdict ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Verdict</h2>
          <p className="text-ink/90">{article.verdict}</p>
        </div>
      ) : null}

      {product?.skus?.length ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Specs</h2>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr>
                  <th className="border-b border-paper-border px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-muted">
                    Weight
                  </th>
                  <th className="border-b border-paper-border px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-muted">
                    RG
                  </th>
                  <th className="border-b border-paper-border px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-muted">
                    Differential
                  </th>
                  <th className="border-b border-paper-border px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-muted">
                    Mass Bias
                  </th>
                </tr>
              </thead>
              <tbody>
                {product.skus.map((sku) => (
                  <tr key={sku.weight_lbs}>
                    <td className="border-b border-paper-border px-3 py-2">{sku.weight_lbs} lb</td>
                    <td className="border-b border-paper-border px-3 py-2">{sku.rg ?? "--"}</td>
                    <td className="border-b border-paper-border px-3 py-2">{sku.differential ?? "--"}</td>
                    <td className="border-b border-paper-border px-3 py-2">{sku.mass_bias ?? "--"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      {article.comparison_table?.length ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Similar Balls</h2>
          <div className="grid grid-cols-1 gap-x-8 gap-y-10 sm:grid-cols-2 lg:grid-cols-3">
            {article.comparison_table.map((c) => {
              const price = formatPrice(c.ecommerce_price, c.ecommerce_price_currency);
              return (
                <a
                  key={c.id}
                  className="group block"
                  href={c.ecommerce_url || bowlerDepotSearchUrl(c.name)}
                  target="_blank"
                  rel="noreferrer"
                >
                  <div className="aspect-[4/3] w-full overflow-hidden rounded-md bg-paper-border/40">
                    {c.primary_image_url ? (
                      <img
                        src={c.primary_image_url}
                        alt={c.name}
                        loading="lazy"
                        className="h-full w-full object-cover"
                      />
                    ) : null}
                  </div>
                  <div className="mt-3">
                    <div className="font-display font-semibold text-ink group-hover:text-accent">{c.name}</div>
                    <div className="text-xs text-muted">
                      {[c.core_name, c.coverstock_name].filter(Boolean).join(" · ")}
                    </div>
                    {price ? (
                      <div className="mt-1 text-sm font-semibold text-accent">
                        {price}
                        {c.ecommerce_in_stock === false ? (
                          <span className="font-normal text-alert"> &middot; Out of stock</span>
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
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">FAQ</h2>
          {article.faq.map((item, i) => (
            <div className="border-t border-paper-border py-4" key={i}>
              <p className="mb-1 font-medium text-ink">{item.question}</p>
              <p className="text-muted">{item.answer}</p>
            </div>
          ))}
        </div>
      ) : null}

      {article.related_reviews?.length ? (
        <div className="mb-10">
          <h2 className="mb-3 font-display text-xl font-semibold text-ink">Related Reviews</h2>
          <div className="grid grid-cols-1 gap-x-8 gap-y-10 sm:grid-cols-2 lg:grid-cols-3">
            {article.related_reviews.map((r) => (
              <Link key={r.product_id} className="group block" to={`/articles/${r.product_id}`}>
                <div className="aspect-[4/3] w-full overflow-hidden rounded-md bg-paper-border/40">
                  {r.primary_image_url ? (
                    <img
                      src={r.primary_image_url}
                      alt={r.product_name}
                      loading="lazy"
                      className="h-full w-full object-cover"
                    />
                  ) : null}
                </div>
                <div className="mt-3">
                  <div className="font-display font-semibold text-ink group-hover:text-accent">{r.title}</div>
                  <div className="text-xs text-muted">{r.product_name}</div>
                </div>
              </Link>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
