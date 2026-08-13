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
import json
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
        self._rows = []
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

        elif q.startswith("update products set video_reviews_summary"):
            # More specific than (and must be checked before) the generic
            # single-column "update products set" branch below -- store_rollup
            # sets three columns in one statement, not one.
            rollup_text, video_count, product_id = params
            row = self.db["products"].setdefault(product_id, {})
            row["video_reviews_summary"] = rollup_text
            row["video_reviews_summary_video_count"] = video_count
            row["video_reviews_summary_updated_at"] = "now"
            self._last_result = None

        elif q.startswith("update products set oil_rating") and "oil_motion_source = 'estimated'" in q:
            # backfill_estimated_plotter_positions' per-row write -- more
            # specific than (and must be checked before) set_plotter_
            # position's own branch below: literal 'estimated' in the SQL
            # itself (not a %s placeholder), 3 params, and the "and
            # oil_rating is null" not-clobber guard baked into the query
            # text rather than passed as a parameter.
            oil_rating, motion_rating, product_id = params
            row = self.db["products"].get(product_id)
            self._last_result = None
            if row is not None and row.get("oil_rating") is None:
                row["oil_rating"] = oil_rating
                row["motion_rating"] = motion_rating
                row["oil_motion_source"] = "estimated"
                self._last_result = (product_id,)
            self.description = [("id",)]

        elif q.startswith("update products set oil_rating"):
            # set_plotter_position -- source is now a real 4th param
            # (migration 012), not hardcoded.
            oil_rating, motion_rating, source, product_id = params
            row = self.db["products"].get(product_id)
            self._last_result = None
            if row is not None:
                row["oil_rating"] = oil_rating
                row["motion_rating"] = motion_rating
                row["oil_motion_source"] = source
                self._last_result = (product_id,)
            self.description = [("id",)]

        elif q.startswith("select p.id, c.core_type, p.coverstock_type, p.coverstock_material, p.has_particle"):
            # backfill_estimated_plotter_positions' missing-position scan.
            # The fake models this as a flat read off each product dict's
            # own core_type/coverstock_type/coverstock_material/has_particle
            # keys (test fixtures set these directly) rather than a real
            # cores join -- same simplification every other product-row
            # fake in this file already uses.
            missing = [
                (pid, row.get("core_type"), row.get("coverstock_type"),
                 row.get("coverstock_material"), row.get("has_particle"))
                for pid, row in self.db["products"].items()
                if row.get("oil_rating") is None
            ]
            self._rows = missing
            self.description = [("id",), ("core_type",), ("coverstock_type",), ("coverstock_material",), ("has_particle",)]

        elif q.startswith("select product_id, weight_lbs, differential from product_skus"):
            (product_ids,) = params
            rows = [
                (sku["product_id"], sku["weight_lbs"], sku["differential"])
                for sku in self.db.get("product_skus_plotter", [])
                if sku["product_id"] in product_ids and sku["differential"] is not None
            ]
            self._rows = rows
            self.description = [("product_id",), ("weight_lbs",), ("differential",)]

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

        elif q.startswith("update product_videos set status = 'pending'"):
            # restore_video_candidate's undo -- clears resolved_at/
            # resolved_by too (see its own docstring for why), so this
            # fake mirrors that by resetting both, not just status.
            (video_id,) = params
            row = self.db["product_videos"][video_id]
            row["status"] = "pending"
            row["resolved_by"] = None
            row["resolved_at"] = None
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

        elif q.startswith("select url, brand_id, source_platform from products"):
            (product_id,) = params
            row = self.db["products"].get(product_id)
            self._last_result = (row["url"], row["brand_id"], row["source_platform"]) if row else None
            self.description = [("url",), ("brand_id",), ("source_platform",)]

        elif q.startswith("select id from products"):
            (product_id,) = params
            row = self.db["products"].get(product_id)
            # "products" here doubles as the update-plan sink elsewhere in
            # this file (setdefault(product_id, {})), so a product "exists"
            # for this fake if its key is present at all, whatever its value.
            self._last_result = (product_id,) if product_id in self.db["products"] else None
            self.description = [("id",)]

        elif q.startswith("select id, transcript, summary, status from product_videos where product_id = %s and youtube_video_id = %s"):
            # reassign_video_candidate's conflict-lookup at the target product.
            target_product_id, youtube_video_id = params
            match = next(
                (v for v in self.db["product_videos"].values()
                 if v["product_id"] == target_product_id and v["youtube_video_id"] == youtube_video_id),
                None,
            )
            self._last_result = (
                (match["id"], match.get("transcript"), match.get("summary"), match["status"]) if match else None
            )
            self.description = [("id",), ("transcript",), ("summary",), ("status",)]

        elif q.startswith("select product_id, youtube_video_id, title, channel_title, published_at"):
            # reassign_video_candidate's full-row fetch of the origin row.
            (video_id,) = params
            row = self.db["product_videos"].get(video_id)
            self._last_result = (
                (row["product_id"], row["youtube_video_id"], row.get("title"), row.get("channel_title"),
                 row.get("published_at"), row.get("thumbnail_url"), row.get("match_query"),
                 row.get("match_confidence"), row.get("transcript"), row.get("transcript_note"),
                 row.get("summary"), row["status"], row.get("source"))
                if row else None
            )
            self.description = [
                ("product_id",), ("youtube_video_id",), ("title",), ("channel_title",), ("published_at",),
                ("thumbnail_url",), ("match_query",), ("match_confidence",), ("transcript",),
                ("transcript_note",), ("summary",), ("status",), ("source",),
            ]

        elif q.startswith("insert into product_videos") and "returning id" in q:
            # reassign_video_candidate's no-conflict path: a fresh row on
            # the target product carrying over the origin's content.
            (product_id, youtube_video_id, title, channel_title, published_at,
             thumbnail_url, match_query, match_confidence, transcript,
             transcript_note, summary, status, source) = params
            self.db.setdefault("_video_id_seq", 0)
            self.db["_video_id_seq"] += 1
            new_id = f"vid-new-{self.db['_video_id_seq']}"
            self.db["product_videos"][new_id] = {
                "id": new_id, "product_id": product_id, "youtube_video_id": youtube_video_id,
                "title": title, "channel_title": channel_title, "published_at": published_at,
                "thumbnail_url": thumbnail_url, "match_query": match_query, "match_confidence": match_confidence,
                "transcript": transcript, "transcript_note": transcript_note, "summary": summary,
                "status": status, "source": source,
            }
            self._last_result = (new_id,)
            self.description = [("id",)]

        elif q.startswith("update product_videos set") and "returning id" in q:
            # reassign_video_candidate's merge-into-existing-target-row
            # backfill -- dynamic column list, same pattern as
            # update_product_image's fake branch above. Last param is
            # always the target row's id.
            video_id = params[-1]
            set_clause_text = q.split("set ", 1)[1].split(" where", 1)[0]
            columns = [c.split(" =", 1)[0].strip() for c in set_clause_text.split(",")]
            row = self.db["product_videos"].get(video_id)
            if row is not None:
                for column, value in zip(columns, params[:-1]):
                    row[column] = value
                self._last_result = (video_id,)
            else:
                self._last_result = None
            self.description = [("id",)]

        elif q.startswith("select id from product_videos where id = %s"):
            (video_id,) = params
            row = self.db["product_videos"].get(video_id)
            self._last_result = (row["id"],) if row else None
            self.description = [("id",)]

        elif q.startswith("delete from product_videos where id = %s"):
            (video_id,) = params
            self.db["product_videos"].pop(video_id, None)
            self._last_result = None

        elif q.startswith("select summary from product_videos"):
            (product_id,) = params
            matches = [
                v for v in self.db["product_videos"].values()
                if v.get("product_id") == product_id and v.get("status") == "approved" and v.get("summary") is not None
            ]
            matches.sort(key=lambda v: (v.get("created_at", ""), v["id"]))
            self._rows = [(v["summary"],) for v in matches]
            self.description = [("summary",)]

        elif q.startswith("select p.id, p.name, b.name as brand_name, p.description from products p"):
            (product_id,) = params
            row = self.db["products"].get(product_id)
            self._last_result = (product_id, row["name"], row["brand_name"], row.get("description")) if row else None
            self.description = [("id",), ("name",), ("brand_name",), ("description",)]

        elif q.startswith("select id, name from brands order by name"):
            rows = sorted(self.db["brands"].values(), key=lambda b: b["name"])
            self._rows = [(b["id"], b["name"]) for b in rows]
            self.description = [("id",), ("name",)]

        elif q.startswith("select product_id, min(created_at) from product_videos group by product_id"):
            by_product = {}
            for v in self.db["product_videos"].values():
                pid = v["product_id"]
                created_at = v["created_at"]
                if pid not in by_product or created_at < by_product[pid]:
                    by_product[pid] = created_at
            self._rows = list(by_product.items())
            self.description = [("product_id",), ("min",)]

        elif q.startswith("update products set last_video_discovery_at = %s where id = %s and last_video_discovery_at is null"):
            earliest, product_id = params
            row = self.db["products"].get(product_id)
            if row is not None and row.get("last_video_discovery_at") is None:
                row["last_video_discovery_at"] = earliest
                self._last_result = (product_id,)
            else:
                self._last_result = None
            self.description = [("id",)]

        elif q.startswith("update product_images set is_thumbnail = false where product_id"):
            product_id, keep_image_id = params
            for row in self.db.get("product_images", {}).values():
                if row["product_id"] == product_id and row["id"] != keep_image_id:
                    row["is_thumbnail"] = False
            self._last_result = None

        elif q.startswith("update product_images set display_order"):
            display_order, image_id, product_id = params
            row = self.db.get("product_images", {}).get(image_id)
            if row is not None and row["product_id"] == product_id:
                row["display_order"] = display_order
            self._last_result = None

        elif q.startswith("update product_images set") and "returning id" in q:
            # Dynamic column list (see service.update_product_image) --
            # the last two params are always (image_id, product_id); every
            # param before that maps positionally onto the SET clauses
            # this query text was built with, in the same order.
            image_id, product_id = params[-2], params[-1]
            set_clause_text = q.split("set ", 1)[1].split(" where", 1)[0]
            columns = [c.split(" =", 1)[0].strip() for c in set_clause_text.split(",")]
            row = self.db.get("product_images", {}).get(image_id)
            if row is not None and row["product_id"] == product_id:
                for column, value in zip(columns, params[:-2]):
                    row[column] = value
                self._last_result = (image_id,)
            else:
                self._last_result = None
            self.description = [("id",)]

        elif q.startswith("select id from product_images where id = %s and product_id = %s"):
            image_id, product_id = params
            row = self.db.get("product_images", {}).get(image_id)
            found = row is not None and row["product_id"] == product_id
            self._last_result = (image_id,) if found else None
            self.description = [("id",)]

        elif q.startswith("update products p set status = du.status_path"):
            # backfill_netsuite_status: no params, pure join-and-correct.
            # Mirrors the real UPDATE ... FROM's WHERE clause exactly:
            # matched by url, netsuite-only, discovered status_path must be
            # non-null and actually differ from what's currently stored.
            discovered = self.db.get("discovered_urls", {})
            corrected_ids = []
            for product_id, row in self.db["products"].items():
                if row.get("source_platform") != "netsuite":
                    continue
                status_path = discovered.get(row.get("url"))
                if status_path is None:
                    continue
                if status_path != row.get("status"):
                    row["status"] = status_path
                    corrected_ids.append(product_id)
            self._rows = [(pid,) for pid in corrected_ids]
            self.description = [("id",)]

        else:
            raise NotImplementedError(f"FakeCursor doesn't support: {q}")

    def fetchone(self):
        return self._last_result

    def fetchall(self):
        return self._rows


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

    def fetchone(self):
        # Default to "not found" -- get_core (and any other single-row
        # get_* this connection is reused for) short-circuits to None on
        # this, which is itself a real, useful thing to confirm (the
        # second query never runs for a missing id) rather than an
        # unsupported-fake gap.
        return None


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


def test_list_video_candidates_selects_stats_columns():
    """Migration 013 -- Al: 'for the videos can we get pull down more data
    points from the videos, date it was added current view counts and any
    other data that make sense.' description is deliberately NOT selected
    here (kept out of the list view the same way transcript/summary
    already are -- see get_video_candidate for the full-row detail
    view)."""
    conn = _QueryCapturingConnection()
    service.list_video_candidates(conn, status="pending", limit=200, offset=0)

    query = conn.cursor().queries[0]
    for col in ("pv.view_count", "pv.like_count", "pv.comment_count",
                "pv.duration_seconds", "pv.stats_fetched_at"):
        assert col in query
    assert "pv.description" not in query


# --- list_video_candidates status=None (status="all" at the app.py layer)
# -- added for the product detail view's Videos section, Al: "can we add
# the video candidates for products into the product details view."

def test_list_video_candidates_status_none_omits_status_filter():
    conn = _QueryCapturingConnection()
    service.list_video_candidates(conn, status=None, product_id="prod-1", limit=200, offset=0)

    query = conn.cursor().queries[0]
    assert "pv.status = %s" not in query  # pv.status is still selected, just not filtered on
    assert "pv.product_id = %s" in query
    assert "where" in query  # product_id condition still present, just not status


def test_list_video_candidates_status_pending_still_filters_by_default():
    """Confirms the default/existing behavior (Video Candidates tab, every
    other current caller) is unchanged by the status=None addition."""
    conn = _QueryCapturingConnection()
    service.list_video_candidates(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "pv.status = %s" in query


def test_list_video_candidates_status_none_without_product_id_omits_where_entirely():
    conn = _QueryCapturingConnection()
    service.list_video_candidates(conn, status=None, limit=200, offset=0)

    query = conn.cursor().queries[0]
    assert "where" not in query


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


# --- restore_video_candidate: undo for a mistaken approve/reject. Al: "it
# appears if i accidentally reject a video i can not undo that action".

def test_restore_video_candidate_from_rejected_marks_pending_and_clears_resolution():
    db = _fake_db_with_pending_video_candidate()
    db["product_videos"]["vid-1"]["status"] = "rejected"
    db["product_videos"]["vid-1"]["resolved_by"] = "al@bringyourbest.co"
    db["product_videos"]["vid-1"]["resolved_at"] = "2026-08-01T00:00:00Z"
    conn = FakeConnection(db)

    result = service.restore_video_candidate(conn, "vid-1")

    assert result == {"video_id": "vid-1", "status": "pending"}
    row = db["product_videos"]["vid-1"]
    assert row["status"] == "pending"
    assert row["resolved_by"] is None
    assert row["resolved_at"] is None
    assert conn.committed is True


def test_restore_video_candidate_from_approved_marks_pending():
    db = _fake_db_with_pending_video_candidate()
    db["product_videos"]["vid-1"]["status"] = "approved"
    db["product_videos"]["vid-1"]["resolved_by"] = "al@bringyourbest.co"
    conn = FakeConnection(db)

    result = service.restore_video_candidate(conn, "vid-1")

    assert result["status"] == "pending"
    assert db["product_videos"]["vid-1"]["status"] == "pending"
    assert db["product_videos"]["vid-1"]["resolved_by"] is None


def test_restore_already_pending_video_candidate_raises():
    # Nothing to undo -- restoring a still-pending row is a hard error, not
    # a silent no-op (see restore_video_candidate's docstring: usually
    # means the caller's UI state is stale).
    db = _fake_db_with_pending_video_candidate()
    conn = FakeConnection(db)
    try:
        service.restore_video_candidate(conn, "vid-1")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_restore_missing_video_candidate_raises():
    db = _fake_db_with_pending_video_candidate()
    conn = FakeConnection(db)
    try:
        service.restore_video_candidate(conn, "does-not-exist")
        assert False, "expected LookupError"
    except LookupError:
        pass


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


def test_reassign_video_candidate_creates_new_row_and_tombstones_origin():
    """No conflict at the destination: a fresh row is created on prod-2
    carrying over the video's content/status, and the origin row (vid-1,
    still under prod-1) becomes a rejected tombstone rather than being
    moved or deleted -- that tombstone is what stops video_discovery's
    ON CONFLICT DO NOTHING from reinserting this exact video under prod-1
    on the next rescan (the real bug Al hit: reassigned videos were coming
    back)."""
    db = _fake_db_with_two_products_and_one_video()
    db["product_videos"]["vid-1"]["status"] = "approved"
    db["product_videos"]["vid-1"]["summary"] = "Great ball for medium oil."
    conn = FakeConnection(db)

    result = service.reassign_video_candidate(conn, "vid-1", "prod-2", resolved_by="al@bringyourbest.co")

    assert result["product_id"] == "prod-2"
    assert result["origin_video_id"] == "vid-1"
    assert result["merged_with_existing"] is False
    new_id = result["video_id"]
    assert new_id != "vid-1"

    # Origin: tombstoned, not moved -- still under prod-1, now rejected.
    origin = db["product_videos"]["vid-1"]
    assert origin["product_id"] == "prod-1"
    assert origin["status"] == "rejected"
    assert origin["resolved_by"] == "al@bringyourbest.co"

    # Target: new row, content carried over.
    target = db["product_videos"][new_id]
    assert target["product_id"] == "prod-2"
    assert target["youtube_video_id"] == "abc123"
    assert target["status"] == "approved"
    assert target["summary"] == "Great ball for medium oil."
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
    assert db["product_videos"]["vid-1"]["status"] == "pending"  # not tombstoned


def test_reassign_to_same_product_raises():
    db = _fake_db_with_two_products_and_one_video()
    conn = FakeConnection(db)
    try:
        service.reassign_video_candidate(conn, "vid-1", "prod-1")
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert db["product_videos"]["vid-1"]["status"] == "pending"  # not tombstoned


def test_reassign_merges_into_existing_row_at_destination_and_tombstones_origin():
    """The real scenario this replaces a hard error with: the video
    already has its own row under the destination product (maybe from a
    separate, correct discovery run there). Used to force the admin to
    manually delete one of the two duplicates before retrying -- which
    had the same resurfacing problem as this whole fix addresses, since
    deleting the origin row removes its blocking tombstone. Now it just
    merges: origin's transcript/summary backfill onto the target's row
    (which has neither yet here) without touching the target's own
    status, and the origin still gets tombstoned."""
    db = _fake_db_with_two_products_and_one_video()
    db["product_videos"]["vid-1"]["transcript"] = "full transcript text"
    db["product_videos"]["vid-1"]["summary"] = "Great ball for medium oil."
    db["product_videos"]["vid-1"]["transcript_note"] = None
    db["product_videos"]["vid-2"] = {
        "id": "vid-2", "product_id": "prod-2",
        "youtube_video_id": "abc123",  # same video, already under prod-2
        "status": "pending", "transcript": None, "summary": None,
    }
    conn = FakeConnection(db)

    result = service.reassign_video_candidate(conn, "vid-1", "prod-2")

    assert result["video_id"] == "vid-2"
    assert result["merged_with_existing"] is True

    target = db["product_videos"]["vid-2"]
    assert target["transcript"] == "full transcript text"
    assert target["summary"] == "Great ball for medium oil."
    assert target["status"] == "pending"  # untouched by the merge

    origin = db["product_videos"]["vid-1"]
    assert origin["product_id"] == "prod-1"  # tombstoned in place, not moved
    assert origin["status"] == "rejected"


def test_reassign_merge_does_not_overwrite_existing_target_content():
    """The target's own transcript/summary (and status) are never
    clobbered by a merge -- only null fields get backfilled. An admin who
    already reviewed the target's copy shouldn't have that judgment
    silently overwritten."""
    db = _fake_db_with_two_products_and_one_video()
    db["product_videos"]["vid-1"]["transcript"] = "origin transcript"
    db["product_videos"]["vid-1"]["summary"] = "origin summary"
    db["product_videos"]["vid-2"] = {
        "id": "vid-2", "product_id": "prod-2",
        "youtube_video_id": "abc123",
        "status": "approved", "transcript": "target's own transcript", "summary": "target's own summary",
    }
    conn = FakeConnection(db)

    service.reassign_video_candidate(conn, "vid-1", "prod-2")

    target = db["product_videos"]["vid-2"]
    assert target["transcript"] == "target's own transcript"
    assert target["summary"] == "target's own summary"
    assert target["status"] == "approved"


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


# --- "Summary of summaries" on-demand refresh (POST /products/{id}/refresh-
# video-summary): fetch_approved_video_summaries / build_rollup_prompt /
# generate_video_reviews_rollup / store_rollup / _fetch_product_for_rollup /
# refresh_video_reviews_rollup. Deliberate duplicates of
# video_summarizer/app.py's functions of the same name (see service.py's
# module comment above these functions) -- these tests mirror
# test_video_summarizer.py's equivalents (_FakeBedrockClient shape included)
# so both copies stay verifiably in sync.

def _fake_db_with_product_and_approved_videos():
    return {
        "products": {
            "prod-1": {"name": "Absolute", "brand_name": "Storm"},
        },
        "product_videos": {
            "vid-1": {
                "id": "vid-1", "product_id": "prod-1", "status": "approved",
                "summary": "In this video, the reviewer notes a strong, early hook.",
                "created_at": "2026-01-01",
            },
            "vid-2": {
                "id": "vid-2", "product_id": "prod-1", "status": "approved",
                "summary": "Smooth and predictable on medium oil.",
                "created_at": "2026-01-02",
            },
            "vid-3": {
                "id": "vid-3", "product_id": "prod-1", "status": "pending",
                "summary": "Should be excluded -- not approved.",
                "created_at": "2026-01-03",
            },
            "vid-4": {
                "id": "vid-4", "product_id": "prod-1", "status": "approved",
                "summary": None,  # approved but not yet summarized -- excluded
                "created_at": "2026-01-04",
            },
            "vid-5": {
                "id": "vid-5", "product_id": "prod-2", "status": "approved",
                "summary": "Belongs to a different product -- excluded.",
                "created_at": "2026-01-01",
            },
        },
    }


def test_fetch_approved_video_summaries_filters_status_and_nonnull():
    db = _fake_db_with_product_and_approved_videos()
    conn = FakeConnection(db)

    result = service.fetch_approved_video_summaries(conn, "prod-1")

    assert result == [
        "In this video, the reviewer notes a strong, early hook.",
        "Smooth and predictable on medium oil.",
    ]


def test_fetch_approved_video_summaries_empty_for_product_with_none():
    db = _fake_db_with_product_and_approved_videos()
    conn = FakeConnection(db)

    assert service.fetch_approved_video_summaries(conn, "prod-does-not-exist") == []


def test_build_rollup_prompt_single_summary_rewrites_standalone():
    prompt = service.build_rollup_prompt("Absolute", "Storm", ["In this video, the reviewer notes strong hook."])
    assert "In this video, the reviewer notes strong hook." in prompt
    assert "Rewrite it as a standalone" in prompt
    assert "remove any references" in prompt


def test_build_rollup_prompt_multiple_summaries_synthesizes():
    summaries = ["Strong hook, clears the front.", "Smooth and predictable on medium oil."]
    prompt = service.build_rollup_prompt("Absolute", "Storm", summaries)
    assert "2 independent review summaries" in prompt
    assert "1. Strong hook, clears the front." in prompt
    assert "2. Smooth and predictable on medium oil." in prompt
    assert "Synthesize them" in prompt
    assert "notable disagreements" in prompt


class _FakeBedrockBody:
    def __init__(self, payload: dict):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data


class _FakeBedrockClient:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.calls = []

    def invoke_model(self, modelId, contentType, accept, body):
        self.calls.append({"modelId": modelId, "contentType": contentType, "accept": accept, "body": body})
        return {"body": _FakeBedrockBody({"content": [{"text": self.response_text}]})}


# --- build_rollup_prompt with a manufacturer description -- kept in sync
# with test_video_summarizer.py's equivalent tests. See service.py's
# build_rollup_prompt docstring for the design reasoning.

def test_build_rollup_prompt_includes_description_when_present():
    prompt = service.build_rollup_prompt(
        "Absolute", "Storm", ["Strong hook, clears the front."],
        description="Sentinel Core: an asymmetric core built for early transition.",
    )
    assert "manufacturer's own description" in prompt
    assert "Sentinel Core: an asymmetric core built for early transition." in prompt
    assert "must still reflect what reviewers actually said" in prompt


def test_build_rollup_prompt_omits_description_block_when_absent():
    prompt = service.build_rollup_prompt("Absolute", "Storm", ["Strong hook, clears the front."])
    assert "manufacturer's own description" not in prompt


def test_generate_video_reviews_rollup_returns_model_text():
    client = _FakeBedrockClient("Reviewers agree this ball hooks hard on medium oil.")

    rollup = service.generate_video_reviews_rollup(
        client, "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "Absolute", "Storm", ["Strong hook.", "Hooks a lot."],
    )

    assert rollup == "Reviewers agree this ball hooks hard on medium oil."
    assert len(client.calls) == 1
    assert client.calls[0]["modelId"] == "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_store_rollup_writes_all_three_columns_and_commits():
    db = _fake_db_with_product_and_approved_videos()
    conn = FakeConnection(db)

    service.store_rollup(conn, "prod-1", "Reviewers agree this hooks hard.", 2)

    row = db["products"]["prod-1"]
    assert row["video_reviews_summary"] == "Reviewers agree this hooks hard."
    assert row["video_reviews_summary_video_count"] == 2
    assert row["video_reviews_summary_updated_at"] == "now"
    assert conn.committed is True


def test_fetch_product_for_rollup_returns_name_and_brand():
    db = _fake_db_with_product_and_approved_videos()
    conn = FakeConnection(db)

    result = service._fetch_product_for_rollup(conn, "prod-1")

    assert result == {"id": "prod-1", "name": "Absolute", "brand_name": "Storm", "description": None}


def test_fetch_product_for_rollup_missing_returns_none():
    db = _fake_db_with_product_and_approved_videos()
    conn = FakeConnection(db)

    assert service._fetch_product_for_rollup(conn, "does-not-exist") is None


def test_refresh_video_reviews_rollup_missing_product_raises():
    db = _fake_db_with_product_and_approved_videos()
    conn = FakeConnection(db)
    try:
        service.refresh_video_reviews_rollup(conn, "does-not-exist")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_refresh_video_reviews_rollup_no_summaries_is_not_an_error():
    """A product that exists but has no approved+summarized videos yet is
    a normal, expected outcome (rollup_regenerated: False) -- not raised as
    an error, same convention as video_summarizer's own version."""
    db = {
        "products": {"prod-empty": {"name": "Nightroad", "brand_name": "Storm"}},
        "product_videos": {},
    }
    conn = FakeConnection(db)

    result = service.refresh_video_reviews_rollup(conn, "prod-empty")

    assert result == {"product_id": "prod-empty", "rollup_regenerated": False, "reason": "no_summaries"}


def test_refresh_video_reviews_rollup_success_builds_bedrock_client_and_stores():
    """refresh_video_reviews_rollup builds its own boto3 bedrock-runtime
    client internally (see its docstring) rather than taking one as a
    parameter, so this fakes boto3.client itself via sys.modules -- same
    approach test_approve_video_candidate_marks_approved_and_does_not_touch_
    queue uses above, just returning a working fake instead of an exploding
    one."""
    db = _fake_db_with_product_and_approved_videos()
    conn = FakeConnection(db)
    fake_client = _FakeBedrockClient("This ball hooks hard and clears the front of the lane.")

    class _FakeBoto3:
        def client(self, name):
            assert name == "bedrock-runtime"
            return fake_client

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    try:
        result = service.refresh_video_reviews_rollup(conn, "prod-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]

    assert result == {"product_id": "prod-1", "rollup_regenerated": True, "video_count": 2}
    assert db["products"]["prod-1"]["video_reviews_summary"] == "This ball hooks hard and clears the front of the lane."
    assert db["products"]["prod-1"]["video_reviews_summary_video_count"] == 2
    assert len(fake_client.calls) == 1


def test_refresh_video_reviews_rollup_threads_description_from_products_row():
    """_fetch_product_for_rollup's query now selects p.description too --
    confirms it actually reaches the Bedrock call, not just that the
    plumbing compiles."""
    db = _fake_db_with_product_and_approved_videos()
    db["products"]["prod-1"]["description"] = (
        "Sentinel Core: an asymmetric core built for early transition."
    )
    conn = FakeConnection(db)
    fake_client = _FakeBedrockClient("This ball hooks hard and clears the front of the lane.")

    class _FakeBoto3:
        def client(self, name):
            return fake_client

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    try:
        service.refresh_video_reviews_rollup(conn, "prod-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]

    sent_body = json.loads(fake_client.calls[0]["body"])
    assert "Sentinel Core: an asymmetric core built for early transition." in sent_body["messages"][0]["content"]


# --- backfill_last_video_discovery_at: one-off migration-005 correction ---

def test_backfill_last_video_discovery_at_sets_null_column_from_earliest_video():
    db = {
        "products": {
            "prod-1": {"name": "Absolute", "last_video_discovery_at": None},
        },
        "product_videos": {
            "vid-1": {"id": "vid-1", "product_id": "prod-1", "created_at": "2026-01-02"},
            "vid-2": {"id": "vid-2", "product_id": "prod-1", "created_at": "2026-01-01"},
        },
    }
    conn = FakeConnection(db)

    result = service.backfill_last_video_discovery_at(conn)

    assert result == {"products_with_video_history": 1, "products_updated": 1}
    # earliest, not latest -- 01-01, not 01-02
    assert db["products"]["prod-1"]["last_video_discovery_at"] == "2026-01-01"
    assert conn.committed is True


def test_backfill_last_video_discovery_at_skips_products_already_set():
    """A product that's already been searched under the new rotation logic
    has a real (non-NULL) last_video_discovery_at -- this backfill must
    never clobber it with an earlier product_videos timestamp, even if one
    exists from before the column was being maintained."""
    db = {
        "products": {
            "prod-1": {"name": "Absolute", "last_video_discovery_at": "2026-03-01"},
        },
        "product_videos": {
            "vid-1": {"id": "vid-1", "product_id": "prod-1", "created_at": "2026-01-01"},
        },
    }
    conn = FakeConnection(db)

    result = service.backfill_last_video_discovery_at(conn)

    assert result == {"products_with_video_history": 1, "products_updated": 0}
    assert db["products"]["prod-1"]["last_video_discovery_at"] == "2026-03-01"


def test_backfill_last_video_discovery_at_leaves_never_searched_products_null():
    """A product with zero product_videos rows never appears in the
    group-by result at all -- its column is left NULL, exactly as
    intended, so it still sorts first under video_discovery's rotation."""
    db = {
        "products": {
            "prod-never-searched": {"name": "Nightroad", "last_video_discovery_at": None},
        },
        "product_videos": {},
    }
    conn = FakeConnection(db)

    result = service.backfill_last_video_discovery_at(conn)

    assert result == {"products_with_video_history": 0, "products_updated": 0}
    assert db["products"]["prod-never-searched"]["last_video_discovery_at"] is None


def test_backfill_last_video_discovery_at_handles_multiple_products_independently():
    db = {
        "products": {
            "prod-1": {"name": "Absolute", "last_video_discovery_at": None},
            "prod-2": {"name": "Phaze II", "last_video_discovery_at": "2026-02-15"},
        },
        "product_videos": {
            "vid-1": {"id": "vid-1", "product_id": "prod-1", "created_at": "2026-01-05"},
            "vid-2": {"id": "vid-2", "product_id": "prod-2", "created_at": "2026-01-01"},
        },
    }
    conn = FakeConnection(db)

    result = service.backfill_last_video_discovery_at(conn)

    assert result == {"products_with_video_history": 2, "products_updated": 1}
    assert db["products"]["prod-1"]["last_video_discovery_at"] == "2026-01-05"
    assert db["products"]["prod-2"]["last_video_discovery_at"] == "2026-02-15"  # untouched


# --- backfill_netsuite_status: one-off MOTIV status-clobber correction --
# see service.backfill_netsuite_status's docstring and netsuite_product_
# scraper's module docstring "REAL INCIDENT" section for the full story.

def test_backfill_netsuite_status_corrects_mismatched_netsuite_products():
    db = {
        "products": {
            "prod-1": {"url": "https://www.motivbowling.com/n_1", "status": "current", "source_platform": "netsuite"},
        },
        "discovered_urls": {
            "https://www.motivbowling.com/n_1": "retired",
        },
    }
    conn = FakeConnection(db)

    result = service.backfill_netsuite_status(conn)

    assert result == {"products_corrected": 1}
    assert db["products"]["prod-1"]["status"] == "retired"
    assert conn.committed is True


def test_backfill_netsuite_status_leaves_already_correct_products_alone():
    db = {
        "products": {
            "prod-1": {"url": "https://www.motivbowling.com/n_1", "status": "retired", "source_platform": "netsuite"},
        },
        "discovered_urls": {
            "https://www.motivbowling.com/n_1": "retired",
        },
    }
    conn = FakeConnection(db)

    result = service.backfill_netsuite_status(conn)

    assert result == {"products_corrected": 0}
    assert db["products"]["prod-1"]["status"] == "retired"


def test_backfill_netsuite_status_ignores_non_netsuite_products():
    """A Shopify/Brunswick/etc. product happening to share a url with a
    discovered_urls row (shouldn't really occur across platforms, but this
    confirms the source_platform = 'netsuite' scope in the real query is
    actually load-bearing) must never get touched by this correction."""
    db = {
        "products": {
            "prod-1": {"url": "https://example.com/ball", "status": "current", "source_platform": "shopify"},
        },
        "discovered_urls": {
            "https://example.com/ball": "retired",
        },
    }
    conn = FakeConnection(db)

    result = service.backfill_netsuite_status(conn)

    assert result == {"products_corrected": 0}
    assert db["products"]["prod-1"]["status"] == "current"


def test_backfill_netsuite_status_leaves_products_with_no_discovered_url_alone():
    """No ground truth to correct against (e.g. a manually-inserted
    product, same case get_product's discovered_url exposure documents) --
    left alone rather than guessed at."""
    db = {
        "products": {
            "prod-1": {"url": "https://www.motivbowling.com/n_999", "status": "current", "source_platform": "netsuite"},
        },
        "discovered_urls": {},
    }
    conn = FakeConnection(db)

    result = service.backfill_netsuite_status(conn)

    assert result == {"products_corrected": 0}
    assert db["products"]["prod-1"]["status"] == "current"


def test_backfill_netsuite_status_handles_multiple_products_independently():
    db = {
        "products": {
            "prod-1": {"url": "https://www.motivbowling.com/n_1", "status": "current", "source_platform": "netsuite"},
            "prod-2": {"url": "https://www.motivbowling.com/n_2", "status": "current", "source_platform": "netsuite"},
            "prod-3": {"url": "https://www.motivbowling.com/n_3", "status": "retired", "source_platform": "netsuite"},
        },
        "discovered_urls": {
            "https://www.motivbowling.com/n_1": "retired",
            "https://www.motivbowling.com/n_2": "current",
            "https://www.motivbowling.com/n_3": "retired",
        },
    }
    conn = FakeConnection(db)

    result = service.backfill_netsuite_status(conn)

    assert result == {"products_corrected": 1}  # only prod-1 actually disagreed
    assert db["products"]["prod-1"]["status"] == "retired"
    assert db["products"]["prod-2"]["status"] == "current"  # already agreed
    assert db["products"]["prod-3"]["status"] == "retired"  # already agreed


# --- resolve_scrape_queue_env_var: pure lookup, no DB/env access ---

def test_resolve_scrape_queue_env_var_craft_cms():
    assert service.resolve_scrape_queue_env_var("craft_cms") == "PRODUCT_SCRAPE_QUEUE_URL"


def test_resolve_scrape_queue_env_var_woocommerce():
    assert service.resolve_scrape_queue_env_var("woocommerce") == "WOOCOMMERCE_PRODUCT_SCRAPE_QUEUE_URL"


def test_resolve_scrape_queue_env_var_netsuite():
    assert service.resolve_scrape_queue_env_var("netsuite") == "NETSUITE_PRODUCT_SCRAPE_QUEUE_URL"


def test_resolve_scrape_queue_env_var_commercebuild():
    assert service.resolve_scrape_queue_env_var("commercebuild") == "COMMERCEBUILD_PRODUCT_SCRAPE_QUEUE_URL"


def test_resolve_scrape_queue_env_var_shopify():
    """Onboarded this session (Hammer) -- shopify now resolves the same
    way every other platform does, unlike before when it deliberately
    returned None (see this file's git history / DEPLOY_RUNBOOK.md)."""
    assert service.resolve_scrape_queue_env_var("shopify") == "SHOPIFY_PRODUCT_SCRAPE_QUEUE_URL"


def test_resolve_scrape_queue_env_var_unsupported_platform_returns_none():
    """'other' (the catch-all source_platform enum value) and any
    unrecognized string both have no scraper deployed -- must return
    None, not raise, so queue_rescrape can build a graceful 'not
    supported' response instead of a 500."""
    assert service.resolve_scrape_queue_env_var("other") is None
    assert service.resolve_scrape_queue_env_var("nonsense") is None


# --- queue_rescrape: fake DB + fake boto3 SQS client, same sys.modules
# fake-injection approach as test_refresh_video_reviews_rollup_success_
# builds_bedrock_client_and_stores above (this function also does an
# inline deferred `import boto3`, no separate wrapper to monkeypatch).

def _fake_db_with_product(source_platform="craft_cms", url="https://brunswickbowling.com/products/balls/current/fury"):
    return {"products": {"prod-1": {"url": url, "brand_id": "brand-abc", "source_platform": source_platform}}}


class _FakeSqsClient:
    def __init__(self):
        self.sent = []

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append({"QueueUrl": QueueUrl, "MessageBody": MessageBody})


def test_queue_rescrape_publishes_to_craft_cms_queue():
    db = _fake_db_with_product(source_platform="craft_cms")
    conn = FakeConnection(db)
    fake_sqs = _FakeSqsClient()

    class _FakeBoto3:
        def client(self, name):
            assert name == "sqs"
            return fake_sqs

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["PRODUCT_SCRAPE_QUEUE_URL"] = "https://sqs.example/product-scrape"
    try:
        result = service.queue_rescrape(conn, "prod-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["PRODUCT_SCRAPE_QUEUE_URL"]

    assert result == {
        "queued": True, "product_id": "prod-1",
        "url": "https://brunswickbowling.com/products/balls/current/fury",
        "queue_env_var": "PRODUCT_SCRAPE_QUEUE_URL",
    }
    assert len(fake_sqs.sent) == 1
    assert fake_sqs.sent[0]["QueueUrl"] == "https://sqs.example/product-scrape"
    body = json.loads(fake_sqs.sent[0]["MessageBody"])
    assert body == {"url": "https://brunswickbowling.com/products/balls/current/fury", "brand_id": "brand-abc"}


def test_queue_rescrape_missing_product_raises():
    db = _fake_db_with_product()
    conn = FakeConnection(db)
    try:
        service.queue_rescrape(conn, "does-not-exist")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_queue_rescrape_unsupported_platform_returns_not_queued_without_touching_sqs():
    """A product on a platform with no scraper deployed yet ('other' --
    shopify itself is now supported, see test_resolve_scrape_queue_env_var_
    shopify above) shouldn't even try to import boto3/publish anything --
    confirms via a boto3 stand-in that would raise if .client() were ever
    called."""
    db = _fake_db_with_product(source_platform="other")
    conn = FakeConnection(db)

    class _ExplodingBoto3:
        def client(self, name):
            raise AssertionError("should never be called for an unsupported platform")

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _ExplodingBoto3()
    try:
        result = service.queue_rescrape(conn, "prod-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]

    assert result == {"queued": False, "reason": "no scraper deployed for source_platform='other' yet"}


def test_queue_rescrape_publishes_to_shopify_queue():
    """Confirms the new mapping actually flows through queue_rescrape end
    to end, not just resolve_scrape_queue_env_var in isolation."""
    db = _fake_db_with_product(source_platform="shopify", url="https://hammerbowling.com/products/spawn")
    conn = FakeConnection(db)
    fake_sqs = _FakeSqsClient()

    class _FakeBoto3:
        def client(self, name):
            assert name == "sqs"
            return fake_sqs

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["SHOPIFY_PRODUCT_SCRAPE_QUEUE_URL"] = "https://sqs.example/shopify-scrape"
    try:
        result = service.queue_rescrape(conn, "prod-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["SHOPIFY_PRODUCT_SCRAPE_QUEUE_URL"]

    assert result == {
        "queued": True, "product_id": "prod-1",
        "url": "https://hammerbowling.com/products/spawn",
        "queue_env_var": "SHOPIFY_PRODUCT_SCRAPE_QUEUE_URL",
    }
    assert fake_sqs.sent[0]["QueueUrl"] == "https://sqs.example/shopify-scrape"


def test_queue_rescrape_missing_queue_env_var_returns_not_queued():
    """The platform IS supported (commercebuild), but this deployment's
    stack just doesn't have COMMERCEBUILD_PRODUCT_SCRAPE_QUEUE_URL set --
    a real misconfiguration, but still a graceful response rather than a
    KeyError, since a batch caller shouldn't hard-stop on one product."""
    db = _fake_db_with_product(source_platform="commercebuild")
    conn = FakeConnection(db)
    os.environ.pop("COMMERCEBUILD_PRODUCT_SCRAPE_QUEUE_URL", None)  # confirm truly unset

    result = service.queue_rescrape(conn, "prod-1")

    assert result == {"queued": False, "reason": "COMMERCEBUILD_PRODUCT_SCRAPE_QUEUE_URL is not configured on this deployment"}


# --- queue_video_discovery (POST /products/{id}/discover-videos) -- the
# "search for candidates again" button Al asked for on the product detail
# view. Same fake-boto3-via-sys.modules approach as queue_rescrape above,
# just invoking a Lambda directly (InvocationType='Event') instead of
# publishing to an SQS queue -- VideoDiscoveryFunction has no queue in
# front of it (manual/direct invoke only).

class _FakeLambdaClient:
    def __init__(self):
        self.invocations = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.invocations.append({"FunctionName": FunctionName, "InvocationType": InvocationType, "Payload": Payload})


def test_queue_video_discovery_invokes_function_with_product_ids_scope():
    db = _fake_db_with_product()
    conn = FakeConnection(db)
    fake_lambda = _FakeLambdaClient()

    class _FakeBoto3:
        def client(self, name):
            assert name == "lambda"
            return fake_lambda

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["VIDEO_DISCOVERY_FUNCTION_NAME"] = "bowling-scraper-video-discovery"
    try:
        result = service.queue_video_discovery(conn, "prod-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["VIDEO_DISCOVERY_FUNCTION_NAME"]

    assert result == {"queued": True, "product_id": "prod-1"}
    assert len(fake_lambda.invocations) == 1
    call = fake_lambda.invocations[0]
    assert call["FunctionName"] == "bowling-scraper-video-discovery"
    assert call["InvocationType"] == "Event"  # async -- see docstring, VideoDiscoveryFunction can take a while
    assert json.loads(call["Payload"]) == {"product_ids": ["prod-1"]}


def test_queue_video_discovery_missing_product_raises():
    db = _fake_db_with_product()
    conn = FakeConnection(db)
    try:
        service.queue_video_discovery(conn, "does-not-exist")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_queue_video_discovery_missing_function_name_returns_not_queued():
    """Deployment hasn't set VIDEO_DISCOVERY_FUNCTION_NAME -- graceful
    response, not a KeyError, same soft-fail convention as queue_rescrape's
    missing-queue-env-var case. Confirms boto3 is never even touched."""
    db = _fake_db_with_product()
    conn = FakeConnection(db)
    os.environ.pop("VIDEO_DISCOVERY_FUNCTION_NAME", None)  # confirm truly unset

    class _ExplodingBoto3:
        def client(self, name):
            raise AssertionError("should never be called when the function name isn't configured")

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _ExplodingBoto3()
    try:
        result = service.queue_video_discovery(conn, "prod-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]

    assert result == {"queued": False, "reason": "VIDEO_DISCOVERY_FUNCTION_NAME is not configured on this deployment"}


# --- queue_video_stats_refresh: catalog-wide "re-pull view counts"
# trigger (POST /admin/refresh-video-stats) -- same invoke-VideoDiscovery
# Function-directly shape as queue_video_discovery above, but with no
# product_id to validate (see its own docstring for why).

def test_queue_video_stats_refresh_invokes_function_with_limit():
    fake_lambda = _FakeLambdaClient()

    class _FakeBoto3:
        def client(self, name):
            assert name == "lambda"
            return fake_lambda

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["VIDEO_DISCOVERY_FUNCTION_NAME"] = "bowling-scraper-video-discovery"
    try:
        result = service.queue_video_stats_refresh(limit=50)
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["VIDEO_DISCOVERY_FUNCTION_NAME"]

    assert result == {"queued": True, "limit": 50}
    assert len(fake_lambda.invocations) == 1
    call = fake_lambda.invocations[0]
    assert call["FunctionName"] == "bowling-scraper-video-discovery"
    assert call["InvocationType"] == "Event"
    assert json.loads(call["Payload"]) == {"refresh_stats": True, "limit": 50}


def test_queue_video_stats_refresh_omits_limit_key_when_not_given():
    """limit=None lets VideoDiscoveryFunction fall back to its own
    DEFAULT_REFRESH_STATS_LIMIT rather than this layer needing to know
    that number too -- see the docstring."""
    fake_lambda = _FakeLambdaClient()

    class _FakeBoto3:
        def client(self, name):
            return fake_lambda

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["VIDEO_DISCOVERY_FUNCTION_NAME"] = "bowling-scraper-video-discovery"
    try:
        service.queue_video_stats_refresh()
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["VIDEO_DISCOVERY_FUNCTION_NAME"]

    assert json.loads(fake_lambda.invocations[0]["Payload"]) == {"refresh_stats": True}


def test_queue_video_stats_refresh_missing_function_name_returns_not_queued():
    os.environ.pop("VIDEO_DISCOVERY_FUNCTION_NAME", None)

    class _ExplodingBoto3:
        def client(self, name):
            raise AssertionError("should never be called when the function name isn't configured")

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _ExplodingBoto3()
    try:
        result = service.queue_video_stats_refresh(limit=50)
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]

    assert result == {"queued": False, "reason": "VIDEO_DISCOVERY_FUNCTION_NAME is not configured on this deployment"}


# --- list_products: needs_video_summary_refresh filter -- confirms the SQL
# text is actually added when the flag is passed (real DB behavior of the
# EXISTS/staleness-comparison subquery itself is untested here for the same
# no-Postgres-in-sandbox reason noted throughout this file; see
# list_products' own docstring for what the filter is supposed to mean).
# Also confirms the plain call (flag omitted/None) doesn't add it, and that
# the id tiebreaker added alongside this filter is present either way.

def test_list_products_omits_refresh_filter_by_default():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "needs_video_summary_refresh" not in query  # not real SQL, just guards against copy-paste
    assert "video_reviews_summary is null" not in query
    assert "order by p.updated_at desc, p.id asc" in query


def test_list_products_needs_video_summary_refresh_adds_filter_sql():
    conn = _QueryCapturingConnection()
    service.list_products(conn, needs_video_summary_refresh=True, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "pv.status = 'approved' and pv.summary is not null" in query
    assert "video_reviews_summary is null" in query
    assert "video_reviews_summary_video_count <>" in query
    assert "order by p.updated_at desc, p.id asc" in query


# --- list_products: has_approved_video_summaries filter -- the deliberately
# broader "just regenerate everything" sibling of needs_video_summary_
# refresh above (see list_products' docstring and backfill_video_review_
# rollups.py's REFRESH_ALL section). Confirms it adds the same EXISTS
# clause but, unlike needs_video_summary_refresh, does NOT add either
# staleness-comparison clause -- that's the whole point of the filter.

def test_list_products_has_approved_video_summaries_adds_filter_sql_without_staleness_check():
    conn = _QueryCapturingConnection()
    service.list_products(conn, has_approved_video_summaries=True, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "pv.status = 'approved' and pv.summary is not null" in query
    assert "video_reviews_summary is null" not in query
    assert "video_reviews_summary_video_count <>" not in query
    assert "order by p.updated_at desc, p.id asc" in query


# --- list_brands: backs the Products/Cores tab brand filter dropdown
# (GET /brands) -- see service.list_brands' docstring. Simple enough
# (single table, no joins/params) for a real FakeCursor test rather than
# just capturing query text.

def test_list_brands_returns_all_brands_sorted_by_name():
    db = {
        "brands": {
            "brand-1": {"id": "brand-1", "name": "Storm"},
            "brand-2": {"id": "brand-2", "name": "Brunswick"},
            "brand-3": {"id": "brand-3", "name": "Ebonite"},
        },
    }
    conn = FakeConnection(db)

    result = service.list_brands(conn)

    assert result == [
        {"id": "brand-2", "name": "Brunswick"},
        {"id": "brand-3", "name": "Ebonite"},
        {"id": "brand-1", "name": "Storm"},
    ]


def test_list_brands_empty_when_none_exist():
    conn = FakeConnection({"brands": {}})
    assert service.list_brands(conn) == []


# --- list_products: missing_core filter + the cores join (migration 007).
# The p./c. aliasing above exists specifically because of this join --
# products and cores both have a plain "name" column, so left-joining
# cores in made every previously-bare column reference ambiguous. Confirms
# the join is actually present (core_name selected) and that missing_core
# adds the expected "core_id is null" clause only when passed.

def test_list_products_joins_cores_and_omits_missing_core_filter_by_default():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "left join cores c on c.id = p.core_id" in query
    assert "c.name as core_name" in query
    assert "left join brands b on b.id = p.brand_id" in query
    assert "b.name as brand_name" in query
    assert "core_id is null" not in query


def test_list_products_missing_core_adds_filter_sql():
    conn = _QueryCapturingConnection()
    service.list_products(conn, missing_core=True, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "p.core_id is null" in query


# --- list_products: source_platform filter -- built for
# scripts/rescrape_netsuite_products.py (the MOTIV image-scoping fix's
# catalog-wide cleanup, see netsuite_product_scraper's "SECOND real bug"
# section and DEPLOY_RUNBOOK.md 6e.6).

def test_list_products_source_platform_adds_filter_sql():
    conn = _QueryCapturingConnection()
    service.list_products(conn, source_platform="netsuite", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "p.source_platform = %s" in query


def test_list_products_omits_source_platform_filter_by_default():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "p.source_platform" not in query


# --- list_products: status filter -- Al's direct ask after the Combat/
# display_order investigation, to filter the Products tab down to just
# current (or just retired) product lines. Same shape as source_platform
# above: adds "p.status = %s" only when passed, param-bound (not string-
# interpolated), no validation against the enum's two values.

def test_list_products_status_adds_filter_sql():
    conn = _QueryCapturingConnection()
    service.list_products(conn, status="current", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "p.status = %s" in query


def test_list_products_omits_status_filter_by_default():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "p.status = %s" not in query


# --- list_products: popularity ranking -- Al's ask ("can we build in a
# view_count time decay so that older videos will organically move down a
# 'popular' ranking"). Same POPULARITY_HALF_LIFE_DAYS/_POPULARITY_SCORE_SQL
# as public_api/service.py's identical copy (kept in sync by hand, see
# that constant's own comment for why there's no shared module). Surfaced
# here too -- not just the public API -- so Al can see/sort by the actual
# computed number in the admin Products tab.

def test_list_products_always_selects_popularity_score():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "as popularity_score" in query
    assert "pv.status = 'approved'" in query
    assert "pv.view_count is not null" in query


def test_list_products_popularity_score_uses_confirmed_half_life():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert f"86400.0 * {service.POPULARITY_HALF_LIFE_DAYS}" in query
    assert service.POPULARITY_HALF_LIFE_DAYS == 180


def test_list_products_popularity_score_averages_not_sums():
    """Al's follow-up, real incident: a raw sum let video COUNT dominate
    the ranking. Confirms the SQL averages per-video decayed views and
    applies a sub-linear ln(1 + count) volume boost, not a plain sum."""
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "select avg(" in query
    assert "* ln(1 + count(*))" in query
    assert "select sum(" not in query


def test_list_products_sort_popularity_orders_by_score_desc():
    conn = _QueryCapturingConnection()
    service.list_products(conn, sort="popularity", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "order by popularity_score desc, p.id asc limit %s offset %s" in query


def test_list_products_default_sort_unaffected_by_popularity_column():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "order by p.updated_at desc, p.id asc limit %s offset %s" in query


# --- common-sense sort options (Al's ask: "lets add some common sense
# sort options for both the admin and consumer UIs") -- newest/oldest by
# release_date, alphabetical by name. See service.py's _SORT_ORDER_BY.

def test_list_products_sort_newest_orders_by_release_date_desc_nulls_last():
    conn = _QueryCapturingConnection()
    service.list_products(conn, sort="newest", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "order by p.release_date desc nulls last, p.id asc limit %s offset %s" in query


def test_list_products_sort_oldest_orders_by_release_date_asc_nulls_last():
    conn = _QueryCapturingConnection()
    service.list_products(conn, sort="oldest", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "order by p.release_date asc nulls last, p.id asc limit %s offset %s" in query


def test_list_products_sort_name_asc_orders_alphabetically():
    conn = _QueryCapturingConnection()
    service.list_products(conn, sort="name_asc", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "order by p.name asc, p.id asc limit %s offset %s" in query


def test_list_products_sort_name_desc_orders_reverse_alphabetically():
    conn = _QueryCapturingConnection()
    service.list_products(conn, sort="name_desc", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "order by p.name desc, p.id asc limit %s offset %s" in query


def test_list_products_every_sort_option_keeps_id_tiebreaker():
    conn = _QueryCapturingConnection()
    for sort_value in service._SORT_ORDER_BY:
        conn.cursor().queries.clear()
        service.list_products(conn, sort=sort_value, limit=50, offset=0)
        query = conn.cursor().queries[0]
        assert ", p.id asc limit %s offset %s" in query, f"sort={sort_value!r} missing id tiebreaker"


# --- list_products: p.release_date column -- real ask from Al ("can we
# pull in the available date from the motiv product page as a release
# date column on products"). Every scraper already parsed and persisted
# this (see 003_date_tracking_and_bowwwl.sql + each *_product_scraper
# module's parse_release_date/upsert_product), it just wasn't in this
# curated SELECT list or rendered anywhere -- this confirms it's actually
# selected now. No filter/params involved, so a plain query-text check is
# enough, same convention as the cores-join test above.

def test_list_products_selects_release_date():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "p.release_date" in query


# --- list_cores / get_core: the "other direction" view of core_name/
# core_type (GET /cores, GET /cores/{id}) -- see service.list_cores'
# docstring for why this exists (multiple products can share one core,
# invisible from the Products tab alone). SQL-text-capturing tests only,
# same convention as list_products' filter tests above -- no real Postgres
# in this sandbox to exercise the actual join/group-by/count behavior
# against. get_core itself (like get_product, get_review_item, etc.) isn't
# otherwise unit tested beyond its not-found path, for the same reason.

def test_list_cores_default_query_joins_brands_and_counts_products():
    conn = _QueryCapturingConnection()
    service.list_cores(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "join brands b on b.id = c.brand_id" in query
    assert "left join products p on p.core_id = c.id" in query
    assert "count(p.id) as product_count" in query
    assert "group by c.id, b.name" in query
    assert "order by product_count desc, c.name asc, c.id asc" in query
    assert "c.brand_id = %s" not in query  # not real SQL, guards against copy-paste
    assert "c.name ilike %s" not in query


def test_list_cores_brand_id_filter_adds_clause():
    conn = _QueryCapturingConnection()
    service.list_cores(conn, brand_id="brand-abc", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "and c.brand_id = %s" in query


def test_list_cores_search_filter_adds_ilike_clause():
    conn = _QueryCapturingConnection()
    service.list_cores(conn, search="Collision", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "and c.name ilike %s" in query


def test_get_core_returns_none_for_missing_id():
    """_QueryCapturingCursor.fetchone() defaults to None (see that class'
    own comment) -- get_core's early `if row is None: return None` means
    the second (products-by-core_id) query never even runs, which this
    also confirms by checking only one query was issued."""
    conn = _QueryCapturingConnection()

    result = service.get_core(conn, "does-not-exist")

    assert result is None
    queries = conn.cursor().queries
    assert len(queries) == 1
    assert "join brands b on b.id = c.brand_id" in queries[0]
    assert "where c.id = %s" in queries[0]


# --- list_products: missing_coverstock filter + coverstock_id/name
# columns (migration 008) -- Al's direct follow-up to the cores work,
# "can we do the same thing we did for cores for covers, those are also
# shared across many balls".

def test_list_products_selects_coverstock_id_and_name():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "p.coverstock_id" in query
    assert "p.coverstock_name" in query


def test_list_products_missing_coverstock_adds_filter_sql():
    conn = _QueryCapturingConnection()
    service.list_products(conn, missing_coverstock=True, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "p.coverstock_id is null" in query


def test_list_products_omits_missing_coverstock_filter_by_default():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "p.coverstock_id is null" not in query


# --- list_coverstocks / get_coverstock: the exact same "other direction"
# view as list_cores/get_core above, one migration later (008). Same
# SQL-text-capturing convention, same reasoning (no real Postgres in this
# sandbox).

def test_list_coverstocks_default_query_joins_brands_and_counts_products():
    conn = _QueryCapturingConnection()
    service.list_coverstocks(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "join brands b on b.id = cs.brand_id" in query
    assert "left join products p on p.coverstock_id = cs.id" in query
    assert "count(p.id) as product_count" in query
    assert "group by cs.id, b.name" in query
    assert "order by product_count desc, cs.name asc, cs.id asc" in query
    assert "cs.brand_id = %s" not in query  # not real SQL, guards against copy-paste
    assert "cs.name ilike %s" not in query


def test_list_coverstocks_brand_id_filter_adds_clause():
    conn = _QueryCapturingConnection()
    service.list_coverstocks(conn, brand_id="brand-abc", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "and cs.brand_id = %s" in query


def test_list_coverstocks_search_filter_adds_ilike_clause():
    conn = _QueryCapturingConnection()
    service.list_coverstocks(conn, search="Pearl Reactive", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "and cs.name ilike %s" in query


def test_get_coverstock_returns_none_for_missing_id():
    """Same not-found short-circuit convention as get_core."""
    conn = _QueryCapturingConnection()

    result = service.get_coverstock(conn, "does-not-exist")

    assert result is None
    queries = conn.cursor().queries
    assert len(queries) == 1
    assert "join brands b on b.id = cs.brand_id" in queries[0]
    assert "where cs.id = %s" in queries[0]


# --- get_product: real ask from Al -- surface every column from every
# related table (not a curated subset) so a data-quality pass can see
# gaps by inspection. See service.get_product's docstring for the full
# reasoning (discovered_urls/bowlerdepot_matches/bowwwl_matches are newly
# exposed here; product_skus/product_images went from a hand-picked
# column list to `select *`).

def test_get_product_returns_none_for_missing_id():
    """Same not-found short-circuit convention as get_core -- the second
    (skus) query and everything after it never runs."""
    conn = _QueryCapturingConnection()

    result = service.get_product(conn, "does-not-exist")

    assert result is None
    queries = conn.cursor().queries
    assert len(queries) == 1
    assert "left join cores c on c.id = p.core_id" in queries[0]
    assert "left join brands b on b.id = p.brand_id" in queries[0]
    assert "left join manufacturers m on m.id = b.manufacturer_id" in queries[0]
    assert "where p.id = %s" in queries[0]


class _SequencedCursor:
    """Fakes a real cursor's fetchone()/fetchall()/description across a
    KNOWN, fixed sequence of queries -- unlike FakeCursor above (which
    matches on query text so call order doesn't matter), this exists
    specifically to test get_product's assembly logic: does each of its
    six sequential queries land in the right key of the final dict. Query
    text itself isn't inspected here (see test_get_product_returns_none_
    for_missing_id above for that, via _QueryCapturingConnection) -- this
    is a complementary test, not a replacement."""
    def __init__(self, results):
        self._results = list(results)
        self._current = None
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.queries.append(" ".join(query.split()))
        self._current = self._results.pop(0)

    @property
    def description(self):
        return [(col,) for col in self._current["columns"]]

    def fetchone(self):
        return self._current.get("one")

    def fetchall(self):
        return self._current.get("all", [])


class _SequencedConnection:
    def __init__(self, results):
        self._cursor = _SequencedCursor(results)

    def cursor(self):
        return self._cursor


def test_get_product_assembles_all_related_data():
    conn = _SequencedConnection([
        {  # main products+cores+brands+manufacturers row
            "columns": ["id", "name", "url", "core_id", "core_name", "core_type",
                        "brand_id", "brand_name", "manufacturer_name"],
            "one": ("prod-1", "Absolute", "https://storm.com/absolute", "core-1",
                    "Collision", "asymmetric", "brand-1", "Storm", "Storm Products"),
        },
        {  # product_skus, select *
            "columns": ["id", "product_id", "weight_lbs", "rg", "differential",
                        "mass_bias", "part_number", "source", "needs_review",
                        "created_at", "updated_at"],
            "all": [("sku-1", "prod-1", 15, "2.500", "0.050", None, None,
                     "html", False, "t0", "t0")],
        },
        {  # product_images, select *
            "columns": ["id", "product_id", "image_type", "weight_lbs_context",
                        "source_url", "stored_url", "created_at"],
            "all": [("img-1", "prod-1", "main", None, "https://cdn/x.jpg", None, "t0")],
        },
        {  # discovered_urls, matched by url -- found this time
            "columns": ["id", "brand_id", "url", "status_path", "sitemap_lastmod",
                        "first_seen_at", "last_seen_at", "last_scraped_at",
                        "scrape_status", "created_at"],
            "one": ("du-1", "brand-1", "https://storm.com/absolute", "current",
                    "t0", "t0", "t0", "t0", "scraped", "t0"),
        },
        {  # bowlerdepot_products
            "columns": ["id", "product_id", "bigcommerce_product_id", "bigcommerce_sku",
                        "match_status", "last_synced_at", "created_at"],
            "all": [],
        },
        {  # bowwwl_products
            "columns": ["id", "product_id", "bowwwl_url", "match_status",
                        "last_checked_at", "created_at"],
            "all": [],
        },
    ])

    result = service.get_product(conn, "prod-1")

    assert result["name"] == "Absolute"
    assert result["brand_name"] == "Storm"
    assert result["manufacturer_name"] == "Storm Products"
    assert result["core_name"] == "Collision"
    assert len(result["skus"]) == 1
    assert result["skus"][0]["rg"] == "2.500"
    assert result["skus"][0]["id"] == "sku-1"  # id is present now -- select * , not a curated list
    assert len(result["images"]) == 1
    assert result["images"][0]["id"] == "img-1"
    assert result["discovered_url"]["scrape_status"] == "scraped"
    assert result["bowlerdepot_matches"] == []
    assert result["bowwwl_matches"] == []


def test_get_product_discovered_url_is_none_when_never_crawled():
    """A product inserted by hand rather than through the normal sitemap/
    collection crawl (e.g. the Hammerhead product from an earlier
    session's manual Lambda invoke) has no discovered_urls row at all --
    this must come back as None, not KeyError or a missing key."""
    conn = _SequencedConnection([
        {"columns": ["id", "name", "url"], "one": ("prod-1", "Hammerhead", "https://hammerbowling.com/products/hammerhead")},
        {"columns": ["id"], "all": []},
        {"columns": ["id"], "all": []},
        {"columns": ["id"], "one": None},
        {"columns": ["id"], "all": []},
        {"columns": ["id"], "all": []},
    ])

    result = service.get_product(conn, "prod-1")

    assert result["discovered_url"] is None


# --- Product image curation (migration 010): display_order/is_thumbnail/
# is_visible -- Al, looking ahead to a customer-facing site: "once we
# actually have a customer facing site we will want to order the images,
# set a thumbnail image and control visibility." See service.
# update_product_image/reorder_product_images docstrings for the full
# reasoning.

def _fake_db_with_product_images():
    return {
        "product_images": {
            "img-1": {"id": "img-1", "product_id": "prod-1", "display_order": 0, "is_thumbnail": True, "is_visible": True},
            "img-2": {"id": "img-2", "product_id": "prod-1", "display_order": 1, "is_thumbnail": False, "is_visible": True},
            "img-3": {"id": "img-3", "product_id": "prod-1", "display_order": 2, "is_thumbnail": False, "is_visible": True},
            "img-other": {"id": "img-other", "product_id": "prod-2", "display_order": 0, "is_thumbnail": True, "is_visible": True},
        },
    }


def test_update_product_image_sets_visibility_only():
    db = _fake_db_with_product_images()
    conn = FakeConnection(db)

    result = service.update_product_image(conn, "prod-1", "img-2", is_visible=False)

    assert result == {"image_id": "img-2", "product_id": "prod-1", "is_visible": False}
    assert db["product_images"]["img-2"]["is_visible"] is False
    assert db["product_images"]["img-2"]["is_thumbnail"] is False  # untouched
    assert conn.committed is True


def test_update_product_image_setting_thumbnail_unsets_others_on_same_product():
    db = _fake_db_with_product_images()
    conn = FakeConnection(db)

    result = service.update_product_image(conn, "prod-1", "img-2", is_thumbnail=True)

    assert result == {"image_id": "img-2", "product_id": "prod-1", "is_thumbnail": True}
    assert db["product_images"]["img-1"]["is_thumbnail"] is False  # was the old thumbnail
    assert db["product_images"]["img-2"]["is_thumbnail"] is True
    assert db["product_images"]["img-3"]["is_thumbnail"] is False  # was already false, stays false


def test_update_product_image_setting_thumbnail_does_not_touch_other_products():
    db = _fake_db_with_product_images()
    conn = FakeConnection(db)

    service.update_product_image(conn, "prod-1", "img-2", is_thumbnail=True)

    assert db["product_images"]["img-other"]["is_thumbnail"] is True  # different product, untouched


def test_update_product_image_can_set_both_fields_in_one_call():
    db = _fake_db_with_product_images()
    conn = FakeConnection(db)

    result = service.update_product_image(conn, "prod-1", "img-2", is_visible=False, is_thumbnail=True)

    assert result == {"image_id": "img-2", "product_id": "prod-1", "is_visible": False, "is_thumbnail": True}
    assert db["product_images"]["img-2"]["is_visible"] is False
    assert db["product_images"]["img-2"]["is_thumbnail"] is True
    assert db["product_images"]["img-1"]["is_thumbnail"] is False


def test_update_product_image_missing_image_raises():
    db = _fake_db_with_product_images()
    conn = FakeConnection(db)
    try:
        service.update_product_image(conn, "prod-1", "does-not-exist", is_visible=False)
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_update_product_image_wrong_product_scoping_raises():
    """img-other belongs to prod-2, not prod-1 -- a caller passing a
    mismatched (product_id, image_id) pair must not be able to mutate a
    different product's image."""
    db = _fake_db_with_product_images()
    conn = FakeConnection(db)
    try:
        service.update_product_image(conn, "prod-1", "img-other", is_visible=False)
        assert False, "expected LookupError"
    except LookupError:
        pass
    assert db["product_images"]["img-other"]["is_visible"] is True  # untouched


def test_update_product_image_no_fields_provided_still_validates_existence():
    db = _fake_db_with_product_images()
    conn = FakeConnection(db)

    result = service.update_product_image(conn, "prod-1", "img-2")

    assert result == {"image_id": "img-2", "product_id": "prod-1"}


def test_reorder_product_images_rewrites_display_order_by_position():
    db = _fake_db_with_product_images()
    conn = FakeConnection(db)

    result = service.reorder_product_images(conn, "prod-1", ["img-3", "img-1", "img-2"])

    assert result == {"product_id": "prod-1", "image_ids": ["img-3", "img-1", "img-2"]}
    assert db["product_images"]["img-3"]["display_order"] == 0
    assert db["product_images"]["img-1"]["display_order"] == 1
    assert db["product_images"]["img-2"]["display_order"] == 2
    assert conn.committed is True


def test_reorder_product_images_ignores_ids_from_other_products():
    """A stray/mistyped id belonging to a different product must not have
    its display_order repointed by this product's reorder call."""
    db = _fake_db_with_product_images()
    conn = FakeConnection(db)

    service.reorder_product_images(conn, "prod-1", ["img-2", "img-other", "img-1"])

    assert db["product_images"]["img-other"]["display_order"] == 0  # untouched, still its original value
    assert db["product_images"]["img-2"]["display_order"] == 0
    assert db["product_images"]["img-1"]["display_order"] == 2


# --- Plotter position (migrations 011/012): oil_rating/motion_rating plus
# oil_motion_source ('chart' | 'estimated' | 'manual'). See service.
# set_plotter_position's docstring -- source defaults to 'manual' (an
# admin's correction), scripts/backfill_plotter_chart_positions.py passes
# 'chart' explicitly.

def test_set_plotter_position_writes_both_fields():
    db = {"products": {"prod-1": {"id": "prod-1"}}}
    conn = FakeConnection(db)

    result = service.set_plotter_position(conn, "prod-1", 6, 18)

    assert db["products"]["prod-1"]["oil_rating"] == 6
    assert db["products"]["prod-1"]["motion_rating"] == 18
    assert db["products"]["prod-1"]["oil_motion_source"] == "manual"  # default, no source given
    assert result == {"product_id": "prod-1", "oil_rating": 6, "motion_rating": 18, "oil_motion_source": "manual"}
    assert conn.committed


def test_set_plotter_position_writes_given_source():
    """scripts/backfill_plotter_chart_positions.py's real call shape --
    passes source='chart' explicitly rather than taking the 'manual'
    default."""
    db = {"products": {"prod-1": {"id": "prod-1"}}}
    conn = FakeConnection(db)

    result = service.set_plotter_position(conn, "prod-1", 6, 18, source="chart")

    assert db["products"]["prod-1"]["oil_motion_source"] == "chart"
    assert result["oil_motion_source"] == "chart"


def test_set_plotter_position_missing_product_raises():
    db = {"products": {}}
    conn = FakeConnection(db)

    try:
        service.set_plotter_position(conn, "no-such-id", 6, 18)
        assert False, "expected LookupError"
    except LookupError:
        pass


# --- estimate_oil_motion / _reference_sku: duplicated from public_api/
# service.py, must behave identically -- spot checks, not the full
# exhaustive sweep (that already lives in test_public_api_service.py).

def test_estimate_oil_motion_matches_public_api_shape():
    result = service.estimate_oil_motion(
        core_type="asymmetric", coverstock_type="solid",
        coverstock_material="reactive_resin", has_particle=False, differential=0.055,
    )
    assert result == {"oil": 13, "motion": 16}


def test_reference_sku_prefers_15lb():
    skus = [
        {"weight_lbs": 14, "differential": 0.01},
        {"weight_lbs": 15, "differential": 0.05},
        {"weight_lbs": 16, "differential": 0.09},
    ]
    assert service._reference_sku(skus)["weight_lbs"] == 15


# --- backfill_estimated_plotter_positions: the "once" half of Al's ask
# ("back fill the values once in the DB and then estimate on scrape if
# not set") -- covers every product that predates the scrapers' own
# estimate-on-scrape hook.

def test_backfill_estimated_plotter_positions_fills_missing_only():
    db = {
        "products": {
            "prod-1": {
                "id": "prod-1", "core_type": "asymmetric", "coverstock_type": "solid",
                "coverstock_material": "reactive_resin", "has_particle": False,
            },
            "prod-2": {
                "id": "prod-2", "oil_rating": 6, "motion_rating": 18, "oil_motion_source": "chart",
                "core_type": "symmetric", "coverstock_type": "pearl", "coverstock_material": "urethane",
                "has_particle": False,
            },
        },
        "product_skus_plotter": [
            {"product_id": "prod-1", "weight_lbs": 15, "differential": 0.055},
        ],
    }
    conn = FakeConnection(db)

    result = service.backfill_estimated_plotter_positions(conn)

    assert result == {"products_missing_position": 1, "products_updated": 1}
    assert db["products"]["prod-1"]["oil_rating"] is not None
    assert db["products"]["prod-1"]["oil_motion_source"] == "estimated"
    # prod-2 already had a chart position -- untouched.
    assert db["products"]["prod-2"]["oil_rating"] == 6
    assert db["products"]["prod-2"]["oil_motion_source"] == "chart"
    assert conn.committed


def test_backfill_estimated_plotter_positions_handles_no_usable_skus():
    """A product with no differential data anywhere still gets a real
    (rounder, less-informed) estimate -- estimate_oil_motion always
    returns a usable position, never skips a product for lack of SKU
    data."""
    db = {
        "products": {
            "prod-1": {"id": "prod-1", "core_type": None, "coverstock_type": None, "coverstock_material": None, "has_particle": False},
        },
        "product_skus_plotter": [],
    }
    conn = FakeConnection(db)

    result = service.backfill_estimated_plotter_positions(conn)

    assert result["products_updated"] == 1
    assert db["products"]["prod-1"]["oil_rating"] is not None
    assert db["products"]["prod-1"]["motion_rating"] is not None


def test_backfill_estimated_plotter_positions_no_op_when_nothing_missing():
    db = {
        "products": {
            "prod-1": {"id": "prod-1", "oil_rating": 6, "motion_rating": 18, "oil_motion_source": "manual"},
        },
        "product_skus_plotter": [],
    }
    conn = FakeConnection(db)

    result = service.backfill_estimated_plotter_positions(conn)

    assert result == {"products_missing_position": 0, "products_updated": 0}


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
