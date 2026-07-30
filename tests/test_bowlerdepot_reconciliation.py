"""
Tests for src/bowlerdepot_reconciliation/app.py. Unlike this project's
manufacturer scrapers, there's no real BowlerDepot store to capture
fixture data from (no store credentials this session -- see app.py's
module docstring for exactly what's confirmed real vs. unverified: the
BigCommerce v3 Catalog Products API response SHAPE is real, fetched
directly from BigCommerce's own current developer docs this session; the
specific custom_fields names BowlerDepot actually uses, and whether it
models weights as true BigCommerce variants or separate products, are
both unverified guesses). Fake BigCommerce product dicts below are
therefore built to match that CONFIRMED real response shape (id, name,
sku, custom_fields: [{name, value, id}]) but with invented values, not a
real captured product -- the pagination/matching/comparison LOGIC is what
these tests verify, not real BowlerDepot data.

Manual-runner pattern, run standalone via
`python3 tests/test_bowlerdepot_reconciliation.py`.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "bowlerdepot_reconciliation"))

import app  # noqa: E402


# --- Name normalization / fuzzy matching ---

def test_fuzzy_match_exact_normalized_match():
    products = [{"id": 1, "name": "Brunswick Fury Emerald/Black Hybrid", "sku": "BRUN-FURY-EB"}]
    match, ratio = app.fuzzy_match_product("Brunswick Fury Emerald/Black Hybrid", products)
    assert match["id"] == 1
    assert ratio == 1.0


def test_fuzzy_match_ignores_punctuation_and_case():
    """Same name, different casing/punctuation only (no inserted space
    where the manufacturer's "/" was) -- a clean exact match after
    normalization."""
    products = [{"id": 1, "name": "BRUNSWICK FURY EMERALD/BLACK HYBRID", "sku": "X"}]
    match, ratio = app.fuzzy_match_product("Brunswick Fury Emerald/Black Hybrid", products)
    assert match["id"] == 1
    assert ratio == 1.0


def test_fuzzy_match_high_but_not_exact_when_slash_becomes_a_space():
    """Real-world case: a human re-typing "Emerald/Black" as "Emerald
    Black" (space instead of dropped slash) doesn't normalize to an exact
    match here, but should still score high enough to match."""
    products = [{"id": 1, "name": "Brunswick Fury Emerald Black Hybrid", "sku": "X"}]
    match, ratio = app.fuzzy_match_product("Brunswick Fury Emerald/Black Hybrid", products)
    assert match["id"] == 1
    assert 0.80 <= ratio < 1.0


def test_fuzzy_match_close_but_not_exact():
    """Real-world case the architecture doc called out: retail listings
    sometimes append/drop qualifiers manufacturers use."""
    products = [{"id": 1, "name": "Brunswick Fury Emerald/Black Hybrid Bowling Ball", "sku": "X"}]
    match, ratio = app.fuzzy_match_product("Brunswick Fury Emerald/Black Hybrid", products)
    assert match["id"] == 1
    assert 0.80 <= ratio < 1.0


def test_fuzzy_match_returns_none_below_threshold():
    products = [{"id": 1, "name": "Storm Phaze II", "sku": "X"}]
    match, ratio = app.fuzzy_match_product("Brunswick Fury Emerald/Black Hybrid", products)
    assert match is None
    assert ratio == 0.0


def test_fuzzy_match_picks_best_of_multiple_candidates():
    products = [
        {"id": 1, "name": "Storm Phaze II", "sku": "X"},
        {"id": 2, "name": "Brunswick Fury Emerald Black Hybrid", "sku": "Y"},
    ]
    match, ratio = app.fuzzy_match_product("Brunswick Fury Emerald/Black Hybrid", products)
    assert match["id"] == 2


# --- check_coverage ---

def test_check_coverage_flags_missing_product():
    our_products = [{"product_id": "p1", "brand_name": "Brunswick", "name": "Fury Emerald/Black Hybrid"}]
    results = app.check_coverage(our_products, [])
    assert results[0]["match"] is None


def test_check_coverage_records_clean_match():
    our_products = [{"product_id": "p1", "brand_name": "Brunswick", "name": "Fury Emerald/Black Hybrid"}]
    bc_products = [{"id": 42, "name": "Brunswick Fury Emerald/Black Hybrid", "sku": "BRUN-FURY-EB", "custom_fields": []}]
    results = app.check_coverage(our_products, bc_products)
    assert results[0]["match"]["bigcommerce_product_id"] == "42"
    assert results[0]["match"]["ambiguous"] is False


def test_check_coverage_flags_ambiguous_match():
    our_products = [{"product_id": "p1", "brand_name": "Brunswick", "name": "Fury Emerald/Black Hybrid"}]
    bc_products = [{"id": 42, "name": "Brunswick Fury Emerald/Black Hybrid Bowling Ball", "sku": "X", "custom_fields": []}]
    results = app.check_coverage(our_products, bc_products)
    assert results[0]["match"]["ambiguous"] is True


# --- custom_fields extraction (real BigCommerce shape, invented values) ---

def test_extract_specs_from_custom_fields_real_shape():
    product = {
        "id": 1,
        "custom_fields": [
            {"id": 10, "name": "RG", "value": "2.533"},
            {"id": 11, "name": "Diff", "value": "0.033"},
        ],
    }
    specs = app.extract_specs_from_custom_fields(product)
    assert specs["rg"] == 2.533
    assert specs["differential"] == 0.033
    assert specs["mass_bias"] is None


def test_extract_specs_from_custom_fields_case_insensitive_label_matching():
    product = {"id": 1, "custom_fields": [{"id": 1, "name": "radius of gyration", "value": "2.5"}]}
    assert app.extract_specs_from_custom_fields(product)["rg"] == 2.5


def test_extract_specs_from_custom_fields_missing_returns_none():
    product = {"id": 1, "custom_fields": []}
    specs = app.extract_specs_from_custom_fields(product)
    assert specs == {"rg": None, "differential": None, "mass_bias": None}


# --- check_accuracy ---

def test_check_accuracy_flags_mismatch_beyond_tolerance():
    pairs = [{
        "product_id": "p1",
        "our_sku": {"weight_lbs": 16, "rg": 2.533, "differential": 0.033, "mass_bias": None},
        "bigcommerce_product": {
            "id": 42,
            "custom_fields": [{"id": 1, "name": "RG", "value": "2.600"}, {"id": 2, "name": "Diff", "value": "0.033"}],
        },
    }]
    mismatches = app.check_accuracy(pairs)
    rg_mismatch = next(m for m in mismatches if m["field_name"] == "rg_16lb")
    assert rg_mismatch["current_value"] == "2.533"
    assert rg_mismatch["proposed_value"] == "2.6"


def test_check_accuracy_no_mismatch_within_tolerance():
    pairs = [{
        "product_id": "p1",
        "our_sku": {"weight_lbs": 16, "rg": 2.5335, "differential": 0.033, "mass_bias": None},
        "bigcommerce_product": {
            "id": 42,
            "custom_fields": [{"id": 1, "name": "RG", "value": "2.533"}, {"id": 2, "name": "Diff", "value": "0.033"}],
        },
    }]
    assert app.check_accuracy(pairs) == []


def test_check_accuracy_skips_when_bigcommerce_field_missing():
    pairs = [{
        "product_id": "p1",
        "our_sku": {"weight_lbs": 16, "rg": 2.533, "differential": 0.033, "mass_bias": 0.015},
        "bigcommerce_product": {"id": 42, "custom_fields": []},
    }]
    assert app.check_accuracy(pairs) == []  # nothing to compare against, not a "wrong" flag


# --- Pagination (real confirmed response shape) ---

def test_fetch_all_products_paginates_using_real_meta_shape(monkeypatch):
    """Response shape ({"data": [...], "meta": {"pagination": {...}}})
    confirmed real against BigCommerce's own current API docs this
    session -- see module docstring."""
    pages = {
        1: {
            "data": [{"id": 1, "name": "Ball One"}],
            "meta": {"pagination": {"total": 2, "count": 1, "per_page": 1, "current_page": 1, "total_pages": 2}},
        },
        2: {
            "data": [{"id": 2, "name": "Ball Two"}],
            "meta": {"pagination": {"total": 2, "count": 1, "per_page": 1, "current_page": 2, "total_pages": 2}},
        },
    }
    calls = []

    def fake_fetch_products_page(store_hash, auth_token, page=1, timeout=30):
        calls.append(page)
        return pages[page]

    monkeypatch.setattr(app, "fetch_products_page", fake_fetch_products_page)
    products = app.fetch_all_products("store-hash", "token")
    assert [p["id"] for p in products] == [1, 2]
    assert calls == [1, 2]


def test_fetch_all_products_stops_at_one_page_when_total_pages_is_one(monkeypatch):
    body = {
        "data": [{"id": 1, "name": "Only Ball"}],
        "meta": {"pagination": {"total": 1, "count": 1, "per_page": 50, "current_page": 1, "total_pages": 1}},
    }
    monkeypatch.setattr(app, "fetch_products_page", lambda store_hash, auth_token, page=1, timeout=30: body)
    products = app.fetch_all_products("store-hash", "token")
    assert len(products) == 1


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
