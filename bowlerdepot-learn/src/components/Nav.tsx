import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { categoryAncestors, getCategories, topLevelCategories } from "../api/client";
import type { Category } from "../api/types";
import {
  LEARN_PAGE_URL,
  LOGO_URL,
  SHIPPING_PROMISE,
  STORE_ACCOUNT_URL,
  STORE_CART_URL,
  STORE_CATEGORIES,
  STORE_GIFT_URL,
  STORE_LOCATIONS_URL,
  STORE_PAGES,
  STORE_SEARCH_URL,
  STORE_URL,
} from "./storeLinks";

// The new store header, rebuilt for Learn (2026-11-01). The request, via
// the Bowlerdepot Theme Development session: "Learn should feel like the
// same site: same logo bar and the same main links back to the store."
// So the top three rows copy the theme's templates/components/common/
// header.html and components/header/* (store bar, logo + search + account
// + cart, category menu; a slide-in <dialog> menu on phones), with the
// store's links from storeLinks.ts. Search, account and cart go to the
// store. What the theme fills in with JS (the shopper's chosen store and
// its hours, the cart count, results as you type) isn't available here:
// Learn is a different site, so the store bar links to the store
// locations instead and search submits to the store's search page.
//
// Learn's own navigation -- its category tabs and the plotter -- is a
// fourth row underneath: "Learn" in display type and its sections as
// tabs, scrolling sideways on a phone. It replaces the 2026-09 "two-tier
// masthead" (utility bar plus a logo with "| LEARN"), which was styled
// after the old storefront.
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

  const drawer = useRef<HTMLDialogElement>(null);
  // Close the phone menu when a Learn tab is followed (store links leave
  // the site anyway).
  useEffect(() => {
    drawer.current?.close();
  }, [pathname, searchParams]);

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

      {/* Store bar (theme: components/header/store-bar.html) */}
      <div className="bg-ink text-sm text-[#e9ecf2]">
        <div className="mx-auto flex max-w-[75rem] items-center justify-between gap-4 px-4 py-1.5 md:px-6">
          <a href={STORE_LOCATIONS_URL} className="flex min-w-0 items-center gap-1.5 text-inherit no-underline hover:underline">
            <svg className="h-4 w-4 shrink-0 text-maple" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <path d="M12 2a7 7 0 0 0-7 7c0 5.25 7 13 7 13s7-7.75 7-13a7 7 0 0 0-7-7zm0 9.5a2.5 2.5 0 1 1 0-5 2.5 2.5 0 0 1 0 5z" />
            </svg>
            <span className="truncate font-semibold text-white">Find your pro shop</span>
          </a>
          <ul className="m-0 hidden list-none items-center gap-5 p-0 text-sm md:flex">
            <li className="text-[#c9d0dc]">{SHIPPING_PROMISE}</li>
            <li>
              <a href={STORE_LOCATIONS_URL} className="text-inherit no-underline hover:underline">
                All store locations
              </a>
            </li>
            <li>
              <a href={STORE_GIFT_URL} className="text-inherit no-underline hover:underline">
                Gift Certificates
              </a>
            </li>
          </ul>
        </div>
      </div>

      <header className="sticky top-0 z-40 border-b border-line bg-white">
        {/* Logo, search, account, cart (theme: common/header.html) */}
        <div className="mx-auto flex max-w-[75rem] flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3 md:flex-nowrap md:gap-x-6 md:px-6">
          <button
            type="button"
            className="-ml-2 flex h-11 w-11 items-center justify-center rounded text-ink hover:bg-paper md:hidden"
            onClick={() => drawer.current?.showModal()}
            aria-haspopup="dialog"
            aria-controls="header-drawer"
          >
            <svg className="h-6 w-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" aria-hidden="true">
              <path d="M4 6h16M4 12h16M4 18h16" />
            </svg>
            <span className="sr-only">Open menu</span>
          </button>

          <a href={STORE_URL} className="block shrink-0 no-underline">
            <img src={LOGO_URL} alt="The Bowler Depot" className="block h-10 w-auto md:h-12" />
          </a>

          <form
            className="order-last w-full md:order-none md:max-w-xl md:flex-1"
            action={STORE_SEARCH_URL}
            method="get"
            role="search"
          >
            <label className="flex h-11 w-full items-center gap-2 rounded border border-line bg-paper pl-3 pr-1 focus-within:border-ink">
              <svg className="h-5 w-5 shrink-0 opacity-60" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" aria-hidden="true">
                <circle cx="11" cy="11" r="7" />
                <path d="m20 20-4-4" />
              </svg>
              <span className="sr-only">Search the store</span>
              <input
                className="min-w-0 grow border-0 bg-transparent p-0 text-base text-ink outline-none placeholder:text-slate"
                type="search"
                name="search_query"
                autoComplete="off"
                placeholder="Search balls, bags, shoes…"
              />
              <button type="submit" className="h-8 shrink-0 rounded bg-ink px-3 text-sm font-semibold text-white hover:bg-[#2a3550]">
                Search
              </button>
            </label>
          </form>

          <nav className="ml-auto flex items-center gap-1 md:ml-0" aria-label="Account">
            <a className="flex h-11 items-center gap-2 rounded px-2 font-semibold text-ink no-underline hover:bg-paper" href={STORE_ACCOUNT_URL}>
              <svg className="h-6 w-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <circle cx="12" cy="8" r="4" />
                <path d="M4 21a8 8 0 0 1 16 0" />
              </svg>
              <span className="sr-only lg:not-sr-only">Account</span>
            </a>
            <a className="flex h-11 items-center gap-2 rounded px-2 font-semibold text-ink no-underline hover:bg-paper" href={STORE_CART_URL}>
              <svg className="h-6 w-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M6 6h15l-1.5 9h-12z" />
                <path d="M6 6 5 3H2" />
                <circle cx="9" cy="20" r="1.5" />
                <circle cx="18" cy="20" r="1.5" />
              </svg>
              <span className="sr-only lg:not-sr-only">Cart</span>
            </a>
          </nav>
        </div>

        {/* Store category menu, 768px and up (theme: header/menu.html) */}
        <nav className="hidden border-t border-line md:block" aria-label="Main menu">
          <ul className="m-0 mx-auto flex max-w-[75rem] list-none flex-wrap items-stretch justify-center gap-x-1 space-y-0 p-0 px-4">
            {[...STORE_CATEGORIES, ...STORE_PAGES].map((link) => {
              const current = link.url === LEARN_PAGE_URL;
              return (
                <li key={link.url} className="flex">
                  <a
                    href={current ? "/" : link.url}
                    aria-current={current ? "page" : undefined}
                    className={`flex items-center px-2 py-3 font-display text-[17px] font-extrabold uppercase leading-none tracking-wide no-underline hover:text-pin lg:px-3 ${
                      current ? "text-pin" : "text-ink"
                    }`}
                  >
                    {link.name}
                  </a>
                </li>
              );
            })}
          </ul>
        </nav>

        {/* Learn's own sections */}
        {learnTabs.length > 0 ? (
          <div className="border-t border-line bg-paper">
            <div className="mx-auto flex max-w-[75rem] items-stretch gap-4 px-4 md:gap-6 md:px-6">
              <Link
                to="/"
                className="flex shrink-0 items-center font-display text-2xl font-black uppercase leading-none text-ink no-underline"
              >
                Learn
              </Link>
              <nav ref={tabRow} aria-label="Learn" className="relative -mr-4 min-w-0 flex-1 overflow-x-auto [scrollbar-width:none] md:mr-0">
                <ul className="m-0 flex list-none gap-1 space-y-0 p-0 pr-4">
                  {learnTabs.map((tab) => (
                    <li key={tab.key} className="flex">
                      <Link
                        to={tab.to}
                        aria-current={tab.active ? "page" : undefined}
                        className={`flex items-center whitespace-nowrap border-b-2 px-3 py-3 text-sm font-semibold no-underline ${
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
          </div>
        ) : null}
      </header>

      {/* Phone menu (theme: header/drawer.html) */}
      <dialog
        ref={drawer}
        id="header-drawer"
        aria-label="Main menu"
        className="m-0 h-dvh max-h-none w-[85vw] max-w-sm overflow-y-auto bg-white p-0 backdrop:bg-black/40"
        onClick={(e) => {
          // A click on the backdrop lands on the <dialog> itself.
          if (e.target === e.currentTarget) e.currentTarget.close();
        }}
      >
        <div className="sticky top-0 z-10 flex items-center justify-between border-b border-line bg-white px-4 py-3">
          <a href={STORE_URL} className="block no-underline">
            <img src={LOGO_URL} alt="The Bowler Depot" className="block h-10 w-auto" />
          </a>
          <button
            type="button"
            className="flex h-11 w-11 items-center justify-center rounded text-ink hover:bg-paper"
            aria-label="Close menu"
            onClick={() => drawer.current?.close()}
          >
            <svg className="h-6 w-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" aria-hidden="true">
              <path d="M6 6l12 12M18 6 6 18" />
            </svg>
          </button>
        </div>
        <ul className="m-0 list-none space-y-0 p-0">
          {STORE_CATEGORIES.map((link) => (
            <li key={link.url} className="border-b border-line">
              <a
                href={link.url}
                className="block px-4 py-3.5 font-display text-xl font-extrabold uppercase leading-none tracking-wide text-ink no-underline"
              >
                {link.name}
              </a>
            </li>
          ))}
          {STORE_PAGES.map((link) => (
            <li key={link.url} className="border-b border-line">
              <a
                href={link.url === LEARN_PAGE_URL ? "/" : link.url}
                className={`block px-4 py-3 no-underline ${link.url === LEARN_PAGE_URL ? "font-semibold text-pin" : "text-ink"}`}
              >
                {link.name}
              </a>
            </li>
          ))}
        </ul>
        <div className="space-y-1 px-4 py-4 text-sm">
          <a className="block py-1.5 text-ink no-underline" href={STORE_ACCOUNT_URL}>
            Sign in / Account
          </a>
          <a className="block py-1.5 text-ink no-underline" href={STORE_GIFT_URL}>
            Gift Certificates
          </a>
          <a className="block py-1.5 text-ink no-underline" href={STORE_LOCATIONS_URL}>
            All store locations
          </a>
        </div>
      </dialog>
    </>
  );
}
