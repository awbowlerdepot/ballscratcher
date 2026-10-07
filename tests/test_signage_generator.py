"""Signage generation (runbook 6cn): prompts, parsing, and both jobs with fake
clients. The prototype's lessons are pinned here -- no lastFrame for Veo,
particles/fire banned, taglines never instruct or mention the website."""
import base64
import json
import os
import sys

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


def test_still_prompt_adds_native_vertical_composition():
    p = signage.build_still_prompt("SCENE")
    assert p.startswith("SCENE") and "9:16 VERTICAL" in p and "blurred or empty bars" in p


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


def test_generate_stills_stores_candidates_and_taglines():
    conn, s3 = FakeConn(), FakeS3()
    calls = []

    def gemini(prompt, ref, ratio):
        calls.append(ratio)
        if len(calls) == 2:
            raise RuntimeError("one candidate failed")
        return b"png"

    out = signage.generate_stills(conn, CTX, scene_prompt="SCENE", reference_b64="ref", gemini_call=gemini,
                                  s3_client=s3, image_bucket="bkt", bedrock_client=FakeBedrock("The hunt is on.\nAsk us"),
                                  model_id="m")
    assert out == {"stills": 2, "taglines": 1}
    assert calls == ["9:16"] * 3
    assert all(p["Key"].startswith("article-images/p1/signage/still_") for p in s3.puts)
    sql, params = conn.log[-1]
    assert "insert into article_signage" in sql and "stills_ready" in params
    assert json.loads(params[params.index("stills_ready") + 3])[0]["url"].startswith("https://bkt.s3.amazonaws.com/")
    assert conn.commits == 1


def test_animate_uploads_clip_records_ball_and_hands_off_to_renderer():
    conn, s3, invoked = FakeConn(), FakeS3(), []
    out = signage.animate(conn, CTX, still_png=b"png", veo_call=lambda png, prompt: b"mp4", s3_client=s3,
                          image_bucket="bkt", bedrock_client=FakeBedrock('{"cx":0.5,"cy":0.6,"r":0.3}'),
                          model_id="m", invoke_renderer=invoked.append)
    assert s3.puts[0]["ContentType"] == "video/mp4" and "clip_raw_" in s3.puts[0]["Key"]
    assert out["ball"] == {"x": 0.5, "y": 0.6, "r": 0.3}
    assert invoked == [{"mode": "loop", "article_id": "a1"}]
