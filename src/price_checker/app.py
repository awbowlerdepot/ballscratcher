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
    default_css_selector."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select pps.id, pps.product_id, pps.product_url,
                   coalesce(pps.css_selector, ps.default_css_selector) as css_selector
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
        {"id": r[0], "product_id": r[1], "product_url": r[2], "css_selector": r[3]}
        for r in rows
    ]


def list_price_sources_for_products(conn, product_ids: list) -> list:
    """Same shape as list_price_sources_due, scoped to specific products
    instead of "most overdue" -- the admin-site per-product "check price
    now" button's target. No limit/rotation here: if a product has 5
    approved sites, all 5 get checked, since the caller explicitly asked
    for this product."""
    if not product_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            select pps.id, pps.product_id, pps.product_url,
                   coalesce(pps.css_selector, ps.default_css_selector) as css_selector
            from product_price_sources pps
            join price_sites ps on ps.id = pps.price_site_id
            where pps.status = 'approved' and pps.is_active = true and ps.is_active = true
              and pps.product_id = any(%s)
            order by pps.product_id asc, pps.id asc
            """,
            (product_ids,),
        )
        rows = cur.fetchall()
    return [
        {"id": r[0], "product_id": r[1], "product_url": r[2], "css_selector": r[3]}
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
    folded into insert_candidates)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into product_price_history (price_source_id, price, raw_price_text, error)
            values (%s, %s, %s, %s)
            """,
            (source_id, result.get("price"), result.get("raw_price_text"), result.get("error")),
        )
        cur.execute(
            "update product_price_sources set last_checked_at = now() where id = %s",
            (source_id,),
        )
    conn.commit()


def check_sources(conn, sources: list, session=None) -> dict:
    """Runs check_price_source + record_price_check for every source,
    tolerating individual failures (already baked into check_price_source
    never raising) and returning a summary dict for handler's log line."""
    checked = 0
    succeeded = 0
    failed = 0
    for source in sources:
        result = check_price_source(source, session=session)
        record_price_check(conn, source["id"], result)
        checked += 1
        if result.get("error"):
            failed += 1
        else:
            succeeded += 1
    return {"sources_checked": checked, "succeeded": succeeded, "failed": failed}


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
    rows to not need one either."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, name, search_url_template, result_link_selector, default_css_selector
            from price_sites
            where is_active = true
            order by name asc
            """
        )
        rows = cur.fetchall()
    return [
        {"id": r[0], "name": r[1], "search_url_template": r[2], "result_link_selector": r[3], "default_css_selector": r[4]}
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


def insert_price_source_candidates(conn, product_id: str, price_site_id: str, query: str, candidates: list) -> int:
    """Inserts one product_price_sources row per candidate, tagged
    'pending' with source='site_search' -- mirrors video_discovery.
    insert_candidates. ON CONFLICT DO NOTHING makes this idempotent
    against re-running discovery for the same product+site (unique
    (product_id, price_site_id, product_url) from 014_price_tracking.sql)
    -- a candidate URL already stored (in any status) is left untouched
    rather than reset back to pending, same as an already-resolved
    product_videos row is never silently reopened by a re-search."""
    inserted = 0
    with conn.cursor() as cur:
        for candidate in candidates:
            cur.execute(
                """
                insert into product_price_sources
                    (product_id, price_site_id, product_url, match_query,
                     match_confidence, status, source)
                values (%s, %s, %s, %s, %s, 'pending', 'site_search')
                on conflict (product_id, price_site_id, product_url) do nothing
                """,
                (
                    product_id, price_site_id, candidate["product_url"], query,
                    candidate["match_confidence"],
                ),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted


def discover_price_sources(conn, job: dict, session=None) -> dict:
    """{"discover": true, ...} job entry point (see handler and this
    module's own docstring's DISCOVERY section). For every product in
    scope, searches every active price_sites row and stores each result
    as a 'pending' product_price_sources candidate, scored via
    score_match. A search failure against one site for one product is
    logged and counted, never raised -- same "one bad row can't stop the
    batch" convention check_sources already uses on the checking side,
    now applied to the search side too."""
    max_products = int(os.environ.get(
        "MAX_PRODUCTS_PER_DISCOVERY_INVOCATION", DEFAULT_MAX_PRODUCTS_PER_DISCOVERY_INVOCATION
    ))
    max_results = int(os.environ.get(
        "MAX_RESULTS_PER_SITE_SEARCH", DEFAULT_MAX_RESULTS_PER_SITE_SEARCH
    ))

    products = fetch_products_to_discover(conn, job, max_products)
    sites = list_active_price_sites(conn)
    logger.info("Discovering price sources for %d product(s) across %d site(s)", len(products), len(sites))

    total_candidates = 0
    search_errors = 0

    for product in products:
        query = build_search_query(product["brand_name"], product["name"])
        for site in sites:
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
