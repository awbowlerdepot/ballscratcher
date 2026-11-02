import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { categoryAncestors, getCategories, topLevelCategories } from "../api/client";
import type { Category } from "../api/types";
import { LOGO_URL, STORE_URL } from "./storeLinks";

// Learn's header in the new Bowler Depot design (2026-11-01). Learn is a
// standalone site with the store's design language, not a copy of the
// store's header: Al, reviewing a first pass that mirrored bowlerdepot.com's
// header and menu, "it was a stand alone site", then chose "Standalone,
// same design". So: the Bowler Depot logo with "Learn" in display type,
// Learn's own sections (its top-level categories as tabs, plus the Ball
// Motion Plotter), and one link back to the store. It replaces the
// 2026-09 "two-tier masthead" (utility bar plus a logo with "| LEARN"),
// which was styled after the old storefront.
//
// Phone first: logo, wordmark and the store link share one row; the tabs
// are a second row that scrolls sideways and keeps the current tab in view.
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

  // Tabs are TOP-LEVEL categories only (migration 037 nests categories;
  // subcategories are chips on LearnIndexPage instead), and each links to
  // /?category_id=<id>, which LearnIndexPage filters on. getCategories()
  // already omits categories with no published articles, so a new, empty
  // "Bowling Tips" doesn't add a tab until it has something in it.
  // Highlighted tab: the top-level ancestor of whatever category_id is in
  // the URL (so a subcategory keeps its parent's tab lit), or the first
  // tab by display_order when there's none -- LearnIndexPage's same
  // default.
  const tabs = topLevelCategories(categories);
  const selected = categories.find((c) => c.id === searchParams.get("category_id"));
  // On an article page there's no ?category_id, and the header doesn't
  // know the article's category -- highlight no tab rather than wrongly
  // lighting the first one (a Bowling Tips article under "Bowling Balls").
  const pathname = useLocation().pathname;
  const onArticlePage = pathname.startsWith("/articles/");
  // Ball motion plotter (runbook 6cc) gets its own tab after the
  // categories, which also means the tab row now always renders.
  const onPlotterPage = pathname.startsWith("/plotter");
  const activeCategoryId = selected
    ? (categoryAncestors(categories, selected)[0] ?? selected).id
    : onArticlePage || onPlotterPage
      ? undefined
      : tabs[0]?.id;

  // On a phone the tab row scrolls sideways; bring the current tab into
  // view (sideways only -- scrollIntoView would also move the page).
  const tabRow = useRef<HTMLElement>(null);
  useEffect(() => {
    const row = tabRow.current;
    const current = row?.querySelector<HTMLElement>('[aria-current="page"]');
    if (!row || !current) return;
    if (current.offsetLeft + current.offsetWidth > row.scrollLeft + row.clientWidth || current.offsetLeft < row.scrollLeft) {
      row.scrollLeft = current.offsetLeft - 16;
    }
  }, [pathname, activeCategoryId, tabs.length]);

  const learnTabs = [
    ...tabs.map((category) => ({
      key: category.id,
      to: `/?category_id=${category.id}`,
      label: category.name,
      active: category.id === activeCategoryId,
    })),
    { key: "plotter", to: "/plotter", label: "Ball Motion Plotter", active: onPlotterPage },
  ];

  return (
    <>
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2"
      >
        Skip to main content
      </a>
      <header className="sticky top-0 z-40 border-b border-line bg-white">
        <div className="mx-auto flex max-w-[75rem] flex-wrap items-center gap-x-6 px-4 md:flex-nowrap md:px-6">
          <Link to="/" className="flex shrink-0 items-center gap-2.5 py-3 no-underline" aria-label="Bowler Depot Learn, home">
            <img src={LOGO_URL} alt="" className="block h-9 w-auto md:h-11" />
            <span className="border-l-2 border-maple pl-2.5 font-display text-[28px] font-black uppercase leading-none text-ink md:text-[32px]">
              Learn
            </span>
          </Link>

          <a
            href={STORE_URL}
            className="ml-auto inline-flex h-10 shrink-0 items-center gap-1 rounded border-[1.5px] border-ink px-3 text-sm font-bold text-ink no-underline hover:bg-ink hover:text-white md:order-last md:ml-0"
          >
            <span className="hidden sm:inline">Shop Bowler Depot</span>
            <span className="sm:hidden">Shop</span>
            <span aria-hidden="true">&rarr;</span>
          </a>

          <nav
            ref={tabRow}
            aria-label="Learn"
            className="relative -mx-4 w-[calc(100%+2rem)] overflow-x-auto border-t border-line [scrollbar-width:none] md:mx-0 md:w-auto md:flex-1 md:self-stretch md:border-t-0"
          >
            <ul className="m-0 flex h-full list-none gap-1 space-y-0 p-0 px-2 md:px-0">
              {learnTabs.map((tab) => (
                <li key={tab.key} className="flex">
                  <Link
                    to={tab.to}
                    aria-current={tab.active ? "page" : undefined}
                    className={`flex items-center whitespace-nowrap border-b-2 px-3 py-3 font-display text-[17px] font-extrabold uppercase leading-none tracking-wide no-underline ${
                      tab.active ? "border-pin text-ink" : "border-transparent text-slate hover:border-ink hover:text-ink"
                    }`}
                  >
                    {tab.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        </div>
      </header>
    </>
  );
}
