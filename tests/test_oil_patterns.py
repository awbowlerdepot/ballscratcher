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
    assert prof["model"] == "transfer_buffer_carry_v2" and prof["burn_lanes"] == 2
    assert all(r[0] == 0 for r in prof["rows"])  # nothing loads board 1
    # Every board still gets exactly what the sheet loads (board 20: 1,100 µL).
    assert abs(sum(r[19] for r in prof["rows"]) - 1100) < 5
    # Transfer brush -> buffer brush -> lane carry (runbook 6ek): the forward loads end at
    # 16.7 ft but the brush keeps dragging them toward the pins, thinning;
    # under the old even spread board 3 was dry past 16.7 ft.
    b3 = [r[2] for r in prof["rows"]]
    assert b3[17] > 10 and b3[20] > b3[25] > b3[30] > b3[35] > 0
    # Oil lands just behind where it's loaded, so the foul line row is lighter
    # than the rows the first steps' oil drags into.
    assert b3[0] < b3[3]


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


def test_zero_length_step_drags_forward_from_its_row():
    steps = op.normalize_steps(
        [{"direction": "forward", "order": 1, "start": "10L", "stop": "10L", "loads": 1, "start_feet": 5.0, "end_feet": 5.0}], 50
    )
    first, _, burned = op.lanes(steps, 10, 50, 3)
    col = [r[9] for r in first]
    # Peaks just past where it's loaded and thins toward the pins; what's
    # still on the brushes at 10 ft comes back on the reverse run, so there is
    # a little behind it too, but less than at the step.
    assert col[5] > col[4] and col[6] > col[8] > 0
    # After the burn pair every lane takes off exactly what's loaded.
    assert abs(sum(r[9] for r in burned) - 50) < 0.5
    assert op.profile(steps, 10, 50)["rows"] == burned


def test_reverse_oil_drags_back_toward_the_foul_line():
    steps = op.normalize_steps(
        [{"direction": "reverse", "order": 1, "start": "10L", "stop": "10L", "loads": 1, "start_feet": 20.0, "end_feet": 20.0}], 30
    )
    first, _, burned = op.lanes(steps, 30, 50, 3)
    col = [r[9] for r in first]
    assert sum(col[21:]) == 0 and col[19] > col[15] > 0  # clean brushes: nothing past the step
    assert abs(sum(r[9] for r in burned) - 50) < 0.5


def test_burn_pair_conditions_the_brushes():
    """Runbook 6em: the first lane on clean brushes is light (oil stays on the
    brushes); from then on each lane gets what's loaded, and what the brushes
    carry from the previous lane goes down in the first feet."""
    p = elite48()
    steps = op.ordered(op.normalize_steps(p["steps"], 50))
    first, second, third = op.lanes(steps, 48, 50, 3)
    total = lambda rows: sum(map(sum, rows))  # noqa: E731
    assert total(first) < 30200 - 300
    assert abs(total(second) - 30200) < 50 and abs(total(third) - 30200) < 50
    assert third[0][2] > first[0][2] + 2  # board 3, first foot
    c = op.compute(p)
    assert c["profile"]["rows"] == third and c["first_lane_rows"] == first


def test_faster_head_drags_oil_further():
    def tail(speed):
        steps = op.normalize_steps(
            [{"direction": "forward", "order": 1, "start": "10L", "stop": "10L", "loads": 1, "speed_ips": speed,
              "start_feet": 2.0, "end_feet": 2.0}], 50
        )
        return sum(r[9] for r in op.profile(steps, 40, 50)["rows"][10:])

    assert tail(30) > tail(14)
