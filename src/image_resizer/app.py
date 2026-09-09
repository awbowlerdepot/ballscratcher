"""
On-demand image resize/optimize endpoint, ahead of the Learn site's release.
Al: "i think it is time to optimize the images... an endpoint that has the
image path and then query params to tell the optimizer what to do for the
particular placement... if the same image is asked for in the same size it
will pull it from a cache instead of doing the resizing/optimizing."

Sits behind this function's own Lambda Function URL, which
ImageResizerDistribution (template.yaml) fronts with CloudFront -- the
caching half of the ask is CloudFront's job, not this function's (see that
resource's own comments for the cache-key/query-string whitelist). This
function only ever runs on a cache miss.

URL shape: <resizer-domain>/<s3-key>?w=400&h=300&fit=cover&fmt=webp&q=80
  - <s3-key> is the object's key inside ImageBucket -- the same
    product-images/... or article-images/... keys already stored in
    products/product_images/product_articles' *_url columns, just without
    the bucket's own domain prefix. Scoped to those two prefixes on
    purpose (ALLOWED_PREFIXES below, matching the same prefix scoping
    IMAGE_BUCKET's IAM policies already use elsewhere in template.yaml) --
    this isn't meant to be a general-purpose S3 proxy for the rest of the
    bucket.
  - w, h: target pixel dimensions. At least one is required; the other,
    if omitted, is derived to preserve the source's aspect ratio.
  - fit: cover (default) | contain | inside. Only meaningful when both w
    and h are given.
      cover   -- crop to fill the box exactly (Pillow's ImageOps.fit).
      contain -- letterbox: whole image visible, centered, padded to
                 exactly fill the box.
      inside  -- scale down to fit within the box, no crop, no padding,
                 never upscales (Pillow's Image.thumbnail semantics) --
                 the resulting image may be smaller than w x h.
  - fmt: webp (default) | avif | jpeg | png. avif silently falls back to
    webp if this Lambda's Pillow build doesn't have libavif support (see
    AVIF_SUPPORTED below) -- every browser that would ask for avif also
    understands webp, so that's a safe substitution rather than a 500.
  - q: JPEG/WebP/AVIF quality, 1-100 (default 82). Ignored for png.

Every response carries a year-long immutable Cache-Control: the full
param set is baked into the output bytes, so the same path+params always
produces byte-identical output -- safe to cache forever at both CloudFront
and the browser.

Deliberately no second-tier cache (e.g. writing the resized output back to
S3) in this first version -- CloudFront's own edge cache already satisfies
the ask as stated. Revisit if a CloudFront invalidation ever needs to
happen often enough that re-paying the Lambda cost on every size again
becomes a real problem.
"""
import base64
import io
import os

from PIL import Image, ImageOps

# Keep in sync with the S3 read policy's Resource prefixes in
# template.yaml's ImageResizerFunction -- this check and that IAM scoping
# are meant to enforce the same boundary. Checking it here first, before
# ever calling S3, also means an out-of-scope key gets a clean 404 instead
# of surfacing an AccessDenied.
ALLOWED_PREFIXES = ("product-images/", "article-images/")

MIN_DIMENSION = 16
MAX_DIMENSION = 2000
DEFAULT_QUALITY = 82
MIN_QUALITY = 1
MAX_QUALITY = 100

FORMAT_CONTENT_TYPES = {
    "webp": "image/webp",
    "avif": "image/avif",
    "jpeg": "image/jpeg",
    "png": "image/png",
}
PILLOW_FORMAT_NAMES = {
    "webp": "WEBP",
    "avif": "AVIF",
    "jpeg": "JPEG",
    "png": "PNG",
}

_s3 = None


def _get_s3_client():
    """Deferred import, same convention every other function in this repo
    uses for boto3 (see e.g. image_processor/app.py) -- keeps this module
    importable for unit tests in environments that don't have boto3
    installed, since only fetch_source_bytes actually needs it."""
    global _s3
    if _s3 is None:
        import boto3

        _s3 = boto3.client("s3")
    return _s3


def _probe_avif_support():
    """Probed once at cold-start, not per-request -- Pillow only has AVIF
    support when built against libavif (via the separate pillow-avif-
    plugin, or a Pillow wheel new enough to bundle it). Cheap (1x1 pixel)
    and side-effect-free."""
    try:
        Image.new("RGB", (1, 1)).save(io.BytesIO(), format="AVIF")
        return True
    except Exception:
        return False


AVIF_SUPPORTED = _probe_avif_support()


class BadRequest(Exception):
    pass


def _clamp_int(raw, name, minimum, maximum):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise BadRequest(f"{name} must be between {minimum} and {maximum}")
    return value


def parse_params(query):
    """query is the flat str->str dict shape API Gateway v2 / Lambda
    Function URL events give as queryStringParameters (repeated keys are
    already collapsed to their last value by that layer -- not this
    function's concern)."""
    query = query or {}

    width = _clamp_int(query["w"], "w", MIN_DIMENSION, MAX_DIMENSION) if query.get("w") else None
    height = _clamp_int(query["h"], "h", MIN_DIMENSION, MAX_DIMENSION) if query.get("h") else None
    if width is None and height is None:
        raise BadRequest("at least one of w or h is required")

    fit = query.get("fit", "cover")
    if fit not in ("cover", "contain", "inside"):
        raise BadRequest("fit must be one of: cover, contain, inside")

    fmt = query.get("fmt", "webp")
    if fmt not in FORMAT_CONTENT_TYPES:
        raise BadRequest(f"fmt must be one of: {', '.join(FORMAT_CONTENT_TYPES)}")
    if fmt == "avif" and not AVIF_SUPPORTED:
        fmt = "webp"

    quality = _clamp_int(query.get("q", DEFAULT_QUALITY), "q", MIN_QUALITY, MAX_QUALITY)

    return {"width": width, "height": height, "fit": fit, "fmt": fmt, "quality": quality}


def _resize_cover(image, width, height):
    return ImageOps.fit(image, (width, height), Image.LANCZOS)


def _resize_contain(image, width, height):
    canvas = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    fitted = image.copy()
    fitted.thumbnail((width, height), Image.LANCZOS)
    offset = ((width - fitted.width) // 2, (height - fitted.height) // 2)
    mask = fitted if fitted.mode == "RGBA" else None
    canvas.paste(fitted, offset, mask)
    return canvas


def _resize_inside(image, width, height):
    resized = image.copy()
    resized.thumbnail((width, height), Image.LANCZOS)
    return resized


def _resize_single_dimension(image, width, height):
    target, is_width = (width, True) if width else (height, False)
    ratio = target / (image.width if is_width else image.height)
    new_size = (round(image.width * ratio), round(image.height * ratio))
    return image.resize(new_size, Image.LANCZOS)


def _convert_for_format(image, fmt):
    if fmt == "jpeg" and image.mode in ("RGBA", "P"):
        # JPEG has no alpha channel -- flatten onto white rather than let
        # Pillow silently drop it, which can leave dark fringing on
        # anything that had real transparency.
        rgba = image.convert("RGBA") if image.mode == "P" else image
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    if fmt == "png" and image.mode not in ("RGBA", "RGB", "P"):
        return image.convert("RGBA")
    return image


def resize_image(source_bytes, params):
    image = Image.open(io.BytesIO(source_bytes))
    # Bake in EXIF-declared rotation now -- a resized-and-recompressed
    # output no longer reliably carries the original EXIF Orientation tag
    # for a browser to interpret later, so this is the only chance to
    # apply it to the actual pixel buffer.
    image = ImageOps.exif_transpose(image)

    width, height = params["width"], params["height"]
    if width and height:
        resizer = {"cover": _resize_cover, "contain": _resize_contain, "inside": _resize_inside}[params["fit"]]
        image = resizer(image, width, height)
    else:
        image = _resize_single_dimension(image, width, height)

    fmt = params["fmt"]
    image = _convert_for_format(image, fmt)

    out = io.BytesIO()
    save_kwargs = {"optimize": True} if fmt == "png" else {"quality": params["quality"]}
    image.save(out, format=PILLOW_FORMAT_NAMES[fmt], **save_kwargs)
    return out.getvalue(), FORMAT_CONTENT_TYPES[fmt]


def _response(status, body_bytes=b"", content_type="text/plain", cacheable=False):
    headers = {"Content-Type": content_type}
    headers["Cache-Control"] = "public, max-age=31536000, immutable" if cacheable else "no-store"
    return {
        "statusCode": status,
        "headers": headers,
        "body": base64.b64encode(body_bytes).decode("ascii") if body_bytes else "",
        "isBase64Encoded": bool(body_bytes),
    }


def fetch_source_bytes(key):
    """Returns raw bytes, or None if the key doesn't exist / can't be
    read. Any S3 failure (missing key, access denied, whatever) maps to
    the same "not found" outcome from this endpoint's point of view --
    there's no meaningful distinction a caller of a public image endpoint
    should see."""
    try:
        obj = _get_s3_client().get_object(Bucket=os.environ["IMAGE_BUCKET"], Key=key)
        return obj["Body"].read()
    except Exception:
        return None


def handler(event, context):
    key = (event.get("rawPath") or "/").lstrip("/")
    if not key or not key.startswith(ALLOWED_PREFIXES):
        return _response(404, b"image not found", "text/plain")

    try:
        params = parse_params(event.get("queryStringParameters"))
    except BadRequest as exc:
        return _response(400, str(exc).encode(), "text/plain")

    source_bytes = fetch_source_bytes(key)
    if source_bytes is None:
        return _response(404, b"image not found", "text/plain")

    try:
        image_bytes, content_type = resize_image(source_bytes, params)
    except Exception:
        return _response(500, b"failed to process image", "text/plain")

    return _response(200, image_bytes, content_type, cacheable=True)
