"""Signage renderer (runbook 6cn): the pure command/URL builders. The
Chromium + ffmpeg path itself was exercised inside the built container image
(see the runbook)."""
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "signage_renderer"))

import app  # noqa: E402


def test_loop_filter_crossfades_last_second_into_first():
    f = app.loop_filter()
    assert "trim=start=1:end=8" in f and "trim=start=0:end=1" in f and "xfade=transition=fade:duration=1:offset=6" in f


def test_extended_15s_clip_loops_at_14s_one_overlay_cycle():
    # Runbook 6cr: Veo extension -> 15 s raw -> a 14 s loop, so the 14 s MP4
    # plays its background exactly once.
    f = app.loop_filter(15)
    assert "trim=start=1:end=15" in f and "offset=13" in f
    assert 15 - 1 == app.OVERLAY_LOOP_S


def test_composite_overlays_frames_on_looped_clip_for_the_full_cycle():
    args = app.composite_args("/tmp/clip.mp4", "/tmp/f_%05d.png", "/tmp/out.mp4")
    assert args[args.index("-stream_loop") + 1] == "-1"
    assert args[args.index("-t") + 1] == str(app.OVERLAY_LOOP_S) == "14"
    assert "overlay=0:0" in args[args.index("-filter_complex") + 1]
    assert "libx264" in args and "+faststart" in args


def test_overlay_url_is_transparent_mode_with_ball_and_tagline():
    url = app.overlay_url("https://learn.bowlerdepot.com/", "motiv-raptor-pursuit",
                          {"ball_x": 0.49, "ball_y": 0.62, "ball_r": 0.29, "tagline": "Talons find the pocket."})
    parsed = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(parsed.query))
    assert parsed.path == "/signage/ball/motiv-raptor-pursuit"
    assert q == {"overlay": "1", "bx": "0.49", "by": "0.62", "br": "0.29", "cta": "Talons find the pocket."}


def test_mp4_fades_in_from_and_out_to_black_for_pisignage_playlists():
    # Runbook 6cq: piSignage has no video transitions -- it cuts to black
    # between playlist items -- so the MP4 itself fades in and out.
    f = app.composite_args("/tmp/clip.mp4", "/tmp/f_%05d.png", "/tmp/out.mp4")
    f = f[f.index("-filter_complex") + 1]
    assert "fade=t=in:st=0:d=0.5" in f
    assert f"fade=t=out:st={app.OVERLAY_LOOP_S - 1.0}:d=1.0" in f
    assert f.index("overlay=0:0") < f.index("fade=t=in")  # the text fades with the picture
