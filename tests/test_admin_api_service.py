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

        elif q.startswith("select status, youtube_video_id from product_videos"):
            (video_id,) = params
            row = self.db["product_videos"].get(video_id)
            self._last_result = (row["status"], row["youtube_video_id"]) if row else None
            self.description = [("status",), ("youtube_video_id",)]

        elif q.startswith("select status from product_videos"):
            (video_id,) = params
            row = self.db["product_videos"].get(video_id)
            self._last_result = (row["status"],) if row else None
            self.description = [("status",)]

        elif q.startswith("select id, youtube_video_id from product_videos"):
            (video_id,) = params
            row = self.db["product_videos"].get(video_id)
            self._last_result = (row["id"], row["youtube_video_id"]) if row else None
            self.description = [("id",), ("youtube_video_id",)]

        elif q.startswith("select id from products"):
            (product_id,) = params
            row = self.db["products"].get(product_id)
            # "products" here doubles as the update-plan sink elsewhere in
            # this file (setdefault(product_id, {})), so a product "exists"
            # for this fake if its key is present at all, whatever its value.
            self._last_result = (product_id,) if product_id in self.db["products"] else None
            self.description = [("id",)]

        elif q.startswith("select id from product_videos where product_id = %s and youtube_video_id = %s"):
            target_product_id, youtube_video_id = params
            match = next(
                (v for v in self.db["product_videos"].values()
                 if v["product_id"] == target_product_id and v["youtube_video_id"] == youtube_video_id),
                None,
            )
            self._last_result = (match["id"],) if match else None
            self.description = [("id",)]

        elif q.startswith("update product_videos set product_id = %s"):
            new_product_id, video_id = params
            self.db["product_videos"][video_id]["product_id"] = new_product_id
            self._last_result = None

        elif q.startswith("select id from product_videos where id = %s"):
            (video_id,) = params
            row = self.db["product_videos"].get(video_id)
            self._last_result = (row["id"],) if row else None
            self.description = [("id",)]

        elif q.startswith("delete from product_videos where id = %s"):
            (video_id,) = params
            self.db["product_videos"].pop(video_id, None)
            self._last_result = None

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


# --- list_video_candidates: pagination-stability regression test ---
# Real bug found via a live full-catalog run of
# auto_approve_video_candidates.py: this query orders by (match_confidence,
# created_at) only, and rows inserted in the same video_discovery
# invocation often share a created_at timestamp -- ties with no
# deterministic tiebreaker make OFFSET/LIMIT pagination unstable, and a
# candidate showed up on two different pages, got approved twice, and the
# second attempt hit a real 422. This test doesn't run the query against a
# real DB (no Postgres in this sandbox) -- it just captures the SQL text
# and confirms pv.id is present as a final ORDER BY tiebreaker, which is
# what actually fixes the instability.

class _QueryCapturingCursor:
    def __init__(self):
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.queries.append(" ".join(query.split()))

    @property
    def description(self):
        return []

    def fetchall(self):
        return []


class _QueryCapturingConnection:
    def __init__(self):
        self._cursor = _QueryCapturingCursor()

    def cursor(self):
        return self._cursor


def test_list_video_candidates_orders_by_id_as_tiebreaker():
    conn = _QueryCapturingConnection()
    service.list_video_candidates(conn, status="pending", limit=200, offset=0)

    query = conn.cursor().queries[0]
    assert "order by pv.match_confidence asc, pv.created_at asc, pv.id asc" in query


# --- Video candidates (YouTube content enrichment): approve/reject flow ---
# Same fake-cursor-shaped-DB approach as review_queue above.
# approve_video_candidate deliberately does NOT publish to
# VIDEO_SUMMARIZE_QUEUE_URL / video_transcript_fetcher anymore -- see its
# docstring for why (that Lambda path is confirmed broken by PoToken, and
# auto-queuing to it on every approval would race-poison transcript_note
# before the working home browser cron ever got a chance). These tests
# confirm approval just marks the row approved and returns a plain result,
# nothing SQS-shaped.

def _fake_db_with_pending_video_candidate():
    return {
        "product_videos": {
            "vid-1": {
                "id": "vid-1", "product_id": "prod-1",
                "youtube_video_id": "abc123", "status": "pending",
            },
        },
    }


def test_approve_video_candidate_marks_approved_and_does_not_touch_queue(monkeypatch):
    """No SQS publish at all anymore -- confirmed here by making sure
    boto3 isn't even touched: if approve_video_candidate regressed back to
    calling something SQS-shaped, importing a fake boto3 that raises on any
    attribute access would catch it."""
    db = _fake_db_with_pending_video_candidate()
    conn = FakeConnection(db)

    class _ExplodingBoto3:
        def __getattr__(self, name):
            raise AssertionError(f"approve_video_candidate should not touch boto3.{name}")

    import sys as _sys
    real_boto3 = _sys.modules.get("boto3")
    _sys.modules["boto3"] = _ExplodingBoto3()
    try:
        result = service.approve_video_candidate(conn, "vid-1", resolved_by="al@bringyourbest.co")
    finally:
        if real_boto3 is not None:
            _sys.modules["boto3"] = real_boto3
        else:
            del _sys.modules["boto3"]

    assert result == {"video_id": "vid-1", "status": "approved"}
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


# --- reassign_video_candidate / delete_video_candidate: correction tools
# for score_match's known false-positive shape (see
# reassign_video_candidate's docstring) -- e.g. a "Storm Absolute Power"
# video landing on the "Storm Absolute" product. These tests need a
# db["products"] dict too (existence-checked by reassign), unlike the
# plain approve/reject tests above.

def _fake_db_with_two_products_and_one_video():
    db = _fake_db_with_pending_video_candidate()
    db["products"] = {"prod-1": {}, "prod-2": {}}
    return db


def test_reassign_video_candidate_moves_to_new_product():
    db = _fake_db_with_two_products_and_one_video()
    conn = FakeConnection(db)

    result = service.reassign_video_candidate(conn, "vid-1", "prod-2")

    assert result == {"video_id": "vid-1", "product_id": "prod-2"}
    assert db["product_videos"]["vid-1"]["product_id"] == "prod-2"
    assert conn.committed is True


def test_reassign_missing_video_candidate_raises():
    db = _fake_db_with_two_products_and_one_video()
    conn = FakeConnection(db)
    try:
        service.reassign_video_candidate(conn, "does-not-exist", "prod-2")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_reassign_to_missing_product_raises():
    db = _fake_db_with_two_products_and_one_video()
    conn = FakeConnection(db)
    try:
        service.reassign_video_candidate(conn, "vid-1", "does-not-exist")
        assert False, "expected LookupError"
    except LookupError:
        pass
    assert db["product_videos"]["vid-1"]["product_id"] == "prod-1"  # unchanged


def test_reassign_raises_on_existing_duplicate_at_destination():
    """The real scenario this guards: the video already has its own row
    under the destination product (maybe from a separate, correct
    discovery run) -- reassigning would collide with product_videos'
    (product_id, youtube_video_id) unique constraint. Checked up front for
    a clear error, not left to a raw IntegrityError."""
    db = _fake_db_with_two_products_and_one_video()
    db["product_videos"]["vid-2"] = {
        "id": "vid-2", "product_id": "prod-2",
        "youtube_video_id": "abc123",  # same video, already under prod-2
        "status": "pending",
    }
    conn = FakeConnection(db)

    try:
        service.reassign_video_candidate(conn, "vid-1", "prod-2")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "vid-2" in str(e)
    assert db["product_videos"]["vid-1"]["product_id"] == "prod-1"  # unchanged


def test_delete_video_candidate_removes_row():
    db = _fake_db_with_two_products_and_one_video()
    conn = FakeConnection(db)

    result = service.delete_video_candidate(conn, "vid-1")

    assert result == {"video_id": "vid-1", "deleted": True}
    assert "vid-1" not in db["product_videos"]


def test_delete_missing_video_candidate_raises():
    db = _fake_db_with_two_products_and_one_video()
    conn = FakeConnection(db)
    try:
        service.delete_video_candidate(conn, "does-not-exist")
        assert False, "expected LookupError"
    except LookupError:
        pass


# --- submit_video_transcript: entry point for scripts/home_transcript_fetcher.py
# (or any other externally-fetched-transcript source) -- publishes straight
# to VideoTranscriptResultQueue rather than writing the DB itself, so these
# tests monkeypatch _publish_transcript_result_message the same way
# test_approve_video_candidate_forwards_youtube_video_id_to_publish
# monkeypatches _publish_video_summarize_message above.

def _fake_db_with_approved_video_candidate():
    db = _fake_db_with_pending_video_candidate()
    db["product_videos"]["vid-1"]["status"] = "approved"
    return db


def test_submit_video_transcript_publishes_and_returns_queued(monkeypatch):
    db = _fake_db_with_approved_video_candidate()
    conn = FakeConnection(db)
    captured = {}

    def fake_publish(product_video_id, transcript, transcript_note):
        captured["product_video_id"] = product_video_id
        captured["transcript"] = transcript
        captured["transcript_note"] = transcript_note

    monkeypatch.setattr(service, "_publish_transcript_result_message", fake_publish)

    result = service.submit_video_transcript(conn, "vid-1", "great ball, strong hook", None)

    assert result == {"video_id": "vid-1", "queued_for_summary": True}
    assert captured == {"product_video_id": "vid-1", "transcript": "great ball, strong hook", "transcript_note": None}


def test_submit_video_transcript_allows_empty_transcript_with_note(monkeypatch):
    """The home fetcher submits a real, checked 'no captions' outcome too,
    not just successful transcripts -- same non-error convention as the
    Lambda-based fetcher's transcript_note."""
    db = _fake_db_with_approved_video_candidate()
    conn = FakeConnection(db)
    captured = {}

    def fake_publish(product_video_id, transcript, transcript_note):
        captured["transcript"] = transcript
        captured["transcript_note"] = transcript_note

    monkeypatch.setattr(service, "_publish_transcript_result_message", fake_publish)

    result = service.submit_video_transcript(conn, "vid-1", "", "no_captions_available")

    assert result["queued_for_summary"] is True
    assert captured == {"transcript": "", "transcript_note": "no_captions_available"}


def test_submit_video_transcript_missing_row_raises():
    db = _fake_db_with_approved_video_candidate()
    conn = FakeConnection(db)
    try:
        service.submit_video_transcript(conn, "does-not-exist", "transcript text", None)
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_submit_video_transcript_rejects_non_approved_row():
    """Same gate video_summarizer's own _process_one applies -- a pending
    or rejected row can't have a transcript submitted for it."""
    db = _fake_db_with_pending_video_candidate()  # status defaults to 'pending'
    conn = FakeConnection(db)
    try:
        service.submit_video_transcript(conn, "vid-1", "transcript text", None)
        assert False, "expected ValueError"
    except ValueError:
        pass


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
