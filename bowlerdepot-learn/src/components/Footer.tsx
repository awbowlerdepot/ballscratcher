import { Link } from "react-router-dom";
import { LOGO_URL, STORE_URL } from "./storeLinks";

// Learn's own footer in the new Bowler Depot design (2026-11-01): ink with
// the lane-arrow top edge (the design brief's masthead and the store
// footer use the same mark), Learn's links, the one link back to the
// store, and the "not affiliated" line kept from Learn's old footer, since
// Learn reviews other companies' balls. Standalone on purpose -- not a copy
// of the store's footer (Al: "Standalone, same design"; see Nav.tsx).

const linkClass = "text-inherit no-underline hover:text-white hover:underline";

export default function Footer() {
  return (
    <footer className="bg-ink text-[#c9d0dc]">
      <div className="lane-arrows h-2.5" aria-hidden="true" />
      <div className="mx-auto grid max-w-[75rem] gap-8 px-4 py-10 md:grid-cols-[1fr_auto] md:items-end md:px-6 md:py-12">
        <div>
          <Link to="/" className="inline-flex items-center gap-2.5 no-underline">
            <img src={LOGO_URL} alt="Bowler Depot" className="block h-8 w-auto brightness-0 invert" loading="lazy" />
            <span className="border-l-2 border-maple pl-2.5 font-display text-[28px] font-black uppercase leading-none text-white">
              Learn
            </span>
          </Link>
          <p className="m-0 mt-4 max-w-[52ch]">
            Ball reviews, bowling tips and the ball motion plotter, from Bowler Depot.
          </p>
          <ul className="m-0 mt-5 flex list-none flex-wrap gap-x-6 gap-y-2 space-y-0 p-0 font-semibold text-inherit">
            <li>
              <Link className={linkClass} to="/">
                Ball reviews
              </Link>
            </li>
            <li>
              <Link className={linkClass} to="/plotter">
                Ball motion plotter
              </Link>
            </li>
          </ul>
        </div>
        <a
          href={STORE_URL}
          className="inline-flex h-11 items-center gap-1 self-start rounded border-[1.5px] border-white px-5 font-bold text-white no-underline hover:bg-white hover:text-ink md:self-auto"
        >
          Shop Bowler Depot <span aria-hidden="true">&rarr;</span>
        </a>
      </div>
      <div className="border-t border-[#2a3550]">
        <div className="mx-auto flex max-w-[75rem] flex-col gap-2 px-4 py-5 font-mono text-xs text-[#9aa4b8] md:flex-row md:justify-between md:px-6">
          <p className="m-0">&copy; {new Date().getFullYear()} The Bowler Depot</p>
          <p className="m-0">Not affiliated with any manufacturer mentioned here.</p>
        </div>
      </div>
    </footer>
  );
}
