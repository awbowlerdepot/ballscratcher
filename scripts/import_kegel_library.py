#!/usr/bin/env python3
"""
Import the Kegel Pattern Library (patternlibrary.kegel.net) into oil_patterns
(runbook 6et).

Al: "https://patternlibrary.kegel.net/ do you think you can pull in all the
patterns from here... each one has a downloadable pdf. KOSI files are there
too, which is the format for the lane machines. that might be helpful for
future use", then "go ahead, i have already mentioned this and they said
they it would be great. clean patterns for this case should be bulk
approved."

The library's own JSON API serves everything structured, so no PDF reading:
  GET /api/v1/patterns/all   -> every pattern's summary (id, name, hash-free)
  GET /api/v1/patterns/{id}  -> the full program: forward/reverse load
                                screens (mics per step, crossed, T.OIL),
                                Kegel's zone ratios, conditioners, drop
                                brush, keywords, the KOSI file text
Two phases, so mapping changes never re-download:
  fetch   one request a second; caches each pattern's JSON (images stripped)
          under --cache. Re-runs only fetch ids not cached yet.
  import  maps each cached pattern (map_pattern), runs oil_patterns.check,
          writes it with the derived fields, saves the raw JSON + KOSI file
          to the private PatternSheetsBucket (patterns/kegel/{id}.*), and
          BULK-APPROVES the clean ones: no check errors or warnings, Kegel's
          HasErrors false, and no machine feature our model doesn't cover
          (forward drop brush, raise brush at the end of the pattern).
          Everything else stays pending with the reason in review_note.
          Rows already imported (same Kegel id) are updated only when
          Kegel's HashSignature changed, and an approved row is never
          un-approved by a re-import.

Usage (PG* from the bowling-scraper-db secret; AWS creds for S3):
    .venv/bin/python scripts/import_kegel_library.py fetch --cache DIR
    .venv/bin/python scripts/import_kegel_library.py import --cache DIR          # dry run
    .venv/bin/python scripts/import_kegel_library.py import --cache DIR --apply
    # link patterns we already had from uploaded sheets to their Kegel ids:
    ... import --cache DIR --apply --link KEGEL_ID=OUR_ID [--link ...]
"""
import argparse
import json
import os
import re
import sys
import time

import requests

BASE = "https://patternlibrary.kegel.net"
UA = "BowlerIQ pattern import (bowleriq.com; with Kegel's OK)"
IMAGE_KEYS = ("Logo", "PatternImage", "CompositeImage")


def _get(path: str):
    r = requests.get(BASE + path, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=60)
    r.raise_for_status()
    return r.json()


def fetch(cache: str, delay: float = 1.0, limit: int = None) -> None:
    os.makedirs(cache, exist_ok=True)
    summary = _get("/api/v1/patterns/all")
    for s in summary:
        s.pop("Logo", None)
    json.dump(summary, open(os.path.join(cache, "_all.json"), "w"))
    todo = [s["Id"] for s in summary if not os.path.exists(os.path.join(cache, f"{s['Id']}.json"))]
    print(f"{len(summary)} patterns in the library; {len(todo)} to fetch", flush=True)
    for n, pid in enumerate(todo[:limit] if limit else todo, 1):
        for attempt in range(3):
            try:
                d = _get(f"/api/v1/patterns/{pid}")
                break
            except Exception as e:  # noqa: BLE001 -- retry, then skip
                print(f"  {pid}: {e} (attempt {attempt + 1})", flush=True)
                time.sleep(5 * (attempt + 1))
        else:
            continue
        for k in IMAGE_KEYS:
            d.pop(k, None)
            (d.get("Pattern") or {}).pop(k, None)
        tmp = os.path.join(cache, f"{pid}.json.tmp")
        json.dump(d, open(tmp, "w"))
        os.replace(tmp, os.path.join(cache, f"{pid}.json"))
        if n % 50 == 0:
            print(f"  {n}/{len(todo)}", flush=True)
        time.sleep(delay)
    print("fetch done", flush=True)


# ---------------------------------------------------------------------------
# Mapping Kegel's JSON onto oil_patterns (the spec's fields).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "admin_api"))
import oil_patterns  # noqa: E402

TOUR_GROUPS = {"PBA PATTERNS", "PWBA PATTERNS", "USBC PATTERNS", "INTERNATIONAL"}
IMPORTER = "kegel-import"
READER = "kegel-pattern-library-api"


def _category(p: dict) -> str:
    kw = {k.lower() for k in p.get("Keywords") or []}
    name = (p.get("Name") or "").lower()
    if "house" in kw or "house" in name:
        return "house"
    for k, cat in (("sport", "sport"), ("challenge", "challenge"), ("recreation", "recreational"),
                   ("recreational", "recreational")):
        if k in kw:
            return cat
    return "tournament" if (p.get("Group") or "").upper() in TOUR_GROUPS else "sport"


def _zones(p: dict) -> list:
    out = []
    for r in p.get("Ratios") or []:
        if r.get("Zone") != "TrackZone" or ":" not in (r.get("Header") or ""):
            continue
        left, right = [x.strip() for x in r["Header"].split(":", 1)]
        try:
            f, t = [x.strip() for x in left.split("-")]
            af, at = [x.strip() for x in right.split("-")]
        except ValueError:
            continue
        out.append({"from": f, "to": t, "against_from": af, "against_to": at, "label": r.get("Description"),
                    "ratio": round(float(r["Ratio"]), 2) if r.get("Ratio") is not None else None})
    # Only the standard six (anything else would just be dropped with a warning).
    std = {oil_patterns._standard_key(z) for z in oil_patterns.STANDARD_ZONES}
    return [z for z in out if oil_patterns._zone_key(z) in std]


def _ml(v):
    return round(float(v), 3) if v is not None else None


def map_pattern(d: dict) -> dict:
    import html

    p = d["Pattern"]
    steps = []
    for direction, key in (("forward", "ForwardLoadscreens"), ("reverse", "ReverseLoadscreens")):
        for s in p.get(key) or []:
            if not s.get("UIisVisible"):
                continue
            steps.append({
                "direction": direction, "order": s["Index"], "start": s["UIStart"], "stop": s["UIStop"],
                "loads": s["Loads"], "mics": s["Microliter"], "speed_ips": s["SpeedIps"], "buffer": s["BuffSpeed"],
                "tank": s.get("TankDescription"), "crossed": s.get("Crossed"),
                "start_feet": s["StartDistance"], "end_feet": s["EndDistance"], "feet": s.get("Feet"),
                "oil_ul": int(round(s["TotalOil"])) if s.get("TotalOil") is not None else None,
            })
    desc = html.unescape(p.get("Description") or "").strip() or None
    model = re.search(r"Designed for ([A-Za-z0-9 ]+?)\s*\|", desc or "")
    tanks = sorted({s["tank"] for s in steps if s.get("tank")})
    machine = {"make": "Kegel"}
    if model:
        machine["model"] = model.group(1).strip().title()
    machine["tank_configuration"] = " + ".join(tanks) if len(tanks) > 1 else f"{tanks[0]} Only" if tanks else None
    if p.get("TankAConditioner"):
        machine["tank_a_conditioner"] = p["TankAConditioner"]
    if p.get("IsDualTanks") and p.get("TankBConditioner"):
        machine["tank_b_conditioner"] = p["TankBConditioner"]
    rpm = {str(s["BuffSpeed"]): s["BuffSpeedRPM"] for key in ("ForwardLoadscreens", "ReverseLoadscreens")
           for s in p.get(key) or [] if s.get("UIisVisible") and s.get("BuffSpeedRPM")}
    if rpm:
        machine["buffer_rpm"] = dict(sorted(rpm.items()))
    pump = re.match(r"\s*(\d+(?:\.\d+)?)", p.get("PumpDescription") or "")
    kid = p["Id"]
    return {
        "name": (p.get("Name") or "").strip(),
        "category": _category(p),
        "series": p.get("Category"),
        "source": "kegel",
        "source_url": f"{BASE}/pattern/{kid}",
        "sheet_url": f"{BASE}/api/v1/patterns/download?id={kid}&format=pdf",
        "status": "current",
        "length_feet": p.get("Distance"),
        "reverse_brush_drop_feet": p.get("ReverseDropBrushDistance") if p.get("IsReverseDropBrush") else None,
        # Kegel's forward drop (9 or 12) has no unit and never appears on the
        # sheet; read as INCHES (runbook 6ey -- Al: "go with inches"), stored in feet.
        "forward_brush_drop_feet": round(p["ForwardDropBrushDistance"] / 12, 3)
        if p.get("IsForwardDropBrush") and p.get("ForwardDropBrushDistance") else None,
        "oil_per_board_ul": float(pump.group(1)) if pump else None,  # "MULTI": each step's mics
        # Kegel's JSON carries float noise (27.200000000000003); its sheets
        # print 3 places at most.
        "forward_ml": _ml(p.get("ForwardVolume")), "reverse_ml": _ml(p.get("ReverseVolume")), "volume_ml": _ml(p.get("Volume")),
        "machine": {k: v for k, v in machine.items() if v is not None},
        "steps": steps,
        "track_zones": _zones(p),
        "description": desc,
        # Runbook 6ex: Kegel's download count, published as a popularity signal.
        "download_count": int(d["DownloadCount"]) if d.get("DownloadCount") is not None else None,
        "external_source": "kegel",
        "external_id": kid,
        "external_hash": (p.get("Metadata") or {}).get("HashSignature"),
    }


def not_clean_reasons(d: dict, pattern: dict) -> list:
    """Why a pattern can't be bulk-approved ([] = clean)."""
    p = d["Pattern"]
    reasons = [f"{i['level']}: {(i['step'] + ': ') if i.get('step') else ''}{i['message']}" for i in oil_patterns.check(pattern)]
    # Kegel's own oil per board (ForwardData/ReverseData) must match ours.
    bt = oil_patterns.compute(pattern)["board_totals"]
    for ours, key in ((bt["forward_ul"], "ForwardData"), (bt["reverse_ul"], "ReverseData")):
        theirs = p.get(key) or []
        if len(theirs) == 39 and any(abs(a - b) > 1 for a, b in zip(ours, theirs)):
            board = next(i + 1 for i, (a, b) in enumerate(zip(ours, theirs)) if abs(a - b) > 1)
            reasons.append(f"error: {key[:-4].lower()} oil on board {board} is {ours[board - 1]} µL; Kegel's graph has {theirs[board - 1]:g}.")
            break
    if p.get("HasErrors"):
        reasons.append("Kegel flags this pattern as having errors.")
    if p.get("EndOfPatternRaiseBrush"):
        reasons.append("Raises the brush at the end of the pattern, which the oil model doesn't cover yet.")
    return reasons


WRITE_COLS = ("name", "category", "series", "source", "source_url", "sheet_url", "status", "length_feet",
              "reverse_brush_drop_feet", "forward_brush_drop_feet", "oil_per_board_ul", "forward_ml", "reverse_ml", "volume_ml", "machine",
              "steps", "track_zones", "board_totals", "profile", "ratio", "profile_source", "description",
              "download_count", "external_source", "external_id", "external_hash", "kosi_key", "raw_key")
JSON_COLS = {"machine", "steps", "track_zones", "board_totals", "profile"}
LINK_COLS = ("source", "source_url", "sheet_url", "description", "download_count", "external_source", "external_id",
             "external_hash", "kosi_key", "raw_key")


def run_import(cache: str, apply: bool = False, links: list = (), bucket: str = None) -> None:
    import collections

    import boto3
    import psycopg2

    bucket = bucket or os.environ.get("PATTERN_SHEETS_BUCKET") or "bowling-scraper-pattern-sheets-563981859606"
    link = dict(x.split("=", 1) for x in links)
    conn = psycopg2.connect()
    s3 = boto3.client("s3")
    with conn.cursor() as cur:
        cur.execute("select external_id, id, external_hash, review_status from oil_patterns where external_source = 'kegel'")
        existing = {r[0]: r[1:] for r in cur.fetchall()}
    files = sorted(f for f in os.listdir(cache) if f.endswith(".json") and not f.startswith("_"))
    stats, reasons_seen = collections.Counter(), collections.Counter()
    for f in files:
        d = json.load(open(os.path.join(cache, f)))
        m = map_pattern(d)
        kid = m["external_id"]
        c = oil_patterns.compute(m)
        reasons = not_clean_reasons(d, m)
        for r in reasons:
            reasons_seen[re.sub(r"\d+(\.\d+)?", "N", r.split(": ", 1)[-1])[:90]] += 1
        stats["clean" if not reasons else "pending"] += 1
        row = {**m, **{k: c[k] for k in ("steps", "track_zones", "board_totals", "profile", "ratio", "profile_source")},
               "kosi_key": f"patterns/kegel/{kid}.kosi.txt", "raw_key": f"patterns/kegel/{kid}.json"}
        if not apply:
            continue
        kosi = d.get("PatternFile")
        if kosi:
            s3.put_object(Bucket=bucket, Key=row["kosi_key"], Body=kosi.encode(), ContentType="text/plain; charset=utf-8")
        s3.put_object(Bucket=bucket, Key=row["raw_key"], Body=json.dumps(d).encode(), ContentType="application/json")
        vals = lambda cols: [json.dumps(row[k]) if k in JSON_COLS else row.get(k) for k in cols]  # noqa: E731
        with conn.cursor() as cur:
            if kid in link:  # a pattern we already had from an uploaded sheet
                cur.execute(f"update oil_patterns set {', '.join(f'{k} = %s' for k in LINK_COLS)} where id = %s",
                            vals(LINK_COLS) + [link[kid]])
                stats["linked"] += 1
            elif kid in existing:
                _, old_hash, status = existing[kid]
                if old_hash == m["external_hash"]:
                    # The program is the same; still refresh the download count.
                    cur.execute("update oil_patterns set download_count = %s where external_source = 'kegel' "
                                "and external_id = %s and download_count is distinct from %s",
                                (m["download_count"], kid, m["download_count"]))
                    stats["unchanged"] += 1
                    conn.commit()
                    continue
                cur.execute(f"update oil_patterns set {', '.join(f'{k} = %s' for k in WRITE_COLS)} where external_source = 'kegel' and external_id = %s",
                            vals(WRITE_COLS) + [kid])
                stats["updated"] += 1
            else:
                approved = not reasons
                cur.execute(
                    f"insert into oil_patterns ({', '.join(WRITE_COLS)}, review_status, published, review_note, resolved_at, "
                    "resolved_by, extraction_status, extraction_model, extracted_at, created_by) "
                    f"values ({', '.join(['%s'] * len(WRITE_COLS))}, %s, %s, %s, case when %s then now() end, %s, 'ready', %s, now(), %s)",
                    vals(WRITE_COLS) + ["approved" if approved else "pending", approved,
                                        None if approved else "Not bulk-approved: " + " | ".join(reasons)[:1900],
                                        approved, f"{IMPORTER} (all checks pass)" if approved else None, READER, IMPORTER],
                )
                stats["inserted"] += 1
        conn.commit()
    conn.close()
    print(dict(stats))
    print("not-clean reasons (pattern counts):")
    for r, n in reasons_seen.most_common(25):
        print(f"  {n:5}  {r}")
    if not apply:
        print("(dry run; --apply to write)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=("fetch", "import"))
    ap.add_argument("--cache", required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--link", action="append", default=[])
    a = ap.parse_args()
    if a.phase == "fetch":
        fetch(a.cache, limit=a.limit)
    else:
        run_import(a.cache, apply=a.apply, links=a.link)
