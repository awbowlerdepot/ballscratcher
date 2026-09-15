import { useEffect, useRef, useState } from "react";

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
//
// Collapse-on-no-fill (Al: "what can we do about formatting when there
// is no ad?"): AdSense sets data-ad-status="unfilled" on the <ins>
// itself once it decides it has no ad to serve for this request (and
// leaves the slot at 0x0) -- confirmed live on learn.bowlerdepot.com.
// Watched here via MutationObserver so the WHOLE unit, including the
// "Advertisement" label above it, disappears instead of leaving a
// labeled empty gap in the article. If AdSense never gets around to
// setting any status at all -- observed in practice for duplicate/
// throttled in-feed slots on the same page, see InFeedAd.tsx -- the
// NO_STATUS_TIMEOUT_MS fallback below collapses it too rather than
// leaving that label stranded indefinitely.
//
// Bounded-height-while-pending (Al: "it 100% looked off when 3 wide"):
// the collapse above only fires once AdSense resolves the slot, but
// AdSense pre-reserves a fluid-layout placeholder sized off the
// CONTAINER'S WIDTH before it knows whether it can fill -- on a wide
// desktop viewport (3-column grid, full-width row) that reservation
// scales up into a genuinely huge blank block, confirmed live at
// 1600px. PENDING_MAX_HEIGHT_PX caps the wrapper itself while still
// "pending" so that reservation can never render larger than a small,
// unobtrusive box no matter how wide the page is; the cap lifts the
// moment a real ad actually fills the slot.
const NO_STATUS_TIMEOUT_MS = 4000;
const PENDING_MAX_HEIGHT_PX = 90;

export default function InArticleAd() {
  const pushedRef = useRef(false);
  const insRef = useRef<HTMLModElement>(null);
  const [status, setStatus] = useState<"pending" | "filled" | "collapsed">("pending");

  useEffect(() => {
    // Guards against React StrictMode's dev-only double-invoke (mount ->
    // cleanup -> mount again within the same instance) pushing twice for
    // one real mount, which AdSense logs as an error.
    if (pushedRef.current) return;
    pushedRef.current = true;

    const insEl = insRef.current;
    let observer: MutationObserver | undefined;
    let timeoutId: ReturnType<typeof setTimeout> | undefined;

    if (insEl) {
      observer = new MutationObserver(() => {
        const adStatus = insEl.getAttribute("data-ad-status");
        if (adStatus === "filled") setStatus("filled");
        else if (adStatus === "unfilled") setStatus("collapsed");
      });
      observer.observe(insEl, { attributes: true, attributeFilter: ["data-ad-status"] });

      timeoutId = setTimeout(() => {
        if (!insEl.getAttribute("data-ad-status")) setStatus("collapsed");
      }, NO_STATUS_TIMEOUT_MS);
    }

    try {
      (window.adsbygoogle = window.adsbygoogle || []).push({});
    } catch {
      // adsbygoogle.js didn't load (network hiccup, ad blocker, offline)
      // -- fail silently rather than break the article around it.
      setStatus("collapsed");
    }

    return () => {
      observer?.disconnect();
      if (timeoutId) clearTimeout(timeoutId);
    };
  }, []);

  if (status === "collapsed") return null;

  return (
    <div
      className="my-10"
      style={status === "pending" ? { maxHeight: PENDING_MAX_HEIGHT_PX, overflow: "hidden" } : undefined}
    >
      <div className="mb-2 text-center text-[10px] uppercase tracking-widest text-muted">Advertisement</div>
      {/* eslint-disable-next-line react/no-unknown-property -- data-ad-*
          are AdSense's own attributes, not standard DOM/React props. */}
      <ins
        ref={insRef}
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
