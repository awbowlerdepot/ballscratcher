"""
Tests for src/public_api/service.py.

Same honesty note as test_admin_api_service.py: fastapi/pydantic/mangum
aren't installable in this sandbox (pip's proxy 403's), so app.py's actual
HTTP routing is untested this session -- only imports it, doesn't exercise
it. What IS tested here: score_similarity/_reference_sku (pure functions,
no DB), the filter-SQL shape of list_products/list_brands via a query-
capturing fake connection, and the full multi-query assembly of
get_product/get_products_compare/list_similar_products against a small
hand-built fake psycopg2-shaped cursor/connection (no real Postgres
available in this sandbox, same limitation noted throughout this project).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "public_api"))

import service  # noqa: E402


# --- get_db_connection: module-level connection reuse across warm Lambda
# invocations -- REAL PERFORMANCE INCIDENT, Al: "the public site is
# pretty slow", root-caused to the old version paying a fresh TCP/TLS/
# Postgres-auth handshake AND a secretsmanager:GetSecretValue call on
# EVERY request, even on an already-warm container. Exercised against
# fake boto3/psycopg2 modules injected via sys.modules (same technique
# test_admin_api_service.py already uses for boto3 -- see its own
# _FakeBoto3 usages), since neither is installed in this sandbox. Every
# test resets service._cached_conn/_cached_secret to None first (module-
# level state persists across tests otherwise) and restores the real
# sys.modules entries in a finally block.

class _ConnCacheFakeCursor:
    # NOTE: deliberately NOT named _FakeCursor -- this file already
    # defines a different _FakeCursor later on (the get_product/
    # list_products fixture) with a totally different constructor/query
    # shape. Class names in a module are resolved at CALL time via the
    # module's global namespace, not at def time, so _FakeConn.cursor()
    # below was silently picking up that OTHER, later-defined _FakeCursor
    # once the whole file had finished executing -- its query dispatcher
    # doesn't recognize "select 1" and raises NotImplementedError, which
    # get_db_connection's health check swallows as "connection is dead",
    # forcing a real reconnect on every single call. That's why
    # conn1 is conn2 is conn3 failed even though the production code was
    # already correct. Every fixture in this block is prefixed
    # _ConnCache* for the same reason -- to never again collide with a
    # same-named fixture anywhere else in this file.
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        if self.conn.broken:
            raise RuntimeError("simulated dropped connection")


class _ConnCacheFakeConn:
    def __init__(self, host, broken=False):
        self.host = host
        self.broken = broken
        self.closed_flag = 0
        self.autocommit = False

    def cursor(self):
        return _ConnCacheFakeCursor(self)

    def close(self):
        self.closed_flag = 1


class _ConnCacheFakePsycopg2:
    class OperationalError(Exception):
        pass

    def __init__(self, fail_hosts=None):
        self.fail_hosts = fail_hosts or set()
        self.connect_calls = []

    def connect(self, host, port, dbname, user, password):
        self.connect_calls.append(host)
        if host in self.fail_hosts:
            raise self.OperationalError(f"could not connect to {host}")
        return _ConnCacheFakeConn(host)


class _ConnCacheFakeSecretsManagerClient:
    def __init__(self, secrets_by_call):
        # list of secret dicts returned in order, one per get_secret_value
        # call -- lets a test simulate a credential rotation (second call
        # returns different values than the first).
        self.secrets_by_call = list(secrets_by_call)
        self.calls = 0

    def get_secret_value(self, SecretId):
        secret = self.secrets_by_call[min(self.calls, len(self.secrets_by_call) - 1)]
        self.calls += 1
        return {"SecretString": json.dumps(secret)}


class _ConnCacheFakeBoto3:
    def __init__(self, secretsmanager_client):
        self._client = secretsmanager_client

    def client(self, name):
        assert name == "secretsmanager"
        return self._client


def _install_conn_cache_fakes(fake_boto3, fake_psycopg2):
    real_boto3 = sys.modules.get("boto3")
    real_psycopg2 = sys.modules.get("psycopg2")
    sys.modules["boto3"] = fake_boto3
    sys.modules["psycopg2"] = fake_psycopg2
    os.environ["DB_SECRET_ARN"] = "arn:aws:secretsmanager:us-west-1:000000000000:secret:fake"

    def _restore():
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]
        if real_psycopg2 is not None:
            sys.modules["psycopg2"] = real_psycopg2
        else:
            del sys.modules["psycopg2"]
        del os.environ["DB_SECRET_ARN"]

    return _restore


def _reset_conn_cache():
    service._cached_conn = None
    service._cached_secret = None


def test_get_db_connection_opens_once_then_reuses_across_calls():
    _reset_conn_cache()
    secret = {"host": "db.example.internal", "dbname": "bowling", "username": "app", "password": "pw"}
    fake_psycopg2 = _ConnCacheFakePsycopg2()
    fake_client = _ConnCacheFakeSecretsManagerClient([secret])
    restore = _install_conn_cache_fakes(_ConnCacheFakeBoto3(fake_client), fake_psycopg2)
    try:
        conn1 = service.get_db_connection()
        conn2 = service.get_db_connection()
        conn3 = service.get_db_connection()

        assert conn1 is conn2 is conn3
        assert len(fake_psycopg2.connect_calls) == 1  # only ONE real connect
        assert fake_client.calls == 1  # only ONE Secrets Manager round trip
    finally:
        restore()
        _reset_conn_cache()


def test_get_db_connection_sets_autocommit_true():
    # This API is entirely read-only -- see get_db_connection's own
    # docstring for why a dangling "idle in transaction" session must
    # never accumulate on a reused connection.
    _reset_conn_cache()
    secret = {"host": "db.example.internal", "dbname": "bowling", "username": "app", "password": "pw"}
    restore = _install_conn_cache_fakes(_ConnCacheFakeBoto3(_ConnCacheFakeSecretsManagerClient([secret])), _ConnCacheFakePsycopg2())
    try:
        conn = service.get_db_connection()
        assert conn.autocommit is True
    finally:
        restore()
        _reset_conn_cache()


def test_get_db_connection_reconnects_when_cached_connection_is_dead():
    _reset_conn_cache()
    secret = {"host": "db.example.internal", "dbname": "bowling", "username": "app", "password": "pw"}
    fake_psycopg2 = _ConnCacheFakePsycopg2()
    fake_client = _ConnCacheFakeSecretsManagerClient([secret])
    restore = _install_conn_cache_fakes(_ConnCacheFakeBoto3(fake_client), fake_psycopg2)
    try:
        conn1 = service.get_db_connection()
        conn1.broken = True  # simulate the server/network dropping it silently

        conn2 = service.get_db_connection()

        assert conn2 is not conn1
        assert len(fake_psycopg2.connect_calls) == 2  # health check failed -- real reconnect
        # The cached SECRET is still reused, no reason to assume rotation
        # just because the connection dropped.
        assert fake_client.calls == 1
    finally:
        restore()
        _reset_conn_cache()


def test_get_db_connection_refetches_secret_when_cached_one_is_stale():
    # Simulates a real credential rotation: the cached secret's password
    # no longer works, so connecting with it raises OperationalError --
    # must re-fetch from Secrets Manager and retry once, not just fail.
    _reset_conn_cache()
    old_secret = {"host": "old.example.internal", "dbname": "bowling", "username": "app", "password": "stale"}
    new_secret = {"host": "new.example.internal", "dbname": "bowling", "username": "app", "password": "fresh"}
    fake_psycopg2 = _ConnCacheFakePsycopg2(fail_hosts={"old.example.internal"})
    fake_client = _ConnCacheFakeSecretsManagerClient([old_secret, new_secret])
    restore = _install_conn_cache_fakes(_ConnCacheFakeBoto3(fake_client), fake_psycopg2)
    try:
        conn = service.get_db_connection()

        assert conn.host == "new.example.internal"
        assert fake_client.calls == 2  # first (stale) + forced re-fetch
        assert fake_psycopg2.connect_calls == ["old.example.internal", "new.example.internal"]
    finally:
        restore()
        _reset_conn_cache()


def test_get_db_connection_health_check_failure_also_falls_back_to_secret_refetch():
    # Chains both failure modes: cached connection is dead AND the cached
    # secret it would reconnect with has also gone stale in the meantime.
    _reset_conn_cache()
    old_secret = {"host": "old.example.internal", "dbname": "bowling", "username": "app", "password": "stale"}
    new_secret = {"host": "new.example.internal", "dbname": "bowling", "username": "app", "password": "fresh"}
    fake_psycopg2 = _ConnCacheFakePsycopg2(fail_hosts={"old.example.internal"})
    fake_client = _ConnCacheFakeSecretsManagerClient([old_secret, new_secret])
    restore = _install_conn_cache_fakes(_ConnCacheFakeBoto3(fake_client), fake_psycopg2)
    try:
        conn1 = service.get_db_connection()  # succeeds on first secret
    finally:
        pass
    # Force the fake to start failing THIS host too (simulating the
    # credential being rotated out from under an already-open connection).
    fake_psycopg2.fail_hosts.add("old.example.internal")
    conn1.broken = True
    try:
        conn2 = service.get_db_connection()
        assert conn2.host == "new.example.internal"
    finally:
        restore()
        _reset_conn_cache()


# --- score_similarity / _reference_sku: pure, no DB ---

def test_score_similarity_identical_specs_and_categories_is_zero():
    source = {"rg": 2.50, "differential": 0.050, "core_type": "asymmetric",
              "coverstock_type": "hybrid", "coverstock_material": "reactive_resin"}
    assert service.score_similarity(source, dict(source)) == 0.0


def test_score_similarity_closer_specs_score_lower():
    source = {"rg": 2.50, "differential": 0.050, "core_type": "asymmetric",
              "coverstock_type": "hybrid", "coverstock_material": "reactive_resin"}
    close = {"rg": 2.52, "differential": 0.048, "core_type": "asymmetric",
             "coverstock_type": "hybrid", "coverstock_material": "reactive_resin"}
    far = {"rg": 2.70, "differential": 0.015, "core_type": "symmetric",
           "coverstock_type": "solid", "coverstock_material": "urethane"}
    assert service.score_similarity(source, close) < service.score_similarity(source, far)


def test_score_similarity_category_mismatch_adds_penalty_even_with_identical_specs():
    source = {"rg": 2.50, "differential": 0.050, "core_type": "asymmetric",
              "coverstock_type": "hybrid", "coverstock_material": "reactive_resin"}
    same_specs_diff_core = dict(source, core_type="symmetric")
    assert service.score_similarity(source, same_specs_diff_core) == service.CORE_TYPE_MISMATCH_PENALTY


def test_score_similarity_missing_rg_skips_numeric_term_not_a_penalty():
    source = {"rg": None, "differential": None, "core_type": "asymmetric"}
    candidate = {"rg": 2.90, "differential": 0.005, "core_type": "asymmetric"}
    # No usable numeric spec on either side to compare, and core_type
    # matches -- score should be exactly 0, not some default/error value.
    assert service.score_similarity(source, candidate) == 0.0


def test_reference_sku_prefers_real_15lb_row():
    skus = [{"weight_lbs": 16, "rg": 2.49}, {"weight_lbs": 15, "rg": 2.51}, {"weight_lbs": 14, "rg": 2.53}]
    assert service._reference_sku(skus)["weight_lbs"] == 15


def test_reference_sku_falls_back_to_nearest_weight():
    skus = [{"weight_lbs": 16, "rg": 2.49}, {"weight_lbs": 12, "rg": 2.60}]
    assert service._reference_sku(skus)["weight_lbs"] == 16  # |16-15|=1 < |12-15|=3


def test_reference_sku_empty_list_returns_none():
    assert service._reference_sku([]) is None


# --- list_products / list_brands: filter-SQL shape, query-capturing fake ---

class _QueryCapturingCursor:
    def __init__(self):
        self.queries = []
        self.params = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.queries.append(" ".join(query.split()))
        self.params.append(params)

    @property
    def description(self):
        return []

    def fetchall(self):
        return []


class _QueryCapturingConnection:
    def __init__(self):
        self._cursor = _QueryCapturingCursor()

    def cursor(self):
        return self._cursor


def test_list_products_defaults_to_status_current():
    conn = _QueryCapturingConnection()
    service.list_products(conn)

    query = conn.cursor().queries[0]
    params = conn.cursor().params[0]
    assert "p.published = true and p.status = %s" in query
    assert params[0] == "current"


def test_list_products_status_retired_is_explicit_not_default():
    conn = _QueryCapturingConnection()
    service.list_products(conn, status="retired")

    params = conn.cursor().params[0]
    assert params[0] == "retired"


def test_list_products_never_exposes_a_published_override_param():
    """published=true is baked into the SQL text itself, not a bind
    param -- confirms there's no way for a caller-supplied value to
    relax it (contrast admin_api.list_products, which takes published
    as a real optional filter)."""
    conn = _QueryCapturingConnection()
    service.list_products(conn, brand_id="brand-1", core_id="core-1",
                           coverstock_id="cs-1", search="fury")

    query = conn.cursor().queries[0]
    params = conn.cursor().params[0]
    assert "p.published = true" in query
    assert "and p.brand_id = %s" in query
    assert "and p.core_id = %s" in query
    assert "and p.coverstock_id = %s" in query
    assert "and p.name ilike %s" in query
    assert params == ["current", "brand-1", "core-1", "cs-1", "%fury%", 24, 0]


def test_list_products_orders_by_id_as_tiebreaker():
    conn = _QueryCapturingConnection()
    service.list_products(conn)

    query = conn.cursor().queries[0]
    assert "order by p.updated_at desc, p.id asc limit %s offset %s" in query


# --- popularity ranking: Al's ask -- decay view_count by video age so a
# ball's ranking reflects CURRENT popularity, not lifetime view totals.
# As of migration 030 (2026-09-06 performance fix), popularity_score is a
# plain materialized column on `products`, recomputed daily by
# refresh_product_scores/app.py -- see that module's own docstring for the
# full formula writeup (180-day half-life, chosen given bowling balls' own
# 6-12 month current-to-retired lifespan -- Al's own follow-up question).
# list_products below just reads the column now; the formula-level tests
# (averaging vs summing, half-life value, etc.) live in
# tests/test_refresh_product_scores.py instead.

def test_list_products_always_selects_popularity_score():
    """popularity_score is unconditional, not gated behind sort= -- a
    Browse card should be able to show a "trending" signal even in the
    default order. As of migration 030 this is a plain column read, not a
    correlated subquery."""
    conn = _QueryCapturingConnection()
    service.list_products(conn)

    query = conn.cursor().queries[0]
    assert "p.popularity_score" in query


def test_list_products_default_sort_is_unaffected_by_popularity_column():
    """Adding popularity_score to the SELECT list must not change the
    default order-by or any existing bind param position."""
    conn = _QueryCapturingConnection()
    service.list_products(conn, brand_id="brand-1")

    query = conn.cursor().queries[0]
    params = conn.cursor().params[0]
    assert "order by p.updated_at desc, p.id asc limit %s offset %s" in query
    assert params == ["current", "brand-1", 24, 0]


def test_list_products_sort_popularity_orders_by_score_desc():
    conn = _QueryCapturingConnection()
    service.list_products(conn, sort="popularity")

    query = conn.cursor().queries[0]
    assert "order by p.popularity_score desc, p.id asc limit %s offset %s" in query


def test_list_products_unrecognized_sort_value_falls_back_to_default_order():
    conn = _QueryCapturingConnection()
    service.list_products(conn, sort="views_all_time")

    query = conn.cursor().queries[0]
    assert "order by p.updated_at desc, p.id asc limit %s offset %s" in query


# --- common-sense sort options (Al's ask: "lets add some common sense
# sort options for both the admin and consumer UIs") -- newest/oldest by
# release_date, alphabetical by name. See service.py's _SORT_ORDER_BY.

def test_list_products_sort_newest_orders_by_coalesced_release_date_desc():
    """Was p.release_date alone with nulls last (see admin_api/service.py's
    identical fix + writeup) -- fixed here too, same "kept in sync by
    hand" copy this module's own _SORT_ORDER_BY comment already flags."""
    conn = _QueryCapturingConnection()
    service.list_products(conn, sort="newest")

    query = conn.cursor().queries[0]
    assert "order by coalesce(p.release_date, p.first_seen_at::date) desc, p.id asc limit %s offset %s" in query


def test_list_products_sort_oldest_orders_by_coalesced_release_date_asc():
    conn = _QueryCapturingConnection()
    service.list_products(conn, sort="oldest")

    query = conn.cursor().queries[0]
    assert "order by coalesce(p.release_date, p.first_seen_at::date) asc, p.id asc limit %s offset %s" in query


def test_list_products_sort_name_asc_orders_alphabetically():
    conn = _QueryCapturingConnection()
    service.list_products(conn, sort="name_asc")

    query = conn.cursor().queries[0]
    assert "order by p.name asc, p.id asc limit %s offset %s" in query


def test_list_products_sort_name_desc_orders_reverse_alphabetically():
    conn = _QueryCapturingConnection()
    service.list_products(conn, sort="name_desc")

    query = conn.cursor().queries[0]
    assert "order by p.name desc, p.id asc limit %s offset %s" in query


def test_list_products_every_sort_option_keeps_id_tiebreaker():
    """Pagination has to stay stable no matter which column is doing the
    primary ordering -- every branch in _SORT_ORDER_BY must end in the
    same `, p.id asc` tiebreaker."""
    conn = _QueryCapturingConnection()
    for sort_value in service._SORT_ORDER_BY:
        conn.cursor().queries.clear()
        service.list_products(conn, sort=sort_value)
        query = conn.cursor().queries[0]
        assert ", p.id asc limit %s offset %s" in query, f"sort={sort_value!r} missing id tiebreaker"


def test_list_brands_only_brands_with_published_products():
    conn = _QueryCapturingConnection()
    service.list_brands(conn)

    query = conn.cursor().queries[0]
    assert "where p.published = true" in query
    assert "join products p on p.brand_id = b.id" in query


# --- list_articles: filter-SQL shape, same query-capturing fake
# connection as list_products above (Learn section browse/index page) ---

def test_list_articles_requires_approved_status_and_published_product():
    conn = _QueryCapturingConnection()
    service.list_articles(conn)

    query = conn.cursor().queries[0]
    assert "pa.status = 'approved'" in query
    assert "p.published = true" in query


def test_list_articles_no_published_override_param():
    """Same non-negotiable posture as list_products -- published/approved
    are baked into the SQL text, not bind params a caller could relax."""
    conn = _QueryCapturingConnection()
    service.list_articles(conn, brand_id="brand-1", coverstock_id="cs-1", search="hook")

    query = conn.cursor().queries[0]
    params = conn.cursor().params[0]
    assert "and p.brand_id = %s" in query
    assert "and p.coverstock_id = %s" in query
    assert "and (pa.title ilike %s or pa.hook ilike %s)" in query
    assert params == ["brand-1", "cs-1", "%hook%", "%hook%", 24, 0]


def test_list_articles_defaults_have_no_extra_filters():
    conn = _QueryCapturingConnection()
    service.list_articles(conn)

    query = conn.cursor().queries[0]
    params = conn.cursor().params[0]
    assert "and p.brand_id = %s" not in query
    assert "and p.coverstock_id = %s" not in query
    assert "ilike" not in query
    assert params == [24, 0]


def test_list_articles_joins_products_and_brands():
    conn = _QueryCapturingConnection()
    service.list_articles(conn)

    query = conn.cursor().queries[0]
    assert "join products p on p.id = pa.product_id" in query
    assert "join brands b on b.id = p.brand_id" in query


def test_list_articles_selects_product_shot_image_url():
    """Al: 'can we use the product shot for the card in the list of
    review articles' -- the Learn index card's <img> needs the article's
    own AI-generated product_shot_image_url alongside the existing
    primary_image_url fallback, not just the raw scraped photo."""
    conn = _QueryCapturingConnection()
    service.list_articles(conn)

    query = conn.cursor().queries[0]
    assert "pa.product_shot_image_url" in query


def test_list_articles_selects_first_published_at():
    """033_product_articles_first_published_at.sql -- Al: 'add published
    dates and last updated dates to the articles.' The Learn index card
    needs this alongside reviewed_at (see ArticleCard's own docstring on
    why generated_at/reviewed_at alone couldn't answer this once an
    article's ever been regenerated)."""
    conn = _QueryCapturingConnection()
    service.list_articles(conn)

    query = conn.cursor().queries[0]
    assert "pa.first_published_at" in query


def test_list_articles_default_sort_is_reviewed_at_desc():
    conn = _QueryCapturingConnection()
    service.list_articles(conn)

    query = conn.cursor().queries[0]
    assert "order by pa.reviewed_at desc nulls last, pa.id asc" in query


def test_list_articles_sort_oldest_orders_reviewed_at_asc():
    conn = _QueryCapturingConnection()
    service.list_articles(conn, sort="oldest")

    query = conn.cursor().queries[0]
    assert "order by pa.reviewed_at asc nulls last, pa.id asc" in query


def test_list_articles_sort_title_asc_orders_alphabetically():
    conn = _QueryCapturingConnection()
    service.list_articles(conn, sort="title_asc")

    query = conn.cursor().queries[0]
    assert "order by pa.title asc, pa.id asc" in query


def test_list_articles_sort_title_desc_orders_reverse_alphabetically():
    conn = _QueryCapturingConnection()
    service.list_articles(conn, sort="title_desc")

    query = conn.cursor().queries[0]
    assert "order by pa.title desc, pa.id asc" in query


def test_list_articles_unrecognized_sort_falls_back_to_default():
    conn = _QueryCapturingConnection()
    service.list_articles(conn, sort="not_a_real_sort")

    query = conn.cursor().queries[0]
    assert "order by pa.reviewed_at desc nulls last, pa.id asc" in query


def test_list_articles_every_sort_option_keeps_id_tiebreaker():
    conn = _QueryCapturingConnection()
    for sort_value in service._ARTICLE_SORT_ORDER_BY:
        conn.cursor().queries.clear()
        service.list_articles(conn, sort=sort_value)
        query = conn.cursor().queries[0]
        assert ", pa.id asc limit %s offset %s" in query, f"sort={sort_value!r} missing id tiebreaker"


def test_list_articles_passes_through_limit_and_offset():
    conn = _QueryCapturingConnection()
    service.list_articles(conn, limit=10, offset=20)

    params = conn.cursor().params[0]
    assert params[-2:] == [10, 20]


def test_list_articles_category_id_filters_and_is_first_bind_param():
    """Migration 031 -- category_id filters the SAME way brand_id/
    coverstock_id do (scoped to the article's own persisted pa.category_id,
    not a live join back through product_type). Al's category filter
    dropdown on the Learn index page needs this to actually narrow results."""
    conn = _QueryCapturingConnection()
    service.list_articles(conn, category_id="cat-1")

    query = conn.cursor().queries[0]
    params = conn.cursor().params[0]
    assert "and pa.category_id = %s" in query
    assert params == ["cat-1", 24, 0]


def test_list_articles_no_category_filter_by_default():
    conn = _QueryCapturingConnection()
    service.list_articles(conn)

    query = conn.cursor().queries[0]
    assert "and pa.category_id = %s" not in query


def test_list_articles_joins_categories_and_article_types():
    """The card list needs the same category/article_type eyebrow label
    the detail page shows (ArticleCard.tsx: "Bowling Balls · Ball
    Review") -- left-joined so a pre-migration/unmapped article still
    lists, just without those four fields."""
    conn = _QueryCapturingConnection()
    service.list_articles(conn)

    query = conn.cursor().queries[0]
    assert "left join categories cat on cat.id = pa.category_id" in query
    assert "left join article_types atype on atype.id = pa.article_type_id" in query
    assert "cat.name as category_name" in query
    assert "atype.name as article_type_name" in query


# --- list_categories (GET /categories, Learn-site taxonomy) --

def test_list_categories_queries_categories_and_article_types_tables():
    """Al, picking a Learn theme: 'having Categories with one being
    Bowling balls and Ball review being a type of article' -- the
    nav/eyebrow labels this backs need both lookup tables read, not just
    categories, so a category with no article_types yet still nests an
    empty list rather than erroring."""
    conn = _QueryCapturingConnection()
    service.list_categories(conn)

    queries = conn.cursor().queries
    assert any("from categories" in q for q in queries)
    assert any("from article_types" in q for q in queries)


def test_list_categories_orders_by_display_order():
    conn = _QueryCapturingConnection()
    service.list_categories(conn)

    queries = conn.cursor().queries
    assert any("order by display_order, name" in q for q in queries)


# --- get_product / get_products_compare / list_similar_products:
# multi-query assembly against a hand-built fake cursor ---

def _derive_primary_image_url(db, pid, p):
    """Mirrors the real SQL's coalesce(...) exactly (see service.py's
    four query sites, all identical since Al's ask: "the images have
    hidden and thumbnail attributes and sorting... use the thumbnail
    for the main image") -- prefer the visible image an admin flagged
    is_thumbnail, then the first visible image by display_order, then
    the raw (can-go-stale) products.primary_image_url column as a last
    resort. A naive `p.get("primary_image_url")` pass-through here
    would silently mask exactly the bug Al hit: the fixture agreeing
    with itself while the real query behaves differently."""
    visible = [img for img in db["images"].get(pid, []) if img.get("is_visible", True)]
    if visible:
        visible.sort(key=lambda img: (0 if img.get("is_thumbnail") else 1, img["display_order"], img["id"]))
        return visible[0].get("stored_url")
    return p.get("primary_image_url")


def _derive_bigcommerce_offer(db, pid):
    """Mirrors get_product_article's ecom_source/ecom_price LATERAL joins:
    picks the most recently checked approved+active BigCommerce
    ('api'/'bigcommerce') product_price_sources row for this product, then
    returns ITS OWN price/currency/in_stock (014/016_price_tracking*.sql --
    see get_product_article's own docstring on reusing this existing data
    instead of a new migration/fabricated rating). Returns an all-None
    dict when price_checker hasn't matched/approved a source yet.
    db["price_sources"] is a flat per-product list of dicts (api_provider/
    status/is_active/product_url/last_checked_at/price/currency/in_stock)
    -- same "flat dict, not a real join" simplification the rest of this
    fixture already uses for cores/coverstocks/images; last_checked_at
    does double duty here for both "which source is chosen" (real
    product_price_sources.last_checked_at) and "that source's latest
    price snapshot" (real product_price_history.checked_at) since this
    project has only ever had one BigCommerce source per product in
    practice -- see test_..._picks_most_recently_checked_source, the one
    test that actually exercises this field."""
    candidates = [
        s for s in db.get("price_sources", {}).get(pid, [])
        if s.get("api_provider") == "bigcommerce" and s.get("status") == "approved" and s.get("is_active", True)
    ]
    if not candidates:
        return {"product_url": None, "price": None, "currency": None, "in_stock": None}
    candidates.sort(key=lambda s: s.get("last_checked_at") or "", reverse=True)
    chosen = candidates[0]
    return {
        "product_url": chosen["product_url"],
        "price": chosen.get("price"),
        "currency": chosen.get("currency"),
        "in_stock": chosen.get("in_stock"),
    }


class _FakeCursor:
    def __init__(self, db):
        self.db = db
        self._result_rows = []
        self._result_row = None
        self._description = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        q = " ".join(query.split())
        params = params or ()

        if q.startswith("select distinct b.id, b.name"):
            rows = [(bid, b["name"]) for bid, b in sorted(self.db["brands"].items(), key=lambda kv: kv[1]["name"])
                     if any(p["brand_id"] == bid and p["published"] for p in self.db["products"].values())]
            self._description = [("id",), ("name",)]
            self._result_rows = rows

        elif q.startswith("select p.id, p.name, p.url, p.color, p.status,"):
            status = params[0]
            rows = []
            for pid, p in self.db["products"].items():
                if p["published"] and p["status"] == status:
                    core = self.db["cores"].get(p.get("core_id"), {})
                    rows.append((
                        pid, p["name"], p["url"], p.get("color"), p["status"],
                        self.db["brands"][p["brand_id"]]["name"],
                        core.get("name"), core.get("core_type"),
                        p.get("coverstock_name"), p.get("coverstock_type"), p.get("coverstock_material"),
                        p.get("release_date"), _derive_primary_image_url(self.db, pid, p),
                        p.get("video_reviews_summary_video_count", 0),
                    ))
            self._description = [(c,) for c in (
                "id", "name", "url", "color", "status", "brand_name", "core_name", "core_type",
                "coverstock_name", "coverstock_type", "coverstock_material", "release_date",
                "primary_image_url", "video_reviews_summary_video_count",
            )]
            self._result_rows = rows

        elif q.startswith("select p.id, p.name, p.url, p.color, p.coverstock_material,"):
            pid = params[0]
            p = self.db["products"].get(pid)
            if p is None or not p["published"]:
                self._result_row = None
                self._description = [("_",)]
            else:
                core = self.db["cores"].get(p.get("core_id"), {})
                cs = self.db["coverstocks"].get(p.get("coverstock_id"), {})
                self._description = [(c,) for c in (
                    "id", "name", "url", "color", "coverstock_material", "coverstock_type",
                    "coverstock_name", "has_particle", "has_custom_graphic", "factory_finish",
                    "part_number", "weights_min", "weights_max", "usbc_approval_date", "release_date",
                    "description", "status", "primary_image_url", "video_reviews_summary",
                    "video_reviews_summary_video_count", "video_reviews_summary_updated_at",
                    "brand_id", "brand_name", "manufacturer_name", "core_id", "core_name", "core_type",
                    "coverstock_id", "coverstock_full_name",
                )]
                # Fixture stores weights_available as a plain (min, max)
                # tuple -- real Postgres/psycopg2 returns lower()/upper()
                # as separate int columns per the ::uuid[]-style fix in
                # service.get_product (see its comment); simulate the
                # same two-column shape here rather than the raw range
                # value so this fixture can't mask the bug it caused.
                weights = p.get("weights_available")
                weights_min, weights_max = weights if weights else (None, None)
                self._result_row = (
                    pid, p["name"], p["url"], p.get("color"), p.get("coverstock_material"),
                    p.get("coverstock_type"), p.get("coverstock_name"), p.get("has_particle", False),
                    p.get("has_custom_graphic", False), p.get("factory_finish"), p.get("part_number"),
                    weights_min, weights_max, p.get("usbc_approval_date"), p.get("release_date"),
                    p.get("description"), p["status"], _derive_primary_image_url(self.db, pid, p),
                    p.get("video_reviews_summary"), p.get("video_reviews_summary_video_count", 0),
                    p.get("video_reviews_summary_updated_at"), p["brand_id"],
                    self.db["brands"][p["brand_id"]]["name"],
                    self.db["brands"][p["brand_id"]].get("manufacturer_name"),
                    p.get("core_id"), core.get("name"), core.get("core_type"),
                    p.get("coverstock_id"), cs.get("name"),
                )

        elif q == "select weight_lbs, rg, differential, mass_bias from product_skus where product_id = %s order by weight_lbs desc":
            pid = params[0]
            self._description = [("weight_lbs",), ("rg",), ("differential",), ("mass_bias",)]
            self._result_rows = [
                (s["weight_lbs"], s.get("rg"), s.get("differential"), s.get("mass_bias"))
                for s in sorted(self.db["skus"].get(pid, []), key=lambda s: -s["weight_lbs"])
            ]

        elif q.startswith("select id, image_type, stored_url, is_thumbnail, display_order"):
            pid = params[0]
            self._description = [("id",), ("image_type",), ("stored_url",), ("is_thumbnail",), ("display_order",)]
            self._result_rows = [
                (img["id"], img["image_type"], img.get("stored_url"), img.get("is_thumbnail", False), img["display_order"])
                for img in self.db["images"].get(pid, []) if img.get("is_visible", True)
            ]

        elif q.startswith("select youtube_video_id, title, channel_title,"):
            pid = params[0]
            self._description = [("youtube_video_id",), ("title",), ("channel_title",), ("published_at",), ("thumbnail_url",), ("summary",)]
            self._result_rows = [
                (v["youtube_video_id"], v.get("title"), v.get("channel_title"), v.get("published_at"),
                 v.get("thumbnail_url"), v.get("summary"))
                for v in self.db["videos"].get(pid, [])
                if v.get("status") == "approved" and v.get("summary") is not None
            ]

        elif q.startswith("select p.id, c.core_type, p.coverstock_type, p.coverstock_material from products p"):
            pid = params[0]
            p = self.db["products"].get(pid)
            if p is None or not p["published"]:
                self._result_row = None
                self._description = [("_",)]
            else:
                core = self.db["cores"].get(p.get("core_id"), {})
                self._description = [("id",), ("core_type",), ("coverstock_type",), ("coverstock_material",)]
                self._result_row = (pid, core.get("core_type"), p.get("coverstock_type"), p.get("coverstock_material"))

        elif q == "select weight_lbs, rg, differential from product_skus where product_id = %s and rg is not null":
            pid = params[0]
            self._description = [("weight_lbs",), ("rg",), ("differential",)]
            self._result_rows = [
                (s["weight_lbs"], s["rg"], s.get("differential"))
                for s in self.db["skus"].get(pid, []) if s.get("rg") is not None
            ]

        elif q.startswith("select p.id, p.name, p.url, p.color, b.name as brand_name,"):
            exclude_id = params[0]
            self._description = [(c,) for c in (
                "id", "name", "url", "color", "brand_name", "core_type", "coverstock_type",
                "coverstock_material", "coverstock_name", "primary_image_url",
            )]
            rows = []
            for pid, p in self.db["products"].items():
                if p["published"] and p["status"] == "current" and pid != exclude_id:
                    core = self.db["cores"].get(p.get("core_id"), {})
                    rows.append((
                        pid, p["name"], p["url"], p.get("color"), self.db["brands"][p["brand_id"]]["name"],
                        core.get("core_type"), p.get("coverstock_type"), p.get("coverstock_material"),
                        p.get("coverstock_name"), _derive_primary_image_url(self.db, pid, p),
                    ))
            self._result_rows = rows

        elif q.startswith("select product_id, weight_lbs, rg, differential from product_skus where product_id = any(%s::uuid[]) and rg is not null"):
            ids = set(params[0])
            self._description = [("product_id",), ("weight_lbs",), ("rg",), ("differential",)]
            rows = []
            for pid in ids:
                for s in self.db["skus"].get(pid, []):
                    if s.get("rg") is not None:
                        rows.append((pid, s["weight_lbs"], s["rg"], s.get("differential")))
            self._result_rows = rows

        elif q.startswith("select p.id, p.name, p.url, b.name as brand_name, c.core_type, p.coverstock_type, p.coverstock_material, p.has_particle,"):
            # Same select list for both branches list_plotter_positions can
            # build (status-filtered vs. ids-filtered) -- see that
            # function's own comment on why the WHERE clause is the only
            # thing that differs. Distinguish by looking for the ids-branch
            # WHERE text rather than by params shape (a status string and a
            # one-element ids list would otherwise be indistinguishable).
            if "p.id = any(" in q:
                wanted = set(params[0])

                def _matches(pid, p):
                    return p["published"] and pid in wanted
            else:
                status = params[0]

                def _matches(pid, p):
                    return p["published"] and p["status"] == status

            self._description = [(c,) for c in (
                "id", "name", "url", "brand_name", "core_type", "coverstock_type", "coverstock_material",
                "has_particle", "oil_rating", "motion_rating", "oil_motion_source", "primary_image_url",
            )]
            rows = []
            for pid, p in self.db["products"].items():
                if _matches(pid, p):
                    core = self.db["cores"].get(p.get("core_id"), {})
                    rows.append((
                        pid, p["name"], p["url"], self.db["brands"][p["brand_id"]]["name"],
                        core.get("core_type"), p.get("coverstock_type"), p.get("coverstock_material"),
                        p.get("has_particle", False), p.get("oil_rating"), p.get("motion_rating"),
                        p.get("oil_motion_source"), _derive_primary_image_url(self.db, pid, p),
                    ))
            self._result_rows = rows

        elif q.startswith("select product_id, weight_lbs, rg, differential from product_skus where product_id = any(%s::uuid[]) and differential is not null"):
            ids = set(params[0])
            self._description = [("product_id",), ("weight_lbs",), ("rg",), ("differential",)]
            rows = []
            for pid in ids:
                for s in self.db["skus"].get(pid, []):
                    if s.get("differential") is not None:
                        rows.append((pid, s["weight_lbs"], s.get("rg"), s["differential"]))
            self._result_rows = rows

        elif q.startswith("select p.video_reviews_summary, p.video_reviews_summary_video_count"):
            bigcommerce_product_id = params[0]
            match = self.db["bowlerdepot_products"].get(bigcommerce_product_id)
            self._description = [("video_reviews_summary",), ("video_reviews_summary_video_count",)]
            if (
                match is None
                or match["match_status"] != "matched"
                or not self.db["products"].get(match["product_id"], {}).get("published")
            ):
                self._result_row = None
            else:
                p = self.db["products"][match["product_id"]]
                self._result_row = (
                    p.get("video_reviews_summary"), p.get("video_reviews_summary_video_count", 0),
                )

        # --- get_product_article (GET /products/{id}/article) --
        # 022_product_articles.sql. Al: "this could be the backend that
        # pulls together all the creative and content for the frontend."
        # db["product_articles"] is keyed by product_id (product_articles
        # is unique on product_id -- one row per product, see that
        # migration's own header comment), value is the approved article
        # dict, same "flat dict, not a real join" fixture simplification
        # used throughout this file.

        elif q == "select id from products where id = %s and published = true":
            pid = params[0]
            p = self.db["products"].get(pid)
            self._description = [("id",)]
            self._result_row = (pid,) if (p is not None and p["published"]) else None

        elif q.startswith("select pa.id, pa.title, pa.hook, pa.performance_summary, pa.who_should_buy, pa.who_should_skip,"):
            pid = params[0]
            article = self.db.get("product_articles", {}).get(pid)
            self._description = [(c,) for c in (
                "id", "title", "hook", "performance_summary", "who_should_buy", "who_should_skip",
                "pros", "cons", "buying_tips", "verdict", "faq", "sibling_product_ids",
                "source_video_ids", "generated_at", "reviewed_at", "first_published_at",
                "action_shot_image_url", "product_shot_image_url",
                "category_name", "category_slug", "article_type_name", "article_type_slug",
            )]
            if article is None or article.get("status") != "approved":
                self._result_row = None
            else:
                # Migration 031 -- read straight off the fixture's own
                # article dict (a test that cares sets these keys
                # directly, same flat-dict-not-a-real-join simplification
                # this whole fixture already uses elsewhere); None/absent
                # is the normal pre-migration/not-yet-onboarded case.
                # first_published_at (033_product_articles_first_published_
                # at.sql) same treatment -- absent/None is the normal
                # pre-migration case, a test that cares sets it directly.
                self._result_row = (
                    article["id"], article.get("title"), article.get("hook"),
                    article.get("performance_summary"), article.get("who_should_buy", []),
                    article.get("who_should_skip", []), article.get("pros", []), article.get("cons", []),
                    article.get("buying_tips"), article.get("verdict"), article.get("faq", []),
                    article.get("sibling_product_ids", []), article.get("source_video_ids", []),
                    article.get("generated_at"), article.get("reviewed_at"), article.get("first_published_at"),
                    article.get("action_shot_image_url"), article.get("product_shot_image_url"),
                    article.get("category_name"), article.get("category_slug"),
                    article.get("article_type_name"), article.get("article_type_slug"),
                )

        elif q.startswith("select p.name, p.url, p.status, c.name as core_name, c.core_type,"):
            pid = params[0]
            p = self.db["products"].get(pid)
            self._description = [(c,) for c in (
                "name", "url", "status", "core_name", "core_type", "coverstock_name", "coverstock_type",
                "brand_id", "brand_name", "primary_image_url", "ecommerce_url",
                "ecommerce_price", "ecommerce_price_currency", "ecommerce_in_stock",
            )]
            if p is None:
                self._result_row = None
            else:
                core = self.db["cores"].get(p.get("core_id"), {})
                offer = _derive_bigcommerce_offer(self.db, pid)
                self._result_row = (
                    p["name"], p["url"], p["status"], core.get("name"), core.get("core_type"),
                    p.get("coverstock_name"), p.get("coverstock_type"),
                    p.get("brand_id"), self.db["brands"].get(p.get("brand_id"), {}).get("name"),
                    _derive_primary_image_url(self.db, pid, p),
                    offer["product_url"], offer["price"], offer["currency"], offer["in_stock"],
                )

        elif q.startswith("select p.id, p.name, p.url, c.name as core_name, p.coverstock_name,") and "p.brand_id = %s" in q:
            # Brand lineup carousel -- see service.get_product_article's
            # own comment on why this is a fresh brand_id query rather
            # than sibling_product_ids-based like comparison_table below
            # (same select-list prefix, distinguished here by the brand_id
            # WHERE clause, same "look at the query text" approach
            # list_plotter_positions' own fixture branch above uses).
            brand_id, exclude_id = params
            self._description = [(c,) for c in (
                "id", "name", "url", "core_name", "coverstock_name", "primary_image_url", "ecommerce_url",
                "ecommerce_price", "ecommerce_price_currency", "ecommerce_in_stock",
            )]
            rows = []
            for pid, p in self.db["products"].items():
                if (
                    p.get("brand_id") == brand_id
                    and p["status"] == "current"
                    and p["published"]
                    and pid != exclude_id
                ):
                    core = self.db["cores"].get(p.get("core_id"), {})
                    offer = _derive_bigcommerce_offer(self.db, pid)
                    rows.append((
                        pid, p["name"], p["url"], core.get("name"), p.get("coverstock_name"),
                        _derive_primary_image_url(self.db, pid, p),
                        offer["product_url"], offer["price"], offer["currency"], offer["in_stock"],
                    ))
            # order by ecom_price.price desc nulls last, p.name -- same
            # two-pass stable-sort shape related_reviews' own branch below
            # uses for its own nulls-last ordering.
            rows.sort(key=lambda r: r[1])
            rows.sort(key=lambda r: (r[7] is not None, r[7] if r[7] is not None else 0), reverse=True)
            self._result_rows = rows[:30]

        elif q.startswith("select p.id, p.name, p.url, c.name as core_name, p.coverstock_name,"):
            wanted = set(params[0])
            self._description = [(c,) for c in (
                "id", "name", "url", "core_name", "coverstock_name", "primary_image_url", "ecommerce_url",
                "ecommerce_price", "ecommerce_price_currency", "ecommerce_in_stock",
            )]
            rows = []
            for pid in wanted:
                p = self.db["products"].get(pid)
                if p is None or not p["published"]:
                    continue  # unpublished/deleted sibling silently drops out -- see service docstring
                core = self.db["cores"].get(p.get("core_id"), {})
                offer = _derive_bigcommerce_offer(self.db, pid)
                rows.append((
                    pid, p["name"], p["url"], core.get("name"), p.get("coverstock_name"),
                    _derive_primary_image_url(self.db, pid, p),
                    offer["product_url"], offer["price"], offer["currency"], offer["in_stock"],
                ))
            rows.sort(key=lambda r: r[1])  # order by p.name
            self._result_rows = rows

        elif q.startswith("select p.id as product_id, p.name as product_name,"):
            # related_reviews -- same sibling_product_ids source as
            # comparison_table above, but narrowed (via the fixture's own
            # db["product_articles"] lookup) to siblings with their OWN
            # approved article, same "only 022 rows with status='approved'
            # count" rule get_product_article's main article lookup uses.
            wanted = set(params[0])
            self._description = [(c,) for c in (
                "product_id", "product_name", "article_id", "title", "hook", "reviewed_at",
                "primary_image_url",
            )]
            rows = []
            for pid in wanted:
                p = self.db["products"].get(pid)
                if p is None or not p["published"]:
                    continue
                sib_article = self.db.get("product_articles", {}).get(pid)
                if sib_article is None or sib_article.get("status") != "approved":
                    continue
                rows.append((
                    pid, p["name"], sib_article["id"], sib_article.get("title"), sib_article.get("hook"),
                    sib_article.get("reviewed_at"),
                    _derive_primary_image_url(self.db, pid, p),
                ))
            # order by pa.reviewed_at desc nulls last, p.name -- name-asc
            # first (stable sort keeps it as the tie-break), then group/
            # order by reviewed_at descending with None pushed last.
            rows.sort(key=lambda r: r[1])
            rows.sort(key=lambda r: (r[5] is not None, r[5] or ""), reverse=True)
            self._result_rows = rows

        else:
            raise NotImplementedError(f"FakeCursor doesn't support: {q}")

    @property
    def description(self):
        return self._description

    def fetchone(self):
        return self._result_row

    def fetchall(self):
        return self._result_rows


class _FakeConnection:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return _FakeCursor(self.db)


def _fresh_db():
    return {
        "brands": {}, "products": {}, "cores": {}, "coverstocks": {}, "skus": {}, "images": {},
        "videos": {}, "bowlerdepot_products": {}, "product_articles": {}, "price_sources": {},
    }


def _seed_published_current_product(db, pid="prod-1", brand_id="brand-1", **overrides):
    db["brands"].setdefault(brand_id, {"name": "Brunswick", "manufacturer_name": "Brunswick Corp"})
    product = {
        "name": "Fury", "url": "https://example.com/fury", "brand_id": brand_id,
        "published": True, "status": "current", "core_id": None, "coverstock_id": None,
        "video_reviews_summary_video_count": 0,
    }
    product.update(overrides)
    db["products"][pid] = product
    return pid


def _seed_bowlerdepot_match(db, product_id, bigcommerce_product_id, match_status="matched"):
    """bowlerdepot_products is keyed by bigcommerce_product_id in this
    fixture (a plain string, matching what the real column stores and
    what the query below filters on), not by our own product_id --
    mirrors migration 018's real unique constraint being on product_id,
    but the lookup this fixture backs (get_video_summary_by_bigcommerce_
    product_id) always starts FROM the BigCommerce id, so that's the more
    useful key here."""
    db["bowlerdepot_products"][bigcommerce_product_id] = {
        "product_id": product_id, "match_status": match_status,
    }


def _seed_bigcommerce_price_source(
    db, product_id, product_url, status="approved", is_active=True, last_checked_at=None,
    price=None, currency=None, in_stock=None,
):
    """014/016_price_tracking*.sql -- an approved+active BigCommerce
    ('api'/'bigcommerce') product_price_sources row, the exact data
    get_product_article's ecommerce_url/ecommerce_price/
    ecommerce_price_currency/ecommerce_in_stock fields read (see
    _derive_bigcommerce_offer's own docstring). price/currency/in_stock
    default to None -- a real approved price SOURCE existing (this
    function always creates one) doesn't imply price_checker has ever
    successfully CHECKED it yet; tests for ecommerce_url alone don't need
    to pass these."""
    db["price_sources"].setdefault(product_id, []).append({
        "api_provider": "bigcommerce", "status": status, "is_active": is_active,
        "product_url": product_url, "last_checked_at": last_checked_at,
        "price": price, "currency": currency, "in_stock": in_stock,
    })


# get_product

def test_get_product_returns_none_for_missing_id():
    db = _fresh_db()
    assert service.get_product(_FakeConnection(db), "no-such-id") is None


def test_get_product_returns_none_for_unpublished_product():
    """The identical-404 guarantee this module's docstring promises --
    exists-but-unpublished must come back exactly like doesn't-exist."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, published=False)
    assert service.get_product(_FakeConnection(db), pid) is None


def test_get_product_assembles_skus_images_and_approved_summarized_videos():
    db = _fresh_db()
    pid = _seed_published_current_product(db)
    db["skus"][pid] = [
        {"weight_lbs": 16, "rg": 2.49, "differential": 0.045, "mass_bias": None},
        {"weight_lbs": 15, "rg": 2.51, "differential": 0.047, "mass_bias": None},
    ]
    db["images"][pid] = [
        {"id": "img-1", "image_type": "main", "stored_url": "https://s3/main.png", "is_thumbnail": True, "display_order": 0, "is_visible": True},
        {"id": "img-2", "image_type": "other", "stored_url": "https://s3/hidden.png", "is_thumbnail": False, "display_order": 1, "is_visible": False},
    ]
    db["videos"][pid] = [
        {"youtube_video_id": "yt-approved", "title": "Review", "channel_title": "Bowler Channel",
         "published_at": "2026-01-01", "thumbnail_url": "https://yt/thumb.jpg",
         "summary": "Great ball.", "status": "approved"},
        {"youtube_video_id": "yt-pending", "title": "Unreviewed", "channel_title": "X",
         "published_at": "2026-01-02", "thumbnail_url": None, "summary": None, "status": "pending"},
        {"youtube_video_id": "yt-approved-no-summary", "title": "Not summarized yet", "channel_title": "X",
         "published_at": "2026-01-03", "thumbnail_url": None, "summary": None, "status": "approved"},
    ]

    product = service.get_product(_FakeConnection(db), pid)

    assert product["id"] == pid
    assert len(product["skus"]) == 2
    assert product["skus"][0]["weight_lbs"] == 16  # order by weight_lbs desc

    assert len(product["images"]) == 1  # hidden image excluded
    assert product["images"][0]["id"] == "img-1"

    assert len(product["videos"]) == 1  # only approved + summary is not null
    assert product["videos"][0]["youtube_video_id"] == "yt-approved"


def test_get_product_formats_weights_available_as_string_not_range_object():
    """Regression test for the real bug Al hit: selecting p.weights_available
    directly (an int4range column) let a raw range value reach FastAPI's
    jsonable_encoder, which serialized it as "{}" -- an empty object the
    consumer site's React detail page then crashed trying to render
    ("Objects are not valid as a React child"). get_product must return a
    plain human-readable string (or None), never anything object-shaped.
    Postgres stores a discrete range in exclusive-upper canonical form, so
    (12, 17) here represents a ball available in 12-16 lb."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, weights_available=(12, 17))

    product = service.get_product(_FakeConnection(db), pid)

    assert product["weights_available"] == "12-16 lb"
    assert isinstance(product["weights_available"], str)


def test_get_product_weights_available_none_when_unset():
    db = _fresh_db()
    pid = _seed_published_current_product(db)

    product = service.get_product(_FakeConnection(db), pid)

    assert product["weights_available"] is None


def test_get_product_uses_thumbnail_flagged_image_not_stale_primary_image_url_column():
    """Regression test for Al's ask: "the images have hidden and
    thumbnail attributes and sorting... use the thumbnail for the main
    image". products.primary_image_url is a separate column an admin's
    PATCH /products/{id}/images/{image_id} thumbnail toggle never
    touches (see admin_api.update_product_image), so it can go stale
    relative to whichever image is actually flagged is_thumbnail --
    get_product (and every other public_api query surfacing primary_
    image_url) must prefer the flagged image over that raw column."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, primary_image_url="https://s3/stale-main.png")
    db["images"][pid] = [
        {"id": "img-1", "image_type": "main", "stored_url": "https://s3/stale-main.png",
         "is_thumbnail": False, "display_order": 0, "is_visible": True},
        {"id": "img-2", "image_type": "other", "stored_url": "https://s3/actual-thumb.png",
         "is_thumbnail": True, "display_order": 1, "is_visible": True},
    ]

    product = service.get_product(_FakeConnection(db), pid)

    assert product["primary_image_url"] == "https://s3/actual-thumb.png"


def test_get_product_falls_back_to_first_visible_image_when_no_thumbnail_flagged():
    db = _fresh_db()
    pid = _seed_published_current_product(db, primary_image_url="https://s3/stale-main.png")
    db["images"][pid] = [
        {"id": "img-1", "image_type": "main", "stored_url": "https://s3/first.png",
         "is_thumbnail": False, "display_order": 0, "is_visible": True},
        {"id": "img-2", "image_type": "other", "stored_url": "https://s3/second.png",
         "is_thumbnail": False, "display_order": 1, "is_visible": True},
    ]

    product = service.get_product(_FakeConnection(db), pid)

    assert product["primary_image_url"] == "https://s3/first.png"


def test_get_product_falls_back_to_raw_column_when_no_images_at_all():
    """A legacy row predating product_images tracking (or one where
    every image is hidden) still needs SOME image rather than None."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, primary_image_url="https://s3/legacy.png")

    product = service.get_product(_FakeConnection(db), pid)

    assert product["primary_image_url"] == "https://s3/legacy.png"


def test_list_products_uses_thumbnail_flagged_image():
    db = _fresh_db()
    pid = _seed_published_current_product(db, primary_image_url="https://s3/stale.png")
    db["images"][pid] = [
        {"id": "img-1", "image_type": "main", "stored_url": "https://s3/stale.png",
         "is_thumbnail": False, "display_order": 0, "is_visible": True},
        {"id": "img-2", "image_type": "other", "stored_url": "https://s3/thumb.png",
         "is_thumbnail": True, "display_order": 1, "is_visible": True},
    ]

    results = service.list_products(_FakeConnection(db))

    assert results[0]["primary_image_url"] == "https://s3/thumb.png"


# get_products_compare

def test_get_products_compare_preserves_input_order():
    db = _fresh_db()
    p1 = _seed_published_current_product(db, pid="prod-1", name="Alpha")
    p2 = _seed_published_current_product(db, pid="prod-2", name="Beta")
    p3 = _seed_published_current_product(db, pid="prod-3", name="Gamma")

    result = service.get_products_compare(_FakeConnection(db), [p3, p1, p2])

    assert [r["id"] for r in result] == [p3, p1, p2]


def test_get_products_compare_silently_drops_missing_and_unpublished():
    db = _fresh_db()
    p1 = _seed_published_current_product(db, pid="prod-1")
    p2 = _seed_published_current_product(db, pid="prod-2", published=False)

    result = service.get_products_compare(_FakeConnection(db), [p1, p2, "no-such-id"])

    assert [r["id"] for r in result] == [p1]


def test_get_products_compare_caps_at_max_compare_ids():
    db = _fresh_db()
    ids = []
    for i in range(service.MAX_COMPARE_IDS + 3):
        pid = f"prod-{i}"
        _seed_published_current_product(db, pid=pid)
        ids.append(pid)

    result = service.get_products_compare(_FakeConnection(db), ids)

    assert len(result) == service.MAX_COMPARE_IDS
    assert [r["id"] for r in result] == ids[:service.MAX_COMPARE_IDS]


# list_similar_products

def test_list_similar_products_returns_empty_for_missing_source():
    db = _fresh_db()
    assert service.list_similar_products(_FakeConnection(db), "no-such-id") == []


def test_list_similar_products_excludes_source_and_non_current_and_unpublished():
    db = _fresh_db()
    retired = _seed_published_current_product(
        db, pid="retired-1", status="retired", core_id="core-a", coverstock_type="hybrid",
        coverstock_material="reactive_resin",
    )
    db["cores"]["core-a"] = {"name": "Old Core", "core_type": "asymmetric"}
    db["skus"]["retired-1"] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]

    close_current = _seed_published_current_product(
        db, pid="current-close", status="current", core_id="core-b",
        coverstock_type="hybrid", coverstock_material="reactive_resin",
    )
    db["cores"]["core-b"] = {"name": "New Core", "core_type": "asymmetric"}
    db["skus"]["current-close"] = [{"weight_lbs": 15, "rg": 2.51, "differential": 0.049}]

    far_current = _seed_published_current_product(
        db, pid="current-far", status="current", core_id="core-c",
        coverstock_type="solid", coverstock_material="urethane",
    )
    db["cores"]["core-c"] = {"name": "Far Core", "core_type": "symmetric"}
    db["skus"]["current-far"] = [{"weight_lbs": 15, "rg": 2.75, "differential": 0.015}]

    unpublished_current = _seed_published_current_product(
        db, pid="current-unpublished", status="current", published=False,
    )
    db["skus"]["current-unpublished"] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]

    another_retired = _seed_published_current_product(
        db, pid="retired-2", status="retired",
    )
    db["skus"]["retired-2"] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]

    results = service.list_similar_products(_FakeConnection(db), retired)
    result_ids = [r["id"] for r in results]

    assert retired not in result_ids  # source itself excluded
    assert "current-unpublished" not in result_ids  # unpublished excluded
    assert "retired-2" not in result_ids  # only status='current' candidates
    assert result_ids == ["current-close", "current-far"]  # ranked closer-first


def test_list_similar_products_respects_limit():
    db = _fresh_db()
    source = _seed_published_current_product(db, pid="retired-1", status="retired")
    db["skus"]["retired-1"] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]

    for i in range(10):
        pid = f"current-{i}"
        _seed_published_current_product(db, pid=pid, status="current")
        db["skus"][pid] = [{"weight_lbs": 15, "rg": 2.50 + i * 0.01, "differential": 0.050}]

    results = service.list_similar_products(_FakeConnection(db), source, limit=3)
    assert len(results) == 3


# --- estimate_oil_motion: pure, no DB ---

def test_estimate_oil_motion_within_valid_ranges_for_every_material_type_combo():
    materials = [None, "polyester_plastic", "urethane", "reactive_resin"]
    types = [None, "solid", "pearl", "hybrid"]
    core_types = [None, "symmetric", "asymmetric"]
    for m in materials:
        for t in types:
            for c in core_types:
                for particle in (True, False):
                    for diff in (None, 0.01, 0.065):
                        result = service.estimate_oil_motion(
                            core_type=c, coverstock_type=t, coverstock_material=m,
                            has_particle=particle, differential=diff,
                        )
                        assert service.OIL_MIN <= result["oil"] <= service.OIL_MAX
                        assert service.MOTION_MIN <= result["motion"] <= service.MOTION_MAX


def test_estimate_oil_motion_heavier_material_and_particle_increase_oil():
    poly = service.estimate_oil_motion(coverstock_material="polyester_plastic")
    solid_resin = service.estimate_oil_motion(coverstock_type="solid", coverstock_material="reactive_resin")
    particle_solid_resin = service.estimate_oil_motion(
        coverstock_type="solid", coverstock_material="reactive_resin", has_particle=True,
    )
    assert poly["oil"] < solid_resin["oil"] < particle_solid_resin["oil"]


def test_estimate_oil_motion_pearl_skids_more_than_solid():
    solid = service.estimate_oil_motion(coverstock_type="solid", coverstock_material="reactive_resin")
    pearl = service.estimate_oil_motion(coverstock_type="pearl", coverstock_material="reactive_resin")
    assert pearl["oil"] < solid["oil"]


def test_estimate_oil_motion_asymmetric_and_higher_differential_increase_motion():
    sym_low_diff = service.estimate_oil_motion(core_type="symmetric", differential=0.015)
    asym_high_diff = service.estimate_oil_motion(core_type="asymmetric", differential=0.06)
    assert sym_low_diff["motion"] < asym_high_diff["motion"]


def test_estimate_oil_motion_no_inputs_falls_back_to_midrange():
    result = service.estimate_oil_motion()
    assert service.OIL_MIN < result["oil"] < service.OIL_MAX
    assert service.MOTION_MIN < result["motion"] < service.MOTION_MAX


# --- list_plotter_positions: multi-query assembly, chart vs. estimated ---

def test_list_plotter_positions_uses_chart_value_when_set():
    db = _fresh_db()
    pid = _seed_published_current_product(
        db, core_id="core-a", coverstock_type="solid", coverstock_material="reactive_resin",
        oil_rating=6, motion_rating=18, oil_motion_source="chart",
    )
    db["cores"]["core-a"] = {"name": "Some Core", "core_type": "asymmetric"}
    db["skus"][pid] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]

    results = service.list_plotter_positions(_FakeConnection(db))

    assert len(results) == 1
    assert results[0]["oil"] == 6
    assert results[0]["motion"] == 18
    assert results[0]["oil_motion_source"] == "chart"


def test_list_plotter_positions_reads_manual_source_unchanged():
    """A manually-corrected position (admin PATCH .../plotter-position,
    migration 012) must come through as 'manual', not get relabeled
    'chart' just because it has real values set -- oil_motion_source is
    READ, never inferred from whether the rating columns are non-null."""
    db = _fresh_db()
    pid = _seed_published_current_product(
        db, core_id="core-a", coverstock_type="solid", coverstock_material="reactive_resin",
        oil_rating=9, motion_rating=11, oil_motion_source="manual",
    )
    db["cores"]["core-a"] = {"name": "Some Core", "core_type": "asymmetric"}
    db["skus"][pid] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]

    results = service.list_plotter_positions(_FakeConnection(db))

    assert results[0]["oil"] == 9
    assert results[0]["motion"] == 11
    assert results[0]["oil_motion_source"] == "manual"


def test_list_plotter_positions_estimates_when_chart_value_unset():
    db = _fresh_db()
    pid = _seed_published_current_product(
        db, core_id="core-a", coverstock_type="pearl", coverstock_material="reactive_resin",
    )
    db["cores"]["core-a"] = {"name": "Some Core", "core_type": "symmetric"}
    db["skus"][pid] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.020}]

    results = service.list_plotter_positions(_FakeConnection(db))

    assert len(results) == 1
    assert results[0]["oil_motion_source"] == "estimated"
    assert service.OIL_MIN <= results[0]["oil"] <= service.OIL_MAX
    assert service.MOTION_MIN <= results[0]["motion"] <= service.MOTION_MAX


def test_list_plotter_positions_only_published_current_by_default():
    db = _fresh_db()
    _seed_published_current_product(db, pid="current-1", status="current")
    db["skus"]["current-1"] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]
    _seed_published_current_product(db, pid="retired-1", status="retired")
    db["skus"]["retired-1"] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]
    _seed_published_current_product(db, pid="unpublished-1", published=False)
    db["skus"]["unpublished-1"] = [{"weight_lbs": 15, "rg": 2.50, "differential": 0.050}]

    results = service.list_plotter_positions(_FakeConnection(db))

    assert [r["id"] for r in results] == ["current-1"]


# --- list_plotter_positions(ids=...): the plotter page's Compare tab --
# Al: "add a tablist toggle to the ball motion plotter that is 'compare'
# and plots the currently selected balls in the compare feature."

def test_list_plotter_positions_with_ids_ignores_status():
    """The compare list is arbitrary ids a visitor picked while browsing
    -- not necessarily all 'current' -- so ids must be able to pull in a
    retired product too, unlike the default status-filtered call."""
    db = _fresh_db()
    _seed_published_current_product(db, pid="current-1", status="current")
    _seed_published_current_product(db, pid="retired-1", status="retired")
    _seed_published_current_product(db, pid="not-selected", status="current")

    results = service.list_plotter_positions(_FakeConnection(db), ids=["retired-1", "current-1"])

    assert {r["id"] for r in results} == {"retired-1", "current-1"}


def test_list_plotter_positions_with_ids_preserves_input_order():
    db = _fresh_db()
    _seed_published_current_product(db, pid="prod-a")
    _seed_published_current_product(db, pid="prod-b")
    _seed_published_current_product(db, pid="prod-c")

    results = service.list_plotter_positions(_FakeConnection(db), ids=["prod-c", "prod-a", "prod-b"])

    assert [r["id"] for r in results] == ["prod-c", "prod-a", "prod-b"]


def test_list_plotter_positions_with_ids_silently_drops_missing_and_unpublished():
    db = _fresh_db()
    _seed_published_current_product(db, pid="prod-a")
    _seed_published_current_product(db, pid="prod-unpublished", published=False)

    results = service.list_plotter_positions(
        _FakeConnection(db), ids=["prod-a", "prod-unpublished", "no-such-id"],
    )

    assert [r["id"] for r in results] == ["prod-a"]


def test_list_plotter_positions_with_ids_caps_at_max_compare_ids():
    db = _fresh_db()
    ids = [f"prod-{i}" for i in range(service.MAX_COMPARE_IDS + 3)]
    for pid in ids:
        _seed_published_current_product(db, pid=pid)

    results = service.list_plotter_positions(_FakeConnection(db), ids=ids)

    assert len(results) == service.MAX_COMPARE_IDS
    assert [r["id"] for r in results] == ids[: service.MAX_COMPARE_IDS]


def test_list_plotter_positions_empty_ids_list_falls_back_to_status():
    """An empty list (as opposed to None) still means 'no ids given' --
    the Compare tab's own empty-compare-list state is handled entirely on
    the frontend (it never calls this with ids=[] in practice), but the
    function itself shouldn't return an empty result set silently if it
    ever is."""
    db = _fresh_db()
    _seed_published_current_product(db, pid="current-1", status="current")

    results = service.list_plotter_positions(_FakeConnection(db), ids=[])

    assert [r["id"] for r in results] == ["current-1"]


def test_get_video_summary_by_bigcommerce_product_id_matched_and_published():
    db = _fresh_db()
    pid = _seed_published_current_product(
        db, pid="prod-1",
        video_reviews_summary="Reviewers love the flare potential.",
        video_reviews_summary_video_count=3,
    )
    _seed_bowlerdepot_match(db, pid, "4390", match_status="matched")

    result = service.get_video_summary_by_bigcommerce_product_id(_FakeConnection(db), "4390")

    assert result == {
        "video_reviews_summary": "Reviewers love the flare potential.",
        "video_reviews_summary_video_count": 3,
    }


def test_get_video_summary_by_bigcommerce_product_id_no_match_row():
    """No bowlerdepot_products row at all for this BigCommerce id -- the
    normal case for most of BowlerDepot's catalog (see the service
    function's own docstring: this must always 200, never 404)."""
    db = _fresh_db()

    result = service.get_video_summary_by_bigcommerce_product_id(_FakeConnection(db), "9999")

    assert result == {"video_reviews_summary": None, "video_reviews_summary_video_count": 0}


def test_get_video_summary_by_bigcommerce_product_id_unmatched_status_excluded():
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1", video_reviews_summary="Some summary")
    _seed_bowlerdepot_match(db, pid, "4390", match_status="unmatched")

    result = service.get_video_summary_by_bigcommerce_product_id(_FakeConnection(db), "4390")

    assert result == {"video_reviews_summary": None, "video_reviews_summary_video_count": 0}


def test_get_video_summary_by_bigcommerce_product_id_ambiguous_status_excluded():
    """An 'ambiguous' match is just as untrustworthy as 'unmatched' here --
    see bowlerdepot_reconciliation's own known suffix-collision incident
    (Storm iQ Tour vs. iQ Tour AI) for why showing a summary against an
    unreliable match would be worse than showing nothing."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1", video_reviews_summary="Some summary")
    _seed_bowlerdepot_match(db, pid, "4390", match_status="ambiguous")

    result = service.get_video_summary_by_bigcommerce_product_id(_FakeConnection(db), "4390")

    assert result == {"video_reviews_summary": None, "video_reviews_summary_video_count": 0}


def test_get_video_summary_by_bigcommerce_product_id_unpublished_product_excluded():
    db = _fresh_db()
    pid = _seed_published_current_product(
        db, pid="prod-1", published=False, video_reviews_summary="Some summary",
    )
    _seed_bowlerdepot_match(db, pid, "4390", match_status="matched")

    result = service.get_video_summary_by_bigcommerce_product_id(_FakeConnection(db), "4390")

    assert result == {"video_reviews_summary": None, "video_reviews_summary_video_count": 0}


def test_get_video_summary_by_bigcommerce_product_id_matched_but_no_summary_yet():
    """Matched product, but video_summarizer hasn't produced a rollup yet
    -- also normal, not an error; video_reviews_summary is null by
    default until a rollup exists."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_bowlerdepot_match(db, pid, "4390", match_status="matched")

    result = service.get_video_summary_by_bigcommerce_product_id(_FakeConnection(db), "4390")

    assert result == {"video_reviews_summary": None, "video_reviews_summary_video_count": 0}


# --- get_product_article (GET /products/{id}/article) -- 022_product_
# articles.sql. Al: "this could be the backend that pulls together all
# the creative and content for the frontend." Always-200 contract for an
# existing published product with no approved article, same as the
# bowlerdepot video-summary embed route above; None (-> app.py's 404)
# only for a nonexistent/unpublished product_id.

def _seed_approved_article(db, product_id, **overrides):
    article = {
        "id": "art-1", "status": "approved", "title": "The Fury: A Heavy-Oil Workhorse",
        "hook": "Picture this...", "performance_summary": "Strong midlane read.",
        "who_should_buy": ["Heavy oil bowlers"], "who_should_skip": ["Light oil bowlers"],
        "pros": ["Strong backend"], "cons": ["Not for light oil"], "buying_tips": "Drill for control.",
        "verdict": "A solid heavy-oil piece.", "faq": [{"question": "Q", "answer": "A"}],
        "sibling_product_ids": [], "source_video_ids": ["vid-1"], "generated_at": "2026-08-01",
    }
    article.update(overrides)
    db.setdefault("product_articles", {})[product_id] = article
    return article


def test_get_product_article_returns_none_for_nonexistent_product():
    db = _fresh_db()
    result = service.get_product_article(_FakeConnection(db), "does-not-exist")
    assert result is None


def test_get_product_article_returns_none_for_unpublished_product():
    """Same non-distinction as get_product's own 404 -- an unpublished
    product must look identical to a nonexistent one to a public caller."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1", published=False)
    result = service.get_product_article(_FakeConnection(db), pid)
    assert result is None


def test_get_product_article_returns_null_article_when_none_approved_yet():
    """The normal case for most of the catalog -- always 200 with
    article: None, not a 404, mirroring get_video_summary_by_bigcommerce_
    product_id's own always-200 contract."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result == {"product_id": pid, "article": None}


def test_get_product_article_ignores_pending_article():
    """A pending (or rejected) article is invisible here even though a
    row exists -- only 'approved' is ever surfaced publicly."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid, status="pending")

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result == {"product_id": pid, "article": None}


def test_get_product_article_returns_full_approved_article_with_live_spec_join():
    db = _fresh_db()
    db["cores"]["core-1"] = {"name": "Sonar", "core_type": "asymmetric"}
    pid = _seed_published_current_product(
        db, pid="prod-1", name="Equinox Solid", url="https://storm.com/equinox-solid",
        core_id="core-1", coverstock_name="R2S Hybrid", coverstock_type="hybrid",
    )
    db["skus"][pid] = [{"weight_lbs": 15, "rg": 2.49, "differential": 0.048, "mass_bias": None}]
    _seed_approved_article(db, pid)

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["product_id"] == pid
    article = result["article"]
    assert article["title"] == "The Fury: A Heavy-Oil Workhorse"
    assert article["faq"] == [{"question": "Q", "answer": "A"}]
    # Live-joined spec highlight -- not stored on the article row itself.
    assert article["product"]["name"] == "Equinox Solid"
    assert article["product"]["core_name"] == "Sonar"
    assert article["product"]["coverstock_name"] == "R2S Hybrid"
    assert article["product"]["skus"] == [{"weight_lbs": 15, "rg": 2.49, "differential": 0.048, "mass_bias": None}]
    # Al: "This should only show while the ball is current" -- the Learn
    # frontend's post-verdict shop CTA (ArticleDetailPage.tsx) gates on
    # this field, so it has to actually reach the payload.
    assert article["product"]["status"] == "current"


def test_get_product_article_product_status_reflects_retired_ball():
    """A retired ball keeps its article (see this function's own
    docstring on why specs/verdict are never regenerated over a status
    change), but the frontend's shop-this-ball CTA must be able to tell
    it's retired so it can hide itself."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1", status="retired")
    _seed_approved_article(db, pid)

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["article"]["product"]["status"] == "retired"


def test_get_product_article_includes_category_and_article_type_when_mapped():
    """Migration 031 -- ArticleDetailPage.tsx's 'Bowling Balls · Ball
    Review' eyebrow is read straight off these four fields."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(
        db, pid,
        category_name="Bowling Balls", category_slug="bowling-balls",
        article_type_name="Ball Review", article_type_slug="ball-review",
    )

    result = service.get_product_article(_FakeConnection(db), pid)

    article = result["article"]
    assert article["category_name"] == "Bowling Balls"
    assert article["category_slug"] == "bowling-balls"
    assert article["article_type_name"] == "Ball Review"
    assert article["article_type_slug"] == "ball-review"


def test_get_product_article_category_fields_null_for_pre_migration_article():
    """An article generated before migration 031's resolve_category_and_
    article_type wiring (or for a not-yet-onboarded product_type) has no
    category_id/article_type_id -- the left join just yields nulls, not
    an error, and the Learn detail page falls back to core_type/
    coverstock_type (see ArticleDetailPage.tsx's own taxonomyLabel)."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid)

    result = service.get_product_article(_FakeConnection(db), pid)

    article = result["article"]
    assert article.get("category_name") is None
    assert article.get("article_type_name") is None


def test_get_product_article_comparison_table_drops_unpublished_siblings():
    """sibling_product_ids on the article row is a heuristic (see
    022_product_articles.sql's own caveat) -- a sibling that's since been
    unpublished or deleted must silently drop out of comparison_table
    rather than breaking the whole article response."""
    db = _fresh_db()
    published_sibling = _seed_published_current_product(
        db, pid="sib-published", name="Equinox Hybrid", url="https://storm.com/equinox-hybrid",
    )
    unpublished_sibling = _seed_published_current_product(
        db, pid="sib-unpublished", name="Equinox Pearl", published=False,
    )
    pid = _seed_published_current_product(db, pid="prod-1", name="Equinox Solid")
    _seed_approved_article(db, pid, sibling_product_ids=[published_sibling, unpublished_sibling, "sib-deleted"])

    result = service.get_product_article(_FakeConnection(db), pid)

    comparison_ids = [row["id"] for row in result["article"]["comparison_table"]]
    assert comparison_ids == ["sib-published"]


def test_get_product_article_includes_image_urls_when_present():
    """023_product_article_images.sql -- action_shot_image_url/product_
    shot_image_url are read straight off the article row (not live-joined
    like the spec fields), so a public GET /products/{id}/article response
    includes them once image generation has produced them."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(
        db, pid,
        action_shot_image_url="https://bucket.s3.amazonaws.com/article-images/prod-1/action_shot.png",
        product_shot_image_url="https://bucket.s3.amazonaws.com/article-images/prod-1/product_shot.png",
    )

    result = service.get_product_article(_FakeConnection(db), pid)

    article = result["article"]
    assert article["action_shot_image_url"] == "https://bucket.s3.amazonaws.com/article-images/prod-1/action_shot.png"
    assert article["product_shot_image_url"] == "https://bucket.s3.amazonaws.com/article-images/prod-1/product_shot.png"


def test_get_product_article_image_urls_null_when_not_yet_generated():
    """The normal case for most articles right after 022's own text
    generation -- image generation hasn't run (or hasn't succeeded) yet,
    so both URLs are null rather than missing keys, matching this
    function's own always-shaped-response contract."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid)  # no image overrides -- defaults to no image columns at all

    result = service.get_product_article(_FakeConnection(db), pid)

    article = result["article"]
    assert article["action_shot_image_url"] is None
    assert article["product_shot_image_url"] is None


# --- get_product_article: ecommerce_url + related_reviews (Al: "add
# cross linking at the bottom to 'related' ball reviews. would it be
# possible to link to the ecommerce product page for some balls inline
# too") -- see get_product_article's own docstring for why both reuse
# existing 014/016 price-tracking data and 022 article-approval data
# rather than new migrations.

def test_get_product_article_ecommerce_url_present_when_bigcommerce_source_approved():
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid)
    _seed_bigcommerce_price_source(db, pid, "https://bowlerdepot.com/fury-solid")

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["article"]["product"]["ecommerce_url"] == "https://bowlerdepot.com/fury-solid"


def test_get_product_article_ecommerce_url_null_when_no_bigcommerce_source():
    """The normal case for most of the catalog today -- price_checker
    hasn't matched/approved a BowlerDepot source for this product yet, so
    the frontend is expected to fall back to bowlerDepotSearchUrl()."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid)

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["article"]["product"]["ecommerce_url"] is None


def test_get_product_article_ecommerce_url_ignores_pending_or_inactive_sources():
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid)
    _seed_bigcommerce_price_source(db, pid, "https://bowlerdepot.com/pending-candidate", status="pending")
    _seed_bigcommerce_price_source(db, pid, "https://bowlerdepot.com/deactivated", is_active=False)

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["article"]["product"]["ecommerce_url"] is None


def test_get_product_article_ecommerce_url_picks_most_recently_checked_source():
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid)
    _seed_bigcommerce_price_source(db, pid, "https://bowlerdepot.com/older", last_checked_at="2026-08-01")
    _seed_bigcommerce_price_source(db, pid, "https://bowlerdepot.com/newer", last_checked_at="2026-09-01")

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["article"]["product"]["ecommerce_url"] == "https://bowlerdepot.com/newer"


def test_get_product_article_comparison_table_includes_ecommerce_url():
    db = _fresh_db()
    sibling = _seed_published_current_product(db, pid="sib-1", name="Equinox Hybrid")
    _seed_bigcommerce_price_source(db, sibling, "https://bowlerdepot.com/equinox-hybrid")
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid, sibling_product_ids=[sibling])

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["article"]["comparison_table"][0]["ecommerce_url"] == "https://bowlerdepot.com/equinox-hybrid"


def test_get_product_article_comparison_table_includes_pricing_when_checked():
    """Al, on the Similar Balls list specifically: 'include links and
    pricing for it using the bowlerdepot.com pricing data' -- comparison_
    table rows now carry the same real Offer fields `product` already
    does, sourced from the SAME chosen price source as that row's own
    ecommerce_url (see this function's own docstring)."""
    db = _fresh_db()
    sibling = _seed_published_current_product(db, pid="sib-1", name="Equinox Hybrid")
    _seed_bigcommerce_price_source(
        db, sibling, "https://bowlerdepot.com/equinox-hybrid",
        price=169.99, currency="USD", in_stock=True,
    )
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid, sibling_product_ids=[sibling])

    result = service.get_product_article(_FakeConnection(db), pid)

    row = result["article"]["comparison_table"][0]
    assert row["ecommerce_price"] == 169.99
    assert row["ecommerce_price_currency"] == "USD"
    assert row["ecommerce_in_stock"] is True


def test_get_product_article_comparison_table_pricing_null_when_never_checked():
    """A real approved+active BigCommerce source existing doesn't imply
    price_checker has ever successfully priced it -- same all-null-
    together posture `product`'s own offer fields already have."""
    db = _fresh_db()
    sibling = _seed_published_current_product(db, pid="sib-1", name="Equinox Hybrid")
    _seed_bigcommerce_price_source(db, sibling, "https://bowlerdepot.com/equinox-hybrid")
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid, sibling_product_ids=[sibling])

    result = service.get_product_article(_FakeConnection(db), pid)

    row = result["article"]["comparison_table"][0]
    assert row["ecommerce_price"] is None
    assert row["ecommerce_price_currency"] is None
    assert row["ecommerce_in_stock"] is None


def test_get_product_article_comparison_table_pricing_null_when_no_bigcommerce_source():
    """The normal case for most siblings today -- no price source at all,
    not just an unchecked one."""
    db = _fresh_db()
    sibling = _seed_published_current_product(db, pid="sib-1", name="Equinox Hybrid")
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid, sibling_product_ids=[sibling])

    result = service.get_product_article(_FakeConnection(db), pid)

    row = result["article"]["comparison_table"][0]
    assert row["ecommerce_url"] is None
    assert row["ecommerce_price"] is None
    assert row["ecommerce_price_currency"] is None
    assert row["ecommerce_in_stock"] is None


def test_get_product_article_related_reviews_only_includes_siblings_with_approved_article():
    """Unlike comparison_table (every published sibling), related_reviews
    is narrowed to siblings that have their OWN approved article -- a
    sibling with no article at all, or only a pending one, has nothing to
    link to and must silently drop out."""
    db = _fresh_db()
    reviewed_sibling = _seed_published_current_product(db, pid="sib-reviewed", name="Equinox Hybrid")
    _seed_approved_article(db, reviewed_sibling, **{"id": "art-sib", "title": "Equinox Hybrid Review", "hook": "..."})
    pending_sibling = _seed_published_current_product(db, pid="sib-pending", name="Equinox Pearl")
    _seed_approved_article(db, pending_sibling, status="pending")
    no_article_sibling = _seed_published_current_product(db, pid="sib-none", name="Equinox Solid")
    pid = _seed_published_current_product(db, pid="prod-1", name="Fury")
    _seed_approved_article(
        db, pid, sibling_product_ids=[reviewed_sibling, pending_sibling, no_article_sibling],
    )

    result = service.get_product_article(_FakeConnection(db), pid)

    related_ids = [r["product_id"] for r in result["article"]["related_reviews"]]
    assert related_ids == ["sib-reviewed"]
    assert result["article"]["related_reviews"][0]["title"] == "Equinox Hybrid Review"


def test_get_product_article_related_reviews_drops_unpublished_siblings():
    db = _fresh_db()
    unpublished = _seed_published_current_product(db, pid="sib-1", name="Equinox Pearl", published=False)
    _seed_approved_article(db, unpublished, **{"id": "art-sib"})
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid, sibling_product_ids=[unpublished])

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["article"]["related_reviews"] == []


def test_get_product_article_related_reviews_orders_newest_reviewed_first():
    db = _fresh_db()
    older = _seed_published_current_product(db, pid="sib-older", name="B Ball")
    _seed_approved_article(db, older, **{"id": "art-older", "reviewed_at": "2026-07-01"})
    newer = _seed_published_current_product(db, pid="sib-newer", name="A Ball")
    _seed_approved_article(db, newer, **{"id": "art-newer", "reviewed_at": "2026-09-01"})
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid, sibling_product_ids=[older, newer])

    result = service.get_product_article(_FakeConnection(db), pid)

    related_ids = [r["product_id"] for r in result["article"]["related_reviews"]]
    assert related_ids == ["sib-newer", "sib-older"]


def test_get_product_article_related_reviews_empty_when_no_siblings():
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid)  # sibling_product_ids defaults to []

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["article"]["related_reviews"] == []


# --- get_product_article: brand_lineup carousel -- Al: "add a other
# balls from the same manufacture carousel to the bottom of each article
# and include current balls sorted by price high to low." Unlike
# comparison_table/related_reviews above, this is a fresh brand_id query,
# not sibling_product_ids-based -- see service.get_product_article's own
# comment on that block for why.

def test_get_product_article_brand_lineup_sorted_price_high_to_low():
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1", brand_id="brand-1", name="Fury")
    cheap = _seed_published_current_product(db, pid="sib-cheap", brand_id="brand-1", name="Cheap Ball")
    _seed_bigcommerce_price_source(db, cheap, "https://bowlerdepot.com/cheap", price=99.99)
    pricey = _seed_published_current_product(db, pid="sib-pricey", brand_id="brand-1", name="Pricey Ball")
    _seed_bigcommerce_price_source(db, pricey, "https://bowlerdepot.com/pricey", price=249.99)
    mid = _seed_published_current_product(db, pid="sib-mid", brand_id="brand-1", name="Mid Ball")
    _seed_bigcommerce_price_source(db, mid, "https://bowlerdepot.com/mid", price=179.99)
    _seed_approved_article(db, pid)

    result = service.get_product_article(_FakeConnection(db), pid)

    lineup = result["article"]["brand_lineup"]
    assert [row["id"] for row in lineup] == [pricey, mid, cheap]
    assert [row["ecommerce_price"] for row in lineup] == [249.99, 179.99, 99.99]


def test_get_product_article_brand_lineup_unpriced_balls_sort_last():
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1", brand_id="brand-1")
    unpriced = _seed_published_current_product(db, pid="sib-unpriced", brand_id="brand-1", name="No Price Ball")
    priced = _seed_published_current_product(db, pid="sib-priced", brand_id="brand-1", name="Priced Ball")
    _seed_bigcommerce_price_source(db, priced, "https://bowlerdepot.com/priced", price=139.99)
    _seed_approved_article(db, pid)

    result = service.get_product_article(_FakeConnection(db), pid)

    lineup = result["article"]["brand_lineup"]
    assert [row["id"] for row in lineup] == [priced, unpriced]
    assert lineup[1]["ecommerce_price"] is None


def test_get_product_article_brand_lineup_excludes_own_product():
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1", brand_id="brand-1")
    _seed_approved_article(db, pid)

    result = service.get_product_article(_FakeConnection(db), pid)

    assert pid not in [row["id"] for row in result["article"]["brand_lineup"]]


def test_get_product_article_brand_lineup_excludes_other_brands():
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1", brand_id="brand-1")
    other_brand = _seed_published_current_product(db, pid="sib-1", brand_id="brand-2", name="Other Brand Ball")
    _seed_approved_article(db, pid)

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["article"]["brand_lineup"] == []


def test_get_product_article_brand_lineup_excludes_retired_and_unpublished():
    """Al's ask was specifically 'current balls' -- a retired sibling
    isn't for sale (same reasoning as the shop-this-ball CTA's own
    product.status gate), and an unpublished one shouldn't be visible to
    a public visitor at all."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1", brand_id="brand-1")
    _seed_published_current_product(db, pid="sib-retired", brand_id="brand-1", status="retired")
    _seed_published_current_product(db, pid="sib-unpublished", brand_id="brand-1", published=False)
    _seed_approved_article(db, pid)

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["article"]["brand_lineup"] == []


def test_get_product_article_brand_lineup_empty_when_no_other_current_siblings():
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid)

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["article"]["brand_lineup"] == []


# --- get_product_article: reviewed_at, brand_name, and real Offer data
# (ecommerce_price/ecommerce_price_currency/ecommerce_in_stock) -- Al:
# "can we add all the proper google structured data to the markup" for
# the Learn article pages. See get_product_article's own docstring for
# why reviewed_at (not generated_at) backs Article's datePublished, and
# why the Offer fields are read from the SAME chosen price_source row
# ecommerce_url already resolves, never fabricated.

def test_get_product_article_returns_reviewed_at():
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid, reviewed_at="2026-08-15")

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["article"]["reviewed_at"] == "2026-08-15"


# --- get_product_article: first_published_at (033_product_articles_
# first_published_at.sql) -- Al's later, more literal follow-up: "can we
# add published dates and last updated dates to the articles." reviewed_
# at alone (above) turned out to be the wrong source for a "published"
# date specifically, since approve_article re-stamps it on EVERY
# approval -- see that migration's own header comment for the full
# incident writeup this fixes.

def test_get_product_article_returns_first_published_at():
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid, first_published_at="2026-01-15", reviewed_at="2026-08-15")

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["article"]["first_published_at"] == "2026-01-15"
    # And distinct from reviewed_at -- the whole point of this column.
    assert result["article"]["reviewed_at"] == "2026-08-15"


def test_get_product_article_first_published_at_null_for_pre_migration_article():
    """Every already-approved article gets backfilled by the migration
    itself, but the API contract shouldn't assume that always holds --
    a null here should behave the same "omit, don't error" way every
    other optional field in this payload does."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid, first_published_at=None)

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["article"]["first_published_at"] is None


def test_get_product_article_reviewed_at_null_when_never_reviewed():
    """Shouldn't happen in practice (status='approved' implies an admin
    reviewed it -- see admin_api's approve workflow), but this function's
    own always-shaped-response contract means a null reviewed_at must
    still come back as None, not a missing key or an error."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid, reviewed_at=None)

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["article"]["reviewed_at"] is None


def test_get_product_article_product_includes_brand_name():
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1", brand_id="brand-1")
    db["brands"]["brand-1"]["name"] = "Storm"
    _seed_approved_article(db, pid)

    result = service.get_product_article(_FakeConnection(db), pid)

    assert result["article"]["product"]["brand_name"] == "Storm"


def test_get_product_article_offer_fields_present_when_price_checked():
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid)
    _seed_bigcommerce_price_source(
        db, pid, "https://bowlerdepot.com/fury-solid",
        price=189.99, currency="USD", in_stock=True,
    )

    result = service.get_product_article(_FakeConnection(db), pid)

    product = result["article"]["product"]
    assert product["ecommerce_price"] == 189.99
    assert product["ecommerce_price_currency"] == "USD"
    assert product["ecommerce_in_stock"] is True


def test_get_product_article_offer_fields_null_when_source_never_checked():
    """A real, approved+active price source can exist before price_checker
    has ever successfully checked it (fresh admin approval, next daily run
    hasn't happened yet) -- Offer data must be null, not zero/False, so a
    frontend/JSON-LD generator can tell 'unknown' apart from 'checked and
    it's actually free' or 'checked and it's actually out of stock'."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid)
    _seed_bigcommerce_price_source(db, pid, "https://bowlerdepot.com/fury-solid")

    result = service.get_product_article(_FakeConnection(db), pid)

    product = result["article"]["product"]
    assert product["ecommerce_price"] is None
    assert product["ecommerce_price_currency"] is None
    assert product["ecommerce_in_stock"] is None


def test_get_product_article_offer_fields_null_when_no_bigcommerce_source():
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid)

    result = service.get_product_article(_FakeConnection(db), pid)

    product = result["article"]["product"]
    assert product["ecommerce_price"] is None
    assert product["ecommerce_price_currency"] is None
    assert product["ecommerce_in_stock"] is None


def test_get_product_article_offer_fields_match_most_recently_checked_source():
    """Same 'most recently checked source wins' rule ecommerce_url already
    follows (test_..._picks_most_recently_checked_source) -- price/
    currency/in_stock must come from that SAME row, not any other
    approved+active bigcommerce source's data."""
    db = _fresh_db()
    pid = _seed_published_current_product(db, pid="prod-1")
    _seed_approved_article(db, pid)
    _seed_bigcommerce_price_source(
        db, pid, "https://bowlerdepot.com/older",
        last_checked_at="2026-08-01", price=199.99, currency="USD", in_stock=False,
    )
    _seed_bigcommerce_price_source(
        db, pid, "https://bowlerdepot.com/newer",
        last_checked_at="2026-09-01", price=179.99, currency="USD", in_stock=True,
    )

    result = service.get_product_article(_FakeConnection(db), pid)

    product = result["article"]["product"]
    assert product["ecommerce_url"] == "https://bowlerdepot.com/newer"
    assert product["ecommerce_price"] == 179.99
    assert product["ecommerce_in_stock"] is True


if __name__ == "__main__":
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for name, t in tests:
        t()
        print(f"PASS: {name}")
        passed += 1
    print(f"\n{passed}/{len(tests)} tests passed")
