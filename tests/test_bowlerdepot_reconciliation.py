"""
Tests for src/bowlerdepot_reconciliation/app.py. Still no real BowlerDepot
store *credentials* (no private v3 Catalog API access), so fake
BigCommerce product dicts below are built to match the CONFIRMED real
response SHAPE (id, name, sku, custom_fields: [{name, value, id}]) --
that part was always real, fetched from BigCommerce's own current
developer docs. But the specific custom_fields label text used in these
fixtures ("Radius of Gyration(15lb)", "Max Differential(15lb)",
"Int. Differential(15lb)") is no longer invented -- it's the real,
confirmed-live display text read directly off two real bowlerdepot.com
product pages this session (Storm Alpha Crux, Roto Grip RST Hyperdrive),
and the "one product with a weight variant, one 15lb-reference spec
value" model these tests exercise (see check_accuracy's tests) is the
real, confirmed structure too -- see app.py's module docstring for the
full detail of what was resolved and how. What's still genuinely
unconfirmed: whether the private v3 API's JSON literally names the field
this way (as opposed to the storefront template reformatting a
differently-named internal field for display) -- that's the one thing
real store credentials would still be needed to nail down completely.

Manual-runner pattern, run standalone via
`python3 tests/test_bowlerdepot_reconciliation.py`.
"""
import json
import os
import sys
from decimal import Decimal

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


# --- custom_fields extraction ---
# Real BigCommerce response SHAPE (custom_fields: [{"name","value","id"}]),
# and the "Radius of Gyration(15lb)"/"Max Differential(15lb)"/
# "Int. Differential(15lb)" label text below is the real, confirmed-live
# display label text read directly off two real bowlerdepot.com product
# pages this session (Storm Alpha Crux, Roto Grip RST Hyperdrive) -- not
# a private-API read (no real store credentials exist), but real
# storefront evidence, not invented.

def test_extract_specs_from_custom_fields_real_confirmed_labels():
    product = {
        "id": 1,
        "custom_fields": [
            {"id": 10, "name": "Radius of Gyration(15lb)", "value": "2.48"},
            {"id": 11, "name": "Max Differential(15lb)", "value": "0.053"},
            {"id": 12, "name": "Int. Differential(15lb)", "value": "0.018"},
        ],
    }
    specs = app.extract_specs_from_custom_fields(product)
    assert specs["rg"] == 2.48
    assert specs["differential"] == 0.053
    assert specs["mass_bias"] == 0.018


def test_extract_specs_from_custom_fields_prefix_match_without_qualifier():
    """_find_custom_field matches by prefix (startswith), not exact
    equality, since it's unconfirmed whether the "(15lb)" qualifier is
    really part of the stored custom_fields.name or just the storefront
    template's display text -- a bare "Radius of Gyration" (no qualifier)
    must still match."""
    product = {"id": 1, "custom_fields": [{"id": 1, "name": "Radius of Gyration", "value": "2.5"}]}
    assert app.extract_specs_from_custom_fields(product)["rg"] == 2.5


def test_find_custom_field_prefix_match_does_not_false_positive_on_embedded_letters():
    """Prefix matching (not a bare substring check) specifically avoids a
    short fallback candidate like "rg" matching an unrelated field that
    merely contains those letters in the middle of an unrelated word --
    "Target Weight" contains "rg" as a substring but doesn't start with
    it, so it must NOT be picked up as the rg value."""
    product = {"id": 1, "custom_fields": [{"id": 1, "name": "Target Weight", "value": "15"}]}
    assert app.extract_specs_from_custom_fields(product)["rg"] is None


def test_extract_specs_from_custom_fields_case_insensitive_label_matching():
    product = {"id": 1, "custom_fields": [{"id": 1, "name": "radius of gyration", "value": "2.5"}]}
    assert app.extract_specs_from_custom_fields(product)["rg"] == 2.5


def test_extract_specs_from_custom_fields_missing_returns_none():
    product = {"id": 1, "custom_fields": []}
    specs = app.extract_specs_from_custom_fields(product)
    assert specs == {"rg": None, "differential": None, "mass_bias": None}


# --- check_accuracy ---
# BowlerDepot's real product pages (confirmed live: Storm Alpha Crux, Roto
# Grip RST Hyperdrive) publish exactly one spec value per ball, qualified
# "(15lb)" -- weight is a BigCommerce variant/option on a single product,
# not a separate product per weight. So check_accuracy only ever compares
# against our own 15lb SKU; see the two tests below for both halves of
# that behavior (compares 15lb, skips everything else).

def test_check_accuracy_flags_mismatch_beyond_tolerance():
    pairs = [{
        "product_id": "p1",
        "our_sku": {"weight_lbs": 15, "rg": 2.533, "differential": 0.033, "mass_bias": None},
        "bigcommerce_product": {
            "id": 42,
            "custom_fields": [
                {"id": 1, "name": "Radius of Gyration(15lb)", "value": "2.600"},
                {"id": 2, "name": "Max Differential(15lb)", "value": "0.033"},
            ],
        },
    }]
    mismatches = app.check_accuracy(pairs)
    rg_mismatch = next(m for m in mismatches if m["field_name"] == "rg_15lb")
    assert rg_mismatch["current_value"] == "2.533"
    assert rg_mismatch["proposed_value"] == "2.6"


def test_check_accuracy_no_mismatch_within_tolerance():
    pairs = [{
        "product_id": "p1",
        "our_sku": {"weight_lbs": 15, "rg": 2.5335, "differential": 0.033, "mass_bias": None},
        "bigcommerce_product": {
            "id": 42,
            "custom_fields": [
                {"id": 1, "name": "Radius of Gyration(15lb)", "value": "2.533"},
                {"id": 2, "name": "Max Differential(15lb)", "value": "0.033"},
            ],
        },
    }]
    assert app.check_accuracy(pairs) == []


def test_check_accuracy_skips_when_bigcommerce_field_missing():
    pairs = [{
        "product_id": "p1",
        "our_sku": {"weight_lbs": 15, "rg": 2.533, "differential": 0.033, "mass_bias": 0.015},
        "bigcommerce_product": {"id": 42, "custom_fields": []},
    }]
    assert app.check_accuracy(pairs) == []  # nothing to compare against, not a "wrong" flag


def test_check_accuracy_skips_non_reference_weights_even_when_clearly_wrong():
    """The regression guard for the real bug this session's live check
    caught: BowlerDepot only ever publishes a 15lb-reference spec value,
    so comparing our 16lb SKU against it would be comparing two genuinely
    different real numbers and flagging a false-positive mismatch, not a
    real disagreement. A 16lb SKU must be skipped entirely, even when its
    values are wildly different from BowlerDepot's 15lb number."""
    pairs = [{
        "product_id": "p1",
        "our_sku": {"weight_lbs": 16, "rg": 99.0, "differential": 99.0, "mass_bias": 99.0},
        "bigcommerce_product": {
            "id": 42,
            "custom_fields": [
                {"id": 1, "name": "Radius of Gyration(15lb)", "value": "2.533"},
                {"id": 2, "name": "Max Differential(15lb)", "value": "0.033"},
            ],
        },
    }]
    assert app.check_accuracy(pairs) == []


def test_check_accuracy_handles_decimal_from_postgres():
    """Real bug (found and fixed proactively before this function's daily
    schedule ever ran against a real store -- see app.py's comment at the
    fix site): our_sku comes from get_product_skus() -> a Postgres
    'numeric' column -> Decimal via psycopg2, not float. BigCommerce's
    custom_fields values come through extract_specs_from_custom_fields()'s
    _to_float() and are plain float. abs(Decimal - float) raises TypeError
    -- the same root cause already fixed in pdf_parser and
    bowwwl_cross_check earlier in this deploy. Every prior test here used
    plain floats for our_sku, which is exactly why this would have shipped
    broken -- this test uses Decimal on our side to match what the real
    database actually returns."""
    pairs = [{
        "product_id": "p1",
        "our_sku": {"weight_lbs": 15, "rg": Decimal("2.533"), "differential": Decimal("0.033"), "mass_bias": None},
        "bigcommerce_product": {
            "id": 42,
            "custom_fields": [
                {"id": 1, "name": "Radius of Gyration(15lb)", "value": "2.600"},
                {"id": 2, "name": "Max Differential(15lb)", "value": "0.033"},
            ],
        },
    }]
    mismatches = app.check_accuracy(pairs)
    rg_mismatch = next(m for m in mismatches if m["field_name"] == "rg_15lb")
    assert rg_mismatch["current_value"] == "2.533"
    assert rg_mismatch["proposed_value"] == "2.6"


def test_check_accuracy_decimal_within_tolerance_not_flagged():
    """Same Decimal-vs-float shape as above, but values agree within
    tolerance -- must not be flagged just because the types differ."""
    pairs = [{
        "product_id": "p1",
        "our_sku": {"weight_lbs": 15, "rg": Decimal("2.5335"), "differential": Decimal("0.033"), "mass_bias": None},
        "bigcommerce_product": {
            "id": 42,
            "custom_fields": [
                {"id": 1, "name": "Radius of Gyration(15lb)", "value": "2.533"},
                {"id": 2, "name": "Max Differential(15lb)", "value": "0.033"},
            ],
        },
    }]
    assert app.check_accuracy(pairs) == []


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
