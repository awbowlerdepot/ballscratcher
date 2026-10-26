"""scripts/import_kegel_library.py mapping (runbook 6et)."""
import importlib.util
import json
import os

HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location("import_kegel_library", os.path.join(HERE, "..", "scripts", "import_kegel_library.py"))
kl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kl)
op = kl.oil_patterns

BIG_BEN = json.load(open(os.path.join(HERE, "fixtures", "kegel_library_big_ben.json")))


def test_big_ben_maps_onto_the_spec():
    m = kl.map_pattern(BIG_BEN)
    assert m["name"] == "Big Ben" and m["source"] == "kegel" and m["category"] == "challenge"
    assert m["series"] == "Landmark Patterns"
    assert m["source_url"].endswith("/pattern/8ae2ae8b-d052-ec11-8c62-000d3a5afff3")
    assert m["sheet_url"].endswith("format=pdf")
    assert (m["length_feet"], m["reverse_brush_drop_feet"], m["volume_ml"]) == (44, 39, 24.7)
    assert m["oil_per_board_ul"] is None  # PUMP: MULTI -- each step has its own mics
    assert m["download_count"] == 9377
    assert m["machine"]["model"] == "Flex" and m["machine"]["tank_a_conditioner"] == "FIRE"
    assert "tank_b_conditioner" not in m["machine"]  # single tank
    fwd = [s for s in m["steps"] if s["direction"] == "forward"]
    rev = [s for s in m["steps"] if s["direction"] == "reverse"]
    assert len(fwd) == 8 and len(rev) == 7  # padding rows (UIisVisible false) dropped
    assert [s["mics"] for s in fwd[:3]] == [50, 50, 45]
    assert len(m["track_zones"]) == 6 and m["track_zones"][0]["ratio"] == 7.33  # arrow zones dropped
    assert "&rsquo;" not in m["description"] and "44’" in m["description"]


def test_big_ben_is_clean_and_matches_kegel():
    m = kl.map_pattern(BIG_BEN)
    assert kl.not_clean_reasons(BIG_BEN, m) == []
    c = op.compute(m)
    assert c["ratio"] == 7.33
    totals = [f + r for f, r in zip(c["board_totals"]["forward_ul"], c["board_totals"]["reverse_ul"])]
    p = BIG_BEN["Pattern"]
    assert totals == [int(f + r) for f, r in zip(p["ForwardData"], p["ReverseData"])]  # Kegel's own per-board oil


def test_features_the_model_does_not_cover_keep_a_pattern_pending():
    # Forward drop brush: modelled since 6ey (Kegel's 9 read as inches).
    d = json.loads(json.dumps(BIG_BEN))
    d["Pattern"]["IsForwardDropBrush"], d["Pattern"]["ForwardDropBrushDistance"] = True, 9
    m = kl.map_pattern(d)
    assert m["forward_brush_drop_feet"] == 0.75 and kl.not_clean_reasons(d, m) == []
    d["Pattern"]["EndOfPatternRaiseBrush"] = True
    assert any("Raises the brush" in r for r in kl.not_clean_reasons(d, kl.map_pattern(d)))
    d = json.loads(json.dumps(BIG_BEN))
    d["Pattern"]["HasErrors"] = True
    assert any("Kegel flags" in r for r in kl.not_clean_reasons(d, kl.map_pattern(d)))
