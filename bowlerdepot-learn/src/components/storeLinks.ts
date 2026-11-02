// The store's own header and footer links, so Learn's header and footer
// (Nav.tsx, Footer.tsx) match bowlerdepot.com's.
//
// The new store theme (awbowlerdepot/bowlerdepot-theme,
// templates/components/header/menu.html and footer/links.html) builds its
// menu at render time from the store's top-level categories plus its
// visible web pages. Learn is a separate site and can't read those, so
// they're copied here, as they were on the live store on 2026-11-01. When a
// category or web page is added, renamed or removed in BigCommerce, update
// this file to match.
//
// Absolute URLs throughout: every link leaves learn.bowlerdepot.com except
// the Learn page itself, which Nav marks as the current one.

export const STORE_URL = "https://bowlerdepot.com";

// The store logo (store settings > logo), unchanged by the redesign -- "only
// the logo is kept" from the old site.
export const LOGO_URL =
  "https://cdn11.bigcommerce.com/s-83dch55a9c/images/stencil/201x75/bowlerdepot_logo_website_logo_website_logo_1566417769__86141.original.png";

export type StoreLink = { name: string; url: string };

// Top-level categories, in the store's menu order.
export const STORE_CATEGORIES: StoreLink[] = [
  { name: "Balls", url: `${STORE_URL}/balls/` },
  { name: "Bags", url: `${STORE_URL}/bags/` },
  { name: "Shoes", url: `${STORE_URL}/shoes/` },
  { name: "Accessories", url: `${STORE_URL}/accessories/` },
  { name: "On Sale", url: `${STORE_URL}/on-sale/` },
];

// Web pages shown in the store's menu after the categories. "Learn" is the
// store's web page that links here (runbook 6fj).
export const LEARN_PAGE_URL = "https://learn.bowlerdepot.com/";
export const STORE_PAGES: StoreLink[] = [
  { name: "Learn", url: LEARN_PAGE_URL },
  { name: "Brad & Kyle", url: `${STORE_URL}/bradandkyle/` },
  { name: "Locations", url: `${STORE_URL}/locations/` },
  { name: "Blog", url: `${STORE_URL}/blog/` },
];

// The footer's "Help & info" column.
export const STORE_HELP: StoreLink[] = [
  { name: "Shipping & Returns", url: `${STORE_URL}/shipping-returns/` },
  { name: "Contact Us", url: `${STORE_URL}/contact-us/` },
  { name: "Privacy Policy", url: `${STORE_URL}/privacy-policy/` },
  { name: "Terms & Conditions", url: `${STORE_URL}/terms-conditions/` },
  { name: "Sitemap", url: `${STORE_URL}/sitemap.php` },
];

export const STORE_LOCATIONS_URL = `${STORE_URL}/locations/`;
export const STORE_SEARCH_URL = `${STORE_URL}/search.php`;
export const STORE_ACCOUNT_URL = `${STORE_URL}/account.php`;
export const STORE_CART_URL = `${STORE_URL}/cart.php`;
export const STORE_GIFT_URL = `${STORE_URL}/giftcertificates.php`;

// The theme's lang/en.json header.shipping_promise.
export const SHIPPING_PROMISE = "Free shipping to the lower 48 · 30-day returns";
