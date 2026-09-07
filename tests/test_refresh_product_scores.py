"""
Tests for src/refresh_product_scores/app.py.

Manual-runner pattern, run standalone via
`python3 tests/test_refresh_product_scores.py` -- same convention as
test_admin_api_service.py/test_price_checker.py (no pytest in this
sandbox, see those files' own header comments).

This Lambda is new as of the 2026-09-06 performance-materialization fix
(030_materialized_product_scores.sql) and had ZERO test coverage until
this file -- see DEPLOY_RUNBOOK.md's writeup of that investigation for
the full incident this Lambda exists to fix. Everything DB-facing is
exercised against a fake psycopg2-shaped cursor/connection, same
limitation as every other DB-touching test file in this project (no
Postgres instance available here) -- these tests check the SQL TEXT each
function issues, not real query execution/results.

get_db_connection itself is NOT tested here, same convention as
price_checker/app.py's identical copy (also untested) -- it's boilerplate
boto3 Secrets Manager + psycopg2.connect wiring with no project-specific
logic in it.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "refresh_product_scores"))

import app  # noqa: E402


# --- fake psycopg2-shaped cursor/connection ---

class _FakeCursor:
    def __init__(self, rowcount=7):
        self.queries = []
        self.rowcount = rowcount

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.queries.append(" ".join(query.split()))


class _FakeConn:
    def __init__(self, rowcount=7):
        self._cursor = _FakeCursor(rowcount=rowcount)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


# --- refresh_popularity_scores: UPDATE 1 of 3 ---

def test_refresh_popularity_scores_executes_a_single_update_and_returns_rowcount():
    conn = _FakeConn(rowcount=42)
    result = app.refresh_popularity_scores(conn)

    assert result == 42
    assert len(conn.cursor().queries) == 1
    assert conn.cursor().queries[0].startswith("update products p set popularity_score =")


def test_refresh_popularity_scores_uses_confirmed_half_life():
    conn = _FakeConn()
    app.refresh_popularity_scores(conn)

    query = conn.cursor().queries[0]
    assert f"86400.0 * {app.POPULARITY_HALF_LIFE_DAYS}" in query
    assert app.POPULARITY_HALF_LIFE_DAYS == 180


def test_refresh_popularity_scores_averages_not_sums():
    """Al's real incident (see admin_api/service.py's own history of this
    formula): a raw sum let video COUNT dominate the ranking. Confirms
    the UPDATE averages per-video decayed views and applies a sub-linear
    ln(1 + count) volume boost, not a plain sum."""
    conn = _FakeConn()
    app.refresh_popularity_scores(conn)

    query = conn.cursor().queries[0]
    assert "select avg(" in query
    assert "* ln(1 + count(*))" in query
    assert "select sum(" not in query


def test_refresh_popularity_scores_only_counts_approved_videos_with_a_view_count():
    conn = _FakeConn()
    app.refresh_popularity_scores(conn)

    query = conn.cursor().queries[0]
    assert "pv.status = 'approved'" in query
    assert "pv.view_count is not null" in query


def test_refresh_popularity_scores_update_itself_is_unconditional():
    """Deliberately unconditional at the UPDATE level -- see this
    function's own docstring: every row gets a fresh value every run,
    including a reset back to 0 for a product with nothing to compute
    anymore. The correlated subquery has its own internal `where`
    (scoping it to one product), but the outer UPDATE has no WHERE of its
    own -- confirmed by checking the query ends with the subquery's
    closing coalesce, not a trailing filter clause."""
    conn = _FakeConn()
    app.refresh_popularity_scores(conn)

    query = conn.cursor().queries[0]
    assert query.endswith("), 0)")


# --- refresh_total_daily_movement: UPDATE 2 of 3 ---

def test_refresh_total_daily_movement_executes_a_single_update_and_returns_rowcount():
    conn = _FakeConn(rowcount=13)
    result = app.refresh_total_daily_movement(conn)

    assert result == 13
    assert len(conn.cursor().queries) == 1
    assert conn.cursor().queries[0].startswith("update products p set total_daily_movement =")


def test_refresh_total_daily_movement_uses_confirmed_lookback_window():
    conn = _FakeConn()
    app.refresh_total_daily_movement(conn)

    query = conn.cursor().queries[0]
    assert f"interval '{app.DAILY_MOVEMENT_LOOKBACK_DAYS} days'" in query
    assert app.DAILY_MOVEMENT_LOOKBACK_DAYS == 30


def test_refresh_total_daily_movement_only_counts_drops_not_restocks():
    conn = _FakeConn()
    app.refresh_total_daily_movement(conn)

    query = conn.cursor().queries[0]
    assert "case when h.delta < 0 then -h.delta else 0 end" in query
    assert "lag(psh.quantity)" in query


def test_refresh_total_daily_movement_requires_at_least_two_readings():
    conn = _FakeConn()
    app.refresh_total_daily_movement(conn)

    query = conn.cursor().queries[0]
    assert "having count(*) >= 2" in query


def test_refresh_total_daily_movement_guards_zero_elapsed_days():
    conn = _FakeConn()
    app.refresh_total_daily_movement(conn)

    query = conn.cursor().queries[0]
    assert "case when sku.elapsed_days > 0 then sku.units_sold / sku.elapsed_days else 0 end" in query


def test_refresh_total_daily_movement_scoped_via_product_skus_join():
    conn = _FakeConn()
    app.refresh_total_daily_movement(conn)

    query = conn.cursor().queries[0]
    assert "ps_dm.product_id = p.id" in query


# --- refresh_demand_scores: UPDATE 3 of 3 ---

def test_refresh_demand_scores_executes_a_single_update_and_returns_rowcount():
    conn = _FakeConn(rowcount=99)
    result = app.refresh_demand_scores(conn)

    assert result == 99
    assert len(conn.cursor().queries) == 1
    assert conn.cursor().queries[0].startswith("update products p set demand_score = d.demand_score")


def test_refresh_demand_scores_blends_both_metrics_via_percent_rank():
    conn = _FakeConn()
    app.refresh_demand_scores(conn)

    query = conn.cursor().queries[0]
    # Both underlying metrics must be ranked, not compared as raw values --
    # a raw sum would let popularity_score's unbounded log-of-views scale
    # swamp total_daily_movement's small units/day rate.
    assert query.count("percent_rank()") == 2
    assert "order by popularity_score" in query
    assert "order by total_daily_movement" in query


def test_refresh_demand_scores_default_weights_sum_to_one():
    assert app.DEMAND_SCORE_POPULARITY_WEIGHT + app.DEMAND_SCORE_DAILY_MOVEMENT_WEIGHT == 1.0


def test_refresh_demand_scores_ranks_against_the_already_materialized_columns():
    """Unlike the old _DEMAND_SCORE_CTE (which ranked over two correlated
    subqueries), this UPDATE ranks over the plain popularity_score/
    total_daily_movement columns -- cheap now that they're real columns,
    not a re-evaluation of either formula's own subquery."""
    conn = _FakeConn()
    app.refresh_demand_scores(conn)

    query = conn.cursor().queries[0]
    assert "product_videos" not in query
    assert "product_sku_stock_history" not in query
    assert "from products" in query
    assert "where d.id = p.id" in query


def test_popularity_formula_dampens_video_count_vs_a_raw_sum():
    """Pure-math sanity check (no DB) of the formula's actual behavior --
    at equal per-video quality, 20 videos should score ~1.9x a 4-video
    ball (ln(21)/ln(5)), not the 5x (20/4) a raw sum would have produced."""
    import math

    per_video_score = 10_000  # equal per-video quality on both balls
    four_video_ball = per_video_score * math.log(1 + 4)
    twenty_video_ball = per_video_score * math.log(1 + 20)

    ratio = twenty_video_ball / four_video_ball
    assert 1.8 < ratio < 2.0  # nowhere near the raw-sum's 20/4 = 5.0

    old_raw_sum_ratio = (per_video_score * 20) / (per_video_score * 4)
    assert old_raw_sum_ratio == 5.0
    assert ratio < old_raw_sum_ratio


# --- handler: ordering, commit/rollback, close, return shape ---

def test_handler_runs_the_three_updates_in_required_order(monkeypatch):
    """demand_score MUST be computed after popularity_score/
    total_daily_movement in the same invocation so it ranks against
    values THIS run just wrote, not yesterday's -- see refresh_
    demand_scores' own docstring."""
    call_order = []
    monkeypatch.setattr(app, "refresh_popularity_scores", lambda conn: call_order.append("popularity") or 1)
    monkeypatch.setattr(app, "refresh_total_daily_movement", lambda conn: call_order.append("movement") or 2)
    monkeypatch.setattr(app, "refresh_demand_scores", lambda conn: call_order.append("demand") or 3)
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())

    app.handler({}, None)

    assert call_order == ["popularity", "movement", "demand"]


def test_handler_commits_and_closes_on_success(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(app, "refresh_popularity_scores", lambda c: 1)
    monkeypatch.setattr(app, "refresh_total_daily_movement", lambda c: 2)
    monkeypatch.setattr(app, "refresh_demand_scores", lambda c: 3)
    monkeypatch.setattr(app, "get_db_connection", lambda: conn)

    app.handler({}, None)

    assert conn.committed is True
    assert conn.rolled_back is False
    assert conn.closed is True


def test_handler_rolls_back_and_recloses_and_reraises_on_error(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(app, "refresh_popularity_scores", lambda c: 1)

    def _boom(c):
        raise RuntimeError("boom")

    monkeypatch.setattr(app, "refresh_total_daily_movement", _boom)
    monkeypatch.setattr(app, "get_db_connection", lambda: conn)

    raised = False
    try:
        app.handler({}, None)
    except RuntimeError:
        raised = True

    assert raised is True
    assert conn.committed is False
    assert conn.rolled_back is True
    assert conn.closed is True


def test_handler_returns_rowcounts_for_all_three_updates(monkeypatch):
    import json

    monkeypatch.setattr(app, "refresh_popularity_scores", lambda c: 11)
    monkeypatch.setattr(app, "refresh_total_daily_movement", lambda c: 22)
    monkeypatch.setattr(app, "refresh_demand_scores", lambda c: 33)
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())

    result = app.handler({}, None)

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body == {
        "popularity_score_rows": 11,
        "total_daily_movement_rows": 22,
        "demand_score_rows": 33,
    }


def test_handler_treats_scheduled_and_manual_invokes_identically(monkeypatch):
    """No job-shape branching -- see handler's own docstring. A manual
    one-time invoke (e.g. right after this feature first deploys, per
    DEPLOY_RUNBOOK.md's own note about the 24h flat-zero window
    otherwise) passes the same empty event the daily EventBridge Schedule
    does."""
    calls = []
    monkeypatch.setattr(app, "refresh_popularity_scores", lambda c: calls.append("p") or 1)
    monkeypatch.setattr(app, "refresh_total_daily_movement", lambda c: calls.append("m") or 1)
    monkeypatch.setattr(app, "refresh_demand_scores", lambda c: calls.append("d") or 1)
    monkeypatch.setattr(app, "get_db_connection", lambda: _FakeConn())

    app.handler({}, None)
    scheduled_calls = list(calls)
    calls.clear()
    app.handler({"source": "manual-invoke"}, None)

    assert calls == scheduled_calls == ["p", "m", "d"]


class _MonkeyPatch:
    """Minimal monkeypatch stand-in (no pytest in this sandbox) -- sets
    an attribute and restores the original value after each test via the
    manual-runner's own try/finally wrapper below."""

    def __init__(self):
        self._restores = []

    def setattr(self, obj, name, value):
        self._restores.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, value in reversed(self._restores):
            setattr(obj, name, value)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        mp = _MonkeyPatch()
        try:
            if "monkeypatch" in t.__code__.co_varnames[: t.__code__.co_argcount]:
                t(mp)
            else:
                t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"FAIL: {t.__name__}: {e}")
        finally:
            mp.undo()
    print(f"\n{passed}/{len(tests)} tests passed")
