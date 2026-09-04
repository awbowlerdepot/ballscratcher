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
THREE rewrites, 024/025/026 -- see v4 below for where this landed and
why): Al's follow-up ask, "can we have it generate some images for the
article from the bowling ball images and using the article to give it
some context" -- scoped via a follow-up AskUserQuestion exchange to
generate BOTH an action/lifestyle hero shot and a stylized product hero
shot, automatically in this same Lambda run (both the daily batch and the
on-demand regenerate path), gated behind the article's existing
pending/approved/rejected review same as the text. Image generation is
deliberately best-effort and non-fatal: if it fails (bad reference photo,
API error, etc.) the article's own text still stores and goes to review
-- see generate_article_image_candidates' own docstring.

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
    but Al's review of real reference examples found the RESULT wasn't
    what he wanted, backgrounds should be driven by the product's own
    NAME/branding rather than generic, and -- verbatim -- "I think we
    are trying to be to technical on this maybe there is a better ai
    model for this." Asked directly, Al confirmed he'd trade the "never
    altered" guarantee for better, simpler results.
  - v3 (025): back to a SINGLE Stable Diffusion image-to-image call per
    variant, theme-driven prompt, letterboxed reference canvas for
    aspect ratio. Shipped, and Al sent back real screenshots: the ball's
    own surface graphics/logo text came out GARBLED ("Voiid"/"Bowing"
    instead of the real brand text), and the backgrounds were poor --
    verbatim, "looks like dropping the cutout was a bad idea... The ball
    needs to be mostly untouched... The background couldn't be worse."
  - v4 (026, current): Al's concrete ask was explicit -- "The ball needs
    to be masked to get rid of anything that is in the image now, white
    mostly. Then that needs to be places in a scene based on its name.
    The fallout is a perfect example of this" -- then sent a THIRD
    reference, from a tool called yeri.ai, built from the prompt
    "generate a scene depicting a Fallout environment and then place the
    ball in the reference image into that scene, match the color scheme
    of the reference image." That result was visibly better than
    anything Stability/Bedrock had produced (real ambient lighting/
    shadow on the ball, no hard-pasted edge) -- researched what's behind
    it: Google's Gemini 2.5 Flash Image model ("Nano Banana"), documented
    by Google itself as built for exactly this ("put an object into a
    scene... maintain character consistency"), a genuinely different
    capability from Stability's single-image text-to-image/image-to-
    image models. Confirmed Gemini image models are NOT on Bedrock as of
    this writing -- reaching it means calling Google's own Gemini API
    directly (see call_gemini_for_image), a new outbound HTTPS call and a
    new API key secret, not an AWS-internal one. Asked Al directly
    whether to add this non-Bedrock provider: yes, ALONGSIDE the existing
    Bedrock/Stability path, not instead of it.

    v4 also changes HOW an image gets chosen: asked directly, Al wants a
    per-article candidate picker, not a system-wide default model -- see
    026_product_article_image_candidates.sql. Every run generates 3
    candidates per shot (2 from Gemini via call_gemini_for_image, 1 from
    the revived v2 Stability mechanism -- call_bedrock_remove_background
    + build_background_prompts + call_bedrock_generate_background +
    composite_ball_on_background, all stored, and an admin picks the best
    one in the Articles review tab (admin_api.select_article_image_
    candidate) instead of the pipeline silently committing to whichever
    came out first. The first-generated candidate for each shot (a
    Gemini one, since Gemini is listed first) is auto-selected as the
    live default so the public site always has SOMETHING before a human
    reviews it, same "best-effort now, refined by review later" posture
    the rest of this feature already uses.

Model/provider choice, researched (not assumed) before building, same
defensive posture as the text model's own us-west-1 CRIS situation above.
Amazon Nova Canvas is Legacy with an EOL of 2026-09-30 (weeks away) and
was never available in us-west-1 to begin with (only us-east-1/eu-west-1/
ap-northeast-1, all Legacy). Amazon Titan Image Generator G1 v2 is
ALREADY past its EOL (2026-06-30). Neither supports Geo/Global cross-
Region inference profiles at all, so there's no CRIS workaround for
either. Stability AI Stable Diffusion 3.5 Large (`stability.sd3-5-large-
v1:0`) is kept for the ONE Bedrock/composite candidate per shot -- only
available in `us-west-2` (Oregon) on Bedrock, not this stack's own home
Region (`us-west-1`); the image Bedrock client is constructed with an
explicit `region_name` (see handler(), BEDROCK_IMAGE_REGION), a plain
cross-Region API call, not CRIS (image models don't support it). Remove
Background (Stability AI Image Services, `us.stability.stable-image-
remove-background-v1:0`) is BACK as of v4 for that same composite
candidate -- see the Geographic-CRIS IAM shape this needs (already solved
once, see DEPLOY_RUNBOOK's 6t section for that saga's full history; v4
reuses that exact, already-correct policy shape rather than re-deriving
it) and BEDROCK_REMOVEBG_REGION (`us-east-1`, a THIRD Region, distinct
from both `us-west-1` and `us-west-2`). Gemini 2.5 Flash Image
(`gemini-2.5-flash-image`) is called directly against Google's own REST
API (`https://generativelanguage.googleapis.com`, see GEMINI_API_BASE_URL
and call_gemini_for_image) using a plain `requests.post` -- no new SDK
dependency (this module already depends on `requests`), authenticated via
an `x-goog-api-key` header whose value comes from a NEW Secrets Manager
secret (GEMINI_API_KEY_SECRET_ARN), same "structured JSON secret, fetched
once via boto3 secretsmanager, never hardcoded" convention get_db_
connection already uses for DB credentials. IMPORTANT: this module's own
parsing of Gemini's response shape (candidates[].content.parts[].
inlineData.data) is built from Google's published REST examples, NOT
verified against a live invocation from inside this environment (no
outbound access to generativelanguage.googleapis.com from this sandbox,
and no real API key to test with) -- same honest posture the Remove
Background IAM fix took before its first real deploy; confirm this on
the first real invocation, don't assume it's correct.
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

# Stability AI Stable Diffusion 3.5 Large -- used here for the ONE
# Bedrock/composite candidate per shot (background generation only, text-
# to-image mode -- see build_background_prompts/call_bedrock_generate_
# background). See this module's own docstring for the full model/Region
# research trail (Nova Canvas Legacy/EOL 2026-09-30, Titan Image
# Generator G1 v2 already past its 2026-06-30 EOL, neither ever available
# in us-west-1 anyway). Invoked as a plain on-demand foundation model (no
# CRIS inference profile -- image models don't support Geo/Global cross-
# Region inference at all, unlike the text model above), but in a
# DIFFERENT Region than the rest of this stack -- see DEFAULT_BEDROCK_
# IMAGE_REGION just below.
DEFAULT_BEDROCK_IMAGE_MODEL_ID = "stability.sd3-5-large-v1:0"

# Stable Diffusion 3.5 Large is only available in us-west-2 (Oregon) on
# Bedrock as of this writing -- not this stack's home Region (us-west-1).
# handler() constructs the bedrock-runtime client used for background-
# generation calls with region_name=this value explicitly, separate from
# both the text model's own bedrock_client (home Region) AND the remove-
# background client below (a THIRD Region) -- a plain cross-Region API
# call, since there's no CRIS profile for image models to route through
# instead. See this module's own docstring for the full research trail.
DEFAULT_BEDROCK_IMAGE_REGION = "us-west-2"

# Stability AI Image Services' "Remove Background" -- revived in v4 (was
# removed in v3, is back for the composite candidate's cutout step). Cuts
# the product's real photo out onto a transparent background BEFORE any
# generation happens, so the ball's own pixels reach the final composite
# untouched. Part of a separate 13-model Stability editing-primitives
# collection from SD3.5 Large's own text-to-image family -- see this
# module's own docstring, and DEPLOY_RUNBOOK's 6t section for the full
# Geographic-CRIS IAM research trail this model's grant needed.
DEFAULT_BEDROCK_REMOVE_BG_MODEL_ID = "us.stability.stable-image-remove-background-v1:0"

# Stability AI Image Services' own docs/examples consistently invoke
# these "us.stability.*" model ids in us-east-1 -- not this stack's home
# Region (us-west-1), and not the same Region as the background-
# generation client above (us-west-2) either. handler() constructs a
# THIRD, separate bedrock-runtime client with region_name=this value. See
# this module's own docstring for the full research trail.
DEFAULT_BEDROCK_REMOVE_BG_REGION = "us-east-1"

# Google's Gemini 2.5 Flash Image ("Nano Banana") -- the model actually
# behind the yeri.ai reference result Al sent (see this module's own
# docstring for the research trail). NOT a Bedrock model id -- this is
# Google's own model id, passed to call_gemini_for_image which builds a
# URL against GEMINI_API_BASE_URL. If Google ships a newer/better image-
# editing model later, this is the one parameter to change (kept as its
# own constant/env var rather than hardcoded in call_gemini_for_image for
# exactly that reason).
DEFAULT_GEMINI_IMAGE_MODEL_ID = "gemini-2.5-flash-image"

# Gemini's REST API -- v1beta as of this writing (Google's own docs use
# this version for the image-generation endpoint; there is no GA/v1
# equivalent for image output as of this writing, only for text). The
# model id is appended as a path segment (":generateContent") by call_
# gemini_for_image, same pattern Google's own curl examples use.
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# aspect_ratio is a TEXT-to-image-only parameter on Stable Diffusion 3.5
# Large (this module only ever calls it in text-to-image mode now, for
# background generation -- see call_bedrock_generate_background) -- Al's
# "specific aspect ratio" ask, satisfied per-variant: 16:9 for the
# action/lifestyle shot (a wide lane scene reads better landscape), 1:1
# for the stylized product shot (square reads better as a catalog/
# thumbnail image). Gemini's own imageConfig also accepts an aspectRatio
# string in the same "16:9"/"1:1" shape (see call_gemini_for_image), so
# these same values are reused for both providers rather than needing a
# second, provider-specific representation.
_ACTION_SHOT_ASPECT_RATIO = "16:9"
_PRODUCT_SHOT_ASPECT_RATIO = "1:1"
_VARIANT_ASPECT_RATIOS = {
    "action_shot": _ACTION_SHOT_ASPECT_RATIO,
    "product_shot": _PRODUCT_SHOT_ASPECT_RATIO,
}

# The v4 "try a few models, pick in the admin UI" split (see this
# module's own docstring, and 026_product_article_image_candidates.sql):
# 2 Gemini candidates (no reliable seed parameter to vary by -- see
# GEMINI candidates' own docstring in generate_article_image_candidates
# -- so diversity comes from calling twice with a lightly varied prompt)
# plus 1 Stability/composite candidate (kept as the one guaranteed-
# untouched-ball baseline, per the original v2 mechanism). 3 total per
# shot, matching Al's explicit "3 candidates, automatically" answer.
NUM_GEMINI_CANDIDATES_PER_VARIANT = 2
NUM_STABILITY_CANDIDATES_PER_VARIANT = 1

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
    no image at all -- generate_article_image_candidates treats that as
    "skip images for this run", not a failure."""
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
    """Both Bedrock's Remove Background model and Gemini's image API
    require a base64-encoded image; re-encoding through Pillow (same
    library/version pin as image_processor's own requirements.txt)
    normalizes whatever format the source photo actually is (often JPEG
    from a manufacturer CDN) into PNG rather than trusting the source's
    own file extension."""
    import base64
    import io

    from PIL import Image

    image = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _resolve_visual_context(article: dict) -> str:
    """Shared fallback chain used by every image prompt builder below
    (Stability's background-only prompt and Gemini's integrated scene
    prompt alike): article's own visual_theme FIRST -- the field the
    article-generation model itself derived from the product's own name/
    branding (build_article_prompt's optional ask), per Al's explicit
    "background driven by the ball's name" answer -- falling back to
    performance_summary, then hook, if visual_theme came back empty (it's
    an OPTIONAL field -- see parse_article_json/_REQUIRED_ARTICLE_KEYS).
    Capped at 300 chars, same budget every version of this prompt-building
    code has used."""
    theme = (article.get("visual_theme") or "").strip()
    context = theme or (article.get("performance_summary") or article.get("hook") or "").strip()
    return context[:300]


def call_bedrock_remove_background(bedrock_removebg_client, model_id: str, reference_image_b64: str) -> bytes:
    """Bedrock's Stability AI "Remove Background" model -- a segmentation
    cutout, not a diffusion regeneration: the ball's own pixels are
    carried straight through untouched, only the background around it is
    stripped to transparent (alpha channel), so the returned PNG can be
    pasted onto a new background later (composite_ball_on_background)
    without ever having regenerated the ball itself. Revived in v4 for
    the one Stability/composite candidate per shot (see this module's own
    docstring). bedrock_removebg_client is a bedrock-runtime client scoped
    to DEFAULT_BEDROCK_REMOVE_BG_REGION (us-east-1), a THIRD Region
    distinct from both the text model's own bedrock_client and the
    background-generation client below -- see this module's docstring for
    why."""
    import base64

    body = json.dumps({
        "image": reference_image_b64,
        "output_format": "png",
    })
    response = bedrock_removebg_client.invoke_model(modelId=model_id, contentType="application/json",
                                                      accept="application/json", body=body)
    payload = json.loads(response["body"].read())
    finish_reasons = [r for r in (payload.get("finish_reasons") or []) if r]
    if finish_reasons:
        raise RuntimeError(f"Remove Background returned a non-null finish reason: {finish_reasons}")
    images = payload.get("images") or []
    if not images:
        raise RuntimeError("Remove Background response contained no images")
    return base64.b64decode(images[0])


def build_background_prompts(product: dict, article: dict) -> dict:
    """Two short, literal BACKGROUND-only scene-description prompts for
    Stable Diffusion 3.5 Large's plain text-to-image mode -- for the ONE
    Stability/composite candidate per shot (revived in v4; see this
    module's own docstring). Grounded in _resolve_visual_context
    (visual_theme first, per Al's "background driven by the ball's name"
    answer). Deliberately never describes the ball itself, or even
    mentions "bowling ball" as a positive element -- the ball is composited
    in afterward from the real, untouched cutout (see composite_ball_on_
    background), not generated, so describing one here would risk the
    model painting a second, different-looking ball into the scene that
    then gets pasted over (or peeks out from behind) the real one.
    _BACKGROUND_NEGATIVE_PROMPT reinforces that same "no ball" instruction
    as a negative_prompt too, for the same reason."""
    context = _resolve_visual_context(article)

    action_prompt = (
        "Empty bowling alley lane viewed down its length toward the pins, "
        "editorial sports-photography lighting, subtle motion-blurred lane arrows, "
        "dramatic side lighting, shallow depth of field, pins softly visible in the "
        "distance, polished wood lane surface, no bowling ball, no product, no "
        "people, no hands, no text, no logos, no watermark."
    )
    product_prompt = (
        "Elevated studio product-photography backdrop, reflective dark surface, "
        "soft dramatic rim lighting, premium catalog-photography style, clean "
        "gradient background, no bowling ball, no product, no people, no hands, "
        "no text, no logos, no watermark."
    )
    if context:
        action_prompt += f" Scene should evoke: {context}"
        product_prompt += f" Scene should evoke: {context}"

    return {"action_shot": action_prompt, "product_shot": product_prompt}


_BACKGROUND_NEGATIVE_PROMPT = (
    "bowling ball, product, sports equipment, text, watermark, logo, signature, "
    "people, hands, blurry, low quality"
)


def call_bedrock_generate_background(bedrock_image_client, model_id: str, prompt: str,
                                      aspect_ratio: str, seed: int = None) -> bytes:
    """Stable Diffusion 3.5 Large's plain TEXT-to-image mode -- no
    reference image, generating the background scene alone (see
    build_background_prompts for why the ball itself is deliberately
    absent from the prompt). aspect_ratio is a text-to-image-only
    parameter on this model (silently ignored in image-to-image mode) --
    this is Al's "specific aspect ratio" ask, set per-variant by the
    caller (see _VARIANT_ASPECT_RATIOS). seed is optional (Stability
    defaults to a random seed when omitted/0) -- passed through and
    recorded on the resulting candidate row so a specific-looking result
    can be reproduced or deliberately avoided on a future regenerate,
    even though v4 only ever requests ONE Stability candidate per shot
    today (see NUM_STABILITY_CANDIDATES_PER_VARIANT). bedrock_image_client
    is scoped to DEFAULT_BEDROCK_IMAGE_REGION (us-west-2) -- see this
    module's docstring for why that's a different Region from both the
    text model's own bedrock_client and the remove-background client
    above."""
    import base64

    body = {
        "prompt": prompt,
        "mode": "text-to-image",
        "aspect_ratio": aspect_ratio,
        "negative_prompt": _BACKGROUND_NEGATIVE_PROMPT,
        "output_format": "png",
    }
    if seed:
        body["seed"] = seed
    response = bedrock_image_client.invoke_model(modelId=model_id, contentType="application/json",
                                                   accept="application/json", body=json.dumps(body))
    payload = json.loads(response["body"].read())
    finish_reasons = [r for r in (payload.get("finish_reasons") or []) if r]
    if finish_reasons:
        raise RuntimeError(f"Stable Diffusion returned a non-null finish reason: {finish_reasons}")
    images = payload.get("images") or []
    if not images:
        raise RuntimeError("Stable Diffusion response contained no images")
    return base64.b64decode(images[0])


def composite_ball_on_background(cutout_png_bytes: bytes, background_png_bytes: bytes) -> bytes:
    """Pastes the untouched ball cutout (RGBA, transparent background,
    produced by call_bedrock_remove_background straight off the
    product's real photo) onto the freshly text-to-image-generated
    background (call_bedrock_generate_background) using plain Pillow
    alpha compositing -- NOT a second diffusion pass, so the ball's own
    pixels are pasted through byte-for-byte from the cutout and are never
    regenerated a second time. Revived in v4 for the one Stability/
    composite candidate per shot -- kept unchanged from v2's own version;
    the complaint that killed v3 was about the img2img mechanism
    replacing this pipeline entirely, not about this function's own
    output quality specifically.

    The cutout is first cropped to its own opaque bounding box (Remove
    Background can leave an inconsistent amount of transparent margin
    around the subject) so its visible size scales predictably, then
    resized to occupy a fixed fraction of the background's shorter side
    and pasted slightly below center -- standard hero-product-shot
    grounding. A soft, blurred shadow ellipse is drawn on its own layer
    UNDER the cutout paste purely for visual grounding; it never risks
    obscuring the ball's own pixels since it's composited first, then the
    untouched cutout goes on top of it."""
    import io

    from PIL import Image, ImageDraw, ImageFilter

    background = Image.open(io.BytesIO(background_png_bytes)).convert("RGBA")
    cutout = Image.open(io.BytesIO(cutout_png_bytes)).convert("RGBA")

    bg_w, bg_h = background.size

    bbox = cutout.getbbox()
    if bbox:
        cutout = cutout.crop(bbox)

    target_size = int(min(bg_w, bg_h) * 0.62)
    cutout_w, cutout_h = cutout.size
    scale = target_size / max(cutout_w, cutout_h, 1)
    new_size = (max(1, int(cutout_w * scale)), max(1, int(cutout_h * scale)))
    cutout = cutout.resize(new_size, Image.LANCZOS)

    paste_x = (bg_w - new_size[0]) // 2
    paste_y = int(bg_h * 0.58) - new_size[1] // 2

    shadow_layer = Image.new("RGBA", background.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    shadow_w = int(new_size[0] * 0.8)
    shadow_h = max(8, int(new_size[1] * 0.18))
    shadow_x = paste_x + (new_size[0] - shadow_w) // 2
    shadow_y = paste_y + new_size[1] - shadow_h // 2
    shadow_draw.ellipse(
        [shadow_x, shadow_y, shadow_x + shadow_w, shadow_y + shadow_h],
        fill=(0, 0, 0, 110),
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=max(4, shadow_h // 3)))

    composite = Image.alpha_composite(background, shadow_layer)
    composite.alpha_composite(cutout, dest=(paste_x, paste_y))

    buf = io.BytesIO()
    composite.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def build_gemini_scene_prompt(product: dict, article: dict, variant: str) -> str:
    """The single integrated prompt Gemini 2.5 Flash Image gets, per
    variant -- unlike Stability's split "cutout here, background prompt
    there" approach, Gemini does the placement AND generation in one
    call, so the prompt has to say BOTH what scene to create and that the
    reference ball must be carried through unchanged. Mirrors the exact
    shape of the prompt that produced the yeri.ai reference result Al
    sent ("generate a scene depicting a Fallout environment and then
    place the ball in the reference image into that scene, match the
    color scheme of the reference image") -- grounded in _resolve_visual_
    context (visual_theme first) rather than a hardcoded "Fallout", since
    the theme has to be product-specific, not that one example."""
    context = _resolve_visual_context(article)
    scene_desc = context or "an elevated, premium studio scene"

    if variant == "action_shot":
        framing = "a dynamic editorial action/lifestyle photograph"
    else:
        framing = "an elevated, premium product photograph, not in motion"

    return (
        f"Generate a scene depicting {scene_desc}, then place the exact bowling "
        f"ball shown in the reference image into that scene as {framing}. Keep the "
        "ball itself completely unchanged -- the same colors, surface pattern, and "
        "logo/text exactly as shown in the reference image -- only change what's "
        "around it. Match the lighting and color grading of the new scene onto the "
        "ball naturally, with a realistic contact shadow and ambient light on its "
        "surface. Photorealistic, high quality, no text overlays, no watermark."
    )


def call_gemini_for_image(api_key: str, model_id: str, prompt: str,
                           reference_image_b64: str, aspect_ratio: str) -> bytes:
    """Google's Gemini 2.5 Flash Image, called directly via its own REST
    API (NOT Bedrock -- see this module's own docstring for why). A plain
    requests.post, same "raw JSON body, no heavy SDK" style this module
    already uses for every Bedrock InvokeModel call -- avoids adding
    google-genai (and its own dependency tree) to this Lambda's package
    just for one call shape.

    IMPORTANT: this function's request/response shape is built from
    Google's own published REST examples, NOT verified against a live
    invocation from inside this environment (no outbound access to
    generativelanguage.googleapis.com from this sandbox, no real API key
    to test with) -- confirm on the first real invocation, don't assume
    this is exactly right. In particular, the response field casing
    (`inlineData` in REST JSON vs. `inline_data` in the Python SDK's own
    object model) is defended against both ways below since published
    examples were inconsistent about which one is authoritative."""
    import base64

    import requests

    url = f"{GEMINI_API_BASE_URL}/{model_id}:generateContent"
    body = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/png", "data": reference_image_b64}},
            ],
        }],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": aspect_ratio},
        },
    }
    response = requests.post(
        url, headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=body, timeout=60,
    )
    response.raise_for_status()
    payload = response.json()

    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini response contained no candidates: {payload}")

    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            return base64.b64decode(inline["data"])

    finish_reason = candidates[0].get("finishReason") or candidates[0].get("finish_reason")
    raise RuntimeError(f"Gemini response contained no image data (finishReason={finish_reason})")


def store_article_image(s3_client, bucket: str, product_id: str, name: str, png_bytes: bytes) -> dict:
    """Mirrors image_processor.upload_variants' exact key/URL convention
    -- raw https://{bucket}.s3.amazonaws.com/{key} PNG URLs on the same
    public-read IMAGE_BUCKET -- but under an "article-images/" prefix
    rather than image_processor's own "product-images/" prefix (see
    023_product_article_images.sql's header comment: these aren't a
    product's real photos and shouldn't be swept up in product_scraper's
    product-images/* orphan-cleanup listing). `name` is the full per-
    candidate filename stem (e.g. "action_shot_gemini_1" or
    "product_shot_stability"), not just the bare variant -- v4 stores
    multiple candidates per variant (see 026_product_article_image_
    candidates.sql), so the un-suffixed "{variant}.png" key v1-v3 used
    would collide across candidates."""
    key = f"article-images/{product_id}/{name}.png"
    s3_client.put_object(Bucket=bucket, Key=key, Body=png_bytes, ContentType="image/png")
    return {"key": key, "url": f"https://{bucket}.s3.amazonaws.com/{key}"}


def generate_article_image_candidates(conn, bedrock_image_client, bedrock_removebg_client, gemini_api_key,
                                       s3_client, image_model_id: str, removebg_model_id: str,
                                       gemini_model_id: str, image_bucket: str,
                                       product: dict, article: dict) -> dict:
    """Best-effort, non-fatal: by the time this runs the article TEXT has
    already been generated successfully (see generate_article_for_
    product) -- an image failure here must never lose an otherwise-good
    article, so every failure path here logs and continues rather than
    raising. Returns {"action_shot": [...], "product_shot": [...]}, each
    a list of 0-3 candidate dicts ({"key", "url", "model_id", "seed"}),
    in the order they were generated (Gemini candidates first, then the
    Stability one) -- generate_article_for_product treats index 0 of each
    list as the auto-selected default (see that function's own docstring)
    and store_article_image_candidates persists the rest alongside it.

    v4 (see this module's own docstring for the full v1-v4 history): the
    product's real reference photo is fetched ONCE and reused across
    every candidate. Per variant, up to NUM_GEMINI_CANDIDATES_PER_VARIANT
    Gemini candidates run first, each fully independent (its own
    try/except) since Gemini needs nothing but the raw reference photo --
    no cutout, no separate background generation. GEMINI candidates use
    the SAME prompt for every call except the second one gets an
    "alternate composition" suffix appended -- Gemini's image model does
    not expose a reliable/reproducible seed parameter (confirmed via
    research, not assumed), so there is no seed to vary the way Stability
    candidates can; this suffix plus the model's own inherent generation
    stochasticity is what produces two different-looking results instead
    of two near-identical ones. The Stability/composite candidate runs
    ONCE per variant afterward, and needs the shared cutout (Remove
    Background, computed once for both variants -- a cutout failure skips
    the Stability candidate for BOTH variants, since there's no ball to
    composite into either one without it, but does NOT affect the Gemini
    candidates, which never depended on the cutout in the first place)."""
    reference_url = fetch_reference_image_url(conn, product["id"])
    if not reference_url:
        logger.info("No reference image available for product_id=%s, skipping article images", product["id"])
        return {}

    try:
        reference_bytes = fetch_reference_image_bytes(reference_url)
        reference_b64 = _reference_image_to_base64_png(reference_bytes)
    except Exception:
        logger.exception("Failed to fetch/prepare reference image for product_id=%s", product["id"])
        return {}

    cutout_png_bytes = None
    try:
        cutout_png_bytes = call_bedrock_remove_background(bedrock_removebg_client, removebg_model_id, reference_b64)
    except Exception:
        logger.exception(
            "Failed to remove background from reference image for product_id=%s -- the Stability/composite "
            "candidate will be skipped for both variants (Gemini candidates are unaffected)", product["id"],
        )

    background_prompts = build_background_prompts(product, article)
    results = {}
    for variant in ("action_shot", "product_shot"):
        candidates = []
        aspect_ratio = _VARIANT_ASPECT_RATIOS[variant]

        for i in range(NUM_GEMINI_CANDIDATES_PER_VARIANT):
            try:
                prompt = build_gemini_scene_prompt(product, article, variant)
                if i > 0:
                    prompt += " (Generate a distinct alternate composition/angle from any previous attempt.)"
                png_bytes = call_gemini_for_image(gemini_api_key, gemini_model_id, prompt, reference_b64, aspect_ratio)
                stored = store_article_image(s3_client, image_bucket, product["id"], f"{variant}_gemini_{i + 1}",
                                              png_bytes)
                candidates.append({"key": stored["key"], "url": stored["url"],
                                    "model_id": gemini_model_id, "seed": None})
            except Exception:
                logger.exception("Failed to generate Gemini %s candidate #%d for product_id=%s",
                                  variant, i + 1, product["id"])

        if cutout_png_bytes is not None:
            import random

            seed = random.randint(1, 2**31 - 1)
            try:
                background_png_bytes = call_bedrock_generate_background(
                    bedrock_image_client, image_model_id, background_prompts[variant], aspect_ratio, seed=seed,
                )
                composited_png_bytes = composite_ball_on_background(cutout_png_bytes, background_png_bytes)
                stored = store_article_image(s3_client, image_bucket, product["id"], f"{variant}_stability",
                                              composited_png_bytes)
                candidates.append({"key": stored["key"], "url": stored["url"],
                                    "model_id": image_model_id, "seed": seed})
            except Exception:
                logger.exception("Failed to generate Stability %s candidate for product_id=%s",
                                  variant, product["id"])

        results[variant] = candidates

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


def store_article_image_candidates(conn, article_id: str, candidates_by_variant: dict) -> None:
    """Persists EVERY candidate generate_article_image_candidates
    produced (not just the auto-selected one that landed in
    product_articles' own image columns via store_article) into
    026_product_article_image_candidates.sql's table, so an admin can
    later switch their pick via admin_api.select_article_image_candidate
    without re-running generation. Index 0 of each variant's candidate
    list is marked is_selected=true -- it's the same one store_article
    was called with as the live image (see generate_article_for_
    product), so this call and that one MUST stay in agreement about
    which candidate is "first"; both simply take candidates_by_variant[
    variant][0], never re-derive it independently. candidates_by_variant
    being empty (no images generated this run, or the whole image step
    was skipped) is a normal no-op, not an error."""
    if not candidates_by_variant:
        return
    with conn.cursor() as cur:
        for variant, candidates in candidates_by_variant.items():
            for i, candidate in enumerate(candidates):
                cur.execute(
                    """
                    insert into product_article_image_candidates
                        (article_id, variant, model_id, image_key, image_url, seed, is_selected)
                    values (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        article_id, variant, candidate["model_id"], candidate["key"], candidate["url"],
                        candidate.get("seed"), i == 0,
                    ),
                )
    conn.commit()


def generate_article_for_product(conn, bedrock_client, model_id: str, product_id: str,
                                  s3_client=None, bedrock_image_client=None, bedrock_removebg_client=None,
                                  gemini_api_key: str = None, image_model_id: str = None,
                                  removebg_model_id: str = None, gemini_model_id: str = None,
                                  image_bucket: str = None, force: bool = False) -> dict:
    """Orchestrates one product's full generation. force=True (the
    admin-triggered on-demand path -- see admin_api.queue_article_
    generation) skips the "already has an article" check the batch
    handler's own list_products_needing_article query applies; a human
    explicitly asking for a regenerate should always get one.

    s3_client/bedrock_image_client/bedrock_removebg_client/gemini_api_key/
    image_model_id/removebg_model_id/gemini_model_id/image_bucket are all
    optional (default None) so existing/simpler callers -- and every
    test that only cares about the article TEXT -- don't need to thread
    image plumbing through just to call this. When ALL EIGHT are supplied
    (the real handler() always supplies them, per Al's "automatically
    with the article" answer), image generation runs as an extra best-
    effort step after the article text is parsed but before it's stored:
    generate_article_image_candidates produces up to 3 candidates per
    shot (see that function's own docstring), index 0 of each variant's
    list becomes the auto-selected default passed to store_article (so
    both text and a starting image land in the same row/commit), and
    every candidate -- not just the auto-selected one -- is then persisted
    via store_article_image_candidates so an admin can pick a different
    one later without regenerating. bedrock_image_client/bedrock_
    removebg_client are separate clients from bedrock_client (not
    reused) -- each scoped to its own Region (see DEFAULT_BEDROCK_IMAGE_
    REGION/DEFAULT_BEDROCK_REMOVE_BG_REGION/this module's docstring).
    gemini_api_key is not a Bedrock client at all -- see call_gemini_for_
    image's own docstring for why Gemini is called directly instead."""
    product = fetch_product_content(conn, product_id)
    if product is None:
        return {"product_id": product_id, "generated": False, "reason": "product_not_found"}

    if not product["videos"]:
        return {"product_id": product_id, "generated": False, "reason": "no_qualifying_videos"}

    siblings = infer_sibling_products(conn, product)
    prompt = build_article_prompt(product, siblings)
    raw = call_bedrock_for_article(bedrock_client, model_id, prompt)
    article = parse_article_json(raw)

    candidates_by_variant = {}
    if (s3_client is not None and bedrock_image_client is not None and bedrock_removebg_client is not None
            and gemini_api_key and image_model_id and removebg_model_id and gemini_model_id and image_bucket):
        candidates_by_variant = generate_article_image_candidates(
            conn, bedrock_image_client, bedrock_removebg_client, gemini_api_key, s3_client,
            image_model_id, removebg_model_id, gemini_model_id, image_bucket, product, article,
        )

    images = {}
    for variant in ("action_shot", "product_shot"):
        variant_candidates = candidates_by_variant.get(variant) or []
        if variant_candidates:
            images[f"{variant}_image_key"] = variant_candidates[0]["key"]
            images[f"{variant}_image_url"] = variant_candidates[0]["url"]

    source_video_ids = [v["id"] for v in product["videos"]]
    sibling_product_ids = [s["id"] for s in siblings]
    article_id = store_article(conn, product_id, article, source_video_ids, sibling_product_ids, images=images)
    store_article_image_candidates(conn, article_id, candidates_by_variant)

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
    TranscriptFetcherFunction. Both paths pass s3_client/bedrock_image_
    client/bedrock_removebg_client/gemini_api_key/image_model_id/
    removebg_model_id/gemini_model_id/image_bucket through to generate_
    article_for_product so images generate automatically alongside the
    text in every run (Al's "Both image types, automatically with the
    article" answer) -- there is no separate on-demand-only image
    trigger. bedrock_image_client/bedrock_removebg_client are each
    constructed with their own explicit region_name (BEDROCK_IMAGE_
    REGION, BEDROCK_REMOVEBG_REGION respectively), separate from
    bedrock_client's own default-Region construction AND from each
    other -- see this module's docstring for why. gemini_api_key comes
    from a Secrets Manager secret (GEMINI_API_KEY_SECRET_ARN), same
    "fetch once via boto3 secretsmanager, never hardcoded" convention
    get_db_connection already uses for DB credentials -- the secret's
    own JSON shape is {"api_key": "..."}. If IMAGE_BUCKET or GEMINI_
    API_KEY_SECRET_ARN isn't configured on a given deployment, the
    corresponding value is falsy/None and generate_article_for_
    product's own image branch is skipped entirely (it requires ALL
    eight image-related arguments), same defensive "not configured ->
    soft no-op" posture admin_api.queue_article_generation already uses
    for the text function name."""
    import boto3

    bedrock_client = boto3.client("bedrock-runtime")
    image_region = os.environ.get("BEDROCK_IMAGE_REGION", DEFAULT_BEDROCK_IMAGE_REGION)
    bedrock_image_client = boto3.client("bedrock-runtime", region_name=image_region)
    removebg_region = os.environ.get("BEDROCK_REMOVEBG_REGION", DEFAULT_BEDROCK_REMOVE_BG_REGION)
    bedrock_removebg_client = boto3.client("bedrock-runtime", region_name=removebg_region)
    s3_client = boto3.client("s3")
    model_id = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_BEDROCK_MODEL_ID)
    image_model_id = os.environ.get("BEDROCK_IMAGE_MODEL_ID", DEFAULT_BEDROCK_IMAGE_MODEL_ID)
    removebg_model_id = os.environ.get("BEDROCK_REMOVEBG_MODEL_ID", DEFAULT_BEDROCK_REMOVE_BG_MODEL_ID)
    gemini_model_id = os.environ.get("GEMINI_IMAGE_MODEL_ID", DEFAULT_GEMINI_IMAGE_MODEL_ID)
    image_bucket = os.environ.get("IMAGE_BUCKET")

    gemini_api_key = None
    gemini_secret_arn = os.environ.get("GEMINI_API_KEY_SECRET_ARN")
    if gemini_secret_arn:
        try:
            secretsmanager_client = boto3.client("secretsmanager")
            secret = json.loads(secretsmanager_client.get_secret_value(SecretId=gemini_secret_arn)["SecretString"])
            gemini_api_key = secret["api_key"]
        except Exception:
            logger.exception("Failed to fetch Gemini API key from Secrets Manager (arn=%s) -- Gemini image "
                              "candidates will be skipped this run", gemini_secret_arn)

    conn = get_db_connection()
    try:
        if event.get("product_id"):
            result = generate_article_for_product(
                conn, bedrock_client, model_id, event["product_id"],
                s3_client=s3_client, bedrock_image_client=bedrock_image_client,
                bedrock_removebg_client=bedrock_removebg_client, gemini_api_key=gemini_api_key,
                image_model_id=image_model_id, removebg_model_id=removebg_model_id,
                gemini_model_id=gemini_model_id, image_bucket=image_bucket, force=True,
            )
            return {"statusCode": 200, "body": json.dumps({"results": [result]})}

        product_ids = list_products_needing_article(conn)
        results = []
        for product_id in product_ids:
            try:
                results.append(generate_article_for_product(
                    conn, bedrock_client, model_id, product_id,
                    s3_client=s3_client, bedrock_image_client=bedrock_image_client,
                    bedrock_removebg_client=bedrock_removebg_client, gemini_api_key=gemini_api_key,
                    image_model_id=image_model_id, removebg_model_id=removebg_model_id,
                    gemini_model_id=gemini_model_id, image_bucket=image_bucket,
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
