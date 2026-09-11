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

  // PublicApiUrl (template.yaml Outputs) -- now api.bowleriq.io (6as).
  // Trailing slash intentionally stripped so the path below can always
  // start with "/".
  var API_BASE_URL = "https://api.bowleriq.io".replace(/\/$/, "");

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

  // Matches the Learn site's own "Editorial Magazine" hero treatment
  // (bowlerdepot-learn/src/pages/ArticleDetailPage.tsx's hero section +
  // tailwind.config.js's color tokens/fonts) as closely as reasonably
  // possible from inside a vanilla-JS, no-build-step, inline-CSS embed --
  // Al: "can we make the article hero styling look more like the learn
  // site." BigCommerce's storefront doesn't load Cabin/Space Grotesk by
  // default, so this injects the same Google Fonts stylesheet the Learn
  // site's own src/index.css imports. Scoped to a one-time injection (id
  // check) since insertReviewTab can theoretically run more than once.
  function injectStyles() {
    if (document.getElementById("bd-review-fonts")) {
      return;
    }

    var fontLink = document.createElement("link");
    fontLink.id = "bd-review-fonts";
    fontLink.rel = "stylesheet";
    fontLink.href = "https://fonts.googleapis.com/css2?family=Cabin:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap";
    document.head.appendChild(fontLink);

    var style = document.createElement("style");
    style.id = "bd-review-styles";
    // Color tokens straight from bowlerdepot-learn/tailwind.config.js's
    // "Editorial Magazine" theme: ink (hero background), accent (CTA),
    // paper/paper-border (body panel below the hero).
    // Al: "styling is perfect on desktop now, not great on mobile yet"
    // (screenshot showed the tilted photo card's corners bleeding past
    // the dark hero background onto the page's white surroundings). Root
    // cause: CSS transform:rotate doesn't expand an element's own layout
    // box, so the padding/gap the hero was relying on for clearance
    // wasn't actually reserving room for the rotated card's larger
    // visual footprint. Fixed two ways: (1) a real margin on the photo
    // element, which DOES affect layout, reserves genuine clearance
    // sized to the rotation; (2) overflow:hidden on the hero itself as a
    // hard backstop so nothing can ever visually escape the dark
    // background regardless of viewport quirks. Mobile also switches to
    // a centered stacked column (flex-direction:column) with a slightly
    // gentler tilt, rather than reusing the desktop's wrapped-row layout.
    //
    // Follow-up, Al: "image is still small on mobile" -- the first mobile
    // pass additionally shrank the photo to 140x105 on top of the layout
    // fix, which was overcorrecting. Mobile briefly kept the same 200x150
    // card as desktop instead.
    //
    // Follow-up, Al: "on mobile we were squaring up the image and making
    // it look like this" (screenshot: a full-width, straight/un-tilted
    // 4:3 image, not a small tilted thumbnail). Checked the Learn site's
    // own source for how IT handles this -- ArticleDetailPage.tsx's hero
    // image div is literally `aspect-[4/3] w-full rotate-0 ... md:-rotate-[8deg]
    // md:w-[325px]`: the tilt and fixed width are md:-breakpoint-only
    // (desktop) overrides: on mobile the Learn site's own hero image is
    // always straight and full-width, not a shrunk tilted card. Matching
    // that exactly: mobile drops the rotation and margin entirely and
    // makes the photo fill the hero's content width at a 4:3 aspect
    // ratio, same as the real site.
    style.textContent =
      ".bd-review-hero{position:relative;background:#0f0f2d;color:#fff;" +
      "padding:32px 24px;display:flex;flex-wrap:wrap;align-items:center;" +
      "gap:28px;font-family:'Cabin',sans-serif;box-sizing:border-box;" +
      "overflow:hidden;}" +
      ".bd-review-hero__photo{flex:0 0 200px;width:200px;background:#fff;" +
      "padding:8px;border-radius:2px;transform:rotate(-6deg);margin:14px;" +
      "box-shadow:0 12px 24px -6px rgba(0,0,0,0.55);box-sizing:border-box;}" +
      ".bd-review-hero__photo img{display:block;width:100%;height:150px;" +
      "object-fit:cover;border-radius:1px;}" +
      ".bd-review-hero__copy{flex:1 1 260px;min-width:220px;}" +
      ".bd-review-hero__eyebrow{font-family:'Cabin',sans-serif;" +
      "font-size:11px;font-weight:600;text-transform:uppercase;" +
      "letter-spacing:0.15em;color:rgba(255,255,255,0.6);margin:0 0 8px;}" +
      ".bd-review-hero__title{font-family:'Space Grotesk',sans-serif;" +
      "font-size:22px;font-weight:600;line-height:1.25;margin:0 0 10px;" +
      "color:#fff;}" +
      ".bd-review-hero__hook{font-family:'Cabin',sans-serif;font-size:15px;" +
      "line-height:1.6;color:rgba(255,255,255,0.8);margin:0;}" +
      ".bd-review-body{padding:18px 24px;background:#faf8f4;" +
      "border:1px solid #e8e2d6;border-top:none;box-sizing:border-box;}" +
      ".bd-review-cta{display:inline-block;font-family:'Cabin',sans-serif;" +
      "font-size:15px;font-weight:600;color:#1f439e;text-decoration:none;}" +
      ".bd-review-cta:hover{text-decoration:underline;}" +
      "@media (max-width:480px){" +
      ".bd-review-hero{padding:24px 16px;flex-direction:column;}" +
      ".bd-review-hero__photo{flex:0 0 auto;width:100%;margin:0 0 18px;" +
      "transform:none;}" +
      ".bd-review-hero__photo img{height:auto;aspect-ratio:4/3;}" +
      ".bd-review-hero__copy{flex-basis:auto;min-width:0;}}";
    document.head.appendChild(style);
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

    injectStyles();

    var tabLi = document.createElement("li");
    tabLi.className = "tab tab--review";
    var tabLink = document.createElement("a");
    tabLink.className = "tab-title";
    tabLink.href = "#tab-review";
    tabLink.textContent = "Learn";
    tabLi.appendChild(tabLink);
    // Al: "is there a way to inject after the description before the
    // videos" -- confirmed live (Claude Browser DOM inspection,
    // bowlerdepot.com/brunswick-combat-solid/) the native tab order is
    // Description, Videos, Product Reviews, Reviews. Insert right after
    // the Description <li> (i.e. right before whatever was originally
    // second, normally Videos) instead of appending to the end. Falls
    // back to appendChild if a theme variant doesn't have a
    // tab--description li for some reason.
    var descriptionLi = tabsList.querySelector(".tab--description");
    if (descriptionLi && descriptionLi.nextSibling) {
      tabsList.insertBefore(tabLi, descriptionLi.nextSibling);
    } else {
      tabsList.appendChild(tabLi);
    }

    var content = document.createElement("div");
    content.className = "tab-content";
    content.id = "tab-review";

    var heroHtml = "";
    if (data.hero_image_url) {
      var imgUrl = resizedImageUrl(data.hero_image_url, { w: 400, h: 300, fit: "cover", fmt: "webp", q: 80 });
      heroHtml += '<div class="bd-review-hero__photo"><img src="' + escapeHtml(imgUrl) + '" alt="' +
        escapeHtml(data.title || "Ball review") + '"></div>';
    }
    var copyHtml = '<div class="bd-review-hero__copy"><div class="bd-review-hero__eyebrow">Ball Review</div>';
    if (data.title) {
      copyHtml += '<h3 class="bd-review-hero__title">' + escapeHtml(data.title) + "</h3>";
    }
    if (data.hook) {
      copyHtml += '<p class="bd-review-hero__hook">' + escapeHtml(data.hook) + "</p>";
    }
    copyHtml += "</div>";

    var html = '<div class="bd-review-hero">' + heroHtml + copyHtml + "</div>";
    if (data.learn_url) {
      html += '<div class="bd-review-body"><a class="bd-review-cta" href="' + escapeHtml(data.learn_url) +
        '" target="_blank" rel="noopener">Read the full review &rarr;</a></div>';
    }
    // Wrap in the theme's own .container, matching how every native tab
    // (#tab-description, etc.) wraps its content -- confirmed live
    // (Claude Browser DOM inspection, bowlerdepot.com/brunswick-combat-solid/)
    // that .tab-content > .container is the theme's own pattern, not
    // something specific to one tab. Reusing it means our hero picks up
    // the theme's own responsive padding/max-width rules for free,
    // including papathemes-section-inner's 1681px+ breakpoint
    // (max-width: 86.5rem; padding: 0 4.5rem) that Al found by inspecting
    // the live page -- rather than us re-deriving/hardcoding those values
    // ourselves and risking drift from the theme.
    content.innerHTML = '<div class="container">' + html + "</div>";

    // Content divs are siblings of ul.tabs (confirmed live: #tab-description,
    // #tab-videos, etc. all live directly under the same wrapper the
    // <ul class="tabs"> is in) -- mirror the same after-Description
    // position here so the tab's content lands in the matching DOM slot.
    var tabsContainer = tabsList.parentNode;
    var descriptionContent = document.getElementById("tab-description");
    if (descriptionContent && descriptionContent.nextSibling) {
      tabsContainer.insertBefore(content, descriptionContent.nextSibling);
    } else {
      tabsContainer.appendChild(content);
    }
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
