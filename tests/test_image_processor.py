"""
Tests for src/image_processor/app.py.

Honesty note (same pattern as the other test files, see their header
comments): this sandbox never had outbound network access to Brunswick's
actual image CDN, so there's no real product photo to test against. These
tests instead generate synthetic images with Pillow itself -- a filled
circle on a transparent canvas (simulating a studio cutout with alpha),
and a filled circle flattened onto a near-white background (simulating a
source that doesn't provide transparency) -- with exactly-known geometry,
so bbox detection and centering can be verified precisely rather than
eyeballed. This proves the geometry/logic is correct; it does NOT prove
Brunswick's real photos actually match either synthetic pattern. Per the
architecture doc's own caution, validate against a handful of real
downloaded images before trusting this in production.
"""
import io
import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "image_processor"))

import app  # noqa: E402


def _synthetic_alpha_circle(canvas_size=(800, 600), ellipse_box=(450, 300, 649, 499), fill=(120, 60, 200, 255)):
    """Transparent canvas with an off-center filled circle (square bounding
    box, i.e. an actual circle -- real bowling ball photos are round). The
    canvas is deliberately non-square and the circle deliberately not
    centered, so tests can't pass by accident (e.g. an all-zero bbox
    trivially "centered" on a square canvas)."""
    image = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse(ellipse_box, fill=fill)
    return image, ellipse_box


def _synthetic_background_circle(canvas_size=(800, 600), ellipse_box=(100, 80, 399, 379), fill=(30, 30, 30), bg=(250, 250, 248)):
    image = Image.new("RGB", canvas_size, bg)
    draw = ImageDraw.Draw(image)
    draw.ellipse(ellipse_box, fill=fill)
    return image.convert("RGBA"), ellipse_box, bg


def _expected_bbox(ellipse_box):
    x0, y0, x1, y1 = ellipse_box
    # PIL's getbbox() right/bottom are exclusive (one past the last
    # non-zero pixel); ellipse() fill is inclusive of x1/y1.
    return (x0, y0, x1 + 1, y1 + 1)


def test_bbox_from_alpha_matches_known_circle_geometry():
    image, ellipse_box = _synthetic_alpha_circle()
    bbox = app.bbox_from_alpha(image)
    assert bbox == _expected_bbox(ellipse_box)


def test_bbox_from_background_matches_known_circle_geometry():
    image, ellipse_box, bg = _synthetic_background_circle()
    bbox = app.bbox_from_background(image, bg_color=bg)
    assert bbox == _expected_bbox(ellipse_box)


def test_sample_background_color_estimates_correctly():
    image, _ellipse_box, bg = _synthetic_background_circle()
    sampled = app.sample_background_color(image)
    assert sampled == bg


def test_has_real_transparency_true_for_alpha_image():
    image, _ = _synthetic_alpha_circle()
    assert app.has_real_transparency(image) is True


def test_has_real_transparency_false_for_flattened_image():
    image, _ellipse_box, _bg = _synthetic_background_circle()
    assert app.has_real_transparency(image) is False


def test_detect_bbox_dispatches_to_alpha_for_transparent_source():
    image, ellipse_box = _synthetic_alpha_circle()
    bbox, method = app.detect_bbox(image)
    assert method == "alpha"
    assert bbox == _expected_bbox(ellipse_box)


def test_detect_bbox_dispatches_to_background_for_flattened_source():
    image, ellipse_box, _bg = _synthetic_background_circle()
    bbox, method = app.detect_bbox(image)
    assert method == "background"
    assert bbox == _expected_bbox(ellipse_box)


def test_normalize_composition_centers_ball_with_correct_margin():
    """The core requirement from the architecture doc: regardless of the
    source photo's original off-center padding, the normalized output
    should have the ball centered with margin_pct on every side.

    Uses a transparent output background (rather than the production
    default of opaque white -- see DEFAULT_BACKGROUND / module docstring)
    specifically so this test can verify centering via alpha-bbox
    detection. With the real opaque-white default, the whole canvas is
    100% opaque by design (that's the point -- a consistent flat
    background across every manufacturer source, not just the ones that
    happened to have alpha), so there'd be no alpha signal left to check
    geometry against; color-threshold detection would be needed instead.
    This test isolates the centering math itself, not the choice of
    output background."""
    image, _ellipse_box = _synthetic_alpha_circle()
    canvas_size = 400
    margin_pct = 0.10

    normalized = app.normalize_composition(
        image, canvas_size=canvas_size, margin_pct=margin_pct, background=(0, 0, 0, 0)
    )
    assert normalized.size == (canvas_size, canvas_size)

    result_bbox = app.bbox_from_alpha(normalized)
    left, top, right, bottom = result_bbox
    right_margin = canvas_size - right
    bottom_margin = canvas_size - bottom

    expected_margin = canvas_size * margin_pct
    # Allow a small tolerance for rounding during scale/resize -- exact
    # sub-pixel margins aren't achievable, but should be within ~2px.
    tolerance = 3
    assert abs(left - expected_margin) <= tolerance
    assert abs(top - expected_margin) <= tolerance
    assert abs(right_margin - expected_margin) <= tolerance
    assert abs(bottom_margin - expected_margin) <= tolerance

    # The source shape is a true circle (square bounding box), so the
    # normalized ball should also come out square -- confirms the scale
    # step preserves aspect ratio rather than stretching to fill the canvas.
    ball_w = right - left
    ball_h = bottom - top
    assert abs(ball_w - ball_h) <= tolerance * 2


def test_normalize_composition_handles_no_detectable_bbox_without_crashing():
    """A fully-transparent image (nothing to bound) shouldn't raise --
    should fall back to returning something rather than dropping the image."""
    blank = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    result = app.normalize_composition(blank, canvas_size=100)
    assert result.size == (100, 100)


def test_generate_size_variants_produces_expected_pixel_sizes():
    normalized = Image.new("RGBA", (1600, 1600), (255, 255, 255, 255))
    variants = app.generate_size_variants(normalized)
    assert set(variants.keys()) == {"thumbnail", "catalog", "detail"}
    assert variants["thumbnail"].size == (200, 200)
    assert variants["catalog"].size == (600, 600)
    assert variants["detail"].size == (1600, 1600)


def test_process_image_end_to_end_synthetic():
    image, _ellipse_box = _synthetic_alpha_circle()
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    result = app.process_image(png_bytes, canvas_size=800)

    assert result["bbox_method"] == "alpha"
    assert set(result["variants"].keys()) == {"thumbnail", "catalog", "detail"}

    detail_img = Image.open(io.BytesIO(result["variants"]["detail"]))
    assert detail_img.size == (800, 800)

    thumb_img = Image.open(io.BytesIO(result["variants"]["thumbnail"]))
    assert thumb_img.size == (200, 200)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"PASS: {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")
