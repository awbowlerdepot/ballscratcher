"""
Tests for scripts/home_transcript_fetcher.py.

Manual-runner pattern, run standalone via
`python3 tests/test_home_transcript_fetcher.py`.

The YouTube-fetching functions here (fetch_watch_page, parse_caption_tracks,
etc.) are a deliberate duplicate of src/video_transcript_fetcher/app.py's --
see that script's module docstring for why. These tests mirror
tests/test_video_transcript_fetcher.py's for the same reason: same logic,
same fixtures, same real, live-observed edge cases. What's new here is
needs_transcript's filtering logic and the admin-API-facing plumbing
(list_candidates_needing_transcripts / submit_transcript / run), which have
no equivalent in the Lambda-based fetcher.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import home_transcript_fetcher as script  # noqa: E402


# --- parse_caption_tracks / pick_caption_track / parse_transcript_xml:
# same fixtures as test_video_transcript_fetcher.py, confirming the
# duplicated logic behaves identically ---

FAKE_WATCH_PAGE_WITH_CAPTIONS = """
<html><body><script>var ytInitialPlayerResponse = {"captions":{"playerCaptionsTracklistRenderer":
{"captionTracks":[
  {"baseUrl":"https://www.youtube.com/api/timedtext?v=abc123\\u0026lang=en", "name":{"simpleText":"English"}, "languageCode":"en", "kind":"asr"},
  {"baseUrl":"https://www.youtube.com/api/timedtext?v=abc123\\u0026lang=es", "name":{"simpleText":"Spanish"}, "languageCode":"es"}
]}}};</script></body></html>
"""

FAKE_WATCH_PAGE_NO_CAPTIONS = "<html><body><script>var ytInitialPlayerResponse = {\"captions\":{}};</script></body></html>"

SAMPLE_TRANSCRIPT_XML = (
    '<?xml version="1.0" encoding="utf-8" ?><transcript>'
    '<text start="0.0" dur="2.0">Alright let&#39;s check out this ball</text>'
    '<text start="2.0" dur="3.5">the hook is pretty strong</text>'
    '</transcript>'
)


def test_parse_caption_tracks_extracts_array():
    tracks = script.parse_caption_tracks(FAKE_WATCH_PAGE_WITH_CAPTIONS)
    assert len(tracks) == 2
    assert tracks[0]["languageCode"] == "en"


def test_pick_caption_track_prefers_human_over_asr():
    tracks = [
        {"languageCode": "en", "kind": "asr", "baseUrl": "asr-url"},
        {"languageCode": "en", "baseUrl": "human-url"},
    ]
    assert script.pick_caption_track(tracks)["baseUrl"] == "human-url"


def test_parse_transcript_xml_concatenates_and_unescapes():
    text = script.parse_transcript_xml(SAMPLE_TRANSCRIPT_XML)
    assert text == "Alright let's check out this ball the hook is pretty strong"


def test_get_transcript_extracts_real_transcript_end_to_end():
    real_fetch = script.fetch_watch_page
    real_fetch_xml = script.fetch_transcript_xml
    script.fetch_watch_page = lambda video_id: FAKE_WATCH_PAGE_WITH_CAPTIONS
    script.fetch_transcript_xml = lambda base_url: SAMPLE_TRANSCRIPT_XML
    try:
        transcript, note = script.get_transcript("abc123")
        assert note is None
        assert transcript == "Alright let's check out this ball the hook is pretty strong"
    finally:
        script.fetch_watch_page = real_fetch
        script.fetch_transcript_xml = real_fetch_xml


def test_get_transcript_no_captions_when_genuinely_absent():
    real_fetch = script.fetch_watch_page
    real_sleep = script.time.sleep
    script.fetch_watch_page = lambda video_id: FAKE_WATCH_PAGE_NO_CAPTIONS
    script.time.sleep = lambda s: None
    try:
        transcript, note = script.get_transcript("abc123")
        assert transcript == ""
        assert note == "no_captions_available"
    finally:
        script.fetch_watch_page = real_fetch
        script.time.sleep = real_sleep


# --- needs_transcript: the filtering logic unique to this script ---

def test_needs_transcript_true_for_approved_untouched_candidate():
    candidate = {"status": "approved", "transcript_note": None, "has_summary": False}
    assert script.needs_transcript(candidate) is True


def test_needs_transcript_false_when_not_approved():
    candidate = {"status": "pending", "transcript_note": None, "has_summary": False}
    assert script.needs_transcript(candidate) is False


def test_needs_transcript_false_when_already_has_a_note():
    """Already checked once (even if the result was 'no captions') -- don't
    re-fetch every day forever."""
    candidate = {"status": "approved", "transcript_note": "no_captions_available", "has_summary": False}
    assert script.needs_transcript(candidate) is False


def test_needs_transcript_false_when_already_summarized():
    candidate = {"status": "approved", "transcript_note": None, "has_summary": True}
    assert script.needs_transcript(candidate) is False


# --- list_candidates_needing_transcripts: fake requests, paginates and filters ---

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeRequestsModule:
    """Captures GET calls and returns canned paginated responses -- mirrors
    how test_video_transcript_fetcher.py fakes boto3 via sys.modules,
    same reason: this script does `import requests` inside each function,
    which just picks up whatever's already in sys.modules."""
    def __init__(self, pages):
        self.pages = pages  # list of page payloads, consumed in order
        self.get_calls = []
        self.post_calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.get_calls.append({"url": url, "params": params, "headers": headers})
        page = self.pages.pop(0)
        return _FakeResponse(page)

    def post(self, url, json=None, headers=None, timeout=None):
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse({})


def test_list_candidates_needing_transcripts_paginates_and_filters():
    import sys as _sys
    real_requests = _sys.modules.get("requests")
    fake = _FakeRequestsModule(pages=[
        {"items": [
            {"id": "vid-1", "status": "approved", "transcript_note": None, "has_summary": False},
            {"id": "vid-2", "status": "approved", "transcript_note": "no_captions_available", "has_summary": False},
        ] * 100},  # 200 items == page_limit, so the loop fetches a second page
        {"items": [
            {"id": "vid-3", "status": "approved", "transcript_note": None, "has_summary": False},
        ]},  # short page -- loop stops here
    ])
    _sys.modules["requests"] = fake
    try:
        result = script.list_candidates_needing_transcripts("https://admin.example", "tok", page_limit=200)
        ids = [c["id"] for c in result]
        assert ids == ["vid-1"] * 100 + ["vid-3"]
        assert len(fake.get_calls) == 2
        assert fake.get_calls[0]["params"]["offset"] == 0
        assert fake.get_calls[1]["params"]["offset"] == 200
        assert fake.get_calls[0]["headers"]["Authorization"] == "Bearer tok"
    finally:
        if real_requests is not None:
            _sys.modules["requests"] = real_requests
        else:
            del _sys.modules["requests"]


def test_run_submits_transcripts_and_tolerates_per_video_errors(monkeypatch):
    """One video failing (network error mid-fetch) shouldn't stop the rest
    of the batch -- same principle as every SQS handler in this project."""
    real_fetch = script.fetch_watch_page
    real_fetch_xml = script.fetch_transcript_xml
    real_sleep = script.time.sleep
    monkeypatch.setattr(script, "list_candidates_needing_transcripts", lambda url, token: [
        {"id": "vid-1", "youtube_video_id": "good1"},
        {"id": "vid-2", "youtube_video_id": "boom"},
        {"id": "vid-3", "youtube_video_id": "good2"},
    ])

    submitted = []
    monkeypatch.setattr(script, "submit_transcript", lambda url, token, vid, transcript, note: submitted.append(
        (vid, transcript, note)
    ))

    def flaky_fetch(video_id):
        if video_id == "boom":
            raise RuntimeError("simulated network failure")
        return FAKE_WATCH_PAGE_WITH_CAPTIONS

    script.fetch_watch_page = flaky_fetch
    script.fetch_transcript_xml = lambda base_url: SAMPLE_TRANSCRIPT_XML
    script.time.sleep = lambda s: None

    try:
        summary = script.run("https://admin.example", "tok", delay_between_videos=0)

        assert summary == {"total": 3, "got_transcript": 2, "no_captions": 0, "errors": 1}
        submitted_ids = [s[0] for s in submitted]
        assert submitted_ids == ["vid-1", "vid-3"]  # vid-2 never got submitted, it errored before that point
    finally:
        script.fetch_watch_page = real_fetch
        script.fetch_transcript_xml = real_fetch_xml
        script.time.sleep = real_sleep


def test_run_honors_get_transcript_fn_override(monkeypatch):
    """The hook home_transcript_fetcher_browser.py relies on: run() must
    call whatever get_transcript_fn it's given instead of this module's own
    HTTP-based get_transcript, so the admin-API listing/submission logic
    can be reused by a completely different fetch mechanism."""
    monkeypatch.setattr(script, "list_candidates_needing_transcripts", lambda url, token: [
        {"id": "vid-1", "youtube_video_id": "abc123"},
    ])
    submitted = []
    monkeypatch.setattr(script, "submit_transcript", lambda url, token, vid, transcript, note: submitted.append(
        (vid, transcript, note)
    ))

    calls = []

    def fake_get_transcript(youtube_video_id):
        calls.append(youtube_video_id)
        return "some transcript from a different fetch mechanism entirely", None

    summary = script.run("https://admin.example", "tok", delay_between_videos=0, get_transcript_fn=fake_get_transcript)

    assert calls == ["abc123"]
    assert submitted == [("vid-1", "some transcript from a different fetch mechanism entirely", None)]
    assert summary == {"total": 1, "got_transcript": 1, "no_captions": 0, "errors": 0}


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
