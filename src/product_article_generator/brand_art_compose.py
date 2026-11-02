"""
Brand-art compositing: real product photos on a brand's colors (runbook 6fq).

Used by brand_art.py for line images, lineup heroes and non-ball product
heroes. A single-ball product hero is drawn by the existing Gemini
product-shot pipeline instead (Al: "BowlerIQ already generates ball images
for social posts and articles. Build the product hero on that existing
pipeline"), and falls back to product_hero() here when every
Gemini attempt fails its logo checks.

Al (2026-11-02, via the Bowler Depot brand-stores session): BowlerIQ owns
each brand's lineups, and Bowler Depot's brand-store art is a standing
task BowlerIQ reruns on its own. The rules for the pictures, from the
request: "Build each image from the balls' real product photos
(image_url) on the brand's colors. Don't invent ball designs, logos or
brand text. No text baked into the image; headlines stay in HTML." And
for heroes: "bags, shoes, gloves and wrist supports, apparel and
accessories, alongside a ball" when Bowler Depot stocks them -- "a new
Storm ball with a Storm roller bag".

So this module only ever moves real pixels around. Nothing here asks an
image model for anything (unlike signage.py, whose stills are outpainted
by Gemini): the background is drawn from the palette (a flat fill plus a
soft accent glow), and every product is a cut-out of its own photo with a
soft contact shadow. Pure Pillow, so it runs in any of our Python Lambdas.

CUT-OUTS. Two kinds of source photo:
  * already transparent -- every Storm/Roto Grip/900 Global ball
    (commercebuild PNGs, ~48% fully transparent pixels) and many other
    brands' PNGs. Used as they are.
  * flattened onto white -- BigCommerce product photos (500x500 JPEGs)
    and our own stored_url copies (image_processor flattens on purpose,
    see its docstring). key_out_background() flood-fills the near-white
    region connected to the border, so white INSIDE a product (a bag's
    white panel, a logo) stays, as long as it isn't connected to the
    outside by a white path. The edge gets a 1px feather so it doesn't
    look stamped out on a dark background.
"""

from __future__ import annotations

import io
from collections import deque

from PIL import Image, ImageChops, ImageDraw, ImageFilter

# How close to the corner color a pixel has to be (max channel distance)
# to count as background. 24 keeps JPEG noise around a white backdrop out
# and a light-grey product edge in; tuned on Storm's BigCommerce bags.
KEY_TOLERANCE = 24


def hex_rgb(value: str) -> tuple:
    v = value.lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def has_real_alpha(im: Image.Image) -> bool:
    """True when the photo is already a cut-out: at least 5% of its pixels
    are (nearly) fully transparent. A PNG saved with an alpha channel but
    a solid background has none."""
    if im.mode not in ("RGBA", "LA", "P"):
        return False
    alpha = im.convert("RGBA").getchannel("A")
    hist = alpha.histogram()
    return sum(hist[:16]) >= 0.05 * im.width * im.height


def key_out_background(im: Image.Image, tolerance: int = KEY_TOLERANCE) -> Image.Image:
    """RGBA cut-out of a product photographed on a flat, light background.

    Flood fill from every border pixel through pixels within `tolerance` of
    the corner color; those become transparent. Interior pixels of the same
    color that the fill can't reach are kept (they're part of the
    product)."""
    rgb = im.convert("RGB")
    w, h = rgb.size
    px = rgb.load()
    bg = px[0, 0]

    def is_bg(x, y):
        r, g, b = px[x, y]
        return max(abs(r - bg[0]), abs(g - bg[1]), abs(b - bg[2])) <= tolerance

    mask = Image.new("L", (w, h), 255)
    m = mask.load()
    seen = bytearray(w * h)
    queue = deque()
    for x in range(w):
        queue.append((x, 0))
        queue.append((x, h - 1))
    for y in range(h):
        queue.append((0, y))
        queue.append((w - 1, y))
    while queue:
        x, y = queue.popleft()
        i = y * w + x
        if seen[i]:
            continue
        seen[i] = 1
        if not is_bg(x, y):
            continue
        m[x, y] = 0
        if x > 0:
            queue.append((x - 1, y))
        if x < w - 1:
            queue.append((x + 1, y))
        if y > 0:
            queue.append((x, y - 1))
        if y < h - 1:
            queue.append((x, y + 1))
    # Feather: shrink the kept area by a pixel and soften it, so the white
    # fringe JPEG leaves around the product doesn't glow on a dark canvas.
    mask = mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.8))
    out = rgb.convert("RGBA")
    out.putalpha(mask)
    return out


def cutout(image_bytes: bytes) -> Image.Image:
    """RGBA cut-out of one product photo, trimmed to its visible pixels."""
    im = Image.open(io.BytesIO(image_bytes))
    im.load()
    cut = im.convert("RGBA") if has_real_alpha(im) else key_out_background(im)
    box = cut.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    return cut.crop(box) if box else cut


def background(size: tuple, palette: dict, glow_at: tuple = (0.5, 0.55)) -> Image.Image:
    """The brand's background color with a soft glow of its accent color
    behind the products. Drawn, not generated: no texture, no marks."""
    w, h = size
    canvas = Image.new("RGB", size, hex_rgb(palette["background"]))
    glow = Image.new("L", size, 0)
    gx, gy = int(glow_at[0] * w), int(glow_at[1] * h)
    r = int(0.42 * max(w, h))
    ImageDraw.Draw(glow).ellipse((gx - r, gy - r // 1.4, gx + r, gy + r // 1.4), fill=95)
    glow = glow.filter(ImageFilter.GaussianBlur(r // 3))
    accent = Image.new("RGB", size, hex_rgb(palette["accent"]))
    return Image.composite(accent, canvas, glow)


def _shadow(canvas: Image.Image, box: tuple, strength: int = 150) -> None:
    """A soft contact shadow under a product whose bottom edge is box[3]."""
    x0, y0, x1, y1 = box
    w = x1 - x0
    layer = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(layer).ellipse((x0 + w * 0.1, y1 - w * 0.06, x1 - w * 0.1, y1 + w * 0.06), fill=strength)
    layer = layer.filter(ImageFilter.GaussianBlur(max(4, w // 18)))
    black = Image.new("RGB", canvas.size, (0, 0, 0))
    canvas.paste(Image.composite(black, canvas, layer))


def place(canvas: Image.Image, item: Image.Image, center_x: float, bottom: float, height: float) -> tuple:
    """Paste `item` scaled to `height` px, centered on center_x, standing on
    `bottom` (all canvas px). Returns the pasted box."""
    scale = height / item.height
    resized = item.resize((max(1, int(item.width * scale)), max(1, int(height))), Image.LANCZOS)
    x0 = int(center_x - resized.width / 2)
    y0 = int(bottom - resized.height)
    box = (x0, y0, x0 + resized.width, y0 + resized.height)
    _shadow(canvas, box)
    canvas.paste(resized, (x0, y0), resized)
    return box


def product_hero(size: tuple, palette: dict, focus: Image.Image, beside: Image.Image | None = None) -> Image.Image:
    """Al's "product hero": one product in focus. Either a single ball, or
    a non-ball product (a bag, shoes) with at most one ball beside it. The
    product sits right of center, leaving the left for the HTML headline
    the theme lays over hero slides."""
    w, h = size
    wide = w / h > 1.5
    canvas = background(size, palette, glow_at=(0.64, 0.55))
    floor = h * 0.9
    if beside is None:
        place(canvas, focus, w * 0.64, floor, h * (0.74 if wide else 0.66))
    else:
        place(canvas, focus, w * 0.66, floor, h * (0.8 if wide else 0.74))
        place(canvas, beside, w * (0.44 if wide else 0.42), floor + h * 0.01, h * (0.46 if wide else 0.42))
    return canvas


def _row(canvas: Image.Image, items: list, left: float, right: float, floor: float, item_h: float) -> None:
    """Items in a slightly overlapping row with the FIRST (newest) one
    front and center: the most central positions go to the earliest items,
    and the row is drawn back to front. Spacing keeps every ball's middle
    (where the logo is) clear of its neighbours."""
    n = len(items)
    xs = [(left + right) / 2] if n == 1 else [left + i * (right - left) / (n - 1) for i in range(n)]
    central = sorted(range(n), key=lambda i: (abs(i - (n - 1) / 2), i))
    slot_of = {k: central[k] for k in range(n)}          # item k -> position
    for k in reversed(range(n)):                          # back (last) to front (first)
        front = k == 0
        lift = 0 if front else canvas.height * 0.03
        place(canvas, items[k], xs[slot_of[k]], floor - lift, item_h * (1.0 if front else 0.9))


def lineup_hero(size: tuple, palette: dict, items: list) -> Image.Image:
    """Al's "lineup hero": several products together, shifted right for the
    headline."""
    w, h = size
    canvas = background(size, palette, glow_at=(0.64, 0.55))
    items = items[:4]
    n = len(items)
    if n:
        item_h = h * (0.6 if n == 1 else 0.5 if n == 2 else 0.42 if n == 3 else 0.36)
        _row(canvas, items, w * 0.44, w * 0.86, h * 0.88, item_h)
    return canvas


def line(size: tuple, palette: dict, balls: list) -> Image.Image:
    """Up to four balls of one line, newest front and center. Ball-only,
    as the request says for lines."""
    w, h = size
    balls = balls[:4]
    canvas = background(size, palette)
    n = len(balls)
    if n:
        ball_h = h * (0.66 if n == 1 else 0.54 if n == 2 else 0.46 if n == 3 else 0.4)
        half = {1: 0.0, 2: 0.2, 3: 0.3, 4: 0.36}[n] * w
        _row(canvas, balls, w / 2 - half, w / 2 + half, h * 0.84, ball_h)
    return canvas


def encode_webp(im: Image.Image, max_bytes: int = 250_000) -> bytes:
    """WebP under max_bytes ("under ~250 KB"), stepping quality down."""
    for q in (86, 80, 74, 68, 60, 52):
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=q, method=6)
        if buf.tell() <= max_bytes:
            return buf.getvalue()
    return buf.getvalue()
