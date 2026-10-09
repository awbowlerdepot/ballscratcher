"""Signage generation (runbook 6cn): prompts, parsing, and both jobs with fake
clients. The prototype's lessons are pinned here -- no lastFrame for Veo,
particles/fire banned, taglines never instruct or mention the website."""
import base64
import io
import json
import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "product_article_generator"))

import signage  # noqa: E402


class FakeCursor:
    def __init__(self, log, fetch=None):
        self.log, self.fetch = log, fetch

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.fetch


class FakeConn:
    def __init__(self):
        self.log, self.commits = [], 0

    def cursor(self):
        return FakeCursor(self.log)

    def commit(self):
        self.commits += 1


class FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)


class FakeBedrock:
    def __init__(self, text):
        self.text, self.bodies = text, []

    def invoke_model(self, **kw):
        self.bodies.append(json.loads(kw["body"]))

        class Body:
            def __init__(s, t):
                s.t = t

            def read(s):
                return json.dumps({"content": [{"text": s.t}]}).encode()

        return {"body": Body(self.text)}


CTX = {"article_id": "a1", "product_id": "p1", "visual_theme": "a raptor diving through clouds",
       "name": "Raptor Pursuit", "brand": "MOTIV", "selected_still_key": None, "selected_still_url": None}


def test_taglines_are_cleaned_and_in_store_safe():
    raw = """1. Talons find the pocket.
- "The hunt is on."
Ask our pro shop to drill yours today
Visit bowlerdepot.com for more
Talons find the pocket.
This tagline is far too long to fit on a store sign under a price
Dive steep. Strike hard."""
    assert signage.parse_tagline_options(raw) == ["Talons find the pocket.", "The hunt is on.", "Dive steep. Strike hard."]


def _png(w, h, draw=None):
    im = Image.new("RGB", (w, h), (40, 60, 90))
    if draw:
        draw(ImageDraw.Draw(im), w, h)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _scene(d, w, h):
    # Smooth vertical gradient + a ball: a natural, seam-free vertical image.
    for y in range(h):
        d.line([(0, y), (w, y)], fill=(30 + y * 100 // h, 50, 90 + y * 60 // h))
    d.ellipse([w * 0.25, h * 0.45, w * 0.75, h * 0.73], fill=(200, 200, 210))


def _letterboxed(d, w, h):
    # Square content in the middle, flat blurred-looking bands above/below.
    _scene(d, w, h)
    band = (h - w) // 2
    d.rectangle([0, 0, w, band], fill=(120, 120, 125))
    d.rectangle([0, h - band, w, h], fill=(120, 120, 125))


def test_outpaint_prompt_keeps_ball_and_bans_letterboxing():
    p = signage.build_outpaint_prompt()
    assert "Extend this exact image" in p and "never letterbox" in p and "9:16 VERTICAL" in p


def test_source_is_enlarged_and_bottom_anchored_on_9x16_canvas():
    ref = Image.open(io.BytesIO(base64.b64decode(signage.place_on_canvas(_png(1376, 768)))))
    assert ref.size == (768, 1376)
    # top is empty grey canvas (to be painted), the source sits just above the bottom margin
    assert ref.getpixel((384, 100)) == signage.CANVAS_GREY
    assert ref.getpixel((384, 1376 - 90)) == (40, 60, 90)
    assert ref.getpixel((384, 1376 - 40)) == signage.CANVAS_GREY
    # a square product shot fills the width
    sq = Image.open(io.BytesIO(base64.b64decode(signage.place_on_canvas(_png(1024, 1024)))))
    assert sq.getpixel((2, 1000)) == (40, 60, 90)


def test_letterbox_score_separates_banded_from_vertical():
    assert signage.letterbox_score(_png(768, 1376, _scene)) < signage.LETTERBOX_THRESHOLD
    assert signage.letterbox_score(_png(768, 1376, _letterboxed)) >= signage.LETTERBOX_THRESHOLD


def test_veo_request_has_no_last_frame_and_bans_particles():
    sent = {}

    class Resp:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class Session:
        def post(self, url, headers=None, json=None, timeout=None):
            if url.endswith(":predictLongRunning"):
                sent["body"] = json
                return Resp({"name": "op/1"})
            return Resp({"done": True, "response": {"videos": [{"bytesBase64Encoded": base64.b64encode(b"mp4").decode()}]}})

    signage.VEO_POLL_S = 0
    out = signage.call_veo(Session(), "tok", "proj", b"png", "prompt")
    assert out == b"mp4"
    inst = sent["body"]["instances"][0]
    assert "lastFrame" not in inst and inst["image"]["mimeType"] == "image/png"
    params = sent["body"]["parameters"]
    assert params["aspectRatio"] == "9:16" and params["generateAudio"] is False
    assert all(w in params["negativePrompt"] for w in ("embers", "sparks", "fire", "lens flare"))


def test_ball_position_parsing_and_fallback():
    assert signage.parse_ball_position('{"cx": 0.49, "cy": 0.62, "r": 0.29}') == {"x": 0.49, "y": 0.62, "r": 0.29}
    assert signage.parse_ball_position("not json") == signage.DEFAULT_BALL_POSITION
    assert signage.parse_ball_position('{"cx": 2, "cy": 0.6, "r": 0.3}') == signage.DEFAULT_BALL_POSITION


def test_generate_stills_outpaints_source_retries_letterboxed_and_stores():
    conn, s3 = FakeConn(), FakeS3()
    good, banded = _png(768, 1376, _scene), _png(768, 1376, _letterboxed)
    calls, refs = [], set()
    # candidate 1: banded then good (retried); 2: raises twice (skipped);
    # 3: banded twice (kept anyway -- least-banded attempt)
    outputs = [banded, good, RuntimeError("boom"), RuntimeError("filtered"), banded, banded]

    def gemini(prompt, ref, ratio):
        calls.append(ratio)
        refs.add(ref)
        assert "Extend this exact image" in prompt
        out = outputs[len(calls) - 1]
        if isinstance(out, Exception):
            raise out
        return out

    out = signage.generate_stills(conn, CTX, source_png=_png(1376, 768), gemini_call=gemini,
                                  s3_client=s3, image_bucket="bkt", bedrock_client=FakeBedrock("The hunt is on.\nAsk us"),
                                  model_id="m")
    assert out == {"stills": 2, "taglines": 1}
    assert calls == ["9:16"] * 6 and len(refs) == 1
    assert s3.puts[0]["Body"] == good
    assert all(p["Key"].startswith("article-images/p1/signage/still_") for p in s3.puts)
    sql, params = conn.log[-1]
    assert "insert into article_signage" in sql and "stills_ready" in params
    assert json.loads(params[params.index("stills_ready") + 3])[0]["url"].startswith("https://bkt.s3.amazonaws.com/")
    assert conn.commits == 1


def test_veo_prompts_lock_the_camera():
    # Runbook 6cr: a forward drift kept pushing in through the extension.
    assert "LOCKED OFF" in signage.build_veo_prompt("webs") and "forward drift" not in signage.build_veo_prompt("webs")
    p = signage.build_veo_extend_prompt("webs")
    assert p.startswith("Continue this exact shot") and "LOCKED OFF" in p


def test_veo_extension_sends_the_clip_as_bytes_without_duration():
    sent = {}

    class Resp:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class Session:
        def post(self, url, headers=None, json=None, timeout=None):
            if url.endswith(":predictLongRunning"):
                sent["body"] = json
                return Resp({"name": "op/1"})
            return Resp({"done": True, "response": {"videos": [{"bytesBase64Encoded": base64.b64encode(b"15s").decode()}]}})

    signage.VEO_POLL_S = 0
    assert signage.call_veo(Session(), "tok", "proj", None, "p", extend_mp4=b"8s") == b"15s"
    inst, params = sent["body"]["instances"][0], sent["body"]["parameters"]
    assert inst["video"] == {"bytesBase64Encoded": base64.b64encode(b"8s").decode(), "mimeType": "video/mp4"}
    assert "image" not in inst and "durationSeconds" not in params
    assert params["resolution"] == "1080p" and "camera movement" in params["negativePrompt"]


def test_animate_extends_the_clip_and_falls_back_to_8s_if_extension_fails():
    for ext, expect in ((lambda mp4, p: mp4 + b"+7s", b"mp4+7s"), (lambda mp4, p: 1 / 0, b"mp4")):
        conn, s3 = FakeConn(), FakeS3()
        signage.animate(conn, CTX, still_png=b"png", veo_call=lambda png, prompt: b"mp4", s3_client=s3,
                        image_bucket="bkt", bedrock_client=FakeBedrock('{"cx":0.5,"cy":0.6,"r":0.3}'),
                        model_id="m", invoke_renderer=lambda p: None, veo_extend_call=ext)
        assert s3.puts[0]["Body"] == expect


def test_animate_uploads_clip_records_ball_and_hands_off_to_renderer():
    conn, s3, invoked = FakeConn(), FakeS3(), []
    out = signage.animate(conn, CTX, still_png=b"png", veo_call=lambda png, prompt: b"mp4", s3_client=s3,
                          image_bucket="bkt", bedrock_client=FakeBedrock('{"cx":0.5,"cy":0.6,"r":0.3}'),
                          model_id="m", invoke_renderer=invoked.append)
    assert s3.puts[0]["ContentType"] == "video/mp4" and "clip_raw_" in s3.puts[0]["Key"]
    assert out["ball"] == {"x": 0.5, "y": 0.6, "r": 0.3}
    assert invoked == [{"mode": "loop", "article_id": "a1"}]
