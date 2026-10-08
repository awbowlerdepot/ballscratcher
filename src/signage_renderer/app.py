"""
Signage renderer (DEPLOY_RUNBOOK.md 6cn) -- a container-image Lambda with
Chromium (Playwright) and ffmpeg, the two tools the in-store signage needs
that the zip-packaged functions can't carry.

Two jobs, both invoked asynchronously:

  {"mode": "loop", "article_id"}    from the article generator after Veo
      Veo's raw 8 s clip -> a seamless 7 s loop: crossfade the last second
      into the first (xfade(A[1:8], A[0:1], offset=6, dur=1)). Veo is NOT
      given a matching last frame -- that made it freeze the scene and paint
      sparks on top (runbook 6cm). A new clip un-approves the signage, so
      nothing new reaches the stores unreviewed.

  {"mode": "render", "article_id"}  from admin ("Render MP4")
      The finished MP4 for piSignage (Al: the Pis "100% can do the videos";
      local video plays hardware-decoded and offline). Chromium renders ONLY
      the data layer -- /signage/ball/<slug>?overlay=1, transparent, no
      <video> -- frame by frame (every CSS animation paused at t), and
      ffmpeg composites it over the looped clip. Headless Chromium never has
      to decode H.264. Output: 14 s (two background loops, the overlay
      cycle), 1080x1920, H.264 + faststart, with a fade in from black and a
      fade out to black (runbook 6cq).

Why the MP4 fades (runbook 6cq), Al: "can we add some version of a fade
out to the end of the signage videos ... these are playlist in pisignage
with many of these videos added." The 14 s cut was built as a seamless
loop, but the stores run piSignage playlists of many balls back to back.
piSignage's own transitions ("Animation: SVG animation options for
image/html transition", user guide) don't apply to videos: a video plays to
its end, the player shows its black background while it loads the next
file, and that one starts. A hard cut at full brightness into black looked
like a glitch; fading to and from black blends with that gap, so the
playlist reads as one ball dissolving into the next.

The drilled price is whatever the live page draws at render time (listing
price + $50); it's also recorded as mp4_price so admin can flag a rendered
MP4 whose price has since changed.
"""
import json
import logging
import os
import shutil
import subprocess
import urllib.parse
import uuid

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FPS = 24
OVERLAY_LOOP_S = 14  # must match HERO_LOOP_S in bowlerdepot-learn SignageBallPage.tsx
DRILLING_UPCHARGE = 50  # must match DRILLING_UPCHARGE there
WIDTH, HEIGHT = 1080, 1920
FADE_IN_S = 0.5   # short, the overlay text animates in on its own anyway
FADE_OUT_S = 1.0  # ends on true black, matching piSignage's gap between videos
TMP = "/tmp/signage"

SEEK_JS = """(t) => {
  document.getAnimations().forEach(a => { a.pause(); a.currentTime = t * 1000; });
  return new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
}"""


def loop_filter() -> str:
    """ffmpeg filtergraph for the seamless loop (8 s in -> 7 s out)."""
    return ("[0:v]split[a][b];[a]trim=start=1:end=8,setpts=PTS-STARTPTS[main];"
            "[b]trim=start=0:end=1,setpts=PTS-STARTPTS[head];"
            "[main][head]xfade=transition=fade:duration=1:offset=6,format=yuv420p[v]")


def composite_args(clip_path: str, frames_glob: str, out_path: str) -> list:
    return [
        "ffmpeg", "-v", "error", "-y",
        "-stream_loop", "-1", "-i", clip_path,
        "-framerate", str(FPS), "-i", frames_glob,
        "-filter_complex",
        f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},setsar=1,fps={FPS}[bg];"
        f"[bg][1:v]overlay=0:0,fade=t=in:st=0:d={FADE_IN_S},"
        f"fade=t=out:st={OVERLAY_LOOP_S - FADE_OUT_S}:d={FADE_OUT_S},format=yuv420p[v]",
        "-map", "[v]", "-t", str(OVERLAY_LOOP_S),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high", "-movflags", "+faststart",
        out_path,
    ]


def overlay_url(learn_site: str, slug: str, row: dict) -> str:
    q = {"overlay": "1", "bx": row["ball_x"], "by": row["ball_y"], "br": row["ball_r"]}
    if row.get("tagline"):
        q["cta"] = row["tagline"]
    return f"{learn_site.rstrip('/')}/signage/ball/{urllib.parse.quote(slug)}?{urllib.parse.urlencode(q)}"


# --------------------------------------------------------------------------

def get_db_connection():
    import boto3
    import psycopg2

    s = json.loads(boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["DB_SECRET_ARN"])["SecretString"])
    return psycopg2.connect(host=s["host"], port=s.get("port", 5432), dbname=s["dbname"], user=s["username"],
                            password=s["password"], connect_timeout=10)


def load_row(conn, article_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            select s.article_id, pa.product_id, pa.slug, s.raw_clip_key, s.clip_key, s.clip_url,
                   s.ball_x, s.ball_y, s.ball_r, s.tagline,
                   (select h.price from product_price_sources pps
                      join price_sites ps on ps.id = pps.price_site_id
                      join product_price_history h on h.price_source_id = pps.id
                     where pps.product_id = pa.product_id and ps.api_provider = 'bigcommerce'
                       and pps.status = 'approved' and pps.is_active = true and h.price is not null
                     order by h.checked_at desc limit 1) as listing_price
            from article_signage s join product_articles pa on pa.id = s.article_id
            where s.article_id = %s
            """,
            (article_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"No signage row for article {article_id}")
        return dict(zip([d[0] for d in cur.description], row))


def update(conn, article_id: str, **fields) -> None:
    sets = ", ".join(f"{k} = %s" for k in fields)
    with conn.cursor() as cur:
        cur.execute(f"update article_signage set {sets}, updated_at = now() where article_id = %s",
                    [*fields.values(), article_id])
    conn.commit()


def make_loop(conn, s3, bucket: str, row: dict) -> dict:
    raw = os.path.join(TMP, "raw.mp4")
    out = os.path.join(TMP, "loop.mp4")
    s3.download_file(bucket, row["raw_clip_key"], raw)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", raw, "-filter_complex", loop_filter(), "-map", "[v]",
                    "-c:v", "libx264", "-crf", "18", "-movflags", "+faststart", out], check=True)
    key = f"article-images/{row['product_id']}/signage/clip_loop_{uuid.uuid4().hex[:8]}.mp4"
    s3.upload_file(out, bucket, key, ExtraArgs={"ContentType": "video/mp4"})
    url = f"https://{bucket}.s3.amazonaws.com/{key}"
    update(conn, row["article_id"], clip_key=key, clip_url=url, clip_generated_at="now", status="ready",
           job_started_at=None, error=None, approved=False, approved_at=None, approved_by=None)
    return {"clip_url": url}


def render_mp4(conn, s3, bucket: str, row: dict, learn_site: str) -> dict:
    from playwright.sync_api import sync_playwright

    if not row["clip_key"]:
        raise RuntimeError("No animated clip yet -- Animate first")
    frames = os.path.join(TMP, "frames")
    os.makedirs(frames, exist_ok=True)
    clip = os.path.join(TMP, "clip.mp4")
    s3.download_file(bucket, row["clip_key"], clip)

    url = overlay_url(learn_site, row["slug"], row)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                                          "--single-process", "--no-zygote"])
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1)
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_selector(".hx-name", timeout=30000)
        page.evaluate("document.fonts.ready")
        n = int(OVERLAY_LOOP_S * FPS)
        for i in range(n):
            page.evaluate(SEEK_JS, i / FPS)
            page.screenshot(path=os.path.join(frames, f"f_{i:05d}.png"), omit_background=True)
        browser.close()

    out = os.path.join(TMP, "signage.mp4")
    subprocess.run(composite_args(clip, os.path.join(frames, "f_%05d.png"), out), check=True)
    key = f"article-images/{row['product_id']}/signage/signage_{uuid.uuid4().hex[:8]}.mp4"
    s3.upload_file(out, bucket, key, ExtraArgs={"ContentType": "video/mp4"})
    mp4_url = f"https://{bucket}.s3.amazonaws.com/{key}"
    price = float(row["listing_price"]) + DRILLING_UPCHARGE if row["listing_price"] is not None else None
    update(conn, row["article_id"], mp4_key=key, mp4_url=mp4_url, mp4_rendered_at="now", mp4_price=price,
           status="ready", job_started_at=None, error=None)
    return {"mp4_url": mp4_url}


def handler(event, context):
    import boto3

    mode, article_id = event.get("mode"), event.get("article_id")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    conn = get_db_connection()
    try:
        row = load_row(conn, article_id)
        s3 = boto3.client("s3")
        bucket = os.environ["IMAGE_BUCKET"]
        if mode == "loop":
            return make_loop(conn, s3, bucket, row)
        if mode == "render":
            return render_mp4(conn, s3, bucket, row, os.environ.get("LEARN_SITE_URL", "https://learn.bowlerdepot.com"))
        raise ValueError(f"Unknown mode {mode!r}")
    except Exception as e:
        logger.exception("signage renderer %s failed for article %s", mode, article_id)
        conn.rollback()
        try:
            update(conn, article_id, status="failed", job_started_at=None, error=str(e)[:500])
        except Exception:
            logger.exception("couldn't record the failure")
        return {"failed": True, "error": str(e)[:500]}
    finally:
        conn.close()
        shutil.rmtree(TMP, ignore_errors=True)
