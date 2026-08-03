"""
Tests for src/video_summarizer/app.py.

Manual-runner pattern, run standalone via
`python3 tests/test_video_summarizer.py`.

Honesty note (see module docstring in app.py): this file originally tested
a watch-page-scraping approach that a live smoke test found to be
unreliable (the same URL returned a real, full-size, non-consent-wall page
that simply didn't have the caption data inlined on one fetch vs. another).
Replaced with YouTube's dedicated timedtext?type=list endpoint, which is
believed more reliable but is, like the approach it replaced, NOT verified
against a live call this session (zero outbound network access in this
sandbox). Confirm the real XML shape via a manual curl before fully
trusting this. The Bedrock request/response shape IS accurate to AWS's
published Anthropic-on-Bedrock wire format, but was also never exercised
against a real Bedrock endpoint this session.
"""
import json
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "video_summarizer"))

import app  # noqa: E402


# --- parse_caption_track_list / pick_caption_track ---

SAMPLE_TRACK_LIST_XML = (
    '<?xml version="1.0" encoding="utf-8" ?><transcript_list docid="abc123">'
    '<track id="0" name="" lang_code="en" lang_original="English" lang_translated="English" kind="asr"/>'
    '<track id="1" name="" lang_code="es" lang_original="Spanish" lang_translated="Spanish"/>'
    '</transcript_list>'
)

EMPTY_TRACK_LIST_XML = '<?xml version="1.0" encoding="utf-8" ?><transcript_list docid="abc123"></transcript_list>'


def test_parse_caption_track_list_extracts_tracks():
    tracks = app.parse_caption_track_list(SAMPLE_TRACK_LIST_XML)
    assert len(tracks) == 2
    assert tracks[0]["lang_code"] == "en"
    assert tracks[0]["kind"] == "asr"
    assert tracks[1]["lang_code"] == "es"
    assert tracks[1]["kind"] is None


def test_parse_caption_track_list_returns_empty_list_when_no_tracks():
    assert app.parse_caption_track_list(EMPTY_TRACK_LIST_XML) == []


def test_parse_caption_track_list_returns_empty_list_for_blank_input():
    assert app.parse_caption_track_list("") == []
    assert app.parse_caption_track_list("   ") == []


def test_parse_caption_track_list_returns_empty_list_for_malformed_xml():
    assert app.parse_caption_track_list("<transcript_list><track unclosed") == []


def test_pick_caption_track_prefers_human_over_asr():
    tracks = [
        {"lang_code": "en", "kind": "asr", "name": "asr-track"},
        {"lang_code": "en", "kind": None, "name": "human-track"},
    ]
    assert app.pick_caption_track(tracks)["name"] == "human-track"


def test_pick_caption_track_falls_back_to_asr_english_when_no_human_track():
    tracks = [{"lang_code": "en", "kind": "asr", "name": "asr-track"}]
    assert app.pick_caption_track(tracks)["name"] == "asr-track"


def test_pick_caption_track_falls_back_to_first_when_no_english():
    tracks = [{"lang_code": "es", "kind": None, "name": "spanish"}, {"lang_code": "fr", "kind": None, "name": "french"}]
    assert app.pick_caption_track(tracks)["name"] == "spanish"


def test_pick_caption_track_returns_none_for_empty_list():
    assert app.pick_caption_track([]) is None


# --- build_transcript_url ---

def test_build_transcript_url_includes_kind_for_asr_track():
    url, params = app.build_transcript_url("abc123", {"lang_code": "en", "kind": "asr", "name": ""})
    assert url == app.TIMEDTEXT_BASE_URL
    assert params == {"v": "abc123", "lang": "en", "kind": "asr"}


def test_build_transcript_url_omits_kind_for_human_track():
    url, params = app.build_transcript_url("abc123", {"lang_code": "en", "kind": None, "name": ""})
    assert "kind" not in params


def test_build_transcript_url_includes_name_when_present():
    _, params = app.build_transcript_url("abc123", {"lang_code": "en", "kind": None, "name": "custom"})
    assert params["name"] == "custom"


# --- list_caption_tracks: retry-on-empty behavior ---

def test_list_caption_tracks_returns_immediately_when_first_attempt_has_tracks():
    real_fetch = app.fetch_caption_track_list_xml
    real_sleep = app.time.sleep
    calls = []
    app.fetch_caption_track_list_xml = lambda video_id: SAMPLE_TRACK_LIST_XML
    app.time.sleep = lambda s: calls.append(s)
    try:
        tracks = app.list_caption_tracks("abc123")
        assert len(tracks) == 2
        assert calls == []  # no retry needed, so no sleep
    finally:
        app.fetch_caption_track_list_xml = real_fetch
        app.time.sleep = real_sleep


def test_list_caption_tracks_retries_once_then_succeeds():
    real_fetch = app.fetch_caption_track_list_xml
    real_sleep = app.time.sleep
    responses = [EMPTY_TRACK_LIST_XML, SAMPLE_TRACK_LIST_XML]
    app.fetch_caption_track_list_xml = lambda video_id: responses.pop(0)
    app.time.sleep = lambda s: None
    try:
        tracks = app.list_caption_tracks("abc123", max_attempts=2, delay_seconds=0)
        assert len(tracks) == 2
        assert responses == []  # both canned responses were consumed
    finally:
        app.fetch_caption_track_list_xml = real_fetch
        app.time.sleep = real_sleep


def test_list_caption_tracks_gives_up_after_max_attempts():
    real_fetch = app.fetch_caption_track_list_xml
    real_sleep = app.time.sleep
    app.fetch_caption_track_list_xml = lambda video_id: EMPTY_TRACK_LIST_XML
    app.time.sleep = lambda s: None
    try:
        tracks = app.list_caption_tracks("abc123", max_attempts=2, delay_seconds=0)
        assert tracks == []
    finally:
        app.fetch_caption_track_list_xml = real_fetch
        app.time.sleep = real_sleep


# --- parse_transcript_xml ---

SAMPLE_TRANSCRIPT_XML = (
    '<?xml version="1.0" encoding="utf-8" ?><transcript>'
    '<text start="0.0" dur="2.0">Alright let&#39;s check out this ball</text>'
    '<text start="2.0" dur="3.5">the hook is pretty strong</text>'
    '</transcript>'
)


def test_parse_transcript_xml_concatenates_and_unescapes():
    text = app.parse_transcript_xml(SAMPLE_TRANSCRIPT_XML)
    assert text == "Alright let's check out this ball the hook is pretty strong"


def test_parse_transcript_xml_empty_for_blank_input():
    assert app.parse_transcript_xml("") == ""
    assert app.parse_transcript_xml("   ") == ""


def test_parse_transcript_xml_malformed_returns_empty():
    assert app.parse_transcript_xml("<transcript><text>unclosed") == ""


# --- get_transcript: end-to-end wiring ---

def test_get_transcript_extracts_real_transcript_end_to_end():
    real_list = app.fetch_caption_track_list_xml
    real_fetch_xml = app.fetch_transcript_xml
    app.fetch_caption_track_list_xml = lambda video_id: SAMPLE_TRACK_LIST_XML
    app.fetch_transcript_xml = lambda url, params: SAMPLE_TRANSCRIPT_XML
    try:
        transcript, note = app.get_transcript("abc123")
        assert note is None
        assert transcript == "Alright let's check out this ball the hook is pretty strong"
    finally:
        app.fetch_caption_track_list_xml = real_list
        app.fetch_transcript_xml = real_fetch_xml


def test_get_transcript_no_captions_when_genuinely_absent():
    real_list = app.fetch_caption_track_list_xml
    real_sleep = app.time.sleep
    app.fetch_caption_track_list_xml = lambda video_id: EMPTY_TRACK_LIST_XML
    app.time.sleep = lambda s: None
    try:
        transcript, note = app.get_transcript("abc123")
        assert transcript == ""
        assert note == "no_captions_available"
    finally:
        app.fetch_caption_track_list_xml = real_list
        app.time.sleep = real_sleep


def test_get_transcript_empty_transcript_after_tracks_listed():
    """Tracks are listed but the actual transcript fetch comes back empty
    -- a different, more specific note than 'no_captions_available' so this
    failure mode is distinguishable in the DB/CloudWatch."""
    real_list = app.fetch_caption_track_list_xml
    real_fetch_xml = app.fetch_transcript_xml
    app.fetch_caption_track_list_xml = lambda video_id: SAMPLE_TRACK_LIST_XML
    app.fetch_transcript_xml = lambda url, params: ""
    try:
        transcript, note = app.get_transcript("abc123")
        assert transcript == ""
        assert note == "captions_listed_but_transcript_fetch_returned_empty"
    finally:
        app.fetch_caption_track_list_xml = real_list
        app.fetch_transcript_xml = real_fetch_xml


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
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.calls = []

    def invoke_model(self, modelId, contentType, accept, body):
        self.calls.append({"modelId": modelId, "contentType": contentType, "accept": accept, "body": body})
        return {"body": _FakeBedrockBody({"content": [{"text": self.response_text}]})}


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


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"PASS: {t.__name__}")
    print(f"\n{passed}/{len(tests)} tests passed")
