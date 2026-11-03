"""
Tests for src/product_article_generator/brand_art.py and
brand_art_compose.py (runbook 6fq): brand lineups, the brand_art task's
planning and trigger rules, and the real-photo compositing.

Pure functions only (no DB): plan_slots / due_slots / listing_members /
in_stock / stocked_companions / the compositor, plus run_task's slot loop
through a fake connection-free path (render_slot + slot_keys_for).
Images are synthetic Pillow images, as in test_image_resizer.py.
"""
import datetime
import io
import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "product_article_generator"))

import brand_art  # noqa: E402
import brand_art_compose as compose  # noqa: E402

PALETTE = {"background": "#191c1b", "accent": "#009d90"}
TODAY = datetime.date(2026, 11, 2)


def _png_cutout(color=(40, 40, 160)):
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse((20, 20, 180, 180), fill=color + (255,))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _jpeg_on_white(color=(200, 30, 30)):
    im = Image.new("RGB", (200, 200), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rectangle((40, 40, 160, 170), fill=color)
    d.rectangle((80, 80, 120, 120), fill=(255, 255, 255))  # white panel inside the product
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _ball(pid, name, released, bc=None, url=None):
    return {"product_id": pid, "name": name, "url": url or f"https://www.stormbowling.com/storm-{pid}-bowling-ball",
            "release_date": released, "bigcommerce_product_id": bc, "photos": [f"https://x/{pid}.png"]}


def _task(**params):
    base = {"palette": PALETTE, "slug": "storm", "line_groupings": ["reaction-profile"]}
    base.update(params)
    return {"id": "t1", "brand_name": "Storm", "params": base, "triggers": [], "last_full_run_at": None}


def _line(key, ids, grouping="reaction-profile"):
    return {"grouping_key": grouping, "grouping_label": "Reaction profile", "line_id": f"l-{key}", "key": key,
            "label": key.title(), "text": None, "assign_rule": {"type": "manual"}, "ball_ids": ids}


# --- compositing --------------------------------------------------------

def test_cutout_keeps_real_alpha_and_trims():
    cut = compose.cutout(_png_cutout())
    assert cut.mode == "RGBA" and cut.size == (161, 161)


def test_cutout_keys_out_white_background_but_keeps_interior_white():
    cut = compose.cutout(_jpeg_on_white())
    a = cut.getchannel("A")
    w, h = cut.size
    assert a.getpixel((w // 2, h // 2)) == 255          # the white panel inside the product stays
    assert a.getpixel((0, 0)) < 255 or cut.size[0] < 200  # the backdrop is gone (trimmed or transparent)


def test_layouts_render_at_slot_sizes_under_250kb():
    ball, bag = compose.cutout(_png_cutout()), compose.cutout(_jpeg_on_white())
    for size, im in [((1200, 900), compose.line((1200, 900), PALETTE, [ball, ball, ball])),
                     ((1600, 1200), compose.product_hero((1600, 1200), PALETTE, bag, ball)),
                     ((1600, 900), compose.product_hero((1600, 900), PALETTE, ball)),
                     ((1600, 1200), compose.lineup_hero((1600, 1200), PALETTE, [ball, ball]))]:
        assert im.size == size
        assert len(compose.encode_webp(im)) <= 250_000


def test_background_is_the_brand_color_in_the_corners():
    im = compose.background((400, 300), PALETTE)
    # The accent glow fades out before the corners (within a couple of levels).
    assert all(abs(a - b) <= 3 for a, b in zip(im.getpixel((0, 0)), (0x19, 0x1C, 0x1B)))


# --- assign rules -------------------------------------------------------

def test_listing_members_matches_brand_urls_and_colorway_pages_only():
    html = ('<a href="/storm-phaze-ii-bowling-ball">x</a><a href="/roto-grip-gremlin-bowling-ball">y</a>'
            '<a href="/storm-mix-bowling-ball">z</a>')
    balls = [_ball("a", "Phaze II", TODAY, url="https://www.stormbowling.com/storm-phaze-ii-bowling-ball"),
             _ball("b", "Mix Platinum", TODAY, url="https://www.stormbowling.com/storm-mix-bowling-ball-platinum"),
             _ball("c", "Phaze 5", TODAY, url="https://www.stormbowling.com/storm-phaze-5-bowling-ball")]
    rule = {"type": "brand_listing", "platform": "commercebuild", "url": "https://www.stormbowling.com/products/x/?f=1"}
    assert brand_art.listing_members(rule, balls, lambda url: html) == {"a", "b"}


def test_listing_members_unreadable_or_unknown_platform_changes_nothing():
    rule = {"type": "brand_listing", "platform": "commercebuild", "url": "https://x/"}
    def boom(url):
        raise IOError("down")
    assert brand_art.listing_members(rule, [], boom) is None
    assert brand_art.listing_members({"platform": "shopify", "url": "u"}, [], lambda u: "") is None


# --- stock --------------------------------------------------------------

def test_in_stock_rules():
    base = {"is_visible": True, "availability": "available"}
    assert brand_art.in_stock({**base, "inventory_tracking": "product", "inventory_level": 3})
    assert not brand_art.in_stock({**base, "inventory_tracking": "product", "inventory_level": 0})
    assert brand_art.in_stock({**base, "inventory_tracking": "none"})
    assert brand_art.in_stock({**base, "inventory_tracking": "variant",
                               "variants": [{"inventory_level": 0}, {"inventory_level": 2}]})
    assert not brand_art.in_stock({**base, "is_visible": False, "inventory_tracking": "none"})
    assert not brand_art.in_stock({**base, "availability": "disabled", "inventory_tracking": "none"})


def test_stocked_companions_newest_first_and_skips_balls_and_sold_out():
    roots = {26: "bags", 25: "balls", 28: "accessories", 190: "on sale"}
    img = [{"url_zoom": "https://cdn/x.jpg", "is_thumbnail": True}]
    live = {"is_visible": True, "availability": "available", "inventory_tracking": "none", "images": img}
    catalog = [
        {**live, "id": 1, "name": "Old Bag", "categories": [26], "date_created": "Mon, 01 Jan 2024 00:00:00 +0000"},
        {**live, "id": 2, "name": "New Roller", "categories": [26, 190], "date_created": "Sun, 01 Nov 2026 00:00:00 +0000"},
        {**live, "id": 3, "name": "A Ball", "categories": [25], "date_created": "Sun, 01 Nov 2026 00:00:00 +0000"},
        {**live, "id": 4, "name": "Sold Out Bag", "categories": [26], "inventory_tracking": "product",
         "inventory_level": 0, "date_created": "Sun, 01 Nov 2026 00:00:00 +0000"},
        {**live, "id": 5, "name": "No Photo", "categories": [28], "images": [], "date_created": ""},
    ]
    got = brand_art.stocked_companions(catalog, roots)
    assert [c["bigcommerce_product_id"] for c in got] == [2, 1]
    assert got[0]["kind"] == "bag"


# --- planning -----------------------------------------------------------

# The pre-6fu default (two newest released balls, then a companion hero):
# still valid pick types when a task lists them in params.heroes.
LEGACY_HEROES = [{"type": "product", "pick": "newest_ball", "rank": 1},
                 {"type": "product", "pick": "newest_ball", "rank": 2},
                 {"type": "product", "pick": "newest_companion", "fallback_rank": 3}]


def _plan(companions=(), stocked=frozenset(), heroes=LEGACY_HEROES):
    balls = [_ball("new", "Ion Pro Pearl", datetime.date(2026, 11, 6), bc=10),   # not released yet
             _ball("b1", "Equinox Hybrid", datetime.date(2026, 9, 4), bc=11),
             _ball("b2", "IQ Tour Sapphire", datetime.date(2026, 8, 7), bc=12),
             _ball("b3", "Alpha Crux", datetime.date(2026, 5, 29), bc=13),
             _ball("b4", "Monsoon", datetime.date(2026, 5, 1), bc=None)]
    lines = [_line("strong-symmetrical", ["b3", "b1"]), _line("empty", []),
             _line("early-read", ["b1"], grouping="lane-read")]
    task = _task(**({"heroes": heroes} if heroes else {}))
    return brand_art.plan_slots(task, balls, lines, list(companions), set(stocked), TODAY)


def test_plan_lines_only_for_chosen_groupings_newest_first_and_skips_empty():
    slots = {s["slot_key"]: s for s in _plan()}
    assert [k for k in slots if k.startswith("line-")] == ["line-strong-symmetrical"]
    assert [i["name"] for i in slots["line-strong-symmetrical"]["items"]] == ["Equinox Hybrid", "Alpha Crux"]
    assert slots["line-strong-symmetrical"]["alt"] == "Storm Equinox Hybrid and Alpha Crux bowling balls"


def test_plan_heroes_are_newest_released_balls_stocked_first():
    slots = {s["slot_key"]: s for s in _plan(stocked={11, 13})}
    names = [slots[f"hero-{i}"]["items"][0]["name"] for i in (1, 2, 3)]
    assert names == ["Equinox Hybrid", "Alpha Crux", "IQ Tour Sapphire"]   # Ion Pro Pearl isn't released
    assert slots["hero-1"]["alt"] == "Storm Equinox Hybrid bowling ball"
    assert slots["hero-1"]["sizes"] == [(1600, 1200), (1600, 900)]


def test_plan_third_hero_is_newest_companion_with_newest_ball_beside():
    bag = {"bigcommerce_product_id": 900, "name": "Storm Rolling Thunder", "kind": "bag", "photo": "https://cdn/b.jpg"}
    slots = {s["slot_key"]: s for s in _plan(companions=[bag], stocked={11, 12, 13})}
    h3 = slots["hero-3"]
    assert [i["name"] for i in h3["items"]] == ["Storm Rolling Thunder", "Equinox Hybrid"]
    assert h3["items"][0]["product_id"] is None and h3["items"][0]["bigcommerce_product_id"] == 900
    assert h3["alt"] == "Storm Rolling Thunder with the Storm Equinox Hybrid bowling ball"


def test_plan_lineup_hero_from_a_line():
    slots = {s["slot_key"]: s for s in _plan(heroes=[{"type": "lineup", "line": "strong-symmetrical"}])}
    assert slots["hero-1"]["hero_type"] == "lineup"
    assert [i["name"] for i in slots["hero-1"]["items"]] == ["Alpha Crux", "Equinox Hybrid"]


# --- triggers -----------------------------------------------------------

def _stored(slot, **over):
    d = {"input_hash": slot["input_hash"], "featured": [
        {k: i[k] for k in ("product_id", "bigcommerce_product_id", "name", "kind")} for i in slot["items"]]}
    d.update(over)
    return d


def test_due_slots_nothing_changed_and_not_a_week():
    slots = _plan()
    stored = {s["slot_key"]: _stored(s) for s in slots}
    task = _task()
    task["last_full_run_at"] = datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc)
    assert brand_art.due_slots(task, slots, stored, datetime.datetime(2026, 11, 2, tzinfo=datetime.timezone.utc)) == {}


def test_due_slots_weekly_sweep_redraws_everything():
    slots = _plan()
    stored = {s["slot_key"]: _stored(s) for s in slots}
    task = _task()
    task["last_full_run_at"] = datetime.datetime(2026, 10, 20, tzinfo=datetime.timezone.utc)
    due = brand_art.due_slots(task, slots, stored, datetime.datetime(2026, 11, 2, tzinfo=datetime.timezone.utc))
    assert set(due) == {s["slot_key"] for s in slots} and set(due.values()) == {"weekly_sweep"}


def test_due_slots_names_the_rule_that_fired():
    slots = {s["slot_key"]: s for s in _plan()}
    task = _task()
    task["last_full_run_at"] = datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc)
    now = datetime.datetime(2026, 11, 2, tzinfo=datetime.timezone.utc)
    stored = {k: _stored(s) for k, s in slots.items()}
    stored["hero-1"] = _stored(slots["hero-1"], input_hash="old",
                               featured=[{"product_id": "older", "bigcommerce_product_id": 1}])
    stored["line-strong-symmetrical"] = _stored(slots["line-strong-symmetrical"], input_hash="old",
                                                featured=[{"product_id": "b3", "bigcommerce_product_id": 13}])
    stored["hero-2"] = _stored(slots["hero-2"], input_hash="old")  # same products, new photo
    stored["hero-3"] = _stored(slots["hero-3"], input_hash="old",
                               featured=[{"product_id": None, "bigcommerce_product_id": 900}])
    due = brand_art.due_slots(task, list(slots.values()), stored, now)
    assert due == {"hero-1": "ball_released", "line-strong-symmetrical": "line_changed",
                   "hero-2": "photo_changed", "hero-3": "companion_stock"}


def test_slot_keys_are_stable_paths():
    assert brand_art.slot_keys_for("storm", "hero-1") == ("brand-art/storm/hero-1.webp", "brand-art/storm/hero-1-16x9.webp")


# --- rendering -----------------------------------------------------------

def test_render_line_and_bag_hero_are_composites():
    deps = {"fetch_bytes": lambda url: _jpeg_on_white() if url.endswith(".jpg") else _png_cutout()}
    bag = {"bigcommerce_product_id": 900, "name": "Bag", "kind": "bag", "photo": "https://cdn/b.jpg"}
    slots = {s["slot_key"]: s for s in _plan(companions=[bag], stocked={11})}
    images, method = brand_art.render_slot(slots["line-strong-symmetrical"], PALETTE, deps)
    assert method == "composite" and images[0][1].size == (1200, 900)
    images, method = brand_art.render_slot(slots["hero-3"], PALETTE, deps)
    assert method == "composite" and [im.size for _, im in images] == [(1600, 1200), (1600, 900)]


def test_ball_hero_uses_gemini_and_falls_back_to_composite_when_checks_fail():
    calls = []
    def gemini_ok(pid, raw, theme, no_text, aspect):
        calls.append(aspect)
        im = Image.new("RGB", (2048, 1536) if aspect == "4:3" else (2048, 1152), (10, 10, 10))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()
    slot = {s["slot_key"]: s for s in _plan(stocked={11})}["hero-1"]
    deps = {"fetch_bytes": lambda url: _png_cutout(), "gemini": gemini_ok}
    images, method = brand_art.render_slot(slot, PALETTE, deps)
    assert method == "gemini" and calls == ["4:3", "16:9"]
    assert [im.size for _, im in images] == [(1600, 1200), (1600, 900)]
    deps["gemini"] = lambda *a: None   # every attempt fails the logo checks
    images, method = brand_art.render_slot(slot, PALETTE, deps)
    assert method == "composite"


def test_backdrop_prompt_bans_text_and_names_the_palette():
    theme = brand_art.HERO_BACKDROP_THEME.format(**PALETTE)
    assert "#191c1b" in theme and "#009d90" in theme
    assert "no text" in brand_art.HERO_NO_TEXT


def test_pick_companion_prefers_3_ball_rollers_then_totes_by_price_and_honors_a_pin():
    # Al: "can you use higher priced bags, 3 ball roller or triple totes?
    # that is an entry level bag and isn't idea[l]" (about a 1-ball roller).
    def bag(i, name, price, cat):
        return {"bigcommerce_product_id": i, "name": name, "kind": "bag", "price": price, "categories": [cat]}
    c = [{"bigcommerce_product_id": 1, "name": "Storm Glove", "kind": "accessory", "price": 300, "categories": []},
         bag(2, "Storm Rascal 1 Ball Roller", 79.95, "bags > roller bags > 1 ball rollers"),
         bag(3, "Storm 3 Ball Travel Tote Pro", 104.95, "bags > tote bags > 3 ball totes"),
         bag(4, "Storm Streamline 3 Ball Roller", 179.95, "bags > roller bags > 3 ball rollers"),
         bag(5, "Storm Rolling Thunder 3 Ball Roller", 269.95, "bags > roller bags > 3 ball rollers"),
         {"bigcommerce_product_id": 6, "name": "Storm Shoe", "kind": "shoes", "price": 150, "categories": []}]
    assert brand_art.pick_companion(c, {})["bigcommerce_product_id"] == 5          # priciest 3-ball roller
    assert brand_art.pick_companion([c[1], c[2]], {})["bigcommerce_product_id"] == 3  # triple tote over 1-ball roller
    assert brand_art.pick_companion([c[0], c[5]], {})["bigcommerce_product_id"] == 6  # shoes before accessories
    assert brand_art.pick_companion(c, {"bigcommerce_product_id": 2})["bigcommerce_product_id"] == 2
    assert brand_art.pick_companion(c, {"bigcommerce_product_id": 99})["bigcommerce_product_id"] == 5  # sold-out pin


def test_cutout_uses_remove_bg_for_flat_photos_and_falls_back_on_failure():
    def matte(raw):
        return _png_cutout((200, 30, 30))
    cut = compose.cutout(_jpeg_on_white(), matte)
    assert cut.size == (161, 161)                       # the model's matte, trimmed
    def boom(raw):
        raise RuntimeError("throttled")
    assert compose.cutout(_jpeg_on_white(), boom).mode == "RGBA"   # flood-fill fallback
    calls = []
    compose.cutout(_png_cutout(), lambda raw: calls.append(1))
    assert calls == []                                  # already a cut-out: no model call


def test_companion_inches_by_type():
    def c(name, cat="", kind="bag"):
        return {"name": name, "kind": kind, "categories": [cat]}
    assert brand_art.companion_inches(c("Storm Rolling Thunder 3 Ball Roller", "bags > roller bags > 3 ball rollers")) == 36
    assert brand_art.companion_inches(c("Storm Streamline 2 Ball Roller", "bags > roller bags > 2 ball rollers")) == 30
    assert brand_art.companion_inches(c("Storm Rascal 1 Ball Roller", "bags > roller bags > 1 ball rollers")) == 24
    assert brand_art.companion_inches(c("Storm 3 Ball Travel Tote Pro", "bags > tote bags > 3 ball totes")) == 27
    assert brand_art.companion_inches(c("Storm Solo Single Tote", "bags > tote bags > 1 ball totes")) == 13
    assert brand_art.companion_inches(c("Dexter SST 8", "shoes > mens", "shoes")) == 12
    assert brand_art.companion_inches(c("Storm Wrist Support", "accessories > gloves", "accessory")) == 9


def test_bag_hero_draws_the_ball_to_scale():
    # A tall, narrow "roller" (1:3) at 45": the ball should be 8.5/45 of the
    # roller's height, not a fixed share of the frame.
    roller = Image.new("RGBA", (100, 300), (30, 30, 30, 255))
    canvas = compose.product_hero((1600, 1200), PALETTE, roller, compose.cutout(_png_cutout()), focus_inches=45)
    # The roller is fitted to 76% of 1200 = 912 px tall, so the ball is
    # 912 * 8.5 / 45 = ~172 px. Measure the drawn ball by its blue.
    px = canvas.load()
    ys = [y for y in range(1200) for x in range(0, 1600, 4)
          if px[x, y][2] > 120 and px[x, y][0] < 80 and px[x, y][1] < 80]
    assert abs((max(ys) - min(ys)) - 912 * 8.5 / 45) < 8


def test_exclusive_lines_exempt_their_balls_and_settle_old_flags():
    # Al: "mix, ice, clear, and norm duke are spare balls. tropical is entry
    # level." -- balls in an exclusive line aren't flagged as unplaced in
    # the brand's other groupings, and pending flags for them are settled.
    import re as _re
    state = {"lines": [
        {"grouping_key": "reaction-profile", "grouping_label": "Reaction profile", "line_id": "L1", "key": "strong",
         "text": None, "label": "Strong", "assign_rule": {"type": "brand_listing"}, "ball_ids": ["a"]},
        {"grouping_key": "ball-type", "grouping_label": "Ball type", "line_id": "L2", "key": "spare",
         "text": None, "label": "Spare", "assign_rule": {"type": "fields", "exclusive": True}, "ball_ids": ["mix"]},
    ]}
    executed = []

    class Cur:
        rowcount = 0
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q, params=()): executed.append((" ".join(q.split()), params))

    class Conn:
        def cursor(self): return Cur()
        def commit(self): pass

    orig = brand_art.list_lines, brand_art.listing_members, brand_art.fields_members
    brand_art.list_lines = lambda conn, bid: state["lines"]
    brand_art.listing_members = lambda rule, balls, fetch: {"a"}
    brand_art.fields_members = lambda rule, conn, bid: {"mix"}
    try:
        balls = [_ball("a", "Phaze II", TODAY), _ball("mix", "MIX PLATINUM", TODAY), _ball("x", "Identity", TODAY)]
        out = brand_art.apply_assign_rules(Conn(), "storm", balls, lambda u: "", sleep=lambda s: None)
    finally:
        brand_art.list_lines, brand_art.listing_members, brand_art.fields_members = orig
    assert out["unplaced"] == [{"grouping": "reaction-profile", "product_id": "x"}]   # not "mix"
    settles = [p for q, p in executed if q.startswith("update review_queue set status = 'approved'")]
    assert ("reaction-profile", ["a", "mix"]) in settles


def test_storm_ball_type_rules_match_the_names_al_listed():
    import json as _json, re as _re
    cfg = _json.load(open(os.path.join(os.path.dirname(__file__), "..", "scripts", "brand_lineups", "storm.json")))
    lines = {l["key"]: l for g in cfg["groupings"] if g["key"] == "ball-type" for l in g["lines"]}
    spare = _re.compile(lines["spare"]["assign_rule"]["all"][0]["matches"])
    entry = _re.compile(lines["entry-level"]["assign_rule"]["all"][0]["matches"])
    for n in ["MIX PLATINUM", "ICE STORM OCEAN BLUE-WHITE", "Ice Storm Teal-Silver-Graphite", "CLEAR STORM TEAL",
              "LIGHTNING STORM CLEAR", "Storm Norm Duke Clear"]:
        assert spare.search(n), n
    assert entry.search("TROPICAL SURGE TEAL-BLUE") and entry.search("Tropical Surge Kiwi-Apple-Avocado")
    for n in ["Phaze II", "Identity", "Equinox Hybrid", "IQ Tour"]:
        assert not spare.search(n) and not entry.search(n), n


def test_no_line_images_unless_a_task_names_groupings():
    # Runbook 6ft: "drop line slots from the brand-art task for every brand".
    balls = [_ball("b1", "Equinox Hybrid", datetime.date(2026, 9, 4), bc=11)]
    task = _task()
    task["params"].pop("line_groupings")
    slots = brand_art.plan_slots(task, balls, [_line("strong-symmetrical", ["b1"])], [], {11}, TODAY)
    assert [s["slot_key"] for s in slots] == ["hero-1"]


# --- Runbook 6fu: default heroes = newest ball (pre-orders too) + line mix ---

def _plan_default(stocked=frozenset()):
    balls = [_ball("new", "Ion Pro Pearl", datetime.date(2026, 11, 6), bc=4519),   # pre-order
             _ball("b1", "Equinox Hybrid", datetime.date(2026, 9, 4), bc=11),
             _ball("b2", "IQ Tour Sapphire", datetime.date(2026, 8, 7), bc=12),
             _ball("b3", "Alpha Crux", datetime.date(2026, 5, 29), bc=13),
             _ball("b4", "Monsoon", datetime.date(2026, 5, 1), bc=None),
             _ball("mix", "MIX PLATINUM", datetime.date(2026, 1, 1), bc=None)]
    lines = [_line("strong", ["b3"]), _line("smooth", ["b4"]), _line("responsive", ["new", "b1"]),
             _line("clean", ["b2", "b1"]), _line("empty", []),
             {**_line("spare", ["mix"], grouping="ball-type"), "assign_rule": {"type": "fields", "exclusive": True}},
             _line("early-read", ["b1", "b2"], grouping="lane-read"),
             _line("late-read", ["b3"], grouping="lane-read")]
    task = _task()
    task["params"].pop("line_groupings")
    return {s["slot_key"]: s for s in brand_art.plan_slots(task, balls, lines, [], set(stocked), TODAY)}


def test_default_hero_1_is_the_newest_ball_including_pre_orders():
    slots = _plan_default(stocked={11, 12})
    assert slots["hero-1"]["items"][0]["name"] == "Ion Pro Pearl"
    assert slots["hero-1"]["items"][0]["bigcommerce_product_id"] == 4519


def test_default_hero_2_mixes_the_newest_ball_of_each_line_of_the_first_grouping():
    slots = _plan_default()
    h2 = slots["hero-2"]
    assert h2["hero_type"] == "lineup" and h2["sizes"] == [(1600, 1200), (1600, 900)]
    # strong -> Alpha Crux, smooth -> Monsoon, responsive -> Ion Pro Pearl,
    # clean -> Equinox Hybrid (its newest, not yet used); the lane-read and
    # exclusive spare lines aren't mixed in.
    assert [i["name"] for i in h2["items"]] == ["Alpha Crux", "Monsoon", "Ion Pro Pearl", "Equinox Hybrid"]


def test_default_has_no_third_hero_and_no_line_images():
    assert sorted(_plan_default()) == ["hero-1", "hero-2"]


def test_lineup_hero_fits_five_or_six_balls_in_both_sizes():
    ball = compose.cutout(_png_cutout())
    for n in (5, 6):
        for size in ((1600, 1200), (1600, 900)):
            im = compose.lineup_hero(size, PALETTE, [ball] * n)
            assert im.size == size
            px = im.convert("L").load()
            assert max(px[2, y] for y in range(size[1])) < 60 and max(px[size[0] - 3, y] for y in range(size[1])) < 60


def test_line_mix_never_repeats_a_ball():
    balls = [_ball("a", "A", datetime.date(2026, 5, 1)), _ball("b", "B", datetime.date(2026, 4, 1))]
    lines = [_line("one", ["a"]), _line("two", ["a", "b"])]
    task = _task(heroes=[{"type": "line_mix"}])
    slot = next(s for s in brand_art.plan_slots(task, balls, lines, [], set(), TODAY) if s["slot_key"] == "hero-1")
    assert [i["name"] for i in slot["items"]] == ["A", "B"]


# --- Runbook 6fv: lineup heroes as a pro shop scene --------------------------

def _scene_png(size):
    im = Image.new("RGB", size, (40, 40, 40))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def test_lineup_hero_is_a_gemini_scene_with_every_ball_as_a_reference():
    calls = []
    def scene(raws, shelf_raws, prompt, aspect):
        calls.append((len(raws), aspect, prompt))
        return _scene_png((2048, 1536) if aspect == "4:3" else (2048, 1152))
    slot = _plan_default()["hero-2"]
    images, method = brand_art.render_slot(slot, PALETTE, {"fetch_bytes": lambda u: _png_cutout(), "gemini_scene": scene})
    assert method == "gemini" and [im.size for _, im in images] == [(1600, 1200), (1600, 900)]
    assert [(n, a) for n, a, _ in calls] == [(4, "4:3"), (4, "16:9")]
    prompt = calls[0][2]
    assert "butcher block" in prompt and "#191c1b" in prompt and "exactly the 4 bowling balls" in prompt
    assert "NO bowling balls anywhere in the background" in prompt


def test_lineup_scene_falls_back_to_the_composite_for_both_sizes():
    sizes = []
    def scene(raws, shelf_raws, prompt, aspect):
        sizes.append(aspect)
        return None if aspect == "16:9" else _scene_png((2048, 1536))
    slot = _plan_default()["hero-2"]
    images, method = brand_art.render_slot(slot, PALETTE, {"fetch_bytes": lambda u: _png_cutout(), "gemini_scene": scene})
    assert method == "composite" and [im.size for _, im in images] == [(1600, 1200), (1600, 900)]
    assert sizes.count("16:9") == brand_art.GEMINI_HERO_ATTEMPTS


def test_scene_has_no_background_balls_unless_a_task_lists_them():
    # Gemini invented display balls even with real ones as references, so
    # by default the shop behind shows bags, accessories and shoes only.
    balls = [_ball(c, c.upper(), datetime.date(2026, 5, 1) - datetime.timedelta(days=i)) for i, c in enumerate("abcd")]
    lines = [_line("one", ["a"]), _line("two", ["b"])]
    slot = next(s for s in brand_art.plan_slots(_task(heroes=[{"type": "line_mix"}]), balls, lines, [], set(), TODAY)
                if s["slot_key"] == "hero-1")
    assert slot["shelf"] == []
    calls = []
    def scene(raws, shelf_raws, prompt, aspect):
        calls.append((len(raws), len(shelf_raws), prompt))
        return _scene_png((2048, 1536) if aspect == "4:3" else (2048, 1152))
    brand_art.render_slot(slot, PALETTE, {"fetch_bytes": lambda u: _png_cutout(), "gemini_scene": scene})
    assert calls[0][:2] == (2, 0) and "NO bowling balls anywhere in the background" in calls[0][2]
    listed = next(s for s in brand_art.plan_slots(_task(heroes=[{"type": "line_mix", "display_ball_ids": ["c", "d"]}]),
                                                  balls, lines, [], set(), TODAY) if s["slot_key"] == "hero-1")
    assert [p["name"] for p in listed["shelf"]] == ["C", "D"]

def test_line_mix_is_ordered_by_demand_highest_first():
    # "the balls in order of demand with highest infront"
    balls = [{**_ball("a", "A", datetime.date(2026, 5, 1)), "demand_score": 0.97},
             {**_ball("b", "B", datetime.date(2026, 4, 1)), "demand_score": 0.99},
             {**_ball("c", "C", datetime.date(2026, 3, 1)), "demand_score": None}]
    lines = [_line("one", ["a"]), _line("two", ["b"]), _line("three", ["c"])]
    task = _task(heroes=[{"type": "line_mix"}])
    slot = next(s for s in brand_art.plan_slots(task, balls, lines, [], set(), TODAY) if s["slot_key"] == "hero-1")
    assert [i["name"] for i in slot["items"]] == ["B", "A", "C"]


def test_scene_prompt_is_a_close_receding_row_with_shallow_focus():
    p = brand_art.SCENE_PROMPT
    assert "close-up" in p and "first reference ball nearest the camera" in p and "shallow depth of field" in p
