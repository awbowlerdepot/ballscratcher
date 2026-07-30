"""
Tests for the orchestration additions to src/pdf_parser/app.py
(_extract_jobs, _process_one, handler's SQS-batch support). Same rationale
and pattern as test_product_scraper_orchestration.py -- separate from
test_pdf_parser.py (which covers the real-data parsing logic) because this
needs fake DB/PDF-fetch objects and runs standalone via
`python3 tests/test_pdf_parser_orchestration.py`.

Uses the real crown_78u_info_sheet.txt fixture (see tests/fixtures/ and
test_pdf_parser.py for its provenance) via a monkeypatched extract_pdf_text,
so _process_one runs the real parse_info_sheet against real field values.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "pdf_parser"))

import app  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def test_extract_jobs_direct_invocation():
    event = {"info_sheet_url": "https://cdn.example/info.pdf", "product_id": "prod-1"}
    assert app._extract_jobs(event) == [(event, None)]


def test_extract_jobs_sqs_batch():
    event = {
        "Records": [
            {"messageId": "m-1", "body": json.dumps({"info_sheet_url": "https://cdn.example/a.pdf", "product_id": "p1"})},
        ]
    }
    jobs = app._extract_jobs(event)
    assert jobs == [({"info_sheet_url": "https://cdn.example/a.pdf", "product_id": "p1"}, "m-1")]


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

        if q.startswith("select weight_lbs, rg, differential, mass_bias"):
            (product_id,) = params
            rows = self.db["product_skus"].get(product_id, {})
            self._result = [(w, v["rg"], v["differential"], v["mass_bias"]) for w, v in sorted(rows.items())]

        elif q.startswith("insert into product_skus"):
            product_id, weight, rg, differential, mass_bias = params
            existing = self.db["product_skus"].setdefault(product_id, {})
            existing.setdefault(weight, {"rg": rg, "differential": differential, "mass_bias": mass_bias})
            self._result = None

        elif q.startswith("update product_skus set"):
            rg, sku_rg, diff, sku_diff, mass_bias, product_id, weight = params
            row = self.db["product_skus"][product_id][weight]
            # Mirrors the real SQL: coalesce(%s, coalesce(rg, %s)) etc.
            row["rg"] = rg if rg is not None else (row["rg"] if row["rg"] is not None else sku_rg)
            row["differential"] = diff if diff is not None else (row["differential"] if row["differential"] is not None else sku_diff)
            row["mass_bias"] = row["mass_bias"] if row["mass_bias"] is not None else mass_bias
            self._result = None

        elif q.startswith("insert into review_queue"):
            product_id, field_name, current_value, proposed_value, reason = params
            self.db["review_queue"].append({
                "product_id": product_id, "field_name": field_name,
                "current_value": current_value, "proposed_value": proposed_value, "reason": reason,
            })
            self._result = None

        else:
            raise NotImplementedError(f"FakeCursor doesn't support: {q}")

    def fetchall(self):
        return self._result

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
    return {"product_skus": {}, "review_queue": []}


def _crown_78u_pdf_text():
    return (FIXTURES / "crown_78u_info_sheet.txt").read_text()


def test_process_one_fills_gaps_with_no_existing_skus(monkeypatch):
    """No pre-existing product_skus rows (e.g. HTML scrape hadn't run, or
    ran with no matching weights) -- every PDF weight should just insert,
    nothing flagged."""
    db = _fresh_db()
    monkeypatch.setattr(app, "fetch_pdf", lambda url: b"fake-pdf-bytes")
    monkeypatch.setattr(app, "extract_pdf_text", lambda pdf_bytes: _crown_78u_pdf_text())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))

    result = app._process_one({"info_sheet_url": "https://cdn.example/crown78u.pdf", "product_id": "prod-1"})

    assert result["sku_count"] == 5
    assert result["flagged_count"] == 0
    assert db["product_skus"]["prod-1"][16]["rg"] == 2.557


def test_process_one_flags_real_crown_78u_mismatch(monkeypatch):
    """The real, motivating case: an existing (HTML-sourced) 16lb RG of
    2.577 disagrees with the PDF's 2.557 -- should be flagged in
    review_queue, and the stored value should NOT be silently overwritten."""
    db = _fresh_db()
    db["product_skus"]["prod-1"] = {16: {"rg": 2.577, "differential": 0.039, "mass_bias": None}}
    monkeypatch.setattr(app, "fetch_pdf", lambda url: b"fake-pdf-bytes")
    monkeypatch.setattr(app, "extract_pdf_text", lambda pdf_bytes: _crown_78u_pdf_text())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))

    result = app._process_one({"info_sheet_url": "https://cdn.example/crown78u.pdf", "product_id": "prod-1"})

    assert result["flagged_count"] == 1
    assert len(db["review_queue"]) == 1
    assert db["review_queue"][0]["field_name"] == "rg_16lb"
    assert db["review_queue"][0]["current_value"] == "2.577"
    assert db["review_queue"][0]["proposed_value"] == "2.557"
    # Stored value must NOT have been silently overwritten.
    assert db["product_skus"]["prod-1"][16]["rg"] == 2.577


def test_handler_sqs_batch_reports_only_failed_message(monkeypatch):
    db = _fresh_db()

    def fake_fetch_pdf(url):
        if "boom" in url:
            raise RuntimeError("simulated fetch failure")
        return b"fake-pdf-bytes"

    monkeypatch.setattr(app, "fetch_pdf", fake_fetch_pdf)
    monkeypatch.setattr(app, "extract_pdf_text", lambda pdf_bytes: _crown_78u_pdf_text())
    monkeypatch.setattr(app, "get_db_connection", lambda: FakeConnection(db))

    event = {
        "Records": [
            {"messageId": "ok-1", "body": json.dumps({"info_sheet_url": "https://cdn.example/ok.pdf", "product_id": "p1"})},
            {"messageId": "fail-1", "body": json.dumps({"info_sheet_url": "https://cdn.example/boom.pdf", "product_id": "p1"})},
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

        def setattr(self, obj, name, value):
            self._sets.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, value in reversed(self._sets):
                setattr(obj, name, value)

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
