"""
Tests for src/admin_api/service.py.

Honesty note (see README): fastapi/pydantic/mangum weren't installable in
this sandbox (pip's proxy 403'd every attempt, same restriction as pytest
in the earlier modules), so app.py's actual HTTP routing is untested this
session -- only imports it, doesn't exercise it. What IS tested here is
everything that doesn't depend on those packages: the field_name parsing
and update-plan logic (pure functions, no DB), plus the approve/reject
control flow exercised against a small hand-rolled fake psycopg2-shaped
cursor/connection (real database interaction is untested for the same
reason it's untested in product_scraper/pdf_parser/image_processor -- no
Postgres instance available in this sandbox).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "admin_api"))

import service  # noqa: E402


# --- parse_review_field_name / build_update_plan: pure, no DB ---

def test_parse_sku_scoped_field_name():
    assert service.parse_review_field_name("rg_16lb") == {"scope": "sku", "column": "rg", "weight_lbs": 16}
    assert service.parse_review_field_name("mass_bias_9lb") == {"scope": "sku", "column": "mass_bias", "weight_lbs": 9}


def test_parse_product_scoped_field_name():
    assert service.parse_review_field_name("color") == {"scope": "product", "column": "color"}
    assert service.parse_review_field_name("published") == {"scope": "product", "column": "published"}


def test_parse_unrecognized_field_name_raises():
    """The injection guard: field_name ultimately becomes a SQL column
    name in execute_update_plan, so anything not in the whitelist or
    matching the SKU pattern must be rejected, not passed through."""
    try:
        service.parse_review_field_name("id; drop table products; --")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_parse_unrecognized_plain_field_name_raises():
    """A real product column that's simply NOT on the updatable whitelist
    (e.g. brand_id -- changing a product's brand via a review approval
    isn't a sane operation) should also be rejected."""
    try:
        service.parse_review_field_name("brand_id")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_build_update_plan_sku_field_casts_to_float():
    plan = service.build_update_plan("rg_16lb", "2.557")
    assert plan == {
        "table": "product_skus",
        "column": "rg",
        "value": 2.557,
        "where": {"weight_lbs": 16},
    }


def test_build_update_plan_product_field_stays_text():
    plan = service.build_update_plan("color", "Purple / Grey")
    assert plan == {
        "table": "products",
        "column": "color",
        "value": "Purple / Grey",
        "where": {},
    }


def test_build_update_plan_published_casts_to_bool():
    assert service.build_update_plan("published", "true")["value"] is True
    assert service.build_update_plan("published", "false")["value"] is False


# --- Fake DB layer: exercises execute_update_plan / approve / reject flow ---

class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._last_result = None
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        params = params or []
        q = " ".join(query.split())  # normalize whitespace for matching

        if q.startswith("update product_skus set"):
            column = q.split("set ", 1)[1].split(" =", 1)[0]
            value, product_id, weight_lbs = params
            key = (product_id, weight_lbs)
            self.db["product_skus"].setdefault(key, {})[column] = value
            self._last_result = None

        elif q.startswith("update products set") and "returning id" not in q:
            column = q.split("set ", 1)[1].split(" =", 1)[0]
            value, product_id = params
            self.db["products"].setdefault(product_id, {})[column] = value
            self._last_result = None

        elif q.startswith("update review_queue set status = 'approved'"):
            resolved_by, review_id = params
            row = self.db["review_queue"][review_id]
            row["status"] = "approved"
            row["resolved_by"] = resolved_by
            self._last_result = None

        elif q.startswith("update review_queue set status = 'rejected'"):
            resolved_by, note, review_id = params
            row = self.db["review_queue"][review_id]
            row["status"] = "rejected"
            row["resolved_by"] = resolved_by
            row["reason"] = f"{row.get('reason') or ''} | {note}".strip(" |")
            self._last_result = None

        elif q.startswith("select status from review_queue"):
            (review_id,) = params
            row = self.db["review_queue"].get(review_id)
            self._last_result = (row["status"],) if row else None
            self.description = [("status",)]

        elif q.startswith("update product_videos set status = 'approved'"):
            resolved_by, video_id = params
            row = self.db["product_videos"][video_id]
            row["status"] = "approved"
            row["resolved_by"] = resolved_by
            self._last_result = None

        elif q.startswith("update product_videos set status = 'rejected'"):
            resolved_by, video_id = params
            row = self.db["product_videos"][video_id]
            row["status"] = "rejected"
            row["resolved_by"] = resolved_by
            self._last_result = None

        elif q.startswith("select status from product_videos"):
            (video_id,) = params
            row = self.db["product_videos"].get(video_id)
            self._last_result = (row["status"],) if row else None
            self.description = [("status",)]

        else:
            raise NotImplementedError(f"FakeCursor doesn't support: {q}")

    def fetchone(self):
        return self._last_result


class FakeConnection:
    def __init__(self, db):
        self.db = db
        self.committed = False

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        self.committed = True


def _fake_db_with_pending_sku_mismatch():
    return {
        "review_queue": {
            "rq-1": {
                "id": "rq-1", "product_id": "prod-1", "field_name": "rg_16lb",
                "current_value": "2.577", "proposed_value": "2.557",
                "status": "pending", "reason": "HTML vs PDF disagree",
            },
        },
        "product_skus": {},
        "products": {},
    }


def test_approve_review_item_applies_plan_and_marks_approved(monkeypatch):
    db = _fake_db_with_pending_sku_mismatch()
    conn = FakeConnection(db)

    # get_review_item does a join query FakeCursor doesn't implement --
    # patch it directly to return the fake row shape approve_review_item
    # needs, since what's under test here is the approve/apply control
    # flow, not the SELECT's SQL text (covered by real DB integration
    # testing this sandbox can't do -- see module docstring).
    monkeypatch.setattr(service, "get_review_item", lambda c, rid: dict(db["review_queue"][rid]))

    result = service.approve_review_item(conn, "rq-1", resolved_by="al@bringyourbest.co")

    assert result["status"] == "approved"
    assert db["product_skus"][("prod-1", 16)]["rg"] == 2.557
    assert db["review_queue"]["rq-1"]["status"] == "approved"
    assert db["review_queue"]["rq-1"]["resolved_by"] == "al@bringyourbest.co"
    assert conn.committed is True


def test_approve_already_resolved_item_raises_and_does_not_reapply(monkeypatch):
    db = _fake_db_with_pending_sku_mismatch()
    db["review_queue"]["rq-1"]["status"] = "approved"
    conn = FakeConnection(db)
    monkeypatch.setattr(service, "get_review_item", lambda c, rid: dict(db["review_queue"][rid]))

    try:
        service.approve_review_item(conn, "rq-1", resolved_by="al@bringyourbest.co")
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert db["product_skus"] == {}  # nothing written


def test_reject_review_item_leaves_data_untouched():
    db = _fake_db_with_pending_sku_mismatch()
    conn = FakeConnection(db)

    result = service.reject_review_item(conn, "rq-1", resolved_by="al@bringyourbest.co", reason="HTML value confirmed correct")

    assert result["status"] == "rejected"
    assert db["review_queue"]["rq-1"]["status"] == "rejected"
    assert db["review_queue"]["rq-1"]["resolved_by"] == "al@bringyourbest.co"
    assert "HTML value confirmed correct" in db["review_queue"]["rq-1"]["reason"]
    assert db["product_skus"] == {}  # reject never touches the underlying value
    assert conn.committed is True


def test_reject_missing_review_item_raises():
    db = _fake_db_with_pending_sku_mismatch()
    conn = FakeConnection(db)
    try:
        service.reject_review_item(conn, "does-not-exist", resolved_by="al@bringyourbest.co")
        assert False, "expected LookupError"
    except LookupError:
        pass


# --- Video candidates (YouTube content enrichment): approve/reject flow ---
# Same fake-cursor-shaped-DB approach as review_queue above. _publish_video_
# summarize_message reads VIDEO_SUMMARIZE_QUEUE_URL directly from os.environ
# and returns False immediately when it's unset, so these tests don't need
# to fake boto3/SQS at all -- they exercise the "queue not configured yet"
# path, which is also the real state of a fresh deploy before that env var
# is wired up.

def _fake_db_with_pending_video_candidate():
    return {
        "product_videos": {
            "vid-1": {
                "id": "vid-1", "product_id": "prod-1",
                "youtube_video_id": "abc123", "status": "pending",
            },
        },
    }


def test_approve_video_candidate_marks_approved_without_queue_configured(monkeypatch):
    db = _fake_db_with_pending_video_candidate()
    conn = FakeConnection(db)
    monkeypatch.delenv("VIDEO_SUMMARIZE_QUEUE_URL", raising=False)

    result = service.approve_video_candidate(conn, "vid-1", resolved_by="al@bringyourbest.co")

    assert result["status"] == "approved"
    assert result["queued_for_summary"] is False
    assert db["product_videos"]["vid-1"]["status"] == "approved"
    assert db["product_videos"]["vid-1"]["resolved_by"] == "al@bringyourbest.co"
    assert conn.committed is True


def test_approve_already_resolved_video_candidate_raises():
    db = _fake_db_with_pending_video_candidate()
    db["product_videos"]["vid-1"]["status"] = "approved"
    conn = FakeConnection(db)

    try:
        service.approve_video_candidate(conn, "vid-1", resolved_by="al@bringyourbest.co")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_approve_missing_video_candidate_raises():
    db = _fake_db_with_pending_video_candidate()
    conn = FakeConnection(db)
    try:
        service.approve_video_candidate(conn, "does-not-exist", resolved_by="al@bringyourbest.co")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_reject_video_candidate_marks_rejected():
    db = _fake_db_with_pending_video_candidate()
    conn = FakeConnection(db)

    result = service.reject_video_candidate(conn, "vid-1", resolved_by="al@bringyourbest.co")

    assert result["status"] == "rejected"
    assert db["product_videos"]["vid-1"]["status"] == "rejected"
    assert conn.committed is True


if __name__ == "__main__":
    # Tiny monkeypatch shim so this file can run standalone the same way
    # as the other manual test runners in this repo, without pytest.
    class _MonkeyPatch:
        def __init__(self):
            self._sets = []
            self._env_sets = []

        def setattr(self, obj, name, value):
            self._sets.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

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
