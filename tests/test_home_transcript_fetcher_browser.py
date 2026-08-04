"""
Tests for scripts/home_transcript_fetcher_browser.py.

Manual-runner pattern, run standalone via
`python3 tests/test_home_transcript_fetcher_browser.py`.

Playwright itself is not installed in this sandbox (no browser to actually
launch), and the real question of whether the selectors in
home_transcript_fetcher_browser.py match YouTube's actual current DOM is
UNVERIFIED here -- these tests only cover the pure logic (timestamp
detection, text-cleanup, selector-fallback ordering, and the
success/failure branching in get_transcript_via_browser) against small
fake Playwright-shaped objects (FakeLocator/FakePage/FakeBrowser below),
not against real YouTube markup. See that module's docstring: real
verification happens on the Pi 5, and any selector mismatch shows up as a
screenshot + HTML dump in ./debug/, not as a sandbox test failure.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import home_transcript_fetcher_browser as script  # noqa: E402


# --- _looks_like_timestamp ---

def test_looks_like_timestamp_mm_ss():
    assert script._looks_like_timestamp("0:03") is True


def test_looks_like_timestamp_hh_mm_ss():
    assert script._looks_like_timestamp("1:02:03") is True


def test_looks_like_timestamp_rejects_plain_text():
    assert script._looks_like_timestamp("Alright let's check out this ball") is False


def test_looks_like_timestamp_rejects_single_number():
    assert script._looks_like_timestamp("42") is False


# --- Fake Playwright-shaped objects, minimal enough to cover how the
# module actually calls them (.first, .wait_for, .click, .inner_text,
# .count, .nth) ---

class FakeLocator:
    def __init__(self, visible=True, text="", segments=None):
        self.visible = visible
        self.text = text
        self.segments = segments or []
        self.clicked = False

    @property
    def first(self):
        return self

    def wait_for(self, state=None, timeout=None):
        if not self.visible:
            raise TimeoutError(f"not visible (state={state})")

    def click(self):
        self.clicked = True

    def inner_text(self):
        return self.text

    def count(self):
        return len(self.segments)

    def nth(self, i):
        return self.segments[i]


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


# --- _extract_transcript_text ---

def test_extract_transcript_text_strips_leading_timestamp_per_segment():
    locator_map = {
        "ytd-transcript-segment-list-renderer": FakeLocator(visible=True),
        "ytd-transcript-segment-renderer": FakeLocator(segments=[
            FakeLocator(text="0:00\nAlright let's check out this ball"),
            FakeLocator(text="0:02\nthe hook is pretty strong"),
        ]),
    }
    page = FakePage(locator_map)
    text = script._extract_transcript_text(page, timeout_ms=1000)
    assert text == "Alright let's check out this ball the hook is pretty strong"


def test_extract_transcript_text_falls_back_to_panel_innertext_when_no_segments():
    locator_map = {
        "ytd-transcript-segment-list-renderer": FakeLocator(visible=True, text="Some raw transcript text"),
        "ytd-transcript-segment-renderer": FakeLocator(segments=[]),  # count() == 0
    }
    page = FakePage(locator_map)
    text = script._extract_transcript_text(page, timeout_ms=1000)
    assert text == "Some raw transcript text"


def test_extract_transcript_text_raises_if_panel_never_appears():
    locator_map = {
        "ytd-transcript-segment-list-renderer": FakeLocator(visible=False),
    }
    page = FakePage(locator_map)
    try:
        script._extract_transcript_text(page, timeout_ms=1000)
        assert False, "expected an exception when the panel never becomes visible"
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
    locator_map = {
        script._SHOW_TRANSCRIPT_SELECTORS[0]: FakeLocator(visible=True),
        "ytd-transcript-segment-list-renderer": FakeLocator(visible=True),
        "ytd-transcript-segment-renderer": FakeLocator(segments=[
            FakeLocator(text="0:00\nAlright let's check out this ball"),
        ]),
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
        "ytd-transcript-segment-list-renderer": FakeLocator(visible=False),  # panel never appears
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
