#!/usr/bin/env python3
"""Make the Learn site's social-share images (runbook 6dt).

Al: "can we add some better seo and social tags and images for when things
are shared from learn site. Main image and plotter image. articles seem to
be good." Articles already get a 1200x630 og:image from their hero shot
(scripts/prerender.ts computeHeroImage). The home page and /plotter had no
image at all, so a shared link unfurled as a bare title.

This builds two 1200x630 cards (the Open Graph / X large-card size) as
plain HTML pages, then screenshots each with headless Chrome so they use
the site's real fonts (Space Grotesk / Cabin), brand colors, real ball
photos and the real chart positions:

- bowlerdepot-learn/public/og/learn-home.jpg -- navy brand panel + the
  product shots from the six most recent ball reviews.
- bowlerdepot-learn/public/og/plotter.jpg -- a mini ball motion chart:
  every current ball's photo at its whole-number spot (one per spot, like
  the plotter's default zoom), same axes as the real chart.

The images are committed (public/ is copied into dist/ by Vite) rather
than made on every deploy. A share card doesn't need to track each new
ball, and this keeps Chrome out of the CI build. Re-run it whenever the
cards look stale, then commit and push:

    python3 scripts/make_learn_og_images.py

Needs Google Chrome installed (macOS path below) and network access to
api.bowleriq.io / img.bowleriq.io / Google Fonts. Stdlib only.
"""

import json
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

API = "https://api.bowleriq.io"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LOGO = (
    "https://cdn11.bigcommerce.com/s-83dch55a9c/images/stencil/original/"
    "bowlerdepot_logo_website_logo_website_logo_1566417769__86141.original.png"
)
OUT_DIR = Path(__file__).resolve().parent.parent / "bowlerdepot-learn" / "public" / "og"
W, H = 1200, 630

# Same palette/fonts as bowlerdepot-learn/tailwind.config.js.
BASE_CSS = """
@import url("https://fonts.googleapis.com/css2?family=Cabin:wght@400;600;700&family=Space+Grotesk:wght@500;600;700&display=swap");
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { width: %dpx; height: %dpx; overflow: hidden; }
body { background: #faf8f4; color: #0f0f2d; font-family: Cabin, Arial, sans-serif; }
.display { font-family: "Space Grotesk", Arial, sans-serif; }
""" % (W, H)


def get_json(path: str) -> dict:
    # curl rather than urllib: python.org builds on macOS ship without CA
    # certs until "Install Certificates.command" is run.
    out = subprocess.run(["curl", "-sf", "--max-time", "30", f"{API}{path}"], check=True, capture_output=True)
    return json.loads(out.stdout)


def resized(raw: str, w: int, h: int | None = None, fit: str = "cover") -> str:
    """Same rewrite as prerender.ts resizedImageUrl: ImageBucket S3 URL -> img.bowleriq.io."""
    key = urllib.parse.urlparse(raw).path.lstrip("/")
    if not key.startswith(("product-images/", "article-images/")):
        return raw
    q = {"w": w, "fit": fit, "fmt": "png"}
    if h:
        q["h"] = h
    return f"https://img.bowleriq.io/{key}?{urllib.parse.urlencode(q)}"


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def home_html() -> str:
    items = get_json("/articles?limit=40")["items"]
    shots = [a["product_shot_image_url"] for a in items if a.get("product_shot_image_url") and a.get("kind") != "video"][:6]
    tiles = "".join(
        f'<div class="tile"><img src="{esc(resized(u, 360, 360))}" alt=""></div>' for u in shots
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE_CSS}
.wrap {{ display: flex; height: 100%; }}
.panel {{ width: 540px; background: #0f0f2d; color: #fff; padding: 56px 52px; display: flex; flex-direction: column; }}
.logo {{ height: 62px; width: auto; filter: brightness(0) invert(1); align-self: flex-start; }}
.tag {{ margin-top: 26px; display: inline-block; align-self: flex-start; border: 2px solid #8fa8ff; color: #c9d5ff;
        font-weight: 700; letter-spacing: .22em; font-size: 18px; padding: 6px 14px; border-radius: 6px; }}
h1 {{ margin-top: 34px; font-size: 50px; line-height: 1.06; font-weight: 700; letter-spacing: -.01em; }}
p.sub {{ margin-top: 20px; font-size: 22px; line-height: 1.35; color: #c9cbe0; }}
.url {{ margin-top: auto; font-size: 22px; font-weight: 700; color: #fff; }}
.url span {{ color: #8fa8ff; }}
.grid {{ flex: 1; display: grid; grid-template-columns: repeat(3, 1fr); grid-template-rows: repeat(2, 1fr);
         gap: 16px; padding: 36px; }}
.tile {{ background: #fff; border: 1px solid #e8e2d6; border-radius: 14px; overflow: hidden; display: flex; }}
.tile img {{ width: 100%; height: 100%; object-fit: cover; }}
</style></head><body><div class="wrap">
  <div class="panel">
    <img class="logo" src="{LOGO}" alt="">
    <div class="tag">LEARN</div>
    <h1 class="display">Bowling ball reviews, guides &amp; the ball motion plotter</h1>
    <p class="sub">In-depth reviews of the newest releases, plus guides to help you pick your next ball.</p>
    <div class="url">learn.<span>bowlerdepot.com</span></div>
  </div>
  <div class="grid">{tiles}</div>
</div></body></html>"""


def plotter_html() -> str:
    items = get_json("/learn/plotter")["items"]
    brands = sorted({b["brand_name"] for b in items})
    # One ball per whole-number spot (the plotter's default-zoom stacking),
    # preferring chart positions over estimates for the photo shown.
    spots: dict[tuple[int, int], dict] = {}
    for b in sorted(items, key=lambda b: b["oil_motion_source"] != "chart"):
        if not b.get("primary_image_url"):
            continue
        spots.setdefault((round(b["oil"]), round(b["motion"])), b)

    # Chart geometry (CSS px inside the 1200x630 card).
    cx0, cy0, cw, ch = 470, 36, 694, 540  # panel box
    ml, mr, mt, mb = 34, 14, 14, 34  # margins inside the panel for axis labels
    pw, ph = cw - ml - mr, ch - mt - mb
    x0, x1, y0, y1 = 0.25, 16.75, 0.25, 18.75  # plotterModel DOMAIN (oil 1-16, motion 1-18, 0.75 pad)

    def px(oil: float) -> float:
        return ml + (oil - x0) / (x1 - x0) * pw

    def py(motion: float) -> float:
        return mt + (y1 - motion) / (y1 - y0) * ph

    lines = "".join(
        f'<line x1="{px(o):.1f}" y1="{mt}" x2="{px(o):.1f}" y2="{mt + ph}" />' for o in range(1, 17)
    ) + "".join(f'<line x1="{ml}" y1="{py(m):.1f}" x2="{ml + pw}" y2="{py(m):.1f}" />' for m in range(1, 19))
    r = 17
    balls = "".join(
        f'<image href="{esc(resized(b["primary_image_url"], 64, 64, "contain"))}" x="{px(o) - r:.1f}" '
        f'y="{py(m) - r:.1f}" width="{2 * r}" height="{2 * r}" clip-path="url(#c)" />'
        f'<circle cx="{px(o):.1f}" cy="{py(m):.1f}" r="{r}" fill="none" stroke="#fff" stroke-width="1.5" />'
        for (o, m), b in spots.items()
    )
    chart = f"""<svg width="{cw}" height="{ch}" viewBox="0 0 {cw} {ch}" xmlns="http://www.w3.org/2000/svg">
  <defs><clipPath id="c" clipPathUnits="objectBoundingBox"><circle cx=".5" cy=".5" r=".5"/></clipPath></defs>
  <rect x="0" y="0" width="{cw}" height="{ch}" rx="14" fill="#fff" stroke="#e8e2d6" />
  <g stroke="#efeae0" stroke-width="1">{lines}</g>
  <rect x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="none" stroke="#d9d3c7" />
  {balls}
  <text x="{ml}" y="{ch - 12}" font-size="15" font-weight="700" fill="#0f0f2d" font-family="Cabin">&#8592; Light oil</text>
  <text x="{ml + pw}" y="{ch - 12}" text-anchor="end" font-size="15" font-weight="700" fill="#0f0f2d" font-family="Cabin">Heavy oil &#8594;</text>
  <text x="18" y="{mt + ph / 2}" transform="rotate(-90 18 {mt + ph / 2})" text-anchor="middle" font-size="15"
        font-weight="700" fill="#0f0f2d" font-family="Cabin">Smooth &#8592; Motion &#8594; Angular</text>
</svg>"""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{BASE_CSS}
.left {{ position: absolute; left: 0; top: 0; width: 450px; height: 100%; padding: 52px 20px 48px 52px;
         display: flex; flex-direction: column; }}
.logo {{ height: 50px; width: auto; align-self: flex-start; }}
.tag {{ margin-top: 22px; align-self: flex-start; background: #1f439e; color: #fff; font-weight: 700;
        letter-spacing: .2em; font-size: 15px; padding: 6px 12px; border-radius: 6px; }}
h1 {{ margin-top: 22px; font-size: 56px; line-height: 1.02; font-weight: 700; letter-spacing: -.015em; }}
p.sub {{ margin-top: 18px; font-size: 21px; line-height: 1.38; color: #45453f; }}
.stats {{ margin-top: 22px; font-size: 18px; font-weight: 700; color: #1f439e; }}
.url {{ margin-top: auto; font-size: 20px; font-weight: 700; }}
.chart {{ position: absolute; left: {cx0}px; top: {cy0}px; }}
</style></head><body>
  <div class="left">
    <img class="logo" src="{LOGO}" alt="">
    <div class="tag">FREE TOOL</div>
    <h1 class="display">Ball Motion Plotter</h1>
    <p class="sub">Every current ball on one chart: oil handling vs. backend motion. Find similar balls across brands.</p>
    <div class="stats">{len(items)} balls &middot; {len(brands)} brands</div>
    <div class="url">learn.bowlerdepot.com/plotter</div>
  </div>
  <div class="chart">{chart}</div>
</body></html>"""


def shoot(html: str, out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "card.html"
        png = Path(tmp) / "card.png"
        page.write_text(html, encoding="utf-8")
        subprocess.run(
            [
                CHROME,
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                f"--window-size={W},{H}",
                "--virtual-time-budget=20000",  # let fonts + remote photos load
                f"--screenshot={png}",
                page.as_uri(),
            ],
            check=True,
            capture_output=True,
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "86", str(png), "--out", str(out)],
            check=True,
            capture_output=True,
        )
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


def main() -> int:
    shoot(home_html(), OUT_DIR / "learn-home.jpg")
    shoot(plotter_html(), OUT_DIR / "plotter.jpg")
    return 0


if __name__ == "__main__":
    sys.exit(main())
