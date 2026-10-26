"""pattern_ingest (runbook 6ej): parsing the model's reply, and the ingest
flow against fake S3/Bedrock/DB. No network."""
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "pattern_ingest"))

import app  # noqa: E402
import extract  # noqa: E402

REPLY = {
    "name": "PBA ELITE SERIES 48 - 42", "machine_model": "Flex", "length_feet": 48, "reverse_brush_drop_feet": 42,
    "oil_per_board_ul": 50, "forward_ml": 14.65, "reverse_ml": 15.55, "volume_ml": 30.2,
    "tank_configuration": "A Only", "tank_a_conditioner": "KEGEL", "tank_b_conditioner": "KEGEL",
    "cleaner_ratio_main": "5:1", "cleaner_ratio_back_end": "5:1", "cleaner_back_end_distance_feet": 59,
    "buffer_rpm": {"1": 100, "2": 200, "3": 500, "4": 650},
    "forward_steps": [{"order": 1, "start": "2L", "stop": "2R", "loads": 3, "speed": 14, "buffer": 3, "tank": "A",
                       "crossed": 111, "start_feet": 0.0, "end_feet": 3.9, "feet": 3.9, "oil": 5550}],
    "reverse_steps": [{"order": 1, "start": "15L", "stop": "14R", "loads": 1, "speed": 22, "buffer": 3, "tank": "A",
                       "crossed": 12, "start_feet": 40.0, "end_feet": 36.9, "feet": -3.1, "oil": 600}],
    "track_zones": [{"item": "3L-7L:18L-18R", "description": "Outside:Middle", "ratio": 2}],
}


def test_parse_response_maps_to_spec_fields_even_inside_a_code_fence():
    f = extract.parse_response("Here it is:\n```json\n" + json.dumps(REPLY) + "\n```")
    assert f["length_feet"] == 48 and f["volume_ml"] == 30.2
    assert f["machine"]["make"] == "Kegel" and f["machine"]["model"] == "Flex" and f["machine"]["buffer_rpm"]["4"] == 650
    fwd, rev = f["steps"]
    assert fwd["direction"] == "forward" and fwd["speed_ips"] == 14 and fwd["oil_ul"] == 5550
    assert rev["direction"] == "reverse" and rev["feet"] == -3.1
    assert f["track_zones"] == [{"label": "Outside:Middle", "ratio": 2, "from": "3L", "to": "7L", "against_from": "18L", "against_to": "18R"}]


def test_parse_response_rejects_non_json():
    try:
        extract.parse_response("I can't read this sheet.")
        assert False
    except ValueError:
        pass


class _Cur:
    def __init__(self, db):
        self.db, self.row = db, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        q = " ".join(sql.split()).lower()
        self.db.executed.append((q, params))
        if q.startswith("select upload_key"):
            self.row = (self.db.upload_key, self.db.review_status, self.db.name)

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, review_status="pending", upload_key="patterns/p1/sheet.pdf", name="Uploaded sheet: x.pdf"):
        self.review_status, self.upload_key, self.name, self.executed, self.commits = review_status, upload_key, name, [], 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class _S3:
    def __init__(self):
        self.put = []

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(b"%PDF fake")}

    def put_object(self, **kw):
        self.put.append(kw)


class _Bedrock:
    def invoke_model(self, **kw):
        return {"body": io.BytesIO(json.dumps({"content": [{"text": json.dumps(REPLY)}], "usage": {}}).encode())}


def test_ingest_writes_fields_and_page_image_for_a_pending_pattern(monkeypatch):
    monkeypatch.setattr(extract, "render_pages", lambda pdf: [b"PNG1"])
    monkeypatch.setattr(extract, "text_layer", lambda pdf: "")  # image-only sheet -> vision
    conn, s3 = _Conn(), _S3()
    out = app.ingest(conn, s3, _Bedrock(), "bucket", "p1")
    assert out["steps"] == 2 and "name" in out["fields"]
    assert s3.put[0]["Key"] == "patterns/p1/page-1.png" and s3.put[0]["ContentType"] == "image/png"
    final_sql, params = conn.executed[-1]
    assert "extraction_status = 'ready'" in final_sql and "review_status = 'pending'" in final_sql
    assert "PBA Elite Series 48 - 42" in params  # title-cased (runbook 6er)


def test_ingest_keeps_a_name_the_admin_already_typed(monkeypatch):
    monkeypatch.setattr(extract, "render_pages", lambda pdf: [b"PNG1"])
    monkeypatch.setattr(extract, "text_layer", lambda pdf: "")
    conn = _Conn(name="Elite 48 (2026)")
    out = app.ingest(conn, _S3(), _Bedrock(), "bucket", "p1")
    assert "name" not in out["fields"]


def test_ingest_never_touches_a_reviewed_pattern():
    for status in ("approved", "rejected"):
        conn = _Conn(review_status=status)
        assert app.ingest(conn, _S3(), _Bedrock(), "bucket", "p1") == {"pattern_id": "p1", "skipped": status}
        assert not any("update" in q for q, _ in conn.executed)


def test_ingest_requires_an_upload():
    try:
        app.ingest(_Conn(upload_key=None), _S3(), _Bedrock(), "bucket", "p1")
        assert False
    except ValueError as e:
        assert "No sheet" in str(e)


def test_names_in_capitals_become_title_case():
    """Runbook 6er: Kegel prints titles in capitals; acronyms stay upper,
    joining words go lower, codes with digits and mixed-case names are kept."""
    t = extract.tidy_name
    assert t("PBA ELITE SERIES 37") == "PBA Elite Series 37"
    assert t("PBA ELITE SERIES 48 - 42") == "PBA Elite Series 48 - 42"
    assert t("USBC OPEN CHAMPIONSHIPS OF THE YEAR") == "USBC Open Championships of the Year"
    assert t("WTBA BEIJING 2026") == "WTBA Beijing 2026"
    assert t("KEGEL CHALLENGE-SERIES BADLANDS") == "Kegel Challenge-Series Badlands"
    assert t("THE BEAST") == "The Beast"
    assert t("Kegel Navigation 3.0 Route 66") == "Kegel Navigation 3.0 Route 66"
    assert t(None) is None and t("") == ""


HOUSE_TEXT = open(os.path.join(os.path.dirname(__file__), "fixtures", "pattern_library_house_2022.txt")).read()


def test_pattern_library_text_layer_is_read_exactly():
    """Runbook 6es: Kegel Pattern Library sheets are read from the PDF's
    text (no vision); MICS per step, distance as a->b, no crossed column."""
    f = extract.parse_pattern_library(HOUSE_TEXT)
    assert f["name"] == "2022 Starting House Pattern" and f["category"] == "house"
    assert (f["length_feet"], f["reverse_brush_drop_feet"], f["oil_per_board_ul"]) == (43.0, 40.0, 50.0)
    assert (f["volume_ml"], f["forward_ml"], f["reverse_ml"]) == (25.4, 12.1, 13.3)
    assert f["machine"] == {"make": "Kegel", "tank_a_conditioner": "KEGEL", "tank_configuration": "A Only"}
    fwd = [s for s in f["steps"] if s["direction"] == "forward"]
    rev = [s for s in f["steps"] if s["direction"] == "reverse"]
    assert len(fwd) == 8 and len(rev) == 8
    assert fwd[0] == {"direction": "forward", "order": 1, "start": "2L", "stop": "2R", "loads": 1, "mics": 50.0,
                      "speed_ips": 14.0, "buffer": 3, "tank": "A", "start_feet": 0.0, "end_feet": 0.0, "feet": 0.0,
                      "oil_ul": 1850}
    assert (rev[3]["start"], rev[3]["loads"], rev[3]["start_feet"], rev[3]["end_feet"], rev[3]["feet"], rev[3]["oil_ul"]) == (
        "10L", 3, 27.0, 19.0, -8.0, 3150)
    assert "crossed" not in fwd[0]
    assert f["track_zones"] == []


def test_other_text_is_not_mistaken_for_a_pattern_library_sheet():
    assert extract.parse_pattern_library("") is None
    assert extract.parse_pattern_library("PBA ELITE SERIES 48 Oil Per Board 50") is None
    # Missing the reverse table -> not a complete program.
    assert extract.parse_pattern_library(HOUSE_TEXT.split("REVERSE LOADS DATA")[0]) is None


def test_ingest_uses_the_text_layer_without_calling_bedrock(monkeypatch):
    monkeypatch.setattr(extract, "render_pages", lambda pdf: [b"PNG1"])
    monkeypatch.setattr(extract, "text_layer", lambda pdf: HOUSE_TEXT)

    class NoBedrock:
        def invoke_model(self, **kw):
            raise AssertionError("vision should not be called for a text-layer sheet")

    conn = _Conn()
    out = app.ingest(conn, _S3(), NoBedrock(), "bucket", "p1")
    assert out["reader"] == extract.TEXT_MODEL_ID and out["steps"] == 16 and "category" in out["fields"]
    final_sql, params = conn.executed[-1]
    assert extract.TEXT_MODEL_ID in params and "house" in params
