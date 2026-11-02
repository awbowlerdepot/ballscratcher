import { Link } from "react-router-dom";
import { LOGO_URL, STORE_CATEGORIES, STORE_GIFT_URL, STORE_HELP, STORE_LOCATIONS_URL, STORE_URL } from "./storeLinks";

// The new store footer, rebuilt for Learn (2026-11-01), from the theme's
// templates/components/common/footer.html and components/footer/*: ink
// background with the lane-arrow top edge, the pro shop services, link
// columns and the small print. Same "feel like the same site" reasoning as
// Nav.tsx.
//
// Left out on purpose: the "Your pro shop" card (needs the store picker's
// saved store, which lives on bowlerdepot.com) -- Learn points to the
// store locations instead; the newsletter form (BigCommerce's subscribe
// endpoint is same-site only); payment and social icons (the theme draws
// them from store settings and its own icon sprite).
//
// Wording is the theme's lang/en.json footer.* strings. Kept from Learn's
// old footer: the "not affiliated with any manufacturer"
// line, since Learn publishes reviews of other companies' balls.

const SERVICES = [
  {
    title: "Drilling",
    text: "Layout and drilling for a new ball, done by a pro shop tech.",
    icon: (
      <>
        <circle cx="12" cy="12" r="9" />
        <circle cx="10" cy="8.5" r="1.3" />
        <circle cx="14" cy="8.5" r="1.3" />
        <circle cx="12" cy="14" r="1.6" />
      </>
    ),
  },
  {
    title: "Fitting",
    text: "Span, pitch and thumb fit, measured for your hand.",
    icon: (
      <>
        <path d="M3 15.5 15.5 3 21 8.5 8.5 21z" />
        <path d="m7 12 2 2M10 9l2 2M13 6l2 2" />
      </>
    ),
  },
  {
    title: "Resurfacing",
    text: "Bring back the reaction on a ball that's lost it.",
    icon: (
      <>
        <path d="M20 12a8 8 0 0 1-14 5.3" />
        <path d="M4 12a8 8 0 0 1 14-5.3" />
        <path d="M18 3v4h-4M6 21v-4h4" />
      </>
    ),
  },
];

const linkClass = "text-inherit no-underline hover:text-white hover:underline";
const headingClass = "m-0 font-display text-xl font-extrabold uppercase tracking-wide text-white";

export default function Footer() {
  return (
    <footer className="relative bg-ink text-[#c9d0dc]">
      <div className="lane-arrows h-2.5" aria-hidden="true" />

      <div className="mx-auto max-w-[75rem] px-4 pb-10 pt-12 md:px-6 md:pt-16">
        <section aria-labelledby="footer-store-heading">
          <p id="footer-store-heading" className="m-0 font-mono text-xs font-medium uppercase tracking-[.12em] text-maple">
            Your pro shop
          </p>
          <h2 className="m-0 mt-3 font-display text-4xl font-black uppercase leading-[.9] text-white md:text-5xl">
            Six pro shops around Denver
          </h2>
          <p className="m-0 mt-3 max-w-[46ch]">
            Read the review here, then bring the ball to a store to have it drilled and fitted.
          </p>
          <a
            href={STORE_LOCATIONS_URL}
            className="mt-5 inline-flex h-11 items-center rounded border border-white px-5 font-semibold text-white no-underline hover:bg-white hover:text-ink"
          >
            Find a store
          </a>
        </section>

        <section className="mt-10" aria-labelledby="footer-services-heading">
          <h2 id="footer-services-heading" className="m-0 font-mono text-xs font-medium uppercase tracking-[.12em] text-[#9aa4b8]">
            Pro shop services
          </h2>
          <ul className="m-0 mt-4 grid list-none gap-3 space-y-0 p-0 text-inherit sm:grid-cols-3 md:gap-4">
            {SERVICES.map((s) => (
              <li key={s.title} className="flex h-full gap-3 rounded-lg border border-[#3a4560] p-4">
                <svg className="h-7 w-7 shrink-0 text-maple" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  {s.icon}
                </svg>
                <span className="min-w-0">
                  <span className="block font-display text-xl font-extrabold uppercase leading-none text-white">{s.title}</span>
                  <span className="mt-1.5 block text-sm leading-snug">{s.text}</span>
                </span>
              </li>
            ))}
          </ul>
        </section>

        <nav
          className="mt-12 grid grid-cols-2 gap-x-6 gap-y-10 border-t border-[#2a3550] pt-10 md:mt-14 md:grid-cols-3 md:gap-8"
          aria-label="Footer"
        >
          <section>
            <h3 className={headingClass}>Shop</h3>
            <ul className="m-0 mt-4 grid list-none gap-2.5 space-y-0 p-0 text-inherit">
              {STORE_CATEGORIES.map((l) => (
                <li key={l.url}>
                  <a className={linkClass} href={l.url}>
                    {l.name}
                  </a>
                </li>
              ))}
              <li>
                <a className={linkClass} href={STORE_GIFT_URL}>
                  Gift Certificates
                </a>
              </li>
            </ul>
          </section>
          <section>
            <h3 className={headingClass}>Learn &amp; tools</h3>
            <ul className="m-0 mt-4 grid list-none gap-2.5 space-y-0 p-0 text-inherit">
              <li>
                <Link className={linkClass} to="/">
                  Ball reviews
                </Link>
              </li>
              <li>
                <Link className={linkClass} to="/plotter">
                  Ball motion plotter
                </Link>
                <span className="mt-0.5 block font-mono text-[11px] uppercase tracking-[.08em] text-[#5fa8d3]">
                  Every current ball, compared
                </span>
              </li>
            </ul>
          </section>
          <section>
            <h3 className={headingClass}>Help &amp; info</h3>
            <ul className="m-0 mt-4 grid list-none gap-2.5 space-y-0 p-0 text-inherit">
              {STORE_HELP.map((l) => (
                <li key={l.url}>
                  <a className={linkClass} href={l.url}>
                    {l.name}
                  </a>
                </li>
              ))}
            </ul>
          </section>
        </nav>
      </div>

      <div className="border-t border-[#2a3550]">
        <div className="mx-auto flex max-w-[75rem] flex-col gap-3 px-4 py-6 md:flex-row md:items-center md:justify-between md:px-6">
          <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
            <a className="block no-underline" href={STORE_URL}>
              <img className="block h-7 w-auto brightness-0 invert" src={LOGO_URL} alt="The Bowler Depot" loading="lazy" />
            </a>
            <p className="m-0 font-mono text-xs text-[#9aa4b8]">&copy; {new Date().getFullYear()} The Bowler Depot</p>
          </div>
          <p className="m-0 text-xs text-[#9aa4b8]">Not affiliated with any manufacturer mentioned here.</p>
        </div>
      </div>
    </footer>
  );
}
