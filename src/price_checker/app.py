"""
Price-tracking Lambda. Al: "id like to start a price tracker. this should
be configurable to have site setup so that it will pull the current price
from a number of sites on a frequency of likely daily? then store this in
a way that would allow for charting that price over time in the admin ui
and eventually the consumer UI."

DESIGN CORRECTION, mid-build (see 014_price_tracking.sql's header comment
for the full writeup): the first draft of this Lambda only ever CHECKED a
price -- fetch_page + extract_price against a product_url an admin was
expected to paste in by hand, one per product per site. Al's actual ask
was for "site setup" to mean choosing which real retailers to track
(bowling.com, bowlingball.com, bowlersmart.com, ...) with the product URL
on each site found AUTOMATICALLY, the same way video_discovery searches
YouTube for candidate review videos -- and, after briefly going back and
forth on whether a found match should auto-track immediately or wait for
admin approval, Al settled on "the reccomended path is best": mirror
product_videos' pending/approved/rejected review workflow exactly,
including admin-side approve/reject/restore (see admin_api/service.py's
list_price_sources/approve_price_source/reject_price_source/
restore_price_source), not auto-track-and-fix-later.

This Lambda therefore has TWO distinct jobs, same "one Lambda, several
job shapes" convention video_discovery already uses for its own search vs.
{"refresh_stats": true} split:

DISCOVERY ({"discover": true, ...}) -- searches configured price_sites for
candidate product-page URLs per product, mirroring video_discovery's
search_youtube/score_match/build_search_query architecture almost exactly,
just against a generic retailer site-search page instead of YouTube's
search.list API. Every candidate found is stored as a 'pending'
product_price_sources row (never silently dropped, never auto-approved) --
an admin resolves each one via the same approve/reject/restore workflow
product_videos already has. Manual/direct invoke only, same convention as
video_discovery's own search flow (see that module's own docstring) --
there's no schedule wired to this job shape in template.yaml.
    {"discover": true, "product_ids": ["<uuid>", ...]}  -- specific products
    {"discover": true, "brand_id": "<uuid>"}            -- all 'current' products for one brand
    {"discover": true}                                   -- all 'current' products (capped, see below)
    {"discover": true, "limit": N}                       -- same as {} but with an explicit cap

CHECKING (every other job shape, unchanged from the original design) --
fetches each APPROVED + active product_price_sources row's product_url and
extracts a price via CSS selector. This is deliberately a THIN, generic
scraper: it cannot understand a site's markup the way the bespoke
per-brand product scrapers do (no table-matching-by-label-text, no JSON
API) -- that's the tradeoff for "any site, no new code per site," and it
means a site's own redesign can silently break a selector. See
record_price_check: every check writes a product_price_history row
REGARDLESS of success, with the failure reason in `error`, so a broken
selector is visible in the admin UI (as a run of error rows) rather than a
check that silently disappears. This job shape only ever touches
status='approved' rows -- pending/rejected candidates are never checked
for price, same as a pending/rejected product_videos row is never
summarized.
    {"product_ids": ["<uuid>", ...]}  -- only approved+active sources belonging to these products
                                          (the admin-site "check price now" button's target)
    {}                                  -- daily batch: up to DEFAULT_PRICE_CHECK_LIMIT of the
                                            most-overdue approved+active sources across the catalog
    {"limit": N}                        -- same as {} but with an explicit batch size

ROTATION: both job families use the same "asc nulls first" idiom video_
discovery pioneered for products.last_video_discovery_at (see
005_products_last_video_discovery_at.sql). Checking orders by
product_price_sources.last_checked_at; discovery orders by the new
products.last_price_discovery_at (015_products_last_price_discovery_at.sql,
added proactively this time, not after finding the same rotation bug in
production a second time) -- both ensure a repeated {} invocation actually
progresses across the whole set instead of re-selecting the same handful
forever.

No external API quota to worry about for either job shape (unlike
YouTube's confirmed 100 search.list calls/day) -- the per-invocation caps
below exist to bound one Lambda invocation's wall-clock time against
arbitrary, unpredictably slow third-party sites, not to respect a rate
limit. Retry-with-backoff (RETRY_TOTAL/RETRY_BACKOFF_FACTOR/
RETRY_STATUS_FORCELIST) is intentionally lighter than video_discovery's
YouTube-specific tuning -- this is generic web scraping against sites with
no known rate-limit behavior, so a handful of quick retries is a
reasonable default, not a response to a confirmed incident the way video_
discovery's retry config is.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEFAULT_PRICE_CHECK_LIMIT = 200
DEFAULT_FETCH_TIMEOUT_SECONDS = 20

# Discovery-side defaults. Generous relative to video_discovery's YouTube-
# quota-constrained MAX_SEARCHES_PER_INVOCATION=70 -- there's no daily quota
# to protect here (see module docstring), so these only need to bound wall-
# clock time for one Lambda invocation against however many configured
# sites there are.
DEFAULT_MAX_PRODUCTS_PER_DISCOVERY_INVOCATION = 100
DEFAULT_MAX_RESULTS_PER_SITE_SEARCH = 5

# BowlerDepot/BigCommerce API-fetch_method sites (016_price_tracking_
# bigcommerce.sql). Same API_BASE/PAGE_LIMIT contract as
# bowlerdepot_reconciliation, duplicated rather than imported -- see this
# module's own "each Lambda is its own deploy package" comment above
# _STOPWORDS. MAX_BIGCOMMERCE_IDS_PER_CALL is new here (bowlerdepot_
# reconciliation fetches the WHOLE catalog with no id filter at all, since
# it needs every product for fuzzy-matching; price_checker only ever needs
# a known, already-matched handful per invocation -- see
# fetch_bigcommerce_products_by_ids) -- kept comfortably under a URL-length-
# safe id:in filter list.
BIGCOMMERCE_API_BASE = "https://api.bigcommerce.com"
BIGCOMMERCE_PAGE_LIMIT = 250
MAX_BIGCOMMERCE_IDS_PER_CALL = 50

RETRY_TOTAL = 3
RETRY_BACKOFF_FACTOR = 1
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)

# Matches "$149.99", "149.99", "1,499.00 USD", preferring a cents-precision
# match over a bare integer so "16 lb" or a stray page number elsewhere in
# the selected text doesn't win over a real price -- see parse_price's own
# docstring for the two-pass reasoning.
_PRICE_WITH_CENTS_RE = re.compile(r"(\d[\d,]*\.\d{2})")
_PRICE_INTEGER_RE = re.compile(r"(\d[\d,]*)")

# Generic words stripped when building the "significant tokens" set used by
# score_match -- every product name contains these, so matching on them
# would make almost any bowling-related search result "high confidence".
# Copied from video_discovery.significant_tokens (see that module's own
# comment) rather than imported -- each Lambda here is its own deploy
# package (separate CodeUri/requirements.txt per function in template.yaml),
# so there's no shared layer to import across src/price_checker and
# src/video_discovery without introducing one just for a ~10-line helper.
_STOPWORDS = {"bowling", "ball", "the", "and", "a", "an", "of", "-", "/"}
_WORD_RE = re.compile(r"[a-z0-9]+")


def significant_tokens(name: str) -> set:
    """Lowercased alphanumeric tokens from a product/brand name, minus
    generic bowling-catalog stopwords. Used by score_match to decide if a
    search-result link's text plausibly refers to this specific product,
    not just to bowling balls in general. See video_discovery.
    significant_tokens -- identical logic, duplicated rather than shared
    (see this module's own comment above significant_tokens)."""
    if not name:
        return set()
    return {tok for tok in _WORD_RE.findall(name.lower()) if tok not in _STOPWORDS}


def score_match(title: str, brand_name: str, product_name: str) -> str:
    """Returns 'high' if the search-result link text contains the brand
    name AND at least one significant token from the product name; 'low'
    otherwise. Deliberately permissive (any one product-name token, not
    all of them), same reasoning and same known tradeoff as video_
    discovery.score_match (e.g. "Storm Absolute Power" review-video titles
    scoring 'high' for the "Storm Absolute" product too) -- this is
    exactly what the admin approval step exists to catch, not something
    this heuristic tries to eliminate on its own."""
    if not title:
        return "low"
    title_lower = title.lower()

    brand_tokens = significant_tokens(brand_name)
    brand_hit = bool(brand_tokens) and any(tok in title_lower for tok in brand_tokens)

    product_tokens = significant_tokens(product_name)
    product_hit = bool(product_tokens) and any(tok in title_lower for tok in product_tokens)

    return "high" if (brand_hit and product_hit) else "low"


def build_search_query(brand_name: str, product_name: str) -> str:
    return f"{brand_name} {product_name}"


def get_requests_session():
    """Fresh requests.Session with urllib3 Retry mounted on https, same
    shape as video_discovery.get_youtube_requests_session -- see this
    module's docstring for why the retry tuning itself is lighter/more
    generic here. A fresh session per call (not a module-level singleton)
    keeps this easy to monkeypatch in tests without cross-test state."""
    import requests
    from urllib3.util.retry import Retry

    session = requests.Session()
    retry = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=RETRY_STATUS_FORCELIST,
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_page(url: str, session=None, timeout: int = DEFAULT_FETCH_TIMEOUT_SECONDS) -> str:
    """Fetch raw HTML. Kept separate from parsing so tests can feed real
    fixture HTML without a network call -- same split product_scraper.
    fetch_page uses. Raises on a non-2xx response or network failure;
    check_price_source/search_site_for_product are the layers that catch
    this and turn it into an `error` string instead of blowing up the
    whole batch."""
    import requests

    sess = session or requests
    resp = sess.get(url, headers={"User-Agent": "bowling-scraper-price-checker/1.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def parse_price(text) -> float:
    """Extracts a dollar amount from arbitrary matched-element text (e.g.
    "$149.99", "Now: $129.99 (was $149.99)", "USD 89.00"). Two-pass:
    first look for a cents-precision match (\\d+.\\d{2}) since that's
    almost always the real price and not, say, a "16 lb" weight or a
    stray count elsewhere in the selected element; only fall back to a
    bare-integer match ("$150", no cents) if nothing with cents is
    present. Returns None (not 0) when nothing parseable is found --
    treated as a failed check by check_price_source, never silently
    recorded as a real $0.00 price.

    Where a page shows a range ("$99.99 - $109.99") or an original-vs-
    sale pair, this takes the FIRST match in document order -- usually
    the actual current/sale price a well-chosen selector already narrows
    down to. This function doesn't try to disambiguate further; a
    selector that's too broad is a site-setup problem to fix (narrow the
    CSS selector), not something to paper over with sale-price-detection
    heuristics here."""
    if not text:
        return None
    match = _PRICE_WITH_CENTS_RE.search(text)
    if match is None:
        match = _PRICE_INTEGER_RE.search(text)
    if match is None:
        return None
    try:
        return round(float(match.group(1).replace(",", "")), 2)
    except ValueError:
        return None


def extract_price(html: str, css_selector: str) -> dict:
    """Runs one CSS selector against the page and parses a price out of
    whatever text it matches. Returns {"price": ..., "raw_price_text":
    ..., "error": ...} -- price/raw_price_text are None and error is set
    on any failure (no element matched, or matched text had nothing
    parseable), never raises. Kept separate from fetch_page so tests can
    feed fixture HTML directly."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one(css_selector)
    if el is None:
        return {"price": None, "raw_price_text": None, "error": f"selector {css_selector!r} matched nothing"}

    # No separator between pieces of text (bs4 default) -- deliberately,
    # since a real e-commerce price is often split across sibling
    # elements at the decimal point ("<span>$99</span><span>.99</span>"
    # for a smaller cents superscript is a common pattern), and inserting
    # a space there would break the \d+\.\d{2} cents-precision match in
    # parse_price. Still collapsed via split()/join() afterward so
    # whitespace WITHIN a single text node (e.g. "  $149.99  ") doesn't
    # leak into raw_price_text.
    raw_text = " ".join(el.get_text(strip=True).split())
    price = parse_price(raw_text)
    if price is None:
        return {"price": None, "raw_price_text": raw_text, "error": f"could not parse a price from {raw_text!r}"}

    return {"price": price, "raw_price_text": raw_text, "error": None}


def check_price_source(source: dict, session=None) -> dict:
    """Fetches source['product_url'] and extracts a price via
    source['css_selector']. NEVER raises -- any failure (network error,
    HTTP error, timeout, bad selector, unparseable text) comes back as
    {"price": None, "raw_price_text": None, "error": "<reason>"} instead,
    same "one bad row can't stop the batch" convention this project uses
    everywhere a loop touches an external site (e.g.
    auto_approve_video_candidates.run(), video_discovery's per-product
    try/except)."""
    try:
        html = fetch_page(source["product_url"], session=session)
    except Exception as exc:
        return {"price": None, "raw_price_text": None, "error": f"fetch failed: {exc}"}

    try:
        return extract_price(html, source["css_selector"])
    except Exception as exc:
        return {"price": None, "raw_price_text": None, "error": f"parse failed: {exc}"}


def list_price_sources_due(conn, limit: int) -> list:
    """Only APPROVED + active sources (both the source row and its
    price_sites row must be is_active) -- a pending or rejected candidate
    is never checked for price, same as a pending/rejected product_videos
    row is never summarized. Ordered by last_checked_at asc nulls first --
    see module docstring's ROTATION section. css_selector is the
    effective one: the source's own override if set, else the site's
    default_css_selector.

    fetch_method/external_product_id (016_price_tracking_bigcommerce.sql)
    are joined in so check_sources can dispatch each row to the right
    check path without a second query -- an 'api' row's css_selector is
    simply unused (still selected for shape-consistency with the 'scrape'
    rows, harmless since it's null on an api-fetch_method price_sites
    row's default_css_selector)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select pps.id, pps.product_id, pps.product_url,
                   coalesce(pps.css_selector, ps.default_css_selector) as css_selector,
                   ps.fetch_method, pps.external_product_id
            from product_price_sources pps
            join price_sites ps on ps.id = pps.price_site_id
            where pps.status = 'approved' and pps.is_active = true and ps.is_active = true
            order by pps.last_checked_at asc nulls first, pps.id asc
            limit %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0], "product_id": r[1], "product_url": r[2], "css_selector": r[3],
            "fetch_method": r[4], "external_product_id": r[5],
        }
        for r in rows
    ]


def list_price_sources_for_products(conn, product_ids: list) -> list:
    """Same shape as list_price_sources_due, scoped to specific products
    instead of "most overdue" -- the admin-site per-product "check price
    now" button's target. No limit/rotation here: if a product has 5
    approved sites, all 5 get checked, since the caller explicitly asked
    for this product. fetch_method/external_product_id joined in for the
    same reason as list_price_sources_due -- see that function's own
    docstring.

    REAL INCIDENT: product_ids = any(%s) without an explicit ::uuid[]
    cast fails against a real Postgres instance -- "operator does not
    exist: uuid = text" -- since psycopg2 sends a plain Python list of
    strings as an untyped/text array parameter, and product_id is a uuid
    column. Found via a real {"product_ids": [...]} Lambda invoke against
    a real database (this function's own docstring and every test against
    it were written and passed against a fake cursor that never caught
    this, since a fake cursor doesn't type-check SQL). Same fix
    fetch_products_to_discover's own `p.id = any(%s::uuid[])` already
    uses -- that one was written with the cast from the start; this one,
    written earlier in the same feature, was missed."""
    if not product_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            select pps.id, pps.product_id, pps.product_url,
                   coalesce(pps.css_selector, ps.default_css_selector) as css_selector,
                   ps.fetch_method, pps.external_product_id
            from product_price_sources pps
            join price_sites ps on ps.id = pps.price_site_id
            where pps.status = 'approved' and pps.is_active = true and ps.is_active = true
              and pps.product_id = any(%s::uuid[])
            order by pps.product_id asc, pps.id asc
            """,
            (product_ids,),
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0], "product_id": r[1], "product_url": r[2], "css_selector": r[3],
            "fetch_method": r[4], "external_product_id": r[5],
        }
        for r in rows
    ]


def record_price_check(conn, source_id: str, result: dict) -> None:
    """Writes one product_price_history row (always -- see module
    docstring, a failed check still gets a row with `error` set) and
    bumps product_price_sources.last_checked_at so the rotation query
    above doesn't pick this source again until it's genuinely due.
    Deliberately two statements, not one CTE -- keeps this readable and
    matches every other "write history + touch a last-* pointer" write
    in this project (e.g. mark_product_searched is its own call, not
    folded into insert_candidates).

    cost_price/in_stock (016_price_tracking_bigcommerce.sql) are read via
    result.get(...) so this stays a no-op for a scrape-sourced result dict
    (check_price_source/extract_price never set either key, so both land
    as None/null, same as before this columns existed)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into product_price_history
                (price_source_id, price, raw_price_text, error, cost_price, in_stock)
            values (%s, %s, %s, %s, %s, %s)
            """,
            (
                source_id, result.get("price"), result.get("raw_price_text"), result.get("error"),
                result.get("cost_price"), result.get("in_stock"),
            ),
        )
        cur.execute(
            "update product_price_sources set last_checked_at = now() where id = %s",
            (source_id,),
        )
    conn.commit()


def check_sources(conn, sources: list, session=None) -> dict:
    """Runs check_price_source + record_price_check for every 'scrape'
    source (unchanged), and check_bigcommerce_sources + record_price_check
    for every 'api' source as ONE batched call (016_price_tracking_
    bigcommerce.sql / discover_bigcommerce_candidates' sibling on the
    checking side) rather than one BigCommerce API request per source --
    see check_bigcommerce_sources' own docstring for why batching matters
    here specifically. fetch_method defaults to 'scrape' via .get() so a
    source dict from before this column existed (or a test fixture that
    hasn't been updated) still routes correctly.

    Tolerates individual failures on both paths (already baked into
    check_price_source never raising, and check_bigcommerce_sources always
    returning a result -- possibly an error one -- for every source it was
    given) and returns one combined summary dict for handler's log line."""
    scrape_sources = [s for s in sources if s.get("fetch_method", "scrape") == "scrape"]
    api_sources = [s for s in sources if s.get("fetch_method") == "api"]

    checked = 0
    succeeded = 0
    failed = 0

    for source in scrape_sources:
        result = check_price_source(source, session=session)
        record_price_check(conn, source["id"], result)
        checked += 1
        if result.get("error"):
            failed += 1
        else:
            succeeded += 1

    if api_sources:
        results_by_source_id = check_bigcommerce_sources(conn, api_sources, session=session)
        for source in api_sources:
            result = results_by_source_id.get(
                source["id"], {"price": None, "raw_price_text": None, "error": "no result returned for this source"}
            )
            record_price_check(conn, source["id"], result)
            checked += 1
            if result.get("error"):
                failed += 1
            else:
                succeeded += 1

    return {"sources_checked": checked, "succeeded": succeeded, "failed": failed}


def get_bigcommerce_credentials():
    """Identical to bowlerdepot_reconciliation.get_bigcommerce_credentials
    -- duplicated, not imported/shared, same "each Lambda is its own
    deploy package" reasoning as significant_tokens/score_match above."""
    import boto3

    secret_arn = os.environ["BIGCOMMERCE_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    secret = json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])
    return secret["store_hash"], secret["auth_token"]


def build_bigcommerce_products_by_id_url(store_hash: str, ids: list, page: int = 1,
                                          limit: int = BIGCOMMERCE_PAGE_LIMIT) -> str:
    """Same v3 Catalog Products endpoint bowlerdepot_reconciliation.
    build_products_url uses, but scoped to specific product ids via
    BigCommerce's documented id:in filter -- price_checker already knows
    exactly which BigCommerce products it needs (from bowlerdepot_products,
    see list_bowlerdepot_matches) and, unlike the reconciliation job, has
    no reason to page through the entire catalog on every daily run.

    include=custom_fields,variants (017_price_tracking_sku_stock.sql --
    Al: "for the instock i was refering to actual number of each sku
    instock") -- each product response now embeds its own variants array
    (id, sku, inventory_level, option_values) in this SAME batched call,
    so per-SKU stock quantities cost nothing extra over one more query
    param; see match_sku_weights_to_variants for how those variants get
    matched back to our own product_skus rows."""
    ids_param = ",".join(str(i) for i in ids)
    return (
        f"{BIGCOMMERCE_API_BASE}/stores/{store_hash}/v3/catalog/products"
        f"?id:in={ids_param}&page={page}&limit={limit}&include=custom_fields,variants"
    )


def fetch_bigcommerce_products_by_ids(store_hash: str, auth_token: str, ids: list, session=None) -> dict:
    """Batches `ids` into groups of MAX_BIGCOMMERCE_IDS_PER_CALL (keeps
    each request's id:in query string comfortably bounded) and paginates
    each batch via meta.pagination.total_pages, same idiom as
    bowlerdepot_reconciliation.fetch_all_products. Returns
    {str(product_id): product_dict} rather than a list -- every caller
    here (discover_bigcommerce_candidates, check_bigcommerce_sources) just
    needs a by-id lookup, not document order. A product id BigCommerce no
    longer has (deleted/unpublished since bowlerdepot_products was last
    synced) simply isn't a key in the result -- callers treat a missing id
    as an error for that one source/candidate, not a reason to fail the
    whole batch."""
    import requests

    sess = session or requests
    products_by_id = {}

    for batch_start in range(0, len(ids), MAX_BIGCOMMERCE_IDS_PER_CALL):
        batch = ids[batch_start:batch_start + MAX_BIGCOMMERCE_IDS_PER_CALL]
        page = 1
        while True:
            resp = sess.get(
                build_bigcommerce_products_by_id_url(store_hash, batch, page=page),
                headers={"Accept": "application/json", "X-Auth-Token": auth_token},
                timeout=DEFAULT_FETCH_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            body = resp.json()
            for product in body.get("data", []):
                products_by_id[str(product["id"])] = product
            total_pages = body.get("meta", {}).get("pagination", {}).get("total_pages", page)
            if page >= total_pages:
                break
            page += 1

    return products_by_id


# 017_price_tracking_sku_stock.sql -- Al, clarifying 016's product-level
# in_stock boolean: "for the instock i was refering to actual number of
# each sku instock." determine_in_stock (product-level BigCommerce
# inventory_tracking/availability heuristic) is SUPERSEDED by
# determine_in_stock_from_sku_quantities below -- the old function is
# removed rather than left dead, since it was never actually a correct
# per-SKU signal to begin with (see its own former docstring's KNOWN
# CAVEAT: inventory_tracking='variant' inventory_level is a rollup across
# ALL weight variants, not the one weight this project sells as a SKU).
_WEIGHT_LABEL_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _extract_weight_from_variant(variant: dict):
    """Pulls a numeric weight (matching product_skus.weight_lbs' unit) out
    of one BigCommerce variant's option_values. BowlerDepot models weight
    as a variant option (see bowlerdepot_reconciliation's own docstring);
    the option's display name varies enough between BigCommerce catalogs
    ('Weight', 'Ball Weight', ...) that this matches on the option whose
    display name CONTAINS 'weight' first, and falls back to "the only
    option this variant has" when there's exactly one (the expected shape
    for this project, since weight is the only variant axis BowlerDepot
    lists) -- never guesses among multiple same-named-nothing options.
    Returns None (not a guess) when neither approach finds a candidate, or
    the candidate's own label has no parseable number in it, so callers
    can tell "no weight option on this variant" apart from "weight option
    present but somehow zero"."""
    option_values = variant.get("option_values") or []
    weight_option = None
    for option_value in option_values:
        display_name = (option_value.get("option_display_name") or "").lower()
        if "weight" in display_name:
            weight_option = option_value
            break
    if weight_option is None and len(option_values) == 1:
        weight_option = option_values[0]
    if weight_option is None:
        return None
    label = weight_option.get("label")
    if not label:
        return None
    match = _WEIGHT_LABEL_RE.search(str(label))
    return float(match.group(1)) if match else None


def match_sku_weights_to_variants(skus: list, variants: list) -> dict:
    """Pure matching logic, no DB/network -- pairs our own product_skus
    rows (list_product_skus_for_stock's per-product list) against one
    BigCommerce product's own variants array (now fetched alongside
    price/cost, see build_bigcommerce_products_by_id_url's include=
    ...,variants) by weight, rounded to 2 decimal places so a variant
    label like "14.0" or "14 lbs" still matches a weight_lbs of 14 without
    silently treating a genuinely different weight as the same SKU.

    Al: "the weights on bigcommerce should match what we have and if they
    don't something should keep track of that so we can fix whatever is
    causing the discrepency" -- our_only/bigcommerce_only below are
    exactly that discrepancy, in both directions, for
    write_sku_weight_mismatch_reviews to surface via review_queue.

    Returns:
      matched: [{"product_sku_id", "weight_lbs", "variant_id", "quantity"}, ...]
                quantity is variant.get("inventory_level") preserved as-is
                (including None -- "BigCommerce isn't tracking this
                variant's inventory," never coerced to 0; see 017's own
                migration comment on product_sku_stock_history.quantity).
      our_only: weight_lbs values we track that had no matching variant
                this check -- "we track a weight BigCommerce doesn't sell."
      bigcommerce_only: weight_lbs values from variants that matched no
                product_skus row -- "BigCommerce sells a weight we don't
                track."."""
    variant_weights = []
    for variant in variants:
        weight = _extract_weight_from_variant(variant)
        if weight is not None:
            variant_weights.append((round(weight, 2), variant))

    matched = []
    matched_variant_ids = set()
    our_only = []

    for sku in skus:
        weight = sku.get("weight_lbs")
        if weight is None:
            continue
        rounded = round(float(weight), 2)
        hit = next((v for w, v in variant_weights if w == rounded and v["id"] not in matched_variant_ids), None)
        if hit is None:
            our_only.append(weight)
            continue
        matched_variant_ids.add(hit["id"])
        matched.append({
            "product_sku_id": sku["id"],
            "weight_lbs": weight,
            "variant_id": hit["id"],
            "quantity": hit.get("inventory_level"),
        })

    bigcommerce_only = [w for w, v in variant_weights if v["id"] not in matched_variant_ids]

    return {"matched": matched, "our_only": our_only, "bigcommerce_only": bigcommerce_only}


def determine_in_stock_from_sku_quantities(quantities: list):
    """quantities: the list of quantity readings (int-or-None) for every
    matched SKU from THIS check (match_sku_weights_to_variants' own
    'matched' list, one entry's ["quantity"] per element). Al: "the
    current instock can still exist but should follow the quantities and
    once 0 it should be false." True if any matched SKU has a confirmed
    quantity above zero; False only once every matched SKU with a real
    (non-null) reading is confirmed at exactly zero; None (not a guess)
    when there's nothing usable to derive from at all -- no matched SKUs
    this check, or every matched SKU's own reading was itself unknown/null
    -- same "null means unknown, never collapsed into false" convention
    this module's parse_price/(the now-removed) determine_in_stock already
    followed."""
    known = [q for q in quantities if q is not None]
    if not known:
        return None
    return any(q > 0 for q in known)


def extract_bigcommerce_price_fields(product: dict, base_url: str = None) -> dict:
    """Pulls price/cost_price/product_url out of one BigCommerce product
    dict -- the API-fetch_method sibling of extract_price (scrape path).
    raw_price_text is always None here (there's no raw matched-text to
    preserve; the API returns a real numeric field, not scraped text to
    parse), kept as a key anyway so the returned dict has the same shape
    record_price_check/check_price_source's result dicts do.

    NO LONGER returns in_stock (017_price_tracking_sku_stock.sql) -- that
    key is now populated by check_bigcommerce_sources itself, from
    determine_in_stock_from_sku_quantities' per-SKU derivation, since this
    function only ever sees one product dict and has no reason to also
    know about product_skus/review_queue.

    product_url resolves product['custom_url']['url'] (BigCommerce's
    documented relative storefront path) against base_url via urljoin
    (already imported for parse_search_results); when base_url isn't
    configured on the price_sites row, or custom_url is missing, falls
    back to the raw relative path rather than raising -- still usable as
    an admin-UI link target, just not resolved to an absolute URL.

    cost_price variant fallback -- Al: "the cost for products if not on
    the product itself is on the variants. it should always be the same
    for all the variants so if we get 0 from the product and we grab it
    from one of the variants." BigCommerce commonly leaves a multi-
    variant product's own top-level cost_price at 0/unset and only sets
    real cost on each variant -- exactly this project's shape, since
    every ball is sold as several weight variants. When the product-level
    value is missing or exactly 0, this now falls back to the first
    variant (in whatever order BigCommerce returned them) whose own
    cost_price is a real, non-zero number -- Al confirmed cost is uniform
    across a product's variants, so which one doesn't matter, only that
    one has the real value. Both `variants` and `custom_fields` are
    already included on every product this function ever sees (see
    build_bigcommerce_products_by_id_url's include= param), so no new
    API call is needed for this."""
    price = product.get("price")
    cost_price = product.get("cost_price")
    if not cost_price:
        for variant in product.get("variants", []) or []:
            variant_cost = variant.get("cost_price")
            if variant_cost:
                cost_price = variant_cost
                break
    custom_url = (product.get("custom_url") or {}).get("url")
    if custom_url and base_url:
        product_url = urljoin(base_url, custom_url)
    else:
        product_url = custom_url

    return {
        "price": float(price) if price is not None else None,
        "cost_price": float(cost_price) if cost_price else None,
        "product_url": product_url,
        "raw_price_text": None,
        "error": None,
    }


def list_product_skus_for_stock(conn, product_ids: list) -> dict:
    """Returns {product_id: [{"id", "weight_lbs"}, ...]} for every
    product_skus row belonging to any of product_ids -- match_sku_weights_
    to_variants' "our side" input. A NEW query, not a reuse of
    bowlerdepot_reconciliation.get_product_skus -- that function selects
    different columns and has no `id` in its result, and this feature
    needs each row's own id as product_sku_stock_history.product_sku_id's
    FK target; see this module's own "each Lambda is its own deploy
    package" convention for why that's duplicated rather than shared.
    Same `= any(%s::uuid[])` cast list_price_sources_for_products already
    needed a real Postgres instance to catch missing (see that function's
    own docstring) -- written with the cast from the start here."""
    if not product_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, product_id, weight_lbs
            from product_skus
            where product_id = any(%s::uuid[])
            order by product_id asc, weight_lbs asc
            """,
            (product_ids,),
        )
        rows = cur.fetchall()
    by_product = {}
    for sku_id, product_id, weight_lbs in rows:
        by_product.setdefault(product_id, []).append({"id": sku_id, "weight_lbs": weight_lbs})
    return by_product


def record_sku_stock_history(conn, price_source_id: str, matched: list) -> None:
    """Writes one product_sku_stock_history row per matched SKU from THIS
    check (match_sku_weights_to_variants' own 'matched' list) -- the
    per-SKU-quantity sibling of record_price_check, same append-only
    "store the raw reading, compute deltas at read time" shape (see 017_
    price_tracking_sku_stock.sql's own header comment for the full "how do
    we answer 'how many sold/when restocked'" reasoning). A no-op (no
    rows, no commit) when matched is empty -- e.g. every one of this
    product's SKUs weight-mismatched this check, so there was nothing to
    record a quantity for."""
    if not matched:
        return
    with conn.cursor() as cur:
        for m in matched:
            cur.execute(
                """
                insert into product_sku_stock_history (product_sku_id, price_source_id, quantity)
                values (%s, %s, %s)
                """,
                (m["product_sku_id"], price_source_id, m["quantity"]),
            )
    conn.commit()


def _write_one_review_if_new(cur, product_id, field_name, current_value, proposed_value, reason) -> int:
    """DEDUP GUARD -- deliberate difference from bowwwl_cross_check.
    write_review_items and bowlerdepot_reconciliation's own review writer
    (both insert a fresh review_queue row unconditionally on every run):
    price_checker runs DAILY, not weekly, and an unresolved weight
    mismatch is expected to often persist across many checks -- inserting
    unconditionally here would write a fresh row every single day forever.
    Skips the insert if a PENDING row already exists for this exact
    (product_id, field_name, source='price_checker', current_value,
    proposed_value); an admin resolving that row (approve/reject) is what
    clears the way for a fresh one the next time the same mismatch is
    (re)found. coalesce(..., '') on both sides of the value comparison so
    a None on either side (only one direction is ever set per mismatch
    kind, see write_sku_weight_mismatch_reviews) compares equal to itself
    rather than SQL's usual "NULL = NULL is never true"."""
    cur.execute(
        """
        select id from review_queue
        where product_id = %s and field_name = %s and source = 'price_checker' and status = 'pending'
          and coalesce(current_value, '') = coalesce(%s, '') and coalesce(proposed_value, '') = coalesce(%s, '')
        """,
        (product_id, field_name, current_value, proposed_value),
    )
    if cur.fetchone():
        return 0
    cur.execute(
        """
        insert into review_queue (product_id, field_name, current_value, proposed_value, source, reason, status)
        values (%s, %s, %s, %s, 'price_checker', %s, 'pending')
        """,
        (product_id, field_name, current_value, proposed_value, reason),
    )
    return 1


def write_sku_weight_mismatch_reviews(conn, product_id: str, our_only: list, bigcommerce_only: list) -> int:
    """Surfaces match_sku_weights_to_variants' own our_only/bigcommerce_
    only lists as review_queue rows -- reusing review_queue rather than a
    new table, same role it already plays for bowlerdepot_reconciliation's
    accuracy mismatches and bowwwl_cross_check's own findings (both admin-
    resolvable via the same approve/reject/restore workflow, and GET
    /review-queue already surfaces any source= value with zero new
    admin_api work). source='price_checker' is new. Two distinct
    field_names (rather than one shared 'sku_weight') so an admin can
    filter/scan which direction a mismatch runs in without reading each
    row's own reason text. See _write_one_review_if_new for the dedup
    guard that makes this safe to call on every daily check."""
    if not our_only and not bigcommerce_only:
        return 0
    written = 0
    with conn.cursor() as cur:
        for weight in our_only:
            written += _write_one_review_if_new(
                cur, product_id, "sku_weight_missing_in_bigcommerce",
                current_value=str(weight), proposed_value=None,
                reason=f"We track a {weight} lb SKU that BigCommerce has no matching variant for.",
            )
        for weight in bigcommerce_only:
            written += _write_one_review_if_new(
                cur, product_id, "sku_weight_missing_in_our_catalog",
                current_value=None, proposed_value=str(weight),
                reason=f"BigCommerce sells a {weight} lb variant we have no matching SKU for.",
            )
    if written:
        conn.commit()
    return written


def record_sku_stock_and_get_in_stock(conn, product_id: str, price_source_id: str,
                                       skus: list, variants: list):
    """Orchestrates one source's own SKU-quantity side of a BigCommerce
    check: matches our SKUs against this product's variants, records a
    product_sku_stock_history row per match, surfaces any weight
    discrepancy to review_queue, and derives the product-level in_stock
    value THIS check should record -- the three things Al's design answer
    asked for in one call ("stored... efficiently", "current instock...
    should follow the quantities", "weights... should match... if they
    don't something should keep track of that"). Returns just the in_stock
    value (True/False/None); check_bigcommerce_sources is the only caller
    and only needs that one value back for this source's result dict."""
    match = match_sku_weights_to_variants(skus, variants)
    record_sku_stock_history(conn, price_source_id, match["matched"])
    if match["our_only"] or match["bigcommerce_only"]:
        write_sku_weight_mismatch_reviews(conn, product_id, match["our_only"], match["bigcommerce_only"])
    return determine_in_stock_from_sku_quantities([m["quantity"] for m in match["matched"]])


def check_bigcommerce_sources(conn, sources: list, session=None) -> dict:
    """The 'api' fetch_method sibling of check_price_source, batched over
    the WHOLE list of sources at once (one get_bigcommerce_credentials()
    call + one fetch_bigcommerce_products_by_ids() call covering every
    source's external_product_id, PLUS one list_product_skus_for_stock()
    call covering every source's product_id) rather than per-source --
    unlike a scrape site's product_url (an arbitrary third-party page, no
    reason two sources would ever share a request), BigCommerce's own API
    supports fetching many product ids in one call, and every 'api' source
    in this project is the same BowlerDepot store, so there's no reason to
    pay for N separate round-trips.

    Now takes `conn` (017_price_tracking_sku_stock.sql) -- the per-SKU
    quantity side (record_sku_stock_and_get_in_stock) needs to both read
    product_skus and write product_sku_stock_history/review_queue, unlike
    the price/cost side which was previously stateless here (writes only
    happened later, in record_price_check).

    NEVER raises, same "one bad row can't stop the batch" convention as
    check_price_source: if credentials are missing/invalid or the
    BigCommerce request itself fails, every source in this batch gets the
    same {"error": "..."} result rather than a partial/silent failure.
    A per-source SKU-matching/stock-recording failure (caught around just
    that one source's own call) never blocks that source's own price/cost
    result from still being recorded -- in_stock simply falls back to None
    (unknown) for that one source, never guessed at. Returns
    {source_id: result_dict} so check_sources can look up each source's
    own outcome after the fact."""
    try:
        store_hash, auth_token = get_bigcommerce_credentials()
    except Exception as exc:
        error = f"BigCommerce credentials unavailable: {exc}"
        return {source["id"]: {"price": None, "raw_price_text": None, "error": error} for source in sources}

    ids = [source["external_product_id"] for source in sources if source.get("external_product_id")]
    try:
        products_by_id = fetch_bigcommerce_products_by_ids(store_hash, auth_token, ids, session=session)
    except Exception as exc:
        error = f"BigCommerce fetch failed: {exc}"
        return {source["id"]: {"price": None, "raw_price_text": None, "error": error} for source in sources}

    product_ids = list({source["product_id"] for source in sources if source.get("product_id")})
    try:
        skus_by_product = list_product_skus_for_stock(conn, product_ids)
    except Exception:
        skus_by_product = {}

    results = {}
    for source in sources:
        external_id = source.get("external_product_id")
        if not external_id:
            results[source["id"]] = {"price": None, "raw_price_text": None, "error": "no external_product_id set"}
            continue
        product = products_by_id.get(str(external_id))
        if product is None:
            results[source["id"]] = {
                "price": None, "raw_price_text": None,
                "error": f"BigCommerce no longer has product id {external_id}",
            }
            continue
        # base_url isn't part of the source row itself (it's a price_sites-
        # level setting) -- callers that care about a resolved product_url
        # (discover_bigcommerce_candidates) call extract_bigcommerce_
        # price_fields directly with the site's base_url; here on the
        # checking path product_url is already fixed (product_price_
        # sources.product_url, set once at discovery time), so it's simply
        # not overwritten with a re-resolved value.
        fields = extract_bigcommerce_price_fields(product)
        try:
            skus = skus_by_product.get(source.get("product_id"), [])
            variants = product.get("variants", []) or []
            in_stock = record_sku_stock_and_get_in_stock(conn, source.get("product_id"), source["id"], skus, variants)
        except Exception:
            in_stock = None
        results[source["id"]] = {
            "price": fields["price"],
            "raw_price_text": fields["raw_price_text"],
            "error": fields["error"],
            "cost_price": fields["cost_price"],
            "in_stock": in_stock,
        }

    return results


# ---------------------------------------------------------------------
# Discovery: searches price_sites for candidate product URLs, mirroring
# video_discovery's search_youtube/score_match/insert_candidates/
# fetch_products_to_search/mark_product_searched architecture (see this
# module's own docstring for why -- Al's corrected "site config means
# which retailers, and matching should be automatic" instruction).
# ---------------------------------------------------------------------

def list_active_price_sites(conn) -> list:
    """The registry of sites discovery searches -- every is_active=true
    price_sites row. No pagination/limit here: this project's price_sites
    table is expected to stay small (a handful of named retailers an
    admin configures, not a large catalog), same assumption video_
    discovery makes about there being few enough YouTube-adjacent config
    rows to not need one either.

    fetch_method/api_provider/base_url (016_price_tracking_bigcommerce.sql)
    are selected so discover_price_sources can partition scrape vs api
    sites without a second query -- see that function's own docstring."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, name, search_url_template, result_link_selector, default_css_selector,
                   fetch_method, api_provider, base_url
            from price_sites
            where is_active = true
            order by name asc
            """
        )
        rows = cur.fetchall()
    return [
        {
            "id": r[0], "name": r[1], "search_url_template": r[2], "result_link_selector": r[3],
            "default_css_selector": r[4], "fetch_method": r[5], "api_provider": r[6], "base_url": r[7],
        }
        for r in rows
    ]


def parse_search_results(html: str, result_link_selector: str, base_url: str, max_results: int) -> list:
    """Runs a site's result_link_selector against its search-results page
    and returns up to max_results {product_url, title} dicts, in document
    order (mirrors video_discovery's parse_search_response returning
    search.list's own relevance-ranked order). href is resolved against
    base_url via urljoin since a site's search results very commonly use
    relative hrefs; an anchor with no href, or one that resolves to an
    empty string, is skipped rather than stored as a candidate with a
    useless URL. Deduplicated by resolved URL -- a site's markup
    occasionally wraps both a thumbnail and a title in separate <a> tags
    matching the same selector, pointing at the identical product page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen_urls = set()
    for link in soup.select(result_link_selector):
        href = link.get("href")
        if not href:
            continue
        product_url = urljoin(base_url, href)
        if not product_url or product_url in seen_urls:
            continue
        seen_urls.add(product_url)
        title = " ".join(link.get_text(strip=True).split())
        results.append({"product_url": product_url, "title": title})
        if len(results) >= max_results:
            break
    return results


def search_site_for_product(site: dict, query: str, session=None,
                             max_results: int = DEFAULT_MAX_RESULTS_PER_SITE_SEARCH) -> list:
    """One site-search request against site['search_url_template'] (the
    {query} placeholder is url-encoded via quote_plus before
    substitution -- product names routinely contain spaces/slashes/
    ampersands that would otherwise corrupt the URL). Returns
    parse_search_results' shape. Kept separate from the DB/looping logic
    (discover_price_sources) so tests can feed canned HTML without a
    network call, same split search_youtube uses relative to handler."""
    search_url = site["search_url_template"].format(query=quote_plus(query))
    html = fetch_page(search_url, session=session)
    return parse_search_results(html, site["result_link_selector"], search_url, max_results)


def fetch_products_to_discover(conn, job: dict, max_products: int) -> list:
    """Resolves the job's scope into a list of {id, name, brand_name}
    dicts, capped at max_products -- identical shape and scoping rules to
    video_discovery.fetch_products_to_search (see that function's own
    docstring for the reasoning behind each piece: the explicit ::uuid[]
    cast for product_ids, why published=true is deliberately not
    required, and the last_price_discovery_at asc nulls first rotation
    ordering, mirroring last_video_discovery_at -- see 015_products_last_
    price_discovery_at.sql)."""
    query = """
        select p.id, p.name, b.name as brand_name
        from products p
        join brands b on b.id = p.brand_id
    """
    params = []
    conditions = []

    product_ids = job.get("product_ids")
    brand_id = job.get("brand_id")

    if product_ids:
        conditions.append("p.id = any(%s::uuid[])")
        params.append(list(product_ids))
    else:
        conditions.append("p.status = 'current'")
        if brand_id:
            conditions.append("p.brand_id = %s")
            params.append(brand_id)

    if conditions:
        query += " where " + " and ".join(conditions)

    if product_ids:
        query += " order by p.id asc limit %s"
    else:
        query += " order by p.last_price_discovery_at asc nulls first, p.id asc limit %s"
    params.append(max_products)

    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def mark_product_price_discovery_searched(conn, product_id: str) -> None:
    """Records that discovery actually completed a search pass (one
    site-search attempt per active price_sites row) for this product --
    called from discover_price_sources' success path only, same "don't
    credit a product as covered when the pass didn't really complete"
    reasoning as video_discovery.mark_product_searched."""
    with conn.cursor() as cur:
        cur.execute(
            "update products set last_price_discovery_at = now() where id = %s",
            (product_id,),
        )
    conn.commit()


def insert_price_source_candidates(conn, product_id: str, price_site_id: str, query: str, candidates: list,
                                    source: str = "site_search") -> int:
    """Inserts one product_price_sources row per candidate, tagged
    'pending' with the given source (default 'site_search', preserving
    every existing scrape-path caller's behavior unchanged) -- mirrors
    video_discovery.insert_candidates. ON CONFLICT DO NOTHING makes this
    idempotent against re-running discovery for the same product+site
    (unique (product_id, price_site_id, product_url) from 014_price_
    tracking.sql) -- a candidate URL already stored (in any status) is
    left untouched rather than reset back to pending, same as an already-
    resolved product_videos row is never silently reopened by a
    re-search.

    external_product_id (016_price_tracking_bigcommerce.sql, via
    candidate.get(...) so a plain scrape-search result dict without that
    key still inserts fine as null) is the one other field an 'api'-source
    candidate carries that a 'site_search' one never does -- see
    discover_bigcommerce_candidates, source='bigcommerce_api'."""
    inserted = 0
    with conn.cursor() as cur:
        for candidate in candidates:
            cur.execute(
                """
                insert into product_price_sources
                    (product_id, price_site_id, product_url, match_query,
                     match_confidence, external_product_id, status, source)
                values (%s, %s, %s, %s, %s, %s, 'pending', %s)
                on conflict (product_id, price_site_id, product_url) do nothing
                """,
                (
                    product_id, price_site_id, candidate["product_url"], query,
                    candidate["match_confidence"], candidate.get("external_product_id"), source,
                ),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted


def upsert_bigcommerce_price_source_candidate(conn, product_id: str, price_site_id: str, product_url: str,
                                               external_product_id: str, query: str, match_confidence: str) -> int:
    """The 'bigcommerce_api' sibling of insert_price_source_candidates,
    used ONLY by discover_bigcommerce_candidates -- NOT a general
    replacement, since a 'site_search' (scrape) site can legitimately
    produce several distinct candidate URLs per product (multiple search
    results for an admin to pick from), while an 'api' site's discovery
    always resolves to exactly one BowlerDepot match per product (see
    discover_bigcommerce_candidates' own docstring), so (product_id,
    price_site_id) is really a unique key for this source specifically,
    even though the table's own constraint is (product_id, price_site_id,
    product_url).

    Real bug found live: insert_price_source_candidates' ON CONFLICT is
    keyed on the literal product_url TEXT. extract_bigcommerce_price_
    fields resolves product_url against price_sites.base_url, so the
    moment an admin fills in a previously-blank base_url (Al: "i can't
    remember if i put in the base url"), the very next discovery run
    computes a different (now-absolute) product_url for a product+site
    pair that already had a row -- the conflict target doesn't match the
    old row's stale (relative) product_url, so a brand-new duplicate row
    gets INSERTed instead of the existing one being corrected in place.
    Al: "there are duplicates now, the ones before having the baseurl and
    now the ones that have it... same record just has different link."
    See service.dedupe_product_price_sources (admin_api) for the one-off
    cleanup of rows that already duplicated before this fix shipped.

    Fix: look up any existing row for (product_id, price_site_id,
    source='bigcommerce_api') FIRST. If one exists and its product_url
    differs from the freshly-resolved value, UPDATE it in place (also
    refreshing external_product_id, in case BowlerDepot's own id for this
    product changed) -- status/is_active/resolved_at and all history stay
    untouched, exactly like fixing a stale link should behave. If none
    exists yet, INSERT a new pending row, same shape
    insert_price_source_candidates already writes (still guarded by that
    same ON CONFLICT DO NOTHING as a defensive fallback, in case two
    concurrent discovery runs race each other).

    Returns 1 if a row was inserted OR corrected, 0 if an existing row's
    product_url already matched (nothing to do) -- discover_bigcommerce_
    candidates sums this the same way it summed insert_price_source_
    candidates' return value before."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, product_url from product_price_sources
            where product_id = %s and price_site_id = %s and source = 'bigcommerce_api'
            order by (status = 'approved' and is_active) desc, created_at asc
            limit 1
            """,
            (product_id, price_site_id),
        )
        existing = cur.fetchone()

        if existing is not None:
            existing_id, existing_url = existing
            if existing_url == product_url:
                return 0
            cur.execute(
                "update product_price_sources set product_url = %s, external_product_id = %s where id = %s",
                (product_url, external_product_id, existing_id),
            )
            conn.commit()
            return 1

        cur.execute(
            """
            insert into product_price_sources
                (product_id, price_site_id, product_url, match_query,
                 match_confidence, external_product_id, status, source)
            values (%s, %s, %s, %s, %s, %s, 'pending', 'bigcommerce_api')
            on conflict (product_id, price_site_id, product_url) do nothing
            """,
            (product_id, price_site_id, product_url, query, match_confidence, external_product_id),
        )
        changed = cur.rowcount
    conn.commit()
    return changed


def list_bowlerdepot_matches(conn) -> list:
    """Reads bowlerdepot_reconciliation's already-maintained product_id <->
    BigCommerce-product-id mapping (001_init_schema.sql's bowlerdepot_
    products table, kept fresh by that Lambda's own daily schedule) --
    see 016_price_tracking_bigcommerce.sql's header comment for the full
    "why reuse this instead of re-deriving fuzzy matching" rationale.
    match_status in ('matched', 'ambiguous') excludes the schema-default
    'unmatched' rows (a real match attempt that didn't clear
    fuzzy_match_product's threshold -- not something to track a price
    for), and product_id is not null excludes the 'not yet listed' case
    (that's a name-only row with nothing to match a price source to)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select product_id, bigcommerce_product_id, match_status
            from bowlerdepot_products
            where product_id is not null and match_status in ('matched', 'ambiguous')
            """
        )
        rows = cur.fetchall()
    return [
        {"product_id": r[0], "external_product_id": r[1], "match_status": r[2]}
        for r in rows
    ]


def discover_bigcommerce_candidates(conn, site: dict, product_ids_in_scope: set, session=None) -> dict:
    """The 'api' fetch_method sibling of the per-product/per-site scrape
    search loop in discover_price_sources -- but shaped completely
    differently, since an API-backed site has no search page to crawl at
    all (016_price_tracking_bigcommerce.sql). Instead of one request per
    product, this does ONE get_bigcommerce_credentials() call + ONE
    batched fetch_bigcommerce_products_by_ids() call covering every
    already-matched product in scope (via list_bowlerdepot_matches,
    filtered down to product_ids_in_scope), then upserts one candidate per
    match via upsert_bigcommerce_price_source_candidate -- NOT plain
    insert_price_source_candidates (see that function's own docstring for
    why: a product_url text change, e.g. from a price_sites row's
    base_url getting filled in after the fact, must correct the existing
    row in place instead of silently creating a duplicate). match_
    confidence is derived straight from bowlerdepot_products.match_status
    -- 'matched' (an exact normalized-name match, see bowlerdepot_
    reconciliation.fuzzy_match_product) becomes 'high', 'ambiguous' (a
    fuzzy-but-not-exact match) becomes 'low' -- same two-tier idea
    score_match uses for scrape candidates, just sourced from an existing
    match decision instead of a fresh title heuristic.

    A match with no corresponding BigCommerce product in the fetched batch
    (deleted/unpublished since bowlerdepot_products was last synced) or no
    resolvable product_url is skipped and counted as an error, never
    raised -- same "one bad row can't stop the batch" convention as the
    scrape-side loop this sits alongside.

    "inserted" in the returned dict now means "created or corrected" --
    see upsert_bigcommerce_price_source_candidate's own return-value note."""
    matches = [m for m in list_bowlerdepot_matches(conn) if m["product_id"] in product_ids_in_scope]
    if not matches:
        return {"inserted": 0, "errors": 0}

    try:
        store_hash, auth_token = get_bigcommerce_credentials()
    except Exception:
        logger.exception("BigCommerce credentials unavailable for price-source discovery on site=%r", site["name"])
        return {"inserted": 0, "errors": len(matches)}

    ids = [m["external_product_id"] for m in matches]
    try:
        products_by_id = fetch_bigcommerce_products_by_ids(store_hash, auth_token, ids, session=session)
    except Exception:
        logger.exception("BigCommerce fetch failed for price-source discovery on site=%r", site["name"])
        return {"inserted": 0, "errors": len(matches)}

    inserted = 0
    errors = 0
    for match in matches:
        product = products_by_id.get(str(match["external_product_id"]))
        if product is None:
            errors += 1
            continue

        fields = extract_bigcommerce_price_fields(product, base_url=site.get("base_url"))
        if not fields["product_url"]:
            errors += 1
            continue

        inserted += upsert_bigcommerce_price_source_candidate(
            conn, match["product_id"], site["id"], fields["product_url"],
            match["external_product_id"], "bowlerdepot_products match",
            "high" if match["match_status"] == "matched" else "low",
        )

    return {"inserted": inserted, "errors": errors}


def discover_price_sources(conn, job: dict, session=None) -> dict:
    """{"discover": true, ...} job entry point (see handler and this
    module's own docstring's DISCOVERY section). For every product in
    scope, searches every active 'scrape' price_sites row and stores each
    result as a 'pending' product_price_sources candidate, scored via
    score_match. A search failure against one site for one product is
    logged and counted, never raised -- same "one bad row can't stop the
    batch" convention check_sources already uses on the checking side,
    now applied to the search side too.

    'api' price_sites rows (016_price_tracking_bigcommerce.sql) are
    handled separately via discover_bigcommerce_candidates, BEFORE the
    per-product scrape loop -- one batched pass per api site covering
    every product in scope at once, rather than one iteration per product
    per site the way the generic scrape search has to work. A failure
    there is logged and counted into the same search_errors total, never
    raised, so a BigCommerce outage can't block scrape-site discovery for
    the same invocation."""
    max_products = int(os.environ.get(
        "MAX_PRODUCTS_PER_DISCOVERY_INVOCATION", DEFAULT_MAX_PRODUCTS_PER_DISCOVERY_INVOCATION
    ))
    max_results = int(os.environ.get(
        "MAX_RESULTS_PER_SITE_SEARCH", DEFAULT_MAX_RESULTS_PER_SITE_SEARCH
    ))

    products = fetch_products_to_discover(conn, job, max_products)
    sites = list_active_price_sites(conn)
    scrape_sites = [s for s in sites if s.get("fetch_method", "scrape") == "scrape"]
    api_sites = [s for s in sites if s.get("fetch_method") == "api"]
    logger.info("Discovering price sources for %d product(s) across %d site(s)", len(products), len(sites))

    total_candidates = 0
    search_errors = 0

    if api_sites:
        product_ids_in_scope = {p["id"] for p in products}
        for site in api_sites:
            try:
                result = discover_bigcommerce_candidates(conn, site, product_ids_in_scope, session=session)
            except Exception:
                logger.exception("BigCommerce price-source discovery failed for site=%r", site["name"])
                search_errors += len(product_ids_in_scope)
                continue
            total_candidates += result["inserted"]
            search_errors += result["errors"]
            logger.info(
                "site=%r (api) -> %d new candidates, %d errors",
                site["name"], result["inserted"], result["errors"],
            )

    for product in products:
        query = build_search_query(product["brand_name"], product["name"])
        for site in scrape_sites:
            try:
                results = search_site_for_product(site, query, session=session, max_results=max_results)
            except Exception:
                logger.exception(
                    "Price-source search failed for product_id=%s site=%r query=%r",
                    product["id"], site["name"], query,
                )
                search_errors += 1
                continue

            for result in results:
                result["match_confidence"] = score_match(result["title"], product["brand_name"], product["name"])

            inserted = insert_price_source_candidates(conn, product["id"], site["id"], query, results)
            total_candidates += inserted
            logger.info(
                "product_id=%s site=%r query=%r -> %d results, %d new candidates",
                product["id"], site["name"], query, len(results), inserted,
            )

        mark_product_price_discovery_searched(conn, product["id"])

    return {
        "products_searched": len(products),
        "sites_searched": len(sites),
        "new_candidates": total_candidates,
        "search_errors": search_errors,
    }


def get_db_connection():
    import boto3
    import psycopg2

    secret_arn = os.environ["DB_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    secret = json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])

    return psycopg2.connect(
        host=secret["host"],
        port=secret.get("port", 5432),
        dbname=secret["dbname"],
        user=secret["username"],
        password=secret["password"],
    )


def handler(event, context):
    job = event or {}
    session = get_requests_session()

    conn = get_db_connection()
    try:
        # {"discover": true} is a completely different job shape from the
        # checking flow below -- searches for new candidate sources rather
        # than checking existing approved ones -- so it's handled as an
        # early return, same pattern video_discovery.handler uses for its
        # own {"refresh_stats": true} early return.
        if job.get("discover"):
            result = discover_price_sources(conn, job, session=session)
            logger.info("Discovery complete: %s", result)
            return {"statusCode": 200, "body": json.dumps(result)}

        product_ids = job.get("product_ids")
        if product_ids:
            sources = list_price_sources_for_products(conn, product_ids)
            logger.info("Checking %d price source(s) for %d product(s)", len(sources), len(product_ids))
        else:
            limit = int(job.get("limit") or DEFAULT_PRICE_CHECK_LIMIT)
            sources = list_price_sources_due(conn, limit)
            logger.info("Checking %d price source(s) (batch limit=%d)", len(sources), limit)

        result = check_sources(conn, sources, session=session)
        logger.info("Price check complete: %s", result)
    finally:
        conn.close()

    return {"statusCode": 200, "body": json.dumps(result)}
