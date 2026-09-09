"""
Tests for src/image_resizer/app.py.

Same synthetic-image approach as test_image_processor.py's own header
comment explains (no outbound network access to a real image CDN in this
sandbox) -- images here are generated with Pillow itself. fetch_source_bytes
(the one function that actually talks to S3) is monkeypatched in handler()
tests rather than faking boto3's S3 client shape, since resize_image/
parse_params are what this module's real logic lives in -- handler() itself
is just wiring.
"""
import io
import os
import sys

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "image_resizer"))

import app  # noqa: E402


def _synthetic_source(size=(400, 200), mode="RGB", color=(200, 50, 50)):
    image = Image.new(mode, size, color)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _synthetic_source_with_alpha(size=(400, 200), color=(200, 50, 50, 128)):
    image = Image.new("RGBA", size, color)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


# --- parse_params ----------------------------------------------------------

def test_parse_params_requires_at_least_one_dimension():
    try:
        app.parse_params({})
        assert False, "expected BadRequest"
    except app.BadRequest:
        pass


def test_parse_params_defaults():
    params = app.parse_params({"w": "300"})
    assert params == {"width": 300, "height": None, "fit": "cover", "fmt": "webp", "quality": app.DEFAULT_QUALITY}


def test_parse_params_both_dimensions_and_overrides():
    params = app.parse_params({"w": "300", "h": "200", "fit": "contain", "fmt": "jpeg", "q": "60"})
    assert params == {"width": 300, "height": 200, "fit": "contain", "fmt": "jpeg", "quality": 60}


def test_parse_params_rejects_out_of_range_width():
    try:
        app.parse_params({"w": str(app.MAX_DIMENSION + 1)})
        assert False, "expected BadRequest"
    except app.BadRequest:
        pass


def test_parse_params_rejects_non_integer_width():
    try:
        app.parse_params({"w": "not-a-number"})
        assert False, "expected BadRequest"
    except app.BadRequest:
        pass


def test_parse_params_rejects_invalid_fit():
    try:
        app.parse_params({"w": "100", "fit": "sideways"})
        assert False, "expected BadRequest"
    except app.BadRequest:
        pass


def test_parse_params_rejects_invalid_fmt():
    try:
        app.parse_params({"w": "100", "fmt": "bmp"})
        assert False, "expected BadRequest"
    except app.BadRequest:
        pass


def test_parse_params_rejects_out_of_range_quality():
    try:
        app.parse_params({"w": "100", "q": "101"})
        assert False, "expected BadRequest"
    except app.BadRequest:
        pass


def test_parse_params_falls_back_to_webp_when_avif_unsupported(monkeypatch=None):
    original = app.AVIF_SUPPORTED
    app.AVIF_SUPPORTED = False
    try:
        params = app.parse_params({"w": "100", "fmt": "avif"})
        assert params["fmt"] == "webp"
    finally:
        app.AVIF_SUPPORTED = original


def test_parse_params_keeps_avif_when_supported():
    original = app.AVIF_SUPPORTED
    app.AVIF_SUPPORTED = True
    try:
        params = app.parse_params({"w": "100", "fmt": "avif"})
        assert params["fmt"] == "avif"
    finally:
        app.AVIF_SUPPORTED = original


# --- resize_image: fit modes ------------------------------------------------

def test_resize_cover_produces_exact_requested_dimensions():
    source = _synthetic_source(size=(400, 200))
    params = {"width": 100, "height": 100, "fit": "cover", "fmt": "png", "quality": app.DEFAULT_QUALITY}
    image_bytes, content_type = app.resize_image(source, params)
    result = Image.open(io.BytesIO(image_bytes))
    assert result.size == (100, 100)
    assert content_type == "image/png"


def test_resize_contain_pads_to_exact_box_without_cropping():
    # A wide source into a square box -- contain must not crop, so the
    # output canvas is still exactly the requested box, letterboxed.
    source = _synthetic_source(size=(400, 100))
    params = {"width": 200, "height": 200, "fit": "contain", "fmt": "png", "quality": app.DEFAULT_QUALITY}
    image_bytes, _content_type = app.resize_image(source, params)
    result = Image.open(io.BytesIO(image_bytes))
    assert result.size == (200, 200)


def test_resize_inside_never_upscales_and_preserves_aspect():
    # Source smaller than the requested box in both dimensions -- "inside"
    # must not enlarge it past its own size.
    source = _synthetic_source(size=(50, 50))
    params = {"width": 200, "height": 200, "fit": "inside", "fmt": "png", "quality": app.DEFAULT_QUALITY}
    image_bytes, _content_type = app.resize_image(source, params)
    result = Image.open(io.BytesIO(image_bytes))
    assert result.size == (50, 50)


def test_resize_single_width_preserves_aspect_ratio():
    source = _synthetic_source(size=(400, 200))  # 2:1
    params = {"width": 100, "height": None, "fit": "cover", "fmt": "png", "quality": app.DEFAULT_QUALITY}
    image_bytes, _content_type = app.resize_image(source, params)
    result = Image.open(io.BytesIO(image_bytes))
    assert result.size == (100, 50)


def test_resize_single_height_preserves_aspect_ratio():
    source = _synthetic_source(size=(400, 200))  # 2:1
    params = {"width": None, "height": 50, "fit": "cover", "fmt": "png", "quality": app.DEFAULT_QUALITY}
    image_bytes, _content_type = app.resize_image(source, params)
    result = Image.open(io.BytesIO(image_bytes))
    assert result.size == (100, 50)


# --- resize_image: format conversion ---------------------------------------

def test_resize_jpeg_flattens_alpha_onto_white_not_black():
    source = _synthetic_source_with_alpha(size=(20, 20), color=(10, 10, 10, 0))  # fully transparent
    params = {"width": 20, "height": 20, "fit": "cover", "fmt": "jpeg", "quality": app.DEFAULT_QUALITY}
    image_bytes, content_type = app.resize_image(source, params)
    result = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    assert content_type == "image/jpeg"
    assert result.mode == "RGB"
    # Fully transparent source pixel should flatten to white, not black --
    # confirms the mask is applied, not just an alpha channel drop.
    corner = result.getpixel((0, 0))
    assert corner[0] > 200 and corner[1] > 200 and corner[2] > 200


def test_resize_webp_output_is_readable_and_correct_type():
    source = _synthetic_source(size=(60, 60))
    params = {"width": 30, "height": 30, "fit": "cover", "fmt": "webp", "quality": app.DEFAULT_QUALITY}
    image_bytes, content_type = app.resize_image(source, params)
    result = Image.open(io.BytesIO(image_bytes))
    assert result.format == "WEBP"
    assert content_type == "image/webp"


# --- handler -----------------------------------------------------------

def _event(path, query=None):
    return {"rawPath": path, "queryStringParameters": query or {}}


def test_handler_rejects_path_outside_allowed_prefixes():
    response = app.handler(_event("/not-a-real-prefix/foo.jpg", {"w": "100"}), None)
    assert response["statusCode"] == 404


def test_handler_rejects_empty_path():
    response = app.handler(_event("/", {"w": "100"}), None)
    assert response["statusCode"] == 404


def test_handler_returns_400_for_bad_params():
    response = app.handler(_event("/product-images/abc/main.jpg", {}), None)
    assert response["statusCode"] == 400
    assert response["headers"]["Cache-Control"] == "no-store"


def test_handler_returns_404_when_source_missing(monkeypatch=None):
    original = app.fetch_source_bytes
    app.fetch_source_bytes = lambda key: None
    try:
        response = app.handler(_event("/product-images/abc/main.jpg", {"w": "100"}), None)
        assert response["statusCode"] == 404
    finally:
        app.fetch_source_bytes = original


def test_handler_happy_path_returns_cacheable_image():
    original = app.fetch_source_bytes
    app.fetch_source_bytes = lambda key: _synthetic_source(size=(400, 400))
    try:
        response = app.handler(_event("/article-images/xyz/hero.png", {"w": "150", "h": "150", "fmt": "png"}), None)
        assert response["statusCode"] == 200
        assert response["isBase64Encoded"] is True
        assert response["headers"]["Content-Type"] == "image/png"
        assert response["headers"]["Cache-Control"] == "public, max-age=31536000, immutable"

        import base64
        result = Image.open(io.BytesIO(base64.b64decode(response["body"])))
        assert result.size == (150, 150)
    finally:
        app.fetch_source_bytes = original


def test_handler_500_on_processing_failure_does_not_leak_exception():
    original = app.fetch_source_bytes
    app.fetch_source_bytes = lambda key: b"not a real image"
    try:
        response = app.handler(_event("/product-images/abc/main.jpg", {"w": "100"}), None)
        assert response["statusCode"] == 500
    finally:
        app.fetch_source_bytes = original


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"PASS: {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")
