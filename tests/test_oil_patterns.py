"""Oil pattern model (runbook 6ej): the Elite 48 sheet, worked end to end.
Expected numbers are the sheet's own (and the spec's worked example)."""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "admin_api"))

import oil_patterns as op  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "pattern_elite48.json")


def elite48():
    with open(FIXTURE) as f:
        return json.load(f)


def test_board_labels_convert_to_1_39_from_the_left():
    assert op.board_from_label("2L") == 2
    assert op.board_from_label("2R") == 38
    assert op.board_from_label("12L") == 12 and op.board_from_label("11R") == 29
    assert op.board_from_label(" 18r ") == 22
    for bad in ("", "40L", "0R", "2X", "L2", None):
        try:
            op.board_from_label(bad)
            assert False, bad
        except ValueError:
            pass


def test_elite48_steps_match_the_sheet():
    steps = op.normalize_steps(elite48()["steps"], 50)
    for s in steps:
        assert s["crossed"] == s["reported_crossed"], s
        assert s["oil_ul"] == s["reported_oil_ul"], s
    assert sum(s["oil_ul"] for s in steps if s["direction"] == "forward") == 14650
    assert sum(s["oil_ul"] for s in steps if s["direction"] == "reverse") == 15550


def test_elite48_board_totals_match_the_bar_chart():
    out = op.compute(elite48())
    bt = out["board_totals"]
    assert bt["forward_ul"][20 - 1] == 450 and bt["reverse_ul"][20 - 1] == 650
    for board in range(2, 8):
        assert bt["forward_ul"][board - 1] + bt["reverse_ul"][board - 1] == 550
    assert bt["forward_ul"][0] == 0 and bt["reverse_ul"][38] == 0  # boards 1 and 39 get nothing
    assert sum(bt["forward_ul"]) == 14650 and sum(bt["reverse_ul"]) == 15550


def test_elite48_profile_shape_and_volume():
    prof = op.compute(elite48())["profile"]
    assert prof["units"] == "ul_per_board_per_foot" and prof["foot_step"] == 1.0
    assert len(prof["rows"]) == 48 and all(len(r) == 39 for r in prof["rows"])
    assert abs(sum(sum(r) for r in prof["rows"]) - 30200) <= 48  # within 1 µL per row of rounding
    # No oil past the reverse brush's longest loaded step (40 ft), none on board 1.
    assert sum(sum(r) for r in prof["rows"][40:]) == 0
    assert all(r[0] == 0 for r in prof["rows"])
    # Feet 0-3.9 carry step 1 (150 µL per board over 3.9 ft) plus nothing else forward... and reverse step 10 is 0 loads.
    assert abs(prof["rows"][0][1] - 150 / 3.9) < 0.11


def test_elite48_passes_every_check_and_ratio_is_the_headline():
    p = elite48()
    assert op.check(p) == []
    assert op.compute(p)["ratio"] == 2.0


def test_checks_catch_ocr_mistakes():
    p = elite48()
    bad = copy.deepcopy(p)
    bad["steps"][2]["loads"] = 3  # forward 3: the sheet says crossed 74, so 3 loads can't be right
    issues = op.check(bad)
    assert any(i["step"] == "forward 3" and "Crossed should be" in i["message"] for i in issues)
    assert any("forward steps add up to" in i["message"] for i in issues)

    bad = copy.deepcopy(p)
    bad["steps"][1]["stop"] = "11X"
    assert any("aren't valid" in i["message"] for i in op.check(bad))

    bad = copy.deepcopy(p)
    bad["steps"][4]["end_feet"] = 52.0
    assert any("outside the 48 ft pattern" in i["message"] for i in op.check(bad))

    bad = copy.deepcopy(p)
    bad["volume_ml"] = 31.2
    assert any("volume" in i["message"] for i in op.check(bad))

    bad = copy.deepcopy(p)
    bad["category"] = "pro"
    assert any("Category" in i["message"] for i in op.check(bad))


def test_zero_length_step_lands_in_its_row():
    steps = op.normalize_steps(
        [{"direction": "forward", "order": 1, "start": "10L", "stop": "10L", "loads": 1, "start_feet": 5.0, "end_feet": 5.0}], 50
    )
    prof = op.profile(steps, 10, 50)
    assert prof["rows"][5][9] == 50.0
