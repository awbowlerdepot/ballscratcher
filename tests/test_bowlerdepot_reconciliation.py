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


def test_fuzzy_match_rejects_distinguishing_suffix_real_incident():
    # Real incident, Al: "it is finding the 'Storm iQ Tour AI' instead of
    # the 'Storm iQ Tour'" -- a short appended word barely dents
    # SequenceMatcher's character-ratio score (this scores ~0.90,
    # comfortably above FUZZY_MATCH_THRESHOLD), but "AI" names a
    # genuinely different ball, not retail-listing noise on the same one.
    products = [{"id": 1, "name": "Storm iQ Tour AI", "sku": "X"}]
    match, ratio = app.fuzzy_match_product("Storm iQ Tour", products)
    assert match is None
    assert ratio == 0.0


def test_fuzzy_match_rejects_other_common_variant_suffixes():
    # Same shape, other real bowling-naming suffixes that name a
    # genuinely different ball, not filler.
    for suffix in ("Solid", "Pearl", "Hybrid", "Pro"):
        products = [{"id": 1, "name": f"Storm iQ Tour {suffix}", "sku": "X"}]
        match, ratio = app.fuzzy_match_product("Storm iQ Tour", products)
        assert match is None, f"should not match on '+{suffix}' suffix"


def test_names_token_compatible_allows_bowling_ball_suffix():
    # The one suffix this project already confirmed as harmless retail-
    # listing noise (FUZZY_MATCH_THRESHOLD's own calibration comment) --
    # the token-compatibility gate itself must still allow it. Tested
    # directly against the helper (rather than through fuzzy_match_
    # product's ratio/threshold) since SequenceMatcher's threshold math
    # is separately, proportionally sensitive to base-name length --
    # see test_fuzzy_match_close_but_not_exact for that existing,
    # unrelated behavior on a longer base name -- and this test should
    # only exercise the gate, not get entangled with it.
    assert app._names_token_compatible("Storm iQ Tour", "Storm iQ Tour Bowling Ball") is True


def test_fuzzy_match_close_but_not_exact_still_works_with_gate_added():
    # End-to-end confirmation that the new token-compatibility gate
    # didn't regress the pre-existing "+ Bowling Ball" tolerance through
    # the real fuzzy_match_product path (same pair test_fuzzy_match_
    # close_but_not_exact above already covers -- kept here so this
    # file's own real-incident test block for the token gate is
    # self-contained).
    products = [{"id": 1, "name": "Brunswick Fury Emerald/Black Hybrid Bowling Ball", "sku": "X"}]
    match, ratio = app.fuzzy_match_product("Brunswick Fury Emerald/Black Hybrid", products)
    assert match["id"] == 1
    assert 0.80 <= ratio < 1.0


def test_fuzzy_match_prefers_real_match_over_distinguishing_suffix_candidate():
    # The actual reported bug shape: both the real match AND the wrong
    # (suffixed) candidate are present -- the real one must win, and the
    # wrong one must never even be considered as a fallback.
    products = [
        {"id": 1, "name": "Storm iQ Tour AI", "sku": "WRONG"},
        {"id": 2, "name": "Storm iQ Tour", "sku": "RIGHT"},
    ]
    match, ratio = app.fuzzy_match_product("Storm iQ Tour", products)
    assert match["id"] == 2
    assert ratio == 1.0


def test_fuzzy_match_edition_suffix_matches_despite_short_base_name_ratio_gap():
    # Real incident, Al: "the word edition is not always used and is more
    # commonly not used. bowlerdepot.com does not use edition" -- adding
    # "edition" to _GENERIC_NAME_SUFFIX_TOKENS alone was NOT enough here.
    # "storm iq tour edition" vs "storm iq tour" scores a raw
    # SequenceMatcher ratio of ~0.7647 -- below FUZZY_MATCH_THRESHOLD
    # (0.80) -- purely because "edition" (7 chars) is proportionally
    # expensive against a short 14-char base name. The token-compatible
    # candidate must still win via TOKEN_COMPATIBLE_MIN_RATIO's floor.
    products = [{"id": 1, "name": "Storm iQ Tour", "sku": "REAL"}]
    match, ratio = app.fuzzy_match_product("Storm iQ Tour Edition", products)
    assert match["id"] == 1
    assert ratio == app.TOKEN_COMPATIBLE_MIN_RATIO


def test_fuzzy_match_edition_suffix_still_rejects_distinguishing_suffix_candidate():
    # Same real incident, both candidates present -- confirms the ratio
    # floor doesn't reopen the door to the original "AI" collision. The
    # AI candidate is still hard-rejected by the token gate before any
    # ratio (floored or not) is ever computed for it.
    products = [
        {"id": 1, "name": "Storm iQ Tour AI", "sku": "WRONG"},
        {"id": 2, "name": "Storm iQ Tour", "sku": "RIGHT"},
    ]
    match, ratio = app.fuzzy_match_product("Storm iQ Tour Edition", products)
    assert match["id"] == 2
    assert ratio == app.TOKEN_COMPATIBLE_MIN_RATIO


def test_fuzzy_match_token_compatible_ratio_floor_does_not_lower_high_ratios():
    # The floor must be a max(), never a clamp/override -- a candidate
    # that already scores above TOKEN_COMPATIBLE_MIN_RATIO on raw
    # character similarity (a long enough base name that "Bowling Ball"
    # only costs a small proportion of it) should keep its own, higher
    # raw ratio, not get pulled down to the floor.
    our_name = "Storm Phaze II Solid Reactive Pearl Hybrid Cover High Performance Ball"
    products = [{"id": 1, "name": our_name + " Bowling", "sku": "X"}]
    match, ratio = app.fuzzy_match_product(our_name, products)
    assert match["id"] == 1
    assert ratio > app.TOKEN_COMPATIBLE_MIN_RATIO


# --- upsert_bowlerdepot_match ---
# 018_bowlerdepot_products_dedupe_by_product.sql -- these use a minimal
# fake conn/cursor that just records the executed SQL/params, since this
# module has no established fake-Postgres-connection test pattern
# elsewhere in this file (handler/write_* functions are documented as
# needing real credentials to verify, per this file's own module
# docstring) -- good enough to pin the ON CONFLICT target itself, which
# is the exact thing that regressed in the real incident.

class _FakeCursor:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self.store["sql"] = sql
        self.store["params"] = params


class _FakeConn:
    def __init__(self):
        self.store = {}
        self.committed = False

    def cursor(self):
        return _FakeCursor(self.store)

    def commit(self):
        self.committed = True


def test_upsert_bowlerdepot_match_conflicts_on_product_id():
    # Real incident, Al: "it still finds the ai version" -- the OLD
    # conflict target was (bigcommerce_product_id, bigcommerce_sku), so a
    # corrected re-match for an already-matched product inserted a SECOND
    # row instead of overwriting the wrong one. Pinning the conflict
    # target in the executed SQL text so this specific regression can't
    # silently come back.
    conn = _FakeConn()
    app.upsert_bowlerdepot_match(conn, "product-1", "999", "SKU-1", "matched")
    sql = conn.store["sql"].lower()
    assert "on conflict (product_id)" in sql
    assert "on conflict (bigcommerce_product_id" not in sql
    assert conn.store["params"] == ("product-1", "999", "SKU-1", "matched")
    assert conn.committed is True


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


def test_handler_matches_unpublished_balls_but_only_flags_published_ones(monkeypatch):
    # Runbook 6dj: "why is it not finding the Brunswick TZone - Ocean Reef
    # price source on bowlerdepot.com" -- BowlerDepot lists it, but the
    # TZones are unpublished, and reconciliation only matched published balls.
    bc = [{"id": 101, "name": "Brunswick TZone Ocean Reef", "sku": ""},
          {"id": 102, "name": "Storm Phaze II", "sku": "", "custom_fields": []}]
    ours = [
        {"product_id": "tz", "brand_name": "Brunswick", "name": "TZone - Ocean Reef", "published": False},
        {"product_id": "ph", "brand_name": "Storm", "name": "Phaze II", "published": True},
        {"product_id": "un", "brand_name": "Brunswick", "name": "Unlisted Ball", "published": False},
        {"product_id": "pub", "brand_name": "Brunswick", "name": "Listed Nowhere", "published": True},
    ]
    upserts, not_listed, sku_lookups = [], [], []

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(app, "get_bigcommerce_credentials", lambda: ("store", "tok"))
    monkeypatch.setattr(app, "fetch_all_products", lambda s, t: bc)
    monkeypatch.setattr(app, "get_db_connection", lambda: Conn())
    monkeypatch.setattr(app, "list_products_to_match", lambda conn: ours)
    monkeypatch.setattr(app, "list_existing_matches", lambda conn: {})
    monkeypatch.setattr(app, "upsert_bowlerdepot_match",
                        lambda conn, pid, bid, sku, status, keep_matched=False: upserts.append((pid, bid, status)))
    monkeypatch.setattr(app, "write_not_yet_listed_item", lambda conn, pid, name: not_listed.append(pid))
    monkeypatch.setattr(app, "get_product_skus", lambda conn, pid: sku_lookups.append(pid) or [])
    monkeypatch.setattr(app, "write_review_items", lambda conn, m: None)
    app.handler({}, None)
    assert ("tz", "101", "matched") in upserts            # unpublished, but matched -> price source can be found
    assert not_listed == ["pub"]                           # only published balls make "not yet listed" items
    assert sku_lookups == ["ph"]                           # accuracy check only for published matches


# --- Runbook 6fn: the daily match also covers PUBLISHED RETIRED balls ---
# 1,732 of 1,747 published balls with no bowlerdepot_url had no
# bowlerdepot_products row at all, because list_our_current_products only
# selected status = 'current' and 1,679 of 1,924 published balls are
# retired. scripts/link_bowlerdepot_products.py backfilled 228 once; these
# pin the daily path that keeps it going, with the script's safety rules.

def _bc(id_, name, visible=True, availability="available", sku=""):
    return {"id": id_, "name": name, "sku": sku, "is_visible": visible, "availability": availability,
            "custom_fields": []}


def _retired(pid, brand, name):
    return {"product_id": pid, "brand_name": brand, "name": name, "published": True, "status": "retired"}


def _current(pid, brand, name, published=True):
    return {"product_id": pid, "brand_name": brand, "name": name, "published": published, "status": "current"}


def _run_handler(monkeypatch, bc, ours, existing=None):
    calls = {"upserts": [], "not_listed": [], "sku_lookups": [], "reviews": []}

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(app, "get_bigcommerce_credentials", lambda: ("store", "tok"))
    monkeypatch.setattr(app, "fetch_all_products", lambda s, t: bc)
    monkeypatch.setattr(app, "get_db_connection", lambda: Conn())
    monkeypatch.setattr(app, "list_products_to_match", lambda conn: ours)
    monkeypatch.setattr(app, "list_existing_matches", lambda conn: existing or {})
    monkeypatch.setattr(app, "upsert_bowlerdepot_match",
                        lambda conn, pid, bid, sku, status, keep_matched=False:
                        calls["upserts"].append((pid, bid, status, keep_matched)))
    monkeypatch.setattr(app, "write_not_yet_listed_item", lambda conn, pid, name: calls["not_listed"].append(pid))
    monkeypatch.setattr(app, "get_product_skus", lambda conn, pid: calls["sku_lookups"].append(pid) or [])
    monkeypatch.setattr(app, "write_review_items", lambda conn, m: calls["reviews"].append(m))
    calls["body"] = json.loads(app.handler({}, None)["body"])
    return calls


def test_list_products_to_match_selects_current_or_published_retired():
    store = {}

    class Cur(_FakeCursor):
        def fetchall(self):
            return [("p1", "Motiv", "Subzero Forge", True, "retired")]

    class Conn(_FakeConn):
        def cursor(self):
            return Cur(store)

    rows = app.list_products_to_match(Conn())
    sql = " ".join(store["sql"].split())
    assert "p.status = 'current' or (p.status = 'retired' and p.published)" in sql
    assert rows == [{"product_id": "p1", "brand_name": "Motiv", "name": "Subzero Forge",
                     "published": True, "status": "retired"}]


def test_is_purchasable():
    assert app.is_purchasable(_bc(1, "x"))
    assert app.is_purchasable(_bc(1, "x", availability="preorder"))
    assert not app.is_purchasable(_bc(1, "x", visible=False))
    assert not app.is_purchasable(_bc(1, "x", availability="disabled"))
    assert not app.is_purchasable({"id": 1, "name": "x"})  # field missing: not proven live


def test_handler_matches_retired_ball_to_visible_listing_without_review_items(monkeypatch):
    bc = [_bc(4017, "Motiv Subzero Forge", sku="MSF")]
    calls = _run_handler(monkeypatch, bc, [_retired("r1", "Motiv", "Subzero Forge"),
                                           _retired("r2", "Motiv", "Not On The Store")])
    assert calls["upserts"] == [("r1", "4017", "matched", True)]
    assert calls["not_listed"] == []       # no "not yet listed" item for a retired ball
    assert calls["sku_lookups"] == []      # no accuracy check for a retired ball either
    assert calls["body"]["retired_matched_count"] == 1


def test_handler_skips_retired_ball_whose_listing_is_hidden_or_disabled(monkeypatch):
    bc = [_bc(1, "Storm Hidden Ball", visible=False), _bc(2, "Storm Disabled Ball", availability="disabled")]
    calls = _run_handler(monkeypatch, bc, [_retired("r1", "Storm", "Hidden Ball"),
                                           _retired("r2", "Storm", "Disabled Ball")])
    assert calls["upserts"] == []
    assert calls["not_listed"] == []


def test_handler_still_matches_current_ball_to_hidden_listing(monkeypatch):
    # Current balls keep their pre-6fn behavior; only retired ones are gated.
    calls = _run_handler(monkeypatch, [_bc(1, "Storm Phaze II", visible=False)], [_current("c1", "Storm", "Phaze II")])
    assert calls["upserts"] == [("c1", "1", "matched", False)]


def test_retired_match_prefers_visible_twin_over_earlier_hidden_duplicate():
    bc = [_bc(10, "Storm Ball X", visible=False), _bc(11, "STORM Ball X!")]
    product, ratio = app.pick_retired_listing("Storm Ball X", bc)
    assert product["id"] == 11 and ratio == 1.0


def test_retired_match_two_visible_twins_is_no_match_unless_one_is_already_ours():
    bc = [_bc(10, "Storm Ball X"), _bc(11, "Storm Ball X")]
    assert app.pick_retired_listing("Storm Ball X", bc) == (None, 0.0)
    product, _ = app.pick_retired_listing("Storm Ball X", bc, existing_id="11")
    assert product["id"] == 11


def test_retired_match_keeps_6h1_token_gate():
    # "iQ Tour" must not reach "iQ Tour AI" just because it's the only live listing.
    bc = [_bc(1, "Storm iQ Tour", visible=False), _bc(2, "Storm iQ Tour AI")]
    assert app.pick_retired_listing("Storm iQ Tour", bc) == (None, 0.0)


def test_handler_rerun_reproduces_link_script_rows(monkeypatch):
    # The 228 rows link_bowlerdepot_products.py wrote on 2026-11-01 are exact
    # matches to a live, unclaimed listing -- a daily run must write back
    # the very same id and status.
    bc = [_bc(4017, "Motiv Subzero Forge"), _bc(4507, "Roto Grip Hustle SOS")]
    existing = {"r1": {"bigcommerce_product_id": "4017", "match_status": "matched"},
                "r2": {"bigcommerce_product_id": "4507", "match_status": "matched"}}
    calls = _run_handler(monkeypatch, bc, [_retired("r1", "Motiv", "Subzero Forge"),
                                           _retired("r2", "Roto Grip", "Hustle SOS")], existing)
    assert sorted(calls["upserts"]) == [("r1", "4017", "matched", True), ("r2", "4507", "matched", True)]


def test_handler_never_downgrades_retired_matched_row_to_ambiguous(monkeypatch):
    # Listing renamed with a "Bowling Ball" suffix -> today's best is 'ambiguous'.
    bc = [_bc(4017, "Motiv Subzero Forge Bowling Ball")]
    existing = {"r1": {"bigcommerce_product_id": "4017", "match_status": "matched"}}
    calls = _run_handler(monkeypatch, bc, [_retired("r1", "Motiv", "Subzero Forge")], existing)
    assert calls["upserts"] == []
    assert calls["body"]["retired_kept_matched_count"] == 1


def test_handler_writes_new_retired_ambiguous_match_as_ambiguous(monkeypatch):
    # No existing row: recorded, but as 'ambiguous' (never trusted, 6h.1).
    calls = _run_handler(monkeypatch, [_bc(7, "Motiv Subzero Forge Bowling Ball")],
                         [_retired("r1", "Motiv", "Subzero Forge")])
    assert calls["upserts"] == [("r1", "7", "ambiguous", True)]


def test_handler_retired_ball_never_takes_a_listing_another_ball_holds(monkeypatch):
    # A current ball and its retired namesake both match listing 5: 6fg would
    # drop bigcommerce_product_id from both, so the retired one stays out.
    bc = [_bc(5, "Motiv Ascend")]
    calls = _run_handler(monkeypatch, bc, [_current("c1", "Motiv", "Ascend"), _retired("r1", "Motiv", "Ascend")])
    assert calls["upserts"] == [("c1", "5", "matched", False)]
    assert calls["body"]["retired_contested_count"] == 1


def test_handler_retired_ball_skips_listing_held_by_an_existing_row(monkeypatch):
    # e.g. an unpublished ball (not in this run) already holds listing 5.
    existing = {"other": {"bigcommerce_product_id": "5", "match_status": "ambiguous"}}
    calls = _run_handler(monkeypatch, [_bc(5, "Motiv Ascend")], [_retired("r1", "Motiv", "Ascend")], existing)
    assert calls["upserts"] == []


def test_handler_two_retired_balls_wanting_one_listing_both_skipped(monkeypatch):
    bc = [_bc(5, "Motiv Ascend")]
    calls = _run_handler(monkeypatch, bc, [_retired("r1", "Motiv", "Ascend"), _retired("r2", "MOTIV", "Ascend")])
    assert calls["upserts"] == []
    assert calls["body"]["retired_contested_count"] == 2


def test_handler_warns_when_current_ball_matches_listing_a_retired_ball_holds(monkeypatch):
    existing = {"r1": {"bigcommerce_product_id": "5", "match_status": "matched"}}
    calls = _run_handler(monkeypatch, [_bc(5, "Motiv Ascend")],
                         [_current("c1", "Motiv", "Ascend"), _retired("r1", "Motiv", "Ascend")], existing)
    assert ("c1", "5", "matched", False) in calls["upserts"]
    assert all(u[0] != "r1" for u in calls["upserts"])   # the retired row is left alone
    assert calls["body"]["shared_with_retired_count"] == 1


def test_upsert_bowlerdepot_match_keep_matched_guard_in_sql():
    conn = _FakeConn()
    app.upsert_bowlerdepot_match(conn, "r1", "4017", None, "ambiguous", keep_matched=True)
    sql = " ".join(conn.store["sql"].split())
    assert "where bowlerdepot_products.match_status <> 'matched' or excluded.match_status = 'matched'" in sql
    assert conn.store["params"] == ("r1", "4017", None, "ambiguous")
    conn = _FakeConn()
    app.upsert_bowlerdepot_match(conn, "c1", "1", None, "ambiguous")
    assert "excluded.match_status = 'matched'" not in conn.store["sql"]
