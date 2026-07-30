"""
Tests for the orchestration additions to src/woocommerce_product_scraper/
app.py (upsert_product's pending_image_jobs, build_image_process_messages,
publish_messages, _extract_jobs, _process_one, handler's SQS-batch
support) -- added when WooCommerceProductScraperFunction was wired into
its own SQS chain (WooCommerceProductScrapeQueue -> this function ->
shared ImageProcessQueue). Same manual-runner pattern as
test_product_scraper_orchestration.py (that file's header comment explains
why: fake DB/SQS objects, run standalone via
`python3 tests/test_woocommerce_product_scraper_orchestration.py` rather
than needing pytest).

Uses tests/fixtures/swag_fusion.html as the source page (same fixture
test_woocommerce_product_scraper.py uses) via a monkeypatched fetch_page,
so _process_one runs the real parse_product_page against real field
values, not a stub.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "woocommerce_product_scraper"))

import app  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
FUSION_URL = "https://www.swagbowling.com/product/swag-fusion-bowling-ball/"


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
    event = {"url": FUSION_URL, "brand_id": "brand-1"}
    jobs = app._extract_jobs(event)
    assert jobs == [({"url": FUSION_URL, "brand_id": "brand-1"}, None)]


def test_extract_jobs_sqs_batch():
    event = {
        "Records": [
            {"messageId": "m-1", "body": json.dumps({"url": FUSION_URL, "brand_id": "b1"})},
        ]
    }
    jobs = app._extract_jobs(event)
    assert jobs == [({"url": FUSION_URL, "brand_id": "b1"}, "m-1")]


# --- Fake DB matching upsert_product's actual queries ---

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

        if q.startswith("insert into products"):
            url = params[2]
            existing_id = self.db["_products_by_url"].get(url)
            product_id = existing_id or self.db["_next_product_id"]
            if existing_id is None:
                self.db["_next_product_id"] += 1
                self.db["_products_by_url"][url] = product_id
            self.db["products"][product_id] = {"url": url}
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


def _fresh_db():
    return {
        "_next_product_id": 1, "_next_image_id": 1, "_products_by_url": {},
        "products": {}, "product_skus": {}, "product_images": {},
    }


class FakeSqs:
    def __init__(self):
        self.sent = []  # list of (queue_url, [message_bodies])

    def send_message_batch(self, QueueUrl, Entries):
        self.sent.append((QueueUrl, [e["MessageBody"] for e in Entries]))


def _fusion_html():
    return (FIXTURES / "swag_fusion.html").read_text()


def test_process_one_publishes_image_jobs_for_all_new_images(monkeypatch):
    db = _fresh_db()
    monkeypatch.setattr(app, "fetch_page", lambda url: _fusion_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.setenv("IMAGE_PROCESS_QUEUE_URL", "https://sqs.example/image-queue")

    sqs = FakeSqs()
    job = {"url": FUSION_URL, "brand_id": "brand-1"}
    result = app._process_one(job, sqs)

    # swag_fusion.html has 2 images (main + core_callout), per
    # test_woocommerce_product_scraper.py's test_images_main_and_core.
    assert result["image_jobs_published"] == 2
    image_calls = [s for s in sqs.sent if s[0] == "https://sqs.example/image-queue"]
    assert len(image_calls) == 1
    assert len(image_calls[0][1]) == 2


def test_process_one_does_not_republish_image_job_for_already_processed_image(monkeypatch):
    db = _fresh_db()
    monkeypatch.setattr(app, "fetch_page", lambda url: _fusion_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.setenv("IMAGE_PROCESS_QUEUE_URL", "https://sqs.example/image-queue")

    sqs = FakeSqs()
    job = {"url": FUSION_URL, "brand_id": "brand-1"}

    first = app._process_one(job, sqs)
    assert first["image_jobs_published"] == 2

    for row in db["product_images"].values():
        row["stored_url"] = "https://images.example/normalized.png"

    sqs2 = FakeSqs()
    second = app._process_one(job, sqs2)
    assert second["image_jobs_published"] == 0


def test_process_one_skips_image_publish_when_queue_url_not_set(monkeypatch):
    """No IMAGE_PROCESS_QUEUE_URL set (e.g. this function deployed without
    the orchestration wiring) -- should still upsert the product, just not
    publish anything."""
    db = _fresh_db()
    monkeypatch.setattr(app, "fetch_page", lambda url: _fusion_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.delenv("IMAGE_PROCESS_QUEUE_URL", raising=False)

    job = {"url": FUSION_URL, "brand_id": "brand-1"}
    result = app._process_one(job, None)
    assert result["image_jobs_published"] == 0
    assert result["sku_count"] == 1


def test_handler_sqs_batch_reports_only_failed_message(monkeypatch):
    db = _fresh_db()

    def fake_fetch_page(url):
        if "boom" in url:
            raise RuntimeError("simulated fetch failure")
        return _fusion_html()

    monkeypatch.setattr(app, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))

    event = {
        "Records": [
            {"messageId": "ok-1", "body": json.dumps({"url": FUSION_URL, "brand_id": "b1"})},
            {"messageId": "fail-1", "body": json.dumps({"url": "https://www.swagbowling.com/product/boom/", "brand_id": "b1"})},
        ]
    }

    response = app.handler(event, None)
    assert response["batchItemFailures"] == [{"itemIdentifier": "fail-1"}]
    body = json.loads(response["body"])
    assert len(body["results"]) == 1


def test_handler_direct_invocation_raises_on_failure(monkeypatch):
    monkeypatch.setattr(app, "fetch_page", lambda url: (_ for _ in ()).throw(RuntimeError("boom")))

    try:
        app.handler({"url": FUSION_URL, "brand_id": "b1"}, None)
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
