"""
Tests for the orchestration parts of src/shopify_product_scraper/app.py
(get_status_for_url, get_or_create_core_id, upsert_product's
pending_image_jobs, build_image_process_messages, publish_messages,
_extract_jobs, _process_one, handler's SQS-batch support). Same
manual-runner pattern as test_product_scraper_orchestration.py /
test_woocommerce_product_scraper_orchestration.py -- fake DB/SQS objects,
run standalone via
`python3 tests/test_shopify_product_scraper_orchestration.py`.

Uses tests/fixtures/hammer_black_widow_3_0_dynasty.json as the source
product (same fixture test_shopify_product_scraper.py uses) via a
monkeypatched fetch_product_json, so _process_one runs the real
parse_product_page against real field values, not a stub.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "shopify_product_scraper"))

import app  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
BWD_URL = "https://hammerbowling.com/products/black-widow-3-0-dynasty"


# --- Pure message builders ---

def test_build_image_process_messages():
    jobs = [{"product_image_id": "img-1", "source_url": "https://cdn.example/a.png"}]
    messages = app.build_image_process_messages(jobs)
    assert len(messages) == 1
    assert json.loads(messages[0]) == jobs[0]


def test_publish_messages_batches_over_10():
    class FakeSqs:
        def __init__(self):
            self.batches = []

        def send_message_batch(self, QueueUrl, Entries):
            self.batches.append(Entries)

    sqs = FakeSqs()
    sent = app.publish_messages(sqs, "q-url", [f"m{i}" for i in range(12)])
    assert sent == 12
    assert [len(b) for b in sqs.batches] == [10, 2]


# --- _extract_jobs: SQS batch vs. direct invocation ---

def test_extract_jobs_direct_invocation():
    event = {"url": BWD_URL, "brand_id": "brand-1"}
    jobs = app._extract_jobs(event)
    assert jobs == [({"url": BWD_URL, "brand_id": "brand-1"}, None)]


def test_extract_jobs_sqs_batch():
    event = {"Records": [{"messageId": "m-1", "body": json.dumps({"url": BWD_URL, "brand_id": "b1"})}]}
    jobs = app._extract_jobs(event)
    assert jobs == [({"url": BWD_URL, "brand_id": "b1"}, "m-1")]


# --- Fake DB matching upsert_product's + get_status_for_url's actual queries ---

class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        q = " ".join(query.split())
        params = params or []

        if q.startswith("select status_path from discovered_urls"):
            (url,) = params
            status = self.db["discovered_urls"].get(url)
            self._result = (status,) if status is not None else None

        elif q.startswith("insert into cores"):
            brand_id, name, core_type = params
            key = (brand_id, name)
            existing = self.db["cores"].get(key)
            if existing is None:
                core_id = self.db["_next_core_id"]
                self.db["_next_core_id"] += 1
                self.db["cores"][key] = {"id": core_id, "core_type": core_type}
            elif self.db["cores"][key]["core_type"] is None:
                self.db["cores"][key]["core_type"] = core_type
            self._result = (self.db["cores"][key]["id"],)

        elif q.startswith("insert into coverstocks"):
            brand_id, name, material, cs_type = params
            key = (brand_id, name)
            existing = self.db["coverstocks"].get(key)
            if existing is None:
                coverstock_id = self.db["_next_coverstock_id"]
                self.db["_next_coverstock_id"] += 1
                self.db["coverstocks"][key] = {"id": coverstock_id, "material": material, "type": cs_type}
            else:
                if self.db["coverstocks"][key]["material"] is None:
                    self.db["coverstocks"][key]["material"] = material
                if self.db["coverstocks"][key]["type"] is None:
                    self.db["coverstocks"][key]["type"] = cs_type
            self._result = (self.db["coverstocks"][key]["id"],)

        elif q.startswith("insert into products"):
            url = params[2]
            existing_id = self.db["_products_by_url"].get(url)
            product_id = existing_id or self.db["_next_product_id"]
            if existing_id is None:
                self.db["_next_product_id"] += 1
                self.db["_products_by_url"][url] = product_id
            # Real ON CONFLICT DO UPDATE never lists oil_rating/motion_
            # rating/oil_motion_source (migrations 011/012) among its SET
            # clauses -- preserve them across a rescrape's reset here too.
            preserved = {
                k: self.db["products"][product_id][k]
                for k in ("oil_rating", "motion_rating", "oil_motion_source")
                if product_id in self.db["products"] and k in self.db["products"][product_id]
            }
            self.db["products"][product_id] = {"url": url, **preserved}
            self._result = (product_id,)

        elif q.startswith("insert into product_skus"):
            product_id, weight_lbs, rg, differential, mass_bias = params
            self.db["product_skus"][(product_id, weight_lbs)] = {
                "rg": rg, "differential": differential, "mass_bias": mass_bias,
            }
            self._result = None

        elif q.startswith("insert into product_images"):
            # 4th param is product_id again, for the correlated
            # display_order subquery -- see product_scraper/app.py's
            # upsert_product docstring for the NotNullViolation incident
            # this fixed. Not modeled with real ordering semantics here.
            product_id, image_type, source_url, _product_id_again = params
            key = (product_id, source_url)
            existing = self.db["product_images"].get(key)
            if existing is None:
                image_id = self.db["_next_image_id"]
                self.db["_next_image_id"] += 1
                self.db["product_images"][key] = {"id": image_id, "image_type": image_type, "stored_url": None}
            else:
                self.db["product_images"][key]["image_type"] = image_type
            row = self.db["product_images"][key]
            self._result = (row["id"], row["stored_url"])

        elif q == "select core_type from cores where id = %s":
            (core_id,) = params
            match = next((c for c in self.db["cores"].values() if c["id"] == core_id), None)
            self._result = (match["core_type"],) if match else None

        elif q == "select has_particle from products where id = %s":
            (pid,) = params
            self._result = (self.db["products"][pid].get("has_particle", False),)

        elif q.startswith("update products set oil_rating"):
            oil, motion, pid = params
            row = self.db["products"].get(pid)
            if row is not None and row.get("oil_rating") is None:
                row["oil_rating"] = oil
                row["motion_rating"] = motion
                row["oil_motion_source"] = "estimated"
            self._result = None

        else:
            raise NotImplementedError(f"FakeCursor doesn't support: {q}")

    def fetchone(self):
        return self._result


class FakeConnection:
    def __init__(self, db):
        self.db = db
        self.committed = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def _fresh_db(discovered_urls=None):
    return {
        "_next_product_id": 1, "_next_image_id": 1, "_next_core_id": 1, "_next_coverstock_id": 1,
        "_products_by_url": {},
        "products": {}, "product_skus": {}, "product_images": {}, "cores": {}, "coverstocks": {},
        "discovered_urls": discovered_urls or {},
    }


class FakeSqs:
    def __init__(self):
        self.sent = []

    def send_message_batch(self, QueueUrl, Entries):
        self.sent.append((QueueUrl, [e["MessageBody"] for e in Entries]))


def _bwd_product():
    return json.loads((FIXTURES / "hammer_black_widow_3_0_dynasty.json").read_text())["product"]


# --- get_status_for_url ---

def test_get_status_for_url_reads_back_discovery_classification():
    db = _fresh_db(discovered_urls={BWD_URL: "retired"})
    conn = FakeConnection(db)
    assert app.get_status_for_url(conn, BWD_URL) == "retired"


def test_get_status_for_url_defaults_to_current_when_never_discovered():
    """A manual/direct scrape of a URL the normal collection-crawl hasn't
    run across yet -- see module docstring for why 'current' is the safe
    default rather than leaving status null."""
    db = _fresh_db(discovered_urls={})
    conn = FakeConnection(db)
    assert app.get_status_for_url(conn, BWD_URL) == "current"


# --- get_or_create_core_id ---

def test_get_or_create_core_id_returns_none_for_no_core_name():
    db = _fresh_db()
    conn = FakeConnection(db)
    assert app.get_or_create_core_id(conn, "brand-1", None) is None
    assert db["cores"] == {}  # never touches the DB at all


def test_get_or_create_core_id_creates_and_reuses():
    db = _fresh_db()
    conn = FakeConnection(db)
    first_id = app.get_or_create_core_id(conn, "brand-1", "Gas Mask", "asymmetric")
    second_id = app.get_or_create_core_id(conn, "brand-1", "Gas Mask", "asymmetric")
    assert first_id == second_id


# --- get_or_create_coverstock_id (migration 008 -- same shape as
# get_or_create_core_id above, Al's direct follow-up ask) ---

def test_get_or_create_coverstock_id_returns_none_for_no_coverstock_name():
    db = _fresh_db()
    conn = FakeConnection(db)
    assert app.get_or_create_coverstock_id(conn, "brand-1", None) is None
    assert db["coverstocks"] == {}


def test_get_or_create_coverstock_id_creates_and_reuses():
    db = _fresh_db()
    conn = FakeConnection(db)
    first_id = app.get_or_create_coverstock_id(conn, "brand-1", "R2S Solid Reactive", "reactive_resin", "solid")
    second_id = app.get_or_create_coverstock_id(conn, "brand-1", "R2S Solid Reactive", "reactive_resin", "solid")
    assert first_id == second_id


# --- _process_one: full real-fixture run through parse + upsert + image fan-out ---

def test_process_one_upserts_current_product_with_core_and_publishes_images(monkeypatch):
    db = _fresh_db(discovered_urls={BWD_URL: "current"})
    monkeypatch.setattr(app, "fetch_product_json", lambda url: _bwd_product())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.setenv("IMAGE_PROCESS_QUEUE_URL", "https://sqs.example/image-queue")

    sqs = FakeSqs()
    job = {"url": BWD_URL, "brand_id": "brand-1"}
    result = app._process_one(job, sqs)

    assert result["sku_count"] == 5
    assert result["image_jobs_published"] == 4
    assert db["cores"][("brand-1", "Gas Mask")]["core_type"] == "asymmetric"
    image_calls = [s for s in sqs.sent if s[0] == "https://sqs.example/image-queue"]
    assert len(image_calls) == 1
    assert len(image_calls[0][1]) == 4


def test_process_one_defaults_status_current_for_undiscovered_url(monkeypatch):
    db = _fresh_db(discovered_urls={})  # never discovered
    monkeypatch.setattr(app, "fetch_product_json", lambda url: _bwd_product())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.delenv("IMAGE_PROCESS_QUEUE_URL", raising=False)

    job = {"url": BWD_URL, "brand_id": "brand-1"}
    result = app._process_one(job, None)
    product_id = int(result["product_id"])
    assert db["products"][product_id]["url"] == BWD_URL


def test_process_one_does_not_republish_image_job_for_already_processed_image(monkeypatch):
    db = _fresh_db(discovered_urls={BWD_URL: "current"})
    monkeypatch.setattr(app, "fetch_product_json", lambda url: _bwd_product())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.setenv("IMAGE_PROCESS_QUEUE_URL", "https://sqs.example/image-queue")

    job = {"url": BWD_URL, "brand_id": "brand-1"}
    first = app._process_one(job, FakeSqs())
    assert first["image_jobs_published"] == 4

    for row in db["product_images"].values():
        row["stored_url"] = "https://images.example/normalized.png"

    sqs2 = FakeSqs()
    second = app._process_one(job, sqs2)
    assert second["image_jobs_published"] == 0


# --- Estimate-on-scrape plotter position (migrations 011/012) -- Al's
# ask: "estimate on scrape if not set".

def test_process_one_writes_estimated_plotter_position_when_unset(monkeypatch):
    db = _fresh_db(discovered_urls={BWD_URL: "current"})
    monkeypatch.setattr(app, "fetch_product_json", lambda url: _bwd_product())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.delenv("IMAGE_PROCESS_QUEUE_URL", raising=False)

    job = {"url": BWD_URL, "brand_id": "brand-1"}
    result = app._process_one(job, None)
    product_id = int(result["product_id"])

    assert db["products"][product_id]["oil_rating"] is not None
    assert db["products"][product_id]["motion_rating"] is not None
    assert db["products"][product_id]["oil_motion_source"] == "estimated"


def test_process_one_never_overwrites_existing_plotter_position(monkeypatch):
    db = _fresh_db(discovered_urls={BWD_URL: "current"})
    monkeypatch.setattr(app, "fetch_product_json", lambda url: _bwd_product())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.delenv("IMAGE_PROCESS_QUEUE_URL", raising=False)

    job = {"url": BWD_URL, "brand_id": "brand-1"}
    first = app._process_one(job, None)  # lands an 'estimated' position
    product_id = int(first["product_id"])
    db["products"][product_id]["oil_rating"] = 6
    db["products"][product_id]["motion_rating"] = 18
    db["products"][product_id]["oil_motion_source"] = "chart"  # simulate a chart match landing afterward

    app._process_one(job, None)  # rescrape

    assert db["products"][product_id]["oil_rating"] == 6
    assert db["products"][product_id]["motion_rating"] == 18
    assert db["products"][product_id]["oil_motion_source"] == "chart"


def test_handler_sqs_batch_reports_only_failed_message(monkeypatch):
    db = _fresh_db(discovered_urls={BWD_URL: "current"})

    def fake_fetch(url):
        if "boom" in url:
            raise RuntimeError("simulated fetch failure")
        return _bwd_product()

    monkeypatch.setattr(app, "fetch_product_json", fake_fetch)
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))

    event = {
        "Records": [
            {"messageId": "ok-1", "body": json.dumps({"url": BWD_URL, "brand_id": "b1"})},
            {"messageId": "fail-1", "body": json.dumps({"url": "https://hammerbowling.com/products/boom", "brand_id": "b1"})},
        ]
    }

    response = app.handler(event, None)
    assert response["batchItemFailures"] == [{"itemIdentifier": "fail-1"}]
    body = json.loads(response["body"])
    assert len(body["results"]) == 1


def test_handler_direct_invocation_raises_on_failure(monkeypatch):
    monkeypatch.setattr(app, "fetch_product_json", lambda url: (_ for _ in ()).throw(RuntimeError("boom")))

    try:
        app.handler({"url": BWD_URL, "brand_id": "b1"}, None)
        assert False, "expected RuntimeError to propagate"
    except RuntimeError:
        pass


if __name__ == "__main__":
    class _MonkeyPatch:
        def __init__(self):
            self._sets = []
            self._env = []

        def setattr(self, obj, name, value):
            self._sets.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def setenv(self, name, value):
            self._env.append((name, os.environ.get(name)))
            os.environ[name] = value

        def delenv(self, name, raising=True):
            self._env.append((name, os.environ.get(name)))
            os.environ.pop(name, None)

        def undo(self):
            for obj, name, value in reversed(self._sets):
                setattr(obj, name, value)
            for name, value in reversed(self._env):
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for name, t in tests:
        mp = _MonkeyPatch()
        try:
            if "monkeypatch" in t.__code__.co_varnames[: t.__code__.co_argcount]:
                t(mp)
            else:
                t()
            print(f"PASS: {name}")
            passed += 1
        finally:
            mp.undo()
    print(f"\n{passed}/{len(tests)} tests passed")
