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

Article images (023_product_article_images.sql; mechanism went through
TWO rewrites, 024_product_article_images_composite_pipeline.sql then
025_product_article_images_theme_driven_pipeline.sql -- see v3 below for
where this landed and why): Al's follow-up ask, "can we have it generate
some images for the article from the bowling ball images and using the
article to give it some context" -- scoped via a follow-up AskUserQuestion
exchange to generate BOTH an action/lifestyle hero shot and a stylized
product hero shot, automatically in this same Lambda run (both the daily
batch and the on-demand regenerate path), gated behind the article's
existing pending/approved/rejected review same as the text. Image
generation is deliberately best-effort and non-fatal: if it fails (bad
reference photo, Bedrock error, etc.) the article's own text still stores
and goes to review -- see generate_article_images' own docstring.

Mechanism history, briefly (full reasoning lives in each migration's own
header comment -- this is a pointer, not a repeat):
  - v1 (023): a single Stable Diffusion image-to-image call per variant,
    conditioned on the product's real reference photo. Al's feedback:
    the images weren't good, needed a specific aspect ratio, and the
    ball itself must never be altered.
  - v2 (024): reacting to "never alter the ball" literally -- a
    three-step pipeline (Bedrock Remove Background cuts the ball out
    untouched, a separate text-to-image call generates a background,
    Pillow composites them). This technically satisfied "never altered"
    but Al's review of real reference examples (a bowling.com-style shot,
    and a separately-themed "Fallout" ball with a wasteland backdrop)
    found the RESULT wasn't what he wanted, backgrounds should be driven
    by the product's own NAME/branding rather than generic, and --
    verbatim -- "I think we are trying to be to technical on this maybe
    there is a better ai model for this." Asked directly, Al confirmed
    he'd trade the "never altered" guarantee for better, simpler results.
  - v3 (025, current): back to a SINGLE Stable Diffusion image-to-image
    call per variant (simpler, one Bedrock call instead of three, one
    Region instead of three -- directly answering "too technical"), but
    with two real improvements over v1: the prompt is grounded in a
    THEME the text-generation model itself derives from the product's
    own name/branding (build_article_prompt's new visual_theme ask,
    read by build_image_prompts) instead of a generic scene description,
    and the aspect ratio Al asked for (16:9 action / 1:1 product) is
    achieved by LETTERBOXING the reference photo onto a target-ratio
    canvas (build_reference_canvas) BEFORE the img2img call, since
    Stable Diffusion's aspect_ratio parameter only works in text-to-image
    mode, not image-to-image. Ball fidelity is now explicitly best-effort
    (the model conditions on the reference photo via `strength`, same as
    v1, but is not guaranteed pixel-identical) -- this is a deliberate
    tradeoff Al made, not an oversight.

Model choice + Region, researched (not assumed) before building, same
defensive posture as the text model's own us-west-1 CRIS situation above.
Amazon Nova Canvas is Legacy with an EOL of 2026-09-30 (weeks away) and
was never available in us-west-1 to begin with (only us-east-1/eu-west-1/
ap-northeast-1, all Legacy). Amazon Titan Image Generator G1 v2 is
ALREADY past its EOL (2026-06-30). Neither supports Geo/Global cross-
Region inference profiles at all, so there's no CRIS workaround for
either -- AWS's own current guidance is to migrate to Stability AI's
models instead. This module uses Stability AI Stable Diffusion 3.5 Large
(`stability.sd3-5-large-v1:0`, not Legacy) for BOTH image-to-image
generation calls -- the SAME model v1 originally used, only available in
`us-west-2` (Oregon) on Bedrock, not this stack's own home Region
(`us-west-1`). The image Bedrock client is constructed with an explicit
`region_name` (see handler(), BEDROCK_IMAGE_REGION) pointed at
`us-west-2` while the rest of the stack (including the text model's own
bedrock-runtime client) stays in `us-west-1` -- a plain cross-Region API
call, not Bedrock's own CRIS mechanism (image models don't support it),
so the IAM Resource ARN below is hardcoded to `us-west-2` rather than
`${AWS::Region}`. v2's THIRD Bedrock client/Region (Stability AI Image
Services' "Remove Background", us-east-1) is GONE as of v3 -- that model
is no longer called at all, which also retires the whole Geographic-CRIS
IAM saga that model required (see 025's own header comment and
DEPLOY_RUNBOOK's 6t section for that saga's full history, kept for
posterity since it's a real lesson about Global vs. Geographic CRIS IAM
shapes, even though the model itself is no longer in use here). If you're
reading this after Nova Canvas's Sept 2026 EOL has passed and Bedrock has
since added Stability (or a successor) support to us-west-1, simplify
back to a single-Region client and drop BEDROCK_IMAGE_REGION -- check the
model's own current "Regional availability" table first, don't assume.
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

# Stability AI Stable Diffusion 3.5 Large -- used here for image-to-image
# generation, conditioned on the product's real reference photo (see
# build_reference_canvas/build_image_prompts/call_bedrock_for_image). See
# this module's own docstring for the full model/Region research trail
# (Nova Canvas Legacy/EOL 2026-09-30, Titan Image Generator G1 v2 already
# past its 2026-06-30 EOL, neither ever available in us-west-1 anyway).
# Invoked as a plain on-demand foundation model (no CRIS inference
# profile -- image models don't support Geo/Global cross-Region inference
# at all, unlike the text model above), but in a DIFFERENT Region than
# the rest of this stack -- see DEFAULT_BEDROCK_IMAGE_REGION just below.
DEFAULT_BEDROCK_IMAGE_MODEL_ID = "stability.sd3-5-large-v1:0"

# Stable Diffusion 3.5 Large is only available in us-west-2 (Oregon) on
# Bedrock as of this writing -- not this stack's home Region (us-west-1).
# handler() constructs the bedrock-runtime client used for image-
# generation calls with region_name=this value explicitly, separate from
# the text model's own bedrock_client (home Region) -- a plain cross-
# Region API
# call, since there's no CRIS profile for image models to route through
# instead. See this module's own docstring for the full research trail.
DEFAULT_BEDROCK_IMAGE_REGION = "us-west-2"

# aspect_ratio is a text-to-image-only parameter on Stable Diffusion 3.5
# Large (silently ignored in image-to-image mode, which is what this
# module uses -- see call_bedrock_for_image) -- so Al's "specific aspect
# ratio" ask is satisfied a different way: build_reference_canvas
# letterboxes the real reference photo onto a canvas of these exact pixel
# dimensions BEFORE the img2img call, and image-to-image mode then
# preserves whatever aspect ratio the INPUT already has. 16:9 for the
# action/lifestyle shot (a wide lane scene reads better landscape), 1:1
# for the stylized product shot (square reads better as a catalog/
# thumbnail image) -- same pixel budgets v2's background-generation calls
# used, kept here rather than re-derived. See build_reference_canvas/
# generate_article_images.
_ACTION_SHOT_CANVAS_SIZE = (1536, 864)
_PRODUCT_SHOT_CANVAS_SIZE = (1024, 1024)
_VARIANT_CANVAS_SIZES = {
    "action_shot": _ACTION_SHOT_CANVAS_SIZE,
    "product_shot": _PRODUCT_SHOT_CANVAS_SIZE,
}

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
    as one JSON object. Every REQUIRED field this asks for maps 1:1 to a
    product_articles column (see that migration's own column comments) --
    kept that way deliberately so parse_article_json/store_article don't
    need any renaming/reshaping logic between what Bedrock returns and
    what gets written to the DB. visual_theme (see the return value's own
    prompt text below) is the one EXCEPTION -- it's asked for here but
    deliberately NOT in _REQUIRED_ARTICLE_KEYS/not written to any column;
    it only exists to feed build_image_prompts (025_product_article_
    images_theme_driven_pipeline.sql) with a name/branding-grounded scene
    concept for the generated article images, per Al's explicit ask that
    backgrounds be driven by what the product's OWN NAME evokes (a ball
    named "Fallout" should evoke a wasteland, not a generic bowling lane)
    -- an LLM already has to reason about the product's name/branding
    connotations to write good article copy anyway, so this rides along
    on the same call rather than needing a second one. Being optional
    (not required) means an older/differently-behaving model response
    that omits it doesn't break parse_article_json -- build_image_
    prompts falls back to performance_summary/hook, same graceful
    degradation this project already uses elsewhere."""
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
        "  visual_theme: OPTIONAL. 1-2 sentences describing a distinctive "
        "visual backdrop/scene concept a product photographer could use for "
        "this specific ball, grounded in what the ball's OWN NAME and "
        "branding evoke -- for example a ball named \"Fallout\" evokes a "
        "post-apocalyptic/nuclear-wasteland aesthetic, a ball named "
        "\"Origin\" might evoke something cosmic or primordial. Be specific "
        "and creative, not generic bowling-alley imagery, UNLESS the name "
        "genuinely has no strong thematic connotation on its own -- in that "
        "case describe an elevated, premium scene instead. This field is "
        "purely for generating article artwork; it is never shown to "
        "readers.\n"
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


def fetch_reference_image_url(conn, product_id: str):
    """The product's own real photo, for use as call_bedrock_remove_
    background's input -- same canonical "pick the one best image"
    coalesce pattern public_api.service.py uses five times (is_visible
    true, order by is_thumbnail desc, display_order, id, limit 1),
    falling back to products.primary_image_url if no product_images row
    qualifies. Returns None (not a KeyError/exception) if the product has
    no image at all -- generate_article_images treats that as "skip
    images for this run", not a failure."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select coalesce(
                (
                    select pi.stored_url from product_images pi
                    where pi.product_id = p.id and pi.is_visible = true
                    order by pi.is_thumbnail desc, pi.display_order, pi.id
                    limit 1
                ),
                p.primary_image_url
            )
            from products p
            where p.id = %s
            """,
            (product_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def fetch_reference_image_bytes(reference_image_url: str) -> bytes:
    """Reference photo lives at a public https URL -- either our own S3
    stored_url or, absent that, the manufacturer's own primary_image_url
    -- and no existing Lambda in this codebase reads an image back out of
    S3 directly (checked; only s3:ListBucket-for-orphan-cleanup precedent
    exists). A plain HTTP GET works identically for both URL shapes
    without needing to special-case which bucket/host it came from."""
    import requests

    response = requests.get(reference_image_url, timeout=30)
    response.raise_for_status()
    return response.content


def _reference_image_to_base64_png(raw_bytes: bytes) -> str:
    """Stable Diffusion 3.5 Large's image-to-image mode requires a
    base64-encoded jpeg/png/webp image; re-encoding through Pillow (same
    library/version pin as image_processor's own requirements.txt)
    normalizes whatever format the source photo actually is (often JPEG
    from a manufacturer CDN, or already a PNG if it's coming out of
    build_reference_canvas) into PNG rather than trusting the source's
    own file extension."""
    import base64
    import io

    from PIL import Image

    image = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def build_reference_canvas(reference_bytes: bytes, canvas_size: tuple) -> bytes:
    """Letterboxes/pillarboxes the product's real reference photo onto a
    canvas of exactly canvas_size (width, height) -- this is how Al's
    "specific aspect ratio" ask is satisfied under v3 (see this module's
    docstring), since Stable Diffusion's own aspect_ratio parameter is
    text-to-image-only and image-to-image mode instead just preserves
    whatever aspect ratio the INPUT already has. Pure Pillow: no
    segmentation, no cutout, no Bedrock call -- the ball is scaled down
    (never up, never cropped) to fit within ~70% of the canvas's shorter
    side, keeping its own aspect ratio intact, and centered on a flat
    neutral-dark fill. That fill is deliberately plain -- it's not meant
    to look finished, it only exists to hand call_bedrock_for_image a
    correctly-shaped starting image to condition on; the actual scene
    comes from the img2img generation itself. Returns PNG bytes, ready
    for _reference_image_to_base64_png."""
    import io

    from PIL import Image

    canvas_w, canvas_h = canvas_size
    reference = Image.open(io.BytesIO(reference_bytes)).convert("RGB")

    target_size = int(min(canvas_w, canvas_h) * 0.7)
    ref_w, ref_h = reference.size
    scale = min(target_size / max(ref_w, 1), target_size / max(ref_h, 1), 1.0)
    new_size = (max(1, int(ref_w * scale)), max(1, int(ref_h * scale)))
    reference = reference.resize(new_size, Image.LANCZOS)

    canvas = Image.new("RGB", (canvas_w, canvas_h), (24, 24, 26))
    paste_x = (canvas_w - new_size[0]) // 2
    paste_y = (canvas_h - new_size[1]) // 2
    canvas.paste(reference, (paste_x, paste_y))

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def build_image_prompts(product: dict, article: dict) -> dict:
    """Two short image-to-image prompts, one per variant -- grounded
    FIRST in the just-generated article's own visual_theme (see
    build_article_prompt's optional visual_theme ask), the field the
    article-generation model itself derived from the product's own name/
    branding, per Al's explicit "background driven by the ball's name"
    answer. Falls back to performance_summary, then hook, if visual_theme
    came back empty (it's an OPTIONAL field -- see parse_article_json/
    _REQUIRED_ARTICLE_KEYS) -- same fallback shape build_background_
    prompts used in v2, kept because it's still the right degradation
    order. Unlike v2's background-only prompts, these DO describe the
    ball itself positively again: this is img2img conditioning on the
    real reference photo (via build_reference_canvas + call_bedrock_for_
    image's strength), not a cutout being composited in afterward, so the
    prompt needs to tell the model what's already there, not pretend it
    isn't."""
    theme = (article.get("visual_theme") or "").strip()
    context = theme or (article.get("performance_summary") or article.get("hook") or "").strip()
    context = context[:300]
    ball_color = (product.get("color") or product.get("primary_color") or "").strip()
    ball_desc = f"a {ball_color} bowling ball" if ball_color else "a bowling ball"

    action_prompt = (
        f"Editorial sports-photography action shot of {ball_desc} on a bowling lane, "
        "dramatic side lighting, shallow depth of field, motion energy, premium "
        "athletic-brand advertising style, no text, no logos, no watermark."
    )
    product_prompt = (
        f"Elevated studio product photograph of {ball_desc}, reflective dark surface, "
        "soft dramatic rim lighting, premium catalog-photography style, no text, "
        "no logos, no watermark."
    )
    if context:
        action_prompt += f" Scene/mood: {context}"
        product_prompt += f" Scene/mood: {context}"

    return {"action_shot": action_prompt, "product_shot": product_prompt}


_IMAGE_NEGATIVE_PROMPT = (
    "extra balls, multiple bowling balls, deformed, distorted, blurry, low quality, "
    "text, watermark, logo, signature, people, hands"
)


def call_bedrock_for_image(bedrock_image_client, model_id: str, prompt: str,
                            reference_image_b64: str, strength: float = 0.65) -> bytes:
    """Stable Diffusion 3.5 Large's image-to-image mode -- conditions
    generation on reference_image_b64 (the letterboxed canvas from
    build_reference_canvas, base64-encoded) rather than generating from
    the prompt alone. strength controls how much the model is allowed to
    deviate from the reference: 0.65 (slightly higher than v1's original
    0.6) trades a little more of the fidelity-to-reference for better,
    more cohesive results, per Al's explicit "prioritize quality over
    pixel-perfection" answer -- ball fidelity is best-effort here, not
    guaranteed the way v2's cutout+composite pipeline made it. Note there
    is no aspect_ratio parameter here -- it's text-to-image-only on this
    model and would be silently ignored; the aspect ratio instead comes
    from whatever shape reference_image_b64 already is (see
    build_reference_canvas). bedrock_image_client is scoped to
    DEFAULT_BEDROCK_IMAGE_REGION (us-west-2) -- see this module's
    docstring for why that's a different Region from the text model's own
    bedrock_client."""
    import base64

    body = json.dumps({
        "prompt": prompt,
        "mode": "image-to-image",
        "image": reference_image_b64,
        "strength": strength,
        "negative_prompt": _IMAGE_NEGATIVE_PROMPT,
        "output_format": "png",
    })
    response = bedrock_image_client.invoke_model(modelId=model_id, contentType="application/json",
                                                   accept="application/json", body=body)
    payload = json.loads(response["body"].read())
    finish_reasons = [r for r in (payload.get("finish_reasons") or []) if r]
    if finish_reasons:
        raise RuntimeError(f"Stable Diffusion returned a non-null finish reason: {finish_reasons}")
    images = payload.get("images") or []
    if not images:
        raise RuntimeError("Stable Diffusion response contained no images")
    return base64.b64decode(images[0])


def store_article_image(s3_client, bucket: str, product_id: str, variant: str, png_bytes: bytes) -> dict:
    """Mirrors image_processor.upload_variants' exact key/URL convention
    -- raw https://{bucket}.s3.amazonaws.com/{key} PNG URLs on the same
    public-read IMAGE_BUCKET -- but under an "article-images/" prefix
    rather than image_processor's own "product-images/" prefix (see
    023_product_article_images.sql's header comment: these aren't a
    product's real photos and shouldn't be swept up in product_scraper's
    product-images/* orphan-cleanup listing)."""
    key = f"article-images/{product_id}/{variant}.png"
    s3_client.put_object(Bucket=bucket, Key=key, Body=png_bytes, ContentType="image/png")
    return {"key": key, "url": f"https://{bucket}.s3.amazonaws.com/{key}"}


def generate_article_images(conn, bedrock_image_client, s3_client,
                             image_model_id: str, image_bucket: str, product: dict, article: dict) -> dict:
    """Best-effort, non-fatal: by the time this runs the article TEXT has
    already been generated successfully (see generate_article_for_
    product) -- an image failure here must never lose an otherwise-good
    article, so every failure path here logs and returns whatever subset
    of {action_shot_image_key, action_shot_image_url, product_shot_
    image_key, product_shot_image_url} actually succeeded (possibly
    empty) rather than raising.

    v3, single call per variant (see this module's own docstring for the
    v1/v2/v3 history): the product's real reference photo is fetched
    ONCE (both variants condition on the same source photo, just
    letterboxed onto a different canvas size each), then per variant --
    letterbox onto that variant's own canvas size (build_reference_
    canvas), base64-encode, build a theme-driven prompt (build_image_
    prompts), and run one Stable Diffusion image-to-image call
    (call_bedrock_for_image). Each variant runs in its own try/except so
    one variant failing doesn't take the other down with it."""
    reference_url = fetch_reference_image_url(conn, product["id"])
    if not reference_url:
        logger.info("No reference image available for product_id=%s, skipping article images", product["id"])
        return {}

    try:
        reference_bytes = fetch_reference_image_bytes(reference_url)
    except Exception:
        logger.exception("Failed to fetch reference image for product_id=%s", product["id"])
        return {}

    prompts = build_image_prompts(product, article)
    results = {}
    for variant, prompt in (("action_shot", prompts["action_shot"]), ("product_shot", prompts["product_shot"])):
        try:
            canvas_png_bytes = build_reference_canvas(reference_bytes, _VARIANT_CANVAS_SIZES[variant])
            canvas_b64 = _reference_image_to_base64_png(canvas_png_bytes)
            generated_png_bytes = call_bedrock_for_image(bedrock_image_client, image_model_id, prompt, canvas_b64)
            stored = store_article_image(s3_client, image_bucket, product["id"], variant, generated_png_bytes)
            results[f"{variant}_image_key"] = stored["key"]
            results[f"{variant}_image_url"] = stored["url"]
        except Exception:
            logger.exception("Failed to generate %s image for product_id=%s", variant, product["id"])

    return results


def store_article(conn, product_id: str, article: dict, source_video_ids: list,
                   sibling_product_ids: list, images: dict = None) -> str:
    """Upsert -- a regenerate overwrites the existing row in place and
    resets status to 'pending' (see 022_product_articles.sql's own
    header comment for why: a previously-approved article going back
    through review on regenerate, rather than silently replacing live
    content, is deliberate)."""
    images = images or {}
    has_images = bool(images)
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into product_articles
                (product_id, status, title, hook, performance_summary, who_should_buy,
                 who_should_skip, pros, cons, buying_tips, verdict, faq, comparison_table,
                 sibling_product_ids, source_video_ids, generated_at,
                 action_shot_image_key, action_shot_image_url,
                 product_shot_image_key, product_shot_image_url, images_generated_at)
            values (%s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(),
                    %s, %s, %s, %s, case when %s then now() else null end)
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
                action_shot_image_key = excluded.action_shot_image_key,
                action_shot_image_url = excluded.action_shot_image_url,
                product_shot_image_key = excluded.product_shot_image_key,
                product_shot_image_url = excluded.product_shot_image_url,
                images_generated_at = excluded.images_generated_at,
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
                images.get("action_shot_image_key"), images.get("action_shot_image_url"),
                images.get("product_shot_image_key"), images.get("product_shot_image_url"),
                has_images,
            ),
        )
        article_id = cur.fetchone()[0]
    conn.commit()
    return article_id


def generate_article_for_product(conn, bedrock_client, model_id: str, product_id: str,
                                  s3_client=None, bedrock_image_client=None,
                                  image_model_id: str = None, image_bucket: str = None,
                                  force: bool = False) -> dict:
    """Orchestrates one product's full generation. force=True (the
    admin-triggered on-demand path -- see admin_api.queue_article_
    generation) skips the "already has an article" check the batch
    handler's own list_products_needing_article query applies; a human
    explicitly asking for a regenerate should always get one.

    s3_client/bedrock_image_client/image_model_id/image_bucket are all
    optional (default None) so existing/simpler callers -- and every
    test that only cares about the article TEXT -- don't need to thread
    image plumbing through just to call this. When all four are supplied
    (the real handler() always supplies them, per Al's "automatically
    with the article" answer), image generation runs as an extra best-
    effort step after the article text is parsed but before it's stored,
    so both text and whatever images succeeded land in the same
    store_article call/row. bedrock_image_client is a separate client
    from bedrock_client (not reused) -- scoped to its own Region (see
    DEFAULT_BEDROCK_IMAGE_REGION / this module's docstring)."""
    product = fetch_product_content(conn, product_id)
    if product is None:
        return {"product_id": product_id, "generated": False, "reason": "product_not_found"}

    if not product["videos"]:
        return {"product_id": product_id, "generated": False, "reason": "no_qualifying_videos"}

    siblings = infer_sibling_products(conn, product)
    prompt = build_article_prompt(product, siblings)
    raw = call_bedrock_for_article(bedrock_client, model_id, prompt)
    article = parse_article_json(raw)

    images = {}
    if s3_client is not None and bedrock_image_client is not None and image_model_id and image_bucket:
        images = generate_article_images(
            conn, bedrock_image_client, s3_client, image_model_id, image_bucket, product, article,
        )

    source_video_ids = [v["id"] for v in product["videos"]]
    sibling_product_ids = [s["id"] for s in siblings]
    article_id = store_article(conn, product_id, article, source_video_ids, sibling_product_ids, images=images)

    return {
        "product_id": product_id, "generated": True, "article_id": article_id,
        "video_count": len(source_video_ids), "sibling_count": len(sibling_product_ids),
        "images_generated": bool(images),
    }


def handler(event, context):
    """Two shapes: {} / {"batch": true} runs the scheduled catalog-wide
    sweep (see list_products_needing_article -- only products with no
    existing article row); {"product_id": "..."} is the on-demand,
    admin-triggered single-product (re)generate, invoked directly by
    AdminApiFunction (see admin_api.queue_article_generation) -- same
    "batch job also accepts a direct manual/admin invoke" shape this
    project already uses for VideoDiscoveryFunction and Video
    TranscriptFetcherFunction. Both paths pass s3_client/
    bedrock_image_client/image_model_id/image_bucket through to
    generate_article_for_product so images generate automatically
    alongside the text in every run (Al's "Both image types,
    automatically with the article" answer) -- there is no separate
    on-demand-only image trigger. bedrock_image_client is constructed
    with its own explicit region_name (BEDROCK_IMAGE_REGION), separate
    from bedrock_client's own default-Region construction -- see this
    module's docstring for why the image-generation Bedrock calls have
    to be made cross-Region from this stack's us-west-1 home. If
    IMAGE_BUCKET isn't configured on a given deployment, image_bucket is
    falsy and generate_article_for_product's own image branch is skipped
    entirely, same defensive "not configured -> soft no-op" posture
    admin_api.queue_article_generation already uses for the text function
    name."""
    import boto3

    bedrock_client = boto3.client("bedrock-runtime")
    image_region = os.environ.get("BEDROCK_IMAGE_REGION", DEFAULT_BEDROCK_IMAGE_REGION)
    bedrock_image_client = boto3.client("bedrock-runtime", region_name=image_region)
    s3_client = boto3.client("s3")
    model_id = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_BEDROCK_MODEL_ID)
    image_model_id = os.environ.get("BEDROCK_IMAGE_MODEL_ID", DEFAULT_BEDROCK_IMAGE_MODEL_ID)
    image_bucket = os.environ.get("IMAGE_BUCKET")

    conn = get_db_connection()
    try:
        if event.get("product_id"):
            result = generate_article_for_product(
                conn, bedrock_client, model_id, event["product_id"],
                s3_client=s3_client, bedrock_image_client=bedrock_image_client,
                image_model_id=image_model_id, image_bucket=image_bucket, force=True,
            )
            return {"statusCode": 200, "body": json.dumps({"results": [result]})}

        product_ids = list_products_needing_article(conn)
        results = []
        for product_id in product_ids:
            try:
                results.append(generate_article_for_product(
                    conn, bedrock_client, model_id, product_id,
                    s3_client=s3_client, bedrock_image_client=bedrock_image_client,
                    image_model_id=image_model_id, image_bucket=image_bucket,
                ))
            except Exception:
                logger.exception("Failed to generate article for product_id=%s", product_id)
                results.append({"product_id": product_id, "generated": False, "reason": "error"})

        generated = sum(1 for r in results if r["generated"])
        return {"statusCode": 200, "body": json.dumps({
            "checked": len(product_ids), "generated": generated, "results": results,
        })}
    finally:
        conn.close()
