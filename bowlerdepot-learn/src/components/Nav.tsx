import { NavLink } from "react-router-dom";

// Real BowlerDepot logo/wordmark asset, pulled live off bowlerdepot.com
// (2026-09-05). Editorial Magazine theme's header is warm-white/paper,
// not black -- the asset is dark-on-transparent, so unlike the site's
// old black header it needs NO invert filter here to stay legible.
const LOGO_URL =
  "https://cdn11.bigcommerce.com/s-83dch55a9c/images/stencil/201x75/bowlerdepot_logo_website_logo_website_logo_1566417769__86141.original.png";

// Al's core ask for this whole section: "how do we best create a user
// experience that is not disjointed" -- this header is deliberately the
// live bowlerdepot.com logo plus an explicit "Shop" link back to the
// storefront (not just a Learn-only nav), so a visitor always has an
// obvious, branded way back to buying, not just reading.
export default function Nav() {
  return (
    <header className="sticky top-0 z-10 border-b border-paper-border bg-paper/95 backdrop-blur-sm">
      <div className="mx-auto flex max-w-[75rem] flex-wrap items-center justify-between gap-3 px-6 py-4">
        <a href="https://bowlerdepot.com" className="flex items-center gap-2">
          <img src={LOGO_URL} alt="The Bowler Depot" className="h-8 w-auto" />
          <span className="font-display text-lg font-semibold text-ink">
            Learn
          </span>
        </a>
        <nav className="flex items-center gap-6 text-sm">
          <NavLink
            to="/"
            end
            className={({ isActive }) =>
              isActive ? "font-medium text-ink" : "font-medium text-muted hover:text-ink"
            }
          >
            Ball Reviews
          </NavLink>
          <a
            href="https://bowlerdepot.com"
            className="rounded-full border border-ink px-4 py-1.5 font-medium text-ink transition-colors hover:bg-ink hover:text-paper hover:no-underline"
          >
            Shop BowlerDepot
          </a>
        </nav>
      </div>
    </header>
  );
}
