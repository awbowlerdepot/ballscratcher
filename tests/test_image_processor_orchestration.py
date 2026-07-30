"""
Tests for the orchestration additions to src/image_processor/app.py
(_extract_jobs, _process_one, handler's SQS-batch support). Same rationale
and pattern as the other *_orchestration.py test files -- separate from
test_image_processor.py (which covers the real image-normalization math
against synthetic fixtures) because this needs fake S3/DB objects and runs
standalone via `python3 tests/test_image_processor_orchestration.py`.
"""
import io
import json
import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "image_processor"))

import app  # noqa: E402


def _synthetic_png_bytes():
    image = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((100, 100, 299, 299), fill=(120, 60, 200, 255))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_extract_jobs_direct_invocation():
    event = {"product_image_id": "img-1", "source_url": "https://cdn.example/a.png"}
    assert app._extract_jobs(event) == [(event, None)]


def test_extract_jobs_sqs_batch():
    event = {
        "Records": [
            {"messageId": "m-1", "body": json.dumps({"product_image_id": "img-1", "source_url": "https://cdn.example/a.png"})},
        ]
    }
    assert app._extract_jobs(event) == [
        ({"product_image_id": "img-1", "source_url": "https://cdn.example/a.png"}, "m-1"),
    ]


class FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, Bucket, Key, Body, ContentType):
        self.puts.append({"Bucket": Bucket, "Key": Key, "size": len(Body), "ContentType": ContentType})


class FakeCursor:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        q = " ".join(query.split())
        if q.startswith("update product_images set stored_url"):
            stored_url, product_image_id = params
            self.db[product_image_id] = stored_url
        else:
            raise NotImplementedError(f"FakeCursor doesn't support: {q}")


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


def test_process_one_uploads_three_variants_and_updates_db(monkeypatch):
    db = {}
    monkeypatch.setattr(app, "fetch_image_bytes", lambda url: _synthetic_png_bytes())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.setenv("IMAGE_BUCKET", "test-bucket")

    s3 = FakeS3()
    job = {"product_image_id": "img-1", "source_url": "https://cdn.example/a.png"}
    result = app._process_one(job, s3)

    assert set(result["urls"].keys()) == {"thumbnail", "catalog", "detail"}
    assert len(s3.puts) == 3
    assert all(p["Bucket"] == "test-bucket" for p in s3.puts)
    assert any("thumbnail.png" in p["Key"] for p in s3.puts)
    assert db["img-1"] == result["urls"]["detail"]


def test_handler_sqs_batch_reports_only_failed_message(monkeypatch):
    db = {}

    def fake_fetch(url):
        if "boom" in url:
            raise RuntimeError("simulated fetch failure")
        return _synthetic_png_bytes()

    monkeypatch.setattr(app, "fetch_image_bytes", fake_fetch)
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.setenv("IMAGE_BUCKET", "test-bucket")

    # boto3 isn't installed in this sandbox (see README), so handler's
    # `import boto3; boto3.client("s3")` line can't run here directly --
    # patch it out at the module level the same way the real handler would
    # get a working client from boto3 in a real Lambda environment.
    import types
    fake_boto3 = types.SimpleNamespace(client=lambda service: FakeS3())
    sys.modules["boto3"] = fake_boto3

    event = {
        "Records": [
            {"messageId": "ok-1", "body": json.dumps({"product_image_id": "img-1", "source_url": "https://cdn.example/ok.png"})},
            {"messageId": "fail-1", "body": json.dumps({"product_image_id": "img-2", "source_url": "https://cdn.example/boom.png"})},
        ]
    }

    response = app.handler(event, None)
    assert response["batchItemFailures"] == [{"itemIdentifier": "fail-1"}]
    body = json.loads(response["body"])
    assert len(body["results"]) == 1


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
