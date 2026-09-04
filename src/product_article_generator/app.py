"""
Generates a full, bowling.com-style "ball review" article per product --
Al: "Do you think we generate ball review article like the one here
[bowling.com's Storm Equinox Hybrid review]... We could use all the video
transcripts to create a FAQ that is meaningful dynamicly based on what is
being talked about in the videos. We also have alot of content already
accumulated from all the sources... We would want to use this on
bowlerdepot.com and could have another project that is the front end but
this could be the backend that pulls together all the creative and
content for the frontend. We could use the same video filter that we are
using for the existing embed."

Scoped via follow-up questions (see 022_product_articles.sql's header
comment for the full framing): full article, not just the FAQ; requires
admin review before public_api ever exposes it; only generated once a
product clears the same bar video_reviews_summary uses (>=1 approved,
summarized video) with blocked_video_channels (021_blocked_video_
channels.sql) excluded -- reusing the exact same competitor-channel
filter bowlerdepot_video_sync already applies, per Al's explicit ask.

Same Bedrock-via-boto3 wire format as src/video_summarizer/app.py
(anthropic_version + messages, InvokeModel) -- this module asks for a
much bigger, STRUCTURED response (a single JSON object matching this
project's product_articles columns) instead of one plain-text paragraph,
so parse_article_json exists to pull that back apart reliably (Bedrock
sometimes wraps JSON in a ```json fence even when asked not to -- this
strips that before parsing rather than failing on it).

What actually goes into the prompt, and why "use all the transcripts"
needed a real design decision: video_summarizer's rollup only ever reads
each video's already-generated summary (short, cheap, already exists).
Al's FAQ ask specifically wants the dynamic FAQ grounded in what
reviewers ACTUALLY say, not just the already-condensed summary -- a
summary is exactly the kind of thing that would flatten "someone asked
about drilling layout at 4:12" into a generic sentence. So this module
sends BOTH: every qualifying video's summary (full context, cheap) and a
bounded excerpt of its transcript (DEFAULT_TRANSCRIPT_EXCERPT_CHARS per
video, capped total across all videos via DEFAULT_MAX_TOTAL_TRANSCRIPT_
CHARS) specifically so the FAQ can surface real, specific things
reviewers discussed rather than reading like a template.

Sibling/"line" comparison: there is no real product-line/family grouping
concept in this schema (see infer_sibling_products' own docstring) --
this heuristically infers siblings by stripping cover/finish qualifier
words off the product name and matching same-brand products with the
same resulting "line name". Expected to sometimes be wrong; the whole
article (including this) goes through admin review before publishing,
same as everything else here.

Structured, not HTML: the comparison_table/who_should_buy/who_should_
skip/pros/cons/faq fields are all plain lists/dicts a future, separate
frontend project can render however it wants -- see Al's own framing,
quoted above. Real spec VALUES (RG, differential, cover material, core
name, etc.) are deliberately NOT written into product_articles at all;
comparison_table only carries the LLM's qualitative synthesis (read/
backend/best-night narrative) per sibling, keyed by product_id -- the
actual numbers get joined from the live products/product_skus/cores/
coverstocks tables at READ time (by the eventual public_api endpoint),
so a later spec correction doesn't require regenerating the article.
"""
import json
import logging
import os
import re

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Same model id convention as video_summarizer/app.py -- see that
# module's own comment on why this is a Global cross-Region inference
# profile id, not a bare on-demand model id.
DEFAULT_BEDROCK_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"

# An article is a much bigger structured generation than the rollup's
# one paragraph (title + hook + narrative + 2 bullet lists + pros/cons +
# buying tips + verdict + an FAQ array + a comparison table) -- sized
# generously so a real multi-video product doesn't get truncated
# mid-JSON, which would otherwise fail parse_article_json outright.
DEFAULT_ARTICLE_MAX_TOKENS = 3000

DEFAULT_TRANSCRIPT_EXCERPT_CHARS = 4000
DEFAULT_MAX_TOTAL_TRANSCRIPT_CHARS = 20000
DEFAULT_MAX_SIBLINGS = 4

# Cover/finish/edition qualifier words stripped off the END of a product
# name (repeatedly, since a name can carry more than one, e.g. "Equinox
# Pearl Pro") to get a heuristic "line name" for sibling matching -- see
# infer_sibling_products. Deliberately a small, conservative list: better
# to under-strip (miss a real sibling) than over-strip (merge two
# genuinely different lines that happen to share a word).
_LINE_QUALIFIER_WORDS = {
    "solid", "pearl", "hybrid", "plus", "pro", "max", "reactive",
}
_VERSION_SUFFIX_RE = re.compile(r"^(v\d+(\.\d+)?|\d+\.\d+|i{1,3}|iv)$", re.IGNORECASE)


def normalize_line_name(product_name: str) -> str:
    """Strips trailing qualifier/version words off a product name to get
    a heuristic "line name" -- "Equinox Solid" and "Equinox Hybrid" both
    normalize to "Equinox", the shared prefix bowling.com's own line
    comparison table groups on. Not a real product-family concept (see
    module docstring); this is intentionally conservative -- it only
    strips whole trailing words it recognizes, never touches the middle
    or front of a name, and returns the name unchanged if nothing at the
    end matches (e.g. "Equinox" itself, or a one-word name)."""
    if not product_name:
        return product_name
    words = product_name.split()
    while words:
        last = re.sub(r"[^a-z0-9.]", "", words[-1].lower())
        if last in _LINE_QUALIFIER_WORDS or _VERSION_SUFFIX_RE.match(last):
            words.pop()
        else:
            break
    return " ".join(words) if words else product_name


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


def list_products_needing_article(conn) -> list:
    """Products that clear the generation gate (>=1 approved, non-
    blocked-channel, summarized video -- same bar 022_product_articles.
    sql's header comment describes) and don't already have a
    product_articles row. Deliberately does NOT re-scan products that
    already have a row of any status (pending/approved/rejected) -- a
    rejected article isn't automatically retried by the batch job; an
    admin regenerating it on demand (see generate_article_for_product's
    force_regenerate) is the only path back, same as this codebase's
    other "AI got it wrong, a human asks for another attempt" flows
    rather than silent auto-retry."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select distinct p.id
            from products p
            join product_videos pv on pv.product_id = p.id
            left join product_articles pa on pa.product_id = p.id
            where p.published = true
              and pv.status = 'approved'
              and pv.summary is not null
              and pa.id is null
              and not exists (
                  select 1 from blocked_video_channels bvc
                  where lower(bvc.channel_title) = lower(pv.channel_title)
              )
            order by p.id
            """
        )
        return [row[0] for row in cur.fetchall()]


def fetch_product_content(conn, product_id: str) -> dict:
    """Everything this module needs to write about ONE product: specs/
    description (same select shape public_api.get_product uses for its
    detail page, minus fields an article has no use for) plus every
    qualifying video's title/channel/summary/transcript excerpt.
    Excludes blocked-channel videos the same way bowlerdepot_video_sync's
    list_videos_needing_sync does (Al's explicit ask to reuse that same
    filter here) -- a blocked competitor's video still isn't allowed to
    shape article content, same reasoning as keeping it off the
    BigCommerce push."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select p.id, p.name, p.color, p.coverstock_material, p.coverstock_type,
                   p.coverstock_name, p.has_particle, p.factory_finish,
                   lower(p.weights_available) as weights_min,
                   upper(p.weights_available) as weights_max,
                   p.release_date, p.description,
                   b.id as brand_id, b.name as brand_name,
                   c.name as core_name, c.core_type
            from products p
            join brands b on b.id = p.brand_id
            left join cores c on c.id = p.core_id
            where p.id = %s
            """,
            (product_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cur.description]
        product = dict(zip(columns, row))

        # Same exclusive-upper-bound int4range gotcha public_api.get_product
        # already documents -- Postgres normalizes weights_available to a
        # canonical "[min,max)" form, so upper() is one past the real max.
        weights_min = product.pop("weights_min", None)
        weights_max = product.pop("weights_max", None)
        if weights_min is not None and weights_max is not None:
            product["weights_available"] = f"{weights_min}-{weights_max - 1} lb"
        else:
            product["weights_available"] = None

        cur.execute(
            """
            select id, title, channel_title, summary, transcript
            from product_videos
            where product_id = %s
              and status = 'approved'
              and summary is not null
              and not exists (
                  select 1 from blocked_video_channels bvc
                  where lower(bvc.channel_title) = lower(product_videos.channel_title)
              )
            order by published_at desc nulls last, id
            """,
            (product_id,),
        )
        video_columns = [desc[0] for desc in cur.description]
        videos = [dict(zip(video_columns, r)) for r in cur.fetchall()]

    product["videos"] = videos
    return product


def infer_sibling_products(conn, product: dict) -> list:
    """Heuristic-only sibling lookup -- see this module's own docstring
    and 022_product_articles.sql's header comment for why there's no
    real product-line grouping to query instead. Matches same brand_id +
    same normalize_line_name() result, excludes the product itself,
    caps at DEFAULT_MAX_SIBLINGS (an article's comparison table should
    read as a short, scannable table, not a dump of every same-brand
    product that happens to share a line name)."""
    # Not short-circuited when nothing was stripped (line_name ==
    # product["name"]) -- e.g. this product's own name has no recognized
    # qualifier word ("Equinox" itself). It should still match a sibling
    # like "Equinox Solid", whose OWN normalized line name strips down to
    # this same bare "Equinox".
    line_name = normalize_line_name(product["name"])

    with conn.cursor() as cur:
        cur.execute(
            """
            select id, name
            from products
            where brand_id = %s and id != %s and published = true and status = 'current'
            order by name
            """,
            (product["brand_id"], product["id"]),
        )
        candidates = [{"id": r[0], "name": r[1]} for r in cur.fetchall()]

    siblings = [
        c for c in candidates
        if normalize_line_name(c["name"]).lower() == line_name.lower()
    ]
    return siblings[:DEFAULT_MAX_SIBLINGS]


def build_article_prompt(product: dict, siblings: list) -> str:
    """Builds the single Bedrock prompt asking for the whole article back
    as one JSON object. Every field this asks for maps 1:1 to a
    product_articles column (see that migration's own column comments) --
    kept that way deliberately so parse_article_json/store_article don't
    need any renaming/reshaping logic between what Bedrock returns and
    what gets written to the DB."""
    spec_lines = [
        f"Name: {product['brand_name']} {product['name']}",
        f"Color: {product.get('color') or 'unknown'}",
        f"Core: {product.get('core_name') or 'unknown'} ({product.get('core_type') or 'unknown'})",
        f"Coverstock: {product.get('coverstock_name') or 'unknown'} "
        f"({product.get('coverstock_type') or 'unknown'} {product.get('coverstock_material') or ''})".strip(),
        f"Factory finish: {product.get('factory_finish') or 'unknown'}",
        f"Weights available: {product.get('weights_available') or 'unknown'}",
        f"Release date: {product.get('release_date') or 'unknown'}",
    ]
    if product.get("description"):
        spec_lines.append(f"Manufacturer description: {product['description']}")

    video_blocks = []
    total_transcript_chars = 0
    for i, v in enumerate(product["videos"], start=1):
        transcript = v.get("transcript") or ""
        remaining_budget = max(0, DEFAULT_MAX_TOTAL_TRANSCRIPT_CHARS - total_transcript_chars)
        excerpt = transcript[:min(DEFAULT_TRANSCRIPT_EXCERPT_CHARS, remaining_budget)]
        total_transcript_chars += len(excerpt)
        block = (
            f"Video {i} (channel: {v.get('channel_title') or 'unknown'}, "
            f"title: {v.get('title') or 'untitled'})\n"
            f"Summary: {v.get('summary')}\n"
        )
        if excerpt:
            block += f"Transcript excerpt: {excerpt}\n"
        video_blocks.append(block)

    sibling_block = ""
    if siblings:
        sibling_names = ", ".join(f"{s['name']} (product_id: {s['id']})" for s in siblings)
        sibling_block = (
            f"\n\nThis product may be part of a line alongside these related "
            f"products from the same brand: {sibling_names}. If your source "
            "material discusses how this ball compares to them, include that "
            "in comparison_table as an array of objects with keys "
            "\"product_id\" (use the exact product_id given above), "
            "\"product_name\", \"read\" (how soon/where it reads the lane "
            "relative to this one), \"backend\" (backend motion comparison), "
            "and \"best_night\" (what kind of lane condition favors it "
            "vs this ball). Only include a sibling if you have real basis "
            "for the comparison; omit it from comparison_table otherwise. "
            "Do not invent numeric specs (RG, differential) for any ball -- "
            "leave those out entirely, they are not your job here."
        )

    return (
        "You are writing a bowling-ball review article for a retail site, in the "
        "voice of an experienced bowling pro-shop staffer -- direct, specific, "
        "grounded in what real reviewers said, never generic marketing filler. "
        "Base every factual claim ONLY on the specs and video content given below "
        "-- do not invent specs, comparisons, or reviewer opinions that aren't "
        "supported by this material.\n\n"
        "Product specs:\n" + "\n".join(spec_lines) + sibling_block + "\n\n"
        "Video review content (from approved YouTube reviews of this ball):\n"
        + "\n".join(video_blocks) + "\n\n"
        "Return ONLY a single JSON object (no markdown fence, no commentary "
        "before or after) with exactly these keys:\n"
        "  title: a specific, benefit-driven headline for this ball\n"
        "  hook: a 2-4 sentence narrative opening anecdote that sets up the "
        "kind of situation this ball solves\n"
        "  performance_summary: 3-6 sentences synthesizing how the ball "
        "actually performs, grounded in the video content -- note real "
        "agreement AND disagreement between reviewers rather than "
        "flattening it into one voice\n"
        "  who_should_buy: array of 3-5 short bullet strings\n"
        "  who_should_skip: array of 2-4 short bullet strings\n"
        "  pros: array of 3-5 short bullet strings\n"
        "  cons: array of 2-4 short bullet strings\n"
        "  buying_tips: 2-4 sentences of practical buying/maintenance/"
        "drilling guidance, only if the source material actually supports it "
        "-- use an empty string if there's nothing real to say\n"
        "  verdict: a 2-4 sentence closing recommendation\n"
        "  faq: array of 3-6 {\"question\": ..., \"answer\": ...} objects. "
        "THIS IS THE MOST IMPORTANT PART: each question must be grounded in "
        "something specific actually discussed in the video content above "
        "(a real comparison mentioned, a real disagreement between "
        "reviewers, a specific lane condition or layout question that came "
        "up) -- not a generic template question you'd ask about any ball\n"
        "  comparison_table: array as described above, or an empty array if "
        "no siblings were given or none are actually discussed\n"
    )


def call_bedrock_for_article(bedrock_client, model_id: str, prompt: str,
                              max_tokens: int = DEFAULT_ARTICLE_MAX_TOKENS) -> str:
    """Same Bedrock wire format as video_summarizer.summarize_transcript/
    generate_video_reviews_rollup -- kept as a thin, separately-mockable
    function for the same reason those are."""
    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    })
    response = bedrock_client.invoke_model(modelId=model_id, contentType="application/json",
                                            accept="application/json", body=body)
    payload = json.loads(response["body"].read())
    return payload["content"][0]["text"].strip()


_REQUIRED_ARTICLE_KEYS = (
    "title", "hook", "performance_summary", "who_should_buy", "who_should_skip",
    "pros", "cons", "buying_tips", "verdict", "faq", "comparison_table",
)


def parse_article_json(raw_text: str) -> dict:
    """Bedrock is asked for a bare JSON object but sometimes wraps it in
    a ```json ... ``` fence anyway (observed behavior with Claude models
    generally, not specific to this prompt) -- strip that before
    json.loads rather than failing outright. Raises ValueError (caller's
    job to catch, same soft-fail-per-product posture as
    bowlerdepot_video_sync's _process_one_video) if the text still isn't
    parseable JSON, or is JSON but missing a required key -- a partial/
    malformed article is worse than no article, since there's no partial-
    field review UI, the whole row is reviewed as a unit."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Bedrock response was not valid JSON: {exc}") from exc

    missing = [k for k in _REQUIRED_ARTICLE_KEYS if k not in data]
    if missing:
        raise ValueError(f"Bedrock response JSON missing required keys: {missing}")

    return data


def store_article(conn, product_id: str, article: dict, source_video_ids: list,
                   sibling_product_ids: list) -> str:
    """Upsert -- a regenerate overwrites the existing row in place and
    resets status to 'pending' (see 022_product_articles.sql's own
    header comment for why: a previously-approved article going back
    through review on regenerate, rather than silently replacing live
    content, is deliberate)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into product_articles
                (product_id, status, title, hook, performance_summary, who_should_buy,
                 who_should_skip, pros, cons, buying_tips, verdict, faq, comparison_table,
                 sibling_product_ids, source_video_ids, generated_at)
            values (%s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            on conflict (product_id) do update set
                status = 'pending',
                title = excluded.title,
                hook = excluded.hook,
                performance_summary = excluded.performance_summary,
                who_should_buy = excluded.who_should_buy,
                who_should_skip = excluded.who_should_skip,
                pros = excluded.pros,
                cons = excluded.cons,
                buying_tips = excluded.buying_tips,
                verdict = excluded.verdict,
                faq = excluded.faq,
                comparison_table = excluded.comparison_table,
                sibling_product_ids = excluded.sibling_product_ids,
                source_video_ids = excluded.source_video_ids,
                generated_at = excluded.generated_at,
                reviewed_at = null,
                resolved_by = null
            returning id
            """,
            (
                product_id, article["title"], article["hook"], article["performance_summary"],
                json.dumps(article["who_should_buy"]), json.dumps(article["who_should_skip"]),
                json.dumps(article["pros"]), json.dumps(article["cons"]), article["buying_tips"],
                article["verdict"], json.dumps(article["faq"]), json.dumps(article["comparison_table"]),
                json.dumps(sibling_product_ids), json.dumps(source_video_ids),
            ),
        )
        article_id = cur.fetchone()[0]
    conn.commit()
    return article_id


def generate_article_for_product(conn, bedrock_client, model_id: str, product_id: str,
                                  force: bool = False) -> dict:
    """Orchestrates one product's full generation. force=True (the
    admin-triggered on-demand path -- see admin_api.queue_article_
    generation) skips the "already has an article" check the batch
    handler's own list_products_needing_article query applies; a human
    explicitly asking for a regenerate should always get one."""
    product = fetch_product_content(conn, product_id)
    if product is None:
        return {"product_id": product_id, "generated": False, "reason": "product_not_found"}

    if not product["videos"]:
        return {"product_id": product_id, "generated": False, "reason": "no_qualifying_videos"}

    siblings = infer_sibling_products(conn, product)
    prompt = build_article_prompt(product, siblings)
    raw = call_bedrock_for_article(bedrock_client, model_id, prompt)
    article = parse_article_json(raw)

    source_video_ids = [v["id"] for v in product["videos"]]
    sibling_product_ids = [s["id"] for s in siblings]
    article_id = store_article(conn, product_id, article, source_video_ids, sibling_product_ids)

    return {
        "product_id": product_id, "generated": True, "article_id": article_id,
        "video_count": len(source_video_ids), "sibling_count": len(sibling_product_ids),
    }


def handler(event, context):
    """Two shapes: {} / {"batch": true} runs the scheduled catalog-wide
    sweep (see list_products_needing_article -- only products with no
    existing article row); {"product_id": "..."} is the on-demand,
    admin-triggered single-product (re)generate, invoked directly by
    AdminApiFunction (see admin_api.queue_article_generation) -- same
    "batch job also accepts a direct manual/admin invoke" shape this
    project already uses for VideoDiscoveryFunction and Video
    TranscriptFetcherFunction."""
    import boto3

    bedrock_client = boto3.client("bedrock-runtime")
    model_id = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_BEDROCK_MODEL_ID)

    conn = get_db_connection()
    try:
        if event.get("product_id"):
            result = generate_article_for_product(
                conn, bedrock_client, model_id, event["product_id"], force=True,
            )
            return {"statusCode": 200, "body": json.dumps({"results": [result]})}

        product_ids = list_products_needing_article(conn)
        results = []
        for product_id in product_ids:
            try:
                results.append(generate_article_for_product(conn, bedrock_client, model_id, product_id))
            except Exception:
                logger.exception("Failed to generate article for product_id=%s", product_id)
                results.append({"product_id": product_id, "generated": False, "reason": "error"})

        generated = sum(1 for r in results if r["generated"])
        return {"statusCode": 200, "body": json.dumps({
            "checked": len(product_ids), "generated": generated, "results": results,
        })}
    finally:
        conn.close()
