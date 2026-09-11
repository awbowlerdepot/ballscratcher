/**
 * Adds a new "Review" tab to a live BowlerDepot (BigCommerce) product page,
 * containing this project's AI-generated article hero (image + title +
 * hook) plus a link to the full review on learn.bowlerdepot.com.
 *
 * Al's ask: "what is the best way to embed the heros on the
 * bowlerdepot.com product pages for these balls" -- scoped to ONLY the
 * article hero (image/title/hook), explicitly NOT the separate
 * featured_video/YouTube content ("we are talking about articles not
 * youtube videos"). That's a different embed entirely (see
 * bowlerdepot-video-summary.js, which as of this writing hasn't been
 * activated in Script Manager either).
 *
 * Why a new tab, not an insert into an existing one (unlike
 * bowlerdepot-video-summary.js, which inserts into the native Videos
 * tab): there's no existing "Review"/"Article" tab on a stock Supermarket
 * PDP to attach to. Confirmed live this session (Claude in Chrome, DOM
 * manipulation against bowlerdepot.com/brunswick-combat-solid/) that the
 * theme's tab-switching JS uses real event delegation on the parent
 * <ul class="tabs" data-tab=""> container, NOT per-element binding at
 * page-init time -- a brand-new <li>/tab-content pair appended to the DOM
 * after load and then clicked becomes correctly active
 * (probeLiIsActive: true, probeContentIsActive: true,
 * probeContentDisplay: "block"). That's real evidence this approach
 * works, not a guess.
 *
 * Same product_id lookup + always-200 contract as
 * bowlerdepot-video-summary.js -- see that file's own header comment and
 * public_api's service.get_article_hero_by_bigcommerce_product_id
 * docstring.
 *
 * Deploy: static asset on the same S3 bucket/CloudFront distribution as
 * bowlerdepot-video-summary.js (see DEPLOY_RUNBOOK.md's "BowlerDepot
 * article hero embed" section for the exact aws s3 cp + cloudfront
 * invalidation commands + Script Manager setup) -- NOT part of any
 * Lambda. This is a SEPARATE Script Manager entry from the video-summary
 * script; the two are independent and either can be enabled without the
 * other.
 *
 * Deliberately vanilla JS, no build step, no dependencies -- runs
 * standalone inside BigCommerce's storefront.
 */
(function () {
  "use strict";

  // Replace with this deployment's real PublicApiUrl (template.yaml
  // Outputs -- see DEPLOY_RUNBOOK.md) before uploading. Trailing slash
  // intentionally stripped so the path below can always start with "/".
  var API_BASE_URL = "https://REPLACE_WITH_PUBLIC_API_URL".replace(/\/$/, "");

  // Same on-demand resizer this project's Learn site uses
  // (bowlerdepot-learn/src/api/client.ts's resizedImageUrl) -- reimplemented
  // here in vanilla JS since this script can't import from that project.
  // Returns the raw URL unchanged if it isn't a recognizable ImageBucket
  // URL, same fallback behavior as the TS original.
  function resizedImageUrl(rawUrl, opts) {
    var key;
    try {
      key = new URL(rawUrl).pathname.replace(/^\/+/, "");
    } catch (e) {
      return rawUrl;
    }
    if (key.indexOf("product-images/") !== 0 && key.indexOf("article-images/") !== 0) {
      return rawUrl;
    }
    var url = new URL("https://img.bowleriq.io/" + key);
    if (opts.w) url.searchParams.set("w", String(opts.w));
    if (opts.h) url.searchParams.set("h", String(opts.h));
    if (opts.fit) url.searchParams.set("fit", opts.fit);
    if (opts.fmt) url.searchParams.set("fmt", opts.fmt);
    if (opts.q) url.searchParams.set("q", String(opts.q));
    return url.toString();
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function insertReviewTab(data) {
    var tabsList = document.querySelector("ul.tabs[data-tab]");
    if (!tabsList) {
      return; // Theme markup doesn't match what was verified live -- no-op.
    }
    // Don't insert twice (e.g. a theme that re-fires DOMContentLoaded-style
    // hooks on a soft navigation).
    if (document.getElementById("tab-review")) {
      return;
    }

    var tabLi = document.createElement("li");
    tabLi.className = "tab tab--review";
    var tabLink = document.createElement("a");
    tabLink.className = "tab-title";
    tabLink.href = "#tab-review";
    tabLink.textContent = "Review";
    tabLi.appendChild(tabLink);
    tabsList.appendChild(tabLi);

    var content = document.createElement("div");
    content.className = "tab-content";
    content.id = "tab-review";

    var html = "";
    if (data.hero_image_url) {
      var imgUrl = resizedImageUrl(data.hero_image_url, { w: 800, fit: "inside", fmt: "webp", q: 80 });
      html += '<img src="' + escapeHtml(imgUrl) + '" alt="' +
        escapeHtml(data.title || "Ball review") +
        '" style="max-width:100%;height:auto;display:block;margin:0 0 16px;">';
    }
    if (data.title) {
      html += "<h3>" + escapeHtml(data.title) + "</h3>";
    }
    if (data.hook) {
      html += '<p style="line-height:1.6;">' + escapeHtml(data.hook) + "</p>";
    }
    if (data.learn_url) {
      html += '<p><a href="' + escapeHtml(data.learn_url) +
        '" target="_blank" rel="noopener">Read the full review &rarr;</a></p>';
    }
    content.innerHTML = html;

    var tabsContainer = tabsList.parentNode;
    tabsContainer.appendChild(content);
  }

  function run() {
    var productIdInput = document.querySelector('input[name="product_id"]');
    if (!productIdInput || !productIdInput.value) {
      return; // Not a real product page (or theme markup changed) -- no-op.
    }

    var url = API_BASE_URL + "/bowlerdepot/products/" +
      encodeURIComponent(productIdInput.value) + "/article-hero";

    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (resp) {
        return resp.ok ? resp.json() : null;
      })
      .then(function (data) {
        // No match yet, or no approved article yet -- both normal/expected
        // for most of the catalog (see public_api's own
        // get_article_hero_by_bigcommerce_product_id docstring), not
        // errors. Just no-op.
        if (data && data.has_article) {
          insertReviewTab(data);
        }
      })
      .catch(function () {
        // Network hiccup, CORS misconfiguration, etc. -- fail silently.
        // This is an enhancement to the page, never something that should
        // surface an error to a storefront visitor.
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
