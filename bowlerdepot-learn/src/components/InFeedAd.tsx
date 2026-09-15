import { useEffect, useRef, useState } from "react";

// Google AdSense in-feed ad unit -- Al: "can we do in feed ads for the
// article list?" after the in-article units went in (see
// InArticleAd.tsx's own comment for the full "no popups/Auto ads"
// reasoning, which applies here too). In-feed is Google's format built
// to sit as a native-looking row inside a list/grid of content, as
// opposed to in-article (mid-paragraph) or Auto ads (anchor/vignette
// interstitials). ca-pub-9681652990338336 / slot 8299838940 / layout-key
// "-6f+cd+1b-14+b1" -- created in AdSense under Ads > By ad unit >
// In-feed ad, same root-domain-approval prerequisite as InArticleAd.tsx
// (see that file's comment on bowlerdepot.com's AdSense approval).
//
// Used by LearnIndexPage.tsx's article grid only -- that page is a pure
// client-rendered SPA view (not prerendered by scripts/prerender.ts,
// unlike individual article pages), so there's no static-HTML/SEO
// concern here the way there is for InArticleAd.tsx.
//
// Same "push to adsbygoogle exactly once per mount" guard as
// InArticleAd.tsx -- see that file's comment on why (React StrictMode's
// dev-only double-invoke).
//
// Collapse-on-no-fill (Al: "what can we do about formatting when there
// is no ad?"): live-checked on learn.bowlerdepot.com and found that
// AdSense often declines to even process every in-feed slot on a page
// that repeats the same ad unit multiple times (no data-ad-status ever
// gets set on the later ones), on top of the normal "unfilled" case
// where it processes the slot but has no ad to serve. Both cases are
// watched for here -- see InArticleAd.tsx's matching comment for the
// full mechanism -- so a skipped/unfilled in-feed row disappears
// entirely (including the "Advertisement" label) rather than leaving a
// labeled empty gap in the article grid.
// Bounded-height-while-pending (Al: "it 100% looked off when 3 wide"):
// confirmed live -- AdSense's fluid-layout placeholder reservation
// scales off this unit's own width, and col-span-full makes that width
// the FULL grid row, so on a wide desktop 3-column layout the blank
// reserved block before a status ever lands is dramatically taller
// than on a narrow one. See InArticleAd.tsx's matching comment for the
// full mechanism; PENDING_MAX_HEIGHT_PX caps it here too so the pending
// placeholder never renders larger than a small box regardless of grid
// width, and the cap lifts the moment a real ad fills the slot.
const NO_STATUS_TIMEOUT_MS = 4000;
const PENDING_MAX_HEIGHT_PX = 90;

export default function InFeedAd() {
  const pushedRef = useRef(false);
  const insRef = useRef<HTMLModElement>(null);
  const [status, setStatus] = useState<"pending" | "filled" | "collapsed">("pending");

  useEffect(() => {
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
      // adsbygoogle.js didn't load -- fail silently, same as InArticleAd.tsx.
      setStatus("collapsed");
    }

    return () => {
      observer?.disconnect();
      if (timeoutId) clearTimeout(timeoutId);
    };
  }, []);

  if (status === "collapsed") return null;

  return (
    // col-span-full -- LearnIndexPage.tsx's grid is 1/2/3 columns
    // (sm:grid-cols-2 lg:grid-cols-3); an in-feed ad reads as a native
    // row break in the list, not another card squeezed into one column.
    <div
      className="col-span-full"
      style={status === "pending" ? { maxHeight: PENDING_MAX_HEIGHT_PX, overflow: "hidden" } : undefined}
    >
      <div className="mb-2 text-center text-[10px] uppercase tracking-widest text-muted">Advertisement</div>
      {/* eslint-disable-next-line react/no-unknown-property -- data-ad-*
          are AdSense's own attributes, not standard DOM/React props. */}
      <ins
        ref={insRef}
        className="adsbygoogle"
        style={{ display: "block" }}
        data-ad-format="fluid"
        data-ad-layout-key="-6f+cd+1b-14+b1"
        data-ad-client="ca-pub-9681652990338336"
        data-ad-slot="8299838940"
      />
    </div>
  );
}
