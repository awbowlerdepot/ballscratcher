"""
Recomputes products.popularity_score/total_daily_movement/demand_score
(030_materialized_product_scores.sql) -- see that migration's own header
comment for the full "why": these three used to be computed live, as
correlated subqueries, on every single admin_api/public_api request that
touched them (list_products, the Dashboard, the consumer-site Browse
page) -- a real, confirmed performance cost that grew with the catalog,
not something any index could fix. This Lambda does that same work ONCE
a day instead, as a plain set-based UPDATE, and every caller now just
reads the stored column.

Three UPDATEs, run in order, each a full unconditional pass over
`products` (no WHERE -- every row gets a fresh value every run, including
one with nothing to compute, which correctly resets it back to 0 rather
than leaving a stale nonzero score behind from before, e.g. a product
whose only qualifying video got rejected/deleted since the last run):

  1. popularity_score -- IDENTICAL formula to admin_api/public_api's old
     inline _POPULARITY_SCORE_SQL (half-life-weighted avg approved-video
     view count * ln(1 + video count)). MUST stay in lockstep with both
     modules' own POPULARITY_HALF_LIFE_DAYS -- same hand-synced-constant
     risk this project already carries elsewhere (see admin_api/
     service.py's own comment on that), now with a third copy to keep in
     sync instead of two.
  2. total_daily_movement -- IDENTICAL formula to admin_api's old inline
     _TOTAL_DAILY_MOVEMENT_SQL (trailing DAILY_MOVEMENT_LOOKBACK_DAYS-day
     drops-only per-SKU depletion rate, summed across the product's
     SKUs).
  3. demand_score -- a 50/50 percent_rank() blend of the two columns
     THIS RUN JUST WROTE (not the correlated-subquery version the old
     _DEMAND_SCORE_CTE computed inline) -- cheap now: two window-function
     passes over already-materialized numeric columns, not two full
     re-evaluations of the underlying subqueries. Must run AFTER steps 1
     and 2 in the same invocation so it ranks against fresh values, not
     yesterday's.

Not scoped to any product_id -- unlike product_article_generator's
per-invocation cap (a real, separate concern: that function's cost is
per-product LLM/image-generation calls that can time out a 600s Lambda,
while this one is three plain SQL UPDATEs against however many rows
`products` has, which Postgres itself executes as a single set-based
scan each -- much cheaper per row than N correlated subqueries issued
from the API layer, and there's no equivalent timeout-risk reason to cap
it). If catalog growth ever makes this function's own duration a real
concern, revisit then -- not preemptively.
"""
import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Kept in lockstep with admin_api/service.py's and public_api/service.py's
# own POPULARITY_HALF_LIFE_DAYS -- see this module's own docstring.
POPULARITY_HALF_LIFE_DAYS = 180

# Kept in lockstep with admin_api/service.py's own
# DAILY_MOVEMENT_LOOKBACK_DAYS.
DAILY_MOVEMENT_LOOKBACK_DAYS = 30

# Kept in lockstep with admin_api/service.py's own
# _DEMAND_SCORE_POPULARITY_WEIGHT/_DEMAND_SCORE_DAILY_MOVEMENT_WEIGHT.
DEMAND_SCORE_POPULARITY_WEIGHT = 0.5
DEMAND_SCORE_DAILY_MOVEMENT_WEIGHT = 0.5

# Identical to admin_api/service.py's old inline _POPULARITY_SCORE_SQL --
# see this module's own docstring, point 1.
_POPULARITY_SCORE_SQL = f"""coalesce((
                   select avg(
                       pv.view_count * power(2, -extract(epoch from (now() - coalesce(pv.published_at, pv.created_at))) / (86400.0 * {POPULARITY_HALF_LIFE_DAYS}))
                   ) * ln(1 + count(*))
                   from product_videos pv
                   where pv.product_id = p.id and pv.status = 'approved' and pv.view_count is not null
               ), 0)"""

# Identical to admin_api/service.py's old inline _TOTAL_DAILY_MOVEMENT_SQL
# -- see this module's own docstring, point 2.
_TOTAL_DAILY_MOVEMENT_SQL = f"""coalesce((
                   select sum(case when sku.elapsed_days > 0 then sku.units_sold / sku.elapsed_days else 0 end)
                   from (
                       select
                           h.product_sku_id,
                           sum(case when h.delta < 0 then -h.delta else 0 end) as units_sold,
                           extract(epoch from (max(h.checked_at) - min(h.checked_at))) / 86400.0 as elapsed_days
                       from (
                           select
                               psh.product_sku_id,
                               psh.checked_at,
                               psh.quantity - lag(psh.quantity) over (partition by psh.product_sku_id order by psh.checked_at) as delta
                           from product_sku_stock_history psh
                           join product_skus ps_dm on ps_dm.id = psh.product_sku_id
                           where ps_dm.product_id = p.id
                             and psh.quantity is not null
                             and psh.checked_at >= now() - interval '{DAILY_MOVEMENT_LOOKBACK_DAYS} days'
                       ) h
                       group by h.product_sku_id
                       having count(*) >= 2
                   ) sku
               ), 0)"""


def refresh_popularity_scores(conn) -> int:
    """UPDATE 1 of 3 -- see this module's own docstring. Returns the
    number of rows touched (psycopg2's cursor.rowcount after an
    unconditional UPDATE -- every row in `products`)."""
    with conn.cursor() as cur:
        cur.execute(f"update products p set popularity_score = {_POPULARITY_SCORE_SQL}")
        return cur.rowcount


def refresh_total_daily_movement(conn) -> int:
    """UPDATE 2 of 3 -- see this module's own docstring."""
    with conn.cursor() as cur:
        cur.execute(f"update products p set total_daily_movement = {_TOTAL_DAILY_MOVEMENT_SQL}")
        return cur.rowcount


def refresh_demand_scores(conn) -> int:
    """UPDATE 3 of 3 -- see this module's own docstring. MUST run after
    refresh_popularity_scores/refresh_total_daily_movement in the same
    invocation (see handler) so it ranks against the values THIS run just
    wrote, not a stale prior day's. percent_rank() can't appear directly
    in an UPDATE...SET the way the two scalar-subquery UPDATEs above can
    -- it needs a query context -- hence the `from (select ...) d` join
    shape rather than a bare `set demand_score = percent_rank() ...`."""
    with conn.cursor() as cur:
        cur.execute(f"""
            update products p
            set demand_score = d.demand_score
            from (
                select id,
                       {DEMAND_SCORE_POPULARITY_WEIGHT} * percent_rank() over (order by popularity_score)
                     + {DEMAND_SCORE_DAILY_MOVEMENT_WEIGHT} * percent_rank() over (order by total_daily_movement)
                     as demand_score
                from products
            ) d
            where d.id = p.id
        """)
        return cur.rowcount


def get_db_connection():
    import boto3
    import psycopg2

    secret_arn = os.environ["DB_SECRET_ARN"]
    client = boto3.client("secretsmanager")
    secret = json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])

    return psycopg2.connect(
        host=secret["host"],
        port=secret.get("port", 5432),
        dbname=secret["dbname"],
        user=secret["username"],
        password=secret["password"],
    )


def handler(event, context):
    """No job-shape branching -- unlike most of this project's other
    scheduled Lambdas, there's only ever one thing to do here (refresh
    every row), so the daily EventBridge Schedule and a manual/on-demand
    invoke (e.g. once right after this feature first deploys, per
    DEPLOY_RUNBOOK.md's own note about the 24h flat-zero window
    otherwise) both just call handler({}, ...) the same way."""
    conn = get_db_connection()
    try:
        popularity_rows = refresh_popularity_scores(conn)
        movement_rows = refresh_total_daily_movement(conn)
        demand_rows = refresh_demand_scores(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    result = {
        "popularity_score_rows": popularity_rows,
        "total_daily_movement_rows": movement_rows,
        "demand_score_rows": demand_rows,
    }
    logger.info("Refreshed product scores: %s", result)
    return {"statusCode": 200, "body": json.dumps(result)}
