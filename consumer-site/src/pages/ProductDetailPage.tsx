import { useEffect, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { ApiError, getProduct, getSimilarProducts } from "../api/client";
import type { ProductDetail, SimilarProduct } from "../api/types";
import { useCompare } from "../context/CompareContext";

// Al's ask: "a page to view the bowling ball details with sections for
// the high level details and summary of summary and then easy ways to
// dive into each video and play them in an embeded player". Four
// sections in reading order: high-level details, summary-of-summaries,
// videos (embedded), and -- for a retired ball -- suggested current
// balls that compare closest (the other half of Al's ask, "suggest
// current balls that best compare to the retired balls").
export default function ProductDetailPage() {
  const { id } = useParams<{ id: string }>();
  const location = useLocation();
  // Real incident, Al: "if i have filtered the consumer products page and
  // click on a product then go back my context is lost and it is
  // confusing". ProductCard (and this page's own "Similar current balls"
  // cards below) now pass `state: {from: "<path+query the visitor came
  // from>"}` on every Link into a product page -- read it back here for
  // "Back to Browse"'s own target so it returns to the exact filtered/
  // sorted Browse view (or Compare/Plotter page) the visitor actually
  // came from, not a bare "/" that silently drops ?status=/brand_id=/q=/
  // sort=. Falls back to "/" when there's no state at all -- a shared
  // direct link to /balls/<id>, or a fresh tab, has no "came from"
  // anywhere to return to.
  const backTo = (location.state as { from?: string } | null)?.from || "/";
  const [product, setProduct] = useState<ProductDetail | null>(null);
  const [similar, setSimilar] = useState<SimilarProduct[]>([]);
  const [notFound, setNotFound] = useState(false);
  const [loading, setLoading] = useState(true);
  // Which image is shown in the header -- null means "use the default"
  // (product.primary_image_url, which the API now derives from the
  // admin-curated is_thumbnail flag on product_images, not the raw,
  // can-go-stale products.primary_image_url column). Clicking a photo
  // in the "Additional images" strip below overrides this so a visitor
  // can browse the full-size gallery without leaving the page.
  const [selectedImageUrl, setSelectedImageUrl] = useState<string | null>(null);
  const { ids, toggle, isFull } = useCompare();

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    setNotFound(false);
    setProduct(null);
    setSelectedImageUrl(null);
    getProduct(id)
      .then(setProduct)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 404) setNotFound(true);
      })
      .finally(() => setLoading(false));
  }, [id]);

  // Similar-balls section only matters for a retired product (Al's
  // exact framing: "a way to still view retired balls and suggest
  // current balls that best compare to the retired balls") -- fetched
  // separately from the main product so a slow similarity computation
  // never blocks the rest of the page from rendering.
  useEffect(() => {
    if (!id || !product || product.status !== "retired") {
      setSimilar([]);
      return;
    }
    getSimilarProducts(id, 5)
      .then(setSimilar)
      .catch(() => setSimilar([]));
  }, [id, product]);

  if (loading) return <div className="page">Loading...</div>;

  if (notFound || !product) {
    return (
      <div className="page">
        <p>That ball couldn't be found.</p>
        <Link to={backTo}>Back to Browse</Link>
      </div>
    );
  }

  const inCompare = ids.includes(product.id);
  const heroImageUrl = selectedImageUrl ?? product.primary_image_url ?? null;
  // Every visible image, including whichever one is currently shown as
  // the hero -- Al: "have all the thumbnails there and just move one
  // to the large box and not swap them it makes it confusing". The
  // active thumbnail stays in the rail (highlighted, not removed) so
  // clicking it again or clicking a different one is a plain toggle,
  // not a shrinking/reordering list. product.images is already
  // visibility-filtered and display_order-sorted server-side (see
  // public_api.get_product).
  const galleryImages = product.images;

  return (
    <div className="page product-detail-page">
      <Link to={backTo} className="back-link">
        &larr; Back to Browse
      </Link>

      <div className="product-detail-header">
        <div className="product-detail-media">
          {heroImageUrl ? (
            <img src={heroImageUrl} alt={product.name} />
          ) : (
            <div className="product-card-media-placeholder" aria-hidden="true" />
          )}
        </div>

        {/* Additional-images rail, to the right of the hero -- the
            hero's box height (see .product-detail-media in index.css)
            is now sized to match this rail's height plus the hero's
            own original height combined, since the two used to be
            stacked as separate rows and are now side by side. */}
        {galleryImages.length > 1 && (
          <div className="product-detail-gallery">
            {galleryImages.map((img) => (
              <button
                key={img.id}
                type="button"
                className={
                  img.stored_url === heroImageUrl
                    ? "product-detail-gallery-thumb active"
                    : "product-detail-gallery-thumb"
                }
                onClick={() => setSelectedImageUrl(img.stored_url)}
                aria-label={`Show ${img.image_type.replace(/_/g, " ")} image`}
                aria-pressed={img.stored_url === heroImageUrl}
              >
                <img src={img.stored_url} alt={`${product.name} -- ${img.image_type.replace(/_/g, " ")}`} loading="lazy" />
              </button>
            ))}
          </div>
        )}

        <div className="product-detail-heading">
          <div className="product-card-brand">{product.brand_name}</div>
          <h1>{product.name}</h1>
          {product.status === "retired" && <span className="badge badge-retired">Retired</span>}
          <button
            type="button"
            className={inCompare ? "btn btn-compare active" : "btn btn-compare"}
            onClick={() => toggle(product.id)}
            disabled={!inCompare && isFull}
          >
            {inCompare ? "Remove from compare" : "Add to compare"}
          </button>
        </div>
      </div>

      {/* High-level details */}
      <section className="detail-section">
        <h2>Details</h2>
        <dl className="spec-list">
          {product.core_name && <SpecRow label="Core" value={`${product.core_name}${product.core_type ? ` (${product.core_type})` : ""}`} />}
          {product.coverstock_full_name && <SpecRow label="Coverstock" value={product.coverstock_full_name} />}
          {product.coverstock_type && <SpecRow label="Coverstock type" value={product.coverstock_type} />}
          {product.coverstock_material && <SpecRow label="Coverstock material" value={product.coverstock_material} />}
          {product.color && <SpecRow label="Color" value={product.color} />}
          {product.factory_finish && <SpecRow label="Factory finish" value={product.factory_finish} />}
          {product.weights_available && <SpecRow label="Weights available" value={product.weights_available} />}
          {product.release_date && <SpecRow label="Release date" value={product.release_date} />}
          {product.has_particle && <SpecRow label="Particle coverstock" value="Yes" />}
        </dl>

        {product.skus.length > 0 && (
          <table className="sku-table">
            <thead>
              <tr>
                <th>Weight</th>
                <th>RG</th>
                <th>Differential</th>
                <th>Mass bias</th>
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
        )}

        {product.description && <p className="product-description">{product.description}</p>}
      </section>

      {/* Summary of summaries */}
      {product.video_reviews_summary && (
        <section className="detail-section">
          <h2>What reviewers are saying</h2>
          <p className="video-summary">{product.video_reviews_summary}</p>
          {product.video_reviews_summary_video_count ? (
            <p className="hint">
              Based on {product.video_reviews_summary_video_count} video review
              {product.video_reviews_summary_video_count === 1 ? "" : "s"}
            </p>
          ) : null}
        </section>
      )}

      {/* Videos, embedded */}
      {product.videos.length > 0 && (
        <section className="detail-section">
          <h2>Video reviews</h2>
          <div className="video-grid">
            {product.videos.map((v) => (
              <div className="video-card" key={v.youtube_video_id}>
                <div className="video-embed">
                  <iframe
                    src={`https://www.youtube.com/embed/${v.youtube_video_id}`}
                    title={v.title}
                    loading="lazy"
                    allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                    allowFullScreen
                  />
                </div>
                <div className="video-card-title">{v.title}</div>
                {v.channel_title && <div className="video-card-channel">{v.channel_title}</div>}
                <p className="video-card-summary">{v.summary}</p>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Retired -> current suggestions */}
      {product.status === "retired" && similar.length > 0 && (
        <section className="detail-section">
          <h2>Similar current balls</h2>
          <div className="product-grid">
            {similar.map((s) => (
              <Link to={`/balls/${s.id}`} state={{ from: backTo }} className="product-card similar-card" key={s.id}>
                <div className="product-card-media">
                  {s.primary_image_url ? (
                    <img src={s.primary_image_url} alt={s.name} loading="lazy" />
                  ) : (
                    <div className="product-card-media-placeholder" aria-hidden="true" />
                  )}
                </div>
                <div className="product-card-body">
                  <div className="product-card-brand">{s.brand_name}</div>
                  <div className="product-card-name">{s.name}</div>
                </div>
              </Link>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

function SpecRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="spec-row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}
