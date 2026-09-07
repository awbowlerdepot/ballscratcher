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


# --- resolve_caller_from_event (task #468, admin SPA users) ---

def test_resolve_caller_from_event_extracts_cognito_user_context():
    event = {"requestContext": {"authorizer": {"lambda": {
        "caller_type": "user", "resolved_by": "al@bowlerdepot.com", "role": "admin",
    }}}}
    assert service.resolve_caller_from_event(event) == {
        "caller_type": "user", "resolved_by": "al@bowlerdepot.com", "role": "admin",
    }


def test_resolve_caller_from_event_extracts_automation_context():
    event = {"requestContext": {"authorizer": {"lambda": {
        "caller_type": "automation", "resolved_by": "automation", "role": "admin",
    }}}}
    assert service.resolve_caller_from_event(event) == {
        "caller_type": "automation", "resolved_by": "automation", "role": "admin",
    }


def test_resolve_caller_from_event_defaults_when_context_missing():
    # Shouldn't happen for a real deployed request (AdminHttpApi's
    # DefaultAuthorizer always runs first and always returns a context on
    # success), but must degrade to a safe, non-crashing default rather
    # than KeyError -- see the function's own docstring for why "admin"
    # (not "editor") is the safe fallback role given nothing is gated on
    # role yet.
    assert service.resolve_caller_from_event({}) == {
        "caller_type": "unknown", "resolved_by": "unknown", "role": "admin",
    }
    assert service.resolve_caller_from_event(None) == {
        "caller_type": "unknown", "resolved_by": "unknown", "role": "admin",
    }


def test_resolve_caller_from_event_defaults_when_authorizer_shape_partial():
    # requestContext present but no authorizer/lambda nesting -- same
    # defensive default, not a crash.
    assert service.resolve_caller_from_event({"requestContext": {}}) == {
        "caller_type": "unknown", "resolved_by": "unknown", "role": "admin",
    }


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

        elif q.startswith("update products set oil_rating") and "where id = %s and oil_motion_source = 'estimated'" in q:
            # reestimate_plotter_positions' per-row OVERWRITE -- checked
            # before the backfill branch below since both queries contain
            # the literal text "oil_motion_source = 'estimated'"
            # somewhere; this one is distinguished by that text living in
            # the WHERE clause (re-checked at write time) rather than the
            # SET clause, and only ever writes 2 columns (oil_rating/
            # motion_rating), never touching oil_motion_source itself --
            # it's still 'estimated' after a re-estimate, just a better
            # estimate now.
            oil_rating, motion_rating, product_id = params
            row = self.db["products"].get(product_id)
            self._last_result = None
            if row is not None and row.get("oil_motion_source") == "estimated":
                row["oil_rating"] = oil_rating
                row["motion_rating"] = motion_rating
                self._last_result = (product_id,)
            self.description = [("id",)]

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

        elif q.startswith("select p.id, c.core_type, p.coverstock_type, p.coverstock_material, p.has_particle") and "oil_motion_source = 'estimated'" in q:
            # reestimate_plotter_positions' scan -- every product CURRENTLY
            # marked 'estimated' (regardless of whether oil_rating is
            # already set, which it always is for this source), checked
            # before the missing-position branch below since both start
            # with the same select-list prefix.
            estimated = [
                (pid, row.get("core_type"), row.get("coverstock_type"),
                 row.get("coverstock_material"), row.get("has_particle"))
                for pid, row in self.db["products"].items()
                if row.get("oil_motion_source") == "estimated"
            ]
            self._rows = estimated
            self.description = [("id",), ("core_type",), ("coverstock_type",), ("coverstock_material",), ("has_particle",)]

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

        # --- price tracking (migration 014/015): create_price_site/
        # update_price_site/delete_price_site, create_product_price_source/
        # update_product_price_source/delete_product_price_source, and
        # approve_price_source/reject_price_source/restore_price_source
        # (same status-transition shape as the product_videos branches
        # above). The read-only list_price_sites/list_product_price_
        # sources/list_price_sources/get_price_history queries are
        # exercised via _QueryCapturingConnection instead (SQL-shape
        # assertions only, same convention list_cores/list_coverstocks
        # tests already use) -- not modeled here.

        elif q.startswith("insert into price_sites"):
            # 016_price_tracking_bigcommerce.sql added fetch_method/
            # api_provider/base_url to this INSERT's column list --
            # params always has all 8 now (create_price_site's own
            # fetch_method default is "scrape", not None, so this never
            # needs an older/shorter-params fallback).
            (name, search_url_template, result_link_selector, default_css_selector, notes,
             fetch_method, api_provider, base_url) = params
            self.db.setdefault("_price_site_id_seq", 0)
            self.db["_price_site_id_seq"] += 1
            new_id = f"site-new-{self.db['_price_site_id_seq']}"
            self.db.setdefault("price_sites", {})[new_id] = {
                "id": new_id, "name": name, "search_url_template": search_url_template,
                "result_link_selector": result_link_selector, "default_css_selector": default_css_selector,
                "notes": notes, "is_active": True,
                "fetch_method": fetch_method, "api_provider": api_provider, "base_url": base_url,
            }
            self._last_result = (new_id,)
            self.description = [("id",)]

        elif q.startswith("select id from price_sites where id = %s"):
            (site_id,) = params
            row = self.db.get("price_sites", {}).get(site_id)
            self._last_result = (row["id"],) if row else None
            self.description = [("id",)]

        elif q.startswith("update price_sites set") and "returning id" in q:
            site_id = params[-1]
            set_clause_text = q.split("set ", 1)[1].split(" where", 1)[0]
            columns = [c.split(" =", 1)[0].strip() for c in set_clause_text.split(",")]
            row = self.db.get("price_sites", {}).get(site_id)
            if row is not None:
                for column, value in zip(columns, params[:-1]):
                    row[column] = value
                self._last_result = (site_id,)
            else:
                self._last_result = None
            self.description = [("id",)]

        elif q.startswith("delete from price_sites where id = %s"):
            (site_id,) = params
            existed = site_id in self.db.get("price_sites", {})
            self.db.get("price_sites", {}).pop(site_id, None)
            self._last_result = (site_id,) if existed else None
            self.description = [("id",)]

        elif q.startswith("insert into product_price_sources") and "'approved', 'manual'" in q:
            # create_product_price_source's manual-override path --
            # distinct branch from discovery's own insert (which price_
            # checker, not admin_api, ever calls -- see that module's own
            # tests) since the column list/status/source differ.
            # external_product_id (016_price_tracking_bigcommerce.sql) is
            # the 5th column now, before resolved_by.
            product_id, price_site_id, product_url, css_selector, external_product_id, resolved_by = params
            self.db.setdefault("_price_source_id_seq", 0)
            self.db["_price_source_id_seq"] += 1
            new_id = f"src-new-{self.db['_price_source_id_seq']}"
            self.db.setdefault("product_price_sources", {})[new_id] = {
                "id": new_id, "product_id": product_id, "price_site_id": price_site_id,
                "product_url": product_url, "css_selector": css_selector, "is_active": True,
                "last_checked_at": None, "status": "approved", "source": "manual",
                "match_query": None, "match_confidence": None, "resolved_by": resolved_by,
                "external_product_id": external_product_id,
            }
            self._last_result = (new_id,)
            self.description = [("id",)]

        elif q.startswith("select id from product_price_sources where id = %s"):
            (source_id,) = params
            row = self.db.get("product_price_sources", {}).get(source_id)
            self._last_result = (row["id"],) if row else None
            self.description = [("id",)]

        elif q.startswith("select status from product_price_sources where id = %s"):
            (source_id,) = params
            row = self.db.get("product_price_sources", {}).get(source_id)
            self._last_result = (row["status"],) if row else None
            self.description = [("status",)]

        elif q.startswith("update product_price_sources set status = 'approved'"):
            resolved_by, source_id = params
            row = self.db["product_price_sources"][source_id]
            row["status"] = "approved"
            row["resolved_by"] = resolved_by
            self._last_result = None

        elif q.startswith("update product_price_sources set status = 'rejected'"):
            resolved_by, source_id = params
            row = self.db["product_price_sources"][source_id]
            row["status"] = "rejected"
            row["resolved_by"] = resolved_by
            self._last_result = None

        elif q.startswith("update product_price_sources set status = 'pending'"):
            # restore_price_source's undo -- clears resolved_at/resolved_by
            # too, same as restore_video_candidate's FakeCursor branch.
            (source_id,) = params
            row = self.db["product_price_sources"][source_id]
            row["status"] = "pending"
            row["resolved_by"] = None
            row["resolved_at"] = None
            self._last_result = None

        elif q.startswith("update product_price_sources set") and "returning id" in q:
            source_id = params[-1]
            set_clause_text = q.split("set ", 1)[1].split(" where", 1)[0]
            columns = [c.split(" =", 1)[0].strip() for c in set_clause_text.split(",")]
            row = self.db.get("product_price_sources", {}).get(source_id)
            if row is not None:
                for column, value in zip(columns, params[:-1]):
                    row[column] = value
                self._last_result = (source_id,)
            else:
                self._last_result = None
            self.description = [("id",)]

        elif q.startswith("delete from product_price_sources where id = %s"):
            (source_id,) = params
            existed = source_id in self.db.get("product_price_sources", {})
            self.db.get("product_price_sources", {}).pop(source_id, None)
            self._last_result = (source_id,) if existed else None
            self.description = [("id",)]

        elif q.startswith("select count(*) from product_price_sources where status = 'pending'"):
            count = sum(1 for r in self.db.get("product_price_sources", {}).values() if r.get("status") == "pending")
            self._last_result = (count,)
            self.description = [("count",)]

        elif q.startswith("select id, product_id, price_site_id, product_url, status, is_active, created_at"):
            # dedupe_product_price_sources' own full-table scan.
            rows = list(self.db.get("product_price_sources", {}).values())
            rows.sort(key=lambda r: (r["product_id"], r["price_site_id"], r.get("created_at") or ""))
            self._rows = [
                (r["id"], r["product_id"], r["price_site_id"], r["product_url"],
                 r["status"], r.get("is_active", True), r.get("created_at"))
                for r in rows
            ]
            self.description = [
                ("id",), ("product_id",), ("price_site_id",), ("product_url",),
                ("status",), ("is_active",), ("created_at",),
            ]

        elif q.startswith("update product_price_sources set product_url = %s where id"):
            product_url, source_id = params
            self.db["product_price_sources"][source_id]["product_url"] = product_url
            self._last_result = None

        elif q.startswith("update product_price_history set price_source_id"):
            new_id, old_id = params
            for row in self.db.get("product_price_history", []):
                if row["price_source_id"] == old_id:
                    row["price_source_id"] = new_id
            self._last_result = None

        elif q.startswith("update product_sku_stock_history set price_source_id"):
            new_id, old_id = params
            for row in self.db.get("product_sku_stock_history", []):
                if row["price_source_id"] == old_id:
                    row["price_source_id"] = new_id
            self._last_result = None

        # --- 021_blocked_video_channels.sql: the competitor-channel
        # filter feeding bowlerdepot_video_sync's list_videos_needing_
        # sync (Al: "some of these videos are from our competitors and we
        # should avoid putting those on there"). db["blocked_video_channels"]
        # is a plain dict keyed by id, same shape as db["price_sites"].

        elif q.startswith("select id, channel_title, note, created_at from blocked_video_channels order by created_at desc"):
            rows = sorted(
                self.db.get("blocked_video_channels", {}).values(),
                key=lambda r: r.get("created_at") or "", reverse=True,
            )
            self._rows = [(r["id"], r["channel_title"], r.get("note"), r.get("created_at")) for r in rows]
            self.description = [("id",), ("channel_title",), ("note",), ("created_at",)]

        elif q.startswith("insert into blocked_video_channels"):
            channel_title, note = params
            existing = next(
                (r for r in self.db.get("blocked_video_channels", {}).values()
                 if r["channel_title"].lower() == channel_title.lower()),
                None,
            )
            if existing is not None:
                # on conflict (lower(channel_title)) do nothing -- no row
                # returned, service.create_blocked_channel falls back to
                # the lookup-by-lower(channel_title) select below.
                self._last_result = None
            else:
                self.db.setdefault("_blocked_channel_id_seq", 0)
                self.db["_blocked_channel_id_seq"] += 1
                new_id = f"blocked-new-{self.db['_blocked_channel_id_seq']}"
                row = {"id": new_id, "channel_title": channel_title, "note": note, "created_at": "now"}
                self.db.setdefault("blocked_video_channels", {})[new_id] = row
                self._last_result = (row["id"], row["channel_title"], row["note"], row["created_at"])
            self.description = [("id",), ("channel_title",), ("note",), ("created_at",)]

        elif q.startswith("select id, channel_title, note, created_at from blocked_video_channels where lower(channel_title)"):
            (channel_title,) = params
            row = next(
                (r for r in self.db.get("blocked_video_channels", {}).values()
                 if r["channel_title"].lower() == channel_title.lower()),
                None,
            )
            self._last_result = (row["id"], row["channel_title"], row.get("note"), row.get("created_at")) if row else None
            self.description = [("id",), ("channel_title",), ("note",), ("created_at",)]

        elif q.startswith("delete from blocked_video_channels where id = %s"):
            (channel_id,) = params
            existed = channel_id in self.db.get("blocked_video_channels", {})
            self.db.get("blocked_video_channels", {}).pop(channel_id, None)
            self._last_result = (channel_id,) if existed else None
            self.description = [("id",)]

        # --- 027_manual_seed_urls.sql: orphan-page catch for url_discovery
        # Lambdas (real Storm Equinox incident, 2026-09-04 -- see that
        # migration's own header comment). db["manual_seed_urls"] rows
        # carry brand_name directly (fixtures set it), same "flat dict, no
        # real join" simplification the product_articles section just
        # below already uses for its own products/brands join.

        elif q.startswith("select m.id, m.brand_id, b.name as brand_name, m.url, m.note, m.created_at from manual_seed_urls"):
            rows = sorted(
                self.db.get("manual_seed_urls", {}).values(),
                key=lambda r: r.get("created_at") or "", reverse=True,
            )
            self._rows = [
                (r["id"], r["brand_id"], r.get("brand_name"), r["url"], r.get("note"), r.get("created_at"))
                for r in rows
            ]
            self.description = [("id",), ("brand_id",), ("brand_name",), ("url",), ("note",), ("created_at",)]

        elif q.startswith("insert into manual_seed_urls"):
            brand_id, url, note = params
            existing = next(
                (r for r in self.db.get("manual_seed_urls", {}).values() if r["url"] == url),
                None,
            )
            if existing is not None:
                # on conflict (url) do nothing -- no row returned,
                # service.create_manual_seed_url falls back to the
                # lookup-by-url select below.
                self._last_result = None
            else:
                self.db.setdefault("_manual_seed_url_id_seq", 0)
                self.db["_manual_seed_url_id_seq"] += 1
                new_id = f"seed-new-{self.db['_manual_seed_url_id_seq']}"
                row = {"id": new_id, "brand_id": brand_id, "url": url, "note": note, "created_at": "now"}
                self.db.setdefault("manual_seed_urls", {})[new_id] = row
                self._last_result = (row["id"], row["brand_id"], row["url"], row["note"], row["created_at"])
            self.description = [("id",), ("brand_id",), ("url",), ("note",), ("created_at",)]

        elif q.startswith("select id, brand_id, url, note, created_at from manual_seed_urls where url = %s"):
            (url,) = params
            row = next(
                (r for r in self.db.get("manual_seed_urls", {}).values() if r["url"] == url),
                None,
            )
            self._last_result = (row["id"], row["brand_id"], row["url"], row.get("note"), row.get("created_at")) if row else None
            self.description = [("id",), ("brand_id",), ("url",), ("note",), ("created_at",)]

        elif q.startswith("delete from manual_seed_urls where id = %s"):
            (seed_id,) = params
            existed = seed_id in self.db.get("manual_seed_urls", {})
            self.db.get("manual_seed_urls", {}).pop(seed_id, None)
            self._last_result = (seed_id,) if existed else None
            self.description = [("id",)]

        # --- 022_product_articles.sql: ball-review article review workflow
        # (Al: "this could be the backend that pulls together all the
        # creative and content for the frontend"). db["product_articles"]
        # rows carry product_name/brand_name directly on the row -- same
        # "flat dict, no real join" simplification blocked_video_channels'
        # own section above uses; this fake doesn't model an actual SQL
        # join against separate products/brands tables.

        elif q.startswith("select pa.id, pa.product_id, p.name as product_name, b.name as brand_name"):
            rows = list(self.db.get("product_articles", {}).values())
            remaining = list(params)
            # Params order mirrors service.list_articles' own conditions
            # list: status (if given), then product_id (if given), then
            # limit/offset always last -- see that function's docstring.
            if "pa.status = %s" in q:
                status = remaining.pop(0)
                rows = [r for r in rows if r["status"] == status]
            if "pa.product_id = %s" in q:
                product_id = remaining.pop(0)
                rows = [r for r in rows if r["product_id"] == product_id]
            rows.sort(key=lambda r: (r.get("created_at") or "", r["id"]), reverse=True)
            self._rows = [
                (r["id"], r["product_id"], r["product_name"], r["brand_name"], r["status"],
                 r.get("title"), r.get("generated_at"), r.get("reviewed_at"), r.get("resolved_by"),
                 r.get("created_at"), r.get("action_shot_image_url"), r.get("product_shot_image_url"),
                 r.get("images_generated_at"), r.get("sync_to_bigcommerce"), r.get("bigcommerce_post_id"),
                 r.get("bowlerdepot_synced_at"))
                for r in rows
            ]
            self.description = [
                ("id",), ("product_id",), ("product_name",), ("brand_name",), ("status",),
                ("title",), ("generated_at",), ("reviewed_at",), ("resolved_by",), ("created_at",),
                ("action_shot_image_url",), ("product_shot_image_url",), ("images_generated_at",),
                ("sync_to_bigcommerce",), ("bigcommerce_post_id",), ("bowlerdepot_synced_at",),
            ]

        elif q.startswith("select pa.*, p.name as product_name, b.name as brand_name"):
            (article_id,) = params
            row = self.db.get("product_articles", {}).get(article_id)
            if row is None:
                self._last_result = None
                self.description = [("id",)]
            else:
                columns = list(row.keys())
                self._last_result = tuple(row[c] for c in columns)
                self.description = [(c,) for c in columns]

        elif q.startswith("select status from product_articles where id = %s"):
            (article_id,) = params
            row = self.db.get("product_articles", {}).get(article_id)
            self._last_result = (row["status"],) if row else None
            self.description = [("status",)]

        elif q.startswith("select id from product_articles where id = %s"):
            # queue_article_sync's existence check -- deliberately just
            # "does this row exist," no status/flag gate (see that
            # function's own docstring for why it doesn't duplicate
            # bowlerdepot_article_sync's own needs-sync gating).
            (article_id,) = params
            row = self.db.get("product_articles", {}).get(article_id)
            self._last_result = (article_id,) if row else None
            self.description = [("id",)]

        elif q.startswith("update product_articles set status = 'approved'"):
            resolved_by, article_id = params
            row = self.db["product_articles"][article_id]
            row["status"] = "approved"
            row["resolved_by"] = resolved_by
            row["reviewed_at"] = "now"
            self._last_result = None

        elif q.startswith("update product_articles set status = 'rejected'"):
            resolved_by, article_id = params
            row = self.db["product_articles"][article_id]
            row["status"] = "rejected"
            row["resolved_by"] = resolved_by
            row["reviewed_at"] = "now"
            self._last_result = None

        elif q.startswith("update product_articles set sync_to_bigcommerce"):
            # set_article_bigcommerce_sync (028_product_articles_
            # bigcommerce_sync.sql) -- returning id, so a missing row
            # yields None here, same "let fetchone() come back empty"
            # shape as set_product_published's own generic "update
            # products set" branch above.
            sync_to_bigcommerce, article_id = params
            row = self.db["product_articles"].get(article_id)
            self._last_result = None
            if row is not None:
                row["sync_to_bigcommerce"] = sync_to_bigcommerce
                self._last_result = (article_id,)
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


# --- get_dashboard_summary: Al: "can we create an admin dashboard with
# some KPIs and top 10 lists... Top 10s i think we can do Popularity and
# ADUs. An interesting number would be total ADUs across all balls, ADUs
# by brand, and things like that." Later same session, follow-up ask:
# "can we add top 10 days of supply skus descending so lowest number of
# days first... can we build something would show top 10 growth ADUs and
# top 10 shrinking ADUs by sku" (both quotes predate the 2026-09-06
# ADU->Daily Movement rename -- see service._TOTAL_DAILY_MOVEMENT_SQL's
# own comment -- underlying metric unchanged). Seven sequential queries
# total (KPIs, top popularity, top daily movement, daily movement by
# brand, top days-of-supply, top growing daily movement, top shrinking
# daily movement) -- _QueryCapturingConnection's fetchone() defaults to
# None (unusable here, an aggregate query with no GROUP BY always returns
# exactly one row), so a tiny local cursor stands in for it that returns
# an empty-but-iterable row/rowset instead, purely so the SQL TEXT of all
# seven queries can still be captured and asserted on without the
# function crashing trying to zip() a real result together.
# Assembly-logic correctness (does each query's result land in the right
# key) is covered separately below via _SequencedConnection, same split
# as test_get_product_* already uses.

class _DashboardQueryCapturingCursor:
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
        return ()  # iterable, unlike _QueryCapturingCursor's None default


class _DashboardQueryCapturingConnection:
    def __init__(self):
        self._cursor = _DashboardQueryCapturingCursor()

    def cursor(self):
        return self._cursor


def test_get_dashboard_summary_kpi_query_counts_expected_things():
    conn = _DashboardQueryCapturingConnection()
    service.get_dashboard_summary(conn)

    kpi_query = conn.cursor().queries[0]
    for fragment in (
        "select count(*) from products",
        "from products where status = 'current'",
        "from products where status = 'retired'",
        "from products where core_id is null",
        "from products where coverstock_id is null",
        "not exists ( select 1 from product_skus ps where ps.product_id = p.id )",
        "pv.status = 'approved' and pv.summary is not null",
        "pps.status = 'approved' and pps.is_active",
    ):
        assert fragment in kpi_query


def test_get_dashboard_summary_kpi_query_reuses_total_daily_movement_sql():
    """total_catalog_daily_movement must read the SAME materialized
    products.total_daily_movement column (030) list_products' own column
    and sort option already read -- not a second, potentially-drifting
    definition of daily movement. As of the 2026-09-06 materialization
    fix this is a plain sum over the column, not a correlated subquery."""
    conn = _DashboardQueryCapturingConnection()
    service.get_dashboard_summary(conn)

    kpi_query = conn.cursor().queries[0]
    assert "(select coalesce(sum(total_daily_movement), 0) from products) as total_catalog_daily_movement" in kpi_query


def test_get_dashboard_summary_top_popularity_query_shape():
    """As of migration 030, this reads the materialized
    products.popularity_score column directly -- no correlated subquery
    against product_videos anymore (that formula now lives solely in
    refresh_product_scores/app.py)."""
    conn = _DashboardQueryCapturingConnection()
    service.get_dashboard_summary(conn)

    query = conn.cursor().queries[1]
    assert "left join brands b on b.id = p.brand_id" in query
    assert "where p.popularity_score > 0" in query
    assert "order by p.popularity_score desc, p.id asc limit 10" in query
    assert "product_videos" not in query  # the old correlated-subquery shape, must be gone


def test_get_dashboard_summary_top_daily_movement_query_shape():
    """As of migration 030, this reads the materialized
    products.total_daily_movement column directly."""
    conn = _DashboardQueryCapturingConnection()
    service.get_dashboard_summary(conn)

    query = conn.cursor().queries[2]
    assert "left join brands b on b.id = p.brand_id" in query
    assert "where p.total_daily_movement > 0" in query
    assert "order by p.total_daily_movement desc, p.id asc limit 10" in query
    assert "product_sku_stock_history" not in query  # the old correlated-subquery shape, must be gone


def test_get_dashboard_summary_daily_movement_by_brand_query_shape():
    """Unlike the other two, this is a real GROUP BY over ALL brands
    (left join, so a brand with zero matching products/movement still
    shows up at 0 -- Al's "ADUs by brand" ask (predates the ADU->Daily
    Movement rename) reads as the full breakdown, not a filtered
    top-N)."""
    conn = _DashboardQueryCapturingConnection()
    service.get_dashboard_summary(conn)

    query = conn.cursor().queries[3]
    assert "with sku_daily_movement as" in query
    assert "from brands b" in query
    assert "left join products p on p.brand_id = b.id" in query
    assert "group by b.name" in query
    assert "order by total_daily_movement desc" in query
    assert "where t.total_daily_movement > 0" not in query  # no >0 filter here, unlike top_daily_movement


def test_get_dashboard_summary_top_growing_daily_movement_query_shape():
    """Al: "top 10 growth ADUs... by sku" (predates the ADU->Daily
    Movement rename). Compares the current
    DAILY_MOVEMENT_LOOKBACK_DAYS-day window's per-SKU rate against the
    DAILY_MOVEMENT_LOOKBACK_DAYS days before that -- both windows driven
    off DAILY_MOVEMENT_LOOKBACK_DAYS (not hardcoded literals), confirming
    the "current" window stays in lockstep with every other daily-
    movement query's own definition of "current". Index 4, not 5 --
    top_days_of_supply is no longer one of get_dashboard_summary's own
    queries (see service.get_top_days_of_supply's own docstring for
    why)."""
    conn = _DashboardQueryCapturingConnection()
    service.get_dashboard_summary(conn)

    query = conn.cursor().queries[4]
    assert "with cw as" in query
    assert "pw as" in query
    assert ("interval '%d days'" % service.DAILY_MOVEMENT_LOOKBACK_DAYS) in query
    assert ("interval '%d days'" % (2 * service.DAILY_MOVEMENT_LOOKBACK_DAYS)) in query
    assert "where cw.daily_movement is not null and pw.daily_movement is not null and (cw.daily_movement - pw.daily_movement) > 0" in query
    assert "order by (cw.daily_movement - pw.daily_movement) desc, sk.id asc limit 10" in query


def test_get_dashboard_summary_top_shrinking_daily_movement_query_shape():
    """Mirror image of top_growing_daily_movement immediately above --
    same shape, opposite filter/sort direction (biggest decrease
    first)."""
    conn = _DashboardQueryCapturingConnection()
    service.get_dashboard_summary(conn)

    query = conn.cursor().queries[5]
    assert "with cw as" in query
    assert "pw as" in query
    assert "where cw.daily_movement is not null and pw.daily_movement is not null and (cw.daily_movement - pw.daily_movement) < 0" in query
    assert "order by (cw.daily_movement - pw.daily_movement) asc, sk.id asc limit 10" in query


def test_get_dashboard_summary_only_runs_six_queries():
    conn = _DashboardQueryCapturingConnection()
    service.get_dashboard_summary(conn)
    assert len(conn.cursor().queries) == 6


def test_get_dashboard_summary_assembles_all_six_results():
    conn = _SequencedConnection([
        {  # KPIs
            "columns": [
                "total_products", "current_products", "retired_products",
                "missing_core", "missing_coverstock", "missing_skus",
                "products_with_video", "products_with_price_tracking",
                "total_catalog_daily_movement",
            ],
            "one": (500, 350, 150, 12, 3, 7, 200, 180, "1234.5"),
        },
        {  # top_popularity
            "columns": ["id", "name", "brand_name", "popularity_score"],
            "all": [
                ("prod-1", "Absolute", "Storm", "980.2"),
                ("prod-2", "Phaze II", "Storm", "875.0"),
            ],
        },
        {  # top_daily_movement
            "columns": ["id", "name", "brand_name", "total_daily_movement"],
            "all": [
                ("prod-3", "Black Widow 3.0", "Hammer", "42.5"),
            ],
        },
        {  # daily_movement_by_brand
            "columns": ["brand_name", "total_daily_movement"],
            "all": [
                ("Storm", "300.1"),
                ("Hammer", "150.0"),
                ("Ebonite", "0"),
            ],
        },
        {  # top_growing_daily_movement
            "columns": ["product_id", "name", "brand_name", "weight_lbs", "previous_daily_movement", "current_daily_movement", "delta_daily_movement"],
            "all": [
                ("prod-5", "Intel Tour", "900 Global", 16, "2.00", "5.70", "3.70"),
            ],
        },
        {  # top_shrinking_daily_movement
            "columns": ["product_id", "name", "brand_name", "weight_lbs", "previous_daily_movement", "current_daily_movement", "delta_daily_movement"],
            "all": [
                ("prod-6", "Bionic", "900 Global", 14, "6.00", "1.50", "-4.50"),
            ],
        },
    ])

    result = service.get_dashboard_summary(conn)

    assert result["kpis"] == {
        "total_products": 500, "current_products": 350, "retired_products": 150,
        "missing_core": 12, "missing_coverstock": 3, "missing_skus": 7,
        "products_with_video": 200, "products_with_price_tracking": 180,
        "total_catalog_daily_movement": "1234.5",
    }
    assert result["top_popularity"] == [
        {"id": "prod-1", "name": "Absolute", "brand_name": "Storm", "popularity_score": "980.2"},
        {"id": "prod-2", "name": "Phaze II", "brand_name": "Storm", "popularity_score": "875.0"},
    ]
    assert result["top_daily_movement"] == [
        {"id": "prod-3", "name": "Black Widow 3.0", "brand_name": "Hammer", "total_daily_movement": "42.5"},
    ]
    assert result["daily_movement_by_brand"] == [
        {"brand_name": "Storm", "total_daily_movement": "300.1"},
        {"brand_name": "Hammer", "total_daily_movement": "150.0"},
        {"brand_name": "Ebonite", "total_daily_movement": "0"},
    ]
    assert "top_days_of_supply" not in result  # split out, see get_top_days_of_supply
    assert result["top_growing_daily_movement"] == [
        {"product_id": "prod-5", "name": "Intel Tour", "brand_name": "900 Global", "weight_lbs": 16,
         "previous_daily_movement": "2.00", "current_daily_movement": "5.70", "delta_daily_movement": "3.70"},
    ]
    assert result["top_shrinking_daily_movement"] == [
        {"product_id": "prod-6", "name": "Bionic", "brand_name": "900 Global", "weight_lbs": 14,
         "previous_daily_movement": "6.00", "current_daily_movement": "1.50", "delta_daily_movement": "-4.50"},
    ]


# --- get_top_days_of_supply / list_sku_weights: split out of
# get_dashboard_summary specifically so Al's weight-toggle ask ("can we
# put a filter so we can toggle the different weights so that we can see
# 15 only or 15 and 14 etc.") can re-run just this one query, not the
# other six dashboard queries, on every checkbox change. A dedicated
# params-capturing double is needed here (unlike the query-text-only
# doubles above) since the whole point of this test is confirming
# weight_lbs is actually bound as a query parameter, not just present in
# the SQL text.

class _ParamsCapturingCursor:
    def __init__(self):
        self.calls = []  # list of (query_text, params)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.calls.append((" ".join(query.split()), params))

    @property
    def description(self):
        return []

    def fetchall(self):
        return []


class _ParamsCapturingConnection:
    def __init__(self):
        self._cursor = _ParamsCapturingCursor()

    def cursor(self):
        return self._cursor


def test_get_top_days_of_supply_unfiltered_omits_weight_clause_and_params():
    conn = _ParamsCapturingConnection()
    service.get_top_days_of_supply(conn)

    query, params = conn.cursor().calls[0]
    assert "and sk.weight_lbs = any(" not in query
    assert params == []


def test_get_top_days_of_supply_filtered_adds_weight_clause_and_params():
    conn = _ParamsCapturingConnection()
    service.get_top_days_of_supply(conn, weight_lbs=[15, 14])

    query, params = conn.cursor().calls[0]
    assert "and sk.weight_lbs = any(%s)" in query
    assert params == [[15, 14]]


def test_get_top_days_of_supply_query_shape():
    """Same query shape the old get_dashboard_summary-embedded version
    had -- this function is a pure extraction, not a rewrite."""
    conn = _ParamsCapturingConnection()
    service.get_top_days_of_supply(conn)

    query, _ = conn.cursor().calls[0]
    assert "with sa as" in query
    assert "latest_qty as" in query
    assert "distinct on (product_sku_id)" in query
    assert "sk.weight_lbs" in query
    assert "where sa.daily_movement > 0 and lq.quantity is not null" in query
    assert "order by (lq.quantity / sa.daily_movement) asc, sk.id asc limit 10" in query


def test_get_top_days_of_supply_only_runs_one_query():
    conn = _ParamsCapturingConnection()
    service.get_top_days_of_supply(conn, weight_lbs=[15])
    assert len(conn.cursor().calls) == 1


def test_get_top_days_of_supply_assembles_rows():
    conn = _SequencedConnection([
        {
            "columns": ["product_id", "name", "brand_name", "weight_lbs", "daily_movement", "latest_quantity", "days_of_supply"],
            "all": [
                ("prod-4", "Fallout", "Roto Grip", 15, "8.90", 4, "0.4"),
            ],
        },
    ])

    result = service.get_top_days_of_supply(conn, weight_lbs=[15])

    assert result == [
        {"product_id": "prod-4", "name": "Fallout", "brand_name": "Roto Grip", "weight_lbs": 15,
         "daily_movement": "8.90", "latest_quantity": 4, "days_of_supply": "0.4"},
    ]


def test_list_sku_weights_query_shape():
    conn = _QueryCapturingConnection()
    service.list_sku_weights(conn)

    query = conn.cursor().queries[0]
    assert "select distinct weight_lbs from product_skus" in query
    assert "where weight_lbs is not null" in query
    assert "order by weight_lbs" in query


def test_list_sku_weights_returns_flat_list():
    conn = _SequencedConnection([
        {"columns": ["weight_lbs"], "all": [(12,), (14,), (15,), (16,)]},
    ])
    assert service.list_sku_weights(conn) == [12, 14, 15, 16]


# --- get_catalog_daily_movement_history: real follow-up ask, same
# session, Al: "can we add some data over time charts to the dashboard,
# maybe total catalog adu over time similar to what we have per product
# 7d, 30d, 90d, 1y and all picker" (predates the 2026-09-06 ADU->Daily
# Movement rename). A DIFFERENT definition from
# kpis.total_catalog_daily_movement above (see the function's own
# docstring) -- a real, honest, catalog-wide rolling-
# DAILY_MOVEMENT_LOOKBACK_DAYS-day average, not that exact per-SKU-gated
# formula replayed at every historical day.

def test_get_catalog_daily_movement_history_query_shape():
    conn = _QueryCapturingConnection()
    service.get_catalog_daily_movement_history(conn)

    query = conn.cursor().queries[0]
    assert "with bounds as" in query
    assert "generate_series(min_day, max_day, interval '1 day')" in query
    assert "product_sku_stock_history" in query
    assert "lag(psh.quantity) over (partition by psh.product_sku_id order by psh.checked_at)" in query
    # Drops-only, same interpretation as
    # _TOTAL_DAILY_MOVEMENT_SQL/daily_movement_by_brand.
    assert "case when delta < 0 then -delta else 0 end" in query
    # Rolling 30-day trailing window, driven off
    # DAILY_MOVEMENT_LOOKBACK_DAYS (not a second hardcoded literal) --
    # confirms the two stay in lockstep.
    assert ("rows between %d preceding and current row" % (service.DAILY_MOVEMENT_LOOKBACK_DAYS - 1)) in query
    assert ("/ %s as total_daily_movement" % float(service.DAILY_MOVEMENT_LOOKBACK_DAYS)) in query


def test_get_catalog_daily_movement_history_only_runs_one_query():
    conn = _QueryCapturingConnection()
    service.get_catalog_daily_movement_history(conn)
    assert len(conn.cursor().queries) == 1


def test_get_catalog_daily_movement_history_assembles_day_and_total_rows():
    conn = _SequencedConnection([
        {
            "columns": ["day", "total_daily_movement"],
            "all": [
                ("2026-07-01", "0.00"),
                ("2026-07-02", "3.50"),
                ("2026-07-03", "3.90"),
            ],
        },
    ])

    result = service.get_catalog_daily_movement_history(conn)

    assert result == [
        {"day": "2026-07-01", "total_daily_movement": "0.00"},
        {"day": "2026-07-02", "total_daily_movement": "3.50"},
        {"day": "2026-07-03", "total_daily_movement": "3.90"},
    ]


def test_get_catalog_daily_movement_history_empty_when_no_stock_history():
    conn = _SequencedConnection([{"columns": ["day", "total_daily_movement"], "all": []}])
    assert service.get_catalog_daily_movement_history(conn) == []


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


# --- list_products: article_id/article_status -- left-joined from
# product_articles (022_product_articles.sql, one row max per product,
# unique on product_id) so admin-spa's Products list can render a
# per-row Article status icon without a second round-trip. Al: "add
# icons to the product list items to click on the different elements
# that could be associated with them from the list... an article icon
# with state so green if approved, yellow if pending, and grey if not
# generated." Always joined/selected (no opt-in flag), same "cheap
# enough to include unconditionally" convention core_name/coverstock_
# name/the three materialized score columns above already follow --
# there's no filter argument gating this one, just the always-present
# join.

def test_list_products_joins_product_articles_for_article_status():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "left join product_articles pa on pa.product_id = p.id" in query
    assert "pa.id as article_id" in query
    assert "pa.status as article_status" in query


# --- list_products: video_count/approved_video_count/approved_summarized_
# video_count -- Al's direct follow-up to the article icon: "can we add
# one similar for videos. similar state. grey if none approved and yellow
# if approve but no summaries and green if approved and summaries. maybe
# a count next to the icon for number of videos." Unlike product_articles
# (one-to-one, plain left join), product_videos is one-to-many -- this
# needs a `left join lateral` computing all three counts in one pass
# rather than a join that would fan out the product row per video.

def test_list_products_joins_lateral_video_counts():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "left join lateral (" in query
    assert "count(*) as video_count" in query
    assert "count(*) filter (where pv.status = 'approved') as approved_video_count" in query
    assert (
        "count(*) filter (where pv.status = 'approved' and pv.summary is not null) "
        "as approved_summarized_video_count" in query
    )
    assert "from product_videos pv" in query
    assert "where pv.product_id = p.id" in query
    assert ") v on true" in query
    assert "coalesce(v.video_count, 0) as video_count" in query
    assert "coalesce(v.approved_video_count, 0) as approved_video_count" in query
    assert "coalesce(v.approved_summarized_video_count, 0) as approved_summarized_video_count" in query


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


# --- list_products: popularity_score/total_daily_movement/demand_score
# -- Al's original asks: video-popularity time decay ("can we build in a
# view_count time decay so that older videos will organically move down a
# 'popular' ranking"), catalog-wide unit movement ("can we add the sum of
# the ADUs for each product to the main table"), and a blended Demand
# Score ("loosely avg daily movement is a demand number... we could take
# this demand number and enhance the popularity number"). As of migration
# 030 (2026-09-06 performance fix), all three are plain materialized
# columns on `products`, recomputed daily by refresh_product_scores/
# app.py -- list_products below just reads them now; no correlated
# subqueries or CTEs left in this query at all. The formula-level tests
# (half-life value, avg-not-sum, drops-only movement, demand weights,
# etc.) live in tests/test_refresh_product_scores.py instead.

def test_list_products_always_selects_the_three_materialized_score_columns():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "p.popularity_score" in query
    assert "p.total_daily_movement" in query
    assert "p.demand_score" in query
    # None of the old live-computed shapes should remain in this query.
    # NOTE: product_videos itself is no longer a valid "must be absent"
    # check here -- the video-status-icon feature (see the "left join
    # lateral" tests above) added a legitimate, deliberate join against
    # it for an unrelated reason (video_count/approved_video_count/
    # approved_summarized_video_count). percent_rank()/product_sku_
    # stock_history are still the right "old scoring shape is gone"
    # checks: neither has any other reason to appear in this query.
    assert "product_sku_stock_history" not in query
    assert "percent_rank()" not in query


def test_list_products_sort_popularity_orders_by_score_desc():
    conn = _QueryCapturingConnection()
    service.list_products(conn, sort="popularity", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "order by p.popularity_score desc, p.id asc limit %s offset %s" in query


def test_list_products_sort_total_daily_movement_orders_by_column_desc():
    conn = _QueryCapturingConnection()
    service.list_products(conn, sort="total_daily_movement", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "order by p.total_daily_movement desc, p.id asc limit %s offset %s" in query


def test_list_products_sort_demand_score_orders_by_column_desc():
    conn = _QueryCapturingConnection()
    service.list_products(conn, sort="demand_score", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "order by p.demand_score desc, p.id asc limit %s offset %s" in query


def test_list_products_default_sort_unaffected_by_score_columns():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "order by p.updated_at desc, p.id asc limit %s offset %s" in query


def test_list_products_score_columns_still_present_alongside_filters():
    """The three score columns are part of the fixed SELECT list, not
    conditionally built like the WHERE clauses below them -- they must
    survive regardless of which list_products filters are active."""
    conn = _QueryCapturingConnection()
    service.list_products(conn, brand_id="some-brand-id", missing_core=True, sort="newest", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "p.popularity_score" in query
    assert "p.total_daily_movement" in query
    assert "p.demand_score" in query


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


# (duplicate test_list_products_sort_total_daily_movement_orders_by_column_desc
# removed here -- see the materialized-columns block above, which now
# covers this same sort option reading the real p.total_daily_movement
# column instead of a select-list alias.)


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


# --- list_products: missing_skus filter -- real incident, Al: product
# 56897c0b-e3ec-4314-a8dc-238e1b8b7a75 (Storm Tropical Surge Black/Cherry)
# had zero product_skus despite its real page clearly showing weight/RG/
# differential values (root cause: commercebuild_product_scraper's
# parse_tech_data_pdf_url missing a "Tech Sheet" wording variant, now
# fixed). Unlike missing_core/missing_coverstock (a nullable column
# directly on products), product_skus is a separate table, so this is a
# `not exists` subquery.

def test_list_products_missing_skus_adds_not_exists_filter_sql():
    conn = _QueryCapturingConnection()
    service.list_products(conn, missing_skus=True, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "not exists (select 1 from product_skus ps where ps.product_id = p.id)" in query


def test_list_products_omits_missing_skus_filter_by_default():
    # Scoped to the specific NOT EXISTS filter clause rather than a
    # blanket "product_skus" substring check --
    # _TOTAL_DAILY_MOVEMENT_SQL now legitimately joins product_skus
    # (aliased ps_dm) unconditionally on every call to compute the Total
    # Daily Movement column, so a bare "product_skus" absence check
    # would false-positive now that it's a normal part of every query,
    # filter or not.
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "not exists (select 1 from product_skus ps where ps.product_id = p.id)" not in query


def test_list_products_missing_skus_combines_with_source_platform():
    # scripts/rescrape_commercebuild_products.py's exact call shape --
    # both filters must AND together, not override each other.
    conn = _QueryCapturingConnection()
    service.list_products(conn, missing_skus=True, source_platform="commercebuild", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "not exists (select 1 from product_skus ps where ps.product_id = p.id)" in query
    assert "p.source_platform = %s" in query


# --- list_products: html_fallback_skus filter -- later real follow-up,
# Al: "viking still only has 1 sku". commercebuild_product_scraper's
# _html_fallback_skus stopgap (missing_skus' own fix) gives an
# image-based-PDF product exactly one source='html' product_skus row --
# a nonzero count, so it stops matching missing_skus above at all, even
# though it's still missing the other real weights until Amazon Textract
# OCR (parse_tech_data_pdf_via_textract) recovers the full table on a
# rescrape. This filter is how to find that specific in-between state
# again: at least one product_skus row exists, AND none of them are
# anything other than source='html'.
#
# REAL BUG, caught live via the batch panel itself: without a
# source_platform scope, this filter matched roughly the entire non-
# commercebuild catalog -- Al pasted a batch log of 1034 "matches" that
# turned out to be real Hammer/Track/Ebonite ball names (Black Widow,
# Raw Hammer, Theorem Delta, Paradox, Scandal, all confirmed against
# this repo's own tests/fixtures/hammer_*.json/track_*.json), because
# every non-commercebuild scraper writes source='html' as its SKUs'
# ONLY, correct, healthy source -- "every row is html" only means
# "still needs OCR" on commercebuild specifically.

def test_list_products_html_fallback_skus_adds_exists_and_not_exists_filter_sql():
    conn = _QueryCapturingConnection()
    service.list_products(conn, html_fallback_skus=True, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "exists (select 1 from product_skus ps3 where ps3.product_id = p.id)" in query
    assert "not exists (select 1 from product_skus ps4 where ps4.product_id = p.id and ps4.source <> 'html')" in query


def test_list_products_html_fallback_skus_scoped_to_commercebuild():
    """Real regression test for the Hammer/Track/Ebonite false-positive
    bug above -- this filter must ALWAYS restrict to commercebuild, even
    when the caller doesn't pass source_platform explicitly, since "all
    SKU rows are source='html'" is the normal, correct, healthy state on
    every other platform."""
    conn = _QueryCapturingConnection()
    service.list_products(conn, html_fallback_skus=True, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "p.source_platform = 'commercebuild'" in query


def test_list_products_omits_html_fallback_skus_filter_by_default():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "ps4.source <> 'html'" not in query


def test_list_products_html_fallback_skus_distinct_from_missing_skus():
    """The two filters must add DIFFERENT where-clause text -- a zero-row
    product should match missing_skus but not html_fallback_skus (there's
    no row to be all-html), and a product with one source='html' row
    should match html_fallback_skus but not missing_skus (it has a row).
    This only asserts the generated SQL text differs when each filter is
    used alone -- the actual row-level semantics aren't exercised here
    (FakeCursor doesn't model NOT EXISTS/EXISTS subqueries), same
    limitation as every other missing_*/html_fallback_skus test in this
    file, all of which check generated SQL text via
    _QueryCapturingConnection rather than real query execution."""
    conn = _QueryCapturingConnection()
    service.list_products(conn, missing_skus=True, limit=50, offset=0)
    missing_skus_query = conn.cursor().queries[0]

    conn2 = _QueryCapturingConnection()
    service.list_products(conn2, html_fallback_skus=True, limit=50, offset=0)
    html_fallback_query = conn2.cursor().queries[0]

    assert "not exists (select 1 from product_skus ps where ps.product_id = p.id)" in missing_skus_query
    assert "not exists (select 1 from product_skus ps where ps.product_id = p.id)" not in html_fallback_query
    assert "ps4.source <> 'html'" in html_fallback_query
    assert "ps4.source <> 'html'" not in missing_skus_query


def test_list_products_html_fallback_skus_combines_with_source_platform():
    """Passing source_platform explicitly alongside html_fallback_skus is
    redundant (the filter already hardcodes the commercebuild scope) but
    harmless -- both the hardcoded literal and the parameterized clause
    end up in the query, ANDed together, not conflicting."""
    conn = _QueryCapturingConnection()
    service.list_products(conn, html_fallback_skus=True, source_platform="commercebuild", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "ps4.source <> 'html'" in query
    assert "p.source_platform = 'commercebuild'" in query
    assert "p.source_platform = %s" in query


# --- list_products: missing_video_candidates filter -- Al's ask after
# learning VideoDiscoveryFunction's search job (the thing that actually
# calls YouTube's search.list to find candidate review videos) is
# deliberately manual/invoke-only, not scheduled, because search.list is
# capped at 100 calls/day for this project -- there's no automatic
# "search every new product" step. This filter finds every product with
# ZERO product_videos rows of any status, i.e. never searched at all
# (indistinguishable here from "searched and came up empty" -- same
# `not exists` shape as missing_skus, product_videos is a separate table).

def test_list_products_missing_video_candidates_adds_not_exists_filter_sql():
    conn = _QueryCapturingConnection()
    service.list_products(conn, missing_video_candidates=True, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "not exists (select 1 from product_videos pv where pv.product_id = p.id)" in query


def test_list_products_omits_missing_video_candidates_filter_by_default():
    # Checks for the exact WHERE-clause text this filter adds (a plain,
    # unscoped `not exists`), not just any mention of product_videos --
    # other filters below (needs_video_summary_refresh/has_approved_
    # video_summaries) also reference product_videos with their own,
    # differently-scoped `exists` clauses.
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "not exists (select 1 from product_videos pv where pv.product_id = p.id)" not in query


def test_list_products_missing_video_candidates_combines_with_status():
    conn = _QueryCapturingConnection()
    service.list_products(conn, missing_video_candidates=True, status="current", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "not exists (select 1 from product_videos pv where pv.product_id = p.id)" in query
    assert "p.status = %s" in query


def test_list_products_missing_video_candidates_distinct_from_needs_video_summary_refresh():
    """Not the same filter as needs_video_summary_refresh/has_approved_
    video_summaries -- those both require an EXISTING approved+summarized
    video (an `exists` check with pv.status/pv.summary conditions inside
    it); this one requires the opposite, zero product_videos rows of any
    status at all -- the WHERE-clause text this filter adds has no
    status/summary condition inside it (unlike needs_video_summary_
    refresh/has_approved_video_summaries' own `exists` clauses, which
    do)."""
    conn = _QueryCapturingConnection()
    service.list_products(conn, missing_video_candidates=True, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "and not exists (select 1 from product_videos pv where pv.product_id = p.id)" in query
    # needs_video_summary_refresh/has_approved_video_summaries' own `exists`
    # clause text (single-spaced, matching how _QueryCapturingCursor
    # normalizes whitespace) -- must NOT be present, confirming this is a
    # genuinely different WHERE condition, not the same filter reused.
    assert "and exists ( select 1 from product_videos pv where pv.product_id = p.id and pv.status = 'approved' and pv.summary is not null )" not in query


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
    # oil: reactive_resin base 10 + solid adjust 0 (2026-08-14 refit, see
    # public_api/service.py's module comment above its own estimate_
    # oil_motion) = 10. motion: asymmetric base 8 + ((0.055-0.02)/0.045)*8
    # ~= 6.22 + solid adjust 1 = 15.22 -> round -> 15.
    result = service.estimate_oil_motion(
        core_type="asymmetric", coverstock_type="solid",
        coverstock_material="reactive_resin", has_particle=False, differential=0.055,
    )
    assert result == {"oil": 10, "motion": 15}


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


# --- reestimate_plotter_positions: the "go fix everything the OLD formula
# already got wrong" pass -- Al: "i feel like it is way off for most
# balls". Unlike backfill_estimated_plotter_positions above, this
# OVERWRITES existing oil_motion_source='estimated' rows rather than only
# filling nulls.

def test_reestimate_plotter_positions_overwrites_estimated_only():
    db = {
        "products": {
            "prod-1": {
                # Seeded with a STALE estimate (as if written by the pre-
                # refit formula) -- the whole point of this test is
                # confirming reestimate_plotter_positions overwrites it
                # with whatever the CURRENT estimate_oil_motion computes.
                "id": "prod-1", "oil_rating": 13, "motion_rating": 16, "oil_motion_source": "estimated",
                "core_type": "asymmetric", "coverstock_type": "solid",
                "coverstock_material": "reactive_resin", "has_particle": False,
            },
            "prod-2": {
                "id": "prod-2", "oil_rating": 6, "motion_rating": 18, "oil_motion_source": "chart",
                "core_type": "asymmetric", "coverstock_type": "solid",
                "coverstock_material": "reactive_resin", "has_particle": False,
            },
            "prod-3": {
                "id": "prod-3", "oil_rating": 9, "motion_rating": 17, "oil_motion_source": "manual",
                "core_type": "asymmetric", "coverstock_type": "pearl",
                "coverstock_material": "reactive_resin", "has_particle": False,
            },
        },
        "product_skus_plotter": [
            {"product_id": "prod-1", "weight_lbs": 15, "differential": 0.055},
        ],
    }
    conn = FakeConnection(db)

    result = service.reestimate_plotter_positions(conn)

    assert result == {"products_estimated": 1, "products_updated": 1}
    # prod-1 (the only 'estimated' row) got recomputed with the CURRENT
    # formula -- same inputs as test_estimate_oil_motion_matches_public_
    # api_shape above (oil=10, motion=15), NOT the stale seeded 13/16.
    assert db["products"]["prod-1"]["oil_rating"] == 10
    assert db["products"]["prod-1"]["motion_rating"] == 15
    assert db["products"]["prod-1"]["oil_motion_source"] == "estimated"  # unchanged
    # chart and manual positions are completely untouched.
    assert db["products"]["prod-2"] == {
        "id": "prod-2", "oil_rating": 6, "motion_rating": 18, "oil_motion_source": "chart",
        "core_type": "asymmetric", "coverstock_type": "solid",
        "coverstock_material": "reactive_resin", "has_particle": False,
    }
    assert db["products"]["prod-3"]["oil_motion_source"] == "manual"
    assert db["products"]["prod-3"]["oil_rating"] == 9
    assert conn.committed


def test_reestimate_plotter_positions_no_op_when_nothing_estimated():
    db = {
        "products": {
            "prod-1": {"id": "prod-1", "oil_rating": 6, "motion_rating": 18, "oil_motion_source": "chart"},
        },
        "product_skus_plotter": [],
    }
    conn = FakeConnection(db)

    result = service.reestimate_plotter_positions(conn)

    assert result == {"products_estimated": 0, "products_updated": 0}


def test_reestimate_plotter_positions_handles_no_usable_skus():
    db = {
        "products": {
            "prod-1": {
                "id": "prod-1", "oil_rating": 9, "motion_rating": 9, "oil_motion_source": "estimated",
                "core_type": None, "coverstock_type": None, "coverstock_material": None, "has_particle": False,
            },
        },
        "product_skus_plotter": [],
    }
    conn = FakeConnection(db)

    result = service.reestimate_plotter_positions(conn)

    assert result["products_updated"] == 1
    assert db["products"]["prod-1"]["oil_rating"] is not None
    assert db["products"]["prod-1"]["motion_rating"] is not None


# ---------------------------------------------------------------------
# Price tracking (migration 014/015) -- Al: "id like to start a price
# tracker... configurable to have site setup so that it will pull the
# current price from a number of sites... store this in a way that
# would allow for charting that price over time."
#
# DESIGN CORRECTION mid-build: "site setup" means choosing which real
# retailers to track, with each product's URL found AUTOMATICALLY by
# price_checker's discovery job (mirroring video_discovery's YouTube
# search) -- and after weighing auto-track-immediately against a
# pending-review gate, Al settled on "the reccomended path is best":
# mirror product_videos' pending/approved/rejected review workflow
# exactly, including undo/restore. See service.py's own "Price tracking"
# section header comment for the full design summary.
#
# list_price_sites/list_product_price_sources/list_price_sources/
# get_price_history are read-only joins with no branching logic to speak
# of -- exercised via _QueryCapturingConnection (SQL-shape assertions),
# same convention list_cores/list_coverstocks already use, rather than
# fully modeled in FakeCursor. approve_price_source/reject_price_source/
# restore_price_source are exercised the same way approve/reject/
# restore_video_candidate are above (status-transition assertions
# against FakeCursor/FakeConnection). Everything else that mutates
# (create/update/delete, the four queue_price_*/queue_price_discovery*
# Lambda triggers) is exercised against FakeCursor/FakeConnection or
# _FakeLambdaClient like the rest of this file.
# ---------------------------------------------------------------------

def test_list_price_sites_orders_by_name():
    conn = _QueryCapturingConnection()
    service.list_price_sites(conn)
    query = conn.cursor().queries[0]
    assert "from price_sites" in query
    assert "order by name asc" in query


def test_list_product_price_sources_joins_price_sites_and_computes_latest_price():
    conn = _QueryCapturingConnection()
    service.list_product_price_sources(conn, "prod-1")
    query = conn.cursor().queries[0]
    assert "from product_price_sources pps" in query
    assert "join price_sites ps on ps.id = pps.price_site_id" in query
    assert "where pps.product_id = %s" in query
    # status=None (the default, unlike GET /price-sources' catalog-wide
    # default of "pending") returns every status -- see this function's
    # own docstring, mirroring list_video_candidates' status=None case.
    assert "pps.status = %s" not in query
    # The "latest price" convenience fields (see service.
    # list_product_price_sources' docstring) are live-computed via a
    # correlated subquery, not a stored column.
    assert "from product_price_history h" in query
    assert "order by h.checked_at desc limit 1" in query


def test_list_product_price_sources_status_filter_when_given():
    conn = _QueryCapturingConnection()
    service.list_product_price_sources(conn, "prod-1", status="pending")
    query = conn.cursor().queries[0]
    assert "pps.status = %s" in query


def test_list_product_price_sources_includes_fetch_method_and_cost_stock():
    # 016_price_tracking_bigcommerce.sql -- ps.fetch_method plus two more
    # correlated subqueries (latest_cost_price/latest_in_stock), same
    # pattern as the existing latest_price/latest_checked_at/latest_error
    # subqueries this function already had.
    conn = _QueryCapturingConnection()
    service.list_product_price_sources(conn, "prod-1")
    query = conn.cursor().queries[0]
    assert "ps.fetch_method" in query
    assert "h.cost_price" in query
    assert "h.in_stock" in query


def test_list_product_price_sources_includes_base_url():
    # Al: "the href in the admin ui on the price sources page is relative
    # so it is broken... it needs to be fully qualified for the site it
    # is for" -- ps.base_url lets the admin-site resolve a relative
    # product_url defensively at render time.
    conn = _QueryCapturingConnection()
    service.list_product_price_sources(conn, "prod-1")
    query = conn.cursor().queries[0]
    assert "ps.base_url" in query


def test_list_price_sources_includes_base_url():
    conn = _QueryCapturingConnection()
    service.list_price_sources(conn)
    query = conn.cursor().queries[0]
    assert "ps.base_url" in query


def test_list_price_sources_defaults_to_pending_and_orders_by_confidence():
    conn = _QueryCapturingConnection()
    service.list_price_sources(conn)
    query = conn.cursor().queries[0]
    assert "from product_price_sources pps" in query
    assert "join products p on p.id = pps.product_id" in query
    assert "join brands b on b.id = p.brand_id" in query
    assert "join price_sites ps on ps.id = pps.price_site_id" in query
    assert "pps.status = %s" in query
    assert "order by pps.match_confidence asc, pps.created_at asc, pps.id asc limit %s offset %s" in query


def test_list_price_sources_status_all_omits_filter():
    conn = _QueryCapturingConnection()
    service.list_price_sources(conn, status=None)
    query = conn.cursor().queries[0]
    assert "pps.status = %s" not in query


def test_get_price_history_scopes_by_product_id_and_days_window():
    conn = _QueryCapturingConnection()
    service.get_price_history(conn, "prod-1", days=30)
    queries = conn.cursor().queries
    assert len(queries) == 2  # sources query, then history query
    assert "where pps.product_id = %s and pps.status = 'approved'" in queries[0]
    assert "where pps.product_id = %s" in queries[1]
    assert "h.checked_at >= now() - (%s || ' days')::interval" in queries[1]


def test_get_price_history_selects_cost_price_and_in_stock():
    # 016_price_tracking_bigcommerce.sql -- both ride along in the same
    # history query, null for a scrape-sourced row, real values for a
    # BowlerDepot/'api' one.
    conn = _QueryCapturingConnection()
    service.get_price_history(conn, "prod-1", days=30)
    history_query = conn.cursor().queries[1]
    assert "h.cost_price" in history_query
    assert "h.in_stock" in history_query


# --- get_sku_stock_history: 017_price_tracking_sku_stock.sql read side.
# Al: "for the instock i was refering to actual number of each sku
# instock." Same two-query shape as get_price_history above. ---

def test_get_sku_stock_history_scopes_by_product_id_and_days_window():
    conn = _QueryCapturingConnection()
    service.get_sku_stock_history(conn, "prod-1", days=30)
    queries = conn.cursor().queries
    assert len(queries) == 2  # skus query, then history query
    assert "from product_skus" in queries[0]
    assert "where product_id = %s" in queries[0]
    assert "from product_sku_stock_history h" in queries[1]
    assert "join product_skus sk on sk.id = h.product_sku_id" in queries[1]
    assert "where sk.product_id = %s" in queries[1]
    assert "h.checked_at >= now() - (%s || ' days')::interval" in queries[1]


def test_get_sku_stock_history_selects_quantity_and_checked_at():
    conn = _QueryCapturingConnection()
    service.get_sku_stock_history(conn, "prod-1", days=30)
    history_query = conn.cursor().queries[1]
    assert "h.quantity" in history_query
    assert "h.checked_at" in history_query
    assert "h.price_source_id" in history_query


def _fake_db_with_price_site():
    return {
        "price_sites": {
            "site-1": {
                "id": "site-1", "name": "BowlerDepot",
                "search_url_template": "https://bowlerdepot.com/search?q={query}",
                "result_link_selector": ".product-link",
                "default_css_selector": ".price", "notes": None, "is_active": True,
            },
        },
    }


def test_create_price_site_inserts_row():
    db = {"price_sites": {}}
    conn = FakeConnection(db)

    result = service.create_price_site(
        conn, "BowlerDepot", "https://bowlerdepot.com/search?q={query}", ".product-link",
        ".price-item--sale", notes="BigCommerce store",
    )

    assert result["name"] == "BowlerDepot"
    new_id = result["id"]
    assert db["price_sites"][new_id]["search_url_template"] == "https://bowlerdepot.com/search?q={query}"
    assert db["price_sites"][new_id]["result_link_selector"] == ".product-link"
    assert db["price_sites"][new_id]["default_css_selector"] == ".price-item--sale"
    assert db["price_sites"][new_id]["notes"] == "BigCommerce store"
    assert conn.committed is True


def test_update_price_site_partial_update_only_touches_given_fields():
    db = _fake_db_with_price_site()
    conn = FakeConnection(db)

    service.update_price_site(conn, "site-1", default_css_selector=".new-price")

    assert db["price_sites"]["site-1"]["default_css_selector"] == ".new-price"
    assert db["price_sites"]["site-1"]["name"] == "BowlerDepot"  # untouched


def test_update_price_site_can_update_search_config():
    db = _fake_db_with_price_site()
    conn = FakeConnection(db)

    service.update_price_site(conn, "site-1", result_link_selector=".item-link")

    assert db["price_sites"]["site-1"]["result_link_selector"] == ".item-link"
    assert db["price_sites"]["site-1"]["search_url_template"] == "https://bowlerdepot.com/search?q={query}"  # untouched


def test_update_price_site_can_deactivate():
    db = _fake_db_with_price_site()
    conn = FakeConnection(db)

    service.update_price_site(conn, "site-1", is_active=False)

    assert db["price_sites"]["site-1"]["is_active"] is False


# --- 016_price_tracking_bigcommerce.sql: fetch_method/api_provider/
# base_url on price_sites, for the BowlerDepot/BigCommerce 'api' source
# type alongside the original 'scrape' design.

def test_create_price_site_defaults_fetch_method_to_scrape():
    db = {"price_sites": {}}
    conn = FakeConnection(db)

    result = service.create_price_site(
        conn, "BowlingBall.com", "https://bowlingball.com/search?q={query}", ".product-link", ".price",
    )

    assert result["fetch_method"] == "scrape"
    new_id = result["id"]
    assert db["price_sites"][new_id]["fetch_method"] == "scrape"
    assert db["price_sites"][new_id]["api_provider"] is None


def test_create_price_site_api_fetch_method_with_no_scrape_fields():
    db = {"price_sites": {}}
    conn = FakeConnection(db)

    result = service.create_price_site(
        conn, "BowlerDepot", fetch_method="api", api_provider="bigcommerce",
        base_url="https://www.bowlerdepot.com",
    )

    assert result["fetch_method"] == "api"
    assert result["api_provider"] == "bigcommerce"
    assert result["search_url_template"] is None
    new_id = result["id"]
    assert db["price_sites"][new_id]["api_provider"] == "bigcommerce"
    assert db["price_sites"][new_id]["base_url"] == "https://www.bowlerdepot.com"


def test_update_price_site_can_set_fetch_method_and_api_fields():
    db = _fake_db_with_price_site()
    conn = FakeConnection(db)

    service.update_price_site(conn, "site-1", fetch_method="api", api_provider="bigcommerce",
                               base_url="https://www.bowlerdepot.com")

    assert db["price_sites"]["site-1"]["fetch_method"] == "api"
    assert db["price_sites"]["site-1"]["api_provider"] == "bigcommerce"
    assert db["price_sites"]["site-1"]["base_url"] == "https://www.bowlerdepot.com"
    # Untouched -- not part of this partial update.
    assert db["price_sites"]["site-1"]["name"] == "BowlerDepot"


def test_update_price_site_missing_raises():
    db = {"price_sites": {}}
    conn = FakeConnection(db)
    try:
        service.update_price_site(conn, "no-such-site", name="X")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_delete_price_site_removes_row():
    db = _fake_db_with_price_site()
    conn = FakeConnection(db)

    result = service.delete_price_site(conn, "site-1")

    assert result == {"deleted": True, "id": "site-1"}
    assert "site-1" not in db["price_sites"]


def test_delete_price_site_missing_raises():
    db = {"price_sites": {}}
    conn = FakeConnection(db)
    try:
        service.delete_price_site(conn, "no-such-site")
        assert False, "expected LookupError"
    except LookupError:
        pass


# --- 021_blocked_video_channels.sql: competitor-channel filter feeding
# bowlerdepot_video_sync (Al: "some of these videos are from our
# competitors and we should avoid putting those on there"). Same
# list/create-with-dedupe/delete-by-id shape as price_sites above.

def test_list_blocked_channels_returns_all_most_recent_first():
    db = {"blocked_video_channels": {
        "b1": {"id": "b1", "channel_title": "Bowling.com", "note": None, "created_at": "2026-01-01"},
        "b2": {"id": "b2", "channel_title": "BowlingBall.com", "note": "competitor retailer", "created_at": "2026-02-01"},
    }}
    conn = FakeConnection(db)

    result = service.list_blocked_channels(conn)

    assert [r["id"] for r in result] == ["b2", "b1"]
    assert result[0] == {"id": "b2", "channel_title": "BowlingBall.com", "note": "competitor retailer", "created_at": "2026-02-01"}


def test_create_blocked_channel_inserts_row():
    db = {"blocked_video_channels": {}}
    conn = FakeConnection(db)

    result = service.create_blocked_channel(conn, "Bowling.com", note="competitor retailer")

    assert result["channel_title"] == "Bowling.com"
    assert result["note"] == "competitor retailer"
    new_id = result["id"]
    assert db["blocked_video_channels"][new_id]["channel_title"] == "Bowling.com"
    assert conn.committed is True


def test_create_blocked_channel_dedupes_case_insensitively():
    """Re-blocking an already-blocked channel (even with different
    casing) is a harmless no-op that returns the EXISTING row, not a
    second row or an error -- mirrors the case-insensitive unique index
    021_blocked_video_channels.sql adds."""
    db = {"blocked_video_channels": {
        "b1": {"id": "b1", "channel_title": "Bowling.com", "note": None, "created_at": "2026-01-01"},
    }}
    conn = FakeConnection(db)

    result = service.create_blocked_channel(conn, "BOWLING.COM", note="second attempt")

    assert result["id"] == "b1"
    assert len(db["blocked_video_channels"]) == 1
    # The original row's note is untouched -- create_blocked_channel's
    # ON CONFLICT DO NOTHING means the second call's note is simply
    # dropped, not merged.
    assert db["blocked_video_channels"]["b1"]["note"] is None


def test_delete_blocked_channel_removes_row():
    db = {"blocked_video_channels": {
        "b1": {"id": "b1", "channel_title": "Bowling.com", "note": None, "created_at": "2026-01-01"},
    }}
    conn = FakeConnection(db)

    result = service.delete_blocked_channel(conn, "b1")

    assert result == {"deleted": True, "id": "b1"}
    assert "b1" not in db["blocked_video_channels"]


def test_delete_blocked_channel_missing_raises():
    db = {"blocked_video_channels": {}}
    conn = FakeConnection(db)
    try:
        service.delete_blocked_channel(conn, "no-such-channel")
        assert False, "expected LookupError"
    except LookupError:
        pass


# --- 027_manual_seed_urls.sql: orphan-page catch (real Storm Equinox
# incident, 2026-09-04) -- list/create/delete_manual_seed_url ---

def test_list_manual_seed_urls_returns_all_most_recent_first_with_brand_name():
    db = {"manual_seed_urls": {
        "s1": {
            "id": "s1", "brand_id": "brand-storm", "brand_name": "Storm",
            "url": "https://www.stormbowling.com/storm-equinox-bowling-ball",
            "note": "orphaned on-site, superseded by Equinox Hybrid/Solid variants",
            "created_at": "2026-09-04",
        },
        "s2": {
            "id": "s2", "brand_id": "brand-rg", "brand_name": "Roto Grip",
            "url": "https://www.stormbowling.com/roto-grip-something-orphaned-bowling-ball",
            "note": None, "created_at": "2026-01-01",
        },
    }}
    conn = FakeConnection(db)

    result = service.list_manual_seed_urls(conn)

    assert [r["id"] for r in result] == ["s1", "s2"]
    assert result[0]["brand_name"] == "Storm"
    assert result[0]["url"] == "https://www.stormbowling.com/storm-equinox-bowling-ball"


def test_create_manual_seed_url_inserts_row():
    db = {"manual_seed_urls": {}}
    conn = FakeConnection(db)

    result = service.create_manual_seed_url(
        conn, "brand-storm", "https://www.stormbowling.com/storm-equinox-bowling-ball",
        note="orphaned on-site",
    )

    assert result["url"] == "https://www.stormbowling.com/storm-equinox-bowling-ball"
    assert result["brand_id"] == "brand-storm"
    assert result["note"] == "orphaned on-site"
    new_id = result["id"]
    assert db["manual_seed_urls"][new_id]["url"] == "https://www.stormbowling.com/storm-equinox-bowling-ball"
    assert conn.committed is True


def test_create_manual_seed_url_dedupes_by_exact_url():
    """Re-seeding an already-seeded URL is a harmless no-op that returns
    the EXISTING row -- mirrors the plain (not case-insensitive) unique
    constraint 027_manual_seed_urls.sql adds on url."""
    db = {"manual_seed_urls": {
        "s1": {
            "id": "s1", "brand_id": "brand-storm",
            "url": "https://www.stormbowling.com/storm-equinox-bowling-ball",
            "note": None, "created_at": "2026-09-04",
        },
    }}
    conn = FakeConnection(db)

    result = service.create_manual_seed_url(
        conn, "brand-storm", "https://www.stormbowling.com/storm-equinox-bowling-ball",
        note="second attempt",
    )

    assert result["id"] == "s1"
    assert len(db["manual_seed_urls"]) == 1
    # ON CONFLICT DO NOTHING means the second call's note is simply
    # dropped, not merged -- same as create_blocked_channel's own conflict path.
    assert db["manual_seed_urls"]["s1"]["note"] is None


def test_delete_manual_seed_url_removes_row():
    db = {"manual_seed_urls": {
        "s1": {"id": "s1", "brand_id": "brand-storm", "url": "https://www.stormbowling.com/storm-equinox-bowling-ball", "note": None, "created_at": "2026-09-04"},
    }}
    conn = FakeConnection(db)

    result = service.delete_manual_seed_url(conn, "s1")

    assert result == {"deleted": True, "id": "s1"}
    assert "s1" not in db["manual_seed_urls"]


def test_delete_manual_seed_url_missing_raises():
    db = {"manual_seed_urls": {}}
    conn = FakeConnection(db)
    try:
        service.delete_manual_seed_url(conn, "no-such-seed")
        assert False, "expected LookupError"
    except LookupError:
        pass


# --- 022_product_articles.sql: ball-review article review workflow --
# Al: "this could be the backend that pulls together all the creative and
# content for the frontend." Same list/get/approve/reject shape as
# video-candidates and price-sources above (list_articles/get_article/
# approve_article/reject_article mirror list_price_sources/get_video_
# candidate/approve_price_source/reject_price_source almost exactly --
# see those functions' own docstrings in service.py).

def _fake_article_row(**overrides):
    row = {
        "id": "art-1", "product_id": "prod-1", "product_name": "Equinox Solid", "brand_name": "Storm",
        "status": "pending", "title": "The Storm Equinox Solid: A Heavy-Oil Workhorse",
        "hook": "Picture this...", "performance_summary": "Strong midlane read.",
        "who_should_buy": ["Heavy oil bowlers"], "who_should_skip": ["Light oil bowlers"],
        "pros": ["Strong backend"], "cons": ["Not for light oil"], "buying_tips": "Drill for control.",
        "verdict": "A solid heavy-oil piece.", "faq": [{"question": "Q", "answer": "A"}],
        "comparison_table": [], "sibling_product_ids": [], "source_video_ids": ["vid-1"],
        "generated_at": "2026-08-01", "reviewed_at": None, "resolved_by": None,
        "created_at": "2026-08-01",
        # 023_product_article_images.sql -- default to "not generated yet"
        # (all null) so existing tests that don't care about images don't
        # need to know these columns exist at all.
        "action_shot_image_key": None, "action_shot_image_url": None,
        "product_shot_image_key": None, "product_shot_image_url": None,
        "images_generated_at": None,
        # 028_product_articles_bigcommerce_sync.sql -- default to "not
        # opted in, never synced" so existing tests that don't care about
        # BigCommerce sync don't need to know these columns exist.
        "sync_to_bigcommerce": False, "bigcommerce_post_id": None, "bowlerdepot_synced_at": None,
    }
    row.update(overrides)
    return row


def test_list_articles_filters_by_status_and_orders_most_recent_first():
    db = {"product_articles": {
        "art-1": _fake_article_row(id="art-1", status="pending", created_at="2026-08-01"),
        "art-2": _fake_article_row(id="art-2", status="approved", created_at="2026-08-02"),
        "art-3": _fake_article_row(id="art-3", status="pending", created_at="2026-08-03"),
    }}
    conn = FakeConnection(db)

    result = service.list_articles(conn, status="pending")

    assert [r["id"] for r in result] == ["art-3", "art-1"]  # most recent first, approved excluded


def test_list_articles_status_none_returns_every_status():
    db = {"product_articles": {
        "art-1": _fake_article_row(id="art-1", status="pending"),
        "art-2": _fake_article_row(id="art-2", status="rejected"),
    }}
    conn = FakeConnection(db)

    result = service.list_articles(conn, status=None)

    assert {r["id"] for r in result} == {"art-1", "art-2"}


def test_list_articles_product_id_filter():
    db = {"product_articles": {
        "art-1": _fake_article_row(id="art-1", product_id="prod-1"),
        "art-2": _fake_article_row(id="art-2", product_id="prod-2"),
    }}
    conn = FakeConnection(db)

    result = service.list_articles(conn, status=None, product_id="prod-2")

    assert [r["id"] for r in result] == ["art-2"]


def test_get_article_returns_full_detail():
    db = {"product_articles": {"art-1": _fake_article_row()}}
    conn = FakeConnection(db)

    result = service.get_article(conn, "art-1")

    assert result["title"] == "The Storm Equinox Solid: A Heavy-Oil Workhorse"
    assert result["faq"] == [{"question": "Q", "answer": "A"}]
    assert result["product_name"] == "Equinox Solid"
    assert result["brand_name"] == "Storm"


def test_get_article_returns_none_for_missing_id():
    db = {"product_articles": {}}
    conn = FakeConnection(db)
    assert service.get_article(conn, "does-not-exist") is None


def test_list_articles_includes_image_fields():
    """023_product_article_images.sql -- list_articles' select was
    extended to include both image URLs plus images_generated_at so the
    admin-site Articles tab can show a thumbnail/badge without opening
    each row (see that tab's own renderArticles change)."""
    db = {"product_articles": {
        "art-1": _fake_article_row(
            id="art-1",
            action_shot_image_url="https://bucket.s3.amazonaws.com/article-images/prod-1/action_shot.png",
            product_shot_image_url="https://bucket.s3.amazonaws.com/article-images/prod-1/product_shot.png",
            images_generated_at="2026-08-01",
        ),
    }}
    conn = FakeConnection(db)

    result = service.list_articles(conn, status="pending")

    assert result[0]["action_shot_image_url"] == "https://bucket.s3.amazonaws.com/article-images/prod-1/action_shot.png"
    assert result[0]["product_shot_image_url"] == "https://bucket.s3.amazonaws.com/article-images/prod-1/product_shot.png"
    assert result[0]["images_generated_at"] == "2026-08-01"


def test_get_article_includes_all_image_columns():
    """get_article's own `select pa.*` picks up whatever columns exist on
    the row with no code change needed -- this just confirms the fake's
    default row (and therefore the real migration 023 columns) actually
    round-trip through, including the null case."""
    db = {"product_articles": {"art-1": _fake_article_row(
        action_shot_image_key="article-images/prod-1/action_shot.png",
        action_shot_image_url="https://bucket.s3.amazonaws.com/article-images/prod-1/action_shot.png",
    )}}
    conn = FakeConnection(db)

    result = service.get_article(conn, "art-1")

    assert result["action_shot_image_key"] == "article-images/prod-1/action_shot.png"
    assert result["action_shot_image_url"] == "https://bucket.s3.amazonaws.com/article-images/prod-1/action_shot.png"
    # Neither image was generated for the product-shot variant in this row.
    assert result["product_shot_image_key"] is None
    assert result["product_shot_image_url"] is None


def test_approve_article_sets_status_and_resolved_by():
    db = {"product_articles": {"art-1": _fake_article_row(status="pending")}}
    conn = FakeConnection(db)

    result = service.approve_article(conn, "art-1", "al@bringyourbest.co")

    assert result == {"article_id": "art-1", "status": "approved"}
    assert db["product_articles"]["art-1"]["status"] == "approved"
    assert db["product_articles"]["art-1"]["resolved_by"] == "al@bringyourbest.co"
    assert conn.committed is True


def test_approve_article_missing_raises():
    db = {"product_articles": {}}
    conn = FakeConnection(db)
    try:
        service.approve_article(conn, "does-not-exist", "al@bringyourbest.co")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_approve_article_already_resolved_raises():
    """Same "fails closed on a bad state transition" guard as
    approve_price_source -- an already-approved/rejected row can't be
    silently re-stamped."""
    db = {"product_articles": {"art-1": _fake_article_row(status="approved")}}
    conn = FakeConnection(db)
    try:
        service.approve_article(conn, "art-1", "al@bringyourbest.co")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_reject_article_sets_status_and_resolved_by():
    db = {"product_articles": {"art-1": _fake_article_row(status="pending")}}
    conn = FakeConnection(db)

    result = service.reject_article(conn, "art-1", "al@bringyourbest.co", reason="FAQ was too generic")

    assert result == {"article_id": "art-1", "status": "rejected"}
    assert db["product_articles"]["art-1"]["status"] == "rejected"
    assert db["product_articles"]["art-1"]["resolved_by"] == "al@bringyourbest.co"


def test_reject_article_already_resolved_raises():
    db = {"product_articles": {"art-1": _fake_article_row(status="rejected")}}
    conn = FakeConnection(db)
    try:
        service.reject_article(conn, "art-1", "al@bringyourbest.co")
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- set_article_bigcommerce_sync (PATCH /articles/{id}/bigcommerce-sync,
# 028_product_articles_bigcommerce_sync.sql) -- Al: "lets add a flag to
# each article that would sync them to bigcommerce if on." Mirrors
# set_product_published's own test shape (no status-gate tests needed --
# this toggle works on an article of any status, unlike approve/reject).

def test_list_articles_includes_bigcommerce_sync_fields():
    db = {"product_articles": {
        "art-1": _fake_article_row(
            sync_to_bigcommerce=True,
            bigcommerce_post_id="789",
            bowlerdepot_synced_at="2026-09-04",
        ),
    }}
    conn = FakeConnection(db)

    result = service.list_articles(conn, status="pending")

    assert result[0]["sync_to_bigcommerce"] is True
    assert result[0]["bigcommerce_post_id"] == "789"
    assert result[0]["bowlerdepot_synced_at"] == "2026-09-04"


def test_get_article_includes_bigcommerce_sync_columns():
    """Same `select pa.*` round-trip guarantee as the image-column test
    above -- confirms migration 028's columns come through get_article
    with no code change needed there."""
    db = {"product_articles": {"art-1": _fake_article_row(sync_to_bigcommerce=True)}}
    conn = FakeConnection(db)

    result = service.get_article(conn, "art-1")

    assert result["sync_to_bigcommerce"] is True
    assert result["bigcommerce_post_id"] is None
    assert result["bowlerdepot_synced_at"] is None


def test_set_article_bigcommerce_sync_turns_flag_on():
    db = {"product_articles": {"art-1": _fake_article_row(sync_to_bigcommerce=False)}}
    conn = FakeConnection(db)

    result = service.set_article_bigcommerce_sync(conn, "art-1", True)

    assert result == {"article_id": "art-1", "sync_to_bigcommerce": True}
    assert db["product_articles"]["art-1"]["sync_to_bigcommerce"] is True
    assert conn.committed is True


def test_set_article_bigcommerce_sync_turns_flag_off():
    """Turning it off must NOT touch status, bigcommerce_post_id, or
    bowlerdepot_synced_at -- see the migration's own header comment for
    why a previously-synced post isn't implicitly taken down by this."""
    db = {"product_articles": {"art-1": _fake_article_row(
        status="approved", sync_to_bigcommerce=True,
        bigcommerce_post_id="789", bowlerdepot_synced_at="2026-09-04",
    )}}
    conn = FakeConnection(db)

    result = service.set_article_bigcommerce_sync(conn, "art-1", False)

    assert result == {"article_id": "art-1", "sync_to_bigcommerce": False}
    row = db["product_articles"]["art-1"]
    assert row["sync_to_bigcommerce"] is False
    assert row["status"] == "approved"
    assert row["bigcommerce_post_id"] == "789"
    assert row["bowlerdepot_synced_at"] == "2026-09-04"


def test_set_article_bigcommerce_sync_works_on_pending_article():
    """Unlike approve_article/reject_article, this toggle has no status
    gate at all -- an admin can opt a still-pending article in before it's
    even reviewed."""
    db = {"product_articles": {"art-1": _fake_article_row(status="pending", sync_to_bigcommerce=False)}}
    conn = FakeConnection(db)

    result = service.set_article_bigcommerce_sync(conn, "art-1", True)

    assert result == {"article_id": "art-1", "sync_to_bigcommerce": True}
    assert db["product_articles"]["art-1"]["status"] == "pending"


def test_set_article_bigcommerce_sync_missing_raises():
    db = {"product_articles": {}}
    conn = FakeConnection(db)
    try:
        service.set_article_bigcommerce_sync(conn, "does-not-exist", True)
        assert False, "expected LookupError"
    except LookupError:
        pass


# --- queue_article_sync (POST /articles/{id}/sync-to-bigcommerce) -- Al
# flipped sync_to_bigcommerce on for a real article via curl, got a clean
# 200, and reported "i don't see it in bigcommerce": correct, since the
# flag alone does nothing but set a column. This on-demand trigger invokes
# BowlerdepotArticleSyncFunction directly instead of making Al wait for
# its hourly schedule. Same direct-lambda-invoke-no-queue shape as
# queue_article_generation immediately below, but scoped to an article_id
# (not a product_id) and with no status/flag gate of its own -- see
# service.queue_article_sync's own docstring.

def test_queue_article_sync_invokes_function_with_article_id():
    db = {"product_articles": {"art-1": _fake_article_row(id="art-1")}}
    conn = FakeConnection(db)
    fake_lambda = _FakeLambdaClient()

    class _FakeBoto3:
        def client(self, name):
            assert name == "lambda"
            return fake_lambda

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["ARTICLE_SYNC_FUNCTION_NAME"] = "bowling-scraper-bowlerdepot-article-sync"
    try:
        result = service.queue_article_sync(conn, "art-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["ARTICLE_SYNC_FUNCTION_NAME"]

    assert result == {"queued": True, "article_id": "art-1"}
    assert len(fake_lambda.invocations) == 1
    call = fake_lambda.invocations[0]
    assert call["FunctionName"] == "bowling-scraper-bowlerdepot-article-sync"
    assert call["InvocationType"] == "Event"
    assert json.loads(call["Payload"]) == {"article_id": "art-1"}


def test_queue_article_sync_missing_article_raises():
    db = {"product_articles": {}}
    conn = FakeConnection(db)
    try:
        service.queue_article_sync(conn, "does-not-exist")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_queue_article_sync_missing_function_name_returns_not_queued():
    db = {"product_articles": {"art-1": _fake_article_row(id="art-1")}}
    conn = FakeConnection(db)
    os.environ.pop("ARTICLE_SYNC_FUNCTION_NAME", None)

    class _ExplodingBoto3:
        def client(self, name):
            raise AssertionError("should never be called when the function name isn't configured")

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _ExplodingBoto3()
    try:
        result = service.queue_article_sync(conn, "art-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]

    assert result == {"queued": False, "reason": "ARTICLE_SYNC_FUNCTION_NAME is not configured on this deployment"}


# --- queue_article_resync (POST /articles/{id}/resync-to-bigcommerce) --
# Added right after the WebDAV Digest-auth fix made thumbnail_path
# actually work: an article synced before that fix has no thumbnail, and
# queue_article_sync's own invokee-side query (list_articles_needing_
# sync) explicitly excludes anything already synced, so re-flagging does
# nothing. This invokes the SAME BowlerdepotArticleSyncFunction (same
# ARTICLE_SYNC_FUNCTION_NAME env var, no new IAM/template wiring needed)
# but with resync: true in the payload, which routes bowlerdepot_
# article_sync/app.py's handler to its update (PUT), not create (POST),
# branch -- see service.queue_article_resync's own docstring.

def test_queue_article_resync_invokes_function_with_resync_flag():
    db = {"product_articles": {"art-1": _fake_article_row(id="art-1")}}
    conn = FakeConnection(db)
    fake_lambda = _FakeLambdaClient()

    class _FakeBoto3:
        def client(self, name):
            assert name == "lambda"
            return fake_lambda

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["ARTICLE_SYNC_FUNCTION_NAME"] = "bowling-scraper-bowlerdepot-article-sync"
    try:
        result = service.queue_article_resync(conn, "art-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["ARTICLE_SYNC_FUNCTION_NAME"]

    assert result == {"queued": True, "article_id": "art-1", "resync": True}
    assert len(fake_lambda.invocations) == 1
    call = fake_lambda.invocations[0]
    assert call["FunctionName"] == "bowling-scraper-bowlerdepot-article-sync"
    assert call["InvocationType"] == "Event"
    assert json.loads(call["Payload"]) == {"article_id": "art-1", "resync": True}


def test_queue_article_resync_missing_article_raises():
    db = {"product_articles": {}}
    conn = FakeConnection(db)
    try:
        service.queue_article_resync(conn, "does-not-exist")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_queue_article_resync_missing_function_name_returns_not_queued():
    db = {"product_articles": {"art-1": _fake_article_row(id="art-1")}}
    conn = FakeConnection(db)
    os.environ.pop("ARTICLE_SYNC_FUNCTION_NAME", None)

    class _ExplodingBoto3:
        def client(self, name):
            raise AssertionError("should never be called when the function name isn't configured")

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _ExplodingBoto3()
    try:
        result = service.queue_article_resync(conn, "art-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]

    assert result == {"queued": False, "reason": "ARTICLE_SYNC_FUNCTION_NAME is not configured on this deployment"}


# --- queue_article_generation (POST /products/{id}/generate-article) --
# Same direct-lambda-invoke-no-queue shape as queue_video_discovery above,
# but the payload key is `product_id` (singular), not `product_ids` -- see
# service.queue_article_generation's own docstring for why that distinction
# actually matters (product_article_generator's on-demand handler path
# checks event.get("product_id") specifically).

def test_queue_article_generation_invokes_function_with_singular_product_id():
    db = _fake_db_with_product()
    conn = FakeConnection(db)
    fake_lambda = _FakeLambdaClient()

    class _FakeBoto3:
        def client(self, name):
            assert name == "lambda"
            return fake_lambda

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["PRODUCT_ARTICLE_GENERATOR_FUNCTION_NAME"] = "bowling-scraper-product-article-generator"
    try:
        result = service.queue_article_generation(conn, "prod-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["PRODUCT_ARTICLE_GENERATOR_FUNCTION_NAME"]

    # v7: mode defaults to "both" and is echoed back in the result, but the
    # Lambda payload itself is unchanged from before v7 existed -- no
    # regenerate_text/regenerate_images keys at all for the default mode
    # (see queue_article_generation's own v7 docstring).
    assert result == {"queued": True, "product_id": "prod-1", "mode": "both"}
    assert len(fake_lambda.invocations) == 1
    call = fake_lambda.invocations[0]
    assert call["FunctionName"] == "bowling-scraper-product-article-generator"
    assert call["InvocationType"] == "Event"
    # Singular "product_id", NOT a "product_ids" list -- see this test
    # section's own comment for why that distinction matters here.
    assert json.loads(call["Payload"]) == {"product_id": "prod-1"}


def test_queue_article_generation_mode_text_sets_regenerate_flags_in_payload():
    db = _fake_db_with_product()
    conn = FakeConnection(db)
    fake_lambda = _FakeLambdaClient()

    class _FakeBoto3:
        def client(self, name):
            return fake_lambda

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["PRODUCT_ARTICLE_GENERATOR_FUNCTION_NAME"] = "bowling-scraper-product-article-generator"
    try:
        result = service.queue_article_generation(conn, "prod-1", mode="text")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["PRODUCT_ARTICLE_GENERATOR_FUNCTION_NAME"]

    assert result == {"queued": True, "product_id": "prod-1", "mode": "text"}
    call = fake_lambda.invocations[0]
    assert json.loads(call["Payload"]) == {
        "product_id": "prod-1", "regenerate_text": True, "regenerate_images": False,
    }


def test_queue_article_generation_mode_images_sets_regenerate_flags_in_payload():
    db = _fake_db_with_product()
    conn = FakeConnection(db)
    fake_lambda = _FakeLambdaClient()

    class _FakeBoto3:
        def client(self, name):
            return fake_lambda

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["PRODUCT_ARTICLE_GENERATOR_FUNCTION_NAME"] = "bowling-scraper-product-article-generator"
    try:
        result = service.queue_article_generation(conn, "prod-1", mode="images")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["PRODUCT_ARTICLE_GENERATOR_FUNCTION_NAME"]

    assert result == {"queued": True, "product_id": "prod-1", "mode": "images"}
    call = fake_lambda.invocations[0]
    assert json.loads(call["Payload"]) == {
        "product_id": "prod-1", "regenerate_text": False, "regenerate_images": True,
    }


def test_queue_article_generation_rejects_unknown_mode():
    db = _fake_db_with_product()
    conn = FakeConnection(db)
    try:
        service.queue_article_generation(conn, "prod-1", mode="bogus")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_queue_article_generation_missing_product_raises():
    db = _fake_db_with_product()
    conn = FakeConnection(db)
    try:
        service.queue_article_generation(conn, "does-not-exist")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_queue_article_generation_missing_function_name_returns_not_queued():
    db = _fake_db_with_product()
    conn = FakeConnection(db)
    os.environ.pop("PRODUCT_ARTICLE_GENERATOR_FUNCTION_NAME", None)

    class _ExplodingBoto3:
        def client(self, name):
            raise AssertionError("should never be called when the function name isn't configured")

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _ExplodingBoto3()
    try:
        result = service.queue_article_generation(conn, "prod-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]

    assert result == {"queued": False, "reason": "PRODUCT_ARTICLE_GENERATOR_FUNCTION_NAME is not configured on this deployment"}


def _fake_db_with_price_source():
    db = _fake_db_with_price_site()
    db["products"] = {"prod-1": {"id": "prod-1"}}
    db["product_price_sources"] = {
        "src-1": {
            "id": "src-1", "product_id": "prod-1", "price_site_id": "site-1",
            "product_url": "https://bowlerdepot.com/p/fury", "css_selector": None, "is_active": True,
            "status": "pending", "source": "site_search", "match_query": "Brunswick Fury",
            "match_confidence": "high", "resolved_by": None,
        },
    }
    return db


# --- create_product_price_source: manual-override path only -- Al:
# "admin can fix mismatches manually after the fact if a match is
# wrong." Always lands as status='approved', source='manual' -- there's
# no candidate to review here, an admin supplied the exact URL directly.

def test_create_product_price_source_inserts_approved_manual_row():
    db = _fake_db_with_price_source()
    conn = FakeConnection(db)

    result = service.create_product_price_source(conn, "prod-1", "site-1", "https://bowlerdepot.com/p/fury2")

    new_id = result["id"]
    assert db["product_price_sources"][new_id]["product_id"] == "prod-1"
    assert db["product_price_sources"][new_id]["price_site_id"] == "site-1"
    assert db["product_price_sources"][new_id]["product_url"] == "https://bowlerdepot.com/p/fury2"
    assert db["product_price_sources"][new_id]["status"] == "approved"
    assert db["product_price_sources"][new_id]["source"] == "manual"
    assert result["status"] == "approved"
    assert result["source"] == "manual"


def test_create_product_price_source_records_resolved_by():
    db = _fake_db_with_price_source()
    conn = FakeConnection(db)

    result = service.create_product_price_source(
        conn, "prod-1", "site-1", "https://bowlerdepot.com/p/fury2", resolved_by="al@bringyourbest.co",
    )

    new_id = result["id"]
    assert db["product_price_sources"][new_id]["resolved_by"] == "al@bringyourbest.co"


def test_create_product_price_source_records_external_product_id():
    # 016_price_tracking_bigcommerce.sql -- a manual override against an
    # 'api'-fetch_method site (e.g. attaching a BowlerDepot product id
    # discovery missed) can carry the platform's own native id.
    db = _fake_db_with_price_source()
    conn = FakeConnection(db)

    result = service.create_product_price_source(
        conn, "prod-1", "site-1", "https://www.bowlerdepot.com/storm-alpha-crux/",
        external_product_id="100",
    )

    new_id = result["id"]
    assert db["product_price_sources"][new_id]["external_product_id"] == "100"
    assert result["external_product_id"] == "100"


def test_create_product_price_source_external_product_id_defaults_to_none():
    db = _fake_db_with_price_source()
    conn = FakeConnection(db)

    result = service.create_product_price_source(conn, "prod-1", "site-1", "https://bowlerdepot.com/p/fury2")

    new_id = result["id"]
    assert db["product_price_sources"][new_id]["external_product_id"] is None
    assert result["external_product_id"] is None


def test_create_product_price_source_missing_product_raises():
    db = _fake_db_with_price_source()
    conn = FakeConnection(db)
    try:
        service.create_product_price_source(conn, "no-such-product", "site-1", "https://example.com/p")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_create_product_price_source_missing_site_raises():
    db = _fake_db_with_price_source()
    conn = FakeConnection(db)
    try:
        service.create_product_price_source(conn, "prod-1", "no-such-site", "https://example.com/p")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_update_product_price_source_partial_update():
    db = _fake_db_with_price_source()
    conn = FakeConnection(db)

    service.update_product_price_source(conn, "src-1", css_selector=".override")

    assert db["product_price_sources"]["src-1"]["css_selector"] == ".override"
    assert db["product_price_sources"]["src-1"]["product_url"] == "https://bowlerdepot.com/p/fury"  # untouched


def test_update_product_price_source_can_deactivate():
    db = _fake_db_with_price_source()
    conn = FakeConnection(db)

    service.update_product_price_source(conn, "src-1", is_active=False)

    assert db["product_price_sources"]["src-1"]["is_active"] is False


def test_update_product_price_source_missing_raises():
    db = _fake_db_with_price_source()
    conn = FakeConnection(db)
    try:
        service.update_product_price_source(conn, "no-such-source", product_url="https://x.example")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_delete_product_price_source_removes_row():
    db = _fake_db_with_price_source()
    conn = FakeConnection(db)

    result = service.delete_product_price_source(conn, "src-1")

    assert result == {"deleted": True, "id": "src-1"}
    assert "src-1" not in db["product_price_sources"]


def test_delete_product_price_source_missing_raises():
    db = _fake_db_with_price_source()
    conn = FakeConnection(db)
    try:
        service.delete_product_price_source(conn, "no-such-source")
        assert False, "expected LookupError"
    except LookupError:
        pass


# --- approve_price_source / reject_price_source / restore_price_source:
# the actual review workflow Al's "the reccomended path is best" locked
# in -- same one-way pending->approved/rejected guard, and the same
# undo-back-to-pending escape hatch, as approve/reject/restore_video_
# candidate, but built in from the start here (see this section's own
# header comment for why that matters).

def test_approve_price_source_marks_approved():
    db = _fake_db_with_price_source()
    conn = FakeConnection(db)

    result = service.approve_price_source(conn, "src-1", resolved_by="al@bringyourbest.co")

    assert result == {"source_id": "src-1", "status": "approved"}
    assert db["product_price_sources"]["src-1"]["status"] == "approved"
    assert db["product_price_sources"]["src-1"]["resolved_by"] == "al@bringyourbest.co"
    assert conn.committed is True


def test_approve_price_source_already_resolved_raises():
    db = _fake_db_with_price_source()
    db["product_price_sources"]["src-1"]["status"] = "approved"
    conn = FakeConnection(db)
    try:
        service.approve_price_source(conn, "src-1", resolved_by="al@bringyourbest.co")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_approve_price_source_missing_raises():
    db = _fake_db_with_price_source()
    conn = FakeConnection(db)
    try:
        service.approve_price_source(conn, "does-not-exist", resolved_by="al@bringyourbest.co")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_reject_price_source_marks_rejected():
    db = _fake_db_with_price_source()
    conn = FakeConnection(db)

    result = service.reject_price_source(conn, "src-1", resolved_by="al@bringyourbest.co")

    assert result == {"source_id": "src-1", "status": "rejected"}
    assert db["product_price_sources"]["src-1"]["status"] == "rejected"


def test_reject_price_source_already_resolved_raises():
    db = _fake_db_with_price_source()
    db["product_price_sources"]["src-1"]["status"] = "rejected"
    conn = FakeConnection(db)
    try:
        service.reject_price_source(conn, "src-1", resolved_by="al@bringyourbest.co")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_restore_price_source_from_rejected_marks_pending_and_clears_resolution():
    db = _fake_db_with_price_source()
    db["product_price_sources"]["src-1"]["status"] = "rejected"
    db["product_price_sources"]["src-1"]["resolved_by"] = "al@bringyourbest.co"
    db["product_price_sources"]["src-1"]["resolved_at"] = "2026-08-01T00:00:00Z"
    conn = FakeConnection(db)

    result = service.restore_price_source(conn, "src-1")

    assert result == {"source_id": "src-1", "status": "pending"}
    row = db["product_price_sources"]["src-1"]
    assert row["status"] == "pending"
    assert row["resolved_by"] is None
    assert row["resolved_at"] is None


def test_restore_price_source_from_approved_marks_pending():
    db = _fake_db_with_price_source()
    db["product_price_sources"]["src-1"]["status"] = "approved"
    conn = FakeConnection(db)

    result = service.restore_price_source(conn, "src-1")

    assert result["status"] == "pending"
    assert db["product_price_sources"]["src-1"]["status"] == "pending"


def test_restore_price_source_from_pending_raises():
    # Not a silent no-op -- usually means stale UI state, worth
    # surfacing, same as restore_video_candidate.
    db = _fake_db_with_price_source()  # status defaults to 'pending'
    conn = FakeConnection(db)
    try:
        service.restore_price_source(conn, "src-1")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_restore_price_source_missing_raises():
    db = _fake_db_with_price_source()
    conn = FakeConnection(db)
    try:
        service.restore_price_source(conn, "does-not-exist")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_get_pending_price_source_count_counts_only_pending():
    db = _fake_db_with_price_source()
    db["product_price_sources"]["src-2"] = dict(db["product_price_sources"]["src-1"])
    db["product_price_sources"]["src-2"]["id"] = "src-2"
    db["product_price_sources"]["src-2"]["status"] = "approved"
    conn = FakeConnection(db)

    assert service.get_pending_price_source_count(conn) == 1


# --- dedupe_product_price_sources: cleanup for the real duplicate-row bug
# (Al: "there are duplicates now, the ones before having the baseurl and
# now the ones that have it... same record just has different link").

def _fake_db_with_duplicate_price_sources():
    db = _fake_db_with_price_site()
    db["products"] = {"prod-1": {"id": "prod-1"}}
    db["product_price_sources"] = {
        "src-old": {
            "id": "src-old", "product_id": "prod-1", "price_site_id": "site-1",
            "product_url": "/storm-alpha-crux/", "status": "approved", "is_active": True,
            "source": "bigcommerce_api", "created_at": "2026-07-01",
        },
        "src-new": {
            "id": "src-new", "product_id": "prod-1", "price_site_id": "site-1",
            "product_url": "https://www.bowlerdepot.com/storm-alpha-crux/", "status": "pending",
            "is_active": True, "source": "bigcommerce_api", "created_at": "2026-08-01",
        },
    }
    db["product_price_history"] = [
        {"id": "h-1", "price_source_id": "src-old", "price": 129.99},
        {"id": "h-2", "price_source_id": "src-old", "price": 124.99},
    ]
    db["product_sku_stock_history"] = [
        {"id": "s-1", "price_source_id": "src-old", "quantity": 3},
    ]
    return db


def test_dedupe_keeps_approved_active_row_as_survivor():
    db = _fake_db_with_duplicate_price_sources()
    conn = FakeConnection(db)
    result = service.dedupe_product_price_sources(conn)

    assert result == {"groups_merged": 1, "rows_deleted": 1}
    assert "src-old" in db["product_price_sources"]
    assert "src-new" not in db["product_price_sources"]


def test_dedupe_migrates_price_and_sku_stock_history_onto_survivor():
    db = _fake_db_with_duplicate_price_sources()
    conn = FakeConnection(db)
    service.dedupe_product_price_sources(conn)

    # The redundant row (src-new) never actually had history in this
    # fixture, but the survivor (src-old) did -- confirming nothing was
    # lost and the migration logic runs even when there's nothing to move
    # for THIS particular group's non-survivor.
    assert [h["price_source_id"] for h in db["product_price_history"]] == ["src-old", "src-old"]
    assert [h["price_source_id"] for h in db["product_sku_stock_history"]] == ["src-old"]


def test_dedupe_corrects_survivor_url_to_absolute_variant():
    # The actual bug: the approved survivor (src-old) is the one stuck
    # with the stale relative URL; the discarded duplicate (src-new) is
    # the one with the correct absolute link. The survivor's product_url
    # must end up corrected, not left relative.
    db = _fake_db_with_duplicate_price_sources()
    conn = FakeConnection(db)
    service.dedupe_product_price_sources(conn)

    assert db["product_price_sources"]["src-old"]["product_url"] == "https://www.bowlerdepot.com/storm-alpha-crux/"


def test_dedupe_migrates_history_when_the_non_survivor_has_it_instead():
    # Flip which row carries history -- src-old is pending (loses the
    # approved/active tiebreak) and src-new is the approved+active
    # survivor. History attached to the non-survivor (src-old) must still
    # move onto whichever row actually wins.
    db = _fake_db_with_price_site()
    db["products"] = {"prod-1": {"id": "prod-1"}}
    db["product_price_sources"] = {
        "src-old": {
            "id": "src-old", "product_id": "prod-1", "price_site_id": "site-1",
            "product_url": "/storm-alpha-crux/", "status": "pending", "is_active": True,
            "source": "bigcommerce_api", "created_at": "2026-07-01",
        },
        "src-new": {
            "id": "src-new", "product_id": "prod-1", "price_site_id": "site-1",
            "product_url": "https://www.bowlerdepot.com/storm-alpha-crux/", "status": "approved",
            "is_active": True, "source": "bigcommerce_api", "created_at": "2026-08-01",
        },
    }
    db["product_price_history"] = [{"id": "h-1", "price_source_id": "src-old", "price": 129.99}]
    db["product_sku_stock_history"] = []
    conn = FakeConnection(db)

    result = service.dedupe_product_price_sources(conn)

    assert result == {"groups_merged": 1, "rows_deleted": 1}
    assert "src-new" in db["product_price_sources"]
    assert "src-old" not in db["product_price_sources"]
    assert db["product_price_history"][0]["price_source_id"] == "src-new"


def test_dedupe_no_duplicates_is_a_noop():
    db = _fake_db_with_price_source()  # single row, single (product_id, price_site_id)
    db["product_price_history"] = []
    db["product_sku_stock_history"] = []
    conn = FakeConnection(db)

    result = service.dedupe_product_price_sources(conn)

    assert result == {"groups_merged": 0, "rows_deleted": 0}
    assert list(db["product_price_sources"].keys()) == ["src-1"]


def test_dedupe_leaves_distinct_products_and_sites_alone():
    # Rows for different products, or the same product against different
    # sites, are never duplicates of each other -- only an exact
    # (product_id, price_site_id) match counts.
    db = _fake_db_with_price_site()
    db["products"] = {"prod-1": {"id": "prod-1"}, "prod-2": {"id": "prod-2"}}
    db["product_price_sources"] = {
        "src-1": {"id": "src-1", "product_id": "prod-1", "price_site_id": "site-1",
                  "product_url": "https://a.example/p1", "status": "approved", "is_active": True,
                  "source": "bigcommerce_api", "created_at": "2026-07-01"},
        "src-2": {"id": "src-2", "product_id": "prod-2", "price_site_id": "site-1",
                  "product_url": "https://a.example/p2", "status": "approved", "is_active": True,
                  "source": "bigcommerce_api", "created_at": "2026-07-01"},
    }
    db["product_price_history"] = []
    db["product_sku_stock_history"] = []
    conn = FakeConnection(db)

    result = service.dedupe_product_price_sources(conn)

    assert result == {"groups_merged": 0, "rows_deleted": 0}
    assert set(db["product_price_sources"].keys()) == {"src-1", "src-2"}


# --- queue_price_check / queue_price_check_batch: same invoke-Lambda-
# directly, fire-and-forget shape as queue_video_discovery/queue_video_
# stats_refresh above (see those tests for the pattern this mirrors).

def test_queue_price_check_invokes_function_with_product_ids_scope():
    db = _fake_db_with_product()
    conn = FakeConnection(db)
    fake_lambda = _FakeLambdaClient()

    class _FakeBoto3:
        def client(self, name):
            assert name == "lambda"
            return fake_lambda

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["PRICE_CHECKER_FUNCTION_NAME"] = "bowling-scraper-price-checker"
    try:
        result = service.queue_price_check(conn, "prod-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["PRICE_CHECKER_FUNCTION_NAME"]

    assert result == {"queued": True, "product_id": "prod-1"}
    assert len(fake_lambda.invocations) == 1
    call = fake_lambda.invocations[0]
    assert call["FunctionName"] == "bowling-scraper-price-checker"
    assert call["InvocationType"] == "Event"
    assert json.loads(call["Payload"]) == {"product_ids": ["prod-1"]}


def test_queue_price_check_missing_product_raises():
    db = _fake_db_with_product()
    conn = FakeConnection(db)
    try:
        service.queue_price_check(conn, "does-not-exist")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_queue_price_check_missing_function_name_returns_not_queued():
    db = _fake_db_with_product()
    conn = FakeConnection(db)
    os.environ.pop("PRICE_CHECKER_FUNCTION_NAME", None)

    class _ExplodingBoto3:
        def client(self, name):
            raise AssertionError("should never be called when the function name isn't configured")

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _ExplodingBoto3()
    try:
        result = service.queue_price_check(conn, "prod-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]

    assert result == {"queued": False, "reason": "PRICE_CHECKER_FUNCTION_NAME is not configured on this deployment"}


def test_queue_price_check_batch_invokes_function_with_limit():
    fake_lambda = _FakeLambdaClient()

    class _FakeBoto3:
        def client(self, name):
            assert name == "lambda"
            return fake_lambda

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["PRICE_CHECKER_FUNCTION_NAME"] = "bowling-scraper-price-checker"
    try:
        result = service.queue_price_check_batch(limit=50)
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["PRICE_CHECKER_FUNCTION_NAME"]

    assert result == {"queued": True, "limit": 50}
    assert len(fake_lambda.invocations) == 1
    call = fake_lambda.invocations[0]
    assert call["InvocationType"] == "Event"
    assert json.loads(call["Payload"]) == {"limit": 50}


# --- queue_price_discovery / queue_price_discovery_batch: same invoke-
# Lambda-directly, fire-and-forget shape as queue_price_check/queue_price_
# check_batch immediately above, just with a {"discover": true, ...}
# payload instead -- see service.queue_price_discovery's docstring.

def test_queue_price_discovery_invokes_function_with_discover_and_product_ids():
    db = _fake_db_with_product()
    conn = FakeConnection(db)
    fake_lambda = _FakeLambdaClient()

    class _FakeBoto3:
        def client(self, name):
            assert name == "lambda"
            return fake_lambda

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["PRICE_CHECKER_FUNCTION_NAME"] = "bowling-scraper-price-checker"
    try:
        result = service.queue_price_discovery(conn, "prod-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["PRICE_CHECKER_FUNCTION_NAME"]

    assert result == {"queued": True, "product_id": "prod-1"}
    call = fake_lambda.invocations[0]
    assert call["FunctionName"] == "bowling-scraper-price-checker"
    assert call["InvocationType"] == "Event"
    assert json.loads(call["Payload"]) == {"discover": True, "product_ids": ["prod-1"]}


def test_queue_price_discovery_missing_product_raises():
    db = _fake_db_with_product()
    conn = FakeConnection(db)
    try:
        service.queue_price_discovery(conn, "does-not-exist")
        assert False, "expected LookupError"
    except LookupError:
        pass


def test_queue_price_discovery_missing_function_name_returns_not_queued():
    db = _fake_db_with_product()
    conn = FakeConnection(db)
    os.environ.pop("PRICE_CHECKER_FUNCTION_NAME", None)

    class _ExplodingBoto3:
        def client(self, name):
            raise AssertionError("should never be called when the function name isn't configured")

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _ExplodingBoto3()
    try:
        result = service.queue_price_discovery(conn, "prod-1")
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]

    assert result == {"queued": False, "reason": "PRICE_CHECKER_FUNCTION_NAME is not configured on this deployment"}


def test_queue_price_discovery_batch_invokes_function_with_discover_and_limit():
    fake_lambda = _FakeLambdaClient()

    class _FakeBoto3:
        def client(self, name):
            assert name == "lambda"
            return fake_lambda

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["PRICE_CHECKER_FUNCTION_NAME"] = "bowling-scraper-price-checker"
    try:
        result = service.queue_price_discovery_batch(limit=25)
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["PRICE_CHECKER_FUNCTION_NAME"]

    assert result == {"queued": True, "limit": 25, "scrape_only": False}
    call = fake_lambda.invocations[0]
    assert call["InvocationType"] == "Event"
    assert json.loads(call["Payload"]) == {"discover": True, "limit": 25}


def test_queue_price_discovery_batch_no_limit_omits_it_from_payload():
    fake_lambda = _FakeLambdaClient()

    class _FakeBoto3:
        def client(self, name):
            assert name == "lambda"
            return fake_lambda

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["PRICE_CHECKER_FUNCTION_NAME"] = "bowling-scraper-price-checker"
    try:
        service.queue_price_discovery_batch()
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["PRICE_CHECKER_FUNCTION_NAME"]

    call = fake_lambda.invocations[0]
    assert json.loads(call["Payload"]) == {"discover": True}


# --- queue_price_discovery_batch: scrape_only, real request, Al: "can we
# not run the bowlerdepot price sources in this one, they have inventory
# numbers too" -> "maybe just scrape sources" ---

def test_queue_price_discovery_batch_scrape_only_included_in_payload():
    fake_lambda = _FakeLambdaClient()

    class _FakeBoto3:
        def client(self, name):
            assert name == "lambda"
            return fake_lambda

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["PRICE_CHECKER_FUNCTION_NAME"] = "bowling-scraper-price-checker"
    try:
        result = service.queue_price_discovery_batch(scrape_only=True)
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["PRICE_CHECKER_FUNCTION_NAME"]

    assert result == {"queued": True, "limit": None, "scrape_only": True}
    call = fake_lambda.invocations[0]
    assert json.loads(call["Payload"]) == {"discover": True, "scrape_only": True}


def test_queue_price_discovery_batch_scrape_only_false_omits_it_from_payload():
    fake_lambda = _FakeLambdaClient()

    class _FakeBoto3:
        def client(self, name):
            assert name == "lambda"
            return fake_lambda

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    os.environ["PRICE_CHECKER_FUNCTION_NAME"] = "bowling-scraper-price-checker"
    try:
        service.queue_price_discovery_batch(limit=10, scrape_only=False)
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        del os.environ["PRICE_CHECKER_FUNCTION_NAME"]

    call = fake_lambda.invocations[0]
    # Default behavior unchanged -- omitted entirely, not sent as false,
    # so an already-deployed price_checker without the scrape_only branch
    # still gets exactly the payload shape it always has.
    assert json.loads(call["Payload"]) == {"discover": True, "limit": 10}


# --- User management (Cognito) tests ---
#
# See service.py's own "User management (Cognito)" section header
# comment for the full design. _FakeCognitoClient is a hand-built fake
# of exactly the cognito-idp Admin*/List* calls those functions use --
# passed in directly as each function's own cognito_client argument
# (not a sys.modules["boto3"] swap like the other AWS-touching tests in
# this file need), since service.py's user-management functions take an
# already-built client as a parameter rather than importing boto3
# internally -- see that section's own header comment for why.
class _FakeCognitoClient:
    def __init__(self, users=None, groups=None):
        # users: username -> {"email", "enabled", "status", "created_at"}
        self.users = users or {}
        # groups: group name -> set of usernames
        self.groups = groups if groups is not None else {"Admins": set(), "Editors": set()}
        self.calls = []

    def list_users(self, UserPoolId, PaginationToken=None):
        self.calls.append(("list_users", PaginationToken))
        items = [
            {
                "Username": username,
                "UserStatus": data.get("status", "CONFIRMED"),
                "Enabled": data.get("enabled", True),
                "UserCreateDate": data.get("created_at", "2026-01-01"),
                "Attributes": [{"Name": "email", "Value": data.get("email", username)}],
            }
            for username, data in self.users.items()
        ]
        return {"Users": items}

    def list_users_in_group(self, UserPoolId, GroupName, NextToken=None):
        self.calls.append(("list_users_in_group", GroupName))
        return {"Users": [{"Username": u} for u in self.groups.get(GroupName, set())]}

    def admin_create_user(self, UserPoolId, Username, UserAttributes, MessageAction, TemporaryPassword):
        self.calls.append(("admin_create_user", Username, MessageAction))
        email = next(a["Value"] for a in UserAttributes if a["Name"] == "email")
        self.users[Username] = {"email": email, "enabled": True, "status": "FORCE_CHANGE_PASSWORD"}

    def admin_set_user_password(self, UserPoolId, Username, Password, Permanent):
        self.calls.append(("admin_set_user_password", Username, Permanent))
        if Permanent:
            self.users[Username]["status"] = "CONFIRMED"

    def admin_add_user_to_group(self, UserPoolId, Username, GroupName):
        self.calls.append(("admin_add_user_to_group", Username, GroupName))
        self.groups.setdefault(GroupName, set()).add(Username)

    def admin_remove_user_from_group(self, UserPoolId, Username, GroupName):
        self.calls.append(("admin_remove_user_from_group", Username, GroupName))
        self.groups.get(GroupName, set()).discard(Username)

    def admin_list_groups_for_user(self, UserPoolId, Username):
        self.calls.append(("admin_list_groups_for_user", Username))
        member_of = [g for g, members in self.groups.items() if Username in members]
        return {"Groups": [{"GroupName": g} for g in member_of]}

    def admin_enable_user(self, UserPoolId, Username):
        self.calls.append(("admin_enable_user", Username))
        self.users[Username]["enabled"] = True

    def admin_disable_user(self, UserPoolId, Username):
        self.calls.append(("admin_disable_user", Username))
        self.users[Username]["enabled"] = False

    def admin_delete_user(self, UserPoolId, Username):
        self.calls.append(("admin_delete_user", Username))
        del self.users[Username]
        for members in self.groups.values():
            members.discard(Username)


def test_require_admin_role_allows_admin():
    service.require_admin_role({"role": "admin"})  # does not raise


def test_require_admin_role_rejects_editor_and_missing_role():
    for caller in [{"role": "editor"}, {}, {"role": None}]:
        try:
            service.require_admin_role(caller)
            assert False, f"expected PermissionError for {caller!r}"
        except PermissionError:
            pass


def test_require_user_pool_id_reads_env_var():
    os.environ["COGNITO_USER_POOL_ID"] = "us-west-1_abc123"
    try:
        assert service.require_user_pool_id() == "us-west-1_abc123"
    finally:
        del os.environ["COGNITO_USER_POOL_ID"]


def test_require_user_pool_id_raises_when_unset():
    os.environ.pop("COGNITO_USER_POOL_ID", None)
    try:
        service.require_user_pool_id()
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


def test_list_users_annotates_group_membership_and_sorts_by_email():
    client = _FakeCognitoClient(
        users={
            "zed@example.com": {"email": "zed@example.com"},
            "al@example.com": {"email": "al@example.com"},
            "unassigned@example.com": {"email": "unassigned@example.com"},
        },
        groups={"Admins": {"al@example.com"}, "Editors": {"zed@example.com"}},
    )

    result = service.list_users(client, "pool-1")

    assert [u["email"] for u in result] == ["al@example.com", "unassigned@example.com", "zed@example.com"]
    by_email = {u["email"]: u for u in result}
    assert by_email["al@example.com"]["group"] == "Admins"
    assert by_email["zed@example.com"]["group"] == "Editors"
    # An account that exists but was never added to either group must
    # come back with group=None, not silently defaulted to something
    # permissive -- see resolve_role_from_groups' own docstring on why
    # that's a real, valid "no access" state, not an oversight.
    assert by_email["unassigned@example.com"]["group"] is None


def test_create_user_rejects_unknown_group():
    client = _FakeCognitoClient()
    try:
        service.create_user(client, "pool-1", "new@example.com", "SuperAdmins")
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert client.calls == []  # rejected before any Cognito call was made


def test_create_user_sets_permanent_password_and_adds_to_group():
    client = _FakeCognitoClient()

    result = service.create_user(client, "pool-1", "new@example.com", "Editors")

    assert result["email"] == "new@example.com"
    assert result["group"] == "Editors"
    assert len(result["password"]) >= 16
    assert "new@example.com" in client.groups["Editors"]
    assert client.users["new@example.com"]["status"] == "CONFIRMED"  # not FORCE_CHANGE_PASSWORD
    call_names = [c[0] for c in client.calls]
    assert call_names == ["admin_create_user", "admin_set_user_password", "admin_add_user_to_group"]


def test_set_user_group_moves_user_between_groups():
    client = _FakeCognitoClient(
        users={"editor@example.com": {"email": "editor@example.com"}},
        groups={"Admins": {"admin@example.com"}, "Editors": {"editor@example.com"}},
    )

    result = service.set_user_group(client, "pool-1", "editor@example.com", "Admins")

    assert result == {"username": "editor@example.com", "group": "Admins"}
    assert "editor@example.com" not in client.groups["Editors"]
    assert "editor@example.com" in client.groups["Admins"]


def test_set_user_group_rejects_unknown_group():
    client = _FakeCognitoClient()
    try:
        service.set_user_group(client, "pool-1", "someone@example.com", "SuperAdmins")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_set_user_group_blocks_removing_the_last_admin():
    client = _FakeCognitoClient(
        users={"al@example.com": {"email": "al@example.com"}},
        groups={"Admins": {"al@example.com"}, "Editors": set()},
    )

    try:
        service.set_user_group(client, "pool-1", "al@example.com", "Editors")
        assert False, "expected ValueError"
    except ValueError:
        pass
    # Rejected before any mutating call -- al@example.com must still be
    # exactly where they started.
    assert "al@example.com" in client.groups["Admins"]
    assert "admin_remove_user_from_group" not in [c[0] for c in client.calls]


def test_set_user_group_allows_moving_an_admin_when_others_remain():
    """The guard is specifically "the LAST Admin", not "any Admin" --
    moving one of two Admins to Editors must still succeed."""
    client = _FakeCognitoClient(
        users={"al@example.com": {"email": "al@example.com"}},
        groups={"Admins": {"al@example.com", "other-admin@example.com"}, "Editors": set()},
    )

    result = service.set_user_group(client, "pool-1", "al@example.com", "Editors")

    assert result["group"] == "Editors"
    assert "al@example.com" in client.groups["Editors"]
    assert "al@example.com" not in client.groups["Admins"]
    assert "other-admin@example.com" in client.groups["Admins"]


def test_set_user_enabled_toggles_both_directions():
    client = _FakeCognitoClient(users={"al@example.com": {"email": "al@example.com", "enabled": True}})

    disabled = service.set_user_enabled(client, "pool-1", "al@example.com", False)
    assert disabled == {"username": "al@example.com", "enabled": False}
    assert client.users["al@example.com"]["enabled"] is False

    enabled = service.set_user_enabled(client, "pool-1", "al@example.com", True)
    assert enabled == {"username": "al@example.com", "enabled": True}
    assert client.users["al@example.com"]["enabled"] is True


def test_delete_user_removes_the_account():
    client = _FakeCognitoClient(
        users={"editor@example.com": {"email": "editor@example.com"}},
        groups={"Admins": set(), "Editors": {"editor@example.com"}},
    )

    result = service.delete_user(client, "pool-1", "editor@example.com")

    assert result == {"username": "editor@example.com", "deleted": True}
    assert "editor@example.com" not in client.users
    assert "editor@example.com" not in client.groups["Editors"]


def test_delete_user_blocks_deleting_the_last_admin():
    client = _FakeCognitoClient(
        users={"al@example.com": {"email": "al@example.com"}},
        groups={"Admins": {"al@example.com"}, "Editors": set()},
    )

    try:
        service.delete_user(client, "pool-1", "al@example.com")
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert "al@example.com" in client.users  # still there, nothing deleted


def test_delete_user_allows_deleting_a_non_admin():
    client = _FakeCognitoClient(
        users={"al@example.com": {"email": "al@example.com"}, "editor@example.com": {"email": "editor@example.com"}},
        groups={"Admins": {"al@example.com"}, "Editors": {"editor@example.com"}},
    )

    result = service.delete_user(client, "pool-1", "editor@example.com")

    assert result["deleted"] is True
    assert "editor@example.com" not in client.users


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
