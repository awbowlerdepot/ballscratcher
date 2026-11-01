"""
Tests for scripts/link_bowlerdepot_products.py (runbook 6fm).

The planning functions are pure, so most of this feeds them real-shaped
BigCommerce products and bowlerdepot_products / product_price_sources rows.
The two write paths get a fake connection that records SQL, to pin what
--apply may and may not touch: it inserts bowlerdepot_products rows only
(never UPDATEs one, so an existing matched or ambiguous row is never
overwritten), and approves price sources only when asked.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import link_bowlerdepot_products as script  # noqa: E402

BASE = "https://bowlerdepot.com"


def bc(id_, name, visible=True, availability="available", categories=(26,), url=None):
    return {"id": id_, "name": name, "is_visible": visible, "availability": availability,
            "categories": list(categories), "custom_url": {"url": url or f"/{id_}/"}}


def ball(pid, brand, name, status="current"):
    return {"product_id": pid, "brand_name": brand, "name": name, "status": status}


BRANDS = [{"id": 1, "name": "Storm"}, {"id": 2, "name": "Brunswick"}, {"id": 3, "name": "Motiv"},
          {"id": 4, "name": "Roto Grip"}]


def plan(balls, bc_products, existing=None):
    return {r["product_id"]: r for r in script.plan_links(balls, existing or {}, bc_products, bc_products, BRANDS)}


# --- match_tier ---------------------------------------------------------------

def test_exact_normalized_name_is_high():
    assert script.match_tier("Storm PHAZE II", "Storm Phaze II") == "high"
    assert script.match_tier("Brunswick TZone - Ocean Reef", "Brunswick TZone Ocean Reef") == "high"


def test_viz_a_ball_suffix_is_medium_only():
    assert script.match_tier("Brunswick Sea Turtle", "Brunswick Sea Turtle  Viz-A-Ball") == "medium"


def test_reordered_or_respaced_colors_are_medium():
    # "RED/WHITE/NAVY" normalizes to "redwhitenavy", so it isn't exact.
    assert script.match_tier("Storm MIX RED/WHITE/NAVY", "Storm Mix Red / White / Navy") == "medium"
    assert script.match_tier("Columbia 300 White Dot - Scarlet / Gold / Black",
                             "Columbia 300 White Dot Scarlet / Black / Gold") == "medium"


def test_storm_iq_tour_ai_is_not_a_match():
    # Runbook 6h.1: a distinguishing suffix word is a different ball.
    assert script.match_tier("Storm iQ Tour", "Storm iQ Tour AI") is None
    assert script.match_tier("Storm Phaze", "Storm Phaze II") is None


def test_generic_words_alone_never_match():
    assert script.match_tier("Brunswick Ball", "Brunswick Viz-a-Ball", "Brunswick") is None
    assert script.match_tier("Brunswick White Ball", "Brunswick White Viz-a-Ball", "Brunswick") == "medium"


# --- ball_products ------------------------------------------------------------

def test_ball_products_keeps_only_the_balls_tree():
    catalog = {
        "categories": [{"category_id": 25, "parent_id": 0, "name": "Balls"},
                       {"category_id": 26, "parent_id": 25, "name": "High Performance"},
                       {"category_id": 90, "parent_id": 0, "name": "Bags"}],
        "products": [bc(1, "Storm Phaze II", categories=[26]), bc(2, "Storm Phaze II Bag", categories=[90])],
    }
    assert [p["id"] for p in script.ball_products(catalog)] == [1]


def test_ball_products_falls_back_to_everything_without_the_category():
    catalog = {"categories": [], "products": [bc(1, "a"), bc(2, "b")]}
    assert len(script.ball_products(catalog)) == 2


# --- plan_links ---------------------------------------------------------------

def test_retired_exact_visible_match_is_proposed():
    rows = plan([ball("p1", "Storm", "Phaze II", "retired")], [bc(10, "Storm Phaze II")])
    assert rows["p1"]["reason"] == "propose_link"
    assert rows["p1"]["bc_id"] == "10"


def test_hidden_listing_is_never_proposed():
    rows = plan([ball("p1", "Storm", "Phaze II")], [bc(10, "Storm Phaze II", visible=False)])
    assert rows["p1"]["reason"] == "listing_hidden"
    rows = plan([ball("p1", "Storm", "Phaze II")], [bc(10, "Storm Phaze II", availability="disabled")])
    assert rows["p1"]["reason"] == "listing_hidden"


def test_visible_listing_wins_over_a_hidden_duplicate():
    rows = plan([ball("p1", "Storm", "Phaze II")], [bc(9, "Storm Phaze II", visible=False), bc(10, "Storm Phaze II")])
    assert rows["p1"]["reason"] == "propose_link"
    assert rows["p1"]["bc_id"] == "10"


def test_two_visible_listings_with_the_same_name_are_contested():
    rows = plan([ball("p1", "Storm", "Phaze II")], [bc(9, "Storm Phaze II"), bc(10, "Storm Phaze II")])
    assert rows["p1"]["reason"] == "contested"


def test_two_of_our_balls_wanting_one_listing_are_both_contested():
    rows = plan([ball("p1", "Motiv", "Ascend"), ball("p2", "Motiv", "Ascend")], [bc(10, "Motiv Ascend")])
    assert rows["p1"]["reason"] == rows["p2"]["reason"] == "contested"


def test_listing_already_claimed_by_another_row_is_contested():
    existing = {"other": {"bigcommerce_product_id": "10", "match_status": "ambiguous"}}
    rows = plan([ball("p1", "Storm", "Phaze II", "retired")], [bc(10, "Storm Phaze II")], existing)
    assert rows["p1"]["reason"] == "contested"


def test_existing_rows_are_reported_not_replanned():
    existing = {
        "p1": {"bigcommerce_product_id": "10", "match_status": "matched"},
        "p2": {"bigcommerce_product_id": "11", "match_status": "ambiguous"},
        "p3": {"bigcommerce_product_id": "12", "match_status": "matched"},
        "p4": {"bigcommerce_product_id": "12", "match_status": "matched"},
    }
    products = [bc(10, "Storm Phaze II"), bc(11, "Storm Hy-Road X"), bc(12, "Storm Mix")]
    rows = plan([ball("p1", "Storm", "Phaze II"), ball("p2", "Storm", "Hy-Road"),
                 ball("p3", "Storm", "Mix A"), ball("p4", "Storm", "Mix B")], products, existing)
    assert rows["p1"]["reason"] == "linked_ok"
    assert rows["p2"]["reason"] == "linked_ambiguous"
    assert rows["p3"]["reason"] == rows["p4"]["reason"] == "linked_shared"


def test_brand_not_carried_vs_not_listed():
    rows = plan([ball("p1", "SWAG", "Craze Pearl Bowling Ball"), ball("p2", "Storm", "Identity")],
                [bc(10, "Storm Phaze II")])
    assert rows["p1"]["reason"] == "brand_not_carried"
    assert rows["p2"]["reason"] == "not_on_bowlerdepot"


def test_medium_match_goes_to_review():
    rows = plan([ball("p1", "Brunswick", "Sea Turtle")], [bc(10, "Brunswick Sea Turtle Viz-A-Ball")])
    assert rows["p1"]["reason"] == "review_link"
    assert rows["p1"]["tier"] == "medium"


# --- plan_price_sources -------------------------------------------------------

def _sources(pid, *rows):
    return {pid: [dict({"id": f"s{i}", "is_active": True, "product_url": None}, **r) for i, r in enumerate(rows)]}


def _price(link_rows, sources, products):
    return script.plan_price_sources(list(link_rows.values()), sources, {str(p["id"]): p for p in products}, BASE)


def test_proposed_link_without_a_source_gets_one_created():
    products = [bc(10, "Storm Phaze II", url="/storm-phaze-ii/")]
    rows = plan([ball("p1", "Storm", "Phaze II", "retired")], products)
    [act] = _price(rows, {}, products)
    assert act["action"] == "create"
    assert act["product_url"] == "https://bowlerdepot.com/storm-phaze-ii/"


def test_pending_source_on_the_linked_listing_is_approved():
    existing = {"p1": {"bigcommerce_product_id": "10", "match_status": "matched"}}
    products = [bc(10, "Hammer Blacklist")]
    rows = plan([ball("p1", "Hammer", "Blacklist")], products, existing)
    [act] = _price(rows, _sources("p1", {"status": "pending", "external_product_id": "10"}), products)
    assert act["action"] == "approve"
    assert rows["p1"]["reason"] == "linked_price_source_issue"


def test_rejected_source_on_a_different_listing_is_repointed():
    # Roto Grip Hustle SOS: the old row pointed at "Hustle HSB" (676) and was rejected.
    existing = {"p1": {"bigcommerce_product_id": "4507", "match_status": "matched"}}
    products = [bc(4507, "Roto Grip Hustle SOS")]
    rows = plan([ball("p1", "Roto Grip", "Hustle SOS")], products, existing)
    [act] = _price(rows, _sources("p1", {"status": "rejected", "external_product_id": "676"}), products)
    assert act["action"] == "repoint"
    assert act["source_id"] == "s0"


def test_rejected_on_the_same_listing_is_left_alone():
    existing = {"p1": {"bigcommerce_product_id": "10", "match_status": "matched"}}
    products = [bc(10, "Storm Phaze II")]
    rows = plan([ball("p1", "Storm", "Phaze II")], products, existing)
    [act] = _price(rows, _sources("p1", {"status": "rejected", "external_product_id": "10"}), products)
    assert act["action"] == "none"


def test_approved_source_on_a_different_listing_is_reported():
    existing = {"p1": {"bigcommerce_product_id": "10", "match_status": "matched"}}
    products = [bc(10, "Storm Phaze II")]
    rows = plan([ball("p1", "Storm", "Phaze II")], products, existing)
    [act] = _price(rows, _sources("p1", {"status": "approved", "external_product_id": "99"}), products)
    assert act["action"] == "none"
    assert rows["p1"]["reason"] == "linked_price_source_issue"


def test_fully_linked_ball_stays_ok():
    existing = {"p1": {"bigcommerce_product_id": "10", "match_status": "matched"}}
    products = [bc(10, "Storm Phaze II")]
    rows = plan([ball("p1", "Storm", "Phaze II")], products, existing)
    [act] = _price(rows, _sources("p1", {"status": "approved", "external_product_id": "10"}), products)
    assert act["action"] == "none"
    assert rows["p1"]["reason"] == "linked_ok"


def test_ambiguous_and_contested_get_no_price_source_action():
    existing = {"p1": {"bigcommerce_product_id": "10", "match_status": "ambiguous"}}
    products = [bc(10, "Storm Phaze II"), bc(11, "Motiv Ascend")]
    rows = plan([ball("p1", "Storm", "Phaze II"), ball("p2", "Motiv", "Ascend"), ball("p3", "Motiv", "Ascend")],
                products, existing)
    assert _price(rows, {}, products) == []


def test_existing_link_to_a_hidden_listing_is_flagged():
    existing = {"p1": {"bigcommerce_product_id": "10", "match_status": "matched"}}
    products = [bc(10, "Storm Phaze II", visible=False)]
    rows = plan([ball("p1", "Storm", "Phaze II")], products, existing)
    assert _price(rows, {}, products) == []
    assert rows["p1"]["reason"] == "linked_price_source_issue"


# --- load_accepted / write_csv ------------------------------------------------

def test_csv_round_trip_accepts_only_marked_rows(tmp_path):
    products = [bc(10, "Brunswick Sea Turtle Viz-A-Ball"), bc(11, "Brunswick World Viz-a-Ball")]
    rows = plan([ball("p1", "Brunswick", "Sea Turtle"), ball("p2", "Brunswick", "World")], products)
    path = tmp_path / "links.csv"
    script.write_csv(str(path), list(rows.values()), [])
    text = path.read_text().splitlines()
    assert text[0].split(",")[:3] == ["reason", "tier", "accept"]
    edited = [text[0]] + [line.replace("review_link,medium,,", "review_link,medium,y,", 1) if ",World," in line
                          else line for line in text[1:]]
    path.write_text("\n".join(edited) + "\n")
    assert script.load_accepted(str(path)) == {"p2"}


# --- write paths (fake connection) --------------------------------------------

class FakeCursor:
    def __init__(self, log, rowcount):
        self.log, self.rowcount = log, rowcount

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, rowcount=1):
        self.log, self.rowcount, self.commits = [], rowcount, 0

    def cursor(self):
        return FakeCursor(self.log, self.rowcount)

    def commit(self):
        self.commits += 1


def test_apply_links_only_inserts_and_guards_claims():
    conn = FakeConn()
    written = script.apply_links(conn, [{"product_id": "p1", "bc_id": "10"}])
    assert written == ["p1"]
    [(sql, params)] = conn.log
    assert sql.startswith("insert into bowlerdepot_products")
    assert "on conflict (product_id) do nothing" in sql
    assert "not exists" in sql and "'matched', 'ambiguous'" in sql
    assert "'matched'" in sql
    assert not any(s.startswith("update bowlerdepot_products") for s, _ in conn.log)


def test_apply_links_reports_rows_that_lost_the_race():
    conn = FakeConn(rowcount=0)
    assert script.apply_links(conn, [{"product_id": "p1", "bc_id": "10"}]) == []


ACTIONS = [
    {"product_id": "p1", "action": "create", "bc_id": "10", "product_url": BASE + "/a/", "source_id": None},
    {"product_id": "p2", "action": "approve", "bc_id": "11", "product_url": BASE + "/b/", "source_id": "s2"},
    {"product_id": "p3", "action": "repoint", "bc_id": "12", "product_url": BASE + "/c/", "source_id": "s3"},
    {"product_id": "p4", "action": "create", "bc_id": "13", "product_url": BASE + "/d/", "source_id": None},
]


def test_price_sources_default_to_pending_and_skip_approve():
    conn = FakeConn()
    result = script.apply_price_sources(conn, ACTIONS, "site", {"p1", "p2", "p3"}, approve=False)
    assert result["counts"] == {"create": 1, "repoint": 1}
    assert result["approved_products"] == []
    sqls = [s for s, _ in conn.log]
    assert not any("content_changed_at" in s for s in sqls)
    assert all(p[4 if s.startswith("insert") else 2] == "pending" for s, p in conn.log)


def test_price_sources_approve_bumps_content_changed_at():
    conn = FakeConn()
    result = script.apply_price_sources(conn, ACTIONS, "site", {"p1", "p2", "p3"}, approve=True)
    assert result["counts"] == {"create": 1, "approve": 1, "repoint": 1}
    assert sorted(result["approved_products"]) == ["p1", "p2", "p3"]
    sql, params = conn.log[-1]
    assert sql.startswith("update products set content_changed_at")
    assert sorted(params[0]) == ["p1", "p2", "p3"]


def test_price_sources_skip_products_that_were_not_linked():
    conn = FakeConn()
    result = script.apply_price_sources(conn, ACTIONS, "site", set(), approve=True)
    assert result["counts"] == {}
    assert conn.log == []


# --- fetch_all ----------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(params["page"])
        return self.responses.pop(0)


def test_fetch_all_waits_out_a_429_and_pages(monkeypatch):
    monkeypatch.setattr(script.time, "sleep", lambda s: None)
    session = FakeSession([
        FakeResponse(429, headers={"X-Rate-Limit-Time-Reset-Ms": "10"}),
        FakeResponse(200, {"data": [1], "meta": {"pagination": {"total_pages": 2}}}),
        FakeResponse(200, {"data": [2], "meta": {"pagination": {"total_pages": 2}}}),
    ])
    assert script.fetch_all("h", "t", "products", {}, session=session) == [1, 2]
    assert session.calls == [1, 1, 2]
