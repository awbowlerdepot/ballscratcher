#!/usr/bin/env python3
"""
Refit the plotter estimator (the PLOTTER ESTIMATOR v3 block in
src/public_api/service.py and src/admin_api/service.py) on every chart-
positioned ball (DEPLOY_RUNBOOK.md 6bx, 6ck).

    .venv/bin/pip install numpy psycopg2-binary boto3
    .venv/bin/python scripts/refit_plotter_estimator.py            # report + constants
    .venv/bin/python scripts/refit_plotter_estimator.py --json out.json

Reads the database READ-ONLY (credentials from the bowling-scraper-db
secret). Training set: products with oil_motion_source = 'chart', minus
plastic (pinned to (1, 1), not fitted). Inputs are exactly what the
estimator consumes: cover material/type, core type, finish category, the
15 lb SKU's RG/differential/mass bias (nearest weight if no 15 lb), and
the HIGHEST approved price ever seen.

Method (same as v3): ridge regression on standardized features with an
unpenalized intercept, lambda picked by leave-one-out MAE. Three models:
oil with price (priced balls), oil without price (all), motion (all; price
doesn't help motion). Input ranges (for clamping) and fill values come
from the training data. Prints LOO scores and the Python constants to
paste into BOTH copies of the block (tests/test_plotter_estimator_sync.py
keeps them identical).

Not refit here: the plastic pin, the +2 particle oil bonus, and the
low-friction pull (6cb) -- no chart data for any of them yet.
"""
import argparse
import json
import math

import numpy as np

FINISH = {"polished": 0.0, "satin": 1.0, "dull": 2.0}
FEATS = ["urethane", "pearl", "solid", "asymmetric", "core_unknown", "finish", "rg15", "diff15", "mass_bias15"]
LAMBDAS = [0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30, 50]

QUERY = """
select b.name as brand, p.name, p.oil_rating, p.motion_rating, p.coverstock_material, p.coverstock_type,
       c.core_type, p.finish_category,
       (select max(h.price) from product_price_sources ps join product_price_history h on h.price_source_id = ps.id
         where ps.product_id = p.id and ps.status = 'approved' and h.price is not null) as max_price,
       (select json_agg(json_build_object('w', s.weight_lbs, 'rg', s.rg, 'diff', s.differential, 'mb', s.mass_bias))
          from product_skus s where s.product_id = p.id and s.differential is not null) as skus
from products p join brands b on b.id = p.brand_id left join cores c on c.id = p.core_id
where p.oil_motion_source = 'chart' and p.coverstock_material is distinct from 'polyester_plastic'
"""


def load_rows():
    import boto3
    import psycopg2

    s = json.loads(boto3.client("secretsmanager", region_name="us-west-1")
                   .get_secret_value(SecretId="bowling-scraper-db")["SecretString"])
    conn = psycopg2.connect(host=s["host"], port=s.get("port", 5432), dbname=s["dbname"], user=s["username"],
                            password=s["password"], options="-c default_transaction_read_only=on", connect_timeout=10)
    with conn.cursor() as cur:
        cur.execute(QUERY)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows


def ref_sku(skus):
    skus = skus or []
    if not skus:
        return {}
    exact = [s for s in skus if s["w"] == 15]
    return exact[0] if exact else min(skus, key=lambda s: abs(s["w"] - 15))


def prepare(rows):
    data = []
    for r in rows:
        s = ref_sku(r["skus"])
        data.append({
            "name": f"{r['brand']} {r['name']}", "oil": float(r["oil_rating"]), "motion": float(r["motion_rating"]),
            "material": r["coverstock_material"], "ctype": r["coverstock_type"], "core": r["core_type"],
            "finish": r["finish_category"], "rg": s.get("rg"), "diff": s.get("diff"), "mb": s.get("mb"),
            "price": float(r["max_price"]) if r["max_price"] is not None else None,
        })
    return data


def fit_all(data):
    def rng(key):
        vals = [d[key] for d in data if d[key] is not None]
        return (min(vals), max(vals))

    ranges = {"rg15": rng("rg"), "diff15": rng("diff"), "mass_bias15": (0.0, max(d["mb"] or 0 for d in data)),
              "price": rng("price")}
    fill = {"rg15": float(np.mean([d["rg"] for d in data if d["rg"] is not None])),
            "diff15": float(np.mean([d["diff"] for d in data if d["diff"] is not None])), "mass_bias15": 0.0}

    def clamp(name, v):
        lo, hi = ranges[name]
        return max(lo, min(hi, float(v)))

    def feats(d, with_price):
        f = [
            1.0 if d["material"] == "urethane" else 0.0,
            1.0 if d["ctype"] == "pearl" else 0.0,
            1.0 if d["ctype"] == "solid" else 0.0,
            1.0 if d["core"] == "asymmetric" else 0.0,
            1.0 if d["core"] is None else 0.0,
            FINISH.get(d["finish"], 1.0),
            clamp("rg15", d["rg"] if d["rg"] is not None else fill["rg15"]),
            clamp("diff15", d["diff"] if d["diff"] is not None else fill["diff15"]),
            clamp("mass_bias15", d["mb"] if d["mb"] is not None else 0.0),
        ]
        if with_price:
            f.append(math.log(clamp("price", d["price"])))
        return f

    def ridge(X, y, lam):
        mu, sd = X.mean(0), X.std(0)
        sd[sd == 0] = 1.0
        Z = (X - mu) / sd
        w = np.linalg.solve(Z.T @ Z + lam * np.eye(Z.shape[1]), Z.T @ (y - y.mean()))
        coef = w / sd
        return y.mean() - (mu * coef).sum(), coef

    def loo(X, y, lam, lo, hi):
        preds = []
        for i in range(len(y)):
            m = np.ones(len(y), bool)
            m[i] = False
            b, c = ridge(X[m], y[m], lam)
            preds.append(min(hi, max(lo, round(float(b + X[i] @ c), 1))))
        err = np.abs(np.array(preds) - y)
        return float(err.mean()), int((err <= 2).sum())

    def fit(subset, target, with_price, lo, hi):
        names = FEATS + (["log_price"] if with_price else [])
        X = np.array([feats(d, with_price) for d in subset])
        y = np.array([d[target] for d in subset])
        lam, mae, w2 = min(((lam, *loo(X, y, lam, lo, hi)) for lam in LAMBDAS), key=lambda t: t[1])
        b, c = ridge(X, y, lam)
        return {"lam": lam, "loo_mae": round(mae, 2), "loo_within2": w2, "n": len(y),
                "intercept": round(float(b), 4), "coef": {n: round(float(v), 4) for n, v in zip(names, c)}}

    priced = [d for d in data if d["price"] is not None]
    return {
        "n": len(data), "n_priced": len(priced),
        "ranges": {k: [round(a, 4), round(b, 4)] for k, (a, b) in ranges.items()},
        "fill": {k: round(v, 4) for k, v in fill.items()},
        "oil_with_price": fit(priced, "oil", True, 1, 16),
        "oil_no_price": fit(data, "oil", False, 1, 16),
        "motion": fit(data, "motion", False, 1, 18),
    }


def constants(r):
    def model(name, m):
        lines = [f"{name} = {{", f'    "intercept": {m["intercept"]},', '    "coef": {']
        lines += [f'        "{k}": {v},' for k, v in m["coef"].items()]
        return "\n".join(lines + ["    },", "}"])

    rg, df, mb, pr = (r["ranges"][k] for k in ("rg15", "diff15", "mass_bias15", "price"))
    return "\n".join([
        "PLOTTER_INPUT_RANGES = {",
        f'    "rg15": ({rg[0]}, {rg[1]}),',
        f'    "diff15": ({df[0]}, {df[1]}),',
        f'    "mass_bias15": ({mb[0]}, {mb[1]}),',
        f'    "price": ({pr[0]}, {pr[1]}),',
        "}",
        f'PLOTTER_INPUT_FILL = {{"rg15": {r["fill"]["rg15"]}, "diff15": {r["fill"]["diff15"]}, "mass_bias15": 0.0}}',
        "",
        model("PLOTTER_OIL_WITH_PRICE", r["oil_with_price"]),
        model("PLOTTER_OIL_NO_PRICE", r["oil_no_price"]),
        model("PLOTTER_MOTION", r["motion"]),
    ])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", help="also write the full fit report to this file")
    args = ap.parse_args()
    result = fit_all(prepare(load_rows()))
    print(f"Training balls: {result['n']} ({result['n_priced']} with a price)")
    for k in ("oil_with_price", "oil_no_price", "motion"):
        m = result[k]
        print(f"  {k:15} lambda {m['lam']:<5} LOO MAE {m['loo_mae']:.2f}  within +/-2 {m['loo_within2']}/{m['n']}")
    print("\n# ---- paste into BOTH PLOTTER ESTIMATOR v3 blocks ----")
    print(constants(result))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(result, f, indent=2)


if __name__ == "__main__":
    main()
