"""
Tests for the orchestration additions to src/product_scraper/app.py
(build_pdf_parse_message, build_image_process_messages, publish_messages,
_extract_jobs, _process_one, handler's SQS-batch support). Kept in a
separate file from test_product_scraper.py deliberately: that file uses
pytest (unrunnable in this sandbox, same as always -- see its own header),
while these tests need fake DB/SQS objects and are written to run standalone
via `python3 tests/test_product_scraper_orchestration.py`, matching the
manual-runner pattern used for pdf_parser/image_processor/admin_api's tests.
This is the one file in this session where that let the new logic actually
be *run*, not just hand-reviewed -- all tests below genuinely execute.

Uses tests/fixtures/crown_78u.html as the source page (same fixture
test_product_scraper.py uses, see its header for what's real vs.
reconstructed about it) via a monkeypatched fetch_page, so _process_one
runs the real parse_product_page against real field values, not a stub.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "product_scraper"))

import app  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


# --- Pure message builders ---

def test_build_pdf_parse_message():
    msg = app.build_pdf_parse_message("prod-1", "https://cdn.example/info.pdf")
    assert json.loads(msg) == {"product_id": "prod-1", "info_sheet_url": "https://cdn.example/info.pdf"}


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
    event = {"url": "https://example.com/ball", "brand_id": "brand-1"}
    jobs = app._extract_jobs(event)
    assert jobs == [({"url": "https://example.com/ball", "brand_id": "brand-1"}, None)]


def test_extract_jobs_sqs_batch():
    event = {
        "Records": [
            {"messageId": "m-1", "body": json.dumps({"url": "https://example.com/a", "brand_id": "b1"})},
            {"messageId": "m-2", "body": json.dumps({"url": "https://example.com/b", "brand_id": "b1"})},
        ]
    }
    jobs = app._extract_jobs(event)
    assert jobs == [
        ({"url": "https://example.com/a", "brand_id": "b1"}, "m-1"),
        ({"url": "https://example.com/b", "brand_id": "b1"}, "m-2"),
    ]


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


def _crown_78u_html():
    return (FIXTURES / "crown_78u.html").read_text()


def test_process_one_publishes_pdf_job_when_info_sheet_present(monkeypatch):
    """Crown 78U's fixture has an info_sheet_url resource -- confirms
    _process_one publishes a PDF-parse job for it when PDF_PARSE_QUEUE_URL
    is configured."""
    db = _fresh_db()
    monkeypatch.setattr(app, "fetch_page", lambda url: _crown_78u_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.setenv("PDF_PARSE_QUEUE_URL", "https://sqs.example/pdf-queue")
    monkeypatch.setenv("IMAGE_PROCESS_QUEUE_URL", "https://sqs.example/image-queue")

    sqs = FakeSqs()
    job = {"url": "https://brunswickbowling.com/products/balls/current/crown-78u", "brand_id": "brand-1"}
    result = app._process_one(job, sqs)

    assert result["pdf_jobs_published"] == 1
    pdf_calls = [s for s in sqs.sent if s[0] == "https://sqs.example/pdf-queue"]
    assert len(pdf_calls) == 1
    published_body = json.loads(pdf_calls[0][1][0])
    assert published_body["info_sheet_url"].endswith("Crown_78U_Info_Sheet_1025-12.pdf")


def test_process_one_publishes_image_jobs_for_all_new_images(monkeypatch):
    db = _fresh_db()
    monkeypatch.setattr(app, "fetch_page", lambda url: _crown_78u_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.setenv("PDF_PARSE_QUEUE_URL", "https://sqs.example/pdf-queue")
    monkeypatch.setenv("IMAGE_PROCESS_QUEUE_URL", "https://sqs.example/image-queue")

    sqs = FakeSqs()
    job = {"url": "https://brunswickbowling.com/products/balls/current/crown-78u", "brand_id": "brand-1"}
    result = app._process_one(job, sqs)

    # crown_78u.html fixture has 3 images (main + 2 core_callout), per
    # test_product_scraper.py's test_crown_78u_images -- all are new,
    # so all 3 should get an image-process job.
    assert result["image_jobs_published"] == 3
    image_calls = [s for s in sqs.sent if s[0] == "https://sqs.example/image-queue"]
    assert len(image_calls) == 1
    assert len(image_calls[0][1]) == 3


def test_process_one_does_not_republish_image_job_for_already_processed_image(monkeypatch):
    """Re-scraping a page whose images already have stored_url set (i.e.
    the image pipeline already ran) shouldn't re-queue them."""
    db = _fresh_db()
    monkeypatch.setattr(app, "fetch_page", lambda url: _crown_78u_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.setenv("IMAGE_PROCESS_QUEUE_URL", "https://sqs.example/image-queue")

    sqs = FakeSqs()
    job = {"url": "https://brunswickbowling.com/products/balls/current/crown-78u", "brand_id": "brand-1"}

    first = app._process_one(job, sqs)
    assert first["image_jobs_published"] == 3

    # Simulate the image pipeline having completed for all 3 images.
    for row in db["product_images"].values():
        row["stored_url"] = "https://images.example/normalized.png"

    sqs2 = FakeSqs()
    second = app._process_one(job, sqs2)
    assert second["image_jobs_published"] == 0


def test_handler_sqs_batch_reports_only_failed_message(monkeypatch):
    """One job succeeds, one raises -- confirms batchItemFailures contains
    only the failed message's id, not the successful one's, so SQS only
    retries the actual failure."""
    db = _fresh_db()

    def fake_fetch_page(url):
        if "boom" in url:
            raise RuntimeError("simulated fetch failure")
        return _crown_78u_html()

    monkeypatch.setattr(app, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))

    event = {
        "Records": [
            {"messageId": "ok-1", "body": json.dumps({"url": "https://brunswickbowling.com/products/balls/current/crown-78u", "brand_id": "b1"})},
            {"messageId": "fail-1", "body": json.dumps({"url": "https://brunswickbowling.com/boom", "brand_id": "b1"})},
        ]
    }

    response = app.handler(event, None)
    assert response["batchItemFailures"] == [{"itemIdentifier": "fail-1"}]
    body = json.loads(response["body"])
    assert len(body["results"]) == 1  # only the successful job produced a result


def test_handler_direct_invocation_raises_on_failure(monkeypatch):
    """Direct (non-SQS) invocation has no batch to report against -- a
    failure should propagate as a real exception, not be silently
    swallowed."""
    monkeypatch.setattr(app, "fetch_page", lambda url: (_ for _ in ()).throw(RuntimeError("boom")))

    try:
        app.handler({"url": "https://example.com/x", "brand_id": "b1"}, None)
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
