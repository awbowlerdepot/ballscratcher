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
    see its docstring). These go through Bedrock's Remove Background
    model when the caller passes remove_bg (see cutout()); otherwise
    key_out_background() flood-fills the near-white
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


def cutout(image_bytes: bytes, remove_bg=None) -> Image.Image:
    """RGBA cut-out of one product photo, trimmed to its visible pixels.

    remove_bg(bytes) -> PNG bytes with alpha is the Bedrock (Stability)
    Remove Background model, used for any photo that isn't already a
    cut-out. Al, on the first bag hero: "it missed on removing the
    background by the handles" -- the flood fill only reaches white that
    touches the photo's edge, so the white seen THROUGH a handle stayed.
    The model takes those out and keeps a white bag's own panels. The
    flood fill is still the fallback when the call fails or returns no
    transparency."""
    im = Image.open(io.BytesIO(image_bytes))
    im.load()
    cut = None
    if has_real_alpha(im):
        cut = im.convert("RGBA")
    elif remove_bg is not None:
        try:
            matted = Image.open(io.BytesIO(remove_bg(image_bytes)))
            matted.load()
            if has_real_alpha(matted):
                cut = matted.convert("RGBA")
        except Exception:
            cut = None
    if cut is None:
        cut = key_out_background(im)
    box = cut.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    return cut.crop(box) if box else cut


def is_light(value: str) -> bool:
    """A pale brand background (Radical's #fff200), by relative luminance."""
    r, g, b = (c / 255 for c in hex_rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.6


# flatten_backdrop's bands, as the largest channel difference from the
# backdrop Gemini drew. Within FLAT_NEAR the pixel is backdrop and becomes
# the band color exactly; a LIGHTER pixel is backdrop out to FLAT_FAR (the
# pale middle of an uneven draw -- a shadow is never lighter). A darker one
# between the two is the contact shadow: shifted onto the band with its
# darkness kept. Past FLAT_FAR (the ball, its edge, its logo) it's left as
# drawn. FLAT_FAR stays well under Radical's orange logo, ~105-117 from
# the drawn yellow. FLAT_GUARD: only a backdrop already this close to the
# band is corrected (the warm draws were within 34); anything further off
# isn't a plain backdrop of the band color, and the check rejects it.
FLAT_NEAR, FLAT_FAR, FLAT_GUARD = 45, 100, 60


def _ramp(near: int, far: int):
    span = far - near
    return lambda d: 255 if d <= near else (0 if d >= far else 255 * (far - d) // span)


def flatten_backdrop(im: Image.Image, value: str, spots: list) -> Image.Image:
    """Gemini's draw with its backdrop set to exactly `value` (runbook 6gj).
    Asked for #fff200, it paints a warmer yellow -- (240-255, 208-228, 2-59)
    over five Radical draws, the "amber/golden" Al flagged -- often with a
    lighter middle, so the backdrop check rejected most draws and the slide
    band and the art didn't meet cleanly. `spots` are the (x, y) points,
    in a 64x48 thumbnail, that measure the drawn backdrop (the check's own
    points, away from the ball). Only for a pale band: there the ball is
    never close to the backdrop's color, which is what keeps it untouched."""
    im = im.convert("RGB")
    small = im.resize((64, 48))
    seen = [small.getpixel(p) for p in spots]
    drawn = tuple(sorted(c[i] for c in seen)[len(seen) // 2] for i in range(3))  # per-channel median
    want = hex_rgb(value)
    if max(abs(a - b) for a, b in zip(drawn, want)) > FLAT_GUARD:
        return im
    diff = ImageChops.difference(im, Image.new("RGB", im.size, drawn))
    dist = ImageChops.lighter(ImageChops.lighter(diff.getchannel(0), diff.getchannel(1)), diff.getchannel(2))
    drawn_lum = Image.new("RGB", (1, 1), drawn).convert("L").getpixel((0, 0))
    # Soft: how much lighter than the drawn backdrop (0 at 12 darker, 255 at level).
    lighter = im.convert("L").point(lambda v: max(0, min(255, (v - drawn_lum + 12) * 255 // 12)))
    shifted = Image.merge("RGB", [im.getchannel(i).point(lambda v, d=want[i] - drawn[i]: max(0, min(255, v + d)))
                                  for i in range(3)])
    # Every edge is a ramp, and the masks are softened by a couple of
    # pixels: hard cut-offs left a cut-out outline on the contact shadow and
    # a jagged rim where the ball reflects the yellow (first try, 6gj).
    soften = ImageFilter.GaussianBlur(2)
    flat_mask = ImageChops.lighter(dist.point(_ramp(15, FLAT_NEAR)),
                                   ImageChops.multiply(lighter, dist.point(_ramp(FLAT_FAR - 25, FLAT_FAR)))).filter(soften)
    keep_mask = ImageChops.lighter(flat_mask, dist.point(_ramp(FLAT_NEAR, FLAT_FAR))).filter(soften)
    out = Image.composite(Image.new("RGB", im.size, want), shifted, flat_mask)
    return Image.composite(out, im, keep_mask)


def background(size: tuple, palette: dict, glow_at: tuple = (0.5, 0.55)) -> Image.Image:
    """The brand's background color with a soft glow of its accent color
    behind the products. Drawn, not generated: no texture, no marks."""
    w, h = size
    canvas = Image.new("RGB", size, hex_rgb(palette["background"]))
    if is_light(palette["background"]):
        # Runbook 6gd: on a pale background the accent "glow" (Radical's
        # black) reads as a dark halo, so a light palette is a flat fill.
        return canvas
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


BALL_INCHES = 8.5  # a bowling ball's diameter (USBC max 8.595")


def _scaled(item: Image.Image, height: float) -> Image.Image:
    scale = height / item.height
    return item.resize((max(1, int(item.width * scale)), max(1, int(height))), Image.LANCZOS)


def product_hero(size: tuple, palette: dict, focus: Image.Image, beside: Image.Image | None = None,
                 focus_inches: float | None = None) -> Image.Image:
    """Al's "product hero": one product in focus. Either a single ball, or
    a non-ball product (a bag, shoes) with at most one ball beside it. The
    product sits right of center, leaving the left for the HTML headline
    the theme lays over hero slides.

    TRUE SCALE. Al, on the first bag hero: "size ratios are way off" -- the
    ball was drawn at a fixed share of the frame, so a 3-ball roller looked
    barely twice the ball's height. With focus_inches (the product's
    longest real dimension, as photographed -- brand_art.companion_inches)
    the ball beside it is drawn at 8.5" on the same pixels-per-inch: the
    product is fitted to the frame first, and the ball follows."""
    w, h = size
    wide = w / h > 1.5
    canvas = background(size, palette, glow_at=(0.64, 0.55))
    floor = h * 0.9
    if beside is None:
        place(canvas, focus, w * 0.64, floor, h * (0.74 if wide else 0.66))
        return canvas
    # Fit the product in its box (most of the height, about half the width).
    box_h, box_w = h * (0.82 if wide else 0.76), w * (0.42 if wide else 0.5)
    focus_h = min(box_h, box_w * focus.height / focus.width)
    if focus_inches:
        longest_px = focus_h * max(focus.width, focus.height) / focus.height
        ball_h = longest_px / focus_inches * BALL_INCHES
    else:
        ball_h = h * (0.46 if wide else 0.42)
    focus_w = focus_h * focus.width / focus.height
    cx = w * 0.68
    place(canvas, focus, cx, floor, focus_h)
    # The ball stands just in front of the product's lower left corner.
    bx = cx - focus_w / 2 - ball_h * 0.15
    place(canvas, beside, bx, floor + h * 0.01, ball_h)
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
    # Every ball is the same real size, so all are drawn the same size
    # (Al: "size ratios are way off"); only the overlap order says which
    # is in front.
    for k in reversed(range(n)):                          # back (last) to front (first)
        place(canvas, items[k], xs[slot_of[k]], floor, item_h)


def lineup_hero(size: tuple, palette: dict, items: list) -> Image.Image:
    """Al's "lineup hero": several products together, shifted right for the
    headline."""
    w, h = size
    canvas = background(size, palette, glow_at=(0.64, 0.55))
    items = items[:6]
    n = len(items)
    if n:
        # Up to six (one per reaction line for "Find your reaction", 6fu).
        # Wide frames have more room, so their balls can be bigger.
        wide = w / h > 1.5
        item_h = h * {1: 0.6, 2: 0.5, 3: 0.42, 4: 0.36, 5: 0.3, 6: 0.26}[n] * (1.12 if wide else 1.0)
        left = w * (0.4 if n <= 4 else 0.32)
        _row(canvas, items, left, w * 0.88, h * 0.86, item_h)
    return canvas


def line(size: tuple, palette: dict, balls: list) -> Image.Image:
    """Up to four balls of one line, newest front and center. Ball-only,
    as the request says for lines."""
    w, h = size
    balls = balls[:4]
    canvas = background(size, palette)
    n = len(balls)
    if n:
        # Equal-size balls need a margin at the frame's edge: with four,
        # the outer ones touched it (2026-11-02 render).
        ball_h = h * (0.62 if n == 1 else 0.5 if n == 2 else 0.42 if n == 3 else 0.35)
        half = {1: 0.0, 2: 0.2, 3: 0.29, 4: 0.32}[n] * w
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
