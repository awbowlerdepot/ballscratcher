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
    """Gains cores/coverstocks/stale-image-delete support here (previously
    a known, pre-existing gap -- see this session's DEPLOY_RUNBOOK.md
    history -- that made this whole file's DB-touching tests fail before
    upsert_product ever reached the query they actually meant to exercise).
    Fixed now, mirroring test_netsuite_product_scraper_orchestration.py's
    FakeCursor exactly, so this file can actually run the new stale-image
    DELETE + S3 orphan cleanup end to end rather than just being hand-
    reviewed."""

    def __init__(self, db):
        self.db = db
        self._result = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        q = " ".join(query.split())
        params = params or []

        if q.startswith("insert into cores"):
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
            # clauses -- only the estimate-on-scrape hook and set_plotter_
            # position ever touch those columns. Preserve them across a
            # rescrape's reset rather than wiping them, matching that.
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
            # display_order subquery (coalesce(max(display_order)+1, 0) --
            # see product_scraper/app.py's upsert_product docstring for
            # the NotNullViolation incident this fixed). Not modeled with
            # real ordering semantics here since nothing in this test file
            # asserts on display_order -- just unpacked so the real 4-arg
            # call shape doesn't blow up a positional unpack.
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

        elif q == "delete from product_images where product_id = %s and source_url <> all(%s) returning id, stored_url":
            # upsert_product's stale-image cleanup, ported from
            # netsuite_product_scraper (see that module's docstring):
            # removes any row for this product_id whose source_url isn't
            # in the just-parsed set, so a rescrape genuinely replaces the
            # image list rather than just adding to it. Returns the
            # deleted rows' (id, stored_url) -- needed by _process_one/
            # delete_orphaned_image_objects to know which ones (if any)
            # also need their S3 objects cleaned up.
            del_product_id, keep_urls = params
            keep_urls = set(keep_urls)
            to_delete = [k for k in self.db["product_images"] if k[0] == del_product_id and k[1] not in keep_urls]
            self._rows = [(self.db["product_images"][k]["id"], self.db["product_images"][k]["stored_url"]) for k in to_delete]
            for key in to_delete:
                del self.db["product_images"][key]

        elif q == "delete from product_images where product_id = %s returning id, stored_url":
            (del_product_id,) = params
            to_delete = [k for k in self.db["product_images"] if k[0] == del_product_id]
            self._rows = [(self.db["product_images"][k]["id"], self.db["product_images"][k]["stored_url"]) for k in to_delete]
            for key in to_delete:
                del self.db["product_images"][key]

        elif q == "select core_type from cores where id = %s":
            # Estimate-on-scrape's core_type lookup (migrations 011/012) --
            # reads back the coalesced value from whichever cores row
            # core_id points at, keyed here by id (not the (brand_id,
            # name) tuple insert into cores uses).
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

    def fetchall(self):
        return self._rows


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
        "_next_product_id": 1, "_next_image_id": 1, "_next_core_id": 1, "_next_coverstock_id": 1,
        "_products_by_url": {},
        "products": {}, "product_skus": {}, "product_images": {}, "cores": {}, "coverstocks": {},
    }


class FakeSqs:
    def __init__(self):
        self.sent = []  # list of (queue_url, [message_bodies])

    def send_message_batch(self, QueueUrl, Entries):
        self.sent.append((QueueUrl, [e["MessageBody"] for e in Entries]))


class _FakeS3Paginator:
    def __init__(self, client):
        self.client = client

    def paginate(self, Bucket, Prefix):
        keys = self.client.objects_by_prefix.get(Prefix, [])
        # Mirrors real boto3: a zero-match page omits "Contents" entirely
        # rather than including an empty list -- delete_orphaned_image_
        # objects' `.get("Contents", [])` exists specifically to handle
        # that shape, so this fake reproduces it rather than always
        # including the key.
        yield {"Contents": [{"Key": k} for k in keys]} if keys else {}


class FakeS3Client:
    def __init__(self, objects_by_prefix=None):
        self.objects_by_prefix = objects_by_prefix or {}
        self.delete_calls = []

    def get_paginator(self, operation_name):
        assert operation_name == "list_objects_v2"
        return _FakeS3Paginator(self)

    def delete_objects(self, Bucket, Delete):
        self.delete_calls.append({"Bucket": Bucket, "Keys": [o["Key"] for o in Delete["Objects"]]})


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


# --- stale-image DELETE + S3 orphan cleanup, ported from
# netsuite_product_scraper (MOTIV) -- see upsert_product's docstring for
# the full incident this closes.

def test_process_one_removes_stale_image_rows_no_longer_matched(monkeypatch):
    """Al confirmed Brunswick has the same gap MOTIV had: a plain
    insert-on-conflict-update never deletes a row for a source_url that's
    no longer part of the current parse, so a rescrape would just add the
    correct photos alongside whatever wrong/old one was already sitting
    there. This seeds exactly that scenario -- a stale row for this
    product_id under a url the real crown_78u fixture never contains --
    and confirms _process_one's rescrape clears it."""
    db = _fresh_db()
    crown_url = "https://brunswickbowling.com/products/balls/current/crown-78u"
    db["_products_by_url"][crown_url] = 1
    db["_next_product_id"] = 2
    db["products"][1] = {"url": crown_url}
    stale_key = (1, "https://brunswickbowling.com/userfiles/unrelated-thumb.png")
    db["product_images"][stale_key] = {
        "id": 999, "image_type": "other", "stored_url": "https://s3.example/already-mirrored.png",
    }

    monkeypatch.setattr(app, "fetch_page", lambda url: _crown_78u_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.delenv("IMAGE_PROCESS_QUEUE_URL", raising=False)
    monkeypatch.delenv("PDF_PARSE_QUEUE_URL", raising=False)

    job = {"url": crown_url, "brand_id": "brand-1"}
    app._process_one(job, None)

    remaining = {url for (pid, url) in db["product_images"] if pid == 1}
    assert stale_key[1] not in remaining
    assert len(remaining) == 3  # only the 3 real crown_78u images remain


def test_process_one_empty_image_parse_clears_all_existing_rows(monkeypatch):
    """Edge case for the same cleanup fix: if a rescrape genuinely finds
    zero images this time, every existing row for that product is stale
    by definition and should be cleared too, not left behind forever."""
    db = _fresh_db()
    crown_url = "https://brunswickbowling.com/products/balls/current/crown-78u"
    db["_products_by_url"][crown_url] = 1
    db["_next_product_id"] = 2
    db["products"][1] = {"url": crown_url}
    db["product_images"][(1, "https://brunswickbowling.com/userfiles/stale1.png")] = {
        "id": 1, "image_type": "main", "stored_url": None,
    }

    _real_parse_product_page = app.parse_product_page
    monkeypatch.setattr(app, "fetch_page", lambda url: _crown_78u_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.setattr(app, "parse_product_page", lambda html, url: {
        **_real_parse_product_page(html, url), "images": [],
    })
    monkeypatch.delenv("IMAGE_PROCESS_QUEUE_URL", raising=False)
    monkeypatch.delenv("PDF_PARSE_QUEUE_URL", raising=False)

    job = {"url": crown_url, "brand_id": "brand-1"}
    app._process_one(job, None)

    remaining = {url for (pid, url) in db["product_images"] if pid == 1}
    assert remaining == set()


# --- delete_orphaned_image_objects: the S3 half of the stale-image
# cleanup fix -- see that function's docstring for the full story.

def test_delete_orphaned_image_objects_deletes_every_variant_for_stale_rows():
    s3 = FakeS3Client(objects_by_prefix={
        "product-images/img-1/": [
            "product-images/img-1/thumbnail.png",
            "product-images/img-1/catalog.png",
            "product-images/img-1/detail.png",
        ],
    })
    stale_rows = [{"id": "img-1", "stored_url": "https://bucket.s3.amazonaws.com/product-images/img-1/detail.png"}]

    deleted = app.delete_orphaned_image_objects(s3, "my-bucket", stale_rows)

    assert deleted == 3
    assert len(s3.delete_calls) == 1
    assert s3.delete_calls[0]["Bucket"] == "my-bucket"
    assert set(s3.delete_calls[0]["Keys"]) == {
        "product-images/img-1/thumbnail.png",
        "product-images/img-1/catalog.png",
        "product-images/img-1/detail.png",
    }


def test_delete_orphaned_image_objects_skips_rows_with_no_stored_url():
    s3 = FakeS3Client(objects_by_prefix={"product-images/img-2/": ["product-images/img-2/detail.png"]})
    stale_rows = [{"id": "img-2", "stored_url": None}]

    deleted = app.delete_orphaned_image_objects(s3, "my-bucket", stale_rows)

    assert deleted == 0
    assert s3.delete_calls == []


def test_delete_orphaned_image_objects_handles_multiple_rows_independently():
    s3 = FakeS3Client(objects_by_prefix={
        "product-images/img-1/": ["product-images/img-1/detail.png"],
        "product-images/img-3/": ["product-images/img-3/detail.png", "product-images/img-3/thumbnail.png"],
    })
    stale_rows = [
        {"id": "img-1", "stored_url": "https://bucket.s3.amazonaws.com/product-images/img-1/detail.png"},
        {"id": "img-2", "stored_url": None},  # skipped -- never processed
        {"id": "img-3", "stored_url": "https://bucket.s3.amazonaws.com/product-images/img-3/detail.png"},
    ]

    deleted = app.delete_orphaned_image_objects(s3, "my-bucket", stale_rows)

    assert deleted == 3  # 1 + 2, img-2 contributes nothing
    assert len(s3.delete_calls) == 2


def test_delete_orphaned_image_objects_no_op_when_nothing_stale():
    s3 = FakeS3Client()
    deleted = app.delete_orphaned_image_objects(s3, "my-bucket", [])
    assert deleted == 0
    assert s3.delete_calls == []


def test_delete_orphaned_image_objects_handles_already_missing_s3_objects():
    """Real edge case: the DB row had a stored_url, but the S3 objects for
    it are already gone somehow -- list_objects_v2 finds nothing under
    that prefix. Must NOT call delete_objects with an empty Objects list
    -- a real, confirmed boto3 requirement: Delete.Objects needs at least
    one entry, or the call raises."""
    s3 = FakeS3Client(objects_by_prefix={})
    stale_rows = [{"id": "img-1", "stored_url": "https://bucket.s3.amazonaws.com/product-images/img-1/detail.png"}]

    deleted = app.delete_orphaned_image_objects(s3, "my-bucket", stale_rows)

    assert deleted == 0
    assert s3.delete_calls == []


# --- _process_one: the S3 cleanup wired end to end ---

def test_process_one_cleans_up_orphaned_s3_objects_for_stale_rows(monkeypatch):
    """End-to-end version of the fix: seeds a stale row that already has a
    real stored_url, and confirms _process_one's S3 cleanup actually fires
    and reports the deleted count."""
    db = _fresh_db()
    crown_url = "https://brunswickbowling.com/products/balls/current/crown-78u"
    db["_products_by_url"][crown_url] = 1
    db["_next_product_id"] = 2
    db["products"][1] = {"url": crown_url}
    stale_key = (1, "https://brunswickbowling.com/userfiles/unrelated-thumb.png")
    db["product_images"][stale_key] = {
        "id": "img-stale", "image_type": "other",
        "stored_url": "https://bucket.s3.amazonaws.com/product-images/img-stale/detail.png",
    }

    monkeypatch.setattr(app, "fetch_page", lambda url: _crown_78u_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.delenv("IMAGE_PROCESS_QUEUE_URL", raising=False)
    monkeypatch.delenv("PDF_PARSE_QUEUE_URL", raising=False)
    monkeypatch.setenv("IMAGE_BUCKET", "my-bucket")

    s3 = FakeS3Client(objects_by_prefix={
        "product-images/img-stale/": [
            "product-images/img-stale/thumbnail.png",
            "product-images/img-stale/catalog.png",
            "product-images/img-stale/detail.png",
        ],
    })

    job = {"url": crown_url, "brand_id": "brand-1"}
    result = app._process_one(job, None, s3_client=s3)

    assert result["orphaned_objects_deleted"] == 3
    assert len(s3.delete_calls) == 1


def test_process_one_skips_s3_cleanup_without_s3_client(monkeypatch):
    """Soft-fail path: no s3_client passed (mirrors handler() not building
    one when IMAGE_BUCKET isn't configured on this deployment) -- the
    DB-side delete still happens, S3 cleanup is just skipped (and logged),
    not an error."""
    db = _fresh_db()
    crown_url = "https://brunswickbowling.com/products/balls/current/crown-78u"
    db["_products_by_url"][crown_url] = 1
    db["_next_product_id"] = 2
    db["products"][1] = {"url": crown_url}
    stale_key = (1, "https://brunswickbowling.com/userfiles/unrelated-thumb.png")
    db["product_images"][stale_key] = {
        "id": "img-stale", "image_type": "other",
        "stored_url": "https://bucket.s3.amazonaws.com/product-images/img-stale/detail.png",
    }

    monkeypatch.setattr(app, "fetch_page", lambda url: _crown_78u_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.delenv("IMAGE_PROCESS_QUEUE_URL", raising=False)
    monkeypatch.delenv("PDF_PARSE_QUEUE_URL", raising=False)
    monkeypatch.delenv("IMAGE_BUCKET", raising=False)

    job = {"url": crown_url, "brand_id": "brand-1"}
    result = app._process_one(job, None)  # no s3_client passed

    assert result["orphaned_objects_deleted"] == 0
    remaining = {url for (pid, url) in db["product_images"] if pid == 1}
    assert stale_key[1] not in remaining  # DB row still removed regardless


def test_handler_builds_s3_client_when_image_bucket_configured(monkeypatch):
    """handler() mirrors sqs_client's existing conditional-build pattern
    for s3_client -- confirmed here by checking _process_one actually
    receives a non-None s3_client when IMAGE_BUCKET is set. boto3 itself
    isn't installed in this sandbox, so handler()'s
    `import boto3; boto3.client("s3")` is faked via sys.modules, same
    approach test_netsuite_product_scraper_orchestration.py's analogous
    test uses."""
    db = _fresh_db()
    crown_url = "https://brunswickbowling.com/products/balls/current/crown-78u"

    monkeypatch.setattr(app, "fetch_page", lambda url: _crown_78u_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.delenv("IMAGE_PROCESS_QUEUE_URL", raising=False)
    monkeypatch.delenv("PDF_PARSE_QUEUE_URL", raising=False)
    monkeypatch.setenv("IMAGE_BUCKET", "my-bucket")

    received = {}

    def fake_process_one(job, sqs_client, s3_client=None):
        received["s3_client"] = s3_client
        return {
            "product_id": "1", "sku_count": 0, "pdf_jobs_published": 0,
            "image_jobs_published": 0, "orphaned_objects_deleted": 0,
        }

    monkeypatch.setattr(app, "_process_one", fake_process_one)

    class _FakeBoto3:
        def client(self, name):
            assert name == "s3"
            return FakeS3Client()

    real_boto3 = sys.modules.get("boto3")
    sys.modules["boto3"] = _FakeBoto3()
    try:
        app.handler({"url": crown_url, "brand_id": "brand-1"}, None)
    finally:
        if real_boto3 is not None:
            sys.modules["boto3"] = real_boto3
        else:
            del sys.modules["boto3"]

    assert received["s3_client"] is not None


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


# --- Estimate-on-scrape plotter position (migrations 011/012) -- Al's
# ask: "estimate on scrape if not set". See app.estimate_oil_motion's
# module comment for the full reasoning.

def test_process_one_writes_estimated_plotter_position_when_unset(monkeypatch):
    db = _fresh_db()
    monkeypatch.setattr(app, "fetch_page", lambda url: _crown_78u_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.delenv("IMAGE_PROCESS_QUEUE_URL", raising=False)
    monkeypatch.delenv("PDF_PARSE_QUEUE_URL", raising=False)

    job = {"url": "https://brunswickbowling.com/products/balls/current/crown-78u", "brand_id": "brand-1"}
    app._process_one(job, None)

    product = next(iter(db["products"].values()))
    assert product["oil_rating"] is not None
    assert product["motion_rating"] is not None
    assert product["oil_motion_source"] == "estimated"


def test_process_one_never_overwrites_existing_plotter_position(monkeypatch):
    """A rescrape must never clobber a chart match (or a manual
    correction) with a fresh estimate -- the "where oil_rating is null"
    guard in the hook itself, exercised end to end here. Seeds the chart
    value AFTER an initial scrape (rather than before it) since the fake
    "insert into products" upsert branch -- mirroring this project's real
    ON CONFLICT DO UPDATE, which never touches oil_rating/motion_rating/
    oil_motion_source at all -- resets the row dict on first insert."""
    db = _fresh_db()
    crown_url = "https://brunswickbowling.com/products/balls/current/crown-78u"
    monkeypatch.setattr(app, "fetch_page", lambda url: _crown_78u_html())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))
    monkeypatch.delenv("IMAGE_PROCESS_QUEUE_URL", raising=False)
    monkeypatch.delenv("PDF_PARSE_QUEUE_URL", raising=False)

    job = {"url": crown_url, "brand_id": "brand-1"}
    app._process_one(job, None)  # first scrape -- lands an 'estimated' position

    product_id = next(iter(db["products"]))
    db["products"][product_id]["oil_rating"] = 6
    db["products"][product_id]["motion_rating"] = 18
    db["products"][product_id]["oil_motion_source"] = "chart"  # simulate a chart match landing afterward

    app._process_one(job, None)  # rescrape

    assert db["products"][product_id]["oil_rating"] == 6
    assert db["products"][product_id]["motion_rating"] == 18
    assert db["products"][product_id]["oil_motion_source"] == "chart"


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
