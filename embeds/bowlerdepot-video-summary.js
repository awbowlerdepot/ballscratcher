/**
 * Injects this project's AI-generated "video reviews summary" rollup
 * paragraph into the existing native Videos tab on a BowlerDepot
 * (BigCommerce) product page.
 *
 * Al's ask: "for the public side of things... pull in the video section
 * into the video section of the bigcommerce product page" + "I would
 * love to also pull in the summary that we have." The individual videos
 * themselves are handled separately, server-side (see
 * src/bowlerdepot_video_sync/app.py -- pushes them straight into
 * BigCommerce's own native Product Videos feature, which the theme
 * already renders, no script needed for that part). This script handles
 * ONLY the one piece BigCommerce has no native slot for: the aggregate
 * rollup paragraph across all of a product's approved videos
 * (products.video_reviews_summary), which needs to be inserted by hand.
 *
 * Confirmed live this session (Claude in Chrome, against
 * bowlerdepot.com/brunswick-combat-solid/) on the Supermarket theme:
 *   - Every PDP's add-to-cart form has <input name="product_id"> holding
 *     BigCommerce's own numeric product id (e.g. "4390") -- the same id
 *     bowlerdepot_products.bigcommerce_product_id stores.
 *   - The native Videos tab is <div id="tab-videos"> containing a
 *     <section class="videoGallery ..."> once BigCommerce has at least
 *     one video for the product. If a product has no videos at all,
 *     #tab-videos (and the "Videos" tab itself) doesn't render -- this
 *     script no-ops in that case, same as it does for any other
 *     "nothing to show" case below.
 *
 * Deploy: this file is a static asset uploaded to the same S3 bucket/
 * CloudFront distribution that already serves the Learn site
 * (LearnSiteBucket/LearnSiteDistribution -- this is Learn-review
 * content, so it belongs there rather than on consumer-site's bucket;
 * see DEPLOY_RUNBOOK.md's "BowlerDepot video summary embed" section for
 * the exact aws s3 cp + cloudfront invalidation commands) -- NOT part of
 * any Lambda. BigCommerce loads it via a one-line loader snippet pasted
 * into Storefront > Script Manager, scoped to Product Pages. Updating this
 * file's logic later only needs a re-upload + cache invalidation, never
 * touching Script Manager again.
 *
 * Deliberately vanilla JS, no build step, no dependencies -- this has to
 * run standalone inside BigCommerce's storefront, which has no awareness
 * of this project's own tooling.
 */
(function () {
  "use strict";

  // PublicApiUrl (template.yaml Outputs) -- now api.bowleriq.io (6as).
  // Trailing slash intentionally stripped so the path below can always
  // start with "/".
  var API_BASE_URL = "https://api.bowleriq.io".replace(/\/$/, "");

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function insertSummary(summary) {
    var tabVideos = document.getElementById("tab-videos");
    if (!tabVideos) {
      return; // No native Videos tab on this page -- nothing to attach to.
    }
    // Don't insert twice if this script somehow runs more than once on
    // the same page (e.g. a theme that re-fires DOMContentLoaded-style
    // hooks on a soft navigation).
    if (tabVideos.querySelector(".bd-video-summary")) {
      return;
    }

    var box = document.createElement("div");
    box.className = "bd-video-summary";
    // Minimal inline styling so this renders reasonably even if the
    // theme's own stylesheet has no rule for an unfamiliar class --
    // intentionally light-touch, not trying to fully match Supermarket's
    // design system from inside a storefront script.
    box.style.margin = "0 0 20px";
    box.style.padding = "12px 16px";
    box.style.background = "#f7f7f7";
    box.style.borderLeft = "3px solid #ccc";
    box.style.fontSize = "14px";
    box.style.lineHeight = "1.5";
    box.innerHTML = "<strong>Video Reviews Summary</strong><p style=\"margin:6px 0 0\">" +
      escapeHtml(summary) + "</p>";

    var gallery = tabVideos.querySelector(".videoGallery");
    if (gallery && gallery.parentNode) {
      gallery.parentNode.insertBefore(box, gallery);
    } else {
      tabVideos.insertBefore(box, tabVideos.firstChild);
    }
  }

  function run() {
    var productIdInput = document.querySelector('input[name="product_id"]');
    if (!productIdInput || !productIdInput.value) {
      return; // Not a real product page (or theme markup changed) -- no-op.
    }

    var url = API_BASE_URL + "/bowlerdepot/products/" +
      encodeURIComponent(productIdInput.value) + "/video-summary";

    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (resp) {
        return resp.ok ? resp.json() : null;
      })
      .then(function (data) {
        // No match yet, or no rollup summary generated yet -- both are
        // normal/expected for most of the catalog (see public_api's own
        // get_video_summary_by_bigcommerce_product_id docstring), not
        // errors. Just no-op.
        if (data && data.video_reviews_summary) {
          insertSummary(data.video_reviews_summary);
        }
      })
      .catch(function () {
        // Network hiccup, CORS misconfiguration, etc. -- fail silently.
        // This is an enhancement to the page, never something that
        // should surface an error to a storefront visitor.
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
