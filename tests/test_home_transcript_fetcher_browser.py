"""
Tests for scripts/home_transcript_fetcher_browser.py.

Manual-runner pattern, run standalone via
`python3 tests/test_home_transcript_fetcher_browser.py`.

Playwright itself is not installed in this sandbox (no browser to actually
launch) -- these tests cover the pure logic (text extraction against the
real, confirmed DOM shape, selector-fallback ordering, and the
success/failure branching in get_transcript_via_browser) against small
fake Playwright-shaped objects (FakeLocator/FakePage/FakeBrowser below),
not a real browser. The `<transcript-segment-view-model>` /
`.ytAttributedStringHost` structure these fakes model is NOT a guess --
it's what a real live test against DcbP2eltVsE on the Pi 5 actually found
(see _extract_transcript_text's docstring for the real markup snippet
pulled from that test's debug HTML dump). What's still unverified here is
only whether YouTube's markup stays this way going forward -- any future
drift shows up as a screenshot + HTML dump in ./debug/, not as a sandbox
test failure.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import home_transcript_fetcher_browser as script  # noqa: E402


# --- Fake Playwright-shaped objects, minimal enough to cover how the
# module actually calls them (.first, .wait_for, .click, .inner_text,
# .count, .nth, and now .locator() for the span scoped inside a segment) ---

class FakeLocator:
    def __init__(self, visible=True, text="", segments=None, child_locator_map=None, raise_on_inner_text=False):
        self.visible = visible
        self.text = text
        self.segments = segments or []
        self.clicked = False
        self.child_locator_map = child_locator_map or {}
        self.raise_on_inner_text = raise_on_inner_text

    @property
    def first(self):
        return self

    def wait_for(self, state=None, timeout=None):
        if not self.visible:
            raise TimeoutError(f"not visible (state={state})")

    def click(self):
        self.clicked = True

    def inner_text(self):
        if self.raise_on_inner_text:
            raise Exception("element not found")
        return self.text

    def count(self):
        return len(self.segments)

    def nth(self, i):
        return self.segments[i]

    def locator(self, selector):
        # Mirrors Playwright's Locator.locator() -- a selector scoped
        # within this element. Missing selector -> a locator whose
        # inner_text() raises, so _extract_transcript_text's fallback path
        # (segment.inner_text() when the span isn't found) is exercisable.
        return self.child_locator_map.get(selector, FakeLocator(raise_on_inner_text=True))


class FakePage:
    def __init__(self, locator_map=None):
        self.locator_map = locator_map or {}
        self.closed = False
        self.goto_calls = []

    def goto(self, url, timeout=None):
        self.goto_calls.append(url)

    def locator(self, selector):
        return self.locator_map.get(selector, FakeLocator(visible=False))

    def screenshot(self, path=None, full_page=None):
        pass

    def content(self):
        return "<html>fake</html>"

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


# --- _extract_transcript_text, against the real confirmed
# transcript-segment-view-model / .ytAttributedStringHost structure ---

def test_extract_transcript_text_reads_caption_span_per_segment():
    segment1 = FakeLocator(child_locator_map={
        ".ytAttributedStringHost": FakeLocator(text="Alright let's check out this ball"),
    })
    segment2 = FakeLocator(child_locator_map={
        ".ytAttributedStringHost": FakeLocator(text="the hook is pretty strong"),
    })
    locator_map = {
        "transcript-segment-view-model": FakeLocator(visible=True, segments=[segment1, segment2]),
    }
    page = FakePage(locator_map)
    text = script._extract_transcript_text(page, timeout_ms=1000)
    assert text == "Alright let's check out this ball the hook is pretty strong"


def test_extract_transcript_text_falls_back_to_segment_innertext_when_span_missing():
    """If .ytAttributedStringHost doesn't match for some reason (a future
    YouTube markup tweak, say), fall back to the whole segment's innerText
    rather than silently dropping that line -- better a timestamp-prefixed
    line makes it into the transcript than the line vanishes entirely."""
    segment1 = FakeLocator(text="0:00 Alright let's check out this ball")  # no child_locator_map entry
    locator_map = {
        "transcript-segment-view-model": FakeLocator(visible=True, segments=[segment1]),
    }
    page = FakePage(locator_map)
    text = script._extract_transcript_text(page, timeout_ms=1000)
    assert text == "0:00 Alright let's check out this ball"


def test_extract_transcript_text_raises_if_no_segments_ever_appear():
    locator_map = {
        "transcript-segment-view-model": FakeLocator(visible=False),
    }
    page = FakePage(locator_map)
    try:
        script._extract_transcript_text(page, timeout_ms=1000)
        assert False, "expected an exception when no segment ever becomes visible"
    except TimeoutError:
        pass


# --- _click_first_visible ---

def test_click_first_visible_uses_first_matching_selector():
    target = FakeLocator(visible=True)
    locator_map = {"a": FakeLocator(visible=False), "b": target, "c": FakeLocator(visible=True)}
    result = script._click_first_visible(FakePage(locator_map), ["a", "b", "c"], timeout_ms=1000)
    assert result is True
    assert target.clicked is True
    assert locator_map["c"].clicked is False  # never got to it, "b" already worked


def test_click_first_visible_returns_false_when_nothing_visible():
    locator_map = {"a": FakeLocator(visible=False), "b": FakeLocator(visible=False)}
    result = script._click_first_visible(FakePage(locator_map), ["a", "b"], timeout_ms=1000)
    assert result is False


# --- get_transcript_via_browser: the real success/failure branching ---

def test_get_transcript_via_browser_success(monkeypatch):
    monkeypatch.setattr(script, "_dump_debug_evidence", lambda *a, **k: None)
    segment = FakeLocator(child_locator_map={
        ".ytAttributedStringHost": FakeLocator(text="Alright let's check out this ball"),
    })
    locator_map = {
        script._SHOW_TRANSCRIPT_SELECTORS[0]: FakeLocator(visible=True),
        "transcript-segment-view-model": FakeLocator(visible=True, segments=[segment]),
    }
    page = FakePage(locator_map)
    browser = FakeBrowser(page)

    transcript, note = script.get_transcript_via_browser("abc123", browser)

    assert note is None
    assert transcript == "Alright let's check out this ball"
    assert page.closed is True  # always closed, even on success


def test_get_transcript_via_browser_no_button_found(monkeypatch):
    dumps = []
    monkeypatch.setattr(script, "_dump_debug_evidence", lambda page, video_id, tag: dumps.append(tag))
    page = FakePage({})  # nothing visible -- every selector falls back to FakeLocator(visible=False)
    browser = FakeBrowser(page)

    transcript, note = script.get_transcript_via_browser("abc123", browser)

    assert transcript == ""
    assert note == "no_captions_available"
    assert dumps == ["no_button"]
    assert page.closed is True


def test_get_transcript_via_browser_panel_extraction_fails(monkeypatch):
    dumps = []
    monkeypatch.setattr(script, "_dump_debug_evidence", lambda page, video_id, tag: dumps.append(tag))
    locator_map = {
        script._SHOW_TRANSCRIPT_SELECTORS[0]: FakeLocator(visible=True),  # button click succeeds
        "transcript-segment-view-model": FakeLocator(visible=False),  # panel never appears
    }
    page = FakePage(locator_map)
    browser = FakeBrowser(page)

    transcript, note = script.get_transcript_via_browser("abc123", browser)

    assert transcript == ""
    assert note == "transcript_panel_found_but_text_extraction_returned_empty"
    assert dumps == ["panel_extraction_failed"]
    assert page.closed is True


if __name__ == "__main__":
    class _MonkeyPatch:
        def __init__(self):
            self._sets = []

        def setattr(self, obj, name, value):
            self._sets.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, value in reversed(self._sets):
                setattr(obj, name, value)

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
