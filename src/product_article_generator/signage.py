"""
In-store signage generation (DEPLOY_RUNBOOK.md 6cn), run by the article
generator Lambda when admin_api invokes it with {"signage": ...}.

Al: "can there just be a generate button on the ball on the admin site so we
can pick and choose" -- the stores' piSignage screens get a 9:16 looping
video per ball, made on demand from the article preview in admin. The
prototype (6cm) settled the recipe; this is it, productized:

  signage="stills"   3 native 9:16 "signage shot" stills (Gemini, built on
                     the article's own scene prompt + signage composition
                     rules -- Al: "generate a third image that is the aspect
                     ratio so it feels right. not something that is cropped
                     incorrectly") + tagline options (Bedrock Haiku).
  signage="animate"  the picked still -> Veo 3.1 Fast image-to-video (8 s,
                     9:16, 1080p, no audio) -> raw clip to S3; where the ball
                     sits in the frame (Haiku vision, for the callout
                     lines); then the signage renderer is invoked to make
                     the seamless loop.

Lessons baked in from the prototype:
  * NO lastFrame for Veo: first frame == last frame made it hold the scene
    still and paint embers/sparks/flares on top ("the background is not
    animating there is some strange over lay that is"). Ask for real scene
    motion; ban particles/fire/flares in the negative prompt. The renderer
    makes the loop with a crossfade instead.
  * Taglines are 2-6 words on the ball's visual theme, never an instruction,
    website, price or the pro shop ("these will be in the pro shop so you
    are already there so just be more to the point").

Every function that talks to a service takes its client/auth as an
argument, so the pure parts (prompts, parsing, validation) unit-test
without AWS or Google.
"""
import base64
import json
import logging
import re
import time
import uuid

logger = logging.getLogger(__name__)

STILL_CANDIDATES = 3
STILL_ASPECT_RATIO = "9:16"
VEO_MODEL_ID = "veo-3.1-fast-generate-001"
VEO_REGION = "us-central1"  # Veo is regional; Gemini images use "global"
VEO_DURATION_S = 8
VEO_POLL_S = 10
VEO_TIMEOUT_S = 420
DEFAULT_BALL_POSITION = {"x": 0.5, "y": 0.62, "r": 0.27}

SIGNAGE_COMPOSITION = """

SIGNAGE COMPOSITION (this image is a 9:16 VERTICAL in-store display poster, composed natively for portrait -- not a crop of a landscape scene, and never a landscape image padded with blurred or empty bars):
- Place the ball in the lower-middle of the frame (its center roughly 58-65% of the way down), large and prominent, the clear hero, fully in frame with breathing room on all sides.
- Keep the top ~25% of the frame calm and uncluttered (sky, soft gradient, out-of-focus depth): a title will be overlaid there.
- Keep the bottom ~18% of the frame darker and simple: a price will be overlaid there.
- Leave the left and right edges beside the ball relatively clean: small spec labels with thin pointer lines will be overlaid there.
- Build depth in layers (foreground, the ball, background) with clear directional energy, so the scene can later be animated.
- Absolutely no text, numbers, words, watermarks, or logos anywhere in the image other than the ball's own printed graphics exactly as in the reference."""

VEO_NEGATIVE_PROMPT = (
    "text, words, letters, watermark, logo distortion, morphing ball, warped ball, people, hands, camera shake, "
    "flicker, embers, sparks, particles, fire, flames, explosion, lens flare, light flashes, static scene, frozen frame"
)


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def build_still_prompt(scene_prompt: str) -> str:
    """The article generator's own action-shot scene prompt (visual_theme,
    every logo/no-people/no-pins rule learned the hard way) plus the
    signage composition rules."""
    return scene_prompt + SIGNAGE_COMPOSITION


def build_tagline_prompt(brand: str, name: str, visual_theme: str) -> str:
    return f"""Write a tagline for an in-store digital sign in a bowling pro shop. The shopper is already standing in the pro shop looking at this ball.

Ball: {brand} {name}
Visual theme of the ball's artwork: {visual_theme or "(none -- riff on the ball's name)"}

Rules:
- 2 to 6 words. Punchy, confident, to the point.
- Riff on the ball's visual theme (its imagery and energy).
- Do NOT tell the shopper to do anything (no "ask", "visit", "get", "try", "come", "talk to").
- Never mention a website, online, prices, or the pro shop itself.
- No performance claims or numbers. No hashtags, emoji, or quotation marks.

Give 8 different options, one per line, nothing else."""


_BANNED_TAGLINE = re.compile(r"\b(ask|visit|website|online|\.com|www|price|\$|shop|scan|qr|click)\b", re.IGNORECASE)


def parse_tagline_options(raw: str, limit: int = 8) -> list:
    """One tagline per line -> cleaned list. Drops numbering/bullets/quotes,
    anything over 8 words, and anything that breaks the in-store rules (a
    model occasionally slips "ask our pro..." back in)."""
    out = []
    for line in (raw or "").splitlines():
        t = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip('"“”\'').strip()
        if not t or len(t.split()) > 8 or _BANNED_TAGLINE.search(t):
            continue
        if t.lower() not in {o.lower() for o in out}:
            out.append(t)
    return out[:limit]


def build_veo_prompt(visual_theme: str) -> str:
    theme = f" The scene's theme: {visual_theme.strip()}" if visual_theme else ""
    return (
        "Cinematic animation of this exact illustrated scene with strong, continuous natural motion throughout the "
        "whole frame: the environment moves naturally (clouds, water, light, air, the backdrop drift at different "
        "speeds for real depth), and any creature or figure in the scene comes alive and moves. The camera makes a "
        "slow, smooth forward drift. The bowling ball stays in the lower middle of the frame and remains perfectly "
        "sharp with its colors, swirl pattern and printed logo exactly as shown -- the logo never changes shape, "
        "size or position; the ball may turn very slightly. Same art style throughout. No text, no new objects, "
        "no people." + theme
    )


def parse_ball_position(raw: str) -> dict:
    """Haiku's {"cx","cy","r"} (fractions) -> validated {"x","y","r"};
    falls back to DEFAULT_BALL_POSITION if missing or implausible (the
    callout lines then just aim at the composition's intended spot)."""
    try:
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        d = json.loads(m.group(0))
        x, y, r = float(d["cx"]), float(d["cy"]), float(d["r"])
    except Exception:  # noqa: BLE001 -- any parse failure -> default
        return dict(DEFAULT_BALL_POSITION)
    if not (0.15 <= x <= 0.85 and 0.3 <= y <= 0.9 and 0.08 <= r <= 0.45):
        return dict(DEFAULT_BALL_POSITION)
    return {"x": round(x, 3), "y": round(y, 3), "r": round(r, 3)}


BALL_POSITION_PROMPT = (
    "This is a 9:16 vertical image with one bowling ball in it. Return ONLY JSON "
    '{"cx": <center x>, "cy": <center y>, "r": <radius>} where cx is the ball center as a fraction of the image '
    "WIDTH (0-1), cy the center as a fraction of the image HEIGHT (0-1), and r the ball's radius as a fraction of "
    "the image WIDTH."
)


# --------------------------------------------------------------------------
# Service calls
# --------------------------------------------------------------------------

def call_bedrock_text(bedrock_client, model_id: str, prompt: str, max_tokens: int = 300, image_png: bytes = None) -> str:
    content = []
    if image_png is not None:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                      "data": base64.b64encode(image_png).decode()}})
    content.append({"type": "text", "text": prompt})
    body = json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": content}]})
    resp = bedrock_client.invoke_model(modelId=model_id, contentType="application/json", accept="application/json", body=body)
    return json.loads(resp["body"].read())["content"][0]["text"].strip()


def call_veo(session, access_token: str, project_id: str, image_png: bytes, prompt: str,
             model_id: str = VEO_MODEL_ID, region: str = VEO_REGION) -> bytes:
    """Veo 3.1 image-to-video on Vertex AI: predictLongRunning, then poll
    fetchPredictOperation until done. Video comes back inline (no
    storageUri). Raises on errors, safety filtering, or timeout."""
    base = (f"https://{region}-aiplatform.googleapis.com/v1/projects/{project_id}/locations/{region}"
            f"/publishers/google/models/{model_id}")
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    body = {
        "instances": [{"prompt": prompt, "image": {"bytesBase64Encoded": base64.b64encode(image_png).decode(),
                                                   "mimeType": "image/png"}}],
        "parameters": {"aspectRatio": "9:16", "durationSeconds": VEO_DURATION_S, "resolution": "1080p",
                       "generateAudio": False, "sampleCount": 1, "negativePrompt": VEO_NEGATIVE_PROMPT},
    }
    r = session.post(f"{base}:predictLongRunning", headers=headers, json=body, timeout=60)
    r.raise_for_status()
    op = r.json()["name"]
    started = time.time()
    while True:
        time.sleep(VEO_POLL_S)
        p = session.post(f"{base}:fetchPredictOperation", headers=headers, json={"operationName": op}, timeout=60)
        p.raise_for_status()
        d = p.json()
        if d.get("done"):
            break
        if time.time() - started > VEO_TIMEOUT_S:
            raise TimeoutError(f"Veo didn't finish within {VEO_TIMEOUT_S}s")
    if d.get("error"):
        raise RuntimeError(f"Veo error: {json.dumps(d['error'])[:300]}")
    videos = (d.get("response") or {}).get("videos") or []
    if not videos:
        reasons = (d.get("response") or {}).get("raiMediaFilteredReasons")
        raise RuntimeError(f"Veo returned no video (filtered: {reasons})")
    return base64.b64decode(videos[0]["bytesBase64Encoded"])


# --------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------

def load_signage_context(conn, article_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            select pa.id, pa.product_id, pa.visual_theme, p.name, b.name,
                   s.selected_still_key, s.selected_still_url
            from product_articles pa
            join products p on p.id = pa.product_id
            join brands b on b.id = p.brand_id
            left join article_signage s on s.article_id = pa.id
            where pa.id = %s
            """,
            (article_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"No ball article {article_id}")
    keys = ("article_id", "product_id", "visual_theme", "name", "brand", "selected_still_key", "selected_still_url")
    return dict(zip(keys, [str(v) if k in ("article_id", "product_id") else v for k, v in zip(keys, row)]))


def update_signage(conn, article_id: str, **fields) -> None:
    """Upsert the given columns on article_signage (jsonb values passed as
    Python lists/dicts are serialized)."""
    cols = list(fields)
    vals = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in fields.values()]
    assignments = ", ".join(f"{c} = excluded.{c}" for c in cols)
    with conn.cursor() as cur:
        cur.execute(
            f"insert into article_signage (article_id, {', '.join(cols)}, updated_at) "
            f"values (%s, {', '.join(['%s'] * len(cols))}, now()) "
            f"on conflict (article_id) do update set {assignments}, updated_at = now()",
            [article_id, *vals],
        )
    conn.commit()


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

def generate_stills(conn, ctx: dict, *, scene_prompt: str, reference_b64: str, gemini_call, s3_client,
                    image_bucket: str, bedrock_client, model_id: str) -> dict:
    """signage="stills". gemini_call(prompt, reference_b64, aspect_ratio) ->
    png bytes (the generator's call_gemini_for_image, bound to its auth)."""
    prompt = build_still_prompt(scene_prompt)
    run = new_run_id()
    stills = []
    for i in range(STILL_CANDIDATES):
        try:
            png = gemini_call(prompt, reference_b64, STILL_ASPECT_RATIO)
        except Exception:  # one bad candidate shouldn't sink the batch
            logger.exception("signage still %d failed for article %s", i + 1, ctx["article_id"])
            continue
        key = f"article-images/{ctx['product_id']}/signage/still_{i + 1}_{run}.png"
        s3_client.put_object(Bucket=image_bucket, Key=key, Body=png, ContentType="image/png")
        stills.append({"key": key, "url": f"https://{image_bucket}.s3.amazonaws.com/{key}"})
    if not stills:
        raise RuntimeError("No signage stills could be generated")
    taglines = parse_tagline_options(call_bedrock_text(
        bedrock_client, model_id, build_tagline_prompt(ctx["brand"], ctx["name"], ctx["visual_theme"])))
    update_signage(conn, ctx["article_id"], status="stills_ready", job_started_at=None, error=None,
                   still_candidates=stills, tagline_options=taglines)
    return {"stills": len(stills), "taglines": len(taglines)}


def animate(conn, ctx: dict, *, still_png: bytes, veo_call, s3_client, image_bucket: str,
            bedrock_client, model_id: str, invoke_renderer) -> dict:
    """signage="animate". veo_call(png, prompt) -> mp4 bytes. Leaves status
    'animating'; the renderer's loop step flips it to 'ready'."""
    clip = veo_call(still_png, build_veo_prompt(ctx["visual_theme"]))
    key = f"article-images/{ctx['product_id']}/signage/clip_raw_{new_run_id()}.mp4"
    s3_client.put_object(Bucket=image_bucket, Key=key, Body=clip, ContentType="video/mp4")
    try:
        pos = parse_ball_position(call_bedrock_text(bedrock_client, model_id, BALL_POSITION_PROMPT, 100, image_png=still_png))
    except Exception:  # noqa: BLE001 -- position is a nicety; default is fine
        logger.exception("ball position detection failed for article %s", ctx["article_id"])
        pos = dict(DEFAULT_BALL_POSITION)
    update_signage(conn, ctx["article_id"], raw_clip_key=key, ball_x=pos["x"], ball_y=pos["y"], ball_r=pos["r"])
    invoke_renderer({"mode": "loop", "article_id": ctx["article_id"]})
    return {"raw_clip_key": key, "ball": pos}
