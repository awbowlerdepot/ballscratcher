"""Reading an oil pattern sheet with Claude vision (runbook 6ej).

Pure functions, no AWS/DB: render_pages (PDF -> PNGs), build_request
(the Bedrock body), parse_response (the model's JSON -> pattern fields).
app.py wires them to S3, Bedrock and Postgres.

Kegel's sheets draw their step tables and charts as images, so the text
layer only has the header. Claude reads the rendered page instead and
returns the fields as JSON. Nothing it returns is trusted: admin_api's
oil_patterns.check recomputes every step and compares the totals with the
sheet's own header before a human can approve the pattern.
"""
import base64
import io
import json
import re

MODEL_ID = "global.anthropic.claude-sonnet-4-6"
RENDER_SCALE = 2.5  # ~180 dpi: small table digits stay legible
MAX_PAGES = 3

PROMPT = """You are transcribing a bowling lane oil pattern sheet (usually a Kegel machine sheet) into JSON for a database. Read every number exactly as printed. Do not compute, round, or fix anything; if a value is unreadable use null.

Return ONLY a JSON object with these keys:
{
  "name": pattern name as printed in the title,
  "machine_model": e.g. "Flex" (from the logo/title) or null,
  "length_feet": "Oil Pattern Distance",
  "reverse_brush_drop_feet": "Reverse Brush Drop",
  "oil_per_board_ul": "Oil Per Board" number in ul,
  "forward_ml": "Forward Oil Total" mL,
  "reverse_ml": "Reverse Oil Total" mL,
  "volume_ml": "Volume Oil Total" mL,
  "tank_configuration": e.g. "A Only",
  "tank_a_conditioner": string, "tank_b_conditioner": string,
  "cleaner_ratio_main": e.g. "5:1", "cleaner_ratio_back_end": e.g. "5:1",
  "cleaner_back_end_distance_feet": number,
  "buffer_rpm": {"1": rpm, "2": rpm, "3": rpm, "4": rpm},
  "forward_steps": [ one object per row of the FIRST (forward) table, top to bottom ],
  "reverse_steps": [ one object per row of the SECOND (reverse) table, top to bottom ],
  "track_zones": [ one object per column of the Track Zone Ratio table ]
}
Each step object: {"order": row number, "start": e.g. "2L", "stop": e.g. "11R", "loads": int, "speed": number, "buffer": int, "tank": e.g. "A", "crossed": int, "start_feet": number, "end_feet": number, "feet": number (keep its sign), "oil": number (the T.OIL column)}.
Each track zone: {"item": the header text e.g. "3L-7L:18L-18R", "description": e.g. "Outside:Middle", "ratio": number}.
Board labels are a number followed by L or R. Keep start/stop exactly as printed."""


def render_pages(pdf_bytes: bytes, scale: float = RENDER_SCALE, max_pages: int = MAX_PAGES) -> list:
    """PNG bytes for the first pages of the PDF."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_bytes)
    out = []
    for i in range(min(len(pdf), max_pages)):
        bitmap = pdf[i].render(scale=scale)
        buf = io.BytesIO()
        bitmap.to_pil().convert("RGB").save(buf, "PNG", optimize=True)
        out.append(buf.getvalue())
    return out


def build_request(pngs: list) -> str:
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(p).decode()}} for p in pngs]
    content.append({"type": "text", "text": PROMPT})
    return json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 6000, "temperature": 0,
                       "messages": [{"role": "user", "content": content}]})


def _json_object(text: str) -> dict:
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("The model didn't return JSON")
    return json.loads(text[start:end + 1])


def _zone(z: dict) -> dict:
    """'3L-7L:18L-18R' -> from/to/against_from/against_to."""
    item = str(z.get("item") or "")
    m = re.match(r"\s*(\d+[LR])\s*-\s*(\d+[LR])\s*:\s*(\d+[LR])\s*-\s*(\d+[LR])", item, re.I)
    out = {"label": z.get("description"), "ratio": z.get("ratio")}
    if m:
        out.update({"from": m.group(1).upper(), "to": m.group(2).upper(), "against_from": m.group(3).upper(), "against_to": m.group(4).upper()})
    else:
        out["item"] = item
    return out


# Title case for names (runbook 6er). Kegel sheets print titles in capitals
# ("PBA ELITE SERIES 37"), which Drilld's pattern picker showed as-is next to
# its own "House shot"; Al: "you can do title case going forward". Only a
# name that's (nearly) all capitals is changed -- a mixed-case name is kept
# as the sheet has it. Known acronyms stay upper; short joining words go
# lower except first; words with digits (a "WTBA2"-style code) are kept.
NAME_ACRONYMS = {"PBA", "PWBA", "USBC", "WTBA", "ABC", "WIBC", "ETBF", "BPAA", "NCAA", "PBA50", "PTQ", "TV", "USA",
                 "US", "UK", "NJCAA", "PBAR", "ITF", "ISBPA", "SR", "JR", "II", "III", "IV", "QB"}
NAME_MINOR = {"a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to", "vs"}


def tidy_name(name):
    if not name:
        return name
    letters = [c for c in name if c.isalpha()]
    if not letters or sum(c.isupper() for c in letters) / len(letters) < 0.8:
        return name

    def word(w, first):
        core = w.strip("()[]\"'.,:;-")
        if not core or any(c.isdigit() for c in core) or core.upper() in NAME_ACRONYMS:
            return w
        if not first and core.lower() in NAME_MINOR:
            return w.lower()
        return "-".join(part[:1].upper() + part[1:].lower() for part in w.split("-"))

    words = name.split(" ")
    return " ".join(word(w, i == 0) for i, w in enumerate(words))


def parse_response(text: str) -> dict:
    """The model's JSON -> oil_patterns fields (names as the spec's)."""
    d = _json_object(text)

    def steps(rows, direction):
        out = []
        for i, r in enumerate(rows or []):
            out.append({
                "direction": direction,
                "order": r.get("order") or i + 1,
                "start": r.get("start"), "stop": r.get("stop"),
                "loads": r.get("loads"), "speed_ips": r.get("speed"), "buffer": r.get("buffer"), "tank": r.get("tank"),
                "crossed": r.get("crossed"),
                "start_feet": r.get("start_feet"), "end_feet": r.get("end_feet"), "feet": r.get("feet"),
                "oil_ul": r.get("oil"),
            })
        return out

    machine = {k: d.get(k) for k in ("tank_configuration", "tank_a_conditioner", "tank_b_conditioner",
                                     "cleaner_ratio_main", "cleaner_ratio_back_end", "cleaner_back_end_distance_feet",
                                     "buffer_rpm") if d.get(k) is not None}
    if d.get("machine_model"):
        machine = {"make": "Kegel", "model": d["machine_model"], **machine}
    return {
        "name": tidy_name((d.get("name") or "").strip() or None),
        "length_feet": d.get("length_feet"),
        "reverse_brush_drop_feet": d.get("reverse_brush_drop_feet"),
        "oil_per_board_ul": d.get("oil_per_board_ul"),
        "forward_ml": d.get("forward_ml"), "reverse_ml": d.get("reverse_ml"), "volume_ml": d.get("volume_ml"),
        "machine": machine,
        "steps": steps(d.get("forward_steps"), "forward") + steps(d.get("reverse_steps"), "reverse"),
        "track_zones": [_zone(z) for z in d.get("track_zones") or []],
    }


# ---------------------------------------------------------------------------
# Kegel Pattern Library sheets (runbook 6es). Al: "kegel posts an alternative
# pattern sheet. the grid is a bit different and boards crossed is no longer
# displayed but is likely just a calculation" and "mics is published". These
# sheets (patternlibrary.kegel.net, e.g. "2022 Starting House Pattern") are
# real PDFs with a TEXT LAYER, unlike the older sheet whose tables are
# images, so they're read exactly from the text -- no vision model, no
# misreads. Per row: # START STOP LOADS MICS SPEED BUFF TANK DISTANCE T.OIL,
# with DISTANCE as "a→b" and no crossed column (oil_patterns computes it;
# T.OIL = boards x loads x MICS is kept as the sheet's value to check).
# The zone ratios are drawn into the graph (not text), so they come from the
# loads only. Anything that doesn't look like this format returns None and
# the vision reader handles the sheet.
TEXT_MODEL_ID = "text-layer:kegel-pattern-library"
_ROW_RE = re.compile(
    r"^\s*(\d+)\s+(\d{1,2}[LR])\s+(\d{1,2}[LR])\s+(\d+)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(\d+)\s+"
    r"([AB])\s*-\s*(.+?)\s+(\d+(?:\.\d+)?)\s*(?:→|->)\s*(\d+(?:\.\d+)?)\s+([\d,]+)\s*$"
)
_NUM = r"(\d+(?:\.\d+)?)"


def text_layer(pdf_bytes: bytes, max_pages: int = MAX_PAGES) -> str:
    """The PDF's own text (empty for image-only sheets)."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_bytes)
    return "\n".join(pdf[i].get_textpage().get_text_range() for i in range(min(len(pdf), max_pages)))


def parse_pattern_library(text: str):
    """Fields from a Kegel Pattern Library sheet's text layer, or None if the
    text isn't that format (or doesn't hold a complete program)."""
    if not text or "FORWARD LOADS DATA" not in text or "REVERSE LOADS DATA" not in text or "MICS" not in text:
        return None
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n")]
    head = "\n".join(lines[: lines.index("FORWARD LOADS DATA")]) if "FORWARD LOADS DATA" in lines else text
    feet = [float(x) for x in re.findall(_NUM + r"\s*FEET", head)]
    mls = [float(x) for x in re.findall(_NUM + r"\s*mL", head)]
    pump = re.search(r"([A-Z][A-Z0-9 &.-]*?)\s+" + _NUM + r"\s*[µμu]L", head)
    if len(feet) < 2 or len(mls) < 3 or not pump:
        return None

    steps, direction, conditioners = [], None, set()
    for ln in lines:
        if ln == "FORWARD LOADS DATA":
            direction = "forward"
            continue
        if ln == "REVERSE LOADS DATA":
            direction = "reverse"
            continue
        m = _ROW_RE.match(ln) if direction else None
        if not m:
            continue
        order, start, stop, loads, mics, speed, buff, tank, cond, a, b, oil = m.groups()
        conditioners.add((tank, cond.strip()))
        a, b = float(a), float(b)
        steps.append({
            "direction": direction, "order": int(order), "start": start, "stop": stop, "loads": int(loads),
            "mics": float(mics), "speed_ips": float(speed), "buffer": int(buff), "tank": tank,
            "start_feet": a, "end_feet": b, "feet": round(b - a, 2), "oil_ul": int(oil.replace(",", "")),
        })
    if not any(s["direction"] == "forward" for s in steps) or not any(s["direction"] == "reverse" for s in steps):
        return None

    # The title is the line just before the app banner.
    name = None
    if "KEGEL PATTERN LIBRARY APP" in lines:
        i = lines.index("KEGEL PATTERN LIBRARY APP")
        name = next((lines[j] for j in range(i - 1, -1, -1) if lines[j]), None)
    machine = {"make": "Kegel"}
    for tank, cond in sorted(conditioners):
        machine[f"tank_{tank.lower()}_conditioner"] = cond
    tanks = sorted({t for t, _ in conditioners})
    machine["tank_configuration"] = f"{' + '.join(tanks)} Only" if len(tanks) == 1 else " + ".join(tanks)
    name = tidy_name(name)
    return {
        "name": name,
        # Kegel's library files house shots as such; anything else is left
        # for the reviewer to categorize.
        "category": "house" if name and "house" in name.lower() else None,
        "length_feet": feet[0],
        "reverse_brush_drop_feet": feet[1],
        "oil_per_board_ul": float(pump.group(2)),
        "volume_ml": mls[0], "forward_ml": mls[1], "reverse_ml": mls[2],
        "machine": machine,
        "steps": steps,
        "track_zones": [],  # the standard six, computed from the loads
    }
