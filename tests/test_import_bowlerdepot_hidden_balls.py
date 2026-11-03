"""
Tests for scripts/import_bowlerdepot_hidden_balls.py (runbook 6ga).

Al, via the Spurline session: add the retired balls bowlerdepot.com hid on
2026-11-02 "but only the ones BowlerIQ doesn't already have", from the store
copy alone ("don't fill in specs it doesn't have"), and report what matched and
how so a near name isn't counted as missing without a check. So most of this
pins classify (which tier, never `missing` for one of ours) and build_ball
(only the store's fields, specs only on 15 lb). The write path gets a fake
connection that records SQL.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import import_bowlerdepot_hidden_balls as script  # noqa: E402

BASE = "https://bowlerdepot.com"


def entry(id_, name, brand, custom_fields=(), variants=None, images=None, mpn="", **extra):
    e = {"bigcommerce_product_id": id_, "name": name, "brand": brand, "custom_url": f"/{id_}/",
         "mpn": mpn, "upc": "", "description_html": "", "custom_fields": [{"name": n, "value": v} for n, v in custom_fields],
         "images": images if images is not None else [{"url": f"https://cdn/{id_}.jpg", "is_thumbnail": True}],
         "variants": variants if variants is not None else [{"sku": f"CPS{id_}-{w}", "options": [f"{w}lbs"]} for w in (16, 15, 14)]}
    e.update(extra)
    return e


def ours(*balls):
    by_brand = {}
    for pid, brand, name in balls:
        by_brand.setdefault(script._brand_key(brand), []).append({"id": pid, "brand": brand, "name": name})
    return by_brand


OURS = ours(("p-payback", "Radical", "Payback"), ("p-rhino", "Brunswick", "Rhino - Black / Blue / Silver Pearl"),
            ("p-scorch", "DV8", "Scorcher Spare"), ("p-harsh", "900 Global", "HARSH REALITY"),
            ("p-method", "Brunswick", "Method Solid"), ("p-phaze", "Storm", "PHAZE A.I."),
            ("p-sub", "MOTIV", "Subzero Forge"), ("p-hyroad", "Storm", "HY-ROAD"))


def tier(e, linked=(), parts=None):
    return script.classify(e, OURS, set(linked), parts or {})


# --- classify --------------------------------------------------------------------

def test_linked_by_bigcommerce_id_first():
    assert tier(entry(4017, "Motiv Subzero Forge", "Motiv"), linked={"4017"})["tier"] == "linked"


def test_part_number_match():
    row = tier(entry(1, "Storm Something Else", "Storm", mpn="ab-12"), parts={"AB-12": {"p-phaze"}})
    assert row["tier"] == "part_number" and row["product_id"] == "p-phaze"


def test_part_number_from_another_brand_does_not_count():
    assert tier(entry(1, "Storm Nova Zz", "Storm", mpn="X1"), parts={"X1": {"p-sub"}})["tier"] == "missing"


def test_exact_name_across_brand_case():
    row = tier(entry(2, "Motiv Subzero Forge", "Motiv"))   # our brand is "MOTIV"
    assert row["tier"] == "exact_name" and row["product_id"] == "p-sub"


def test_spacing_variant_is_near_not_missing():
    row = tier(entry(3, "Radical Pay Back", "Radical"))
    assert row["tier"] == "near_name" and row["product_id"] == "p-payback"


def test_store_name_inside_ours_is_hold():
    row = tier(entry(4, "Brunswick Rhino Black/Blue/Silver", "Brunswick"))
    assert row["tier"] == "hold" and row["product_id"] == "p-rhino" and "pearl" in row["note"]


def test_viz_a_ball_vs_spare_is_hold():
    assert tier(entry(5, "DV8 Scorcher Viz-a-Ball", "DV8"))["tier"] == "hold"


def test_our_name_inside_theirs_is_a_different_ball():
    # "Harsh Reality Pearl" / "Hy-Road Pearl" are separate balls from the base one.
    assert tier(entry(6, "900 Global Harsh Reality Pearl", "900 Global"))["tier"] == "missing"
    assert tier(entry(7, "Storm HY-Road Pearl", "Storm"))["tier"] == "missing"


def test_distinguishing_suffix_never_near():
    # 6h.1: "Phaze III" must not reach "PHAZE A.I." or anything else.
    assert tier(entry(8, "Storm Phaze III", "Storm"))["tier"] == "missing"


# --- build_ball ------------------------------------------------------------------

FIELDS = [("Core", "Magnitude 055 High Diff"), ("Core Type", "Asymmetric"), ("Coverstock", "Synthesis Solid"),
          ("Cover Type", "Solid Reactive"), ("Finish", "500/2000 Siaair"), ("Color", "Black/Red"),
          ("RG (15 lb)", "2.49"), ("Differential (15 lb)", "0.055"), ("Mass Bias (15 lb)", "0.017"),
          ("Lane Condition", "Heavy Oil"), ("Fragrance", "Cherry")]


def test_build_ball_maps_only_the_store_fields():
    b = script.build_ball(entry(492, "Brunswick Method Solid", "Brunswick", FIELDS,
                                description_html="<p>Strong &amp; smooth</p>"), BASE)
    assert b["name"] == "Method Solid"
    assert b["url"] == "https://bowlerdepot.com/492/"
    assert (b["coverstock_name"], b["coverstock_material"], b["coverstock_type"]) == ("Synthesis Solid", "reactive_resin", "solid")
    assert (b["core_name"], b["core_type"]) == ("Magnitude 055 High Diff", "asymmetric")
    assert (b["factory_finish"], b["color"], b["fragrance"]) == ("500/2000 Siaair", "Black/Red", "Cherry")
    assert b["description"] == "Strong & smooth"
    assert b["weights"] == [14, 15, 16]
    assert b["part_number"] is None
    assert "release_date" not in b   # the listing's date_created is not a release date


def test_specs_only_on_the_15_lb_sku():
    b = script.build_ball(entry(492, "Brunswick Method Solid", "Brunswick", FIELDS), BASE)
    by_w = {s["weight_lbs"]: s for s in b["skus"]}
    assert (by_w[15]["rg"], by_w[15]["differential"], by_w[15]["mass_bias"]) == (2.49, 0.055, 0.017)
    assert all(by_w[w][k] is None for w in (14, 16) for k in ("rg", "differential", "mass_bias"))
    assert {s["source"] for s in b["skus"]} == {"html"}


def test_older_label_spelling_is_read_too():
    fields = [("Radius of Gyration(15lb)", "2.52"), ("Max Differential(15lb)", "0.048"), ("Int. Differential(15lb)", "0.012")]
    b = script.build_ball(entry(1, "Storm X", "Storm", fields), BASE)
    sku15 = next(s for s in b["skus"] if s["weight_lbs"] == 15)
    assert (sku15["rg"], sku15["differential"], sku15["mass_bias"]) == (2.52, 0.048, 0.012)


def test_no_specs_no_guess():
    b = script.build_ball(entry(1, "Storm X", "Storm", [("Coverstock", "R2S Pearl")]), BASE)
    assert all(s["rg"] is None and s["differential"] is None for s in b["skus"])
    assert b["core_name"] is None and b["core_type"] is None
    assert (b["coverstock_material"], b["coverstock_type"]) == ("reactive_resin", "pearl")  # from the coverstock name


def test_specs_without_a_15_lb_variant_are_noted_not_stored():
    variants = [{"sku": "a", "options": ["14lbs"]}, {"sku": "b", "options": ["12 lbs"]}]
    b = script.build_ball(entry(1, "Storm X", "Storm", FIELDS, variants=variants), BASE)
    assert b["weights"] == [12, 14]
    assert all(s["rg"] is None for s in b["skus"])
    assert any("no 15 lb variant" in n for n in b["notes"])


def test_polyester_and_particle_covers():
    b = script.build_ball(entry(1, "Ebonite Maxim", "Ebonite", [("Cover Type", "Polyester")]), BASE)
    assert b["coverstock_material"] == "polyester_plastic"
    b = script.build_ball(entry(2, "Storm Y", "Storm", [("Cover Type", "Particle Pearl Reactive")]), BASE)
    assert b["has_particle"] is True and b["coverstock_type"] == "pearl"


def test_images_thumbnail_first_and_deduped():
    images = [{"url": "https://cdn/b.jpg", "is_thumbnail": False}, {"url": "https://cdn/a.jpg", "is_thumbnail": True},
              {"url": "https://cdn/b.jpg", "is_thumbnail": False}]
    b = script.build_ball(entry(1, "Storm X", "Storm", images=images), BASE)
    assert b["image_urls"] == ["https://cdn/a.jpg", "https://cdn/b.jpg"]


def test_non_weight_options_ignored():
    variants = [{"sku": "a", "options": ["15lbs", "Right Hand"]}, {"sku": "b", "options": ["Top Weight 3oz"]}]
    assert script.weights_from_variants(variants) == [15]


# --- plan ------------------------------------------------------------------------

def test_plan_writes_missing_and_included_holds_only():
    brands = {script._brand_key(b): (f"id-{b}", b) for b in ("Storm", "Brunswick", "Radical", "MOTIV", "DV8")}
    entries = [entry(10, "Storm Phaze III", "Storm"), entry(11, "Brunswick Rhino Black/Blue/Silver", "Brunswick"),
               entry(12, "DV8 Scorcher Viz-a-Ball", "DV8"), entry(13, "Radical Pay Back", "Radical"),
               entry(14, "Swag Thing", "SWAG")]
    rows = {r["bigcommerce_product_id"]: r for r in
            script.plan(entries, brands, OURS, {}, set(), BASE, include={"11"})}
    assert rows["10"]["write"] and rows["10"]["brand_id"] == "id-Storm"
    assert rows["11"]["write"]                 # hold, named in --include
    assert not rows["12"]["write"]             # hold, not included
    assert not rows["13"]["write"]             # near name: one of ours
    assert not rows["14"]["write"] and "brand not in BowlerIQ" in rows["14"]["note"]


# --- insert_ball -----------------------------------------------------------------

class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        q = " ".join(sql.split())
        self.conn.log.append((q, params))
        if q.startswith("insert into products"):
            self._row = None if self.conn.url_exists else ("new-id",)
        elif q.startswith("insert into product_images"):
            self.conn.n += 1
            self._row = (f"img-{self.conn.n}",)
        elif q.startswith("insert into cores") or q.startswith("insert into coverstocks"):
            self._row = ("ref-id",)
        else:
            self._row = None

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, url_exists=False):
        self.log, self.n, self.url_exists = [], 0, url_exists
        self.committed = self.rolled_back = False

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def test_insert_ball_writes_retired_product_skus_images_and_link():
    b = script.build_ball(entry(492, "Brunswick Method Solid", "Brunswick", FIELDS,
                                images=[{"url": "https://cdn/1.jpg", "is_thumbnail": True},
                                        {"url": "https://cdn/2.jpg"}]), BASE)
    conn = _Conn()
    result = script.insert_ball(conn, "brand-1", b)
    assert result == {"product_id": "new-id", "image_jobs": [
        {"product_image_id": "img-1", "source_url": "https://cdn/1.jpg"},
        {"product_image_id": "img-2", "source_url": "https://cdn/2.jpg"}]}
    sql = [q for q, _ in conn.log]
    product_q, product_p = conn.log[0]
    assert "'retired', 'other', 'ball'" in product_q and "on conflict (url) do nothing" in product_q
    assert product_p[2] == "https://bowlerdepot.com/492/"
    assert sum(q.startswith("insert into product_skus") for q in sql) == 3
    images = [(p[1], p[3], p[4]) for q, p in conn.log if q.startswith("insert into product_images")]
    assert images == [("main", 0, True), ("other", 1, False)]
    link_q = [(q, p) for q, p in conn.log if q.startswith("insert into bowlerdepot_products")]
    assert len(link_q) == 1 and "'matched'" in link_q[0][0] and "on conflict (product_id) do nothing" in link_q[0][0]
    assert not any(q.startswith("update bowlerdepot_products") or "product_price_sources" in q for q in sql)
    assert conn.committed


def test_insert_ball_existing_url_writes_nothing_else():
    b = script.build_ball(entry(492, "Brunswick Method Solid", "Brunswick", FIELDS), BASE)
    conn = _Conn(url_exists=True)
    assert script.insert_ball(conn, "brand-1", b) is None
    assert len(conn.log) == 1 and conn.rolled_back and not conn.committed
