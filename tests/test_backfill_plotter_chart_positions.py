"""
Tests for scripts/backfill_plotter_chart_positions.py.

Manual-runner pattern, run standalone via
`python3 tests/test_backfill_plotter_chart_positions.py`.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import backfill_plotter_chart_positions as script  # noqa: E402


# --- load_chart_entries: the bundled dataset itself ---

def test_load_chart_entries_reads_the_bundled_json():
    entries = script.load_chart_entries()
    assert len(entries) == 56  # digitized from Brunswick's own published chart
    for entry in entries:
        assert set(entry.keys()) == {"brand", "name", "oil", "motion"}
        assert 1 <= entry["oil"] <= 16
        assert 1 <= entry["motion"] <= 18


# --- match_entry: the matching LOGIC, no network ---

def test_match_entry_no_brand_found():
    result = script.match_entry(
        {"brand": "Nonexistent", "name": "Ball"}, {}, search_fn=lambda bid, name: [],
    )
    assert result["status"] == "no_brand"


def test_match_entry_exact_case_insensitive_single_match():
    brands = {"brunswick": {"id": "brand-1", "name": "Brunswick"}}
    search_calls = []

    def fake_search(brand_id, name):
        search_calls.append((brand_id, name))
        return [{"id": "prod-1", "name": "Crown Victory"}, {"id": "prod-2", "name": "Crown Victory Pearl"}]

    result = script.match_entry({"brand": "Brunswick", "name": "crown victory"}, brands, fake_search)

    assert result["status"] == "matched"
    assert result["product"]["id"] == "prod-1"
    assert search_calls == [("brand-1", "crown victory")]


def test_match_entry_no_exact_match_flagged_for_review():
    brands = {"brunswick": {"id": "brand-1", "name": "Brunswick"}}
    result = script.match_entry(
        {"brand": "Brunswick", "name": "Crown Victory"}, brands,
        search_fn=lambda bid, name: [{"id": "prod-1", "name": "Crown Victory Black/Red"}],
    )
    assert result["status"] == "no_match"
    assert result["candidates"] == [{"id": "prod-1", "name": "Crown Victory Black/Red"}]


def test_match_entry_no_search_results_at_all():
    brands = {"brunswick": {"id": "brand-1", "name": "Brunswick"}}
    result = script.match_entry(
        {"brand": "Brunswick", "name": "Crown Victory"}, brands, search_fn=lambda bid, name: [],
    )
    assert result["status"] == "no_match"
    assert result["candidates"] == []


def test_match_entry_multiple_exact_matches_flagged_ambiguous():
    """Shouldn't really happen (products.name is not unique but two
    identically-named products on the same brand would be a real data
    problem elsewhere), handled defensively rather than assumed away --
    same principle as every other batch script in this project."""
    brands = {"brunswick": {"id": "brand-1", "name": "Brunswick"}}
    result = script.match_entry(
        {"brand": "Brunswick", "name": "Crown Victory"}, brands,
        search_fn=lambda bid, name: [
            {"id": "prod-1", "name": "Crown Victory"}, {"id": "prod-2", "name": "Crown Victory"},
        ],
    )
    assert result["status"] == "ambiguous"
    assert len(result["candidates"]) == 2


# --- run: orchestration -- matches, writes, tolerates per-entry errors,
# never guesses on an ambiguous/unmatched entry ---

def test_run_writes_plotter_position_for_confident_matches():
    entries = [{"brand": "Brunswick", "name": "Crown Victory", "oil": 6, "motion": 18}]
    written = []

    summary = script.run(
        "https://admin.example", "tok", chart_entries=entries,
        list_brands_fn=lambda: [{"id": "brand-1", "name": "Brunswick"}],
        search_fn=lambda bid, name: [{"id": "prod-1", "name": "Crown Victory"}],
        set_position_fn=lambda pid, oil, motion: written.append((pid, oil, motion)),
    )

    assert summary == {"total": 1, "matched": 1, "no_brand": 0, "no_match": 0, "ambiguous": 0, "errors": 0}
    assert written == [("prod-1", 6, 18)]


def test_run_never_writes_for_no_match_or_ambiguous():
    entries = [
        {"brand": "Brunswick", "name": "No Match Ball", "oil": 6, "motion": 18},
        {"brand": "Brunswick", "name": "Ambiguous Ball", "oil": 6, "motion": 18},
    ]
    written = []

    def fake_search(bid, name):
        if name == "No Match Ball":
            return [{"id": "prod-1", "name": "Something Else"}]
        return [{"id": "prod-2", "name": "Ambiguous Ball"}, {"id": "prod-3", "name": "Ambiguous Ball"}]

    summary = script.run(
        "https://admin.example", "tok", chart_entries=entries,
        list_brands_fn=lambda: [{"id": "brand-1", "name": "Brunswick"}],
        search_fn=fake_search,
        set_position_fn=lambda pid, oil, motion: written.append((pid, oil, motion)),
    )

    assert written == []
    assert summary["matched"] == 0
    assert summary["no_match"] == 1
    assert summary["ambiguous"] == 1


def test_run_counts_missing_brand_separately():
    entries = [{"brand": "NotOnboardedYet", "name": "Some Ball", "oil": 6, "motion": 18}]

    summary = script.run(
        "https://admin.example", "tok", chart_entries=entries,
        list_brands_fn=lambda: [{"id": "brand-1", "name": "Brunswick"}],
        search_fn=lambda bid, name: [],
        set_position_fn=lambda pid, oil, motion: None,
    )

    assert summary["no_brand"] == 1
    assert summary["matched"] == 0


def test_run_tolerates_per_entry_errors():
    entries = [
        {"brand": "Brunswick", "name": "Boom", "oil": 6, "motion": 18},
        {"brand": "Brunswick", "name": "Fine", "oil": 6, "motion": 18},
    ]

    def flaky_search(bid, name):
        if name == "Boom":
            raise RuntimeError("simulated network failure")
        return [{"id": "prod-1", "name": "Fine"}]

    summary = script.run(
        "https://admin.example", "tok", chart_entries=entries,
        list_brands_fn=lambda: [{"id": "brand-1", "name": "Brunswick"}],
        search_fn=flaky_search,
        set_position_fn=lambda pid, oil, motion: None,
    )

    assert summary == {"total": 2, "matched": 1, "no_brand": 0, "no_match": 0, "ambiguous": 0, "errors": 1}


# --- get_requests_session: same retry-config sanity check as every other
# script in this project.

def test_get_requests_session_retries_on_throttle_and_5xx_status_codes():
    session = script.get_requests_session()
    adapter = session.get_adapter("https://admin.example")
    retry = adapter.max_retries

    assert retry.total == script.RETRY_TOTAL
    assert set(retry.status_forcelist) == set(script.RETRY_STATUS_FORCELIST)
    assert 503 in retry.status_forcelist
    assert "GET" in retry.allowed_methods
    assert "PATCH" in retry.allowed_methods
    assert retry.backoff_factor == script.RETRY_BACKOFF_FACTOR


if __name__ == "__main__":
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for name, t in tests:
        t()
        print(f"PASS: {name}")
        passed += 1
    print(f"\n{passed}/{len(tests)} tests passed")
