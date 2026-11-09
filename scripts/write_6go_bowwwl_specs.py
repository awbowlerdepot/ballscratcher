"""Runbook 6go: fill the three specs 6gn left blank (the manufacturer's own
page is wrong) from bowwwl.com, plus Cyborg's mass bias. Al: "look up the
300T, Cyborg and Recon specs on bowwwl", then "yes write them, including
cyborg mass bias".

bowwwl.com (bowling-ball-database/track/300t, track/cyborg,
motiv/recon-rx1-silver, read 2026-11-09):
- Track 300T 15 lb differential 0.029 (Track's page: 0.090; 14 lb 0.036,
  16 lb 0.023).
- Track Cyborg 13 lb differential 0.055 (Track's page: 2.530, its RG).
- Track Cyborg mass bias 0.007 at 14/15/16 lb (we had none).
- Motiv Recon RX1 Silver 16 lb RG 2.59 (Motiv's page: 2.29).

Al chose to publish these knowing bowwwl.com's terms restrict copying
(see bowwwl_cross_check/app.py's legal note); until now bowwwl values were
only shown in the review queue, never written as ours.

Not taken: bowwwl's 300T 12/13 lb rows, which are Cyborg's 12/13 lb
numbers exactly (a copy on their side), and we don't carry those weights.

Re-scrapes keep these: the pages' typos are out of range, so migration
063 keeps the stored value. One transaction; re-estimates plotter
positions after.

    DB_SECRET_ARN=... .venv/bin/python scripts/write_6go_bowwwl_specs.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "admin_api"))
import service  # noqa: E402

T300 = "9d81784a-de8c-44f5-9f90-a534d3992c39"
CYBORG = "003615af-97e4-4e06-8cf4-c6901cd559b4"
RECON = "5e76a25f-768c-40f1-8d41-b703ee211e08"

FIX = {
    (T300, 15): {"differential": "0.029"},
    (CYBORG, 13): {"differential": "0.055"},
    (CYBORG, 14): {"mass_bias": "0.007"},
    (CYBORG, 15): {"mass_bias": "0.007"},
    (CYBORG, 16): {"mass_bias": "0.007"},
    (RECON, 16): {"rg": "2.59"},
}

conn = service.get_db_connection()
cur = conn.cursor()
for (pid, weight), vals in FIX.items():
    sets = ", ".join(f"{k} = %s" for k in vals)
    cur.execute(
        f"update product_skus set {sets}, needs_review = false, updated_at = now() "
        f"where product_id = %s and weight_lbs = %s",
        (*vals.values(), pid, weight),
    )
    assert cur.rowcount == 1, ((pid, weight), cur.rowcount)
    cur.execute(f"select {', '.join(vals)} from product_skus where product_id = %s and weight_lbs = %s", (pid, weight))
    got = cur.fetchone()
    assert [float(x) for x in got] == [float(x) for x in vals.values()], ((pid, weight), got)
    for field, value in vals.items():
        cur.execute(
            "update review_queue set status = 'approved', proposed_value = %s, resolved_at = now(), "
            "resolved_by = 'runbook 6go (bowwwl.com lookup)' "
            "where source = 'usbc_spec_check' and status = 'pending' and product_id = %s and field_name = %s",
            (value, pid, f"{field}_{weight}lb"),
        )
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
cur.execute("select name, oil_rating, motion_rating from products where id in %s order by 1", ((T300, CYBORG, RECON),))
for row in cur.fetchall():
    print(row)
