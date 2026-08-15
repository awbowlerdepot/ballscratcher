"""Manual, no-DB smoke test for a Price Site's search_url_template /
result_link_selector / default_css_selector, before those values are ever
saved via POST /price-sites. Al: "how do i test just that" (of the values
for a new site) -- rather than saving the row and using the discover-
price-sources button (which checks EVERY active site for a product, not
just the one you're setting up), this hits the target site directly with
the same selector logic price_checker/app.py uses (parse_search_results'
soup.select() + extract_price's soup.select_one(), copied in below rather
than imported since this script is meant to run standalone with just
`pip3 install -r scripts/requirements.txt`, no AWS/DB config needed).

Edit SITE/QUERY/CSS_SELECTOR below for whatever site you're testing, then:
    python3 scripts/test_price_site.py
"""
import re
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

# --- Edit these for the site you're testing ---
SEARCH_URL_TEMPLATE = "https://www.bowling.com/search/results?s={query}"
RESULT_LINK_SELECTOR = "a.prlnk"
PRICE_CSS_SELECTOR = "#mainPrice"
QUERY = "storm bionic"
# ------------------------------------------------

_PRICE_WITH_CENTS_RE = re.compile(r"(\d[\d,]*\.\d{2})")
_PRICE_INTEGER_RE = re.compile(r"(\d[\d,]*)")


def fetch_page(url):
    resp = requests.get(url, headers={"User-Agent": "bowling-scraper-price-checker/1.0"}, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_price(text):
    # Mirrors src/price_checker/app.py's parse_price exactly.
    if not text:
        return None
    match = _PRICE_WITH_CENTS_RE.search(text) or _PRICE_INTEGER_RE.search(text)
    if match is None:
        return None
    try:
        return round(float(match.group(1).replace(",", "")), 2)
    except ValueError:
        return None


def find_search_result(html, base_url):
    # Mirrors src/price_checker/app.py's parse_search_results, first match only.
    soup = BeautifulSoup(html, "html.parser")
    for link in soup.select(RESULT_LINK_SELECTOR):
        href = link.get("href")
        if not href:
            continue
        return {"product_url": urljoin(base_url, href), "title": " ".join(link.get_text(strip=True).split())}
    return None


def extract_price(html):
    # Mirrors src/price_checker/app.py's extract_price.
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one(PRICE_CSS_SELECTOR)
    if el is None:
        return {"price": None, "error": f"selector {PRICE_CSS_SELECTOR!r} matched nothing"}
    raw_text = " ".join(el.get_text(strip=True).split())
    price = parse_price(raw_text)
    if price is None:
        return {"price": None, "error": f"could not parse a price from {raw_text!r}"}
    return {"price": price, "error": None}


def main():
    print(f"1. Searching via search_url_template for {QUERY!r}...")
    search_url = SEARCH_URL_TEMPLATE.format(query=quote_plus(QUERY))
    search_html = fetch_page(search_url)
    result = find_search_result(search_html, search_url)

    if not result:
        print(f"   FAILED: no results found via result_link_selector {RESULT_LINK_SELECTOR!r}")
        raise SystemExit(1)

    print(f"   -> {result['title']!r} -> {result['product_url']}")

    print(f"\n2. Fetching that product page and extracting price via default_css_selector {PRICE_CSS_SELECTOR!r}...")
    product_html = fetch_page(result["product_url"])
    price_result = extract_price(product_html)
    print(f"   -> {price_result}")

    if price_result["error"]:
        print("   FAILED: could not extract a price")
        raise SystemExit(1)

    print(f"\nPASS: found ${price_result['price']} on {result['product_url']}")


if __name__ == "__main__":
    main()
