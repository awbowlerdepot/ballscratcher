"""Partner API v1 service: row mapping, cursors, and the /v1/changes sync
feed rules (runbook 6cj). Every result is also validated against the v1
pydantic models, so a mapping that drifts from the contract fails here."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "partner_api"))

import models  # noqa: E402
import service  # noqa: E402

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _row(pid, *, published=True, changed=T0, **kw):
    row = {
        "id": pid, "name": f"Ball {pid[:4]}", "brand_id": "b1111111-0000-0000-0000-000000000000",
        "brand_name": "Storm", "status": "current", "release_date": None, "color": "Red / Black",
        "coverstock_name": "R2S Pearl", "coverstock_material": "reactive_resin", "coverstock_type": "pearl",
        "core_name": "Velocity", "core_type": "symmetric", "has_particle": False,
        "factory_finish": "1500 Polished", "finish_category": "polished",
        "oil_rating": 9.1, "motion_rating": 14.4, "oil_motion_source": "estimated",
        "image_url": "https://img/x.png", "url": "https://storm/x", "article_slug": None,
        "bowlerdepot_url": None, "content_changed_at": changed, "published": published,
        "core_id": None, "coverstock_id": None,
    }
    row.update(kw)
    return row


class FakeCursor:
    """Answers the two queries the service issues: the BALL_SELECT scan
    (filtered/ordered/limited here like Postgres would) and the SKU lookup."""

    def __init__(self, db):
        self.db = db
        self.description = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=()):
        q = " ".join(query.split())
        if q.startswith("select product_id, weight_lbs, rg, differential, mass_bias from product_skus"):
            ids = set(params[0])
            rows = [s for s in self.db["skus"] if s["product_id"] in ids]
            cols = ["product_id", "weight_lbs", "rg", "differential", "mass_bias"]
        elif "(p.content_changed_at, p.id) > (%s, %s::uuid)" in q:
            pos_t, pos_id, _lag, limit = params
            cutoff = self.db["now"] - timedelta(seconds=service.CHANGE_LAG_SECONDS)
            rows = sorted(
                (r for r in self.db["products"]
                 if (r["content_changed_at"], r["id"]) > (pos_t, pos_id) and r["content_changed_at"] < cutoff),
                key=lambda r: (r["content_changed_at"], r["id"]),
            )[:limit]
            cols = list(_row("x" * 36).keys())
        elif "p.id > %s::uuid order by p.id limit %s" in q:
            after, limit = params
            rows = sorted((r for r in self.db["products"] if r["published"] and r["id"] > after), key=lambda r: r["id"])[:limit]
            cols = list(_row("x" * 36).keys())
        else:
            raise AssertionError(f"unexpected query: {q[:120]}")
        self.description = [(c,) for c in cols]
        self._rows = [tuple(r[c] for c in cols) for r in rows]

    def fetchall(self):
        return self._rows


class FakeConn:
    autocommit = True

    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)


def _id(n):
    return f"{n:08d}-0000-0000-0000-000000000000"


def _db(products, skus=(), now=T0 + timedelta(hours=1)):
    return {"products": list(products), "skus": list(skus), "now": now}


# --- mapping -------------------------------------------------------------

def test_map_ball_matches_the_v1_model_and_orders_weights_heaviest_first():
    skus = [
        {"product_id": _id(1), "weight_lbs": 14, "rg": 2.52, "differential": 0.048, "mass_bias": None},
        {"product_id": _id(1), "weight_lbs": 16, "rg": 2.48, "differential": 0.054, "mass_bias": None},
    ]
    ball = service.map_ball(_row(_id(1), article_slug="storm-phaze-ii"), skus)
    models.Ball.model_validate(ball)
    assert [w["weight_lbs"] for w in ball["weights"]] == [16, 14]
    assert ball["learn_article_url"] == "https://learn.bowlerdepot.com/articles/storm-phaze-ii"
    assert ball["plotter"] == {"oil": 9.1, "motion": 14.4, "source": "estimated"}


def test_map_ball_handles_no_position_no_specs_and_decimals():
    from decimal import Decimal

    ball = service.map_ball(_row(_id(2), oil_rating=None, motion_rating=None, oil_motion_source=None), [])
    models.Ball.model_validate(ball)
    assert ball["plotter"] is None and ball["weights"] == []
    w = service.map_weight({"weight_lbs": 15, "rg": Decimal("2.490"), "differential": Decimal("0.054"), "mass_bias": None})
    assert w == {"weight_lbs": 15, "rg": 2.49, "differential": 0.054, "mass_bias": None}


def test_models_reject_unknown_fields():
    ball = service.map_ball(_row(_id(3)), [])
    ball["internal_note"] = "should never leak"
    with pytest.raises(Exception):
        models.Ball.model_validate(ball)


# --- cursors -------------------------------------------------------------

def test_cursor_round_trip_and_garbage_is_rejected():
    c = service.encode_cursor({"t": T0.isoformat(), "id": _id(1)})
    assert service.decode_cursor(c) == {"t": T0.isoformat(), "id": _id(1)}
    for bad in ("not-a-cursor", "", service.encode_cursor({"id": "not-a-uuid"})):
        with pytest.raises(service.InvalidCursor):
            service.list_balls(FakeConn(_db([])), cursor=bad or "!!")


def test_clamp_limit():
    assert service.clamp_limit(None) == service.DEFAULT_PAGE_SIZE
    assert service.clamp_limit(0) == 1
    assert service.clamp_limit(10_000) == service.MAX_PAGE_SIZE


# --- /v1/balls -----------------------------------------------------------

def test_list_balls_pages_by_id_and_hides_unpublished():
    db = _db([_row(_id(i)) for i in (3, 1, 2)] + [_row(_id(4), published=False)])
    page1 = service.list_balls(FakeConn(db), limit=2)
    models.BallPage.model_validate(page1)
    assert [b["id"] for b in page1["items"]] == [_id(1), _id(2)]
    page2 = service.list_balls(FakeConn(db), cursor=page1["next_cursor"], limit=2)
    assert [b["id"] for b in page2["items"]] == [_id(3)]
    assert page2["next_cursor"] is None


# --- /v1/changes ---------------------------------------------------------

def test_first_sync_returns_published_balls_skips_removes_and_advances_past_them():
    db = _db([
        _row(_id(1), changed=T0),
        _row(_id(2), changed=T0 + timedelta(minutes=1), published=False),
        _row(_id(3), changed=T0 + timedelta(minutes=2)),
    ])
    page = service.list_changes(FakeConn(db))
    models.ChangePage.model_validate(page)
    assert [(c["id"], c["change"]) for c in page["items"]] == [(_id(1), "upsert"), (_id(3), "upsert")]
    assert page["has_more"] is False
    # Cursor sits after the LAST scanned row, so a re-poll returns nothing.
    again = service.list_changes(FakeConn(db), cursor=page["next_cursor"])
    assert again["items"] == [] and again["next_cursor"] == page["next_cursor"]


def test_incremental_sync_reports_upserts_and_removes_in_order():
    first = service.list_changes(FakeConn(_db([_row(_id(1), changed=T0)])))
    later = _db(
        [
            _row(_id(1), changed=T0 + timedelta(minutes=5), published=False),  # unpublished since
            _row(_id(2), changed=T0 + timedelta(minutes=6)),                   # new ball
        ],
        now=T0 + timedelta(hours=2),
    )
    page = service.list_changes(FakeConn(later), cursor=first["next_cursor"])
    models.ChangePage.model_validate(page)
    assert [(c["id"], c["change"]) for c in page["items"]] == [(_id(1), "remove"), (_id(2), "upsert")]
    assert page["items"][0]["ball"] is None and page["items"][1]["ball"]["id"] == _id(2)


def test_changes_hold_back_the_newest_minute():
    now = T0 + timedelta(hours=1)
    db = _db([_row(_id(1), changed=now - timedelta(seconds=10))], now=now)
    assert service.list_changes(FakeConn(db))["items"] == []


def test_changes_paging_with_ties_on_the_same_timestamp():
    db = _db([_row(_id(i), changed=T0) for i in range(1, 6)])
    seen, cursor = [], None
    while True:
        page = service.list_changes(FakeConn(db), cursor=cursor, limit=2)
        seen += [c["id"] for c in page["items"]]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert seen == [_id(i) for i in range(1, 6)]  # nothing skipped or repeated


def test_since_starts_from_a_time_and_bad_since_is_rejected():
    db = _db([_row(_id(1), changed=T0), _row(_id(2), changed=T0 + timedelta(minutes=10))])
    page = service.list_changes(FakeConn(db), since=(T0 + timedelta(minutes=1)).isoformat())
    assert [c["id"] for c in page["items"]] == [_id(2)]
    with pytest.raises(service.InvalidCursor):
        service.list_changes(FakeConn(db), since="yesterday")
