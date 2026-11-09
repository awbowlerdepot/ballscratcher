"""Runbook 6gn: apply migration 063, write the specs looked up from the
manufacturers for the values 062 blanked, approve their review rows, and
re-estimate plotter positions. Al: "look up the real specs for the 14
blanked values", then "yes write them all".

Sources:
- Motiv TR2 (motivbowling.com/n_860): the weight table prints 0.31-0.52;
  the same page's description says "differential of .052 (15# ball)", so
  the table is off by ten.
- Motiv Tribal Fire 12 lb (n_874): ".31", the same typo (other weights
  .036-.038).
- Hammer Raw Hammer Burn 15 lb: the page prints "RG (.48)" with the 2
  dropped (14 lb 2.50, 16 lb 2.48).
- DV8 Diva Style: the info sheet PDF (form 1216-11), RG Min / RG Diff /
  RG ASY per weight. It also corrects 16 lb RG (stored 2.516, the 15 lb
  headline) and adds the missing 14 lb row and the differentials.

Track 300T 15 lb, Track Cyborg 13 lb and Recon RX1 Silver 16 lb stay
blank and pending: the manufacturer's page itself is wrong.

Runs in one transaction; a simulated re-scrape with the pages' typos is
checked (and rolled back) before commit, so the corrected values are
proven to survive Sunday's re-scrape under 063.

    DB_SECRET_ARN=... .venv/bin/python scripts/write_6gn_spec_lookups.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "admin_api"))
import service  # noqa: E402

DIVA = "6a8ceaf1-00f7-441f-a8ee-d3155a47673e"
TR2 = "4244a892-e262-433e-8496-eed3b0feca4b"
TRIBAL = "2d85684e-ad2b-4cc3-ae09-fc49e023f4ef"
BURN = "5b19b3a7-60f6-430e-a874-6b45c472c7ea"

FIX = {
    (TR2, 12): {"differential": "0.031"},
    (TR2, 13): {"differential": "0.042"},
    (TR2, 14): {"differential": "0.048"},
    (TR2, 15): {"differential": "0.052"},
    (TR2, 16): {"differential": "0.048"},
    (TRIBAL, 12): {"differential": "0.031"},
    (BURN, 15): {"rg": "2.48"},
    (DIVA, 10): {"rg": "2.800", "differential": "0.002", "mass_bias": "0.001"},
    (DIVA, 11): {"rg": "2.769", "differential": "0.002", "mass_bias": "0.001"},
    (DIVA, 12): {"rg": "2.608", "differential": "0.040", "mass_bias": "0.001"},
    (DIVA, 13): {"rg": "2.585", "differential": "0.040", "mass_bias": "0.001"},
    (DIVA, 14): {"rg": "2.532", "differential": "0.052", "mass_bias": "0.001"},
    (DIVA, 15): {"rg": "2.516", "differential": "0.052", "mass_bias": "0.001"},
    (DIVA, 16): {"rg": "2.503", "differential": "0.052", "mass_bias": "0.001"},
}

conn = service.get_db_connection()
cur = conn.cursor()
cur.execute(open(os.path.join(ROOT, "db", "migrations", "063_product_skus_spec_check_keeps_good_value.sql")).read())

for (pid, weight), vals in FIX.items():
    cur.execute(
        "insert into product_skus (product_id, weight_lbs, source) values (%s, %s, 'pdf') on conflict do nothing",
        (pid, weight),
    )
    sets = ", ".join(f"{k} = %s" for k in vals)
    cur.execute(
        f"update product_skus set {sets}, needs_review = false, updated_at = now() "
        f"where product_id = %s and weight_lbs = %s",
        (*vals.values(), pid, weight),
    )
    cur.execute(f"select {', '.join(vals)} from product_skus where product_id = %s and weight_lbs = %s", (pid, weight))
    got = cur.fetchone()
    assert [float(x) for x in got] == [float(x) for x in vals.values()], ((pid, weight), got)
    for field, value in vals.items():
        cur.execute(
            "update review_queue set status = 'approved', proposed_value = %s, resolved_at = now(), "
            "resolved_by = 'runbook 6gn (manufacturer spec lookup)' "
            "where source = 'usbc_spec_check' and status = 'pending' and product_id = %s and field_name = %s",
            (value, pid, f"{field}_{weight}lb"),
        )

# Simulated Sunday re-scrape carrying the pages' typos: 063 must keep the
# corrected values.
cur.execute("savepoint sim")
cur.execute(
    """insert into product_skus (product_id, weight_lbs, rg, differential, mass_bias, source)
       values (%s, 15, 2.54, 0.52, null, 'html'), (%s, 15, 0.48, 0.050, null, 'html')
       on conflict (product_id, weight_lbs) do update set rg = excluded.rg, differential = excluded.differential,
         mass_bias = coalesce(excluded.mass_bias, product_skus.mass_bias)
       returning rg, differential""",
    (TR2, BURN),
)
sim = [tuple(float(v) for v in r) for r in cur.fetchall()]
print("re-scrape simulation:", sim)
assert sorted(sim) == sorted([(2.54, 0.052), (2.48, 0.050)]), sim
cur.execute("rollback to savepoint sim")
conn.commit()

cur.execute("select status, count(*) from review_queue where source = 'usbc_spec_check' group by 1 order by 1")
print("review rows:", cur.fetchall())
cur.execute(
    "select p.name, s.weight_lbs from product_skus s join products p on p.id = s.product_id "
    "where s.needs_review order by 1, 2"
)
print("still needs review:", cur.fetchall())

res = service.reestimate_plotter_positions(conn)
conn.commit()
print({k: v for k, v in res.items() if not isinstance(v, (list, dict))})
cur.execute("select name, oil_rating, motion_rating from products where id in %s order by 1", ((DIVA, TR2, TRIBAL, BURN),))
for row in cur.fetchall():
    print(row)
