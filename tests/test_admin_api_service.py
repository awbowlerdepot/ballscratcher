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
# top 10 shrinking ADUs by sku." Seven sequential queries total (KPIs, top
# popularity, top ADU, ADU by brand, top days-of-supply, top growing ADU,
# top shrinking ADU) -- _QueryCapturingConnection's fetchone() defaults to
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


def test_get_dashboard_summary_kpi_query_reuses_total_adu_sql():
    """total_catalog_adu must be the SAME _TOTAL_ADU_SQL expression
    list_products' own total_adu column and sort option already use --
    not a second, potentially-drifting definition of ADU."""
    conn = _DashboardQueryCapturingConnection()
    service.get_dashboard_summary(conn)

    kpi_query = conn.cursor().queries[0]
    assert "product_sku_stock_history" in kpi_query
    assert "having count(*) >= 2" in kpi_query
    assert "coalesce(sum(t.total_adu), 0)" in kpi_query
    assert ") t) as total_catalog_adu" in kpi_query


def test_get_dashboard_summary_top_popularity_query_shape():
    conn = _DashboardQueryCapturingConnection()
    service.get_dashboard_summary(conn)

    query = conn.cursor().queries[1]
    assert "left join brands b on b.id = p.brand_id" in query
    assert "where t.popularity_score > 0" in query
    assert "order by t.popularity_score desc, t.id asc limit 10" in query
    # Reuses the real formula, same reasoning as the KPI query's total_adu.
    assert "product_videos pv" in query
    assert "ln(1 + count(*))" in query


def test_get_dashboard_summary_top_adu_query_shape():
    conn = _DashboardQueryCapturingConnection()
    service.get_dashboard_summary(conn)

    query = conn.cursor().queries[2]
    assert "left join brands b on b.id = p.brand_id" in query
    assert "where t.total_adu > 0" in query
    assert "order by t.total_adu desc, t.id asc limit 10" in query
    assert "product_sku_stock_history" in query


def test_get_dashboard_summary_adu_by_brand_query_shape():
    """Unlike the other two, this is a real GROUP BY over ALL brands
    (left join, so a brand with zero matching products/ADU still shows
    up at 0 -- Al's "ADUs by brand" ask reads as the full breakdown, not
    a filtered top-N)."""
    conn = _DashboardQueryCapturingConnection()
    service.get_dashboard_summary(conn)

    query = conn.cursor().queries[3]
    assert "with sku_adu as" in query
    assert "from brands b" in query
    assert "left join products p on p.brand_id = b.id" in query
    assert "group by b.name" in query
    assert "order by total_adu desc" in query
    assert "where t.total_adu > 0" not in query  # no >0 filter here, unlike top_adu


def test_get_dashboard_summary_top_days_of_supply_query_shape():
    """Al: "top 10 days of supply skus descending so lowest number of days
    first." PER-SKU (product_skus/weight_lbs joined in, unlike the
    per-product top_popularity/top_adu queries above), ordered ascending
    (lowest days-of-supply -- soonest to stock out -- first)."""
    conn = _DashboardQueryCapturingConnection()
    service.get_dashboard_summary(conn)

    query = conn.cursor().queries[4]
    assert "with sa as" in query
    assert "latest_qty as" in query
    assert "distinct on (product_sku_id)" in query
    assert "sk.weight_lbs" in query
    assert "where sa.adu > 0 and lq.quantity is not null" in query
    assert "order by (lq.quantity / sa.adu) asc, sk.id asc limit 10" in query


def test_get_dashboard_summary_top_growing_adu_query_shape():
    """Al: "top 10 growth ADUs... by sku." Compares the current
    ADU_LOOKBACK_DAYS-day window's per-SKU rate against the
    ADU_LOOKBACK_DAYS days before that -- both windows driven off
    ADU_LOOKBACK_DAYS (not hardcoded literals), confirming the "current"
    window stays in lockstep with every other ADU query's own definition
    of "current"."""
    conn = _DashboardQueryCapturingConnection()
    service.get_dashboard_summary(conn)

    query = conn.cursor().queries[5]
    assert "with cw as" in query
    assert "pw as" in query
    assert ("interval '%d days'" % service.ADU_LOOKBACK_DAYS) in query
    assert ("interval '%d days'" % (2 * service.ADU_LOOKBACK_DAYS)) in query
    assert "where cw.adu is not null and pw.adu is not null and (cw.adu - pw.adu) > 0" in query
    assert "order by (cw.adu - pw.adu) desc, sk.id asc limit 10" in query


def test_get_dashboard_summary_top_shrinking_adu_query_shape():
    """Mirror image of top_growing_adu immediately above -- same shape,
    opposite filter/sort direction (biggest decrease first)."""
    conn = _DashboardQueryCapturingConnection()
    service.get_dashboard_summary(conn)

    query = conn.cursor().queries[6]
    assert "with cw as" in query
    assert "pw as" in query
    assert "where cw.adu is not null and pw.adu is not null and (cw.adu - pw.adu) < 0" in query
    assert "order by (cw.adu - pw.adu) asc, sk.id asc limit 10" in query


def test_get_dashboard_summary_only_runs_seven_queries():
    conn = _DashboardQueryCapturingConnection()
    service.get_dashboard_summary(conn)
    assert len(conn.cursor().queries) == 7


def test_get_dashboard_summary_assembles_all_seven_results():
    conn = _SequencedConnection([
        {  # KPIs
            "columns": [
                "total_products", "current_products", "retired_products",
                "missing_core", "missing_coverstock", "missing_skus",
                "products_with_video", "products_with_price_tracking",
                "total_catalog_adu",
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
        {  # top_adu
            "columns": ["id", "name", "brand_name", "total_adu"],
            "all": [
                ("prod-3", "Black Widow 3.0", "Hammer", "42.5"),
            ],
        },
        {  # adu_by_brand
            "columns": ["brand_name", "total_adu"],
            "all": [
                ("Storm", "300.1"),
                ("Hammer", "150.0"),
                ("Ebonite", "0"),
            ],
        },
        {  # top_days_of_supply
            "columns": ["product_id", "name", "brand_name", "weight_lbs", "adu", "latest_quantity", "days_of_supply"],
            "all": [
                ("prod-4", "Fallout", "Roto Grip", 15, "8.90", 4, "0.4"),
            ],
        },
        {  # top_growing_adu
            "columns": ["product_id", "name", "brand_name", "weight_lbs", "previous_adu", "current_adu", "delta_adu"],
            "all": [
                ("prod-5", "Intel Tour", "900 Global", 16, "2.00", "5.70", "3.70"),
            ],
        },
        {  # top_shrinking_adu
            "columns": ["product_id", "name", "brand_name", "weight_lbs", "previous_adu", "current_adu", "delta_adu"],
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
        "total_catalog_adu": "1234.5",
    }
    assert result["top_popularity"] == [
        {"id": "prod-1", "name": "Absolute", "brand_name": "Storm", "popularity_score": "980.2"},
        {"id": "prod-2", "name": "Phaze II", "brand_name": "Storm", "popularity_score": "875.0"},
    ]
    assert result["top_adu"] == [
        {"id": "prod-3", "name": "Black Widow 3.0", "brand_name": "Hammer", "total_adu": "42.5"},
    ]
    assert result["adu_by_brand"] == [
        {"brand_name": "Storm", "total_adu": "300.1"},
        {"brand_name": "Hammer", "total_adu": "150.0"},
        {"brand_name": "Ebonite", "total_adu": "0"},
    ]
    assert result["top_days_of_supply"] == [
        {"product_id": "prod-4", "name": "Fallout", "brand_name": "Roto Grip", "weight_lbs": 15,
         "adu": "8.90", "latest_quantity": 4, "days_of_supply": "0.4"},
    ]
    assert result["top_growing_adu"] == [
        {"product_id": "prod-5", "name": "Intel Tour", "brand_name": "900 Global", "weight_lbs": 16,
         "previous_adu": "2.00", "current_adu": "5.70", "delta_adu": "3.70"},
    ]
    assert result["top_shrinking_adu"] == [
        {"product_id": "prod-6", "name": "Bionic", "brand_name": "900 Global", "weight_lbs": 14,
         "previous_adu": "6.00", "current_adu": "1.50", "delta_adu": "-4.50"},
    ]


# --- get_catalog_adu_history: real follow-up ask, same session, Al: "can
# we add some data over time charts to the dashboard, maybe total catalog
# adu over time similar to what we have per product 7d, 30d, 90d, 1y and
# all picker." A DIFFERENT definition from kpis.total_catalog_adu above
# (see the function's own docstring) -- a real, honest, catalog-wide
# rolling-ADU_LOOKBACK_DAYS-day average, not that exact per-SKU-gated
# formula replayed at every historical day.

def test_get_catalog_adu_history_query_shape():
    conn = _QueryCapturingConnection()
    service.get_catalog_adu_history(conn)

    query = conn.cursor().queries[0]
    assert "with bounds as" in query
    assert "generate_series(min_day, max_day, interval '1 day')" in query
    assert "product_sku_stock_history" in query
    assert "lag(psh.quantity) over (partition by psh.product_sku_id order by psh.checked_at)" in query
    # Drops-only, same interpretation as _TOTAL_ADU_SQL/adu_by_brand.
    assert "case when delta < 0 then -delta else 0 end" in query
    # Rolling 30-day trailing window, driven off ADU_LOOKBACK_DAYS (not a
    # second hardcoded literal) -- confirms the two stay in lockstep.
    assert ("rows between %d preceding and current row" % (service.ADU_LOOKBACK_DAYS - 1)) in query
    assert ("/ %s as total_adu" % float(service.ADU_LOOKBACK_DAYS)) in query


def test_get_catalog_adu_history_only_runs_one_query():
    conn = _QueryCapturingConnection()
    service.get_catalog_adu_history(conn)
    assert len(conn.cursor().queries) == 1


def test_get_catalog_adu_history_assembles_day_and_total_adu_rows():
    conn = _SequencedConnection([
        {
            "columns": ["day", "total_adu"],
            "all": [
                ("2026-07-01", "0.00"),
                ("2026-07-02", "3.50"),
                ("2026-07-03", "3.90"),
            ],
        },
    ])

    result = service.get_catalog_adu_history(conn)

    assert result == [
        {"day": "2026-07-01", "total_adu": "0.00"},
        {"day": "2026-07-02", "total_adu": "3.50"},
        {"day": "2026-07-03", "total_adu": "3.90"},
    ]


def test_get_catalog_adu_history_empty_when_no_stock_history():
    conn = _SequencedConnection([{"columns": ["day", "total_adu"], "all": []}])
    assert service.get_catalog_adu_history(conn) == []


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
    applies a sub-linear ln(1 + count) volume boost, not a plain sum.

    Scoped to the _POPULARITY_SCORE_SQL constant itself (not the full
    query string) since list_products' query now also embeds
    _TOTAL_ADU_SQL, which legitimately contains its own unrelated
    "select sum(" (summing per-SKU ADU into a per-product total) --
    a blanket substring check against the whole query would false-
    positive on that, so this checks only the fragment this test
    actually cares about."""
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "select avg(" in query
    assert "* ln(1 + count(*))" in query
    assert "select sum(" not in service._POPULARITY_SCORE_SQL


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


# --- list_products: total_adu -- Al: "can we add the sum of the ADUs
# for each product to the main table." Same trailing-window/drops-only
# ADU definition as admin-site's own computeSkuForecast, re-implemented
# in SQL (see _TOTAL_ADU_SQL's own comment for why it can't be shared
# with that JS copy and must be kept in lockstep by hand). ---

def test_list_products_always_selects_total_adu():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "as total_adu" in query
    assert "product_sku_stock_history" in query
    assert "product_skus" in query


def test_list_products_total_adu_uses_confirmed_lookback_window():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert f"interval '{service.ADU_LOOKBACK_DAYS} days'" in query
    assert service.ADU_LOOKBACK_DAYS == 30


def test_list_products_total_adu_only_counts_drops_not_restocks():
    """The lag()-based delta must only sum NEGATIVE deltas (a quantity
    drop = sold) -- a positive delta (restock) must never add to
    units_sold, same "drop=sold, rise=restock" interpretation
    computeSkuForecast's own docstring documents."""
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "case when h.delta < 0 then -h.delta else 0 end" in query
    assert "lag(psh.quantity)" in query


def test_list_products_total_adu_requires_at_least_two_readings():
    """A SKU with fewer than 2 readings in the window can't compute a
    rate at all -- must be excluded from the sum entirely (having
    count(*) >= 2), not counted as a zero, same as computeSkuForecast's
    own `rows.length < 2 -> adu: null`."""
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "having count(*) >= 2" in query


def test_list_products_total_adu_guards_zero_elapsed_days():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "case when sku.elapsed_days > 0 then sku.units_sold / sku.elapsed_days else 0 end" in query


def test_list_products_total_adu_scoped_to_this_product_only():
    conn = _QueryCapturingConnection()
    service.list_products(conn, limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "ps_adu.product_id = p.id" in query


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


def test_list_products_sort_total_adu_orders_by_column_desc():
    # Al's direct follow-up to the Total ADU column itself: "can we add
    # a sort to the admin ui products list for total ADU". Orders by
    # the select-list alias (total_adu, not a repeated subquery) --
    # Postgres allows ORDER BY to reference a SELECT list alias, no
    # need to duplicate _TOTAL_ADU_SQL a second time. No "nulls last"
    # needed unlike newest/oldest since _TOTAL_ADU_SQL is always
    # coalesce(..., 0), never actually null.
    conn = _QueryCapturingConnection()
    service.list_products(conn, sort="total_adu", limit=50, offset=0)

    query = conn.cursor().queries[0]
    assert "order by total_adu desc, p.id asc limit %s offset %s" in query


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
    # blanket "product_skus" substring check -- _TOTAL_ADU_SQL now
    # legitimately joins product_skus (aliased ps_adu) unconditionally
    # on every call to compute the Total ADU column, so a bare
    # "product_skus" absence check would false-positive now that it's
    # a normal part of every query, filter or not.
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
    # Note: product_videos is ALREADY referenced in every query's SELECT
    # list via _POPULARITY_SCORE_SQL (an `exists`-free correlated
    # subquery scoped to pv.status = 'approved' and pv.view_count is not
    # null) -- so this checks for the exact WHERE-clause text this
    # filter adds (a plain, unscoped `not exists`), not just any mention
    # of product_videos, to avoid a false positive against that
    # pre-existing subquery.
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
    status/summary condition inside it (unlike _POPULARITY_SCORE_SQL's
    always-present SELECT-list subquery, which does)."""
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
