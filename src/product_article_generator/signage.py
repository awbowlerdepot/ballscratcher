"""
In-store signage generation (DEPLOY_RUNBOOK.md 6cn), run by the article
generator Lambda when admin_api invokes it with {"signage": ...}.

Al: "can there just be a generate button on the ball on the admin site so we
can pick and choose" -- the stores' piSignage screens get a 9:16 looping
video per ball, made on demand from the article preview in admin. The
prototype (6cm) settled the recipe; this is it, productized:

  signage="stills"   3 native 9:16 "signage shot" stills (Gemini outpaints a
                     source image -- see below -- under the signage
                     composition rules; Al: "generate a third image that is
                     the aspect ratio so it feels right. not something that
                     is cropped incorrectly") + tagline options (Haiku).
  signage="animate"  the picked still -> Veo 3.1 Fast image-to-video (8 s,
                     9:16, 1080p, no audio) -> Veo EXTENSION of that clip
                     to 15 s (runbook 6cr) -> raw clip to S3; where the
                     ball sits in the frame (Haiku vision, for the callout
                     lines); then the signage renderer is invoked to make
                     the seamless 14 s loop.

The 15 s background (runbook 6cr). Al: "i noticed that the video for the
widow spare + is less than 14 seconds so it loops. was that by design?" It
was: Veo makes at most 8 s, so the 14 s MP4 played a 7 s loop twice -- fine
for a page that loops forever, visibly repetitive in a piSignage playlist
that plays each ball once. Veo's extension (same model, the clip passed
back as bytes -- no GCS bucket needed) continues the clip by 7 s and returns
the whole 15 s video at 1080p. Prototyped on Black Widow Spare+: the 8 s
join is invisible (a smaller frame-to-frame change than the clip's own
motion). Two things learned:
  * The base clip's old "slow forward drift" kept pushing in through the
    extension -- by 15 s the ball was twice the size, off the bottom of the
    frame under the price, and the callout lines (aimed at the still's ball
    position) pointed at nothing. Asking the extension to pull back instead
    turned the ball away and lost the logo. So the camera is now LOCKED OFF
    for both: the scene moves, the framing never does.
  * Extension is best-effort: if it fails, the 8 s clip is used as before
    (the renderer loops it), so a Veo hiccup never costs Al the animation.

Stills are OUTPAINTED from a source image (runbook 6co). Al, on the first
real batch: "The image is blurred letter boxing a 1:1 ration image to the
9:16 aspect ration." Asked for 9:16 with only the square product photo as
reference, gemini-3-pro-image kept returning the square scene padded with
blurred top/bottom bands -- even with the reference pre-placed on a 9:16
grey canvas and an explicit "fill the frame" instruction (both test
candidates still letterboxed). What reliably works: put a real SCENE (the
article's action shot) on the 9:16 canvas, enlarged and anchored near the
bottom, and ask Gemini to extend it upward -- it continues the environment
instead of padding it. So every still starts from a source image, which is
also Al's other ask: "Can we select an existing image action or product
shot as input to the Signage Video still as part of the workflow?"
  source="action_shot"   the article's approved action shot (default)
  source="product_shot"  the article's approved product shot
  source="new"           a fresh themed action shot first (the generator's
                         own 16:9 scene prompt), then outpainted
Each candidate is checked with letterbox_score() and retried once if it
still came back banded.

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
STILL_SOURCES = ("action_shot", "product_shot", "new")
CANVAS_W, CANVAS_H = 768, 1376  # gemini-3-pro-image's native 9:16 size
CANVAS_GREY = (128, 128, 128)
# Source placement on the canvas: tall enough (~42% of the height, at least
# full width) that the ball stays the hero, anchored 6% above the bottom so
# Gemini extends UP (calm title area) and adds only a sliver of floor. The
# first try (full width, centered) gave a visible seam where the floor met
# the original image edge; enlarged + bottom-anchored didn't.
SOURCE_HEIGHT_FRAC = 0.42
SOURCE_BOTTOM_MARGIN = 0.06
# letterbox_score(): on the stills we had (runbook 6co) letterboxed ones
# scored 7-24.5, genuinely vertical ones 0.5-3.3 (5.5 for the worst
# outpaint from the abandoned centered placement).
LETTERBOX_THRESHOLD = 6.5
STILL_ATTEMPTS = 2  # per candidate
VEO_MODEL_ID = "veo-3.1-fast-generate-001"
VEO_REGION = "us-central1"  # Veo is regional; Gemini images use "global"
VEO_DURATION_S = 8
VEO_POLL_S = 10
VEO_TIMEOUT_S = 420
VEO_EXTEND_TIMEOUT_S = 360  # base + extension must fit the generator's 900 s Lambda timeout
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


OUTPAINT_PROMPT = (
    "Extend this exact image into a complete 9:16 vertical composition. Keep the existing scene, art style, lighting, "
    "and the bowling ball exactly as they are (same colors, pattern, and printed logo, same proportions); continue the "
    "environment naturally upward and downward so the whole frame is one continuous scene."
    "\n\nTHE REFERENCE IMAGE IS ALREADY THE FINAL 9:16 VERTICAL CANVAS. The flat grey areas are EMPTY canvas, not part "
    "of the scene: paint the scene so it fills the ENTIRE frame edge to edge, top to bottom, as one continuous image. "
    "Never leave flat, blurred, mirrored, or repeated bands at the top or bottom; never letterbox."
)


def build_outpaint_prompt() -> str:
    return OUTPAINT_PROMPT + SIGNAGE_COMPOSITION


def place_on_canvas(image_bytes: bytes) -> str:
    """Source image -> base64 PNG of the 9:16 grey canvas with the image
    enlarged (SOURCE_HEIGHT_FRAC of the height, never narrower than the
    canvas; overflow is cropped evenly left/right) and bottom-anchored."""
    import io
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    h = int(CANVAS_H * SOURCE_HEIGHT_FRAC)
    w = int(img.width * h / img.height)
    if w < CANVAS_W:
        w, h = CANVAS_W, int(img.height * CANVAS_W / img.width)
    img = img.resize((w, h), Image.LANCZOS)
    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), CANVAS_GREY)
    top = max(0, CANVAS_H - int(CANVAS_H * SOURCE_BOTTOM_MARGIN) - h)
    canvas.paste(img, ((CANVAS_W - w) // 2, top))
    buf = io.BytesIO()
    canvas.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def letterbox_score(png: bytes) -> float:
    """How strongly the image has a horizontal seam in the band where a
    square-in-9:16 letterbox edge falls (12-32% down). Per row: the median
    absolute vertical brightness change across the width; score = the
    band's peak / the image's typical row. A real scene has no row where
    the WHOLE width changes at once; a blurred-bar edge does. Pure PIL (no
    numpy in this Lambda) on a 192x344 thumbnail."""
    import io
    from PIL import Image

    w, h = 192, 344
    px = Image.open(io.BytesIO(png)).convert("L").resize((w, h)).tobytes()  # one byte per pixel
    rows = []
    for y in range(h - 1):
        a, b = px[y * w:(y + 1) * w], px[(y + 1) * w:(y + 2) * w]
        d = sorted(abs(p - q) for p, q in zip(a, b))
        rows.append(d[w // 2])
    base = max(sorted(rows)[len(rows) // 2], 0.5)
    return max(rows[int(h * 0.12):int(h * 0.32)]) / base


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


CAMERA_LOCKED = (
    "The camera is LOCKED OFF on a tripod: no zoom, no dolly, no push-in, no pan -- the framing never changes and "
    "the ball keeps exactly the same size and position in the frame the whole time. "
)
VEO_EXTEND_NEGATIVE_EXTRA = ", zoom, dolly, camera movement, ball rotating away"


def build_veo_extend_prompt(visual_theme: str) -> str:
    theme = f" Theme: {visual_theme.strip()}" if visual_theme else ""
    return (
        "Continue this exact shot seamlessly. " + CAMERA_LOCKED + "The environment keeps its natural motion and any "
        "creature keeps moving, same lighting and art style. The bowling ball stays perfectly sharp with its printed "
        "logo facing the camera exactly as shown. No new objects, no text, no people." + theme
    )


def build_veo_prompt(visual_theme: str) -> str:
    theme = f" The scene's theme: {visual_theme.strip()}" if visual_theme else ""
    return (
        "Cinematic animation of this exact illustrated scene with strong, continuous natural motion throughout the "
        "whole frame: the environment moves naturally (clouds, water, light, air, the backdrop drift at different "
        "speeds for real depth), and any creature or figure in the scene comes alive and moves. " + CAMERA_LOCKED +
        "The bowling ball stays in the lower middle of the frame and remains perfectly "
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
             model_id: str = VEO_MODEL_ID, region: str = VEO_REGION, *, extend_mp4: bytes = None) -> bytes:
    """Veo 3.1 on Vertex AI: predictLongRunning, then poll
    fetchPredictOperation until done. Video comes back inline (no
    storageUri). Raises on errors, safety filtering, or timeout.

    Image-to-video by default (8 s). With extend_mp4, a video EXTENSION
    instead: the clip goes in as bytes (the docs only show gcsUri; bytes
    work), no durationSeconds (extensions are a fixed 7 s), and the
    response is the WHOLE extended video, not just the new part."""
    base = (f"https://{region}-aiplatform.googleapis.com/v1/projects/{project_id}/locations/{region}"
            f"/publishers/google/models/{model_id}")
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    params = {"aspectRatio": "9:16", "resolution": "1080p", "generateAudio": False, "sampleCount": 1,
              "negativePrompt": VEO_NEGATIVE_PROMPT}
    if extend_mp4 is not None:
        instance = {"prompt": prompt, "video": {"bytesBase64Encoded": base64.b64encode(extend_mp4).decode(),
                                                "mimeType": "video/mp4"}}
        params["negativePrompt"] += VEO_EXTEND_NEGATIVE_EXTRA
        timeout_s = VEO_EXTEND_TIMEOUT_S
    else:
        instance = {"prompt": prompt, "image": {"bytesBase64Encoded": base64.b64encode(image_png).decode(),
                                                "mimeType": "image/png"}}
        params["durationSeconds"] = VEO_DURATION_S
        timeout_s = VEO_TIMEOUT_S
    r = session.post(f"{base}:predictLongRunning", headers=headers,
                     json={"instances": [instance], "parameters": params}, timeout=120)
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
        if time.time() - started > timeout_s:
            raise TimeoutError(f"Veo didn't finish within {timeout_s}s")
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
                   s.selected_still_key, s.selected_still_url,
                   pa.action_shot_image_url, pa.product_shot_image_url
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
    keys = ("article_id", "product_id", "visual_theme", "name", "brand", "selected_still_key", "selected_still_url",
            "action_shot_url", "product_shot_url")
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

def generate_stills(conn, ctx: dict, *, source_png: bytes, gemini_call, s3_client,
                    image_bucket: str, bedrock_client, model_id: str) -> dict:
    """signage="stills". source_png is the picked source image (see the
    module docstring); gemini_call(prompt, reference_b64, aspect_ratio) ->
    png bytes (the generator's call_gemini_for_image, bound to its auth).
    A candidate that errors or still comes back letterboxed is retried; if every try
    is banded the least-banded one is kept rather than dropped (better
    something to look at than nothing -- Al picks)."""
    prompt = build_outpaint_prompt()
    reference_b64 = place_on_canvas(source_png)
    run = new_run_id()
    stills = []
    for i in range(STILL_CANDIDATES):
        best = None  # (score, png)
        for attempt in range(STILL_ATTEMPTS):
            try:
                png = gemini_call(prompt, reference_b64, STILL_ASPECT_RATIO)
            except Exception:  # retried like a banded one; never sinks the batch
                # (seen live: a spurious IMAGE_PROHIBITED_CONTENT on a
                # plain product shot that the next call rendered fine)
                logger.exception("signage still %d attempt %d failed for article %s", i + 1, attempt + 1,
                                 ctx["article_id"])
                continue
            score = letterbox_score(png)
            if best is None or score < best[0]:
                best = (score, png)
            if score < LETTERBOX_THRESHOLD:
                break
            logger.warning("signage still %d attempt %d letterboxed (score %.1f) for article %s",
                           i + 1, attempt + 1, score, ctx["article_id"])
        if best is None:
            continue
        png = best[1]
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
            bedrock_client, model_id: str, invoke_renderer, veo_extend_call=None) -> dict:
    """signage="animate". veo_call(png, prompt) -> 8 s mp4 bytes;
    veo_extend_call(mp4, prompt) -> the 15 s extended mp4 (best-effort, see
    the module docstring). Leaves status 'animating'; the renderer's loop
    step flips it to 'ready'."""
    clip = veo_call(still_png, build_veo_prompt(ctx["visual_theme"]))
    if veo_extend_call is not None:
        try:
            clip = veo_extend_call(clip, build_veo_extend_prompt(ctx["visual_theme"]))
        except Exception:  # noqa: BLE001 -- fall back to the 8 s clip (renderer loops it)
            logger.exception("Veo extension failed for article %s -- using the 8 s clip", ctx["article_id"])
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
