import { useEffect, useRef } from "react";

// Google AdSense in-article ad unit (Al: "would love to have some inline
// ads to generate some revenue" / "don't want popups or anything that is
// disruptive" -- in-article is Google's own ad type built to sit inline
// in body text and read as part of the page rather than an Auto-ads
// anchor bar or interstitial, which is exactly what Al asked to avoid).
// ca-pub-9681652990338336 / slot 2430978321 -- created in AdSense under
// Ads > By ad unit > In-article ad, root domain bowlerdepot.com approved
// first (AdSense only lets you register the root domain; subdomains,
// including this Learn site, are automatically covered once it clears
// review).
//
// Deliberately NOT rendered into scripts/prerender.ts's static HTML --
// that script exists so crawlers see clean, fast, ad-free article
// content (see its own header comment on why prerendering exists at
// all: SEO), and ad code has no business being baked into what a search
// engine indexes. This component only ever runs client-side, after
// index.html's adsbygoogle.js loader script has had a chance to load,
// same as any other post-hydration enhancement on this page.
//
// The visible "Advertisement" label keeps the unit clearly
// distinguishable from editorial content -- both a UX courtesy (Al's
// "not disruptive" ask) and standard ad-disclosure practice.
//
// Callers (ArticleDetailPage.tsx) MUST pass a `key` tied to the
// article's own id when rendering this. react-router client-side
// navigation between two articles (Related Reviews / More from this
// Brand links) reuses the same ArticleDetailPage component instance --
// without a fresh key forcing a fresh mount, the adsbygoogle.push()
// below would only ever fire once, for the FIRST article a visitor
// lands on, and every article navigated to afterward would show a
// blank slot.
export default function InArticleAd() {
  const pushedRef = useRef(false);

  useEffect(() => {
    // Guards against React StrictMode's dev-only double-invoke (mount ->
    // cleanup -> mount again within the same instance) pushing twice for
    // one real mount, which AdSense logs as an error.
    if (pushedRef.current) return;
    pushedRef.current = true;
    try {
      (window.adsbygoogle = window.adsbygoogle || []).push({});
    } catch {
      // adsbygoogle.js didn't load (network hiccup, ad blocker, offline)
      // -- fail silently rather than break the article around it.
    }
  }, []);

  return (
    <div className="my-10">
      <div className="mb-2 text-center text-[10px] uppercase tracking-widest text-muted">Advertisement</div>
      {/* eslint-disable-next-line react/no-unknown-property -- data-ad-*
          are AdSense's own attributes, not standard DOM/React props. */}
      <ins
        className="adsbygoogle"
        style={{ display: "block", textAlign: "center" }}
        data-ad-layout="in-article"
        data-ad-format="fluid"
        data-ad-client="ca-pub-9681652990338336"
        data-ad-slot="2430978321"
      />
    </div>
  );
}

declare global {
  interface Window {
    adsbygoogle?: unknown[];
  }
}
