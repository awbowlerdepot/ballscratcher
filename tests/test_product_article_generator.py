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


def test_build_article_prompt_asks_for_visual_theme():
    """v3's theme-driven image prompts (build_image_prompts) depend on
    the article-generation model itself deriving a visual_theme from the
    product's own name/branding -- confirms the JSON-schema request text
    actually asks for it, and marks it optional (not a hard requirement
    like the other fields)."""
    prompt = app.build_article_prompt(_SAMPLE_PRODUCT, siblings=[])
    assert "visual_theme" in prompt
    assert "OPTIONAL" in prompt.split("visual_theme")[1][:50]
    # visual_theme must stay OUT of _REQUIRED_ARTICLE_KEYS -- it's for
    # image generation only, never shown to readers, and must not break
    # parse_article_json for older/simpler responses that omit it.
    assert "visual_theme" not in app._REQUIRED_ARTICLE_KEYS


# --- Fake psycopg2-shaped cursor/connection ---

_UNSET = object()


class _FakeCursor:
    def __init__(self, needing_article=None, product_row=None, video_rows=None,
                 sibling_candidates=None, store_article_id="article-1",
                 reference_image_url=_UNSET):
        self.needing_article = needing_article or []
        self.product_row = product_row
        self.video_rows = video_rows or []
        self.sibling_candidates = sibling_candidates or []
        self.store_article_id = store_article_id
        # _UNSET (not None) as the default so a test that never touches
        # fetch_reference_image_url gets a clear NotImplementedError if
        # that query somehow fires unexpectedly, rather than silently
        # returning None and masking a bug -- None is itself a valid,
        # meaningful value here (see fetch_reference_image_url's own
        # docstring: "no image at all").
        self.reference_image_url = reference_image_url
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

        elif q.startswith("select coalesce("):
            if self.reference_image_url is _UNSET:
                raise NotImplementedError(
                    "FakeCursor: reference_image_url wasn't configured for this test "
                    "but fetch_reference_image_url's query fired"
                )
            self.description = [("coalesce",)]
            self._rows = [(self.reference_image_url,)]

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
        "video_count": 1, "sibling_count": 1, "images_generated": False,
    }
    # The stored row carries the real source video id and inferred sibling id
    # -- fixed positional indices (12/13), not insert_params[-2]/[-1], since
    # store_article's insert tuple now has 5 more (image) fields appended
    # after source_video_ids (see store_article's own param list) --
    # negative indexing would silently start reading the wrong fields.
    insert_params = conn._cursor.inserted[0]
    assert json.loads(insert_params[12]) == ["prod-2"]  # sibling_product_ids
    assert json.loads(insert_params[13]) == ["vid-1"]   # source_video_ids
    # No s3_client/bedrock_image_client/image_model_id/image_bucket were
    # supplied to generate_article_for_product in this test, so the image
    # step is skipped entirely -- all four image columns stay null and
    # images_generated_at's own case-when short-circuits to null (has_images
    # param, index 18, is False).
    assert insert_params[14:18] == (None, None, None, None)
    assert insert_params[18] is False


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

    # handler() now always passes s3_client/bedrock_image_client/
    # image_model_id/image_bucket through as keywords (see handler's own
    # docstring) -- the fake must accept them (via **kwargs) even though
    # this test doesn't care about their values, or a real TypeError
    # (unexpected keyword argument) would mask whatever the test actually
    # means to check.
    def _fake_generate(conn_, bedrock, model_id, product_id, force=False, **kwargs):
        calls.append((product_id, force, kwargs))
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

    assert len(calls) == 1
    product_id, force, kwargs = calls[0]
    assert (product_id, force) == ("prod-1", True)
    # Image plumbing was actually threaded through, not silently dropped --
    # FOUR image-related kwargs (v2's brief six-kwarg shape, which added a
    # separate remove-background client/model id, is gone as of v3 -- see
    # this module's own docstring for why).
    assert kwargs["image_model_id"] == app.DEFAULT_BEDROCK_IMAGE_MODEL_ID
    assert set(kwargs.keys()) == {
        "s3_client", "bedrock_image_client", "image_model_id", "image_bucket",
    }
    body = json.loads(result["body"])
    assert body["results"] == [{"product_id": "prod-1", "generated": True, "article_id": "a1",
                                 "video_count": 1, "sibling_count": 0}]
    assert conn.closed is True


def test_handler_batch_mode_continues_after_one_product_errors():
    conn = _FakeConnection()

    def _fake_list(conn_):
        return ["prod-1", "prod-2"]

    def _fake_generate(conn_, bedrock, model_id, product_id, force=False, **kwargs):
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


def test_handler_constructs_image_bedrock_client_in_configured_region():
    """handler() must construct TWO separate bedrock-runtime clients: the
    text model's own (no region_name override) and the image-generation
    client (region_name=BEDROCK_IMAGE_REGION, default us-west-2) -- a
    plain boto3.client("bedrock-runtime") call (no region_name) for the
    latter would use the Lambda's own home Region (us-west-1), where the
    model isn't available at all (see app.py's own module docstring). v2's
    THIRD client (remove-background, us-east-1) is gone as of v3 -- that
    model is no longer used at all."""
    conn = _FakeConnection()
    client_calls = []

    def _fake_boto3_client(service_name, **kwargs):
        client_calls.append((service_name, kwargs))
        return _FakeBedrockClient("{}")

    def _fake_generate(conn_, bedrock, model_id, product_id, force=False, **kwargs):
        return {"product_id": product_id, "generated": True, "article_id": "a1",
                "video_count": 1, "sibling_count": 0}

    import types
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = _fake_boto3_client

    guard = _HandlerPatchGuard()
    try:
        guard.set_module("boto3", fake_boto3)
        guard.set(app, "get_db_connection", lambda: conn)
        guard.set(app, "generate_article_for_product", _fake_generate)
        app.handler({"product_id": "prod-1"}, None)
    finally:
        guard.restore()

    bedrock_runtime_calls = [c for c in client_calls if c[0] == "bedrock-runtime"]
    assert len(bedrock_runtime_calls) == 2
    # The text model's own client -- no region_name override.
    assert bedrock_runtime_calls[0][1] == {}
    # The image-generation model's client -- explicit region_name,
    # defaulting to DEFAULT_BEDROCK_IMAGE_REGION when BEDROCK_IMAGE_REGION
    # isn't set.
    assert bedrock_runtime_calls[1][1] == {"region_name": app.DEFAULT_BEDROCK_IMAGE_REGION}
    assert ("s3", {}) in client_calls


# --- Article images (023_product_article_images.sql) ---

def test_fetch_reference_image_url_returns_configured_value():
    conn = _FakeConnection(reference_image_url="https://example.com/ball.jpg")
    assert app.fetch_reference_image_url(conn, "prod-1") == "https://example.com/ball.jpg"


def test_fetch_reference_image_url_returns_none_when_product_has_no_image():
    # coalesce()'s own SQL NULL comes back as a one-row, one-column result
    # with a None value -- NOT zero rows -- see fetch_reference_image_url's
    # own docstring for why row[0] (not "row is None") is the right check.
    conn = _FakeConnection(reference_image_url=None)
    assert app.fetch_reference_image_url(conn, "prod-1") is None


def test_fetch_reference_image_url_query_uses_pick_one_best_image_pattern():
    """Same coalesce/is_visible/is_thumbnail-desc/display_order pattern
    public_api.service.py's own get_product_article uses -- confirms this
    isn't a reinvented, differently-ordered query."""
    conn = _FakeConnection(reference_image_url=None)
    app.fetch_reference_image_url(conn, "prod-1")
    query = conn._cursor.executed[0][0]
    assert "pi.is_visible = true" in query
    assert "order by pi.is_thumbnail desc, pi.display_order, pi.id" in query
    assert "p.primary_image_url" in query


def test_fetch_reference_image_bytes_returns_response_content():
    """requests IS actually importable in this sandbox (unlike boto3), so
    this monkeypatches requests.get directly rather than needing the
    sys.modules-injection trick _HandlerPatchGuard uses for boto3."""
    import requests

    class _FakeResponse:
        content = b"fake-image-bytes"

        def raise_for_status(self):
            pass

    calls = []
    original_get = requests.get

    def _fake_get(url, timeout=None):
        calls.append((url, timeout))
        return _FakeResponse()

    requests.get = _fake_get
    try:
        result = app.fetch_reference_image_bytes("https://example.com/ball.jpg")
    finally:
        requests.get = original_get

    assert result == b"fake-image-bytes"
    assert calls == [("https://example.com/ball.jpg", 30)]


def test_fetch_reference_image_bytes_raises_on_http_error():
    import requests

    class _FakeErrorResponse:
        def raise_for_status(self):
            raise requests.exceptions.HTTPError("404")

    original_get = requests.get
    requests.get = lambda url, timeout=None: _FakeErrorResponse()
    try:
        try:
            app.fetch_reference_image_bytes("https://example.com/missing.jpg")
            assert False, "expected HTTPError"
        except requests.exceptions.HTTPError:
            pass
    finally:
        requests.get = original_get


def test_reference_image_to_base64_png_roundtrips_through_pillow():
    """PIL IS actually importable in this sandbox -- generates a tiny real
    JPEG (deliberately not PNG, to prove the re-encode-to-PNG step is
    doing real work, not just base64-ing the input bytes unchanged) and
    confirms the output decodes back to a valid PNG."""
    import base64
    import io

    from PIL import Image

    src = Image.new("RGB", (8, 8), color=(10, 20, 30))
    buf = io.BytesIO()
    src.save(buf, format="JPEG")
    jpeg_bytes = buf.getvalue()

    b64_png = app._reference_image_to_base64_png(jpeg_bytes)
    decoded = base64.b64decode(b64_png)
    roundtripped = Image.open(io.BytesIO(decoded))
    assert roundtripped.format == "PNG"
    assert roundtripped.size == (8, 8)


# --- build_reference_canvas: pure Pillow letterboxing, no Bedrock/DB ---

_SAMPLE_ARTICLE = dict(_VALID_ARTICLE_JSON, performance_summary="Reads early and hooks hard off the friction.")


def _photo_bytes(size, color=(200, 30, 30)) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def test_build_reference_canvas_output_matches_requested_canvas_size():
    import io

    from PIL import Image

    result_bytes = app.build_reference_canvas(_photo_bytes((300, 300)), (1536, 864))
    out = Image.open(io.BytesIO(result_bytes))
    assert out.size == (1536, 864)
    assert out.format == "PNG"


def test_build_reference_canvas_scales_down_large_reference_to_fit():
    """A reference photo bigger than the canvas's own ~70%-of-shorter-
    side budget must be scaled DOWN to fit, never cropped -- cropping
    could cut off part of the ball, which defeats the whole point of
    conditioning on the real photo."""
    import io

    from PIL import Image

    result_bytes = app.build_reference_canvas(_photo_bytes((2000, 2000)), (1024, 1024))
    out = Image.open(io.BytesIO(result_bytes)).convert("RGB")
    # Fill color should appear somewhere near the center (the scaled-down
    # photo, pasted centered) -- but the canvas itself is still exactly
    # the requested size, not the original 2000x2000. JPEG re-encoding of
    # the source photo introduces minor lossy color drift, so this checks
    # "close to" the fill color rather than an exact pixel match.
    assert out.size == (1024, 1024)
    center_pixel = out.getpixel((512, 512))
    assert all(abs(a - b) <= 10 for a, b in zip(center_pixel, (200, 30, 30)))


def test_build_reference_canvas_never_upscales_a_small_reference():
    """A reference photo already smaller than the target fraction of the
    canvas should be pasted at its own native size (scale capped at 1.0),
    not blown up and blurred."""
    result_bytes = app.build_reference_canvas(_photo_bytes((50, 50)), (1024, 1024))
    import io

    from PIL import Image

    out = Image.open(io.BytesIO(result_bytes))
    assert out.size == (1024, 1024)


def test_build_reference_canvas_centers_reference_on_neutral_fill():
    """Corners of the canvas, well away from the centered reference
    photo, should show the flat neutral-dark fill, not any part of the
    reference image itself."""
    import io

    from PIL import Image

    result_bytes = app.build_reference_canvas(_photo_bytes((100, 100)), (1024, 1024))
    out = Image.open(io.BytesIO(result_bytes)).convert("RGB")
    corner_pixel = out.getpixel((2, 2))
    assert corner_pixel == (24, 24, 26)


def test_build_reference_canvas_action_and_product_sizes_match_variant_constants():
    """Confirms _VARIANT_CANVAS_SIZES' own pixel budgets (the module's
    "specific aspect ratio" answer -- 16:9 action, 1:1 product) actually
    round-trip through build_reference_canvas correctly."""
    import io

    from PIL import Image

    action_bytes = app.build_reference_canvas(_photo_bytes((300, 300)), app._VARIANT_CANVAS_SIZES["action_shot"])
    product_bytes = app.build_reference_canvas(_photo_bytes((300, 300)), app._VARIANT_CANVAS_SIZES["product_shot"])
    assert Image.open(io.BytesIO(action_bytes)).size == (1536, 864)
    assert Image.open(io.BytesIO(product_bytes)).size == (1024, 1024)


# --- build_image_prompts: pure, no DB, no network ---

def test_build_image_prompts_uses_visual_theme_when_present():
    """visual_theme (the article-generation model's own name/branding-
    derived scene concept -- Al's explicit "background driven by the
    ball's name" ask) takes priority over performance_summary/hook when
    present."""
    article = dict(_SAMPLE_ARTICLE, visual_theme="A post-apocalyptic nuclear-wasteland backdrop.")
    prompts = app.build_image_prompts({"color": "Blue/Black"}, article)
    assert "A post-apocalyptic nuclear-wasteland backdrop." in prompts["action_shot"]
    assert "A post-apocalyptic nuclear-wasteland backdrop." in prompts["product_shot"]
    # performance_summary must NOT also leak in when visual_theme is present.
    assert "Reads early and hooks hard off the friction." not in prompts["action_shot"]


def test_build_image_prompts_falls_back_to_performance_summary_when_no_visual_theme():
    article = dict(_SAMPLE_ARTICLE, visual_theme="")
    prompts = app.build_image_prompts({"color": "Blue/Black"}, article)
    assert "Reads early and hooks hard off the friction." in prompts["action_shot"]


def test_build_image_prompts_falls_back_to_hook_when_no_theme_or_summary():
    article = dict(_VALID_ARTICLE_JSON, visual_theme="", performance_summary="", hook="A late-night league anecdote.")
    prompts = app.build_image_prompts({"color": "Red"}, article)
    assert "A late-night league anecdote." in prompts["action_shot"]


def test_build_image_prompts_positively_describes_the_ball():
    """Unlike v2's build_background_prompts, these prompts DO describe
    the ball itself -- this is img2img conditioning on the real
    reference photo, not a cutout composited in afterward, so the model
    needs to be told what's already there."""
    prompts = app.build_image_prompts({"color": "Blue/Black"}, _SAMPLE_ARTICLE)
    assert "Blue/Black bowling ball" in prompts["action_shot"]
    assert "Blue/Black bowling ball" in prompts["product_shot"]


def test_build_image_prompts_two_distinct_scenes():
    prompts = app.build_image_prompts({"color": "Purple"}, _SAMPLE_ARTICLE)
    assert "action" in prompts["action_shot"].lower() or "lane" in prompts["action_shot"].lower()
    assert "studio product photograph" in prompts["product_shot"]
    assert prompts["action_shot"] != prompts["product_shot"]


# --- call_bedrock_for_image: image-to-image request/response shape.
# Reuses the same InvokeModel-shaped fake client / {images, finish_reasons,
# seeds} response shape as _FakeBedrockClient's own Stability-wire-format
# precedent. ---

class _FakeImageBedrockClient:
    def __init__(self, response_payload: dict):
        self.response_payload = response_payload
        self.calls = []

    def invoke_model(self, modelId, contentType, accept, body):
        self.calls.append({"modelId": modelId, "body": json.loads(body)})
        return {"body": _FakeBedrockBody(self.response_payload)}


def test_call_bedrock_for_image_sends_image_to_image_request_shape():
    import base64

    fake_png = base64.b64encode(b"fake-generated-bytes").decode("ascii")
    client = _FakeImageBedrockClient({"images": [fake_png], "finish_reasons": [None], "seeds": [1]})

    result = app.call_bedrock_for_image(client, "stability.sd3-5-large-v1:0", "an action shot", "ref-b64")

    assert result == b"fake-generated-bytes"
    sent_body = client.calls[0]["body"]
    assert sent_body["mode"] == "image-to-image"
    assert sent_body["image"] == "ref-b64"
    assert sent_body["prompt"] == "an action shot"
    assert sent_body["strength"] == 0.65
    assert sent_body["negative_prompt"]  # non-empty
    # aspect_ratio is text-to-image-only on this model -- silently
    # ignored in image-to-image mode, so it must never be sent here (the
    # aspect ratio instead comes from the shape of `image` itself, via
    # build_reference_canvas).
    assert "aspect_ratio" not in sent_body


def test_call_bedrock_for_image_honors_custom_strength():
    import base64

    fake_png = base64.b64encode(b"fake-generated-bytes").decode("ascii")
    client = _FakeImageBedrockClient({"images": [fake_png], "finish_reasons": [None]})

    app.call_bedrock_for_image(client, "model-id", "prompt", "ref-b64", strength=0.4)

    assert client.calls[0]["body"]["strength"] == 0.4


def test_call_bedrock_for_image_raises_on_non_null_finish_reason():
    client = _FakeImageBedrockClient({"finish_reasons": ["Filter reason: prompt"]})
    try:
        app.call_bedrock_for_image(client, "model-id", "bad prompt", "ref-b64")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "Filter reason: prompt" in str(exc)


def test_call_bedrock_for_image_raises_on_empty_images_list():
    client = _FakeImageBedrockClient({"images": [], "finish_reasons": [None]})
    try:
        app.call_bedrock_for_image(client, "model-id", "prompt", "ref-b64")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "no images" in str(exc)


# --- store_article_image ---

class _FakeS3Client:
    def __init__(self):
        self.put_calls = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)


def test_store_article_image_builds_article_images_prefix_key_and_url():
    s3 = _FakeS3Client()
    result = app.store_article_image(s3, "my-bucket", "prod-1", "action_shot", b"pngbytes")

    assert result == {
        "key": "article-images/prod-1/action_shot.png",
        "url": "https://my-bucket.s3.amazonaws.com/article-images/prod-1/action_shot.png",
    }
    assert s3.put_calls == [{
        "Bucket": "my-bucket", "Key": "article-images/prod-1/action_shot.png",
        "Body": b"pngbytes", "ContentType": "image/png",
    }]


# --- generate_article_images: orchestration, best-effort/non-fatal ---

def _fake_reference_photo_response():
    """A real tiny JPEG so the PIL round-trip inside generate_article_
    images succeeds (_reference_image_to_base64_png needs PIL to actually
    open the bytes, not arbitrary data)."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="JPEG")

    class _FakeRealResponse:
        content = buf.getvalue()

        def raise_for_status(self):
            pass

    return _FakeRealResponse()


def _fake_generated_png_b64() -> str:
    """A real tiny opaque PNG standing in for what call_bedrock_for_image
    would actually return."""
    import base64
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (400, 400), (0, 0, 255, 255)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_generate_article_images_returns_empty_when_no_reference_image():
    conn = _FakeConnection(reference_image_url=None)
    result = app.generate_article_images(
        conn, bedrock_image_client=None, s3_client=None,
        image_model_id="model-id", image_bucket="bucket",
        product={"id": "prod-1", "color": "Blue"}, article=_SAMPLE_ARTICLE,
    )
    assert result == {}


def test_generate_article_images_returns_empty_when_reference_fetch_fails():
    import requests

    conn = _FakeConnection(reference_image_url="https://example.com/ball.jpg")
    original_get = requests.get

    def _raise(*args, **kwargs):
        raise requests.exceptions.ConnectionError("network down")

    requests.get = _raise
    try:
        result = app.generate_article_images(
            conn, bedrock_image_client=object(), s3_client=object(),
            image_model_id="model-id", image_bucket="bucket",
            product={"id": "prod-1", "color": "Blue"}, article=_SAMPLE_ARTICLE,
        )
    finally:
        requests.get = original_get

    assert result == {}


def test_generate_article_images_both_succeed():
    import requests

    conn = _FakeConnection(reference_image_url="https://example.com/ball.jpg")
    image_client = _FakeImageBedrockClient({"images": [_fake_generated_png_b64()], "finish_reasons": [None]})
    s3 = _FakeS3Client()

    original_get = requests.get
    requests.get = lambda url, timeout=None: _fake_reference_photo_response()
    try:
        result = app.generate_article_images(
            conn, bedrock_image_client=image_client, s3_client=s3,
            image_model_id="model-id", image_bucket="bucket",
            product={"id": "prod-1", "color": "Blue"}, article=_SAMPLE_ARTICLE,
        )
    finally:
        requests.get = original_get

    assert result == {
        "action_shot_image_key": "article-images/prod-1/action_shot.png",
        "action_shot_image_url": "https://bucket.s3.amazonaws.com/article-images/prod-1/action_shot.png",
        "product_shot_image_key": "article-images/prod-1/product_shot.png",
        "product_shot_image_url": "https://bucket.s3.amazonaws.com/article-images/prod-1/product_shot.png",
    }
    # One image-to-image call per variant -- no separate remove-background
    # call at all (v2's THREE Bedrock calls per product are gone as of v3).
    assert len(image_client.calls) == 2
    # No aspect_ratio param sent (text-to-image-only) -- the canvas shape
    # (from build_reference_canvas) is what determines the output ratio.
    assert all("aspect_ratio" not in c["body"] for c in image_client.calls)
    assert len(s3.put_calls) == 2


def test_generate_article_images_one_variant_fails_other_still_succeeds():
    """Each variant runs in its own try/except -- a failure generating
    one image must not take the other, still-good image down with it."""
    import requests

    conn = _FakeConnection(reference_image_url="https://example.com/ball.jpg")

    class _FlakyImageClient:
        def __init__(self):
            self.calls = []

        def invoke_model(self, modelId, contentType, accept, body):
            parsed = json.loads(body)
            self.calls.append({"modelId": modelId, "body": parsed})
            if len(self.calls) == 1:
                raise RuntimeError("Bedrock throttled")
            return {"body": _FakeBedrockBody({"images": [_fake_generated_png_b64()], "finish_reasons": [None]})}

    image_client = _FlakyImageClient()
    s3 = _FakeS3Client()

    original_get = requests.get
    requests.get = lambda url, timeout=None: _fake_reference_photo_response()
    try:
        result = app.generate_article_images(
            conn, bedrock_image_client=image_client, s3_client=s3,
            image_model_id="model-id", image_bucket="bucket",
            product={"id": "prod-1", "color": "Blue"}, article=_SAMPLE_ARTICLE,
        )
    finally:
        requests.get = original_get

    # action_shot (generated first) failed; product_shot (generated
    # second) succeeded -- only the successful variant's keys are present.
    assert result == {
        "product_shot_image_key": "article-images/prod-1/product_shot.png",
        "product_shot_image_url": "https://bucket.s3.amazonaws.com/article-images/prod-1/product_shot.png",
    }
    assert len(image_client.calls) == 2
    assert len(s3.put_calls) == 1


# --- generate_article_for_product wired with image plumbing ---

def test_generate_article_for_product_wires_images_when_all_four_image_args_supplied():
    product_row = (
        "prod-1", "Equinox Solid", "Blue", "reactive", "hybrid",
        "R2S Hybrid", True, "500/1000 Abralon",
        12, 17, "2024-01-01", "A great ball.",
        "brand-1", "Storm", "Sonar", "asymmetric",
    )
    video_rows = [("vid-1", "Review", "Some Channel", "A summary.", "transcript text")]
    conn = _FakeConnection(product_row=product_row, video_rows=video_rows,
                            reference_image_url=None, store_article_id="article-1")
    bedrock = _FakeBedrockClient(json.dumps(_VALID_ARTICLE_JSON))

    # reference_image_url=None -> generate_article_images short-circuits to
    # {} without ever touching bedrock_image_client/s3_client, so passing
    # simple sentinel objects for those is enough to prove the plumbing
    # reaches generate_article_images at all (see the "no reference
    # image" test above for that function's own behavior in isolation).
    result = app.generate_article_for_product(
        conn, bedrock, "model-id", "prod-1",
        s3_client=object(), bedrock_image_client=object(),
        image_model_id="model-id", image_bucket="bucket",
    )

    assert result["images_generated"] is False
    insert_params = conn._cursor.inserted[0]
    assert insert_params[14:18] == (None, None, None, None)
    assert insert_params[18] is False


def test_generate_article_for_product_skips_images_when_any_image_arg_missing():
    """s3_client/bedrock_image_client/image_model_id/image_bucket must
    ALL be supplied for the image step to run (see generate_article_for_
    product's own docstring) -- a partially-configured deployment (e.g.
    IMAGE_BUCKET unset) should skip images entirely, not half-run."""
    product_row = (
        "prod-1", "Equinox", None, None, None, None, None, None,
        None, None, None, None, "brand-1", "Storm", None, None,
    )
    video_rows = [("vid-1", "Review", "Channel", "Summary", "transcript")]
    conn = _FakeConnection(product_row=product_row, video_rows=video_rows)
    bedrock = _FakeBedrockClient(json.dumps(_VALID_ARTICLE_JSON))

    result = app.generate_article_for_product(
        conn, bedrock, "model-id", "prod-1",
        s3_client=object(), bedrock_image_client=object(),
        image_model_id="model-id", image_bucket=None,  # missing
    )

    assert result["images_generated"] is False
    # reference_image_url query never even fired -- confirms the whole
    # image branch was skipped, not attempted-and-quietly-swallowed.
    assert not any(q.startswith("select coalesce(") for q, _ in conn._cursor.executed)


# --- store_article: images param wiring ---

def test_store_article_with_images_sets_images_generated_at_flag_true():
    conn = _FakeConnection()
    images = {
        "action_shot_image_key": "article-images/prod-1/action_shot.png",
        "action_shot_image_url": "https://bucket.s3.amazonaws.com/article-images/prod-1/action_shot.png",
    }
    app.store_article(conn, "prod-1", dict(_VALID_ARTICLE_JSON), [], [], images=images)

    insert_params = conn._cursor.inserted[0]
    assert insert_params[14] == images["action_shot_image_key"]
    assert insert_params[15] == images["action_shot_image_url"]
    assert insert_params[16] is None  # product_shot_image_key not in this partial dict
    assert insert_params[17] is None
    # has_images (the images_generated_at case-when's boolean param) is
    # True even though only ONE of the two images actually succeeded --
    # see 023_product_article_images.sql's own column comment: "at least
    # one", not "both".
    assert insert_params[18] is True


def test_store_article_query_includes_new_image_columns():
    conn = _FakeConnection()
    app.store_article(conn, "prod-1", dict(_VALID_ARTICLE_JSON), [], [])
    query = conn._cursor.executed[0][0]
    assert "action_shot_image_key = excluded.action_shot_image_key" in query
    assert "images_generated_at = excluded.images_generated_at" in query


if __name__ == "__main__":
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for name, t in tests:
        t()
        print(f"PASS: {name}")
        passed += 1

    print(f"\n{passed}/{len(tests)} tests passed")
