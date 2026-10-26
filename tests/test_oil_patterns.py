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
    assert prof["model"] == "transfer_buffer_carry_v3" and prof["burn_lanes"] == 2
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
        [{"direction": "reverse", "order": 1, "start": "10L", "stop": "10L", "loads": 1, "start_feet": 20.0, "end_feet": 20.0}], 50
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
    first, second, third = op.lanes(steps, 48, 50, 3, reverse_brush_drop_feet=42)
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


def test_step_past_the_pattern_end_lands_in_the_last_row():
    """Spec change 2 (drilld PR #167 found it dropped): a step running past
    length_feet still loads all its oil; the overrun goes in at the end."""
    steps = op.normalize_steps(
        [{"direction": "forward", "order": 1, "start": "10L", "stop": "10L", "loads": 1, "start_feet": 8.0, "end_feet": 12.0}], 50
    )
    loads, _ = op._run_deposits(steps, int(10 / op._DX), 50, "forward")
    assert abs(sum(r[9] for r in loads) - 50) < 1e-9
    assert abs(loads[-1][9] - 25 - 50 * op._DX / 4) < 1e-9
    burned = op.profile(steps, 10, 50)["rows"]
    assert abs(sum(r[9] for r in burned) - 50) < 0.5


def test_reverse_brush_drop_holds_oil_until_the_drop():
    """Runbook 6en: on the reverse run the buffer brush is up until
    reverse_brush_drop_feet, so reverse loads before the drop go down from
    the drop toward the foul line."""
    steps = op.normalize_steps(
        [{"direction": "reverse", "order": 1, "start": "10L", "stop": "10L", "loads": 1, "start_feet": 30.0, "end_feet": 26.0}], 50
    )
    no_drop = [r[9] for r in op.lanes(steps, 30, 50, 1)[0]]
    drop = [r[9] for r in op.lanes(steps, 30, 50, 1, reverse_brush_drop_feet=22)[0]]
    assert sum(no_drop[22:]) > 20  # brush down: most goes down where it's loaded
    assert sum(drop[22:]) == 0 and drop[21] > drop[15] > 0  # brush up until 22 ft
    # Through compute(): the pattern's own field is used.
    p = elite48()
    assert op.compute(p)["profile"]["rows"] != op.profile(
        op.ordered(op.normalize_steps(p["steps"], 50)), 48, 50
    )["rows"]


def test_track_zone_ratios_come_from_the_loads():
    """Runbook 6ep: Kegel's ratios are the zones' average total oil per
    board; the computed values match every Elite 48 sheet value."""
    p = elite48()
    c = op.compute(p)
    assert [z["ratio"] for z in c["track_zones"]] == [2.0, 1.59, 1.03, 1.01, 1.43, 2.0]
    assert [z["reported_ratio"] for z in c["track_zones"]] == [2.0, 1.59, 1.03, 1.01, 1.43, 2.0]
    assert c["ratio"] == 2.0 and op.check(p) == []


def test_misread_ratio_is_flagged_and_the_loads_win():
    p = elite48()
    p["track_zones"][1]["ratio"] = 1.95  # OCR misread of 1.59
    c = op.compute(p)
    assert c["track_zones"][1]["ratio"] == 1.59 and c["track_zones"][1]["reported_ratio"] == 1.95
    issues = op.check(p)
    assert [i["level"] for i in issues] == ["warning"]
    assert "the loads give 1.59; the sheet reads 1.95" in issues[0]["message"]
    # Once stored, the sheet's value stays the reported one on recompute.
    again = op.compute({**p, "track_zones": c["track_zones"]})
    assert again["track_zones"][1]["reported_ratio"] == 1.95 and again["track_zones"][1]["ratio"] == 1.59


def test_typed_pattern_gets_the_standard_zones():
    p = elite48()
    p["track_zones"] = []
    zones = op.compute(p)["track_zones"]
    assert [z["label"] for z in zones] == [
        "Outside:Middle", "Middle:Middle", "Inside:Middle", "Middle:Inside", "Middle:Middle", "Middle:Outside"
    ]
    assert [z["ratio"] for z in zones] == [2.0, 1.59, 1.03, 1.01, 1.43, 2.0]
    assert all(z["reported_ratio"] is None for z in zones)


def test_zones_are_fixed_to_the_standard_six():
    """Runbook 6eq: zone boards can't be edited; a sheet zone that isn't
    one of the six is warned about and dropped, a bad label too."""
    p = elite48()
    p["track_zones"][0]["to"] = "7X"  # misread label: no longer a standard zone
    p["track_zones"].append({"from": "1L", "to": "5L", "against_from": "18L", "against_to": "18R", "ratio": 3.1})
    issues = op.check(p)
    assert [i["level"] for i in issues] == ["warning", "warning"]
    assert all("isn't one of the six standard zones" in i["message"] for i in issues)
    zones = op.compute(p)["track_zones"]
    assert [(z["from"], z["to"]) for z in zones] == [("3L", "7L"), ("8L", "12L"), ("13L", "17L"), ("18L", "18R"), ("18L", "18R"), ("18L", "18R")]
    assert zones[0]["reported_ratio"] is None and zones[0]["ratio"] == 2.0  # still computed
    assert zones[1]["reported_ratio"] == 1.59


def test_sheet_zone_written_the_other_way_round_still_matches():
    p = elite48()
    z = p["track_zones"][0]
    z["from"], z["to"], z["against_from"], z["against_to"] = "18R", "18L", "7L", "3L"
    assert op.check(p) == []
    assert op.compute(p)["track_zones"][0]["reported_ratio"] == 2.0


def test_step_mics_override_the_pattern_oil_per_board():
    """Runbook 6es: Pattern Library sheets print MICS per step; a step's own
    mics drives its oil, board totals and profile."""
    steps = [
        {"direction": "forward", "order": 1, "start": "10L", "stop": "10L", "loads": 1, "mics": 30, "start_feet": 0.0, "end_feet": 5.0},
        {"direction": "forward", "order": 2, "start": "11L", "stop": "11L", "loads": 1, "start_feet": 5.0, "end_feet": 10.0},
    ]
    n = op.normalize_steps(steps, 50)
    assert [(s["mics"], s["oil_ul"]) for s in n] == [(30.0, 30), (50.0, 50)]
    bt = op.board_totals(n, 50)
    assert bt["forward_ul"][9] == 30 and bt["forward_ul"][10] == 50
    rows = op.profile(n, 20, 50)["rows"]
    assert abs(sum(r[9] for r in rows) - 30) < 0.5 and abs(sum(r[10] for r in rows) - 50) < 0.5
    # No pattern-wide value is fine when every loaded step has its own.
    p = {"name": "x", "category": "house", "length_feet": 20, "oil_per_board_ul": None, "steps": [dict(steps[0])]}
    assert not any("Oil per board" in i["message"] for i in op.check(p))
    p["steps"].append(dict(steps[1]))
    assert any("Oil per board" in i["message"] for i in op.check(p))


def test_pattern_library_house_sheet_adds_up():
    """The 2022 Starting House Pattern (Kegel Pattern Library format): every
    check passes and all six zone ratios match the sheet's graph."""
    import json, os  # noqa: E401
    p = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "pattern_house_2022.json")))
    c = op.compute(p)
    assert op.check(p) == []
    assert [z["ratio"] for z in c["track_zones"]] == [12.0, 1.94, 1.0, 1.0, 1.94, 12.0] and c["ratio"] == 12.0
    assert abs(sum(map(sum, c["profile"]["rows"])) - 25400) < 45
