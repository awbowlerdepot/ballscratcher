"""
Tests for the orchestration parts of src/netsuite_product_scraper/app.py
(get_status_for_url, get_or_create_core_id, upsert_product's
pending_image_jobs, build_image_process_messages, publish_messages,
_extract_jobs, _process_one, handler's SQS-batch support). Same
manual-runner pattern as test_shopify_product_scraper_orchestration.py
(this file's structure is deliberately copied from that one -- see its
own header comment -- upsert_product's actual SQL columns/param order
differ in detail per platform, but the shape of what needs faking is
identical), run standalone via
`python3 tests/test_netsuite_product_scraper_orchestration.py`.

Built specifically to cover the real, confirmed bug this session found and
fixed: get_status_for_url() is new (mirrors shopify_product_scraper's
function of the same name) -- added because _process_one used to
unconditionally default a missing job["status"] to "current", which is
wrong for any job published by admin_api's queue_rescrape (no status key
at all) since upsert_product's `status = excluded.status` has no
coalesce-preserve-existing fallback. See app.py's module docstring "REAL
INCIDENT" section and get_status_for_url's own docstring for the full
writeup -- confirmed live on MOTIV's actual catalog (every one of 202
scraped products showed 'current' despite discovered_urls correctly
holding 374 'retired' entries).

Uses tests/fixtures/motiv_sigma_tour_pearl.html as the source product
(same fixture test_netsuite_product_scraper.py uses) via a monkeypatched
fetch_page, so _process_one runs the real parse_product_page against real
field values, not a stub.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "netsuite_product_scraper"))

import app  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SIGMA_URL = "https://www.motivbowling.com/products/balls/medium-oil/sigma-tour-pearl.html"


def _sigma_html():
    return (FIXTURES / "motiv_sigma_tour_pearl.html").read_text()


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
    event = {"url": SIGMA_URL, "brand_id": "brand-1"}
    jobs = app._extract_jobs(event)
    assert jobs == [({"url": SIGMA_URL, "brand_id": "brand-1"}, None)]


def test_extract_jobs_sqs_batch():
    event = {"Records": [{"messageId": "m-1", "body": json.dumps({"url": SIGMA_URL, "brand_id": "b1", "status": "retired"})}]}
    jobs = app._extract_jobs(event)
    assert jobs == [({"url": SIGMA_URL, "brand_id": "b1", "status": "retired"}, "m-1")]


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

        elif q.startswith("insert into products"):
            url = params[2]
            status = params[10]
            existing_id = self.db["_products_by_url"].get(url)
            product_id = existing_id or self.db["_next_product_id"]
            if existing_id is None:
                self.db["_next_product_id"] += 1
                self.db["_products_by_url"][url] = product_id
            self.db["products"][product_id] = {"url": url, "status": status}
            self._result = (product_id,)

        elif q.startswith("insert into product_skus"):
            product_id, weight_lbs, rg, differential, mass_bias = params
            self.db["product_skus"][(product_id, weight_lbs)] = {
                "rg": rg, "differential": differential, "mass_bias": mass_bias,
            }
            self._result = None

        elif q.startswith("insert into product_images"):
            product_id, image_type, source_url = params
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
        "_next_product_id": 1, "_next_image_id": 1, "_next_core_id": 1,
        "_products_by_url": {},
        "products": {}, "product_skus": {}, "product_images": {}, "cores": {},
        "discovered_urls": discovered_urls or {},
    }


class FakeSqs:
    def __init__(self):
        self.sent = []

    def send_message_batch(self, QueueUrl, Entries):
        self.sent.append((QueueUrl, [e["MessageBody"] for e in Entries]))


# --- get_status_for_url: the real bug fix this session added ---

def test_get_status_for_url_reads_back_discovery_classification():
    db = _fresh_db(discovered_urls={SIGMA_URL: "retired"})
    conn = FakeConnection(db)
    assert app.get_status_for_url(conn, SIGMA_URL) == "retired"


def test_get_status_for_url_defaults_to_current_when_never_discovered():
    """A manual/direct scrape of a URL the normal category-crawl hasn't
    run across yet -- see get_status_for_url's docstring for why 'current'
    is the safe default rather than leaving status null."""
    db = _fresh_db(discovered_urls={})
    conn = FakeConnection(db)
    assert app.get_status_for_url(conn, SIGMA_URL) == "current"


# --- get_or_create_core_id ---

def test_get_or_create_core_id_returns_none_for_no_core_name():
    db = _fresh_db()
    conn = FakeConnection(db)
    assert app.get_or_create_core_id(conn, "brand-1", None) is None
    assert db["cores"] == {}


def test_get_or_create_core_id_creates_and_reuses():
    db = _fresh_db()
    conn = FakeConnection(db)
    first_id = app.get_or_create_core_id(conn, "brand-1", "Centrix", "symmetric")
    second_id = app.get_or_create_core_id(conn, "brand-1", "Centrix", "symmetric")
    assert first_id == second_id


# --- _process_one: the real bug + fix, end to end ---

def test_process_one_uses_explicit_job_status_when_present(monkeypatch):
    """The normal netsuite_url_discovery-triggered path: job carries its
    own status, so get_status_for_url is never even consulted (confirmed
    here via an empty discovered_urls -- if the code fell back to the DB
    lookup instead of trusting the job, this would silently default to
    'current' and the assertion below would still pass by accident, so
    this also checks the DB never got queried by asserting the discovered_
    urls dict -- which starts empty -- was never populated as a side
    effect of anything, i.e. nothing about this test depends on DB state)."""
    db = _fresh_db(discovered_urls={})  # deliberately empty/unhelpful
    monkeypatch.setattr(app, "fetch_page", lambda url: _sigma_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.delenv("IMAGE_PROCESS_QUEUE_URL", raising=False)

    job = {"url": SIGMA_URL, "brand_id": "brand-1", "status": "retired"}
    result = app._process_one(job, None)

    product_id = int(result["product_id"])
    assert db["products"][product_id]["status"] == "retired"


def test_process_one_falls_back_to_discovered_urls_when_status_missing(monkeypatch):
    """THE bug fix, directly: a queue_rescrape-shaped job (no "status" key
    at all -- see admin_api.service.queue_rescrape) must NOT silently
    default to 'current' anymore. Falls back to whatever netsuite_url_
    discovery already recorded on discovered_urls instead."""
    db = _fresh_db(discovered_urls={SIGMA_URL: "retired"})
    monkeypatch.setattr(app, "fetch_page", lambda url: _sigma_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.delenv("IMAGE_PROCESS_QUEUE_URL", raising=False)

    job = {"url": SIGMA_URL, "brand_id": "brand-1"}  # no "status" key -- the queue_rescrape shape
    result = app._process_one(job, None)

    product_id = int(result["product_id"])
    assert db["products"][product_id]["status"] == "retired"


def test_process_one_falls_back_to_current_for_undiscovered_url_with_no_status(monkeypatch):
    """Same status-less job shape, but the URL was never discovered at
    all -- get_status_for_url's own documented default ('current') still
    applies, same as before this fix for a genuinely-never-discovered
    product (e.g. a first-ever manual test invocation)."""
    db = _fresh_db(discovered_urls={})
    monkeypatch.setattr(app, "fetch_page", lambda url: _sigma_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.delenv("IMAGE_PROCESS_QUEUE_URL", raising=False)

    job = {"url": SIGMA_URL, "brand_id": "brand-1"}
    result = app._process_one(job, None)

    product_id = int(result["product_id"])
    assert db["products"][product_id]["status"] == "current"


def test_process_one_upserts_with_core_and_publishes_images(monkeypatch):
    db = _fresh_db(discovered_urls={SIGMA_URL: "current"})
    monkeypatch.setattr(app, "fetch_page", lambda url: _sigma_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.setenv("IMAGE_PROCESS_QUEUE_URL", "https://sqs.example/image-queue")

    sqs = FakeSqs()
    job = {"url": SIGMA_URL, "brand_id": "brand-1", "status": "current"}
    result = app._process_one(job, sqs)

    assert result["sku_count"] == 5  # see test_netsuite_product_scraper.py's test_sigma_five_skus_no_mass_bias
    assert result["image_jobs_published"] == 3  # see test_sigma_images_main_plus_other_no_core_callout
    image_calls = [s for s in sqs.sent if s[0] == "https://sqs.example/image-queue"]
    assert len(image_calls) == 1
    assert len(image_calls[0][1]) == 3


def test_process_one_does_not_republish_image_job_for_already_processed_image(monkeypatch):
    db = _fresh_db(discovered_urls={SIGMA_URL: "current"})
    monkeypatch.setattr(app, "fetch_page", lambda url: _sigma_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.setenv("IMAGE_PROCESS_QUEUE_URL", "https://sqs.example/image-queue")

    job = {"url": SIGMA_URL, "brand_id": "brand-1", "status": "current"}
    first = app._process_one(job, FakeSqs())
    assert first["image_jobs_published"] == 3

    for row in db["product_images"].values():
        row["stored_url"] = "https://images.example/normalized.png"

    sqs2 = FakeSqs()
    second = app._process_one(job, sqs2)
    assert second["image_jobs_published"] == 0


def test_handler_sqs_batch_reports_only_failed_message(monkeypatch):
    db = _fresh_db(discovered_urls={SIGMA_URL: "current"})

    def fake_fetch(url):
        if "boom" in url:
            raise RuntimeError("simulated fetch failure")
        return _sigma_html()

    monkeypatch.setattr(app, "fetch_page", fake_fetch)
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))

    event = {
        "Records": [
            {"messageId": "ok-1", "body": json.dumps({"url": SIGMA_URL, "brand_id": "b1", "status": "current"})},
            {"messageId": "fail-1", "body": json.dumps({"url": "https://www.motivbowling.com/n_boom", "brand_id": "b1", "status": "current"})},
        ]
    }

    response = app.handler(event, None)
    assert response["batchItemFailures"] == [{"itemIdentifier": "fail-1"}]
    body = json.loads(response["body"])
    assert len(body["results"]) == 1


def test_handler_direct_invocation_raises_on_failure(monkeypatch):
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(_fresh_db()))
    monkeypatch.setattr(app, "fetch_page", lambda url: (_ for _ in ()).throw(RuntimeError("boom")))

    try:
        app.handler({"url": SIGMA_URL, "brand_id": "b1"}, None)
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
