"""
Tests for src/video_summarizer/app.py.

Manual-runner pattern, run standalone via
`python3 tests/test_video_summarizer.py`.

Honesty note (see module docstring in app.py): the captionTracks JSON shape
and timedtext XML shape below are built from widely-documented (if
unofficial) YouTube behavior, NOT a live capture -- this sandbox has zero
outbound network access, so nothing here could be verified against a real
YouTube watch page this session. Confirm both shapes against a real video
before trusting this in production, same discipline this project applied
to the commercebuild archived-page template (which turned out to be stale
after a site redesign -- assumed-but-unverified structure has burned this
project before). The Bedrock request/response shape IS accurate to AWS's
published Anthropic-on-Bedrock wire format, but was also never exercised
against a real Bedrock endpoint this session.
"""
import json
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "video_summarizer"))

import app  # noqa: E402


# --- parse_caption_tracks / pick_caption_track ---

FAKE_WATCH_PAGE_WITH_CAPTIONS = """
<html><body><script>var ytInitialPlayerResponse = {"captions":{"playerCaptionsTracklistRenderer":
{"captionTracks":[
  {"baseUrl":"https://www.youtube.com/api/timedtext?v=abc123\\u0026lang=en", "name":{"simpleText":"English"}, "languageCode":"en", "kind":"asr"},
  {"baseUrl":"https://www.youtube.com/api/timedtext?v=abc123\\u0026lang=es", "name":{"simpleText":"Spanish"}, "languageCode":"es"}
]}}};</script></body></html>
"""

FAKE_WATCH_PAGE_NO_CAPTIONS = "<html><body><script>var ytInitialPlayerResponse = {\"captions\":{}};</script></body></html>"


def test_parse_caption_tracks_extracts_array():
    tracks = app.parse_caption_tracks(FAKE_WATCH_PAGE_WITH_CAPTIONS)
    assert len(tracks) == 2
    assert tracks[0]["languageCode"] == "en"
    assert tracks[0]["kind"] == "asr"


def test_parse_caption_tracks_returns_empty_list_when_absent():
    assert app.parse_caption_tracks(FAKE_WATCH_PAGE_NO_CAPTIONS) == []


def test_parse_caption_tracks_returns_empty_list_for_malformed_json():
    broken = '<script>"captionTracks":[{not valid json</script>'
    assert app.parse_caption_tracks(broken) == []


def test_pick_caption_track_prefers_human_over_asr():
    tracks = [
        {"languageCode": "en", "kind": "asr", "baseUrl": "asr-url"},
        {"languageCode": "en", "baseUrl": "human-url"},
    ]
    assert app.pick_caption_track(tracks)["baseUrl"] == "human-url"


def test_pick_caption_track_falls_back_to_asr_english_when_no_human_track():
    tracks = [{"languageCode": "en", "kind": "asr", "baseUrl": "asr-url"}]
    assert app.pick_caption_track(tracks)["baseUrl"] == "asr-url"


def test_pick_caption_track_falls_back_to_first_when_no_english():
    tracks = [{"languageCode": "es", "baseUrl": "spanish-url"}, {"languageCode": "fr", "baseUrl": "french-url"}]
    assert app.pick_caption_track(tracks)["baseUrl"] == "spanish-url"


def test_pick_caption_track_returns_none_for_empty_list():
    assert app.pick_caption_track([]) is None


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
