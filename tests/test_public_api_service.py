"""
Tests for src/public_api/service.py.

Same honesty note as test_admin_api_service.py: fastapi/pydantic/mangum
aren't installable in this sandbox (pip's proxy 403's), so app.py's actual
HTTP routing is untested this session -- only imports it, doesn't exercise
it. What IS tested here: score_similarity/_reference_sku (pure functions,
no DB), the filter-SQL shape of list_products/list_brands via a query-
capturing fake connection, and the full multi-query assembly of
get_product/get_products_compare/list_similar_products against a small
hand-built fake psycopg2-shaped cursor/connection (no real Postgres
available in this sandbox, same limitation noted throughout this project).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "public_api"))

import service  # noqa: E402


# --- score_similarity / _reference_sku: pure, no DB ---

def test_score_similarity_identical_specs_and_categories_is_zero():
    source = {"rg": 2.50, "differential": 0.050, "core_type": "asymmetric",
              "coverstock_type": "hybrid", "coverstock_material": "reactive_resin"}
    assert service.score_similarity(source, dict(source)) == 0.0


def test_score_similarity_closer_specs_score_lower():
    source = {"rg": 2.50, "differential": 0.050, "core_type": "asymmetric",
              "coverstock_type": "hybrid", "coverstock_material": "reactive_resin"}
    close = {"rg": 2.52, "differential": 0.048, "core_type": "asymmetric",
             "coverstock_type": "hybrid", "coverstock_material": "reactive_resin"}
    far = {"rg": 2.70, "differential": 0.015, "core_type": "symmetric",
           "coverstock_type": "solid", "coverstock_material": "urethane"}
    assert service.score_similarity(source, close) < service.score_similarity(source, far)


def test_score_similarity_category_mismatch_adds_penalty_even_with_identical_specs():
    source = {"rg": 2.50, "differential": 0.050, "core_type": "asymmetric",
              "coverstock_type": "hybrid", "coverstock_material": "reactive_resin"}
    same_specs_diff_core = dict(source, core_type="symmetric")
    assert service.score_similarity(source, same_specs_diff_core) == service.CORE_TYPE_MISMATCH_PENALTY


def test_score_similarity_missing_rg_skips_numeric_term_not_a_penalty():
    source = {"rg": None, "differential": None, "core_type": "asymmetric"}
    candidate = {"rg": 2.90, "differential": 0.005, "core_type": "asymmetric"}
    # No usable numeric spec on either side to compare, and core_type
    # matches -- score should be exactly 0, not some default/error value.
    assert service.score_similarity(source, candidate) == 0.0


def test_reference_sku_prefers_real_15lb_row():
    skus = [{"weight_lbs": 16, "rg": 2.49}, {"weight_lbs": 15, "rg": 2.51}, {"weight_lbs": 14, "rg": 2.53}]
    assert service._reference_sku(skus)["weight_lbs"] == 15


def test_reference_sku_falls_back_to_nearest_weight():
    skus = [{"weight_lbs": 16, "rg": 2.49}, {"weight_lbs": 12, "rg": 2.60}]
    assert service._reference_sku(skus)["weight_lbs"] == 16  # |16-15|=1 < |12-15|=3


def test_reference_sku_empty_list_returns_none():
    assert service._reference_sku([]) is None


# --- list_products / list_brands: filter-SQL shape, query-capturing fake ---

class _QueryCapturingCursor:
    def __init__(self):
        self.queries = []
        self.params = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.queries.append(" ".join(query.split()))
        self.params.append(params)

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


def test_list_products_defaults_to_status_current():
    conn = _QueryCapturingConnection()
    service.list_products(conn)

    query = conn.cursor().queries[0]
    params = conn.cursor().params[0]
    assert "p.published = true and p.status = %s" in query
    assert params[0] == "current"


def test_list_products_status_retired_is_explicit_not_default():
    conn = _QueryCapturingConnection()
    service.list_products(conn, status="retired")

    params = conn.cursor().params[0]
    assert params[0] == "retired"


def test_list_products_never_exposes_a_published_override_param():
    """published=true is baked into the SQL text itself, not a bind
    param -- confirms there's no way for a caller-supplied value to
    relax it (contrast admin_api.list_products, which takes published
    as a real optional filter)."""
    conn = _QueryCapturingConnection()
    service.list_products(conn, brand_id="brand-1", core_id="core-1",
                           coverstock_id="cs-1", search="fury")

    query = conn.cursor().queries[0]
    params = conn.cursor().params[0]
    assert "p.published = true" in query
    assert "and p.brand_id = %s" in query
    assert "and p.core_id = %s" in query
    assert "and p.coverstock_id = %s" in query
    assert "and p.name ilike %s" in query
    assert params == ["current", "brand-1", "core-1", "cs-1", "%fury%", 24, 0]


def test_list_products_orders_by_id_as_tiebreaker():
    conn = _QueryCapturingConnection()
    service.list_products(conn)

    query = conn.cursor().queries[0]
    assert "order by p.updated_at desc, p.id asc limit %s offset %s" in query


def test_list_brands_only_brands_with_published_products():
    conn = _QueryCapturingConnection()
    service.list_brands(conn)

    query = conn.cursor().queries[0]
    assert "where p.published = true" in query
    assert "join products p on p.brand_id = b.id" in query


# --- get_product / get_products_compare / list_similar_products:
# multi-query assembly against a hand-built fake cursor ---

class _FakeCursor:
    def __init__(self, db):
        self.db = db
        self._result_rows = []
        self._result_row = None
        self._description = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        q = " ".join(query.split())
        params = params or ()

        if q.startswith("select distinct b.id, b.name"):
            rows = [(bid, b["name"]) for bid, b in sorted(self.db["brands"].items(), key=lambda kv: kv[1]["name"])
                     if any(p["brand_id"] == bid and p["published"] for p in self.db["products"].values())]
            self._description = [("id",), ("name",)]
            self._result_rows = rows

        elif q.startswith("select p.id, p.name, p.url, p.color, p.status,"):
            status = params[0]
            rows = []
            for pid, p in self.db["products"].items():
                if p["published"] and p["status"] == status:
                    core = self.db["cores"].get(p.get("core_id"), {})
                    rows.append((
                        pid, p["name"], p["url"], p.get("color"), p["status"],
                        self.db["brands"][p["brand_id"]]["name"],
                        core.get("name"), core.get("core_type"),
                        p.get("coverstock_name"), p.get("coverstock_type"), p.get("coverstock_material"),
                        p.get("release_date"), p.get("primary_image_url"),
                        p.get("video_reviews_summary_video_count", 0),
                    ))
            self._description = [(c,) for c in (
                "id", "name", "url", "color", "status", "brand_name", "core_name", "core_type",
                "coverstock_name", "coverstock_type", "coverstock_material", "release_date",
                "primary_image_url", "video_reviews_summary_video_count",
            )]
            self._result_rows = rows

        elif q.startswith("select p.id, p.name, p.url, p.color, p.coverstock_material,"):
            pid = params[0]
            p = self.db["products"].get(pid)
            if p is None or not p["published"]:
                self._result_row = None
                self._description = [("_",)]
            else:
                core = self.db["cores"].get(p.get("core_id"), {})
                cs = self.db["coverstocks"].get(p.get("coverstock_id"), {})
                self._description = [(c,) for c in (
                    "id", "name", "url", "color", "coverstock_material", "coverstock_type",
                    "coverstock_name", "has_particle", "has_custom_graphic", "factory_finish",
                    "part_number", "weights_available", "usbc_approval_date", "release_date",
                    "description", "status", "primary_image_url", "video_reviews_summary",
                    "video_reviews_summary_video_count", "video_reviews_summary_updated_at",
                    "brand_id", "brand_name", "manufacturer_name", "core_id", "core_name", "core_type",
                    "coverstock_id", "coverstock_full_name",
                )]
                self._result_row = (
                    pid, p["name"], p["url"], p.get("color"), p.get("coverstock_material"),
                    p.get("coverstock_type"), p.get("coverstock_name"), p.get("has_particle", False),
                    p.get("has_custom_graphic", False), p.get("factory_finish"), p.get("part_number"),
                    p.get("weights_available"), p.get("usbc_approval_date"), p.get("release_date"),
                    p.get("description"), p["status"], p.get("primary_image_url"),
                    p.get("video_reviews_summary"), p.get("video_reviews_summary_video_count", 0),
                    p.get("video_reviews_summary_updated_at"), p["brand_id"],
                    self.db["brands"][p["brand_id"]]["name"],
                    self.db["brands"][p["brand_id"]].get("manufacturer_name"),
                    p.get("core_id"), core.get("name"), core.get("core_type"),
                    p.get("coverstock_id"), cs.get("name"),
                )

        elif q == "select weight_lbs, rg, differential, mass_bias from product_skus where product_id = %s order by weight_lbs desc":
            pid = params[0]
            self._description = [("weight_lbs",), ("rg",), ("differential",), ("mass_bias",)]
            self._result_rows = [
                (s["weight_lbs"], s.get("rg"), s.get("differential"), s.get("mass_bias"))
                for s in sorted(self.db["skus"].get(pid, []), key=lambda s: -s["weight_lbs"])
            ]

        elif q.startswith("select id, image_type, stored_url, is_thumbnail, display_order"):
            pid = params[0]
            self._description = [("id",), ("image_type",), ("stored_url",), ("is_thumbnail",), ("display_order",)]
            self._result_rows = [
                (img["id"], img["image_type"], img.get("stored_url"), img.get("is_thumbnail", False), img["display_order"])
                for img in self.db["images"].get(pid, []) if img.get("is_visible", True)
            ]

        elif q.startswith("select youtube_video_id, title, channel_title,"):
            pid = params[0]
            self._description = [("youtube_video_id",), ("title",), ("channel_title",), ("published_at",), ("thumbnail_url",), ("summary",)]
            self._result_rows = [
                (v["youtube_video_id"], v.get("title"), v.get("channel_title"), v.get("published_at"),
                 v.get("thumbnail_url"), v.get("summary"))
                for v in self.db["videos"].get(pid, [])
                if v.get("status") == "approved" and v.get("summary") is not None
            ]

        elif q.startswith("select p.id, c.core_type, p.coverstock_type, p.coverstock_material from products p"):
            pid = params[0]
            p = self.db["products"].get(pid)
            if p is None or not p["published"]:
                self._result_row = None
                self._description = [("_",)]
            else:
                core = self.db["cores"].get(p.get("core_id"), {})
                self._description = [("id",), ("core_type",), ("coverstock_type",), ("coverstock_material",)]
                self._result_row = (pid, core.get("core_type"), p.get("coverstock_type"), p.get("coverstock_material"))

        elif q == "select weight_lbs, rg, differential from product_skus where product_id = %s and rg is not null":
            pid = params[0]
            self._description = [("weight_lbs",), ("rg",), ("differential",)]
            self._result_rows = [
                (s["weight_lbs"], s["rg"], s.get("differential"))
                for s in self.db["skus"].get(pid, []) if s.get("rg") is not None
            ]

        elif q.startswith("select p.id, p.name, p.url, p.color, b.name as brand_name,"):
            exclude_id = params[0]
            self._description = [(c,) for c in (
                "id", "name", "url", "color", "brand_name", "core_type", "coverstock_type",
                "coverstock_material", "coverstock_name", "primary_image_url",
            )]
            rows = []
            for pid, p in self.db["products"].items():
                if p["published"] and p["status"] == "current" and pid != exclude_id:
                    core = self.db["cores"].get(p.get("core_id"), {})
                    rows.append((
                        pid, p["name"], p["url"], p.get("color"), self.db["brands"][p["brand_id"]]["name"],
                        core.get("core_type"), p.get("coverstock_type"), p.get("coverstock_material"),
                        p.get("coverstock_name"), p.get("primary_image_url"),
                    ))
            self._result_rows = rows

        elif q.startswith("select product_id, weight_lbs, rg, differential from product_skus where product_id = any(%s::uuid[]) and rg is not null"):
            ids = set(params[0])
            self._description = [("product_id",), ("weight_lbs",), ("rg",), ("differential",)]
            rows = []
            for pid in ids:
                for s in self.db["skus"].get(pid, []):
                    if s.get("rg") is not None:
                        rows.append((pid, s["weight_lbs"], s["rg"], s.get("differential")))
            self._result_rows = rows

        elif q.startswith("select p.id, p.name, p.url, b.name as brand_name, c.core_type, p.coverstock_type, p.coverstock_material, p.has_particle,"):
            status = params[0]
            self._description = [(c,) for c in (
                "id", "name", "url", "brand_name", "core_type", "coverstock_type", "coverstock_material",
                "has_particle", "oil_rating", "motion_rating", "oil_motion_source", "primary_image_url",
            )]
            rows = []
            for pid, p in self.db["products"].items():
                if p["published"] and p["status"] == status:
                    core = self.db["cores"].get(p.get("core_id"), {})
                    rows.append((
                        pid, p["name"], p["url"], self.db["brands"][p["brand_id"]]["name"],
                        core.get("core_type"), p.get("coverstock_type"), p.get("coverstock_material"),
                        p.get("has_particle", False), p.get("oil_rating"), p.get("motion_rating"),
                        p.get("oil_motion_source"), p.get("primary_image_url"),
                    ))
            self._result_rows = rows

        elif q.startswith("select product_id, weight_lbs, rg, differential from product_skus where product_id = any(%s::uuid[]) and differential is not null"):
            ids = set(params[0])
            self._description = [("product_id",), ("weight_lbs",), ("rg",), ("differential",)]
            rows = []
            for pid in ids:
                for s in self.db["skus"].get(pid, []):
                    if s.get("differential") is not None:
                        rows.append((pid, s["weight_lbs"], s.get("rg"), s["differential"]))
            self._result_rows = rows

        else:
            raise NotImplementedError(f"FakeCursor doesn't support: {q}")

    @property
    def description(self):
        return self._description

    def fetchone(self):
        return self._result_row

    def fetchall(self):
        return self._result_rows


class _FakeConnection:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return _FakeCursor(self.db)


def _fresh_db():
    return {"brands": {}, "products": {}, "cores": {}, "coverstocks": {}, "skus": {}, "images": {}, "videos": {}}


def _seed_published_current_product(db, pid="prod-1", brand_id="brand-1", **overrides):
    db["brands"].setdefault(brand_id, {"name": "Brunswick", "manufacturer_name": "Brunswick Corp"})
    product = {
        "name": "Fury", "url": "https://example.com/fury", "brand_id": brand_id,
        "published": True, "status": "current", "core_id": None, "coverstock_id": None,
        "video_reviews_summary_video_count": 0,
    }
    product.update(overrides)
    db["products"][pid] = product
    return pid


# get_product

def test_get_product_returns_none_for_missing_id():
    db = _fresh_db()
    assert service.get_product(_FakeConnection(db), "no-such-id") is None


def test_get_product_returns_none_for_unpublished_product():
    """The identical-404 guarantee this module's docstring promises --
    exists-but-unpublished must come back exactly like doesn't-exist."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, published=False)
    assert service.get_product(_FakeConnection(db), pid) is None


def test_get_product_assembles_skus_images_and_approved_summarized_videos():
    db = _fresh_db()
    pid = _seed_published_current_product(db)
    db["skus"][pid] = [
        {"weight_lbs": 16, "rg": 2.49, "differential": 0.045, "mass_bias": None},
        {"weight_lbs": 15, "rg": 2.51, "differential": 0.047, "mass_bias": None},
    ]
    db["images"][pid] = [
        {"id": "img-1", "image_type": "main", "stored_url": "https://s3/main.png", "is_thumbnail": True, "display_order": 0, "is_visible": True},
        {"id": "img-2", "image_type": "other", "stored_url": "https://s3/hidden.png", "is_thumbnail": False, "display_order": 1, "is_visible": False},
    ]
    db["videos"][pid] = [
        {"youtube_video_id": "yt-approved", "title": "Review", "channel_title": "Bowler Channel",
         "published_at": "2026-01-01", "thumbnail_url": "https://yt/thumb.jpg",
         "summary": "Great ball.", "status": "approved"},
        {"youtube_video_id": "yt-pending", "title": "Unreviewed", "channel_title": "X",
         "published_at": "2026-01-02", "thumbnail_url": None, "summary": None, "status": "pending"},
        {"youtube_video_id": "yt-approved-no-summary", "title": "Not summarized yet", "channel_title": "X",
         "published_at": "2026-01-03", "thumbnail_url": None, "summary": None, "status": "approved"},
    ]

    product = service.get_product(_FakeConnection(db), pid)

    assert product["id"] == pid
    assert len(product["skus"]) == 2
    assert product["skus"][0]["weight_lbs"] == 16  # order by weight_lbs desc

    assert len(product["images"]) == 1  # hidden image excluded
    assert product["images"][0]["id"] == "img-1"

    assert len(product["videos"]) == 1  # only approved + summary is not null
    assert product["videos"][0]["youtube_video_id"] == "yt-approved"


# get_products_compare

def test_get_products_compare_preserves_input_order():
    db = _fresh_db()
    p1 = _seed_published_current_product(db, pid="prod-1", name="Alpha")
    p2 = _seed_published_current_product(db, pid="prod-2", name="Beta")
    p3 = _seed_published_current_product(db, pid="prod-3", name="Gamma")

    result = service.get_products_compare(_FakeConnection(db), [p3, p1, p2])

    assert [r["id"] for r in result] == [p3, p1, p2]


def test_get_products_compare_silently_drops_missing_and_unpublished():
    db = _fresh_db()
    p1 = _seed_published_current_product(db, pid="prod-1")
    p2 = _seed_published_current_product(db, pid="prod-2", published=False)

    result = service.get_products_compare(_FakeConnection(db), [p1, p2, "no-such-id"])

    assert [r["id"] for r in result] == [p1]


def test_get_products_compare_caps_at_max_compare_ids():
    db = _fresh_db()
    ids = []
    for i in range(service.MAX_COMPARE_IDS + 3):
        pid = f"prod-{i}"
        _seed_published_current_product(db, pid=pid)
        ids.append(pid)

    result = service.get_products_compare(_FakeConnection(db), ids)

    assert len(result) == service.MAX_COMPARE_IDS
    assert [r["id"] for r in result] == ids[:service.MAX_COMPARE_IDS]


# list_similar_products

def test_list_similar_products_returns_empty_for_missing_source():
    db = _fresh_db()
    assert service.list_similar_products(_FakeConnection(db), "no-such-id") == []


def test_list_similar_products_excludes_source_and_non_current_and_unpublished():
    db = _fresh_db()
    retired = _seed_published_current_product(
        db, pid="retired-1", status="retired", core_id="core-a", coverstock_type="hybrid",
        coverstock_material="reactive_resin",
    )
    db["cores"]["core-a"] = {"name": "Old Core", "core_type": "asymmetric"}
    db["skus"]["retired-1"] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]

    close_current = _seed_published_current_product(
        db, pid="current-close", status="current", core_id="core-b",
        coverstock_type="hybrid", coverstock_material="reactive_resin",
    )
    db["cores"]["core-b"] = {"name": "New Core", "core_type": "asymmetric"}
    db["skus"]["current-close"] = [{"weight_lbs": 15, "rg": 2.51, "differential": 0.049}]

    far_current = _seed_published_current_product(
        db, pid="current-far", status="current", core_id="core-c",
        coverstock_type="solid", coverstock_material="urethane",
    )
    db["cores"]["core-c"] = {"name": "Far Core", "core_type": "symmetric"}
    db["skus"]["current-far"] = [{"weight_lbs": 15, "rg": 2.75, "differential": 0.015}]

    unpublished_current = _seed_published_current_product(
        db, pid="current-unpublished", status="current", published=False,
    )
    db["skus"]["current-unpublished"] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]

    another_retired = _seed_published_current_product(
        db, pid="retired-2", status="retired",
    )
    db["skus"]["retired-2"] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]

    results = service.list_similar_products(_FakeConnection(db), retired)
    result_ids = [r["id"] for r in results]

    assert retired not in result_ids  # source itself excluded
    assert "current-unpublished" not in result_ids  # unpublished excluded
    assert "retired-2" not in result_ids  # only status='current' candidates
    assert result_ids == ["current-close", "current-far"]  # ranked closer-first


def test_list_similar_products_respects_limit():
    db = _fresh_db()
    source = _seed_published_current_product(db, pid="retired-1", status="retired")
    db["skus"]["retired-1"] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]

    for i in range(10):
        pid = f"current-{i}"
        _seed_published_current_product(db, pid=pid, status="current")
        db["skus"][pid] = [{"weight_lbs": 15, "rg": 2.50 + i * 0.01, "differential": 0.050}]

    results = service.list_similar_products(_FakeConnection(db), source, limit=3)
    assert len(results) == 3


# --- estimate_oil_motion: pure, no DB ---

def test_estimate_oil_motion_within_valid_ranges_for_every_material_type_combo():
    materials = [None, "polyester_plastic", "urethane", "reactive_resin"]
    types = [None, "solid", "pearl", "hybrid"]
    core_types = [None, "symmetric", "asymmetric"]
    for m in materials:
        for t in types:
            for c in core_types:
                for particle in (True, False):
                    for diff in (None, 0.01, 0.065):
                        result = service.estimate_oil_motion(
                            core_type=c, coverstock_type=t, coverstock_material=m,
                            has_particle=particle, differential=diff,
                        )
                        assert service.OIL_MIN <= result["oil"] <= service.OIL_MAX
                        assert service.MOTION_MIN <= result["motion"] <= service.MOTION_MAX


def test_estimate_oil_motion_heavier_material_and_particle_increase_oil():
    poly = service.estimate_oil_motion(coverstock_material="polyester_plastic")
    solid_resin = service.estimate_oil_motion(coverstock_type="solid", coverstock_material="reactive_resin")
    particle_solid_resin = service.estimate_oil_motion(
        coverstock_type="solid", coverstock_material="reactive_resin", has_particle=True,
    )
    assert poly["oil"] < solid_resin["oil"] < particle_solid_resin["oil"]


def test_estimate_oil_motion_pearl_skids_more_than_solid():
    solid = service.estimate_oil_motion(coverstock_type="solid", coverstock_material="reactive_resin")
    pearl = service.estimate_oil_motion(coverstock_type="pearl", coverstock_material="reactive_resin")
    assert pearl["oil"] < solid["oil"]


def test_estimate_oil_motion_asymmetric_and_higher_differential_increase_motion():
    sym_low_diff = service.estimate_oil_motion(core_type="symmetric", differential=0.015)
    asym_high_diff = service.estimate_oil_motion(core_type="asymmetric", differential=0.06)
    assert sym_low_diff["motion"] < asym_high_diff["motion"]


def test_estimate_oil_motion_no_inputs_falls_back_to_midrange():
    result = service.estimate_oil_motion()
    assert service.OIL_MIN < result["oil"] < service.OIL_MAX
    assert service.MOTION_MIN < result["motion"] < service.MOTION_MAX


# --- list_plotter_positions: multi-query assembly, chart vs. estimated ---

def test_list_plotter_positions_uses_chart_value_when_set():
    db = _fresh_db()
    pid = _seed_published_current_product(
        db, core_id="core-a", coverstock_type="solid", coverstock_material="reactive_resin",
        oil_rating=6, motion_rating=18, oil_motion_source="chart",
    )
    db["cores"]["core-a"] = {"name": "Some Core", "core_type": "asymmetric"}
    db["skus"][pid] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]

    results = service.list_plotter_positions(_FakeConnection(db))

    assert len(results) == 1
    assert results[0]["oil"] == 6
    assert results[0]["motion"] == 18
    assert results[0]["oil_motion_source"] == "chart"


def test_list_plotter_positions_reads_manual_source_unchanged():
    """A manually-corrected position (admin PATCH .../plotter-position,
    migration 012) must come through as 'manual', not get relabeled
    'chart' just because it has real values set -- oil_motion_source is
    READ, never inferred from whether the rating columns are non-null."""
    db = _fresh_db()
    pid = _seed_published_current_product(
        db, core_id="core-a", coverstock_type="solid", coverstock_material="reactive_resin",
        oil_rating=9, motion_rating=11, oil_motion_source="manual",
    )
    db["cores"]["core-a"] = {"name": "Some Core", "core_type": "asymmetric"}
    db["skus"][pid] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]

    results = service.list_plotter_positions(_FakeConnection(db))

    assert results[0]["oil"] == 9
    assert results[0]["motion"] == 11
    assert results[0]["oil_motion_source"] == "manual"


def test_list_plotter_positions_estimates_when_chart_value_unset():
    db = _fresh_db()
    pid = _seed_published_current_product(
        db, core_id="core-a", coverstock_type="pearl", coverstock_material="reactive_resin",
    )
    db["cores"]["core-a"] = {"name": "Some Core", "core_type": "symmetric"}
    db["skus"][pid] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.020}]

    results = service.list_plotter_positions(_FakeConnection(db))

    assert len(results) == 1
    assert results[0]["oil_motion_source"] == "estimated"
    assert service.OIL_MIN <= results[0]["oil"] <= service.OIL_MAX
    assert service.MOTION_MIN <= results[0]["motion"] <= service.MOTION_MAX


def test_list_plotter_positions_only_published_current_by_default():
    db = _fresh_db()
    _seed_published_current_product(db, pid="current-1", status="current")
    db["skus"]["current-1"] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]
    _seed_published_current_product(db, pid="retired-1", status="retired")
    db["skus"]["retired-1"] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]
    _seed_published_current_product(db, pid="unpublished-1", published=False)
    db["skus"]["unpublished-1"] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]

    results = service.list_plotter_positions(_FakeConnection(db))

    assert [r["id"] for r in results] == ["current-1"]


if __name__ == "__main__":
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for name, t in tests:
        t()
        print(f"PASS: {name}")
        passed += 1
    print(f"\n{passed}/{len(tests)} tests passed")
