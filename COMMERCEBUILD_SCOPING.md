COMMERCEBUILD_SCOPING.md — Storm / Roto Grip / 900 Global

Scoping notes for the fourth manufacturer platform, following up on the
gap identified after DEPLOY_RUNBOOK.md steps 6f/6g closed out: the
original architecture doc's platform list was Craft CMS / Shopify /
commercebuild, and commercebuild was never built. This file is research
only -- no code yet. Everything below comes from real fetches against
live stormbowling.com pages this session (fetched via a readability/
markdown-converting tool, same caveat this project has hit before with
Brunswick: that tool does NOT return the literal bytes `requests.get()`
would see, so anything marked "NEEDS RAW HTML CONFIRM" below is a real
open risk, not a settled fact -- see "Open questions" at the end).

## Platform

All three brands (Storm, Roto Grip, 900 Global) live on ONE site,
stormbowling.com, running on "commercebuild" (footer: "Copyright ©
commercebuild Ltd.", robots.txt header: "# XM Symphony robots.txt file" --
commercebuild appears to be built on/branded as XM Symphony). This is a
meaningfully different shape than the Craft CMS family (Brunswick/
Radical/DV8 = one stack per brand, same template): here it's one stack,
three brands, distinguished by a `**Brand:**` field on each product page
and a `custom1` filter facet on the category listing.

robots.txt sets `Crawl-delay: 10` for all user-agents (10 seconds between
requests) -- Brunswick's site had no such directive. Should add an
explicit delay/rate-limit in this scraper's fetch loop, unlike the
existing product scrapers.

## URL discovery

No flat, guessable URL pattern -- confirmed inconsistent even within one
brand:
- `storm-alpha-crux-bowling-ball` (brand prefix + name + "-bowling-ball")
- `storm-crux-ball`, `storm-omega-crux-ball` (no "-bowling-ball" suffix)
- `storm-alpha-crux-2016` (no suffix, no "ball" at all)
- `roto-grip-gremlin-bowling-ball` (canonical) vs.
  `/products/equipment/bowling-balls/bbmrgx-gremlin` (SKU-prefixed path
  that redirects/canonicalizes to the flat URL -- both work as a fetch
  target)

Two viable discovery sources, matching this project's usual sitemap-or-
category-crawl pattern:

1. **Category listing pagination** (like SWAG/WooCommerce's approach):
   `https://www.stormbowling.com/products/equipment/bowling-balls/?per_page=24&sort_by=1&filter[custom1][0]={Storm|Roto-Grip|900-Global}`
   Confirmed real counts via the page's own facet list: Storm (41),
   Roto Grip (15), 900 Global (5) -- 61 current balls total.
   Retired/discontinued balls live in a separate "Bowling Balls Archive"
   collection: `https://www.stormbowling.com/products/collections/bowling-balls-archive/`,
   confirmed real, 22 pages at 24/page (~500+ items, spanning years of
   discontinued balls across all three brands mixed together, not
   separated by current1 filter on this specific collection page as
   fetched). This is a much bigger retired catalog than Brunswick's.

2. **sitemap_index.xml**: confirmed to exist at
   `https://www.stormbowling.com/sitemap_index.xml` but the web-fetch tool
   returned it as unparsed binary (likely gzip) -- NEEDS RAW HTML CONFIRM
   to actually see if it offers `<lastmod>` values (which is what made
   Brunswick's Craft CMS sitemap useful for the new-vs-updated diff logic
   `diff_against_known` already relies on elsewhere in this project).

Recommendation: category-listing pagination for the primary current/
retired product list (proven to work, gives per-brand counts directly via
the `custom1` filter), sitemap only as a secondary lastmod source if the
raw-XML check confirms it's useful.

## Product page shape

Confirmed real on two products (Storm Alpha Crux, Roto Grip Gremlin) --
both share the same label:value spec block:

    **Brand:** Storm
    **Line:** Premier
    **Core:** S_AI
    **Weight Block:** S_Catalyst_AI
    **Finish:** S_2000 Grit
    **Durometer:** S_73-75
    **Symmetry:** S_Asymmetrical
    **Differential:** 0.052
    **Flare Potential:** S_High
    **Radius of Gyration:** 2.48
    **Weight:** 16
    **Coverstock:** S_GI26_Solid
    **Color:** Black/Turquoise/Violet
    **Release Date:** 05/29/26
    **Fragrance:** Apple Fritter
    **Avail. for Sales Orders:** Yes
    **PSA:** 0.017

Roto Grip's Gremlin page had the same fields plus two Storm's page didn't
have (`MatchMaker App`, `MatchMaker`) -- fields are not 100% uniform
across products, parsing needs to be label-driven and tolerant of
missing/extra fields, same philosophy as this project's existing Brunswick
scraper (match by visible label text, not position/CSS class).

**Open risk, not yet resolved: only ONE weight (16lb) appears in the
visible spec block on both products checked.** Real bowling balls in this
lineup ship in multiple weights (12-16lb), so either (a) weight is a
BigCommerce/commercebuild-style variant selector that AJAX-swaps the RG/
Differential/Weight fields via JS after page load -- meaning the raw HTML
`requests.get()` would receive might only ever contain the default (16lb)
variant's numbers, same failure mode Brunswick would have hit if its
weight table had been JS-driven -- or (b) there's a hidden JSON blob
powering that variant switcher that a raw-HTML parse could still extract
without executing JS. **NEEDS RAW HTML CONFIRM** before deciding whether
this scraper can get all weights from the product page alone, or needs
the same "HTML get one weight + PDF gets the rest, cross-check between
them" pattern this project already built for Brunswick
(pdf_parser/find_mismatches).

**Working hypothesis, needs confirmation, not yet proven:** every product
checked so far has a "Tech Data" PDF in its Downloads section (`Alpha
Crux Tech Data.pdf`, `Tech Doc_HP3_GREMLIN.pdf`) -- naming varies per
product, but the presence of a dedicated spec-sheet PDF is consistent so
far (n=2). If that PDF contains the full per-weight RG/Differential/mass-
bias breakdown (unconfirmed -- these are PDFs, need the pdf skill or a
direct fetch+parse to check), this maps directly onto the existing
pdf_parser architecture already built and battle-tested for Brunswick:
HTML gives one reference value, PDF gives the full table, mismatches
between them get flagged the same way.

## Images

Main product photo comes from `assets.1.commercebuild.com`, e.g.
`.../contents/BBMVXA/BBMVXA.png` -- no `data:` placeholder or `srcset`-
only pattern spotted on the two live/current products checked, unlike the
real bug just fixed in Brunswick's `parse_images()`. **Still needs the
same raw-HTML check** before trusting this, given that exact bug was
invisible in the readability-tool view for Brunswick too until a literal
curl exposed it.

Separately (not a bug, a real archive-catalog quirk): many retired/
archived balls on the Bowling Balls Archive listing use a generic
`ajax-loader.gif` or `coming_soon.jpg` placeholder image instead of a
`data:` URI -- these are genuinely-missing photos for old discontinued
balls, not a lazy-load artifact. The scraper should recognize and skip
these (e.g. filename match on `ajax-loader.gif`/`coming_soon.jpg`) rather
than storing them as a real source_url, same spirit as skipping `data:`
URIs in the Brunswick fix but a different real cause.

## Brand / deploy architecture decision (needs a decision, not just research)

Because all three brands share one site and one page template, this
doesn't need the Craft-CMS pattern of "redeploy the same stack with a
different BrandId parameter." Two real options:

1. **One Lambda deployment, three brand rows**: url_discovery runs three
   category-listing crawls (one per `custom1` filter value) or one crawl
   that reads each product's own `**Brand:**` field to route it to the
   right `brands.id` at write time. Matches how this file's research was
   actually done (Storm vs Roto Grip pages differ only in that `Brand:`
   field's value, not in template shape).
2. Three separate BrandId-parameterized deployments like the Craft CMS
   family -- doesn't fit as well here since it's genuinely one site, not
   three separate manufacturer domains.

Recommendation: option 1. Flagging as a decision point rather than just
picking it, since it's a real architectural choice, not purely a
technical-verification question like the others above.

## Open questions requiring real raw-HTML confirmation before writing code

Same "verify against reality" pattern this project has used throughout
(the pdf_parser Decimal bug, the lazy-load placeholder bug, the day-
precision release-date bug -- all real bugs found via literal raw
requests, never via the readability-tool view alone). Before building:

1. Does the raw (non-JS) HTML for a product page expose all weights'
   RG/Differential/mass-bias values anywhere (hidden variant-selector
   data), or genuinely just the one default weight? Determines whether
   the "Tech Data" PDF is required reading, not optional.
2. Pull one "Tech Data" PDF and confirm it actually contains a per-weight
   breakdown (vs. just marketing copy).
3. Does any product's raw `<img>` tag use a `data:` placeholder + real
   `srcset`, like Brunswick's bug? Check at least one current-brand
   product from each of the three brands.
4. Parse `sitemap_index.xml` for real (gunzip if needed) -- does it cover
   product pages, and does it carry `<lastmod>`?
5. Spot-check one 900 Global product page (only Storm and Roto Grip
   checked so far) to confirm the same template applies to all three
   brands, not just two of them.

Next step: get real raw HTML for a product page from each brand (same
approach used for Brunswick: either a literal `curl` from your Mac, or a
Claude-in-Chrome `fetch()` against the live page) before writing any
scraper code, so the same kind of invisible-until-raw-HTML bug that hit
Brunswick's image parsing doesn't ship blind here too.
