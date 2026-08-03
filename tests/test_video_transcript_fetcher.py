"""
Tests for src/video_transcript_fetcher/app.py.

Manual-runner pattern, run standalone via
`python3 tests/test_video_transcript_fetcher.py`.

This file was split out of test_video_summarizer.py when the YouTube-
fetching code moved to its own, non-VPC-attached Lambda (see
video_transcript_fetcher/app.py's module docstring for the full "split the
architecture" reasoning). The tests below are otherwise unchanged from
their original video_summarizer versions -- same fixtures, same real,
live-observed edge cases (captionTracks blob sometimes missing on an
identical URL, the timedtext-endpoint dead end, the consent-wall
diagnostic) -- just exercising the moved functions plus the new
_process_one/handler orchestration that publishes the fetch result to
VideoTranscriptResultQueue.

Honesty note (see module docstring in app.py for the full history): this
went watch-page-scraping -> timedtext?type=list -> back to watch-page-
scraping-with-retries, each swap based on real live-smoke-test evidence
gathered along the way (not guesses). The captionTracks JSON shape and
timedtext XML shape below are widely-documented (if unofficial) YouTube
behavior, confirmed present in a real page via a manual curl this session
-- but this Lambda (in its prior, VPC-attached form) has also been
observed NOT finding that same blob on real attempts, hence the retry
logic under test below, and hence the whole reason this function is now
non-VPC-attached. This is explicitly shipped as best-effort, not
guaranteed -- and whether moving off VPC actually fixes the fetch is
UNVERIFIED as of this deploy (no outbound network access in this sandbox
to confirm against real YouTube).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "video_transcript_fetcher"))

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


# --- list_caption_tracks: retry-on-empty behavior, and the consent-wall
# diagnostic on the final failed attempt. Real evidence this is based on:
# multiple live Lambda attempts against videos confirmed (via curl from a
# residential IP) to have real captions came back with zero tracks -- the
# retry loop remains best-effort insurance, but the leading theory as of
# the split-architecture change is a deterministic, IP-based block rather
# than per-attempt randomness (see app.py's module docstring).

def test_list_caption_tracks_returns_immediately_when_first_attempt_has_tracks():
    real_fetch = app.fetch_watch_page
    real_sleep = app.time.sleep
    calls = []
    app.fetch_watch_page = lambda video_id: FAKE_WATCH_PAGE_WITH_CAPTIONS
    app.time.sleep = lambda s: calls.append(s)
    try:
        tracks = app.list_caption_tracks("abc123")
        assert len(tracks) == 2
        assert calls == []  # no retry needed, so no sleep
    finally:
        app.fetch_watch_page = real_fetch
        app.time.sleep = real_sleep


def test_list_caption_tracks_retries_once_then_succeeds():
    real_fetch = app.fetch_watch_page
    real_sleep = app.time.sleep
    responses = [FAKE_WATCH_PAGE_NO_CAPTIONS, FAKE_WATCH_PAGE_WITH_CAPTIONS]
    app.fetch_watch_page = lambda video_id: responses.pop(0)
    app.time.sleep = lambda s: None
    try:
        tracks = app.list_caption_tracks("abc123", max_attempts=2, delay_seconds=0)
        assert len(tracks) == 2
        assert responses == []  # both canned responses were consumed
    finally:
        app.fetch_watch_page = real_fetch
        app.time.sleep = real_sleep


def test_list_caption_tracks_gives_up_after_max_attempts():
    real_fetch = app.fetch_watch_page
    real_sleep = app.time.sleep
    app.fetch_watch_page = lambda video_id: FAKE_WATCH_PAGE_NO_CAPTIONS
    app.time.sleep = lambda s: None
    try:
        tracks = app.list_caption_tracks("abc123", max_attempts=2, delay_seconds=0)
        assert tracks == []
    finally:
        app.fetch_watch_page = real_fetch
        app.time.sleep = real_sleep


def test_list_caption_tracks_logs_consent_wall_on_final_attempt_without_changing_result():
    """The consent-wall marker only changes what gets logged, not the
    return value -- confirming that here so the diagnostic can't
    accidentally change behavior."""
    real_fetch = app.fetch_watch_page
    real_sleep = app.time.sleep
    app.fetch_watch_page = lambda video_id: (
        "<html><body>Before you continue to YouTube, consent.youtube.com wants your consent</body></html>"
    )
    app.time.sleep = lambda s: None
    try:
        tracks = app.list_caption_tracks("abc123", max_attempts=2, delay_seconds=0)
        assert tracks == []
    finally:
        app.fetch_watch_page = real_fetch
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
    real_fetch = app.fetch_watch_page
    real_fetch_xml = app.fetch_transcript_xml
    app.fetch_watch_page = lambda video_id: FAKE_WATCH_PAGE_WITH_CAPTIONS
    app.fetch_transcript_xml = lambda base_url: SAMPLE_TRANSCRIPT_XML
    try:
        transcript, note = app.get_transcript("abc123")
        assert note is None
        assert transcript == "Alright let's check out this ball the hook is pretty strong"
    finally:
        app.fetch_watch_page = real_fetch
        app.fetch_transcript_xml = real_fetch_xml


def test_get_transcript_no_captions_when_genuinely_absent():
    real_fetch = app.fetch_watch_page
    real_sleep = app.time.sleep
    app.fetch_watch_page = lambda video_id: FAKE_WATCH_PAGE_NO_CAPTIONS
    app.time.sleep = lambda s: None
    try:
        transcript, note = app.get_transcript("abc123")
        assert transcript == ""
        assert note == "no_captions_available"
    finally:
        app.fetch_watch_page = real_fetch
        app.time.sleep = real_sleep


def test_get_transcript_empty_transcript_after_tracks_listed():
    """Tracks are listed but the actual transcript fetch comes back empty
    -- a different, more specific note than 'no_captions_available' so this
    failure mode is distinguishable in the DB/CloudWatch."""
    real_fetch = app.fetch_watch_page
    real_fetch_xml = app.fetch_transcript_xml
    app.fetch_watch_page = lambda video_id: FAKE_WATCH_PAGE_WITH_CAPTIONS
    app.fetch_transcript_xml = lambda base_url: ""
    try:
        transcript, note = app.get_transcript("abc123")
        assert transcript == ""
        assert note == "captions_listed_but_transcript_fetch_returned_empty"
    finally:
        app.fetch_watch_page = real_fetch
        app.fetch_transcript_xml = real_fetch_xml


# --- build_result_message ---

def test_build_result_message_shape():
    body = json.loads(app.build_result_message("vid-1", "some transcript", None))
    assert body == {"product_video_id": "vid-1", "transcript": "some transcript", "transcript_note": None}


def test_build_result_message_truncates_pathologically_long_transcript():
    huge = "x" * (app.MAX_TRANSCRIPT_CHARS_FOR_HANDOFF + 1000)
    body = json.loads(app.build_result_message("vid-1", huge, None))
    assert len(body["transcript"]) == app.MAX_TRANSCRIPT_CHARS_FOR_HANDOFF


# --- _process_one / handler orchestration: fake SQS client, mirrors the
# FakeSqs pattern used in test_netsuite_product_scraper_orchestration.py ---

class FakeSqs:
    def __init__(self):
        self.sent = []  # [(QueueUrl, MessageBody), ...]

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append((QueueUrl, MessageBody))
        return {"MessageId": f"msg-{len(self.sent)}"}


def test_process_one_publishes_transcript_result(monkeypatch):
    real_fetch = app.fetch_watch_page
    real_fetch_xml = app.fetch_transcript_xml
    monkeypatch.setenv("TRANSCRIPT_RESULT_QUEUE_URL", "https://sqs.example/result-queue")
    app.fetch_watch_page = lambda video_id: FAKE_WATCH_PAGE_WITH_CAPTIONS
    app.fetch_transcript_xml = lambda base_url: SAMPLE_TRANSCRIPT_XML
    try:
        sqs = FakeSqs()
        result = app._process_one({"product_video_id": "vid-1", "youtube_video_id": "abc123"}, sqs)

        assert result["transcript_note"] is None
        assert result["transcript_chars"] > 0
        assert len(sqs.sent) == 1
        queue_url, body = sqs.sent[0]
        assert queue_url == "https://sqs.example/result-queue"
        parsed = json.loads(body)
        assert parsed["product_video_id"] == "vid-1"
        assert parsed["transcript_note"] is None
        assert "hook is pretty strong" in parsed["transcript"]
    finally:
        app.fetch_watch_page = real_fetch
        app.fetch_transcript_xml = real_fetch_xml


def test_process_one_publishes_no_captions_note_without_raising(monkeypatch):
    real_fetch = app.fetch_watch_page
    real_sleep = app.time.sleep
    monkeypatch.setenv("TRANSCRIPT_RESULT_QUEUE_URL", "https://sqs.example/result-queue")
    app.fetch_watch_page = lambda video_id: FAKE_WATCH_PAGE_NO_CAPTIONS
    app.time.sleep = lambda s: None
    try:
        sqs = FakeSqs()
        result = app._process_one({"product_video_id": "vid-1", "youtube_video_id": "abc123"}, sqs)

        assert result["transcript_note"] == "no_captions_available"
        assert len(sqs.sent) == 1
        parsed = json.loads(sqs.sent[0][1])
        assert parsed["transcript"] == ""
        assert parsed["transcript_note"] == "no_captions_available"
    finally:
        app.fetch_watch_page = real_fetch
        app.time.sleep = real_sleep


class _FakeBoto3Module:
    """boto3 isn't installable in this sandbox (pip's proxy 403s every
    attempt -- same restriction noted throughout this project's other test
    files), so handler-level tests fake the whole module via sys.modules
    rather than importing the real thing. app.handler does `import boto3`
    inside the function body, which just binds to whatever's already in
    sys.modules -- planting a fake module object there before calling
    handler() is enough."""
    def __init__(self, fake_client):
        self._fake_client = fake_client

    def client(self, service_name):
        return self._fake_client


def test_handler_reports_batch_item_failure_without_blocking_other_jobs(monkeypatch):
    """Mirrors this project's established SQS-consumer pattern: one job's
    exception shouldn't stop the others in the same batch, and only the
    failing job's messageId goes into batchItemFailures for SQS to retry."""
    import sys

    real_fetch = app.fetch_watch_page
    real_fetch_xml = app.fetch_transcript_xml
    monkeypatch.setenv("TRANSCRIPT_RESULT_QUEUE_URL", "https://sqs.example/result-queue")

    def flaky_fetch(video_id):
        if video_id == "boom":
            raise RuntimeError("simulated network failure")
        return FAKE_WATCH_PAGE_WITH_CAPTIONS

    app.fetch_watch_page = flaky_fetch
    app.fetch_transcript_xml = lambda base_url: SAMPLE_TRANSCRIPT_XML

    fake_sqs = FakeSqs()
    had_boto3 = "boto3" in sys.modules
    real_boto3_module = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3Module(fake_sqs)

    event = {
        "Records": [
            {"messageId": "m1", "body": json.dumps({"product_video_id": "vid-1", "youtube_video_id": "ok"})},
            {"messageId": "m2", "body": json.dumps({"product_video_id": "vid-2", "youtube_video_id": "boom"})},
        ]
    }
    try:
        response = app.handler(event, None)
        assert response["batchItemFailures"] == [{"itemIdentifier": "m2"}]
        assert len(fake_sqs.sent) == 1  # only vid-1 made it through to publish
    finally:
        app.fetch_watch_page = real_fetch
        app.fetch_transcript_xml = real_fetch_xml
        if had_boto3:
            sys.modules["boto3"] = real_boto3_module
        else:
            del sys.modules["boto3"]


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
