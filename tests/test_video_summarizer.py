"""
Tests for src/video_summarizer/app.py.

Manual-runner pattern, run standalone via
`python3 tests/test_video_summarizer.py`.

Trimmed down as part of the "split the architecture" change: everything
that talked to YouTube (watch-page fetching, caption-track parsing,
transcript XML parsing, and their tests) moved to
src/video_transcript_fetcher/app.py and
tests/test_video_transcript_fetcher.py, running as a separate, non-VPC
Lambda -- see that module's docstring for the full reasoning (real,
live-tested evidence that this function's old VPC/NAT-gateway network
path got different treatment from YouTube's consumer-facing surface than
a residential IP did). What's left here is DB + Bedrock only: this
function now consumes an already-fetched transcript (or an empty one plus
a note explaining why) from VideoTranscriptResultQueue, and its job is
just to summarize (if there's a transcript) and write the result to the
product_videos row.

The Bedrock request/response shape IS accurate to AWS's published
Anthropic-on-Bedrock wire format, but was also never exercised against a
real Bedrock endpoint in this sandbox (no outbound network access here --
verified separately via real `aws lambda invoke` calls the user ran).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "video_summarizer"))

import app  # noqa: E402


# --- build_summary_prompt ---

def test_build_summary_prompt_includes_key_context():
    prompt = app.build_summary_prompt("Absolute", "Storm", "Storm Absolute Review", "great ball, strong hook")
    assert "Absolute" in prompt
    assert "Storm" in prompt
    assert "Storm Absolute Review" in prompt
    assert "great ball, strong hook" in prompt


def test_build_summary_prompt_truncates_long_transcript():
    long_transcript = "word " * 10000  # far past DEFAULT_TRANSCRIPT_CHAR_LIMIT
    prompt = app.build_summary_prompt("Absolute", "Storm", "title", long_transcript)
    # The truncated transcript (not the full one) should appear in the prompt.
    assert len(prompt) < len(long_transcript) + 500


# --- summarize_transcript: fake Bedrock client ---

class _FakeBedrockBody:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data


class _FakeBedrockClient:
    """response_text: same reply for every call (existing single-Bedrock-
    call tests). responses: a list consumed in order (needed once
    _process_one can make two calls -- summarize_transcript, then
    generate_video_reviews_rollup). fail_on_call_number: 1-indexed,
    raises instead of returning on that call -- used to test that a rollup
    failure doesn't undo the video's own already-saved summary."""

    def __init__(self, response_text: str = None, responses: list = None, fail_on_call_number: int = None):
        self.response_text = response_text
        self.responses = list(responses) if responses is not None else None
        self.fail_on_call_number = fail_on_call_number
        self.calls = []

    def invoke_model(self, modelId, contentType, accept, body):
        self.calls.append({"modelId": modelId, "contentType": contentType, "accept": accept, "body": body})
        if self.fail_on_call_number is not None and len(self.calls) == self.fail_on_call_number:
            raise RuntimeError("simulated Bedrock failure")
        text = self.responses.pop(0) if self.responses is not None else self.response_text
        return {"body": _FakeBedrockBody({"content": [{"text": text}]})}


def test_summarize_transcript_returns_model_text_and_calls_expected_model():
    """Uses the Global cross-Region inference profile ID (see
    DEFAULT_BEDROCK_MODEL_ID's comment) rather than a bare on-demand model
    ID -- summarize_transcript just passes whatever modelId it's given
    straight through to invoke_model, so this only tests that pass-through,
    not the specific string."""
    client = _FakeBedrockClient("This ball hooks strong and clears the front of the lane.")
    summary = app.summarize_transcript(client, "global.anthropic.claude-haiku-4-5-20251001-v1:0",
                                        "Absolute", "Storm", "Storm Absolute Review", "great ball")

    assert summary == "This ball hooks strong and clears the front of the lane."
    assert len(client.calls) == 1
    assert client.calls[0]["modelId"] == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    sent_body = json.loads(client.calls[0]["body"])
    assert sent_body["anthropic_version"] == "bedrock-2023-05-31"
    assert sent_body["messages"][0]["role"] == "user"
    assert "great ball" in sent_body["messages"][0]["content"]


# --- build_rollup_prompt: different prompt for 1 vs 2+ summaries -- see
# module docstring's SUMMARY OF SUMMARIES section for why a single source
# still gets rewritten rather than copied verbatim ---

def test_build_rollup_prompt_single_summary_rewrites_standalone():
    prompt = app.build_rollup_prompt("Absolute", "Storm", ["In this video, the reviewer notes strong hook."])
    assert "In this video, the reviewer notes strong hook." in prompt
    assert "Rewrite it as a standalone" in prompt
    assert "remove any references" in prompt


def test_build_rollup_prompt_multiple_summaries_synthesizes():
    summaries = ["Strong hook, clears the front.", "Smooth and predictable on medium oil."]
    prompt = app.build_rollup_prompt("Absolute", "Storm", summaries)
    assert "2 independent review summaries" in prompt
    assert "1. Strong hook, clears the front." in prompt
    assert "2. Smooth and predictable on medium oil." in prompt
    assert "Synthesize them" in prompt
    assert "notable disagreements" in prompt


def test_generate_video_reviews_rollup_returns_model_text():
    client = _FakeBedrockClient("Reviewers agree this ball hooks hard on medium oil.")
    rollup = app.generate_video_reviews_rollup(
        client, "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "Absolute", "Storm", ["Strong hook.", "Hooks a lot."],
    )
    assert rollup == "Reviewers agree this ball hooks hard on medium oil."
    assert len(client.calls) == 1


# --- _process_one / handler orchestration: fake DB + Bedrock, job now
# carries transcript/transcript_note directly (no YouTube fetch here) ---

class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._last_result = None
        self._rows = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        params = params or []
        q = " ".join(query.split())

        if q.startswith("select pv.id, pv.youtube_video_id"):
            (product_video_id,) = params
            row = self.db["product_videos"].get(product_video_id)
            if row is None:
                self._last_result = None
                self.description = None
            else:
                self._last_result = (
                    row["id"], row["youtube_video_id"], row["title"], row["status"],
                    row["product_id"], row["product_name"], row["brand_name"],
                )
                self.description = [
                    ("id",), ("youtube_video_id",), ("title",), ("status",),
                    ("product_id",), ("product_name",), ("brand_name",),
                ]

        elif q.startswith("update product_videos set transcript"):
            transcript, note, summary, product_video_id = params
            row = self.db["product_videos"][product_video_id]
            row["transcript"] = transcript
            row["transcript_note"] = note
            row["summary"] = summary
            self._last_result = None

        elif q.startswith("select summary from product_videos"):
            product_id = params[0]
            matches = [
                v for v in self.db["product_videos"].values()
                if v["product_id"] == product_id and v["status"] == "approved" and v.get("summary") is not None
            ]
            matches.sort(key=lambda v: (v.get("created_at", ""), v["id"]))
            self._rows = [(v["summary"],) for v in matches]

        elif q.startswith("update products set video_reviews_summary"):
            rollup_text, video_count, product_id = params
            row = self.db["products"][product_id]
            row["video_reviews_summary"] = rollup_text
            row["video_reviews_summary_video_count"] = video_count
            row["video_reviews_summary_updated_at"] = "now"
            self._last_result = None

        else:
            raise NotImplementedError(f"FakeCursor doesn't support: {q}")

    def fetchone(self):
        return self._last_result

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, db):
        self.db = db
        self.committed = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def _fake_db_with_approved_video():
    return {
        "product_videos": {
            "vid-1": {
                "id": "vid-1", "youtube_video_id": "abc123", "title": "Storm Absolute Review",
                "status": "approved", "product_id": "prod-1",
                "product_name": "Absolute", "brand_name": "Storm",
            },
        },
        "products": {
            "prod-1": {
                "video_reviews_summary": None,
                "video_reviews_summary_video_count": 0,
                "video_reviews_summary_updated_at": None,
            },
        },
    }


# --- fetch_approved_video_summaries / store_rollup / refresh_video_reviews_rollup:
# the DB-facing rollup functions, tested directly (not just through _process_one) ---

def test_fetch_approved_video_summaries_filters_status_and_nonnull():
    db = _fake_db_with_approved_video()
    db["product_videos"]["vid-1"]["summary"] = "Strong hook."
    db["product_videos"]["vid-2"] = {
        "id": "vid-2", "product_id": "prod-1", "status": "approved", "summary": "Smooth on medium oil.",
    }
    db["product_videos"]["vid-3-rejected"] = {
        "id": "vid-3-rejected", "product_id": "prod-1", "status": "rejected", "summary": "Should be excluded.",
    }
    db["product_videos"]["vid-4-no-summary"] = {
        "id": "vid-4-no-summary", "product_id": "prod-1", "status": "approved", "summary": None,
    }
    db["product_videos"]["vid-5-other-product"] = {
        "id": "vid-5-other-product", "product_id": "prod-other", "status": "approved", "summary": "Different ball entirely.",
    }
    conn = FakeConnection(db)

    summaries = app.fetch_approved_video_summaries(conn, "prod-1")

    assert summaries == ["Strong hook.", "Smooth on medium oil."]


def test_store_rollup_writes_and_commits():
    db = _fake_db_with_approved_video()
    conn = FakeConnection(db)

    app.store_rollup(conn, "prod-1", "Reviewers agree this ball hooks hard.", 2)

    assert db["products"]["prod-1"]["video_reviews_summary"] == "Reviewers agree this ball hooks hard."
    assert db["products"]["prod-1"]["video_reviews_summary_video_count"] == 2
    assert db["products"]["prod-1"]["video_reviews_summary_updated_at"] == "now"
    assert conn.committed is True


def test_refresh_video_reviews_rollup_returns_no_summaries_when_none_exist():
    """A real, if rare, guard -- see the function's docstring: this
    shouldn't happen in _process_one's actual flow (it only calls this
    right after saving a new summary), but fetch_approved_video_summaries'
    filters are independent of what just got written, so a race is
    possible."""
    db = _fake_db_with_approved_video()
    conn = FakeConnection(db)
    bedrock = _FakeBedrockClient("should not be called")

    result = app.refresh_video_reviews_rollup(conn, bedrock, "model-id", "prod-1", "Absolute", "Storm")

    assert result == {"product_id": "prod-1", "rollup_regenerated": False, "reason": "no_summaries"}
    assert len(bedrock.calls) == 0


def test_refresh_video_reviews_rollup_success():
    db = _fake_db_with_approved_video()
    db["product_videos"]["vid-1"]["summary"] = "Strong hook."
    conn = FakeConnection(db)
    bedrock = _FakeBedrockClient("This ball is a strong hooker.")

    result = app.refresh_video_reviews_rollup(conn, bedrock, "model-id", "prod-1", "Absolute", "Storm")

    assert result == {"product_id": "prod-1", "rollup_regenerated": True, "video_count": 1}
    assert db["products"]["prod-1"]["video_reviews_summary"] == "This ball is a strong hooker."
    assert db["products"]["prod-1"]["video_reviews_summary_video_count"] == 1


def test_process_one_summarizes_when_transcript_present(monkeypatch):
    """A successful summarization now also regenerates the product-level
    rollup (see refresh_video_reviews_rollup) -- two Bedrock calls total:
    summarize_transcript for the video itself, then generate_video_
    reviews_rollup since this product now has 1 approved summary."""
    db = _fake_db_with_approved_video()
    conn = FakeConnection(db)
    monkeypatch.setattr(app, "get_db_connection", lambda: conn)

    bedrock = _FakeBedrockClient(responses=[
        "Strong hook, clears the front of the lane, recommended for medium-heavy oil.",
        "This ball hooks strong and clears the front of the lane.",
    ])
    job = {"product_video_id": "vid-1", "transcript": "great ball, strong hook", "transcript_note": None}

    result = app._process_one(job, bedrock)

    assert result["summarized"] is True
    assert result["transcript_note"] is None
    assert result["rollup_regenerated"] is True
    assert db["product_videos"]["vid-1"]["summary"] == "Strong hook, clears the front of the lane, recommended for medium-heavy oil."
    assert db["product_videos"]["vid-1"]["transcript"] == "great ball, strong hook"
    assert db["products"]["prod-1"]["video_reviews_summary"] == "This ball hooks strong and clears the front of the lane."
    assert db["products"]["prod-1"]["video_reviews_summary_video_count"] == 1
    assert conn.committed is True
    assert conn.closed is True
    assert len(bedrock.calls) == 2


def test_process_one_rollup_failure_does_not_undo_video_summary(monkeypatch):
    """The soft-fail contract (see module docstring): a Bedrock error on
    the rollup step must not affect the video's own already-saved summary,
    and must not raise out of _process_one (which would land the whole SQS
    message in a retry/DLQ over a purely cosmetic rollup failure)."""
    db = _fake_db_with_approved_video()
    conn = FakeConnection(db)
    monkeypatch.setattr(app, "get_db_connection", lambda: conn)

    bedrock = _FakeBedrockClient(
        responses=["Strong hook, clears the front of the lane."],
        fail_on_call_number=2,  # the rollup call
    )
    job = {"product_video_id": "vid-1", "transcript": "great ball, strong hook", "transcript_note": None}

    result = app._process_one(job, bedrock)

    assert result["summarized"] is True
    assert result["rollup_regenerated"] is False
    assert db["product_videos"]["vid-1"]["summary"] == "Strong hook, clears the front of the lane."
    assert db["products"]["prod-1"]["video_reviews_summary"] is None  # untouched
    assert conn.committed is True  # the video's own row still committed
    assert conn.closed is True


def test_process_one_skips_bedrock_when_no_transcript(monkeypatch):
    """The whole point of transcript_note as a non-error, best-effort
    outcome (see video_transcript_fetcher's module docstring): no
    transcript means no Bedrock call, but the row still gets updated with
    the note so it's visible in the DB/admin UI, and the job doesn't fail."""
    db = _fake_db_with_approved_video()
    conn = FakeConnection(db)
    monkeypatch.setattr(app, "get_db_connection", lambda: conn)

    bedrock = _FakeBedrockClient("should not be called")
    job = {"product_video_id": "vid-1", "transcript": "", "transcript_note": "no_captions_available"}

    result = app._process_one(job, bedrock)

    assert result["summarized"] is False
    assert result["rollup_regenerated"] is False
    assert result["transcript_note"] == "no_captions_available"
    assert db["product_videos"]["vid-1"]["summary"] is None
    assert len(bedrock.calls) == 0
    assert conn.committed is True


def test_process_one_skips_when_row_not_approved(monkeypatch):
    db = _fake_db_with_approved_video()
    db["product_videos"]["vid-1"]["status"] = "rejected"
    conn = FakeConnection(db)
    monkeypatch.setattr(app, "get_db_connection", lambda: conn)

    bedrock = _FakeBedrockClient("should not be called")
    job = {"product_video_id": "vid-1", "transcript": "great ball", "transcript_note": None}

    result = app._process_one(job, bedrock)

    assert result["skipped"] == "status_is_rejected"
    assert len(bedrock.calls) == 0


def test_process_one_skips_when_row_missing(monkeypatch):
    db = _fake_db_with_approved_video()
    conn = FakeConnection(db)
    monkeypatch.setattr(app, "get_db_connection", lambda: conn)

    bedrock = _FakeBedrockClient("should not be called")
    job = {"product_video_id": "does-not-exist", "transcript": "", "transcript_note": None}

    result = app._process_one(job, bedrock)

    assert result["skipped"] == "not_found"
    assert len(bedrock.calls) == 0


if __name__ == "__main__":
    class _MonkeyPatch:
        def __init__(self):
            self._sets = []
            self._env_sets = []

        def setattr(self, obj, name, value):
            self._sets.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def setenv(self, name, value):
            had_it = name in os.environ
            self._env_sets.append((name, os.environ.get(name), had_it))
            os.environ[name] = value

        def delenv(self, name, raising=True):
            had_it = name in os.environ
            self._env_sets.append((name, os.environ.get(name), had_it))
            if had_it:
                del os.environ[name]
            elif raising:
                raise KeyError(name)

        def undo(self):
            for obj, name, value in reversed(self._sets):
                setattr(obj, name, value)
            for name, value, had_it in reversed(self._env_sets):
                if had_it:
                    os.environ[name] = value
                else:
                    os.environ.pop(name, None)

    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for name, t in tests:
        mp = _MonkeyPatch()
        try:
            if "monkeypatch" in t.__code__.co_varnames[: t.__code__.co_argcount]:
                t(mp)
            else:
                t()
            print(f"PASS: {name}")
            passed += 1
        finally:
            mp.undo()
    print(f"\n{passed}/{len(tests)} tests passed")
