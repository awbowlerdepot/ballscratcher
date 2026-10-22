"""
Oil pattern model (runbook 6ej; spec docs/partner-api-v1-patterns.md).

Drilld proposed an oil pattern catalog in the partner API, so Flare can roll
a ball over the oil a real pattern lays down. Al: "go with your suggestion
... pulling in the patterns and using OCR is fine but they need approved
before they are added to the live catalog. similar pattern to the videos.
manual uploads of patterns as the first way they get pulled in".

This module is the ONE place a sheet's machine program becomes the numbers
partners get: board numbers, each step's crossings and oil, board totals,
the per-board-per-foot `profile` grid, the headline ratio, and the checks
that compare all of it with the sheet's own header totals. admin_api runs it
on every save and before approval, so what's published is always computed
the same way (the spec's "every partner shares one interpretation").

Conventions (from the spec):
  * Boards 1-39 from the left as the bowler faces the pins; Kegel labels
    convert as nL -> n, nR -> 40 - n.
  * Oil in microliters (µL); a step lays loads x oil_per_board_ul on every
    board in its range, spread evenly over its distance
    [min(start, end), max(start, end)]. Forward and reverse are the same
    for the profile; direction only orders the program.
  * Profile rows: ceil(length / foot_step); row i covers
    [i*step, (i+1)*step). Oil past the last row's end lands in the last row.
"""
import math
import re

BOARDS = 39
LABEL_RE = re.compile(r"^\s*(\d{1,2})\s*([LR])\s*$", re.IGNORECASE)
CATEGORIES = ("house", "sport", "challenge", "recreational", "tournament")
SOURCES = ("kegel", "pba", "usbc", "wtba", "brunswick", "center", "manual", "upload")
# OCR'd and typed numbers get this much slack against the sheet's totals.
ML_TOLERANCE = 0.051


def board_from_label(label) -> int:
    """'2L' -> 2, '11R' -> 29. Raises ValueError for anything else."""
    m = LABEL_RE.match(str(label or ""))
    if not m:
        raise ValueError(f"'{label}' isn't a board label like 2L or 11R")
    n, side = int(m.group(1)), m.group(2).upper()
    if not 1 <= n <= BOARDS:
        raise ValueError(f"'{label}' is off the lane (boards run 1L-39L / 1R-39R)")
    return n if side == "L" else 40 - n


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_steps(steps: list, oil_per_board_ul) -> list:
    """Fills from_board/to_board/crossed/oil_ul from each step's labels and
    loads. The sheet's own crossed/oil values are kept as reported_* so the
    checks can compare them."""
    opb = _num(oil_per_board_ul) or 0
    out = []
    for s in steps or []:
        step = dict(s)
        step["direction"] = (step.get("direction") or "forward").lower()
        try:
            a, b = board_from_label(step.get("start")), board_from_label(step.get("stop"))
            lo, hi = min(a, b), max(a, b)
            step["from_board"], step["to_board"] = lo, hi
        except ValueError:
            step["from_board"] = step["to_board"] = None
        loads = int(_num(step.get("loads")) or 0)
        step["loads"] = loads
        if "reported_crossed" not in step and step.get("crossed") is not None:
            step["reported_crossed"] = step.get("crossed")
        if "reported_oil_ul" not in step and step.get("oil_ul") is not None:
            step["reported_oil_ul"] = step.get("oil_ul")
        if step["from_board"] is not None:
            step["crossed"] = (step["to_board"] - step["from_board"] + 1) * loads
            step["oil_ul"] = int(round(step["crossed"] * opb))
        for k in ("start_feet", "end_feet", "feet", "speed_ips"):
            if step.get(k) is not None:
                step[k] = _num(step[k])
        out.append(step)
    return out


def ordered(steps: list) -> list:
    """Program order: forward steps by order, then reverse steps by order."""
    return sorted(steps, key=lambda s: (0 if s.get("direction") == "forward" else 1, int(_num(s.get("order")) or 0)))


def board_totals(steps: list, oil_per_board_ul) -> dict:
    opb = _num(oil_per_board_ul) or 0
    fwd, rev = [0.0] * BOARDS, [0.0] * BOARDS
    for s in steps:
        if s.get("from_board") is None or not s.get("loads"):
            continue
        target = fwd if s.get("direction") == "forward" else rev
        for board in range(s["from_board"], s["to_board"] + 1):
            target[board - 1] += s["loads"] * opb
    return {"forward_ul": [int(round(v)) for v in fwd], "reverse_ul": [int(round(v)) for v in rev]}


def profile(steps: list, length_feet, oil_per_board_ul, foot_step: float = 1.0) -> dict:
    """µL per board per foot (spec change #2/#3)."""
    length = _num(length_feet) or 0
    opb = _num(oil_per_board_ul) or 0
    n_rows = max(1, math.ceil(length / foot_step)) if length > 0 else 1
    rows = [[0.0] * BOARDS for _ in range(n_rows)]
    for s in steps:
        if s.get("from_board") is None or not s.get("loads"):
            continue
        per_board = s["loads"] * opb
        a, b = _num(s.get("start_feet")), _num(s.get("end_feet"))
        if a is None or b is None:
            continue
        lo, hi = min(a, b), max(a, b)
        spans = []
        if hi - lo < 1e-9:
            spans.append((min(int(lo // foot_step), n_rows - 1), 1.0))
        else:
            r = int(lo // foot_step)
            while r * foot_step < hi - 1e-9:
                seg_lo, seg_hi = max(lo, r * foot_step), min(hi, (r + 1) * foot_step)
                if seg_hi > seg_lo:
                    spans.append((min(r, n_rows - 1), (seg_hi - seg_lo) / (hi - lo)))
                r += 1
        for row, frac in spans:
            for board in range(s["from_board"], s["to_board"] + 1):
                rows[row][board - 1] += per_board * frac
    return {
        "foot_step": foot_step,
        "units": "ul_per_board_per_foot",
        "rows": [[round(v, 1) for v in row] for row in rows],
    }


def headline_ratio(track_zones):
    ratios = [_num(z.get("ratio")) for z in track_zones or []]
    ratios = [r for r in ratios if r is not None]
    return max(ratios) if ratios else None


def compute(pattern: dict) -> dict:
    """Normalized steps + every derived field, ready to store/publish."""
    steps = ordered(normalize_steps(pattern.get("steps") or [], pattern.get("oil_per_board_ul")))
    return {
        "steps": steps,
        "board_totals": board_totals(steps, pattern.get("oil_per_board_ul")),
        "profile": profile(steps, pattern.get("length_feet"), pattern.get("oil_per_board_ul")),
        "ratio": headline_ratio(pattern.get("track_zones")),
        "profile_source": "steps",
    }


def check(pattern: dict) -> list:
    """Problems to fix before approval: [{"level": "error"|"warning",
    "step": "forward 3" | None, "message": str}]. Errors block approval."""
    issues = []

    def add(level, message, step=None):
        issues.append({"level": level, "step": step, "message": message})

    if not (pattern.get("name") or "").strip():
        add("error", "The pattern needs a name.")
    if pattern.get("category") not in CATEGORIES:
        add("error", f"Category must be one of: {', '.join(CATEGORIES)}.")
    length = _num(pattern.get("length_feet"))
    opb = _num(pattern.get("oil_per_board_ul"))
    if not length or length <= 0 or length > 70:
        add("error", "Pattern length must be between 1 and 70 feet.")
    if not opb or opb <= 0:
        add("error", "Oil per board (µL) is missing.")
    steps = normalize_steps(pattern.get("steps") or [], opb)
    if not steps:
        add("error", "There are no machine steps.")

    sums = {"forward": 0, "reverse": 0}
    for s in steps:
        label = f"{s.get('direction')} {s.get('order')}"
        if s.get("from_board") is None:
            add("error", f"Board labels '{s.get('start')}' to '{s.get('stop')}' aren't valid.", label)
            continue
        rc, ro = _num(s.get("reported_crossed")), _num(s.get("reported_oil_ul"))
        if rc is not None and int(rc) != s["crossed"]:
            add("error", f"Crossed should be {s['crossed']} ({s['to_board'] - s['from_board'] + 1} boards × {s['loads']} loads); the sheet reads {int(rc)}.", label)
        if ro is not None and int(round(ro)) != s["oil_ul"]:
            add("error", f"Oil should be {s['oil_ul']} µL; the sheet reads {int(round(ro))}.", label)
        a, b = _num(s.get("start_feet")), _num(s.get("end_feet"))
        if a is None or b is None:
            add("error", "Start and end distance are missing.", label)
        else:
            if length and (max(a, b) > length + 0.05 or min(a, b) < -0.05):
                add("error", f"Distance {a}–{b} ft runs outside the {length:g} ft pattern.", label)
            if s.get("direction") == "reverse" and a < b:
                add("warning", "A reverse step usually runs from the far end back (start > end).", label)
            if s.get("direction") == "forward" and a > b:
                add("warning", "A forward step usually runs toward the pins (start < end).", label)
        sums[s.get("direction") if s.get("direction") in sums else "forward"] += s["oil_ul"]

    # Steps should join up: each starts where the previous one ended.
    for direction in ("forward", "reverse"):
        seq = [s for s in ordered(steps) if s.get("direction") == direction]
        for prev, cur in zip(seq, seq[1:]):
            pe, cs = _num(prev.get("end_feet")), _num(cur.get("start_feet"))
            if pe is not None and cs is not None and abs(pe - cs) > 0.05:
                add("warning", f"Starts at {cs} ft but the previous step ended at {pe} ft.", f"{direction} {cur.get('order')}")

    for key, total in (("forward_ml", sums["forward"]), ("reverse_ml", sums["reverse"])):
        sheet = _num(pattern.get(key))
        name = key.split("_")[0]
        if sheet is None:
            add("warning", f"The sheet's {name} total is missing.")
        elif abs(total / 1000 - sheet) > ML_TOLERANCE:
            add("error", f"The {name} steps add up to {total / 1000:.2f} mL; the sheet says {sheet:g} mL.")
    vol = _num(pattern.get("volume_ml"))
    if vol is not None and abs((sums["forward"] + sums["reverse"]) / 1000 - vol) > ML_TOLERANCE:
        add("error", f"Forward + reverse is {(sums['forward'] + sums['reverse']) / 1000:.2f} mL; the sheet's volume is {vol:g} mL.")
    return issues
