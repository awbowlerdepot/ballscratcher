#!/usr/bin/env python3
"""
Fetch every imported Kegel pattern's PDF sheet into the private
PatternSheetsBucket and render its page image (runbook 6ex).

Al: "im not following the uses forward drop brush, which also isn't clear
because you didn't pull in the PDFs" -> "pull all the pdfs". The 6et
import used Kegel's JSON only, so the admin review page had no sheet to
look at. This stores patterns/kegel/{kegel_id}.pdf plus page-1.png (the
same render the upload reader makes) and points the row's upload_key /
page_image_key at them, so the review page shows the sheet beside the
numbers exactly as for an uploaded sheet. Those keys aren't partner
content (no content_changed_at bump). Re-runnable: rows that already have
a page image are skipped. One request a second.

Usage (PG* from the bowling-scraper-db secret; AWS creds for S3):
    .venv/bin/python scripts/fetch_kegel_pdfs.py [--limit N]
"""
import argparse
import io
import os
import sys
import time

import boto3
import psycopg2
import requests

BASE = "https://patternlibrary.kegel.net"
UA = "BowlerIQ pattern import (bowleriq.com; with Kegel's OK)"
BUCKET = os.environ.get("PATTERN_SHEETS_BUCKET") or "bowling-scraper-pattern-sheets-563981859606"
RENDER_SCALE = 2.5  # same as pattern_ingest/extract.py


def render_page1(pdf: bytes) -> bytes:
    import pypdfium2 as pdfium

    page = pdfium.PdfDocument(pdf)[0]
    buf = io.BytesIO()
    page.render(scale=RENDER_SCALE).to_pil().convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def main(limit=None, delay=1.0):
    conn = psycopg2.connect()
    s3 = boto3.client("s3")
    with conn.cursor() as cur:
        cur.execute("select id, external_id, name from oil_patterns where external_source = 'kegel' and page_image_key is null "
                    "order by review_status = 'pending' desc, name")
        rows = cur.fetchall()
    rows = rows[:limit] if limit else rows
    print(f"{len(rows)} patterns to fetch", flush=True)
    ok = failed = 0
    for n, (pid, kid, name) in enumerate(rows, 1):
        try:
            r = requests.get(f"{BASE}/api/v1/patterns/download", params={"id": kid, "format": "pdf"},
                             headers={"User-Agent": UA}, timeout=120)
            r.raise_for_status()
            if not r.content.startswith(b"%PDF"):
                raise ValueError(f"not a PDF ({r.headers.get('content-type')})")
            pdf_key, png_key = f"patterns/kegel/{kid}.pdf", f"patterns/kegel/{kid}.page-1.png"
            s3.put_object(Bucket=BUCKET, Key=pdf_key, Body=r.content, ContentType="application/pdf")
            s3.put_object(Bucket=BUCKET, Key=png_key, Body=render_page1(r.content), ContentType="image/png")
            with conn.cursor() as cur:
                cur.execute("update oil_patterns set upload_key = %s, page_image_key = %s where id = %s",
                            (pdf_key, png_key, pid))
            conn.commit()
            ok += 1
        except Exception as e:  # noqa: BLE001 -- log and keep going; a re-run retries it
            conn.rollback()
            failed += 1
            print(f"  {name} ({kid}): {e}", flush=True)
        if n % 50 == 0:
            print(f"  {n}/{len(rows)} ({ok} ok, {failed} failed)", flush=True)
        time.sleep(delay)
    print(f"done: {ok} ok, {failed} failed", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    main(ap.parse_args().limit)
