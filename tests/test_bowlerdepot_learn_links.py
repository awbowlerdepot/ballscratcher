"""
Tests for src/bowlerdepot_learn_links/app.py -- the static Learn review
link written into each matched bowlerdepot.com product description
(runbook 6fj; see app.py's module docstring for the why).

The planning functions are pure and tested directly; the HTTP calls run
against a fake requests-Session; handler is exercised with the DB and
credential lookups monkeypatched, same split as
test_bowlerdepot_video_sync.py.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "bowlerdepot_learn_links"))

import app  # noqa: E402


LINK = (
    '<p class="bowleriq-learn-link"><a href="https://learn.bowlerdepot.com/articles/storm-ion-pro-pearl/">'
    "Read our full Storm Ion Pro Pearl review on BowlerDepot Learn</a></p>"
)


def test_ball_display_name_prefixes_brand_once():
    assert app.ball_display_name("Storm", "Ion Pro Pearl") == "Storm Ion Pro Pearl"
    assert app.ball_display_name("Storm", "Storm Ion Pro Pearl") == "Storm Ion Pro Pearl"
    assert app.ball_display_name(None, "Ion Pro Pearl") == "Ion Pro Pearl"


def test_build_link_paragraph_uses_trailing_slash_and_escapes():
    assert app.build_link_paragraph("storm-ion-pro-pearl", "Storm Ion Pro Pearl") == LINK
    assert "Roto Grip &amp; Co" in app.build_link_paragraph("x", "Roto Grip & Co")


def test_desired_description_appends_once_and_is_idempotent():
    once = app.desired_description("<p>Store copy.</p>\n", LINK)
    assert once == "<p>Store copy.</p>\n" + LINK
    assert app.desired_description(once, LINK) == once


def test_desired_description_replaces_stale_link_in_place_of_old():
    old = "<p>Store copy.</p>\n" + LINK.replace("storm-ion-pro-pearl", "old-slug")
    new = app.desired_description(old, LINK)
    assert new == "<p>Store copy.</p>\n" + LINK
    assert "old-slug" not in new


def test_desired_description_removes_link_and_keeps_store_copy():
    current = "<p>Store copy.</p>\n" + LINK
    assert app.desired_description(current, None) == "<p>Store copy.</p>"


def test_desired_description_never_touches_other_paragraphs():
    current = '<p class="lead">Keep me.</p>\n<p>And me.</p>'
    assert app.desired_description(current, None) == current
    assert app.desired_description(current, LINK).startswith(current)


def test_desired_description_handles_empty_description():
    assert app.desired_description("", LINK) == LINK
    assert app.desired_description(None, None) == ""


def test_plan_changes_add_update_remove_and_skip():
    products = [
        {"bigcommerce_product_id": 1, "brand_name": "Storm", "product_name": "Ion Pro Pearl", "slug": "storm-ion-pro-pearl"},
        {"bigcommerce_product_id": 2, "brand_name": "Storm", "product_name": "Ion Pro Pearl", "slug": "storm-ion-pro-pearl"},
        {"bigcommerce_product_id": 3, "brand_name": "Storm", "product_name": "Ion Pro Pearl", "slug": "storm-ion-pro-pearl"},
        {"bigcommerce_product_id": 4, "brand_name": "Storm", "product_name": "Phaze", "slug": None},
        {"bigcommerce_product_id": 5, "brand_name": "Storm", "product_name": "Gone", "slug": "storm-gone"},
        {"bigcommerce_product_id": 6, "brand_name": "Storm", "product_name": "No Article", "slug": None},
    ]
    descriptions = {
        "1": "<p>Copy.</p>",                                         # add
        "2": "<p>Copy.</p>\n" + LINK,                                # already right
        "3": "<p>Copy.</p>\n" + LINK.replace("Storm Ion", "Old"),    # update
        "4": "<p>Copy.</p>\n" + LINK,                                # remove
        # 5 isn't in BigCommerce any more: skipped
        "6": "<p>Copy.</p>",                                         # nothing to do
    }
    changes = app.plan_changes(products, descriptions)
    assert [(c["bigcommerce_product_id"], c["action"]) for c in changes] == [("1", "add"), ("3", "update"), ("4", "remove")]
    assert changes[0]["description"] == "<p>Copy.</p>\n" + LINK
    assert changes[2]["description"] == "<p>Copy.</p>"


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload, self.status = payload, status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"{self.status} Client Error")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, products=None, put_status=200):
        self.products = products or {}
        self.put_status = put_status
        self.gets, self.puts = [], []

    def get(self, url, headers, params, timeout):
        self.gets.append(params)
        ids = params["id:in"].split(",")
        return FakeResponse({"data": [{"id": int(i), "description": self.products[i]} for i in ids if i in self.products]})

    def put(self, url, headers, json, timeout):
        self.puts.append((url.rsplit("/", 1)[-1], json))
        return FakeResponse({}, self.put_status)


def test_fetch_descriptions_batches_by_250():
    ids = [str(i) for i in range(1, 302)]
    session = FakeSession({i: f"<p>{i}</p>" for i in ids})
    out = app.fetch_descriptions(session, "hash", "tok", ids)
    assert len(out) == 301 and out["7"] == "<p>7</p>"
    assert [len(g["id:in"].split(",")) for g in session.gets] == [250, 51]
    assert session.gets[0]["include_fields"] == "description"


def _run_handler(monkeypatch_rows, session, event):
    import requests

    class FakeConn:
        def close(self):
            pass

    originals = (app.get_db_connection, app.list_matched_products, app.get_bigcommerce_credentials, requests.Session)
    app.get_db_connection = lambda: FakeConn()
    app.list_matched_products = lambda conn: monkeypatch_rows
    app.get_bigcommerce_credentials = lambda: ("hash", "tok")
    requests.Session = lambda: session
    try:
        return json.loads(app.handler(event, None)["body"])
    finally:
        app.get_db_connection, app.list_matched_products, app.get_bigcommerce_credentials, requests.Session = originals


ROWS = [
    {"bigcommerce_product_id": 10, "brand_name": "Storm", "product_name": "Ion Pro Pearl", "slug": "storm-ion-pro-pearl"},
    {"bigcommerce_product_id": 11, "brand_name": "Storm", "product_name": "Phaze", "slug": None},
]


def test_handler_dry_run_writes_nothing():
    session = FakeSession({"10": "<p>Copy.</p>", "11": "<p>Copy.</p>"})
    body = _run_handler(ROWS, session, {"dry_run": True})
    assert session.puts == []
    assert body["planned"] == {"add": 1, "update": 0, "remove": 0}
    assert body["changes"] == [{"bigcommerce_product_id": "10", "action": "add"}]


def test_handler_writes_changes_and_reports_failures():
    session = FakeSession({"10": "<p>Copy.</p>", "11": "<p>Copy.</p>"})
    body = _run_handler(ROWS, session, {})
    assert session.puts == [("10", {"description": "<p>Copy.</p>\n" + LINK})]
    assert body["written"] == 1 and body["failed"] == 0

    forbidden = FakeSession({"10": "<p>Copy.</p>"}, put_status=403)
    body = _run_handler(ROWS, forbidden, {})
    assert body["written"] == 0 and body["failed"] == 1
    assert "403" in body["failures"][0]["error"]


def test_handler_prefers_the_row_with_an_article_for_a_shared_product():
    rows = [
        {"bigcommerce_product_id": 10, "brand_name": "Storm", "product_name": "Old", "slug": None},
        {"bigcommerce_product_id": 10, "brand_name": "Storm", "product_name": "Ion Pro Pearl", "slug": "storm-ion-pro-pearl"},
    ]
    session = FakeSession({"10": "<p>Copy.</p>\n" + LINK})
    body = _run_handler(rows, session, {})
    assert session.puts == [] and body["matched"] == 1


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            passed += 1
    print(f"{passed} passed")
