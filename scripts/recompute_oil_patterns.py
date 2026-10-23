#!/usr/bin/env python3
"""
Recompute every oil pattern's stored derived fields (runbook 6eo).

WHY THIS EXISTS: admin_api writes oil_patterns.compute's derived fields
(steps' board numbers, board_totals, profile, ratio) only when a pattern
is saved or approved. When the profile model changes (6ek v1 -> 6em v2 ->
6en v3) rows saved earlier keep the old grid -- the partner API serves the
stored row, so an approved pattern would keep publishing the old model
until someone happened to save it. Al approved Elite 48 under the old
model on 2026-10-23 and it was still serving that profile after v3 shipped.

Run it after any change to oil_patterns.profile/compute (and its
PROFILE_MODEL), against approved AND pending rows. It only rewrites the
derived fields, never the sheet's own numbers or the review state; the
content_changed_at trigger moves for rows whose output actually changed,
so partners syncing /v1/patterns/changes pick them up.

Usage (PG* exported from the bowling-scraper-db secret):
    .venv/bin/python scripts/recompute_oil_patterns.py           # dry run
    .venv/bin/python scripts/recompute_oil_patterns.py --apply
"""
import json
import os
import sys

import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "admin_api"))
import oil_patterns  # noqa: E402

SOURCE = ("name", "category", "length_feet", "oil_per_board_ul", "forward_ml", "reverse_ml", "volume_ml",
          "reverse_brush_drop_feet", "track_zones", "steps")
DERIVED = ("steps", "board_totals", "track_zones", "profile", "ratio", "profile_source")
JSON_COLS = {"steps", "board_totals", "profile", "track_zones"}


def main(apply: bool) -> None:
    conn = psycopg2.connect()
    with conn.cursor() as cur:
        cur.execute(f"select id, review_status, {', '.join(SOURCE)}, board_totals, profile, ratio, profile_source "
                    "from oil_patterns order by created_at")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    changed = 0
    for row in rows:
        p = {k: (float(v) if k in ("length_feet", "oil_per_board_ul", "forward_ml", "reverse_ml", "volume_ml",
                                   "reverse_brush_drop_feet") and v is not None else v) for k, v in row.items()}
        c = oil_patterns.compute(p)
        new = {k: c[k] for k in DERIVED}
        old_model = (row.get("profile") or {}).get("model")
        same = all(json.dumps(new[k], sort_keys=True, default=float) ==
                   json.dumps(row.get(k), sort_keys=True, default=float) for k in DERIVED)
        print(f"{row['id']}  {row['review_status']:<8}  {row['name'][:40]:<40}  {old_model or '(none)'} -> "
              f"{new['profile']['model']}  {'unchanged' if same else 'CHANGED'}")
        if same:
            continue
        changed += 1
        if apply:
            with conn.cursor() as cur:
                cur.execute(
                    f"update oil_patterns set {', '.join(f'{k} = %s' for k in DERIVED)} where id = %s",
                    [json.dumps(new[k]) if k in JSON_COLS else new[k] for k in DERIVED] + [row["id"]],
                )
    if apply:
        conn.commit()
    print(f"{changed} of {len(rows)} rows {'updated' if apply else 'would change (dry run; --apply to write)'}")
    conn.close()


if __name__ == "__main__":
    main("--apply" in sys.argv[1:])
