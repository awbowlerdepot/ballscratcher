import { useEffect, useRef } from "react";

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
export default function InFeedAd() {
  const pushedRef = useRef(false);

  useEffect(() => {
    if (pushedRef.current) return;
    pushedRef.current = true;
    try {
      (window.adsbygoogle = window.adsbygoogle || []).push({});
    } catch {
      // adsbygoogle.js didn't load -- fail silently, same as InArticleAd.tsx.
    }
  }, []);

  return (
    // col-span-full -- LearnIndexPage.tsx's grid is 1/2/3 columns
    // (sm:grid-cols-2 lg:grid-cols-3); an in-feed ad reads as a native
    // row break in the list, not another card squeezed into one column.
    <div className="col-span-full">
      <div className="mb-2 text-center text-[10px] uppercase tracking-widest text-muted">Advertisement</div>
      {/* eslint-disable-next-line react/no-unknown-property -- data-ad-*
          are AdSense's own attributes, not standard DOM/React props. */}
      <ins
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
