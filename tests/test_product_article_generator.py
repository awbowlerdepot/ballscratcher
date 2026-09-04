"""
Tests for src/product_article_generator/app.py -- the full bowling.com-
style "ball review" article generator (022_product_articles.sql). See that
module's own docstring for the full "why" (Al's ask, the review-workflow/
full-article/generation-gate scope decisions, the transcript+summary
prompt design, and the sibling-inference heuristic).

Manual-runner pattern, run standalone via
`python3 tests/test_product_article_generator.py` -- same convention as
test_bowlerdepot_video_sync.py/test_video_summarizer.py (no pytest in this
sandbox).

Everything DB-facing is exercised against a fake psycopg2-shaped
cursor/connection; Bedrock calls against a fake bedrock-runtime-shaped
client (same _FakeBedrockClient shape as test_video_summarizer.py).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "product_article_generator"))

import app  # noqa: E402


# --- normalize_line_name: pure, no DB, no network ---

def test_normalize_line_name_strips_single_qualifier():
    assert app.normalize_line_name("Equinox Solid") == "Equinox"
    assert app.normalize_line_name("Equinox Hybrid") == "Equinox"


def test_normalize_line_name_strips_multiple_trailing_qualifiers():
    # "Pro" and "Pearl" are both recognized qualifier words -- both get
    # stripped, one at a time from the end, same as the docstring's own
    # "Equinox Pearl Pro" example.
    assert app.normalize_line_name("Equinox Pearl Pro") == "Equinox"


def test_normalize_line_name_strips_version_suffix():
    assert app.normalize_line_name("Absolute V2") == "Absolute"
    assert app.normalize_line_name("Absolute II") == "Absolute"


def test_normalize_line_name_unchanged_when_nothing_recognized():
    # A bare, one-word name (or any name with no trailing qualifier word)
    # is returned unchanged, not stripped down to nothing -- see the
    # docstring's own "returns the name unchanged if nothing at the end
    # matches" guarantee.
    assert app.normalize_line_name("Equinox") == "Equinox"
    assert app.normalize_line_name("Storm Absolute") == "Storm Absolute"


def test_normalize_line_name_strips_qualifier_and_version_together():
    # "Solid" (qualifier) then "II" (version suffix) both strip in one
    # pass -- confirms the while-loop keeps popping trailing words of
    # EITHER recognized kind, not just one or the other.
    assert app.normalize_line_name("Phaze II Solid") == "Phaze"


def test_normalize_line_name_empty_and_none_pass_through():
    assert app.normalize_line_name("") == ""
    assert app.normalize_line_name(None) is None


# --- parse_article_json: pure, no DB, no network ---

_VALID_ARTICLE_JSON = {
    "title": "T", "hook": "H", "performance_summary": "P",
    "who_should_buy": ["a"], "who_should_skip": ["b"],
    "pros": ["c"], "cons": ["d"], "buying_tips": "tips",
    "verdict": "V", "faq": [{"question": "Q", "answer": "A"}],
    "comparison_table": [],
}


def test_parse_article_json_parses_bare_json():
    data = app.parse_article_json(json.dumps(_VALID_ARTICLE_JSON))
    assert data == _VALID_ARTICLE_JSON


def test_parse_article_json_strips_markdown_fence():
    fenced = "```json\n" + json.dumps(_VALID_ARTICLE_JSON) + "\n```"
    data = app.parse_article_json(fenced)
    assert data == _VALID_ARTICLE_JSON


def test_parse_article_json_strips_bare_fence_without_json_tag():
    fenced = "```\n" + json.dumps(_VALID_ARTICLE_JSON) + "\n```"
    data = app.parse_article_json(fenced)
    assert data == _VALID_ARTICLE_JSON


def test_parse_article_json_raises_on_invalid_json():
    try:
        app.parse_article_json("not json at all {{{")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "not valid JSON" in str(exc)


def test_parse_article_json_raises_on_missing_required_key():
    incomplete = dict(_VALID_ARTICLE_JSON)
    del incomplete["faq"]
    try:
        app.parse_article_json(json.dumps(incomplete))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "faq" in str(exc)


# --- build_article_prompt: pure, no DB, no network ---

_SAMPLE_PRODUCT = {
    "id": "prod-1", "name": "Equinox Solid", "brand_id": "brand-1", "brand_name": "Storm",
    "color": "Blue/Black", "core_name": "Sonar", "core_type": "asymmetric",
    "coverstock_name": "R2S Hybrid", "coverstock_type": "hybrid", "coverstock_material": "reactive",
    "factory_finish": "500/1000 Abralon", "weights_available": "12-16 lb",
    "release_date": "2024-01-01", "description": "A strong asymmetric ball.",
    "videos": [
        {"id": "vid-1", "title": "Storm Equinox Solid Review", "channel_title": "Bowling Channel",
         "summary": "Strong midlane read, clears the front easily.", "transcript": "word " * 50},
    ],
}


def test_build_article_prompt_includes_product_specs_and_video_content():
    prompt = app.build_article_prompt(_SAMPLE_PRODUCT, siblings=[])
    assert "Storm Equinox Solid" in prompt
    assert "Sonar" in prompt
    assert "R2S Hybrid" in prompt
    assert "Bowling Channel" in prompt
    assert "Strong midlane read, clears the front easily." in prompt


def test_build_article_prompt_omits_sibling_block_when_no_siblings():
    prompt = app.build_article_prompt(_SAMPLE_PRODUCT, siblings=[])
    assert "related products from the same brand" not in prompt


def test_build_article_prompt_includes_sibling_block_with_product_ids():
    siblings = [{"id": "prod-2", "name": "Equinox Hybrid"}]
    prompt = app.build_article_prompt(_SAMPLE_PRODUCT, siblings=siblings)
    assert "Equinox Hybrid (product_id: prod-2)" in prompt
    assert "Do not invent numeric specs" in prompt


def test_build_article_prompt_caps_transcript_excerpt_length():
    long_video = {
        "id": "vid-1", "title": "T", "channel_title": "C",
        "summary": "S", "transcript": "x" * 100000,
    }
    product = dict(_SAMPLE_PRODUCT, videos=[long_video])
    prompt = app.build_article_prompt(product, siblings=[])
    # Excerpt is capped at DEFAULT_TRANSCRIPT_EXCERPT_CHARS (4000), not the
    # full 100000-char transcript -- the whole prompt should stay well
    # under that raw size.
    assert len(prompt) < 20000


def test_build_article_prompt_caps_total_transcript_budget_across_videos():
    # Six videos, each individually under the per-video cap (4000) but
    # together (24000) exceeding DEFAULT_MAX_TOTAL_TRANSCRIPT_CHARS
    # (20000) -- later videos' excerpts should shrink (or go empty) once
    # the shared budget runs out, not just cap per-video independently.
    # Transcripts use a single repeated char with no spaces so each
    # excerpt is isolable in the rendered prompt via regex, unlike
    # counting a letter's occurrences across the WHOLE prompt (which
    # would also pick up unrelated 'a'/'b' letters from spec text and the
    # fixed instructions block).
    import re

    videos = [
        {"id": f"v{i}", "title": f"T{i}", "channel_title": f"C{i}", "summary": f"S{i}", "transcript": "q" * 4000}
        for i in range(6)
    ]
    product = dict(_SAMPLE_PRODUCT, videos=videos)
    prompt = app.build_article_prompt(product, siblings=[])
    excerpts = re.findall(r"Transcript excerpt: (q*)\n", prompt)
    total_excerpt_chars = sum(len(e) for e in excerpts)
    assert total_excerpt_chars <= app.DEFAULT_MAX_TOTAL_TRANSCRIPT_CHARS
    # And the budget was actually exhausted (not just individually capped
    # at 4000 each, which would total 24000) -- confirms the shared,
    # running total_transcript_chars accumulator is doing real work.
    assert total_excerpt_chars == app.DEFAULT_MAX_TOTAL_TRANSCRIPT_CHARS


# --- Fake psycopg2-shaped cursor/connection ---

class _FakeCursor:
    def __init__(self, needing_article=None, product_row=None, video_rows=None,
                 sibling_candidates=None, store_article_id="article-1"):
        self.needing_article = needing_article or []
        self.product_row = product_row
        self.video_rows = video_rows or []
        self.sibling_candidates = sibling_candidates or []
        self.store_article_id = store_article_id
        self.executed = []
        self.description = None
        self._rows = []
        self.inserted = []  # (product_id, article dict-shaped params) from store_article

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        params = params or ()
        q = " ".join(query.split())
        self.executed.append((q, params))

        if q.startswith("select distinct p.id"):
            self.description = [("id",)]
            self._rows = [(pid,) for pid in self.needing_article]

        elif q.startswith("select p.id, p.name, p.color, p.coverstock_material"):
            self.description = [
                ("id",), ("name",), ("color",), ("coverstock_material",), ("coverstock_type",),
                ("coverstock_name",), ("has_particle",), ("factory_finish",),
                ("weights_min",), ("weights_max",), ("release_date",), ("description",),
                ("brand_id",), ("brand_name",), ("core_name",), ("core_type",),
            ]
            self._rows = [self.product_row] if self.product_row else []

        elif q.startswith("select id, title, channel_title, summary, transcript"):
            self.description = [
                ("id",), ("title",), ("channel_title",), ("summary",), ("transcript",),
            ]
            self._rows = list(self.video_rows)

        elif q.startswith("select id, name from products where brand_id"):
            self.description = [("id",), ("name",)]
            self._rows = [(c["id"], c["name"]) for c in self.sibling_candidates]

        elif q.startswith("insert into product_articles"):
            self.inserted.append(params)
            self.description = [("id",)]
            self._rows = [(self.store_article_id,)]

        else:
            raise NotImplementedError(f"FakeCursor doesn't support: {q}")

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConnection:
    def __init__(self, **kwargs):
        self._cursor = _FakeCursor(**kwargs)
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


# --- list_products_needing_article ---

def test_list_products_needing_article_returns_ids():
    conn = _FakeConnection(needing_article=["prod-1", "prod-2"])
    assert app.list_products_needing_article(conn) == ["prod-1", "prod-2"]


def test_list_products_needing_article_query_excludes_blocked_channels_and_existing_articles():
    """Same "verify the executed query text, not real Postgres filtering"
    approach test_bowlerdepot_video_sync.py's own blocked-channel test
    uses -- there's no real Postgres in this sandbox."""
    conn = _FakeConnection(needing_article=[])
    app.list_products_needing_article(conn)
    executed_query = conn._cursor.executed[0][0]
    assert "blocked_video_channels" in executed_query
    assert "not exists" in executed_query
    assert "pa.id is null" in executed_query
    assert "pv.status = 'approved'" in executed_query
    assert "pv.summary is not null" in executed_query


# --- fetch_product_content ---

def test_fetch_product_content_returns_none_for_missing_product():
    conn = _FakeConnection(product_row=None)
    assert app.fetch_product_content(conn, "nonexistent") is None


def test_fetch_product_content_formats_weights_available_and_includes_videos():
    product_row = (
        "prod-1", "Equinox Solid", "Blue", "reactive", "hybrid",
        "R2S Hybrid", True, "500/1000 Abralon",
        12, 17,  # weights_min/max (max is exclusive-upper-bound, see docstring)
        "2024-01-01", "A great ball.",
        "brand-1", "Storm", "Sonar", "asymmetric",
    )
    video_rows = [("vid-1", "Review Title", "Some Channel", "A summary.", "full transcript text")]
    conn = _FakeConnection(product_row=product_row, video_rows=video_rows)

    product = app.fetch_product_content(conn, "prod-1")

    assert product["name"] == "Equinox Solid"
    assert product["weights_available"] == "12-16 lb"
    assert len(product["videos"]) == 1
    assert product["videos"][0]["title"] == "Review Title"


def test_fetch_product_content_query_excludes_blocked_channels():
    conn = _FakeConnection(product_row=None)
    # product_row=None short-circuits before the video query runs -- use a
    # real row instead so both queries fire.
    conn._cursor.product_row = (
        "prod-1", "Name", None, None, None, None, None, None,
        None, None, None, None, "brand-1", "Brand", None, None,
    )
    app.fetch_product_content(conn, "prod-1")
    video_query = [q for q, _ in conn._cursor.executed if q.startswith("select id, title, channel_title")][0]
    assert "blocked_video_channels" in video_query
    assert "not exists" in video_query
    assert "lower(bvc.channel_title) = lower(product_videos.channel_title)" in video_query


# --- infer_sibling_products ---

def test_infer_sibling_products_matches_same_normalized_line_name():
    product = {"id": "prod-1", "name": "Equinox Solid", "brand_id": "brand-1"}
    candidates = [
        {"id": "prod-2", "name": "Equinox Hybrid"},   # normalizes to "Equinox" -- matches
        {"id": "prod-3", "name": "Equinox Pearl"},    # also normalizes to "Equinox" -- matches
        {"id": "prod-4", "name": "Phaze II"},         # normalizes to "Phaze" -- no match
    ]
    conn = _FakeConnection(sibling_candidates=candidates)

    siblings = app.infer_sibling_products(conn, product)

    sibling_ids = {s["id"] for s in siblings}
    assert sibling_ids == {"prod-2", "prod-3"}


def test_infer_sibling_products_caps_at_max_siblings():
    product = {"id": "prod-1", "name": "Equinox", "brand_id": "brand-1"}
    # 6 same-line candidates, more than DEFAULT_MAX_SIBLINGS (4).
    candidates = [{"id": f"prod-{i}", "name": "Equinox Solid"} for i in range(6)]
    conn = _FakeConnection(sibling_candidates=candidates)

    siblings = app.infer_sibling_products(conn, product)

    assert len(siblings) == app.DEFAULT_MAX_SIBLINGS


def test_infer_sibling_products_empty_when_no_candidates():
    product = {"id": "prod-1", "name": "Equinox", "brand_id": "brand-1"}
    conn = _FakeConnection(sibling_candidates=[])
    assert app.infer_sibling_products(conn, product) == []


# --- store_article ---

def test_store_article_inserts_json_encoded_fields_and_commits():
    conn = _FakeConnection()
    article = dict(_VALID_ARTICLE_JSON)

    article_id = app.store_article(conn, "prod-1", article, source_video_ids=["vid-1"],
                                    sibling_product_ids=["prod-2"])

    assert article_id == "article-1"
    assert conn.commits == 1
    params = conn._cursor.inserted[0]
    assert params[0] == "prod-1"
    assert params[1] == article["title"]
    # who_should_buy (index 4) is JSON-encoded, not passed as a raw list --
    # product_articles.who_should_buy is jsonb, and psycopg2 needs a JSON
    # string (or a Json() wrapper) to write one, not a bare Python list.
    assert json.loads(params[4]) == article["who_should_buy"]


def test_store_article_query_upserts_on_conflict_and_resets_review_state():
    conn = _FakeConnection()
    app.store_article(conn, "prod-1", dict(_VALID_ARTICLE_JSON), [], [])
    query = conn._cursor.executed[0][0]
    assert "on conflict (product_id) do update set" in query
    assert "status = 'pending'" in query
    assert "reviewed_at = null" in query
    assert "resolved_by = null" in query


# --- generate_article_for_product: orchestration against fake cursor + fake Bedrock ---

class _FakeBedrockBody:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data


class _FakeBedrockClient:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.calls = []

    def invoke_model(self, modelId, contentType, accept, body):
        self.calls.append({"modelId": modelId, "body": body})
        return {"body": _FakeBedrockBody({"content": [{"text": self.response_text}]})}


def test_generate_article_for_product_reports_product_not_found():
    conn = _FakeConnection(product_row=None)
    bedrock = _FakeBedrockClient("should not be called")

    result = app.generate_article_for_product(conn, bedrock, "model-id", "nonexistent")

    assert result == {"product_id": "nonexistent", "generated": False, "reason": "product_not_found"}
    assert bedrock.calls == []


def test_generate_article_for_product_reports_no_qualifying_videos():
    product_row = (
        "prod-1", "Equinox", None, None, None, None, None, None,
        None, None, None, None, "brand-1", "Storm", None, None,
    )
    conn = _FakeConnection(product_row=product_row, video_rows=[])
    bedrock = _FakeBedrockClient("should not be called")

    result = app.generate_article_for_product(conn, bedrock, "model-id", "prod-1")

    assert result == {"product_id": "prod-1", "generated": False, "reason": "no_qualifying_videos"}
    assert bedrock.calls == []


def test_generate_article_for_product_full_success_path():
    product_row = (
        "prod-1", "Equinox Solid", "Blue", "reactive", "hybrid",
        "R2S Hybrid", True, "500/1000 Abralon",
        12, 17, "2024-01-01", "A great ball.",
        "brand-1", "Storm", "Sonar", "asymmetric",
    )
    video_rows = [("vid-1", "Review", "Some Channel", "A summary.", "transcript text")]
    sibling_candidates = [{"id": "prod-2", "name": "Equinox Hybrid"}]
    conn = _FakeConnection(product_row=product_row, video_rows=video_rows,
                            sibling_candidates=sibling_candidates, store_article_id="article-99")
    bedrock = _FakeBedrockClient(json.dumps(_VALID_ARTICLE_JSON))

    result = app.generate_article_for_product(conn, bedrock, "model-id", "prod-1")

    assert result == {
        "product_id": "prod-1", "generated": True, "article_id": "article-99",
        "video_count": 1, "sibling_count": 1,
    }
    # The stored row carries the real source video id and inferred sibling id.
    insert_params = conn._cursor.inserted[0]
    assert json.loads(insert_params[-2]) == ["prod-2"]  # sibling_product_ids
    assert json.loads(insert_params[-1]) == ["vid-1"]   # source_video_ids


def test_generate_article_for_product_propagates_bad_bedrock_json():
    """A malformed/incomplete Bedrock response must raise (parse_article_
    json's own ValueError), not silently store a broken row -- caller
    (handler's batch loop) is responsible for catching this per-product,
    same soft-fail posture bowlerdepot_video_sync's _process_one_video
    uses at its own layer."""
    product_row = (
        "prod-1", "Equinox", None, None, None, None, None, None,
        None, None, None, None, "brand-1", "Storm", None, None,
    )
    video_rows = [("vid-1", "Review", "Channel", "Summary", "transcript")]
    conn = _FakeConnection(product_row=product_row, video_rows=video_rows)
    bedrock = _FakeBedrockClient("not valid json")

    try:
        app.generate_article_for_product(conn, bedrock, "model-id", "prod-1")
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert conn._cursor.inserted == []


# --- handler: orchestration only -- DB/Bedrock-client construction monkeypatched ---

class _HandlerPatchGuard:
    """Manual save/restore for the module-level functions handler() calls
    through (get_db_connection/list_products_needing_article/
    generate_article_for_product), plus a fake `boto3` module injected
    into sys.modules -- this sandbox has no real boto3 installed (pip's
    proxy returns 403 here, same caveat noted elsewhere in this project),
    and handler() itself does a bare `import boto3` before calling
    boto3.client(...), so there's no way to reach that line at all
    without something importable under that name. Injecting a fake
    module into sys.modules["boto3"] makes `import boto3` succeed with
    our fake .client(...) rather than needing the real package.

    This test file's runner (see __main__ below) doesn't thread through
    the pytest-only `monkeypatch` fixture the way test_bowlerdepot_video_
    sync.py's does, so these two handler tests restore state themselves
    rather than leaking a patched app.generate_article_for_product (or a
    fake sys.modules["boto3"]) into whichever test happens to run next in
    this same process."""

    def __init__(self):
        self._saved_attrs = {}
        self._saved_modules = {}

    def set(self, obj, name, value):
        self._saved_attrs[(obj, name)] = getattr(obj, name)
        setattr(obj, name, value)

    def set_module(self, name, module):
        import sys

        self._saved_modules[name] = sys.modules.get(name)
        sys.modules[name] = module

    def restore(self):
        import sys

        for (obj, name), value in self._saved_attrs.items():
            setattr(obj, name, value)
        for name, original in self._saved_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


def _fake_boto3_module(bedrock_client):
    import types

    fake = types.ModuleType("boto3")
    fake.client = lambda *args, **kwargs: bedrock_client
    return fake


def test_handler_on_demand_product_id_forces_single_generation():
    conn = _FakeConnection()
    calls = []

    def _fake_generate(conn_, bedrock, model_id, product_id, force=False):
        calls.append((product_id, force))
        return {"product_id": product_id, "generated": True, "article_id": "a1",
                "video_count": 1, "sibling_count": 0}

    guard = _HandlerPatchGuard()
    try:
        guard.set_module("boto3", _fake_boto3_module(_FakeBedrockClient("{}")))
        guard.set(app, "get_db_connection", lambda: conn)
        guard.set(app, "generate_article_for_product", _fake_generate)
        result = app.handler({"product_id": "prod-1"}, None)
    finally:
        guard.restore()

    assert calls == [("prod-1", True)]
    body = json.loads(result["body"])
    assert body["results"] == [{"product_id": "prod-1", "generated": True, "article_id": "a1",
                                 "video_count": 1, "sibling_count": 0}]
    assert conn.closed is True


def test_handler_batch_mode_continues_after_one_product_errors():
    conn = _FakeConnection()

    def _fake_list(conn_):
        return ["prod-1", "prod-2"]

    def _fake_generate(conn_, bedrock, model_id, product_id, force=False):
        if product_id == "prod-1":
            raise ValueError("bad bedrock json")
        return {"product_id": product_id, "generated": True, "article_id": "a1",
                "video_count": 1, "sibling_count": 0}

    guard = _HandlerPatchGuard()
    try:
        guard.set_module("boto3", _fake_boto3_module(_FakeBedrockClient("{}")))
        guard.set(app, "get_db_connection", lambda: conn)
        guard.set(app, "list_products_needing_article", _fake_list)
        guard.set(app, "generate_article_for_product", _fake_generate)
        result = app.handler({}, None)
    finally:
        guard.restore()

    body = json.loads(result["body"])
    assert body["checked"] == 2
    assert body["generated"] == 1
    assert body["results"][0] == {"product_id": "prod-1", "generated": False, "reason": "error"}
    assert body["results"][1]["generated"] is True
    assert conn.closed is True


if __name__ == "__main__":
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for name, t in tests:
        t()
        print(f"PASS: {name}")
        passed += 1

    print(f"\n{passed}/{len(tests)} tests passed")
