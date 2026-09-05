import { NavLink } from "react-router-dom";

// Real BowlerDepot logo/wordmark asset, pulled live off bowlerdepot.com
// (2026-09-05) -- see index.css's own header comment for why it's
// inverted via CSS filter for use on this header's black background.
const LOGO_URL =
  "https://cdn11.bigcommerce.com/s-83dch55a9c/images/stencil/201x75/bowlerdepot_logo_website_logo_website_logo_1566417769__86141.original.png";

// Al's core ask for this whole section: "how do we best create a user
// experience that is not disjointed" -- this header is deliberately the
// live bowlerdepot.com logo plus an explicit "Shop" link back to the
// storefront (not just a Learn-only nav), so a visitor always has an
// obvious, branded way back to buying, not just reading.
export default function Nav() {
  return (
    <header className="site-header">
      <div className="site-header-inner">
        <a href="https://bowlerdepot.com" className="brand">
          <img src={LOGO_URL} alt="The Bowler Depot" />
          <span className="brand-divider">|</span>
          <span className="brand-suffix">Learn</span>
        </a>
        <nav className="main-nav">
          <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
            Ball Reviews
          </NavLink>
          <a href="https://bowlerdepot.com" className="shop-link">
            Shop BowlerDepot
          </a>
        </nav>
      </div>
    </header>
  );
}
