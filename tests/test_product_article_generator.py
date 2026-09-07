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
    """Every image prompt builder in the v1-v4 history (_resolve_visual_
    context as of v4; build_image_prompts in v3) depends on the article-
    generation model itself deriving a visual_theme from the product's
    own name/branding -- confirms the JSON-schema request text actually
    asks for it, and marks it optional (not a hard requirement like the
    other fields)."""
    prompt = app.build_article_prompt(_SAMPLE_PRODUCT, siblings=[])
    assert "visual_theme" in prompt
    assert "OPTIONAL" in prompt.split("visual_theme")[1][:50]
    # visual_theme must stay OUT of _REQUIRED_ARTICLE_KEYS -- it's for
    # image generation only, never shown to readers, and must not break
    # parse_article_json for older/simpler responses that omit it.
    assert "visual_theme" not in app._REQUIRED_ARTICLE_KEYS


def test_build_article_prompt_warns_against_borrowing_a_different_editions_name():
    """Real, confirmed incident (Al, 2026-09-06): a non-Pearl product's
    article came back naming/describing the Pearl edition, traced to a
    Pearl review video that had been (wrongly) approved onto the
    non-pearl product -- see video_discovery's score_match fix for the
    matching-side half of this. This is the prompt-level defense-in-
    depth half: even for a correctly-matched video (e.g. one that
    compares editions), the model shouldn't blend a different edition's
    name/claims into this article."""
    prompt = app.build_article_prompt(_SAMPLE_PRODUCT, siblings=[])
    assert "different edition" in prompt.lower()
    assert "pearl" in prompt.lower()
    assert "solid" in prompt.lower()


# --- Fake psycopg2-shaped cursor/connection ---

_UNSET = object()


class _FakeCursor:
    def __init__(self, needing_article=None, product_row=None, video_rows=None,
                 sibling_candidates=None, store_article_id="article-1",
                 reference_image_url=_UNSET, existing_article=None,
                 selected_variants=None):
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
        # v7 decoupled-regenerate feature -- {"id", "performance_summary",
        # "hook", and (as of the 2026-09-06 image-lock fix) the four flat
        # image key/url fields too} dict backing fetch_existing_article,
        # and also what update_article_text_only/update_article_images_
        # only's own UPDATE...returning fetchone() resolves against (None
        # simulates "no existing article row for this product", i.e. the
        # ValueError/early-return paths those functions and generate_
        # article_for_product itself are guarded against).
        self.existing_article = existing_article
        # 2026-09-06 image-lock fix -- which variants (a list/set of
        # "action_shot"/"product_shot" strings) already have an is_
        # selected=true candidate row for `existing_article`'s id, backing
        # the "select variant ... where is_selected" query generate_
        # article_for_product runs to compute locked_variants. Only
        # meaningful together with existing_article being set.
        self.selected_variants = selected_variants or []
        self.executed = []
        self.description = None
        self._rows = []
        self.inserted = []  # (product_id, article dict-shaped params) from store_article
        # v4 (026_product_article_image_candidates.sql) -- params tuples
        # from every store_article_image_candidates insert, one per
        # candidate row (article_id, variant, model_id, image_key,
        # image_url, seed, is_selected).
        self.candidate_inserts = []
        # v5 regenerate-safety fix (2026-09-04 real incident) -- params
        # tuples (article_id, variant) from every store_article_image_
        # candidates delete, one per variant that got NEW candidates this
        # run (see that function's own docstring for why a variant with
        # no new candidates is skipped entirely, delete included).
        self.candidate_deletes = []
        # v7 -- params tuples from every update_article_text_only /
        # update_article_images_only UPDATE, respectively.
        self.text_only_updates = []
        self.images_only_updates = []

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

        elif q.startswith("insert into product_article_image_candidates"):
            self.candidate_inserts.append(params)

        elif q.startswith("delete from product_article_image_candidates"):
            self.candidate_deletes.append(params)

        elif q.startswith("select id, performance_summary, hook, visual_theme,"):
            self.description = [
                ("id",), ("performance_summary",), ("hook",), ("visual_theme",),
                ("action_shot_image_key",), ("action_shot_image_url",),
                ("product_shot_image_key",), ("product_shot_image_url",),
            ]
            if self.existing_article is None:
                self._rows = []
            else:
                self._rows = [(
                    self.existing_article["id"],
                    self.existing_article.get("performance_summary"),
                    self.existing_article.get("hook"),
                    self.existing_article.get("visual_theme"),
                    self.existing_article.get("action_shot_image_key"),
                    self.existing_article.get("action_shot_image_url"),
                    self.existing_article.get("product_shot_image_key"),
                    self.existing_article.get("product_shot_image_url"),
                )]

        elif q.startswith("select variant from product_article_image_candidates where article_id"):
            self.description = [("variant",)]
            self._rows = [(v,) for v in self.selected_variants]

        elif q.startswith("update product_articles set status = 'pending', title ="):
            self.text_only_updates.append(params)
            self.description = [("id",)]
            self._rows = [(self.existing_article["id"],)] if self.existing_article else []

        elif q.startswith("update product_articles set action_shot_image_key ="):
            self.images_only_updates.append(params)
            self.description = [("id",)]
            self._rows = [(self.existing_article["id"],)] if self.existing_article else []

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


def test_list_products_needing_article_no_limit_when_max_products_omitted():
    """Default/uncapped call (max_products=None) must not append a LIMIT
    clause at all -- see "INVOCATION CAP" in app.py's own module
    docstring for why a cap exists elsewhere, but this function itself
    stays usable uncapped rather than requiring every caller to pass a
    sentinel like 0 or -1."""
    conn = _FakeConnection(needing_article=[])
    app.list_products_needing_article(conn)
    executed_query, executed_params = conn._cursor.executed[0]
    assert "limit" not in executed_query
    assert executed_params == ()


def test_list_products_needing_article_applies_limit_when_max_products_given():
    """See "INVOCATION CAP" in app.py's own module docstring -- a plain
    SQL LIMIT on top of the existing `order by p.id`, so a capped run is
    deterministic (same lowest-id products every time) rather than an
    arbitrary subset."""
    conn = _FakeConnection(needing_article=["prod-1", "prod-2"])
    result = app.list_products_needing_article(conn, max_products=10)
    executed_query, executed_params = conn._cursor.executed[0]
    assert executed_query.rstrip().endswith("limit %s")
    assert executed_params == [10]
    # The fake cursor doesn't actually enforce the LIMIT (no real
    # Postgres here -- see test_list_products_needing_article_query_
    # excludes_blocked_channels_and_existing_articles's own comment), so
    # this only confirms the query/params shape, not real row-limiting.
    assert result == ["prod-1", "prod-2"]


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


def test_store_article_persists_visual_theme():
    """Migration 029 (2026-09-06). store_article is always called with a
    FRESH article dict (brand-new generation, or the combined regenerate_
    text+regenerate_images path), so the theme it persists is always the
    model's own current-run derivation."""
    conn = _FakeConnection()
    article = dict(_VALID_ARTICLE_JSON, visual_theme="A post-apocalyptic wasteland.")
    app.store_article(conn, "prod-1", article, [], [])
    query = conn._cursor.executed[0][0]
    assert "visual_theme = excluded.visual_theme" in query
    params = conn._cursor.inserted[0]
    assert "A post-apocalyptic wasteland." in params


def test_store_article_persists_none_when_visual_theme_omitted():
    """visual_theme is OPTIONAL (_REQUIRED_ARTICLE_KEYS doesn't include
    it) -- an older/simpler model response that omits it entirely must
    not raise a KeyError, just persist None."""
    conn = _FakeConnection()
    app.store_article(conn, "prod-1", dict(_VALID_ARTICLE_JSON), [], [])
    params = conn._cursor.inserted[0]
    assert None in params  # doesn't raise; visual_theme param is present as None


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
    # store_article's insert tuple now has 6 more (visual_theme + image)
    # fields appended after source_video_ids (see store_article's own param
    # list) -- negative indexing would silently start reading the wrong
    # fields.
    insert_params = conn._cursor.inserted[0]
    assert json.loads(insert_params[12]) == ["prod-2"]  # sibling_product_ids
    assert json.loads(insert_params[13]) == ["vid-1"]   # source_video_ids
    # No s3_client/bedrock_image_client/image_model_id/image_bucket were
    # supplied to generate_article_for_product in this test, so the image
    # step is skipped entirely -- all four image columns stay null and
    # images_generated_at's own case-when short-circuits to null (has_images
    # param, index 19, is False). Index 14 is visual_theme -- absent from
    # _VALID_ARTICLE_JSON, so None.
    assert insert_params[15:19] == (None, None, None, None)
    assert insert_params[19] is False


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


# --- v7 decoupled regenerate: fetch_existing_article / update_article_text_only /
# update_article_images_only, plus generate_article_for_product's new
# regenerate_text/regenerate_images branches ---

def test_fetch_existing_article_returns_id_and_text_fields():
    conn = _FakeConnection(existing_article={"id": "article-1", "performance_summary": "Great ball.", "hook": "A hook."})
    result = app.fetch_existing_article(conn, "prod-1")
    # 2026-09-06 image-lock fix widened this query/return shape to include
    # the four flat image columns too (see fetch_existing_article's own
    # docstring) -- a fixture that never set them comes back None, same as
    # a real row with no images generated yet. The 2026-09-06 visual_theme
    # persistence fix (migration 029) widened it again -- same None-when-
    # unset story for an article predating that migration.
    assert result == {
        "id": "article-1", "performance_summary": "Great ball.", "hook": "A hook.", "visual_theme": None,
        "action_shot_image_key": None, "action_shot_image_url": None,
        "product_shot_image_key": None, "product_shot_image_url": None,
    }


def test_fetch_existing_article_returns_image_fields_when_present():
    conn = _FakeConnection(existing_article={
        "id": "article-1", "performance_summary": "Great ball.", "hook": "A hook.",
        "action_shot_image_key": "article-images/prod-1/action_shot_gemini_1.png",
        "action_shot_image_url": "https://b/action_shot_gemini_1.png",
        "product_shot_image_key": "article-images/prod-1/product_shot_gemini_1.png",
        "product_shot_image_url": "https://b/product_shot_gemini_1.png",
    })
    result = app.fetch_existing_article(conn, "prod-1")
    assert result["action_shot_image_key"] == "article-images/prod-1/action_shot_gemini_1.png"
    assert result["product_shot_image_url"] == "https://b/product_shot_gemini_1.png"


def test_fetch_existing_article_returns_visual_theme_when_present():
    """The core regression test for the 2026-09-06 "images drifted away
    from matching the ball's name" fix (migration 029) -- confirms the
    persisted theme actually comes back, not just the pre-existing text/
    image fields."""
    conn = _FakeConnection(existing_article={
        "id": "article-1", "performance_summary": "Great ball.", "hook": "A hook.",
        "visual_theme": "A post-apocalyptic nuclear-wasteland backdrop.",
    })
    result = app.fetch_existing_article(conn, "prod-1")
    assert result["visual_theme"] == "A post-apocalyptic nuclear-wasteland backdrop."


def test_fetch_existing_article_returns_none_when_no_row():
    conn = _FakeConnection(existing_article=None)
    assert app.fetch_existing_article(conn, "prod-1") is None


def test_update_article_text_only_sets_text_columns_and_resets_review_state():
    conn = _FakeConnection(existing_article={"id": "article-1", "performance_summary": None, "hook": None})
    article_id = app.update_article_text_only(conn, "prod-1", dict(_VALID_ARTICLE_JSON), ["vid-1"], ["prod-2"])
    assert article_id == "article-1"
    assert conn.commits == 1
    assert len(conn._cursor.text_only_updates) == 1
    query = conn._cursor.executed[-1][0]
    params = conn._cursor.text_only_updates[0]
    assert "status = 'pending'" in query
    assert "reviewed_at = null" in query
    assert "resolved_by = null" in query
    # No image columns at all in this UPDATE's SET clause -- that's the
    # whole point of a text-only regenerate (an admin's already-picked
    # images must survive it untouched).
    assert "action_shot_image_key" not in query
    assert "images_generated_at" not in query
    assert params[0] == _VALID_ARTICLE_JSON["title"]


def test_update_article_text_only_raises_when_no_existing_article():
    conn = _FakeConnection(existing_article=None)
    try:
        app.update_article_text_only(conn, "prod-1", dict(_VALID_ARTICLE_JSON), [], [])
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_update_article_text_only_persists_fresh_visual_theme():
    """Migration 029 (2026-09-06, Al's "images drifted away from matching
    the ball's name" report): a text regenerate re-derives its own fresh
    theme from the current article draft, so the persisted value must
    move forward with it, same as every other text field this UPDATE
    sets."""
    conn = _FakeConnection(existing_article={"id": "article-1", "performance_summary": None, "hook": None})
    article = dict(_VALID_ARTICLE_JSON, visual_theme="A neon-lit sci-fi corridor.")
    app.update_article_text_only(conn, "prod-1", article, [], [])
    query = conn._cursor.executed[-1][0]
    assert "visual_theme = %s" in query
    params = conn._cursor.text_only_updates[0]
    assert "A neon-lit sci-fi corridor." in params


def test_update_article_images_only_sets_only_image_columns():
    conn = _FakeConnection(existing_article={"id": "article-1", "performance_summary": "x", "hook": "y"})
    images = {
        "action_shot_image_key": "k1", "action_shot_image_url": "u1",
        "product_shot_image_key": "k2", "product_shot_image_url": "u2",
    }
    article_id = app.update_article_images_only(conn, "prod-1", images)
    assert article_id == "article-1"
    assert conn.commits == 1
    query, params = conn._cursor.executed[-1]
    # No status/reviewed_at/resolved_by/text columns at all -- per select_
    # article_image_candidate's own precedent, changing images shouldn't
    # gate on or reset the separate text-review workflow. Also no visual_
    # theme (migration 029) -- an images-only regenerate has no fresh
    # theme of its own to write; it only ever READS the persisted one via
    # fetch_existing_article, never rewrites it here.
    assert "status" not in query
    assert "reviewed_at" not in query
    assert "resolved_by" not in query
    assert "title" not in query
    assert "visual_theme" not in query
    assert params == ("k1", "u1", "k2", "u2", "prod-1")


def test_update_article_images_only_raises_when_no_existing_article():
    conn = _FakeConnection(existing_article=None)
    try:
        app.update_article_images_only(conn, "prod-1", {})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_generate_article_for_product_nothing_to_regenerate_when_both_flags_false():
    conn = _FakeConnection()
    bedrock = _FakeBedrockClient("should not be called")
    result = app.generate_article_for_product(conn, bedrock, "model-id", "prod-1",
                                               regenerate_text=False, regenerate_images=False)
    assert result == {"product_id": "prod-1", "generated": False, "reason": "nothing_to_regenerate"}
    assert bedrock.calls == []


def test_generate_article_for_product_text_only_calls_bedrock_and_leaves_images_alone():
    """regenerate_text=True, regenerate_images=False ('Regenerate text'):
    Bedrock is still called (the whole point is fresh text), but the
    write goes through update_article_text_only, not store_article --
    the combined insert path must not fire at all."""
    product_row = (
        "prod-1", "Equinox Solid", "Blue", "reactive", "hybrid",
        "R2S Hybrid", True, "500/1000 Abralon",
        12, 17, "2024-01-01", "A great ball.",
        "brand-1", "Storm", "Sonar", "asymmetric",
    )
    video_rows = [("vid-1", "Review", "Some Channel", "A summary.", "transcript text")]
    conn = _FakeConnection(product_row=product_row, video_rows=video_rows,
                            existing_article={"id": "article-1", "performance_summary": None, "hook": None})
    bedrock = _FakeBedrockClient(json.dumps(_VALID_ARTICLE_JSON))

    result = app.generate_article_for_product(conn, bedrock, "model-id", "prod-1",
                                               regenerate_text=True, regenerate_images=False)

    assert result["generated"] is True
    assert result["article_id"] == "article-1"
    assert len(bedrock.calls) == 1
    assert conn._cursor.inserted == []  # store_article's insert path never fired
    assert len(conn._cursor.text_only_updates) == 1


def test_generate_article_for_product_images_only_skips_bedrock_and_reuses_existing_text():
    """regenerate_text=False, regenerate_images=True ('Regenerate
    images'): no Bedrock call at all -- the existing row's performance_
    summary/hook are reused as prompt context. This fixture's existing_
    article has no visual_theme set, so this exercises the genuinely
    theme-less fallback case (an article predating migration 029, or one
    whose model response omitted the optional field) -- see the
    dedicated test below for the case where a persisted theme EXISTS and
    must be reused instead of this fallback."""
    product_row = (
        "prod-1", "Equinox Solid", "Blue", "reactive", "hybrid",
        "R2S Hybrid", True, "500/1000 Abralon",
        12, 17, "2024-01-01", "A great ball.",
        "brand-1", "Storm", "Sonar", "asymmetric",
    )
    video_rows = [("vid-1", "Review", "Some Channel", "A summary.", "transcript text")]
    conn = _FakeConnection(product_row=product_row, video_rows=video_rows,
                            existing_article={"id": "article-1", "performance_summary": "Great ball.", "hook": "A hook."})
    bedrock = _FakeBedrockClient("should not be called")

    # No s3_client/image_model_id/etc supplied, so the image-generation
    # step itself is skipped (same "all eight or none" gate as before) --
    # this test only checks the text-skip/existing-row-reuse half.
    result = app.generate_article_for_product(conn, bedrock, "model-id", "prod-1",
                                               regenerate_text=False, regenerate_images=True)

    assert result == {
        "product_id": "prod-1", "generated": True, "article_id": "article-1",
        "video_count": 1, "sibling_count": 0, "images_generated": False,
    }
    assert bedrock.calls == []
    assert conn._cursor.inserted == []
    # No images were actually produced (image plumbing wasn't supplied),
    # so update_article_images_only is never even called -- nothing to
    # write. See the "wires_images_when_all_eight_image_args_supplied"
    # tests further down for the full image-pipeline path.
    assert conn._cursor.images_only_updates == []


def test_generate_article_for_product_images_only_regenerate_reuses_persisted_visual_theme():
    """The core regression test for Al's 2026-09-06 "images drifted away
    from matching the ball's name" report, and migration 029's fix.
    Before 029, an images-only regenerate had no visual_theme to reuse at
    all -- it always fell back to performance_summary/hook (a generic
    review-narrative string), quietly downgrading the scene prompt's
    specificity on every single "Regenerate images" click. Confirms that
    when the existing article HAS a persisted theme, that's what actually
    reaches generate_article_image_candidates as the `article` dict's
    visual_theme -- not the performance_summary/hook fallback -- by
    patching generate_article_image_candidates to capture its own
    `article` argument (same technique as the lock-down tests' fake
    image-candidates helper, just capturing input instead of faking
    output)."""
    product_row = (
        "prod-1", "Equinox Solid", "Blue", "reactive", "hybrid",
        "R2S Hybrid", True, "500/1000 Abralon",
        12, 17, "2024-01-01", "A great ball.",
        "brand-1", "Storm", "Sonar", "asymmetric",
    )
    video_rows = [("vid-1", "Review", "Some Channel", "A summary.", "transcript text")]
    conn = _FakeConnection(
        product_row=product_row, video_rows=video_rows,
        existing_article={
            "id": "article-1",
            "performance_summary": "Reads early and hooks hard off the friction.",
            "hook": "A late-night league anecdote.",
            "visual_theme": "A post-apocalyptic nuclear-wasteland backdrop.",
        },
    )
    bedrock = _FakeBedrockClient("should not be called")
    captured_articles = []

    def _fake_generate_image_candidates(conn_, bedrock_image_client, bedrock_removebg_client, gemini_auth,
                                         s3_client, image_model_id, removebg_model_id, gemini_model_id,
                                         image_bucket, product, article):
        captured_articles.append(article)
        return {}

    guard = _HandlerPatchGuard()
    try:
        guard.set(app, "generate_article_image_candidates", _fake_generate_image_candidates)
        app.generate_article_for_product(conn, bedrock, "model-id", "prod-1",
                                          regenerate_text=False, regenerate_images=True,
                                          **_ALL_EIGHT_IMAGE_KWARGS)
    finally:
        guard.restore()

    assert len(captured_articles) == 1
    assert captured_articles[0]["visual_theme"] == "A post-apocalyptic nuclear-wasteland backdrop."
    # And _resolve_visual_context itself actually picks the theme over
    # the fallback text, end to end -- not just that the key is present.
    assert app._resolve_visual_context(captured_articles[0]) == "A post-apocalyptic nuclear-wasteland backdrop."


def test_generate_article_for_product_images_only_reports_reason_when_no_existing_article():
    product_row = (
        "prod-1", "Equinox", None, None, None, None, None, None,
        None, None, None, None, "brand-1", "Storm", None, None,
    )
    video_rows = [("vid-1", "Review", "Channel", "Summary", "transcript")]
    conn = _FakeConnection(product_row=product_row, video_rows=video_rows, existing_article=None)
    bedrock = _FakeBedrockClient("should not be called")

    result = app.generate_article_for_product(conn, bedrock, "model-id", "prod-1",
                                               regenerate_text=False, regenerate_images=True)

    assert result == {"product_id": "prod-1", "generated": False,
                       "reason": "no_existing_article_to_regenerate"}
    assert bedrock.calls == []


def test_generate_article_for_product_text_only_reports_reason_when_no_existing_article():
    """Symmetric guard for the text-only branch -- generate_article_for_
    product fetches the existing row up front for EITHER decoupled
    branch, so a missing article is caught before ever calling Bedrock,
    not surfaced as a confusing ValueError from update_article_text_
    only's own UPDATE...returning check."""
    product_row = (
        "prod-1", "Equinox", None, None, None, None, None, None,
        None, None, None, None, "brand-1", "Storm", None, None,
    )
    video_rows = [("vid-1", "Review", "Channel", "Summary", "transcript")]
    conn = _FakeConnection(product_row=product_row, video_rows=video_rows, existing_article=None)
    bedrock = _FakeBedrockClient("should not be called")

    result = app.generate_article_for_product(conn, bedrock, "model-id", "prod-1",
                                               regenerate_text=True, regenerate_images=False)

    assert result == {"product_id": "prod-1", "generated": False,
                       "reason": "no_existing_article_to_regenerate"}
    assert bedrock.calls == []


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
    # bedrock_removebg_client/gemini_auth/image_model_id/
    # removebg_model_id/gemini_model_id/image_bucket through as keywords
    # (see handler's own docstring) -- the fake must accept them (via
    # **kwargs) even though this test doesn't care about their values, or
    # a real TypeError (unexpected keyword argument) would mask whatever
    # the test actually means to check.
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
    # EIGHT image-related kwargs as of v4/v5 (three Bedrock clients +
    # Gemini auth bundle/model id -- see generate_article_for_product's
    # own docstring for the full v1-v5 shape history).
    assert kwargs["image_model_id"] == app.DEFAULT_BEDROCK_IMAGE_MODEL_ID
    assert kwargs["removebg_model_id"] == app.DEFAULT_BEDROCK_REMOVE_BG_MODEL_ID
    assert kwargs["gemini_model_id"] == app.DEFAULT_GEMINI_IMAGE_MODEL_ID
    # GEMINI_SERVICE_ACCOUNT_SECRET_ARN isn't set in this test's
    # environment, so handler() never even attempts the Secrets Manager
    # fetch/token mint -- see test_handler_mints_gemini_access_token_from_
    # service_account_secret_when_configured below for the "configured"
    # path.
    assert kwargs["gemini_auth"] is None
    # v7: regenerate_text/regenerate_images are always threaded through on
    # the on-demand path now too, defaulting to True/True when the event
    # doesn't specify them -- see test_handler_on_demand_product_id_reads_
    # regenerate_flags_from_event below for the non-default case.
    assert kwargs["regenerate_text"] is True
    assert kwargs["regenerate_images"] is True
    assert set(kwargs.keys()) == {
        "s3_client", "bedrock_image_client", "bedrock_removebg_client", "gemini_auth",
        "image_model_id", "removebg_model_id", "gemini_model_id", "image_bucket",
        "regenerate_text", "regenerate_images",
    }
    body = json.loads(result["body"])
    assert body["results"] == [{"product_id": "prod-1", "generated": True, "article_id": "a1",
                                 "video_count": 1, "sibling_count": 0}]
    assert conn.closed is True


def test_handler_on_demand_product_id_reads_regenerate_flags_from_event():
    """v7 (2026-09-05): {"product_id": ..., "regenerate_text": False,
    "regenerate_images": True} -- the decoupled "Regenerate images"
    admin-site trigger -- must reach generate_article_for_product exactly
    as specified, not silently coerced back to True/True."""
    conn = _FakeConnection()
    calls = []

    def _fake_generate(conn_, bedrock, model_id, product_id, force=False, **kwargs):
        calls.append((product_id, force, kwargs))
        return {"product_id": product_id, "generated": True, "article_id": "a1",
                "video_count": 1, "sibling_count": 0}

    guard = _HandlerPatchGuard()
    try:
        guard.set_module("boto3", _fake_boto3_module(_FakeBedrockClient("{}")))
        guard.set(app, "get_db_connection", lambda: conn)
        guard.set(app, "generate_article_for_product", _fake_generate)
        app.handler({"product_id": "prod-1", "regenerate_text": False, "regenerate_images": True}, None)
    finally:
        guard.restore()

    assert len(calls) == 1
    _, _, kwargs = calls[0]
    assert kwargs["regenerate_text"] is False
    assert kwargs["regenerate_images"] is True


def test_handler_batch_mode_continues_after_one_product_errors():
    conn = _FakeConnection()

    def _fake_list(conn_, max_products=None):
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


def test_handler_batch_mode_defaults_cap_to_DEFAULT_MAX_PRODUCTS_PER_INVOCATION():
    """See "INVOCATION CAP" in app.py's own module docstring -- with no
    MAX_PRODUCTS_PER_INVOCATION env var set and no event["limit"], the
    scheduled {}/{"batch": true} path must still cap at the module's own
    default rather than falling back to uncapped (max_products=None),
    which is what this whole feature exists to prevent."""
    conn = _FakeConnection()
    captured = {}

    def _fake_list(conn_, max_products=None):
        captured["max_products"] = max_products
        return []

    guard = _HandlerPatchGuard()
    try:
        guard.set_module("boto3", _fake_boto3_module(_FakeBedrockClient("{}")))
        guard.set(app, "get_db_connection", lambda: conn)
        guard.set(app, "list_products_needing_article", _fake_list)
        os.environ.pop("MAX_PRODUCTS_PER_INVOCATION", None)
        app.handler({}, None)
    finally:
        guard.restore()

    assert captured["max_products"] == app.DEFAULT_MAX_PRODUCTS_PER_INVOCATION


def test_handler_batch_mode_reads_cap_from_env_var():
    conn = _FakeConnection()
    captured = {}

    def _fake_list(conn_, max_products=None):
        captured["max_products"] = max_products
        return []

    guard = _HandlerPatchGuard()
    try:
        guard.set_module("boto3", _fake_boto3_module(_FakeBedrockClient("{}")))
        guard.set(app, "get_db_connection", lambda: conn)
        guard.set(app, "list_products_needing_article", _fake_list)
        os.environ["MAX_PRODUCTS_PER_INVOCATION"] = "25"
        app.handler({"batch": True}, None)
    finally:
        os.environ.pop("MAX_PRODUCTS_PER_INVOCATION", None)
        guard.restore()

    assert captured["max_products"] == 25


def test_handler_batch_mode_event_limit_overrides_env_var():
    """A manual {"batch": true, "limit": N} invoke -- see handler's own
    v8 docstring -- is the deliberate-backlog-catch-up override, and must
    win over MAX_PRODUCTS_PER_INVOCATION rather than being ignored."""
    conn = _FakeConnection()
    captured = {}

    def _fake_list(conn_, max_products=None):
        captured["max_products"] = max_products
        return []

    guard = _HandlerPatchGuard()
    try:
        guard.set_module("boto3", _fake_boto3_module(_FakeBedrockClient("{}")))
        guard.set(app, "get_db_connection", lambda: conn)
        guard.set(app, "list_products_needing_article", _fake_list)
        os.environ["MAX_PRODUCTS_PER_INVOCATION"] = "10"
        app.handler({"batch": True, "limit": 50}, None)
    finally:
        os.environ.pop("MAX_PRODUCTS_PER_INVOCATION", None)
        guard.restore()

    assert captured["max_products"] == 50


def test_handler_constructs_image_and_removebg_bedrock_clients_in_configured_regions():
    """handler() must construct THREE separate bedrock-runtime clients:
    the text model's own (no region_name override), the background-
    generation client (region_name=BEDROCK_IMAGE_REGION, default
    us-west-2), and the remove-background client (region_name=
    BEDROCK_REMOVEBG_REGION, default us-east-1) -- a plain boto3.client(
    "bedrock-runtime") call (no region_name) for either of the latter two
    would use the Lambda's own home Region (us-west-1), where neither
    model is available at all (see app.py's own module docstring). v3
    briefly dropped the remove-background client entirely; v4 revives it
    (see BedrockRemoveBgModelId's own template.yaml parameter description
    for that saga)."""
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
    assert len(bedrock_runtime_calls) == 3
    # The text model's own client -- no region_name override.
    assert bedrock_runtime_calls[0][1] == {}
    # The background-generation model's client -- explicit region_name,
    # defaulting to DEFAULT_BEDROCK_IMAGE_REGION when BEDROCK_IMAGE_REGION
    # isn't set.
    assert bedrock_runtime_calls[1][1] == {"region_name": app.DEFAULT_BEDROCK_IMAGE_REGION}
    # The remove-background model's client -- a THIRD, separate Region,
    # defaulting to DEFAULT_BEDROCK_REMOVE_BG_REGION when
    # BEDROCK_REMOVEBG_REGION isn't set.
    assert bedrock_runtime_calls[2][1] == {"region_name": app.DEFAULT_BEDROCK_REMOVE_BG_REGION}
    assert ("s3", {}) in client_calls
    # GEMINI_SERVICE_ACCOUNT_SECRET_ARN isn't set in this test's
    # environment, so no secretsmanager client is constructed at all --
    # see the dedicated gemini-secret tests just below for the
    # "configured" path.
    assert not any(c[0] == "secretsmanager" for c in client_calls)


def test_handler_mints_gemini_access_token_from_service_account_secret_when_configured():
    """GEMINI_SERVICE_ACCOUNT_SECRET_ARN set -> handler() fetches the
    secret (the service account's WHOLE JSON key file, per this module's
    own docstring -- NOT wrapped in an extra field, unlike v4's own
    {"api_key": "..."} shape), passes it to mint_gemini_access_token to
    get a Bearer token, then threads {"access_token", "project_id",
    "region"} through to generate_article_for_product as gemini_auth.
    mint_gemini_access_token itself is patched out here -- it does a real
    OAuth2 exchange against Google's own token endpoint via google-auth,
    which is call_gemini_for_image's own concern to get right (tested in
    isolation elsewhere), not handler()'s wiring."""
    conn = _FakeConnection()
    calls = []
    mint_calls = []
    service_account_json = {
        "type": "service_account", "project_id": "my-gcp-project",
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
        "client_email": "gemini@my-gcp-project.iam.gserviceaccount.com",
    }

    class _FakeSecretsManagerClient:
        def get_secret_value(self, SecretId):
            assert SecretId == "arn:aws:secretsmanager:us-west-1:123:secret:gemini-sa-key"
            return {"SecretString": json.dumps(service_account_json)}

    def _fake_boto3_client(service_name, **kwargs):
        if service_name == "secretsmanager":
            return _FakeSecretsManagerClient()
        return _FakeBedrockClient("{}")

    def _fake_generate(conn_, bedrock, model_id, product_id, force=False, **kwargs):
        calls.append(kwargs)
        return {"product_id": product_id, "generated": True, "article_id": "a1",
                "video_count": 1, "sibling_count": 0}

    def _fake_mint(service_account_info):
        mint_calls.append(service_account_info)
        return "real-access-token"

    import types
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = _fake_boto3_client

    guard = _HandlerPatchGuard()
    try:
        guard.set_module("boto3", fake_boto3)
        guard.set(app, "get_db_connection", lambda: conn)
        guard.set(app, "generate_article_for_product", _fake_generate)
        guard.set(app, "mint_gemini_access_token", _fake_mint)
        os.environ["GEMINI_SERVICE_ACCOUNT_SECRET_ARN"] = "arn:aws:secretsmanager:us-west-1:123:secret:gemini-sa-key"
        app.handler({"product_id": "prod-1"}, None)
    finally:
        os.environ.pop("GEMINI_SERVICE_ACCOUNT_SECRET_ARN", None)
        guard.restore()

    assert mint_calls == [service_account_json]
    assert calls[0]["gemini_auth"] == {
        "access_token": "real-access-token", "project_id": "my-gcp-project",
        "region": app.DEFAULT_GEMINI_REGION,
    }


def test_handler_tolerates_gemini_secret_fetch_failure():
    """A Secrets Manager error (bad ARN, denied IAM, etc.) fetching the
    Gemini service-account key must not crash the whole handler invocation
    -- same "images are always best-effort" posture as every other image
    failure path in this module. gemini_auth just stays None, which
    generate_article_image_candidates' own all-or-nothing gate then turns
    into "skip the Gemini candidates this run" (see that function's own
    docstring), not a hard failure of the whole product."""
    conn = _FakeConnection()
    calls = []

    class _FailingSecretsManagerClient:
        def get_secret_value(self, SecretId):
            raise RuntimeError("AccessDenied")

    def _fake_boto3_client(service_name, **kwargs):
        if service_name == "secretsmanager":
            return _FailingSecretsManagerClient()
        return _FakeBedrockClient("{}")

    def _fake_generate(conn_, bedrock, model_id, product_id, force=False, **kwargs):
        calls.append(kwargs)
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
        os.environ["GEMINI_SERVICE_ACCOUNT_SECRET_ARN"] = "arn:aws:secretsmanager:us-west-1:123:secret:gemini-sa-key"
        result = app.handler({"product_id": "prod-1"}, None)
    finally:
        os.environ.pop("GEMINI_SERVICE_ACCOUNT_SECRET_ARN", None)
        guard.restore()

    assert calls[0]["gemini_auth"] is None
    assert json.loads(result["body"])["results"][0]["generated"] is True


def test_handler_tolerates_gemini_token_mint_failure():
    """Distinct from the Secrets Manager fetch failure above: the secret
    fetches fine but mint_gemini_access_token itself raises (e.g. a bad
    key, or Google's token endpoint rejecting the JWT) -- handler()'s own
    try/except wraps BOTH the fetch and the mint in one block (see
    handler's own docstring), so this must be equally non-fatal."""
    conn = _FakeConnection()
    calls = []

    class _FakeSecretsManagerClient:
        def get_secret_value(self, SecretId):
            return {"SecretString": json.dumps({"project_id": "my-gcp-project"})}

    def _fake_boto3_client(service_name, **kwargs):
        if service_name == "secretsmanager":
            return _FakeSecretsManagerClient()
        return _FakeBedrockClient("{}")

    def _fake_generate(conn_, bedrock, model_id, product_id, force=False, **kwargs):
        calls.append(kwargs)
        return {"product_id": product_id, "generated": True, "article_id": "a1",
                "video_count": 1, "sibling_count": 0}

    def _failing_mint(service_account_info):
        raise RuntimeError("invalid_grant: JWT signature verification failed")

    import types
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = _fake_boto3_client

    guard = _HandlerPatchGuard()
    try:
        guard.set_module("boto3", fake_boto3)
        guard.set(app, "get_db_connection", lambda: conn)
        guard.set(app, "generate_article_for_product", _fake_generate)
        guard.set(app, "mint_gemini_access_token", _failing_mint)
        os.environ["GEMINI_SERVICE_ACCOUNT_SECRET_ARN"] = "arn:aws:secretsmanager:us-west-1:123:secret:gemini-sa-key"
        result = app.handler({"product_id": "prod-1"}, None)
    finally:
        os.environ.pop("GEMINI_SERVICE_ACCOUNT_SECRET_ARN", None)
        guard.restore()

    assert calls[0]["gemini_auth"] is None
    assert json.loads(result["body"])["results"][0]["generated"] is True


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


# --- _resolve_visual_context: shared fallback chain, pure, no DB/network ---

_SAMPLE_ARTICLE = dict(_VALID_ARTICLE_JSON, performance_summary="Reads early and hooks hard off the friction.")


def test_resolve_visual_context_uses_visual_theme_when_present():
    """visual_theme (the article-generation model's own name/branding-
    derived scene concept -- Al's explicit "background driven by the
    ball's name" ask) takes priority over performance_summary/hook when
    present."""
    article = dict(_SAMPLE_ARTICLE, visual_theme="A post-apocalyptic nuclear-wasteland backdrop.")
    assert app._resolve_visual_context(article) == "A post-apocalyptic nuclear-wasteland backdrop."


def test_resolve_visual_context_falls_back_to_performance_summary_when_no_visual_theme():
    article = dict(_SAMPLE_ARTICLE, visual_theme="")
    assert app._resolve_visual_context(article) == "Reads early and hooks hard off the friction."


def test_resolve_visual_context_falls_back_to_hook_when_no_theme_or_summary():
    article = dict(_VALID_ARTICLE_JSON, visual_theme="", performance_summary="", hook="A late-night league anecdote.")
    assert app._resolve_visual_context(article) == "A late-night league anecdote."


def test_resolve_visual_context_caps_at_300_chars():
    article = dict(_VALID_ARTICLE_JSON, visual_theme="x" * 500)
    assert len(app._resolve_visual_context(article)) == 300


def test_resolve_visual_context_empty_when_nothing_available():
    article = dict(_VALID_ARTICLE_JSON, visual_theme="", performance_summary="", hook="")
    assert app._resolve_visual_context(article) == ""


# --- call_bedrock_remove_background / call_bedrock_generate_background:
# request/response shapes for the two Bedrock-adjacent calls the ONE
# Stability/composite candidate per shot uses (revived in v4 -- see this
# module's own docstring for the full v1-v4 history). Both InvokeModel-
# shaped clients below share the same {images, finish_reasons, seeds}
# response shape as _FakeBedrockClient's own Stability-wire-format
# precedent. ---

class _FakeImageBedrockClient:
    def __init__(self, response_payload: dict):
        self.response_payload = response_payload
        self.calls = []

    def invoke_model(self, modelId, contentType, accept, body):
        self.calls.append({"modelId": modelId, "body": json.loads(body)})
        return {"body": _FakeBedrockBody(self.response_payload)}


def test_call_bedrock_remove_background_sends_correct_request_shape():
    import base64

    fake_png = base64.b64encode(b"fake-cutout-bytes").decode("ascii")
    client = _FakeImageBedrockClient({"images": [fake_png], "finish_reasons": [None], "seeds": [1]})

    result = app.call_bedrock_remove_background(
        client, "us.stability.stable-image-remove-background-v1:0", "ref-b64",
    )

    assert result == b"fake-cutout-bytes"
    sent_body = client.calls[0]["body"]
    assert sent_body["image"] == "ref-b64"
    assert sent_body["output_format"] == "png"
    # Remove Background takes only image (+ optional output_format) -- no
    # prompt/mode/strength at all, unlike a diffusion call.
    assert "prompt" not in sent_body
    assert "mode" not in sent_body


def test_call_bedrock_remove_background_raises_on_non_null_finish_reason():
    client = _FakeImageBedrockClient({"finish_reasons": ["Filter reason: input image"]})
    try:
        app.call_bedrock_remove_background(client, "model-id", "ref-b64")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "Filter reason: input image" in str(exc)


def test_call_bedrock_remove_background_raises_on_empty_images_list():
    client = _FakeImageBedrockClient({"images": [], "finish_reasons": [None]})
    try:
        app.call_bedrock_remove_background(client, "model-id", "ref-b64")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "no images" in str(exc)


# --- build_background_prompts: pure, no DB, no network ---

def test_build_background_prompts_include_no_ball_negative_instruction():
    prompts = app.build_background_prompts({"color": "Blue/Black"}, _SAMPLE_ARTICLE)
    assert "no bowling ball" in prompts["action_shot"]
    assert "no bowling ball" in prompts["product_shot"]


def test_build_background_prompts_never_positively_describe_the_ball():
    """Al: "The ball needs to be mostly untouched." The ball is
    composited in afterward from the real, untouched cutout (see
    composite_ball_on_background), never generated, so describing one in
    this prompt would risk the model painting a second, different-
    looking ball into the scene."""
    prompts = app.build_background_prompts({"color": "Blue/Black"}, _SAMPLE_ARTICLE)
    assert "Blue/Black bowling ball" not in prompts["action_shot"]
    assert "Blue/Black bowling ball" not in prompts["product_shot"]


def test_build_background_prompts_uses_visual_theme_via_resolve_visual_context():
    article = dict(_SAMPLE_ARTICLE, visual_theme="A post-apocalyptic nuclear-wasteland backdrop.")
    prompts = app.build_background_prompts({"color": "Blue/Black"}, article)
    assert "A post-apocalyptic nuclear-wasteland backdrop." in prompts["action_shot"]
    assert "Reads early and hooks hard off the friction." not in prompts["action_shot"]


def test_build_background_prompts_falls_back_to_hook_when_no_theme_or_summary():
    article = dict(_VALID_ARTICLE_JSON, visual_theme="", performance_summary="", hook="A late-night league anecdote.")
    prompts = app.build_background_prompts({"color": "Red"}, article)
    assert "A late-night league anecdote." in prompts["action_shot"]


def test_build_background_prompts_two_distinct_scenes():
    prompts = app.build_background_prompts({"color": "Purple"}, _SAMPLE_ARTICLE)
    assert "bowling alley lane" in prompts["action_shot"]
    assert "studio product-photography backdrop" in prompts["product_shot"]
    assert prompts["action_shot"] != prompts["product_shot"]


def test_call_bedrock_generate_background_sends_text_to_image_request_shape():
    import base64

    fake_png = base64.b64encode(b"fake-background-bytes").decode("ascii")
    client = _FakeImageBedrockClient({"images": [fake_png], "finish_reasons": [None], "seeds": [1]})

    result = app.call_bedrock_generate_background(client, "stability.sd3-5-large-v1:0", "an empty lane", "16:9")

    assert result == b"fake-background-bytes"
    sent_body = client.calls[0]["body"]
    assert sent_body["mode"] == "text-to-image"
    assert sent_body["aspect_ratio"] == "16:9"
    assert sent_body["prompt"] == "an empty lane"
    assert sent_body["negative_prompt"]  # non-empty, keeps text/watermarks/the ball itself out
    # No reference-image conditioning at all -- plain text-to-image.
    assert "image" not in sent_body
    assert "strength" not in sent_body
    # No seed argument supplied -> not sent at all (Stability defaults to
    # a random seed when omitted).
    assert "seed" not in sent_body


def test_call_bedrock_generate_background_sends_seed_when_supplied():
    """v4 adds an optional seed param (recorded on the resulting
    candidate row, see 026_product_article_image_candidates.sql's own
    `seed` column comment) -- confirms it's actually threaded into the
    request body when truthy, not just accepted and dropped."""
    import base64

    fake_png = base64.b64encode(b"fake-background-bytes").decode("ascii")
    client = _FakeImageBedrockClient({"images": [fake_png], "finish_reasons": [None]})

    app.call_bedrock_generate_background(client, "model-id", "prompt", "1:1", seed=12345)

    assert client.calls[0]["body"]["seed"] == 12345


def test_call_bedrock_generate_background_raises_on_non_null_finish_reason():
    client = _FakeImageBedrockClient({"finish_reasons": ["Filter reason: prompt"]})
    try:
        app.call_bedrock_generate_background(client, "model-id", "bad prompt", "1:1")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "Filter reason: prompt" in str(exc)


def test_call_bedrock_generate_background_raises_on_empty_images_list():
    client = _FakeImageBedrockClient({"images": [], "finish_reasons": [None]})
    try:
        app.call_bedrock_generate_background(client, "model-id", "prompt", "1:1")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "no images" in str(exc)


# --- composite_ball_on_background: pure Pillow compositing, no Bedrock/DB.
# This is the function that actually satisfies Al's "mostly untouched"
# ask for the Stability candidate -- these tests confirm the cutout's own
# pixels survive compositing byte-for-byte, not just that the function
# runs without error. ---

def _png_bytes(image) -> bytes:
    import io

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_composite_ball_on_background_preserves_cutout_pixels_untouched():
    import io

    from PIL import Image, ImageDraw

    cutout = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    ImageDraw.Draw(cutout).ellipse([100, 100, 300, 300], fill=(255, 0, 0, 255))
    background = Image.new("RGBA", (1536, 864), (0, 0, 255, 255))

    result_bytes = app.composite_ball_on_background(_png_bytes(cutout), _png_bytes(background))

    out = Image.open(io.BytesIO(result_bytes)).convert("RGB")
    w, h = out.size
    center_pixel = out.getpixel((w // 2, int(h * 0.58) - 1))
    assert center_pixel == (255, 0, 0)


def test_composite_ball_on_background_output_matches_background_size_and_is_flattened():
    import io

    from PIL import Image, ImageDraw

    cutout = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(cutout).ellipse([50, 50, 150, 150], fill=(10, 200, 30, 255))
    background = Image.new("RGBA", (1024, 1024), (50, 50, 50, 255))

    result_bytes = app.composite_ball_on_background(_png_bytes(cutout), _png_bytes(background))

    out = Image.open(io.BytesIO(result_bytes))
    assert out.size == (1024, 1024)
    assert out.mode == "RGB"  # flattened for storage -- no alpha channel in the final stored PNG


def test_composite_ball_on_background_ball_fills_most_of_the_frames_shorter_side():
    """2026-09-04, Al's ask: the ball was reading too small/distant.
    Confirms the pasted cutout's rendered size is a LARGE majority of the
    background's shorter side (bumped from a 0.62 to a 0.82 target
    fraction -- see composite_ball_on_background's own docstring), not
    just that compositing runs. Measures the ball's actual rendered
    width by scanning the output's own pixels for the cutout's known
    fill color, rather than trusting an internal implementation detail,
    so this test would still catch a regression even if the scaling
    logic were reshuffled."""
    import io

    from PIL import Image, ImageDraw

    cutout = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    ImageDraw.Draw(cutout).ellipse([100, 100, 300, 300], fill=(255, 0, 0, 255))
    background = Image.new("RGBA", (1000, 1000), (0, 0, 255, 255))

    result_bytes = app.composite_ball_on_background(_png_bytes(cutout), _png_bytes(background))

    out = Image.open(io.BytesIO(result_bytes)).convert("RGB")
    row_y = out.size[1] // 2  # the ellipse's widest row once pasted, per the preceding pixel test
    red_xs = [x for x in range(out.size[0]) if out.getpixel((x, row_y)) == (255, 0, 0)]
    ball_width = max(red_xs) - min(red_xs) + 1
    assert ball_width / min(out.size) > 0.7  # comfortably above the old 0.62 fraction


def test_composite_ball_on_background_leaves_background_untouched_away_from_the_ball():
    import io

    from PIL import Image, ImageDraw

    cutout = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(cutout).ellipse([80, 80, 120, 120], fill=(10, 200, 30, 255))
    background = Image.new("RGBA", (800, 800), (5, 5, 200, 255))

    result_bytes = app.composite_ball_on_background(_png_bytes(cutout), _png_bytes(background))

    out = Image.open(io.BytesIO(result_bytes)).convert("RGB")
    corner_pixel = out.getpixel((2, 2))
    assert corner_pixel == (5, 5, 200)


# --- mint_gemini_access_token / build_gemini_scene_prompt /
# call_gemini_for_image: the second candidate SOURCE (Google Cloud, not
# Bedrock -- see this module's own docstring for why). ---

def test_mint_gemini_access_token_builds_credentials_with_cloud_platform_scope_and_returns_token():
    """google-auth itself can't be installed in this sandbox (same pip-
    proxy-403 caveat noted elsewhere in this project's test files -- see
    _HandlerPatchGuard's own docstring), so this fakes the handful of
    google.* module/class names mint_gemini_access_token actually touches
    -- google.oauth2.service_account.Credentials.from_service_account_
    info(...) and google.auth.transport.requests.Request -- via
    sys.modules injection, the same pattern _fake_boto3_module already
    uses for boto3 elsewhere in this file. Confirms mint_gemini_access_
    token's OWN logic (the right service-account info and scope get
    passed through, refresh() is actually called, and the resulting
    .token is what gets returned) without needing the real library or a
    real service-account key."""
    import types

    calls = {}

    class _FakeCredentials:
        def __init__(self, info, scopes):
            calls["info"] = info
            calls["scopes"] = scopes
            self.token = None

        def refresh(self, request):
            calls["refresh_request"] = request
            self.token = "minted-access-token"

    class _FakeCredentialsClass:
        @staticmethod
        def from_service_account_info(info, scopes):
            return _FakeCredentials(info, scopes)

    class _FakeRequest:
        pass

    fake_google_auth_transport_requests = types.ModuleType("google.auth.transport.requests")
    fake_google_auth_transport_requests.Request = _FakeRequest
    fake_google_oauth2_service_account = types.ModuleType("google.oauth2.service_account")
    fake_google_oauth2_service_account.Credentials = _FakeCredentialsClass

    guard = _HandlerPatchGuard()
    try:
        guard.set_module("google", types.ModuleType("google"))
        guard.set_module("google.auth", types.ModuleType("google.auth"))
        guard.set_module("google.auth.transport", types.ModuleType("google.auth.transport"))
        guard.set_module("google.auth.transport.requests", fake_google_auth_transport_requests)
        guard.set_module("google.oauth2", types.ModuleType("google.oauth2"))
        guard.set_module("google.oauth2.service_account", fake_google_oauth2_service_account)

        service_account_info = {"project_id": "my-gcp-project", "client_email": "x@y.iam.gserviceaccount.com"}
        result = app.mint_gemini_access_token(service_account_info)
    finally:
        guard.restore()

    assert result == "minted-access-token"
    assert calls["info"] == service_account_info
    assert calls["scopes"] == [app.GEMINI_OAUTH_SCOPE]
    assert isinstance(calls["refresh_request"], _FakeRequest)


def test_build_gemini_scene_prompt_uses_visual_theme_via_resolve_visual_context():
    article = dict(_SAMPLE_ARTICLE, visual_theme="A Fallout-style post-apocalyptic wasteland.")
    prompt = app.build_gemini_scene_prompt({"color": "Blue"}, article, "action_shot")
    assert "A Fallout-style post-apocalyptic wasteland" in prompt


def test_build_gemini_scene_prompt_falls_back_to_generic_scene_when_no_context():
    article = dict(_VALID_ARTICLE_JSON, visual_theme="", performance_summary="", hook="")
    prompt = app.build_gemini_scene_prompt({"color": "Blue"}, article, "product_shot")
    assert "elevated, premium studio scene" in prompt


def test_build_gemini_scene_prompt_instructs_keeping_the_ball_unchanged():
    """The core "mostly untouched" requirement, satisfied a different way
    than Stability's cutout+composite mechanism -- Gemini gets no cutout
    at all, so the prompt itself has to carry the instruction."""
    prompt = app.build_gemini_scene_prompt({"color": "Blue"}, _SAMPLE_ARTICLE, "action_shot")
    assert "completely unchanged" in prompt
    assert "logo/text exactly as shown in the reference image" in prompt


def test_build_gemini_scene_prompt_distinguishes_action_from_product_framing():
    action_prompt = app.build_gemini_scene_prompt({"color": "Blue"}, _SAMPLE_ARTICLE, "action_shot")
    product_prompt = app.build_gemini_scene_prompt({"color": "Blue"}, _SAMPLE_ARTICLE, "product_shot")
    # 2026-09-06 fix: "action/lifestyle photograph" (the pre-fix wording)
    # dropped in favor of "dynamic hero shot conveying motion and energy"
    # -- "lifestyle photograph" is exactly the phrase that invited Gemini
    # 3 Pro Image to render a literal photograph of a person bowling (see
    # this function's own "REAL INCIDENT" docstring section).
    assert "dynamic hero shot conveying motion and energy" in action_prompt
    assert "not in motion" in product_prompt
    assert action_prompt != product_prompt


def test_build_gemini_scene_prompt_instructs_ball_to_be_large_and_prominent():
    """2026-09-04, Al's ask: the ball was reading too small/distant in
    generated images. Gemini gets no pixel-level size control (unlike
    the Stability path's composite_ball_on_background, which places the
    cutout at an explicit fraction of the frame) -- the prompt itself is
    the only lever, so it has to say so explicitly, for both variants."""
    action_prompt = app.build_gemini_scene_prompt({"color": "Blue"}, _SAMPLE_ARTICLE, "action_shot")
    product_prompt = app.build_gemini_scene_prompt({"color": "Blue"}, _SAMPLE_ARTICLE, "product_shot")
    for prompt in (action_prompt, product_prompt):
        assert "hero subject" in prompt
        assert "large and prominent" in prompt
        assert "not small or distant" in prompt


def test_build_gemini_scene_prompt_excludes_people_and_bowling_venue_props():
    """REAL INCIDENT (2026-09-06, Al): with Stability disabled by default,
    every candidate now comes from this prompt alone -- and it had NEVER
    excluded people/pins/a bowler the way the (now-unused-by-default)
    Stability prompt always did. Al sent two side-by-side examples of the
    same product: a themed neon-lane BACKDROP with the ball as the sole
    subject (wanted) vs. a photograph of a person mid-delivery with
    bowling shoes and scattered pins (not wanted, "hallucinations that
    just feel phony"). Confirms the new exclusion list is present for
    both variants, and that a themed lane/alley backdrop itself is still
    explicitly allowed -- Al's own distinction was people/pins/props, not
    the lane setting itself."""
    action_prompt = app.build_gemini_scene_prompt({"color": "Blue"}, _SAMPLE_ARTICLE, "action_shot")
    product_prompt = app.build_gemini_scene_prompt({"color": "Blue"}, _SAMPLE_ARTICLE, "product_shot")
    for prompt in (action_prompt, product_prompt):
        assert "do not include any people, hands, arms, legs, human figures" in prompt
        assert "bowling shoes" in prompt
        assert "bowling pins" in prompt
        assert "not a photograph of someone in the act of bowling" in prompt
        # The lane/alley setting itself is still explicitly permitted --
        # this is a "no people/props" fix, not a "no bowling lane" one.
        assert "may still evoke a bowling lane or alley setting" in prompt


class _FakeGeminiResponse:
    def __init__(self, payload: dict, status_ok: bool = True):
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            import requests

            raise requests.exceptions.HTTPError("400")

    def json(self):
        return self._payload


class _FakeGeminiSession:
    """Stand-in for the retry-with-backoff requests.Session call_gemini_
    for_image now takes as its optional `session` argument (see that
    function's own "REAL INCIDENT, part three" docstring section --
    get_gemini_requests_session mounts a real urllib3 Retry adapter that
    would otherwise try a real HTTP connection in tests). Tests pass one
    of these directly via `session=` instead of the old "monkeypatch the
    module-level requests.post, restore it in finally" pattern -- module-
    level requests.post was never actually what call_gemini_for_image hit
    once it moved to session.post(...), so that old pattern would now
    silently NOT intercept anything and tests would attempt a real
    network call. A plain wrapper around whatever fake post function the
    test already wants to use, so every existing test's own `_fake_post`/
    lambda signature (url, headers=None, json=None, timeout=None) keeps
    working unchanged."""
    def __init__(self, post_fn):
        self._post_fn = post_fn

    def post(self, url, headers=None, json=None, timeout=None):
        return self._post_fn(url, headers=headers, json=json, timeout=timeout)


# A stand-in gemini_auth bundle (see call_gemini_for_image's own
# docstring) for tests that don't care about its exact contents, just
# that SOME auth dict flows through -- same "any string will do" spirit
# the old "api-key" literal had, updated for the v5 dict shape.
_FAKE_GEMINI_AUTH = {"access_token": "fake-token", "project_id": "fake-project", "region": "us-central1"}


def test_call_gemini_for_image_sends_correct_request_shape_and_auth_header():
    """Confirms the request is a direct call to Vertex AI's own REST API
    (Authorization: Bearer <token>, NOT the x-goog-api-key header v4 used
    against the Developer API, and NOT any AWS/Bedrock mechanism -- see
    this module's own docstring for the full v4->v5 story) with the
    reference photo attached as inline image data alongside the text
    prompt, and the URL built from gemini_auth's project_id/region rather
    than a fixed base URL (Vertex AI's endpoint is per-project/per-Region,
    unlike the Developer API's single global host)."""
    import base64

    calls = []
    fake_image_bytes = b"fake-gemini-output-bytes"
    payload = {
        "candidates": [{
            "content": {"parts": [{"inlineData": {
                "mimeType": "image/png", "data": base64.b64encode(fake_image_bytes).decode("ascii"),
            }}]},
        }],
    }

    def _fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _FakeGeminiResponse(payload)

    gemini_auth = {"access_token": "token-abc-123", "project_id": "my-gcp-project", "region": "us-central1"}

    result = app.call_gemini_for_image(gemini_auth, "gemini-3-pro-image", "a scene", "ref-b64", "16:9",
                                        session=_FakeGeminiSession(_fake_post))

    assert result == fake_image_bytes
    call = calls[0]
    assert call["url"] == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/my-gcp-project/"
        "locations/us-central1/publishers/google/models/gemini-3-pro-image:generateContent"
    )
    assert call["headers"]["Authorization"] == "Bearer token-abc-123"
    # "role": "user" is REQUIRED by Vertex AI (unlike the Developer API,
    # which tolerated omitting it) -- confirmed the hard way against a
    # real invocation on 2026-09-04, see this function's own docstring.
    assert call["json"]["contents"][0]["role"] == "user"
    parts = call["json"]["contents"][0]["parts"]
    assert parts[0]["text"] == "a scene"
    # camelCase, not the snake_case v4's own Developer-API version used --
    # also confirmed the hard way, same incident.
    assert parts[1]["inlineData"] == {"mimeType": "image/png", "data": "ref-b64"}
    assert call["json"]["generationConfig"]["imageConfig"]["aspectRatio"] == "16:9"


def test_call_gemini_for_image_uses_global_host_shape_for_global_region():
    """REAL INCIDENT (2026-09-04), part two regression test: with region=
    "global" (now DEFAULT_GEMINI_REGION -- see that constant's own
    comment), the URL must use the bare `aiplatform.googleapis.com` host
    (NO per-Region subdomain prefix), not `global-aiplatform.googleapis.
    com` -- that wrong prefixed-host guess is exactly what caused a real
    404 even after the model id itself was confirmed correct. Only the
    `locations/` path segment says "global"; the host does not carry it."""
    calls = []
    payload = {"candidates": [{"content": {"parts": [{"inlineData": {
        "mimeType": "image/png", "data": "ZmFrZQ==",
    }}]}}]}

    def _fake_post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        return _FakeGeminiResponse(payload)

    gemini_auth = {"access_token": "tok", "project_id": "my-gcp-project", "region": "global"}

    app.call_gemini_for_image(gemini_auth, "gemini-3-pro-image", "a scene", "ref-b64", "16:9",
                               session=_FakeGeminiSession(_fake_post))

    assert calls[0] == (
        "https://aiplatform.googleapis.com/v1/projects/my-gcp-project/"
        "locations/global/publishers/google/models/gemini-3-pro-image:generateContent"
    )


def test_call_gemini_for_image_handles_snake_case_inline_data_field():
    """Defends against BOTH inlineData (REST JSON casing) and inline_data
    (Python SDK object-model casing) in the response -- see call_gemini_
    for_image's own docstring on why published examples were inconsistent
    about which one is authoritative."""
    import base64

    fake_image_bytes = b"snake-case-bytes"
    payload = {
        "candidates": [{
            "content": {"parts": [{"inline_data": {
                "mime_type": "image/png", "data": base64.b64encode(fake_image_bytes).decode("ascii"),
            }}]},
        }],
    }

    fake_post = lambda url, headers=None, json=None, timeout=None: _FakeGeminiResponse(payload)
    result = app.call_gemini_for_image(_FAKE_GEMINI_AUTH, "model-id", "prompt", "ref-b64", "1:1",
                                        session=_FakeGeminiSession(fake_post))

    assert result == fake_image_bytes


def test_call_gemini_for_image_raises_when_no_image_data_in_response():
    payload = {"candidates": [{"content": {"parts": [{"text": "I can't do that."}]}, "finishReason": "SAFETY"}]}

    fake_post = lambda url, headers=None, json=None, timeout=None: _FakeGeminiResponse(payload)
    try:
        app.call_gemini_for_image(_FAKE_GEMINI_AUTH, "model-id", "prompt", "ref-b64", "1:1",
                                   session=_FakeGeminiSession(fake_post))
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "SAFETY" in str(exc)


def test_call_gemini_for_image_raises_when_no_candidates_in_response():
    fake_post = lambda url, headers=None, json=None, timeout=None: _FakeGeminiResponse({"candidates": []})
    try:
        app.call_gemini_for_image(_FAKE_GEMINI_AUTH, "model-id", "prompt", "ref-b64", "1:1",
                                   session=_FakeGeminiSession(fake_post))
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "no candidates" in str(exc)


def test_call_gemini_for_image_defaults_to_retry_session_when_none_given():
    """When the caller doesn't pass session= at all (the real handler()
    path never does -- see generate_article_image_candidates, which
    builds and threads through its own shared session), call_gemini_for_
    image must build one itself via get_gemini_requests_session rather
    than silently having no retry behavior. Confirms the wiring, not
    get_gemini_requests_session's own retry mechanics (that's urllib3's
    own, well-tested Retry class -- see that function's docstring)."""
    fake_image_bytes = b"built-a-session-myself"
    import base64
    payload = {"candidates": [{"content": {"parts": [{"inlineData": {
        "mimeType": "image/png", "data": base64.b64encode(fake_image_bytes).decode("ascii"),
    }}]}}]}

    built_sessions = []

    class _RecordingSession(_FakeGeminiSession):
        def __init__(self, post_fn):
            super().__init__(post_fn)
            built_sessions.append(self)

    original_get_session = app.get_gemini_requests_session
    app.get_gemini_requests_session = lambda: _RecordingSession(
        lambda url, headers=None, json=None, timeout=None: _FakeGeminiResponse(payload)
    )
    try:
        result = app.call_gemini_for_image(_FAKE_GEMINI_AUTH, "model-id", "prompt", "ref-b64", "1:1")
    finally:
        app.get_gemini_requests_session = original_get_session

    assert result == fake_image_bytes
    assert len(built_sessions) == 1


def test_get_gemini_requests_session_retries_429_and_5xx():
    """Confirms the actual retry configuration -- status_forcelist
    includes 429 (the real incident's own status code, see this module's
    "REAL INCIDENT, part three" comment) and the 500/502/503/504 quintet
    already established by scripts/backfill_core_ids.py's own get_
    requests_session for the same class of transient error."""
    session = app.get_gemini_requests_session()
    adapter = session.get_adapter("https://aiplatform.googleapis.com/")
    retry = adapter.max_retries
    assert retry.total == app.GEMINI_RETRY_TOTAL
    assert set(retry.status_forcelist) == set(app.GEMINI_RETRY_STATUS_FORCELIST)
    assert 429 in retry.status_forcelist


# --- store_article_image ---

class _FakeS3Client:
    def __init__(self):
        self.put_calls = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)


def test_store_article_image_builds_article_images_prefix_key_and_url():
    """`name` is the full per-candidate filename stem as of v4 (e.g.
    "action_shot_gemini_1"), not the bare variant -- v4 stores multiple
    candidates per variant (026_product_article_image_candidates.sql), so
    the un-suffixed "{variant}.png" key v1-v3 used would collide across
    candidates."""
    s3 = _FakeS3Client()
    result = app.store_article_image(s3, "my-bucket", "prod-1", "action_shot_gemini_1", b"pngbytes")

    assert result == {
        "key": "article-images/prod-1/action_shot_gemini_1.png",
        "url": "https://my-bucket.s3.amazonaws.com/article-images/prod-1/action_shot_gemini_1.png",
    }
    assert s3.put_calls == [{
        "Bucket": "my-bucket", "Key": "article-images/prod-1/action_shot_gemini_1.png",
        "Body": b"pngbytes", "ContentType": "image/png",
    }]


# --- generate_article_image_candidates: orchestration, best-effort/
# non-fatal -- v4's core new function, producing up to 3 candidates per
# shot. v5 (2026-09-04): ENABLE_STABILITY_CANDIDATES defaults to False
# (Al: "they will never be better than the gemini images"), so the
# DEFAULT shape is now 3 Gemini candidates and 0 Stability ones (see
# NUM_GEMINI_CANDIDATES_PER_VARIANT) -- the tests below that flip
# app.ENABLE_STABILITY_CANDIDATES back to True for the duration of the
# test exist specifically to keep the toggle's fallback path (kept,
# not deleted -- see this module's own docstring) from silently
# rotting. ---

def _fake_reference_photo_response():
    """A real tiny JPEG so the PIL round-trip inside generate_article_
    image_candidates succeeds (_reference_image_to_base64_png needs PIL
    to actually open the bytes, not arbitrary data)."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="JPEG")

    class _FakeRealResponse:
        content = buf.getvalue()

        def raise_for_status(self):
            pass

    return _FakeRealResponse()


def _fake_cutout_png_b64() -> str:
    """A real tiny RGBA PNG (opaque red circle on a transparent
    background) standing in for what call_bedrock_remove_background
    would actually return -- used as the fake Remove Background client's
    response payload so composite_ball_on_background has real image
    bytes to work with."""
    import base64
    import io

    from PIL import Image, ImageDraw

    cutout = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    ImageDraw.Draw(cutout).ellipse([20, 20, 80, 80], fill=(255, 0, 0, 255))
    buf = io.BytesIO()
    cutout.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _fake_background_png_b64() -> str:
    """A real tiny opaque PNG standing in for what call_bedrock_generate_
    background would actually return."""
    import base64
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (400, 400), (0, 0, 255, 255)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _fake_gemini_post(image_bytes_by_call=None, default_bytes=b"fake-gemini-bytes"):
    """A fake requests.post() returning a successful Gemini-shaped
    response -- call_gemini_for_image's own request/response SHAPE is
    tested in isolation above; this only cares about generate_article_
    image_candidates' own orchestration (how many Gemini calls happen per
    variant, whether failures are isolated, etc), so the returned bytes
    don't need to be a real image (Gemini candidates are stored straight
    to S3 via store_article_image, never opened with PIL, unlike the
    Stability/composite path). image_bytes_by_call, if given, is indexed
    by call count (0-based) so a test can make the SECOND call (the
    "alternate composition" one) return different bytes than the first;
    falls back to default_bytes past the end of that list."""
    import base64

    calls = []

    def _fake_post(url, headers=None, json=None, timeout=None):
        i = len(calls)
        calls.append({"url": url, "headers": headers, "json": json})
        image_bytes = (image_bytes_by_call[i] if image_bytes_by_call and i < len(image_bytes_by_call)
                       else default_bytes)
        data = base64.b64encode(image_bytes).decode("ascii")
        return _FakeGeminiResponse({"candidates": [{"content": {"parts": [{"inlineData": {"data": data}}]}}]})

    return _fake_post, calls


def test_generate_article_image_candidates_returns_empty_when_no_reference_image():
    conn = _FakeConnection(reference_image_url=None)
    result = app.generate_article_image_candidates(
        conn, bedrock_image_client=None, bedrock_removebg_client=None, gemini_auth=_FAKE_GEMINI_AUTH, s3_client=None,
        image_model_id="model-id", removebg_model_id="removebg-model-id", gemini_model_id="gemini-model-id",
        image_bucket="bucket", product={"id": "prod-1", "color": "Blue"}, article=_SAMPLE_ARTICLE,
    )
    assert result == {}


def test_generate_article_image_candidates_returns_empty_when_reference_fetch_fails():
    import requests

    conn = _FakeConnection(reference_image_url="https://example.com/ball.jpg")
    original_get = requests.get
    requests.get = lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError("network down"))
    try:
        result = app.generate_article_image_candidates(
            conn, bedrock_image_client=object(), bedrock_removebg_client=object(), gemini_auth=_FAKE_GEMINI_AUTH,
            s3_client=object(), image_model_id="model-id", removebg_model_id="removebg-model-id",
            gemini_model_id="gemini-model-id", image_bucket="bucket",
            product={"id": "prod-1", "color": "Blue"}, article=_SAMPLE_ARTICLE,
        )
    finally:
        requests.get = original_get

    assert result == {}


def test_generate_article_image_candidates_default_is_three_gemini_no_stability():
    """v5 default (ENABLE_STABILITY_CANDIDATES = False, see this
    module's own docstring): 3 Gemini candidates per shot, Gemini first
    (only, now -- there is no Stability candidate to come "after"), and
    Bedrock is never touched at all -- not Remove Background, not the
    background-generation model -- since nothing here needs the cutout
    when Stability is off."""
    import requests

    conn = _FakeConnection(reference_image_url="https://example.com/ball.jpg")
    removebg_client = _FakeImageBedrockClient({"images": [_fake_cutout_png_b64()], "finish_reasons": [None]})
    bg_client = _FakeImageBedrockClient({"images": [_fake_background_png_b64()], "finish_reasons": [None]})
    fake_post, gemini_calls = _fake_gemini_post()
    s3 = _FakeS3Client()

    original_get = requests.get
    original_get_session = app.get_gemini_requests_session
    requests.get = lambda url, timeout=None: _fake_reference_photo_response()
    app.get_gemini_requests_session = lambda: _FakeGeminiSession(fake_post)
    try:
        result = app.generate_article_image_candidates(
            conn, bedrock_image_client=bg_client, bedrock_removebg_client=removebg_client,
            gemini_auth=_FAKE_GEMINI_AUTH, s3_client=s3, image_model_id="model-id",
            removebg_model_id="removebg-model-id", gemini_model_id="gemini-model-id", image_bucket="bucket",
            product={"id": "prod-1", "color": "Blue"}, article=_SAMPLE_ARTICLE,
        )
    finally:
        requests.get = original_get
        app.get_gemini_requests_session = original_get_session

    assert set(result.keys()) == {"action_shot", "product_shot"}
    for variant in ("action_shot", "product_shot"):
        candidates = result[variant]
        assert len(candidates) == 3
        assert all(c["model_id"] == "gemini-model-id" for c in candidates)
        assert all(c["seed"] is None for c in candidates)
        assert candidates[0]["key"] == f"article-images/prod-1/{variant}_gemini_1.png"
        assert candidates[1]["key"] == f"article-images/prod-1/{variant}_gemini_2.png"
        assert candidates[2]["key"] == f"article-images/prod-1/{variant}_gemini_3.png"
    # Stability disabled -- Remove Background and background-generation
    # are BOTH never even attempted.
    assert len(removebg_client.calls) == 0
    assert len(bg_client.calls) == 0
    # 3 Gemini calls per variant x 2 variants = 6.
    assert len(gemini_calls) == 6
    # First call per variant gets no suffix; second gets "alternate
    # composition"; third gets its own distinct, numbered suffix -- true
    # regardless of interleaving (see v6 in this module's own docstring),
    # since the suffix is keyed on each variant's OWN candidate index i,
    # not the call's position in the shared, interleaved call order.
    prompts = [c["json"]["contents"][0]["parts"][0]["text"] for c in gemini_calls]
    assert sum("alternate composition" in p for p in prompts) == 2
    assert sum("composition/angle #3" in p for p in prompts) == 2
    assert sum("alternate composition" not in p and "composition/angle #3" not in p for p in prompts) == 2
    # 6 Gemini S3 writes total, no Stability writes.
    assert len(s3.put_calls) == 6


def test_generate_article_image_candidates_stability_enabled_adds_fourth_candidate():
    """Regression coverage for the ENABLE_STABILITY_CANDIDATES fallback
    itself (see this module's own docstring on why it's kept, not
    deleted): flipped back to True, the original v4 shape returns --
    3 Gemini candidates plus 1 Stability composite per shot, Gemini
    first then Stability (generate_article_for_product treats index 0
    as the auto-selected default)."""
    import requests

    conn = _FakeConnection(reference_image_url="https://example.com/ball.jpg")
    removebg_client = _FakeImageBedrockClient({"images": [_fake_cutout_png_b64()], "finish_reasons": [None]})
    bg_client = _FakeImageBedrockClient({"images": [_fake_background_png_b64()], "finish_reasons": [None]})
    fake_post, gemini_calls = _fake_gemini_post()
    s3 = _FakeS3Client()

    original_get = requests.get
    original_get_session = app.get_gemini_requests_session
    requests.get = lambda url, timeout=None: _fake_reference_photo_response()
    app.get_gemini_requests_session = lambda: _FakeGeminiSession(fake_post)
    app.ENABLE_STABILITY_CANDIDATES = True
    try:
        result = app.generate_article_image_candidates(
            conn, bedrock_image_client=bg_client, bedrock_removebg_client=removebg_client,
            gemini_auth=_FAKE_GEMINI_AUTH, s3_client=s3, image_model_id="model-id",
            removebg_model_id="removebg-model-id", gemini_model_id="gemini-model-id", image_bucket="bucket",
            product={"id": "prod-1", "color": "Blue"}, article=_SAMPLE_ARTICLE,
        )
    finally:
        requests.get = original_get
        app.get_gemini_requests_session = original_get_session
        app.ENABLE_STABILITY_CANDIDATES = False

    for variant in ("action_shot", "product_shot"):
        candidates = result[variant]
        assert len(candidates) == 4
        assert candidates[0]["model_id"] == "gemini-model-id"
        assert candidates[1]["model_id"] == "gemini-model-id"
        assert candidates[2]["model_id"] == "gemini-model-id"
        assert candidates[3]["model_id"] == "model-id"
        assert isinstance(candidates[3]["seed"], int)
        assert candidates[3]["key"] == f"article-images/prod-1/{variant}_stability.png"
    # Remove Background is only called ONCE -- the cutout is shared across
    # both variants (same source photo).
    assert len(removebg_client.calls) == 1
    # Stability background generation runs once per variant, at that
    # variant's own aspect ratio.
    assert len(bg_client.calls) == 2
    aspect_ratios = {c["body"]["aspect_ratio"] for c in bg_client.calls}
    assert aspect_ratios == {"16:9", "1:1"}
    # 3 Gemini calls per variant x 2 variants = 6.
    assert len(gemini_calls) == 6
    # 6 Gemini + 2 Stability = 8 S3 writes total.
    assert len(s3.put_calls) == 8


def test_generate_article_image_candidates_skips_stability_when_remove_background_fails():
    """With Stability enabled but Remove Background failing: no cutout,
    nothing to composite into the Stability candidate for EITHER
    variant -- but Gemini is entirely unaffected, since it never
    depended on the cutout in the first place (see this function's own
    docstring)."""
    import requests

    conn = _FakeConnection(reference_image_url="https://example.com/ball.jpg")

    class _FailingRemoveBgClient:
        def invoke_model(self, **kwargs):
            raise RuntimeError("Bedrock throttled")

    bg_client = _FakeImageBedrockClient({"images": [_fake_background_png_b64()], "finish_reasons": [None]})
    fake_post, gemini_calls = _fake_gemini_post()
    s3 = _FakeS3Client()

    original_get = requests.get
    original_get_session = app.get_gemini_requests_session
    requests.get = lambda url, timeout=None: _fake_reference_photo_response()
    app.get_gemini_requests_session = lambda: _FakeGeminiSession(fake_post)
    app.ENABLE_STABILITY_CANDIDATES = True
    try:
        result = app.generate_article_image_candidates(
            conn, bedrock_image_client=bg_client, bedrock_removebg_client=_FailingRemoveBgClient(),
            gemini_auth=_FAKE_GEMINI_AUTH, s3_client=s3, image_model_id="model-id",
            removebg_model_id="removebg-model-id", gemini_model_id="gemini-model-id", image_bucket="bucket",
            product={"id": "prod-1", "color": "Blue"}, article=_SAMPLE_ARTICLE,
        )
    finally:
        requests.get = original_get
        app.get_gemini_requests_session = original_get_session
        app.ENABLE_STABILITY_CANDIDATES = False

    for variant in ("action_shot", "product_shot"):
        assert len(result[variant]) == 3
        assert all(c["model_id"] == "gemini-model-id" for c in result[variant])
    assert len(bg_client.calls) == 0  # never even attempted without a cutout
    assert len(gemini_calls) == 6


def test_generate_article_image_candidates_one_gemini_call_fails_others_still_succeed():
    """Each Gemini call is independently try/excepted -- a failure on one
    must not take down the other, still-good candidates for that same
    variant, nor affect the other variant's calls at all. v6 (see this
    module's own docstring): calls now go out INTERLEAVED -- action_shot
    #1, product_shot #1, action_shot #2, product_shot #2, action_shot #3,
    product_shot #3 -- so the 3rd call overall (global index 2, 0-based)
    is action_shot's OWN second ("alternate composition") attempt, not
    the 2nd call overall the way it was pre-v6."""
    import requests

    conn = _FakeConnection(reference_image_url="https://example.com/ball.jpg")

    class _FlakyGeminiPost:
        def __init__(self):
            self.calls = []

        def __call__(self, url, headers=None, json=None, timeout=None):
            import base64

            self.calls.append({"json": json})
            if len(self.calls) == 3:  # action_shot's own 2nd ("alternate composition") call, 3rd overall post-v6
                raise RuntimeError("Gemini rate limited")
            data = base64.b64encode(b"gemini-bytes").decode("ascii")
            return _FakeGeminiResponse({"candidates": [{"content": {"parts": [{"inlineData": {"data": data}}]}}]})

    flaky_post = _FlakyGeminiPost()
    s3 = _FakeS3Client()

    original_get = requests.get
    original_get_session = app.get_gemini_requests_session
    requests.get = lambda url, timeout=None: _fake_reference_photo_response()
    app.get_gemini_requests_session = lambda: _FakeGeminiSession(flaky_post)
    try:
        result = app.generate_article_image_candidates(
            conn, bedrock_image_client=object(), bedrock_removebg_client=_RaisingRemoveBgClient(),
            gemini_auth=_FAKE_GEMINI_AUTH, s3_client=s3, image_model_id="model-id",
            removebg_model_id="removebg-model-id", gemini_model_id="gemini-model-id", image_bucket="bucket",
            product={"id": "prod-1", "color": "Blue"}, article=_SAMPLE_ARTICLE,
        )
    finally:
        requests.get = original_get
        app.get_gemini_requests_session = original_get_session

    # action_shot's second (of three) Gemini calls failed -- 2 candidates
    # for it (the first and third both succeeded).
    assert len(result["action_shot"]) == 2
    # product_shot's own three calls were unaffected.
    assert len(result["product_shot"]) == 3
    assert len(flaky_post.calls) == 6


class _RaisingRemoveBgClient:
    """Shared no-cutout stand-in for tests that don't care about the
    Stability path at all -- always fails so bedrock_image_client never
    actually needs to be a working fake."""

    def invoke_model(self, **kwargs):
        raise RuntimeError("no cutout for this test")


# --- store_article_image_candidates ---

def test_store_article_image_candidates_inserts_one_row_per_candidate():
    conn = _FakeConnection()
    candidates_by_variant = {
        "action_shot": [
            {"key": "article-images/p1/action_shot_gemini_1.png", "url": "https://b/action_shot_gemini_1.png",
             "model_id": "gemini-model", "seed": None},
            {"key": "article-images/p1/action_shot_stability.png", "url": "https://b/action_shot_stability.png",
             "model_id": "stability-model", "seed": 42},
        ],
        "product_shot": [
            {"key": "article-images/p1/product_shot_gemini_1.png", "url": "https://b/product_shot_gemini_1.png",
             "model_id": "gemini-model", "seed": None},
        ],
    }

    app.store_article_image_candidates(conn, "article-1", candidates_by_variant)

    assert conn.commits == 1
    assert len(conn._cursor.candidate_inserts) == 3
    # Index 0 of EACH variant's own list is marked is_selected=True -- the
    # same candidate generate_article_for_product mirrors onto product_
    # articles' own flat image columns (see store_article_image_
    # candidates' own docstring on why these two must stay in agreement).
    action_rows = [p for p in conn._cursor.candidate_inserts if p[1] == "action_shot"]
    product_rows = [p for p in conn._cursor.candidate_inserts if p[1] == "product_shot"]
    assert sum(1 for p in action_rows if p[6] is True) == 1
    assert sum(1 for p in product_rows if p[6] is True) == 1
    selected_action = next(p for p in action_rows if p[6] is True)
    assert selected_action[3] == "article-images/p1/action_shot_gemini_1.png"


def test_store_article_image_candidates_is_a_noop_for_empty_dict():
    conn = _FakeConnection()
    app.store_article_image_candidates(conn, "article-1", {})
    assert conn._cursor.candidate_inserts == []
    assert conn.commits == 0


def test_store_article_image_candidates_clears_prior_candidates_before_inserting_on_regenerate():
    """REAL INCIDENT (2026-09-04): a regenerate of the same product hit
    UniqueViolation on product_article_image_candidates_one_selected_idx
    -- this function used to be insert-only, so a second run's own index-
    0-selected candidate for a variant collided with the FIRST run's
    still-is_selected=true row for that same (article_id, variant). Fixed
    by unconditionally deleting a variant's existing rows immediately
    before writing its new ones (a no-op delete on a fresh article, a
    real one on a regenerate -- both cases go through the same code
    path, no first-run/second-run branching needed). This test calls the
    function twice for the SAME article_id (simulating a regenerate) and
    confirms: (1) the delete is scoped to (article_id, variant) -- not a
    blanket delete across variants or articles; (2) on each call, the
    delete precedes that variant's own new inserts in execution order,
    not after (a delete-after-insert would just erase what was only just
    written)."""
    conn = _FakeConnection()
    candidates_by_variant = {
        "action_shot": [
            {"key": "article-images/p1/action_shot_gemini_1.png", "url": "https://b/action_shot_gemini_1.png",
             "model_id": "gemini-model", "seed": None},
        ],
    }

    app.store_article_image_candidates(conn, "article-1", candidates_by_variant)
    app.store_article_image_candidates(conn, "article-1", candidates_by_variant)
    assert conn._cursor.candidate_deletes == [("article-1", "action_shot"), ("article-1", "action_shot")]

    # On each run, the delete precedes that run's own inserts in execution order.
    delete_indices = [i for i, (q, _) in enumerate(conn._cursor.executed)
                       if q.startswith("delete from product_article_image_candidates")]
    insert_indices = [i for i, (q, _) in enumerate(conn._cursor.executed)
                       if q.startswith("insert into product_article_image_candidates")]
    assert delete_indices[0] < insert_indices[0]
    assert delete_indices[1] < insert_indices[1]


def test_store_article_image_candidates_locked_variant_skips_delete_and_inserts_unselected():
    """2026-09-06 image-lock fix, Al: "lock that down once we have
    selected one ... bring in new images but never remove the existing
    ones." A variant passed in locked_variants must get NEITHER its prior
    rows deleted NOR any of this run's new candidates marked is_selected
    -- they're appended purely as extra options for an admin to review
    later via select_article_image_candidate, the live pick stays
    whatever it already was."""
    conn = _FakeConnection()
    candidates_by_variant = {
        "action_shot": [
            {"key": "article-images/p1/action_shot_gemini_2.png", "url": "https://b/action_shot_gemini_2.png",
             "model_id": "gemini-model", "seed": None},
            {"key": "article-images/p1/action_shot_gemini_3.png", "url": "https://b/action_shot_gemini_3.png",
             "model_id": "gemini-model", "seed": None},
        ],
    }

    app.store_article_image_candidates(conn, "article-1", candidates_by_variant, locked_variants={"action_shot"})

    assert conn._cursor.candidate_deletes == []
    assert len(conn._cursor.candidate_inserts) == 2
    assert all(p[6] is False for p in conn._cursor.candidate_inserts)


def test_store_article_image_candidates_locked_variant_leaves_prior_rows_untouched_while_unlocked_variant_behaves_as_before():
    """A single call touching both a locked and an unlocked variant --
    confirms locked_variants is applied per-variant, not as an all-or-
    nothing switch for the whole call."""
    conn = _FakeConnection()
    candidates_by_variant = {
        "action_shot": [
            {"key": "new-action-key", "url": "new-action-url", "model_id": "gemini-model", "seed": None},
        ],
        "product_shot": [
            {"key": "new-product-key", "url": "new-product-url", "model_id": "gemini-model", "seed": None},
        ],
    }

    app.store_article_image_candidates(conn, "article-1", candidates_by_variant, locked_variants={"action_shot"})

    assert conn._cursor.candidate_deletes == [("article-1", "product_shot")]
    action_rows = [p for p in conn._cursor.candidate_inserts if p[1] == "action_shot"]
    product_rows = [p for p in conn._cursor.candidate_inserts if p[1] == "product_shot"]
    assert action_rows[0][6] is False  # locked -- new candidate never auto-selected
    assert product_rows[0][6] is True  # unlocked -- unchanged index-0-selected behavior


def test_store_article_image_candidates_skips_delete_for_variant_with_no_new_candidates():
    """A variant that produced ZERO new candidates this run (every Gemini
    AND Stability call failed) must NOT have its prior-run candidates
    deleted -- same "best-effort, don't destroy existing good state on a
    partial failure" posture this module uses everywhere else. Only
    variants present with a non-empty list get cleared."""
    conn = _FakeConnection()
    app.store_article_image_candidates(conn, "article-1", {
        "action_shot": [
            {"key": "article-images/p1/action_shot_gemini_1.png", "url": "https://b/action_shot_gemini_1.png",
             "model_id": "gemini-model", "seed": None},
        ],
        "product_shot": [],
    })

    assert conn._cursor.candidate_deletes == [("article-1", "action_shot")]
    assert len(conn._cursor.candidate_inserts) == 1


# --- generate_article_for_product wired with image plumbing ---

def test_generate_article_for_product_wires_images_when_all_eight_image_args_supplied():
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

    # reference_image_url=None -> generate_article_image_candidates short-
    # circuits to {} without ever touching bedrock_image_client/bedrock_
    # removebg_client/s3_client, so passing simple sentinel objects for
    # those is enough to prove the plumbing reaches generate_article_
    # image_candidates at all (see that function's own "no reference
    # image" test above for its behavior in isolation).
    result = app.generate_article_for_product(
        conn, bedrock, "model-id", "prod-1",
        s3_client=object(), bedrock_image_client=object(), bedrock_removebg_client=object(),
        gemini_auth=_FAKE_GEMINI_AUTH, image_model_id="model-id", removebg_model_id="removebg-model-id",
        gemini_model_id="gemini-model-id", image_bucket="bucket",
    )

    assert result["images_generated"] is False
    insert_params = conn._cursor.inserted[0]
    assert insert_params[15:19] == (None, None, None, None)
    assert insert_params[19] is False
    # candidates_by_variant was {} -- store_article_image_candidates ran
    # as a no-op, no candidate rows inserted.
    assert conn._cursor.candidate_inserts == []


def test_generate_article_for_product_skips_images_when_any_image_arg_missing():
    """s3_client/bedrock_image_client/bedrock_removebg_client/gemini_
    auth/image_model_id/removebg_model_id/gemini_model_id/image_bucket
    must ALL be supplied for the image step to run (see generate_article_
    for_product's own docstring) -- a partially-configured deployment
    (e.g. IMAGE_BUCKET unset) should skip images entirely, not half-run."""
    product_row = (
        "prod-1", "Equinox", None, None, None, None, None, None,
        None, None, None, None, "brand-1", "Storm", None, None,
    )
    video_rows = [("vid-1", "Review", "Channel", "Summary", "transcript")]
    conn = _FakeConnection(product_row=product_row, video_rows=video_rows)
    bedrock = _FakeBedrockClient(json.dumps(_VALID_ARTICLE_JSON))

    result = app.generate_article_for_product(
        conn, bedrock, "model-id", "prod-1",
        s3_client=object(), bedrock_image_client=object(), bedrock_removebg_client=object(),
        gemini_auth=_FAKE_GEMINI_AUTH, image_model_id="model-id", removebg_model_id="removebg-model-id",
        gemini_model_id="gemini-model-id", image_bucket=None,  # missing
    )

    assert result["images_generated"] is False
    # reference_image_url query never even fired -- confirms the whole
    # image branch was skipped, not attempted-and-quietly-swallowed.
    assert not any(q.startswith("select coalesce(") for q, _ in conn._cursor.executed)


def test_generate_article_for_product_skips_images_when_gemini_auth_missing():
    """Same as above but specifically the NEW gemini_auth arg missing --
    confirms it's actually part of the all-or-nothing gate, not silently
    optional/backward-compatible (unlike handler()'s own softer "fetch/
    mint failed -> None, Gemini candidates just get skipped inside
    generate_article_image_candidates" posture -- this is the OUTER gate
    that decides whether to call that function at all)."""
    product_row = (
        "prod-1", "Equinox", None, None, None, None, None, None,
        None, None, None, None, "brand-1", "Storm", None, None,
    )
    video_rows = [("vid-1", "Review", "Channel", "Summary", "transcript")]
    conn = _FakeConnection(product_row=product_row, video_rows=video_rows)
    bedrock = _FakeBedrockClient(json.dumps(_VALID_ARTICLE_JSON))

    result = app.generate_article_for_product(
        conn, bedrock, "model-id", "prod-1",
        s3_client=object(), bedrock_image_client=object(), bedrock_removebg_client=object(),
        gemini_auth=None,  # missing
        image_model_id="model-id", removebg_model_id="removebg-model-id",
        gemini_model_id="gemini-model-id", image_bucket="bucket",
    )

    assert result["images_generated"] is False
    assert not any(q.startswith("select coalesce(") for q, _ in conn._cursor.executed)


# --- generate_article_for_product: image-lock-down (2026-09-06, Al's ask)
# -- these patch app.generate_article_image_candidates directly (same
# _HandlerPatchGuard save/restore pattern used for the handler tests
# above) rather than threading a real reference-image-url + fake Gemini/
# Stability calls all the way through, since the thing under test here
# is what generate_article_for_product DOES with whatever candidates_by_
# variant it gets back, not the image-generation call itself (already
# covered by the generate_article_image_candidates tests elsewhere in
# this file). ---

def _install_fake_image_candidates(guard, candidates_by_variant):
    guard.set(app, "generate_article_image_candidates",
               lambda *args, **kwargs: candidates_by_variant)


_ALL_EIGHT_IMAGE_KWARGS = dict(
    s3_client=object(), bedrock_image_client=object(), bedrock_removebg_client=object(),
    gemini_auth=_FAKE_GEMINI_AUTH, image_model_id="model-id", removebg_model_id="removebg-model-id",
    gemini_model_id="gemini-model-id", image_bucket="bucket",
)


def test_generate_article_for_product_preserves_locked_image_and_appends_new_candidate_unselected():
    """The core regression test for Al's report: "I am seeing article
    images get regenerated after I have already selected one that I
    like." action_shot is already locked (has an is_selected=true row);
    this run produces a NEW action_shot candidate too -- it must be
    appended to the candidates table (not lost) but must NOT become the
    live image. product_shot has no existing selection, so its own new
    candidate becomes the live pick as usual."""
    product_row = (
        "prod-1", "Equinox Solid", "Blue", "reactive", "hybrid",
        "R2S Hybrid", True, "500/1000 Abralon",
        12, 17, "2024-01-01", "A great ball.",
        "brand-1", "Storm", "Sonar", "asymmetric",
    )
    video_rows = [("vid-1", "Review", "Some Channel", "A summary.", "transcript text")]
    conn = _FakeConnection(
        product_row=product_row, video_rows=video_rows,
        existing_article={
            "id": "article-1", "performance_summary": "Old summary.", "hook": "Old hook.",
            "action_shot_image_key": "article-images/prod-1/action_shot_gemini_1.png",
            "action_shot_image_url": "https://b/action_shot_gemini_1.png",
            "product_shot_image_key": None, "product_shot_image_url": None,
        },
        selected_variants=["action_shot"],
    )
    bedrock = _FakeBedrockClient(json.dumps(_VALID_ARTICLE_JSON))
    fake_candidates = {
        "action_shot": [{"key": "article-images/prod-1/action_shot_gemini_2.png",
                          "url": "https://b/action_shot_gemini_2.png", "model_id": "gemini-model", "seed": None}],
        "product_shot": [{"key": "article-images/prod-1/product_shot_gemini_1.png",
                           "url": "https://b/product_shot_gemini_1.png", "model_id": "gemini-model", "seed": None}],
    }

    guard = _HandlerPatchGuard()
    try:
        _install_fake_image_candidates(guard, fake_candidates)
        result = app.generate_article_for_product(conn, bedrock, "model-id", "prod-1", **_ALL_EIGHT_IMAGE_KWARGS)
    finally:
        guard.restore()

    assert result["images_generated"] is True
    insert_params = conn._cursor.inserted[0]
    # action_shot stayed the OLD, already-selected value -- not the new
    # candidate this run produced. (Index 14 is visual_theme.)
    assert insert_params[15] == "article-images/prod-1/action_shot_gemini_1.png"
    assert insert_params[16] == "https://b/action_shot_gemini_1.png"
    # product_shot had no prior selection, so its new candidate DID become
    # the live pick.
    assert insert_params[17] == "article-images/prod-1/product_shot_gemini_1.png"
    assert insert_params[18] == "https://b/product_shot_gemini_1.png"

    # The new action_shot candidate was still appended to the candidates
    # table (never lost, per Al's "bring in new images" half of the ask)
    # -- just not selected, and its prior row was never deleted.
    action_inserts = [p for p in conn._cursor.candidate_inserts if p[1] == "action_shot"]
    assert len(action_inserts) == 1
    assert action_inserts[0][3] == "article-images/prod-1/action_shot_gemini_2.png"
    assert action_inserts[0][6] is False
    assert ("article-1", "action_shot") not in conn._cursor.candidate_deletes

    # product_shot, never locked, kept its normal clear-then-select-index-0
    # behavior.
    assert ("article-1", "product_shot") in conn._cursor.candidate_deletes
    product_inserts = [p for p in conn._cursor.candidate_inserts if p[1] == "product_shot"]
    assert product_inserts[0][6] is True


def test_generate_article_for_product_preserves_existing_image_on_partial_failure_for_unlocked_variant():
    """A variant with NO existing selection (never locked) but that also
    produced zero new candidates this run (a partial generation failure,
    e.g. every Gemini call for that shot failed) must still keep its
    existing live image rather than having it nulled out -- same "don't
    destroy existing good state on a partial failure" posture this module
    already applies to the candidates table itself."""
    product_row = (
        "prod-1", "Equinox Solid", "Blue", "reactive", "hybrid",
        "R2S Hybrid", True, "500/1000 Abralon",
        12, 17, "2024-01-01", "A great ball.",
        "brand-1", "Storm", "Sonar", "asymmetric",
    )
    video_rows = [("vid-1", "Review", "Some Channel", "A summary.", "transcript text")]
    conn = _FakeConnection(
        product_row=product_row, video_rows=video_rows,
        existing_article={
            "id": "article-1", "performance_summary": "Old summary.", "hook": "Old hook.",
            "action_shot_image_key": None, "action_shot_image_url": None,
            "product_shot_image_key": "article-images/prod-1/product_shot_gemini_1.png",
            "product_shot_image_url": "https://b/product_shot_gemini_1.png",
        },
        selected_variants=[],  # nothing actually locked -- pure partial-failure case
    )
    bedrock = _FakeBedrockClient(json.dumps(_VALID_ARTICLE_JSON))
    fake_candidates = {
        "action_shot": [{"key": "article-images/prod-1/action_shot_gemini_1.png",
                          "url": "https://b/action_shot_gemini_1.png", "model_id": "gemini-model", "seed": None}],
        "product_shot": [],  # every candidate call for this shot failed this run
    }

    guard = _HandlerPatchGuard()
    try:
        _install_fake_image_candidates(guard, fake_candidates)
        app.generate_article_for_product(conn, bedrock, "model-id", "prod-1", **_ALL_EIGHT_IMAGE_KWARGS)
    finally:
        guard.restore()

    insert_params = conn._cursor.inserted[0]
    # (Index 14 is visual_theme.)
    assert insert_params[15] == "article-images/prod-1/action_shot_gemini_1.png"  # fresh pick
    assert insert_params[17] == "article-images/prod-1/product_shot_gemini_1.png"  # preserved, not nulled
    assert insert_params[18] == "https://b/product_shot_gemini_1.png"


def test_generate_article_for_product_skips_locked_variants_lookup_when_regenerate_images_false():
    """A text-only regenerate must never even query for locked variants --
    it can't touch images either way (update_article_text_only's own
    UPDATE structurally omits all image columns), so there's no reason to
    ask. Confirms the gate is on regenerate_images, not just on whether an
    existing article/selections exist."""
    product_row = (
        "prod-1", "Equinox Solid", "Blue", "reactive", "hybrid",
        "R2S Hybrid", True, "500/1000 Abralon",
        12, 17, "2024-01-01", "A great ball.",
        "brand-1", "Storm", "Sonar", "asymmetric",
    )
    video_rows = [("vid-1", "Review", "Some Channel", "A summary.", "transcript text")]
    conn = _FakeConnection(
        product_row=product_row, video_rows=video_rows,
        existing_article={"id": "article-1", "performance_summary": "Old.", "hook": "Old hook."},
        selected_variants=["action_shot"],  # would be locked IF this ever got queried
    )
    bedrock = _FakeBedrockClient(json.dumps(_VALID_ARTICLE_JSON))

    result = app.generate_article_for_product(conn, bedrock, "model-id", "prod-1",
                                                regenerate_text=True, regenerate_images=False)

    assert result["images_generated"] is False
    assert not any(
        q.startswith("select variant from product_article_image_candidates")
        for q, _ in conn._cursor.executed
    )
    assert len(conn._cursor.text_only_updates) == 1


# --- store_article: images param wiring ---

def test_store_article_with_images_sets_images_generated_at_flag_true():
    conn = _FakeConnection()
    images = {
        "action_shot_image_key": "article-images/prod-1/action_shot.png",
        "action_shot_image_url": "https://bucket.s3.amazonaws.com/article-images/prod-1/action_shot.png",
    }
    app.store_article(conn, "prod-1", dict(_VALID_ARTICLE_JSON), [], [], images=images)

    insert_params = conn._cursor.inserted[0]
    # Index 14 is visual_theme (migration 029) -- absent from
    # _VALID_ARTICLE_JSON, so None.
    assert insert_params[14] is None
    assert insert_params[15] == images["action_shot_image_key"]
    assert insert_params[16] == images["action_shot_image_url"]
    assert insert_params[17] is None  # product_shot_image_key not in this partial dict
    assert insert_params[18] is None
    # has_images (the images_generated_at case-when's boolean param) is
    # True even though only ONE of the two images actually succeeded --
    # see 023_product_article_images.sql's own column comment: "at least
    # one", not "both".
    assert insert_params[19] is True


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
