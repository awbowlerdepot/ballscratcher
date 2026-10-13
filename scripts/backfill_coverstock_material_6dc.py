#!/usr/bin/env python3
"""
One-off backfill (DEPLOY_RUNBOOK.md 6dc): infer coverstock_material for the
482 balls that had none, then re-estimate ONLY those balls' plotter positions.

Al: "yes audit the 482 balls missing cover material" / "yes go ahead with
all three". 477 are retired: retired manufacturer pages mostly omit the
"Cover Type" field the scrapers read material from, so even DV8's
"Polyester" line had none, and proprietary reactive cover names ("NBT
Pearl", "GB 10.7", "MOTIVator-Z") matched no keyword. Retired balls aren't
on the public plotter, but their position drives the retired -> current
replacement suggestions (6cl), so a retired polyester spare plotted
mid-chart was getting reactive balls as its replacements.

Decision order per ball (first hit wins):
  1. cover name keyword  (polyester/poly/plastic/polythane -> polyester;
     urethane -> urethane, except Brunswick's "Better Than Urethane" BTU,
     which is reactive; reactive/resin -> reactive)
  2. ball name keyword   (same), then "Spare" or "Clear" -> polyester
  3. description         -- polyester ONLY (urethane/reactive in copy is
     usually a comparison: MOTIV Blue Tank's mentions urethane, its
     Frixion cover is reactive)
  4. manufacturer page   -- checked by hand for the balls with no cover
     name (PAGE below)
  5. a named proprietary cover -> reactive
  6. Radical / Storm (performance-only brands) -> reactive
  7. otherwise a performance model with no published material -> reactive

Dry run by default (writes a CSV); --apply writes coverstock_material on
the products (and on their coverstocks rows where blank), then
re-estimates only these products with admin_api's own v3 estimator (same
as the Batch Jobs "Re-estimate plotter positions" button: polyester is
pinned at oil 1 / motion 1). Never touches chart or manual positions.

Usage:
    python3 scripts/backfill_coverstock_material_6dc.py out.csv
    python3 scripts/backfill_coverstock_material_6dc.py out.csv --apply
"""
import csv
import json
import os
import re
import sys

import boto3
import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "admin_api"))

POLY = re.compile(r"polyester|\bpoly\b|plastic|polythane|polyster", re.I)
URE = re.compile(r"urethane", re.I)
REACT = re.compile(r"reactive|\bresin\b", re.I)
def from_text(t):
    if not t: return None
    if re.search(r"better than urethane", t, re.I): return "reactive_resin"   # Brunswick "BTU"
    if POLY.search(t): return "polyester_plastic"
    if URE.search(t): return "urethane"
    if REACT.search(t): return "reactive_resin"
    return None
def from_desc(d):
    """Only an explicit cover statement in the description, e.g. 'polyester coverstock', 'urethane cover'."""
    m = re.search(r"(polyester|plastic|polythane|urethane|reactive)[^.]{0,30}\bcover", d, re.I) or \
        re.search(r"\bcover(?:stock)?\b[^.]{0,30}(polyester|plastic|polythane|urethane|reactive)", d, re.I)
    # Polyester only: urethane/reactive in a description is usually a comparison
    # (MOTIV Blue Tank's copy mentions urethane; its Frixion cover is reactive).
    v = from_text(m.group(0)) if m else None
    return v if v == "polyester_plastic" else None
def classify(conn):
    cur = conn.cursor()
    cur.execute("""select p.id::text, b.name, p.name, p.coverstock_name, p.status, coalesce(p.description,''), p.oil_rating, p.motion_rating
               from products p join brands b on b.id=p.brand_id
               where p.product_type='ball' and p.coverstock_material is null order by b.name, p.name""")
    rows = []
    PAGE = {("Ebonite","Angry Birds"):"polyester_plastic", ("Ebonite","Maxim - Blazing Fire"):"polyester_plastic",
            ("Ebonite","Maxim - Cool Water"):"polyester_plastic", ("Hammer","Carbon Fiber"):"polyester_plastic",
            ("Hammer","Skulz"):"polyester_plastic", ("Track","Sugar Ray Leonard Boxing Glove"):"polyester_plastic",
            ("Ebonite","Game Changer"):"reactive_resin", ("Ebonite","Tornado - Navy / Gold / Red"):"reactive_resin",
            ("Hammer","Raw Hammer Doom"):"reactive_resin", ("Track","715A"):"reactive_resin", ("Track","Heat Blast"):"reactive_resin"}
    for pid, brand, name, cov, status, desc, oil, mot in cur.fetchall():
        key = (brand, name)
        mat, why = from_text(cov), "cover name keyword"
        if not mat:
            mat, why = from_text(name), "ball name keyword"
        if not mat and re.search(r"\bspare\b", name, re.I):
            mat, why = "polyester_plastic", "ball name 'Spare'"
        if not mat and re.search(r"\bclear\b", name, re.I):
            mat, why = "polyester_plastic", "ball name 'Clear'"
        if not mat:
            mat, why = from_desc(desc), "description"
        if not mat and key in PAGE:
            mat, why = PAGE[key], "manufacturer page"
        if not mat and cov:
            mat, why = "reactive_resin", "proprietary cover name (default reactive)"
        if not mat and brand in ("Radical", "Storm"):
            mat, why = "reactive_resin", f"{brand} makes reactive performance balls only"
        if not mat:
            mat, why = "reactive_resin", "performance model, no material published (default reactive)"
        if not mat:
            why = "UNRESOLVED"
        rows.append({"id": pid, "brand": brand, "name": name, "cover": cov or "", "status": status, "material": mat or "",
                     "why": why, "oil": oil, "motion": mot})

    return rows


def main():
    out, apply = sys.argv[1], "--apply" in sys.argv
    s = json.loads(boto3.client("secretsmanager", region_name="us-west-1")
                   .get_secret_value(SecretId="bowling-scraper-db")["SecretString"])
    conn = psycopg2.connect(host=s["host"], port=s["port"], dbname=s["dbname"], user=s["username"],
                            password=s["password"])
    rows = classify(conn)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    from collections import Counter
    print(Counter(r["material"] for r in rows))
    if not apply:
        print("dry run -- wrote", out)
        return
    import service  # admin_api/service.py: the v3 estimator

    ids = [r["id"] for r in rows]
    with conn.cursor() as cur:
        for r in rows:
            cur.execute("update products set coverstock_material = %s where id = %s and coverstock_material is null",
                        (r["material"], r["id"]))
            cur.execute("update coverstocks set material = %s where id = (select coverstock_id from products where id = %s) "
                        "and material is null", (r["material"], r["id"]))
        products = service._fetch_plotter_estimate_inputs(
            cur, "p.id = any(%s::uuid[]) and p.oil_motion_source = 'estimated'", (ids,))
        moved = 0
        for p in products:
            e = service._estimate_from_inputs(p)
            cur.execute("update products set oil_rating = %s, motion_rating = %s where id = %s "
                        "and oil_motion_source = 'estimated'", (e["oil"], e["motion"], p["id"]))
            moved += (p["oil_rating"], p["motion_rating"]) != (e["oil"], e["motion"])
    conn.commit()
    print(f"materials set: {len(rows)}; re-estimated: {len(products)}; positions changed: {moved}")


if __name__ == "__main__":
    main()
