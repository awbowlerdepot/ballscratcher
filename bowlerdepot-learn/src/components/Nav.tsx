import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { getCategories } from "../api/client";
import type { Category } from "../api/types";

// Real BowlerDepot logo/wordmark asset, pulled live off bowlerdepot.com
// (2026-09-05). Editorial Magazine theme's header is warm-white/paper,
// not black -- the asset is dark-on-transparent, so unlike the site's
// old black header it needs NO invert filter here to stay legible.
const LOGO_URL =
  "https://cdn11.bigcommerce.com/s-83dch55a9c/images/stencil/201x75/bowlerdepot_logo_website_logo_website_logo_1566417769__86141.original.png";

// Al: "two-tier masthead is probably best" (picked over a minimal
// single-row header and an editorial-tabs single row, after being shown
// all three as a static HTML mockup). Thin utility bar up top (just the
// storefront link -- "how do we best create a user experience that is
// not disjointed" is still the core reasoning, just moved off the main
// row) plus a bigger, centered logo lockup below, with an optional
// category-tab row underneath that.
export default function Nav() {
  const [searchParams] = useSearchParams();

  // Migration 031 -- Al: "not sure having the tab row when only one
  // category make sense. could be conditional for when there are more
  // than one." Fetched here (not hardcoded) so the tab row below the
  // masthead appears automatically once a second category exists --
  // today there's exactly one ("Bowling Balls"), so categories.length > 1
  // is false and NOTHING renders below the masthead. Same
  // getCategories()/seam-for-a-future-switcher reasoning as
  // LearnIndexPage.tsx's own eyebrow label, just surfaced in the header
  // instead.
  const [categories, setCategories] = useState<Category[]>([]);

  useEffect(() => {
    getCategories()
      .then(setCategories)
      .catch(() => setCategories([]));
  }, []);

  // Tabs link to /?category_id=<id> -- not wired to an actual filter on
  // LearnIndexPage.tsx yet (that page only filters on brand_id/q/sort
  // today), same "seam, not a finished feature" status as this file's
  // own categories fetch above. Highlighted tab: whatever category_id is
  // in the URL, or the first (== highest, migration 031's display_order)
  // category when there's no override -- mirrors LearnIndexPage.tsx's
  // own `items[0] ?? null` default.
  const activeCategoryId = searchParams.get("category_id") || categories[0]?.id;

  return (
    <header className="sticky top-0 z-10 border-b border-paper-border bg-paper/95 backdrop-blur-sm">
      <div className="bg-secondary">
        <div className="mx-auto max-w-[75rem] px-6 py-1.5 text-right md:px-8">
          <a
            href="https://bowlerdepot.com"
            className="text-xs font-semibold tracking-wide text-white/85 hover:text-white hover:no-underline"
          >
            Shop bowlerdepot.com
          </a>
        </div>
      </div>

      <div className="mx-auto flex max-w-[75rem] justify-center px-6 py-5 md:px-8">
        {/* Al: "can we have the logo then ' | LEARN' after it" -- replaces
            the old separate "Learn" wordmark (redundant next to a logo
            that already says BowlerDepot) with a single fused lockup. */}
        <a href="https://bowlerdepot.com" className="flex items-center gap-2 hover:no-underline">
          <img src={LOGO_URL} alt="The Bowler Depot" className="h-8 w-auto" />
          <span className="font-display text-lg font-semibold tracking-wide text-ink">
            | LEARN
          </span>
        </a>
      </div>

      {categories.length > 1 ? (
        <nav className="border-t border-paper-border">
          <div className="mx-auto flex max-w-[75rem] justify-center gap-8 px-6 py-3 text-xs font-semibold uppercase tracking-wide md:px-8">
            {categories.map((category) => (
              <Link
                key={category.id}
                to={`/?category_id=${category.id}`}
                className={
                  category.id === activeCategoryId
                    ? "text-ink hover:no-underline"
                    : "text-muted hover:text-ink hover:no-underline"
                }
              >
                {category.name}
              </Link>
            ))}
          </div>
        </nav>
      ) : null}
    </header>
  );
}
