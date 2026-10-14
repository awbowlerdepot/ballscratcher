import { useEffect, useRef, useState } from "react";

// Google AdSense display ad unit -- Al: "can we add an ad placement to the
// bottom of the ball motion chart" (runbook 6dq). The plotter isn't body
// text or a content list, so neither of the existing units fits:
// in-article (InArticleAd.tsx) is Google's format for between paragraphs,
// and in-feed (InFeedAd.tsx) is built to look like a row in a list. A
// plain responsive *display* unit is the one Google intends for a
// stand-alone banner under a tool like this. Same "no popups / no Auto
// ads" reasoning as InArticleAd.tsx's comment (runbook 6bi): a fixed,
// labeled slot that sits in the page flow and never overlays the chart.
//
// ca-pub-9681652990338336 / slot 5838656128 -- created in AdSense
// under Ads > By ad unit > Display ads (responsive), same root-domain
// approval as the other two units. data-ad-format="horizontal" asks for
// banner shapes (728x90-style on desktop, 320x50/100 on phones) so the
// ad reads as a strip under the chart rather than a tall square block
// pushing the brand legend and the mobile "Pick a ball" panel down.
//
// Same "push to adsbygoogle exactly once per mount" guard and the same
// collapse-on-no-fill / bounded-height-while-pending handling as
// InArticleAd.tsx -- see that file's comments for the full mechanism
// (AdSense sometimes never sets data-ad-status, and a fluid/responsive
// slot pre-reserves a placeholder sized off its own width, which on the
// full-width plotter would be a large blank box until it resolves).
const DISPLAY_AD_SLOT = "5838656128";
const NO_STATUS_TIMEOUT_MS = 4000;
const PENDING_MAX_HEIGHT_PX = 90;

export default function DisplayAd({ className = "" }: { className?: string }) {
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
    <div
      className={className}
      style={status === "pending" ? { maxHeight: PENDING_MAX_HEIGHT_PX, overflow: "hidden" } : undefined}
    >
      <div className="mb-2 text-center text-[10px] uppercase tracking-widest text-muted">Advertisement</div>
      {/* eslint-disable-next-line react/no-unknown-property -- data-ad-*
          are AdSense's own attributes, not standard DOM/React props. */}
      <ins
        ref={insRef}
        className="adsbygoogle"
        style={{ display: "block" }}
        data-ad-client="ca-pub-9681652990338336"
        data-ad-slot={DISPLAY_AD_SLOT}
        data-ad-format="horizontal"
        data-full-width-responsive="true"
      />
    </div>
  );
}
