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
  * Oil in microliters (µL); a step loads loads x oil_per_board_ul for
    every board in its range onto the transfer brush, evenly over its
    distance [min(start, end), max(start, end)]; the transfer brush ->
    buffer brush -> lane carry
    model (see profile) decides where on the lane it ends up, dragged in
    each run's direction of travel.
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


# Brush-carry model (runbook 6ek). Al: "in the machines the oil is applied
# to a spinning brush and then is buffed onto the lane. so oil applied is
# thick where it is first applied and then tapered out over a distance ...
# think of it dragging it in the direction of the application and then
# getting thinner as it goes without more application. forward is tapered
# forward and reverse blends in with the forward and tapers backwards" and
# "really it is applied to a rod then to the transfer brush then the lane so
# the brush is pulling it from the rod that it got applied to then putting
# it on the lane", then, with a diagram of the head (oil head -> nozzle ->
# transfer brush -> buffer brush -> lane surface): "so not a rod but
# something called a transfer brush down to the buffer brush. your new
# output looks more accurate".
#
# So each run (forward, then reverse) is two reservoirs in series that move
# with the head: a step's loads come out of the nozzle onto the TRANSFER
# brush, evenly over the step's distance; each foot of travel the spinning
# BUFFER brush takes a share of what's on the transfer brush and lays a share
# of what it carries onto the LANE. Oil therefore
# lands just behind where it was loaded and drags out in the direction of
# travel, thinning as the head keeps moving with no new loads (the 0-load
# "buff" steps). The buffer brush turns at a fixed rpm, so the share per FOOT falls
# as the head moves faster: carry lengths scale with speed / REF_SPEED_IPS.
#
# Carry-over and burn pairs (runbook 6em). Al: "is there a way to use what
# the pattern would have done on the previous lane pass to determine what the
# residual oil would be carried to the next lane. typically the last few
# loads and the existing oil on the lane would still be left in the brush as
# the lane moves to the next lane. they will do 'burn pairs' which preloads
# everything so that the machine oils the first lane for use having oiled 2
# to 'precondition' itself". So nothing on the brushes is thrown away or
# rescaled: what's left at the far end of the forward run goes into the
# reverse run, and what's left at the foul line after the reverse run starts
# the NEXT lane's forward run (it goes down in the first feet). We run
# BURN_LANES lanes on clean brushes, then publish the next one -- the lane a
# bowler actually gets. In steady state each lane takes off exactly what's
# loaded, so every board still totals board_totals; on Elite 48 the second
# lane is already steady (the first, on clean brushes, is ~2% light because
# ~0.59 mL stays on the brushes). The admin preview can also show that first
# clean-brush lane. Oil the buffer brush lifts back off the lane was tried
# and left out: at any plausible rate it moved < 0.1 µL anywhere.
# A first-order physical model, not a lane-graph measurement; partners see
# it named in profile.model (v1 was the same carry without the carry-over:
# leftovers were rescaled into each run).
PROFILE_MODEL = "transfer_buffer_carry_v2"
BURN_LANES = 2
REF_SPEED_IPS = 14.0
TRANSFER_TO_BUFFER_FEET = 0.5  # carry length (1/e) transfer brush -> buffer brush at REF_SPEED_IPS
BUFFER_TO_LANE_FEET = 4.0  # carry length (1/e) buffer brush -> lane at REF_SPEED_IPS
_DX = 0.05  # integration step, feet


def _run_deposits(steps, n_cells, opb, direction):
    """Oil per cell per board loaded onto the transfer brush by one run's steps, plus the
    head speed over each cell (for the carry lengths)."""
    loads = [[0.0] * BOARDS for _ in range(n_cells)]
    speed = [None] * n_cells
    for s in steps:
        if s.get("direction") != direction or s.get("from_board") is None:
            continue
        a, b = _num(s.get("start_feet")), _num(s.get("end_feet"))
        if a is None or b is None:
            continue
        lo, hi = max(0.0, min(a, b)), max(0.0, max(a, b))
        i_lo = min(int(lo / _DX), n_cells - 1)
        i_hi = max(i_lo, min(int(math.ceil(hi / _DX - 1e-9)) - 1, n_cells - 1))
        v = _num(s.get("speed_ips")) or REF_SPEED_IPS
        for i in range(i_lo, i_hi + 1):
            speed[i] = v
        if not s.get("loads"):
            continue
        per_board = s["loads"] * opb
        cells = []
        if hi - lo < 1e-9:
            cells.append((i_lo, 1.0))
        else:
            for i in range(i_lo, i_hi + 1):
                seg = min(hi, (i + 1) * _DX) - max(lo, i * _DX)
                if seg > 0:
                    cells.append((i, seg / (hi - lo)))
        for i, frac in cells:
            row = loads[i]
            for board in range(s["from_board"] - 1, s["to_board"]):
                row[board] += per_board * frac
    return loads, speed


def _carry(lane, loads, speed, order, transfer, buffer):
    """Sweep the transfer brush -> buffer brush -> lane reservoirs along
    `order` (cell indexes in travel direction), adding to `lane` (oil per
    cell per board). `transfer`/`buffer` are the brushes' oil per board, in
    and out: whatever they hold at the end carries on to the next run."""
    last_speed = REF_SPEED_IPS
    for i in order:
        last_speed = speed[i] or last_speed
        k = last_speed / REF_SPEED_IPS
        to_buffer = 1 - math.exp(-_DX / (TRANSFER_TO_BUFFER_FEET * k))
        to_lane = 1 - math.exp(-_DX / (BUFFER_TO_LANE_FEET * k))
        row_in, row_out = loads[i], lane[i]
        for b in range(BOARDS):
            q = row_in[b]
            if q:
                transfer[b] += q
            if transfer[b] or buffer[b]:
                moved = transfer[b] * to_buffer
                transfer[b] -= moved
                buffer[b] += moved
                laid = buffer[b] * to_lane
                buffer[b] -= laid
                row_out[b] += laid


def lanes(steps: list, length_feet, oil_per_board_ul, count: int, foot_step: float = 1.0) -> list:
    """Rows (µL per board per foot) for `count` lanes oiled one after another
    by the same machine, starting on clean brushes: lanes(...)[0] is the
    first lane, [BURN_LANES] the one after the burn pair."""
    length = _num(length_feet) or 0
    opb = _num(oil_per_board_ul) or 0
    n_rows = max(1, math.ceil(length / foot_step)) if length > 0 else 1
    n_cells = max(1, int(round(n_rows * foot_step / _DX)))
    runs = []
    for direction, order in (("forward", range(n_cells)), ("reverse", range(n_cells - 1, -1, -1))):
        loads, speed = _run_deposits(steps, n_cells, opb, direction)
        runs.append((loads, speed, order))
    transfer, buffer = [0.0] * BOARDS, [0.0] * BOARDS
    out = []
    for _ in range(count):
        lane = [[0.0] * BOARDS for _ in range(n_cells)]
        for loads, speed, order in runs:
            _carry(lane, loads, speed, order, transfer, buffer)
        rows = [[0.0] * BOARDS for _ in range(n_rows)]
        for i, cell in enumerate(lane):
            row = rows[min(int(i * _DX / foot_step + 1e-9), n_rows - 1)]
            for b, v in enumerate(cell):
                if v:
                    row[b] += v
        out.append([[round(v, 1) for v in row] for row in rows])
    return out


def profile(steps: list, length_feet, oil_per_board_ul, foot_step: float = 1.0, _lanes: list | None = None) -> dict:
    """µL per board per foot on the lane after a burn pair, under the
    transfer/buffer brush carry model above."""
    series = _lanes or lanes(steps, length_feet, oil_per_board_ul, BURN_LANES + 1, foot_step)
    return {
        "foot_step": foot_step,
        "units": "ul_per_board_per_foot",
        "model": PROFILE_MODEL,
        "burn_lanes": BURN_LANES,
        "rows": series[BURN_LANES],
    }


def headline_ratio(track_zones):
    ratios = [_num(z.get("ratio")) for z in track_zones or []]
    ratios = [r for r in ratios if r is not None]
    return max(ratios) if ratios else None


def compute(pattern: dict) -> dict:
    """Normalized steps + every derived field, ready to store/publish."""
    steps = ordered(normalize_steps(pattern.get("steps") or [], pattern.get("oil_per_board_ul")))
    series = lanes(steps, pattern.get("length_feet"), pattern.get("oil_per_board_ul"), BURN_LANES + 1)
    return {
        "steps": steps,
        "board_totals": board_totals(steps, pattern.get("oil_per_board_ul")),
        "profile": profile(steps, pattern.get("length_feet"), pattern.get("oil_per_board_ul"), _lanes=series),
        # Admin preview only (not stored or published): the first lane on
        # clean brushes, before the burn pair.
        "first_lane_rows": series[0],
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
