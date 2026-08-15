"""
Tests for scripts/dump_plotter_estimate_training_data.py.

Manual-runner pattern, run standalone via
`python3 tests/test_dump_plotter_estimate_training_data.py`.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import dump_plotter_estimate_training_data as script  # noqa: E402


# --- reference_sku: must match every scraper's own _reference_sku
# byte-for-byte (15lb preferred, else heaviest weight with a differential)
# -- the whole point of this script is reproducing the exact differential
# estimate_oil_motion would see at scrape time. ---

def test_reference_sku_prefers_15lb():
    skus = [
        {"weight_lbs": 14, "differential": 0.020},
        {"weight_lbs": 15, "differential": 0.035},
        {"weight_lbs": 16, "differential": 0.050},
    ]
    assert script.reference_sku(skus)["weight_lbs"] == 15


def test_reference_sku_falls_back_to_heaviest_with_differential():
    skus = [
        {"weight_lbs": 14, "differential": 0.020},
        {"weight_lbs": 16, "differential": 0.050},
    ]
    assert script.reference_sku(skus)["weight_lbs"] == 16


def test_reference_sku_skips_15lb_row_with_no_differential():
    skus = [
        {"weight_lbs": 15, "differential": None},
        {"weight_lbs": 14, "differential": 0.020},
    ]
    assert script.reference_sku(skus)["weight_lbs"] == 14


def test_reference_sku_none_for_empty_list():
    assert script.reference_sku([]) is None


def test_reference_sku_none_when_no_sku_has_a_differential():
    skus = [{"weight_lbs": 15, "differential": None}]
    assert script.reference_sku(skus) is None


# --- build_training_row: assembly logic, no network ---

def test_build_training_row_shape():
    entry = {"id": "prod-1", "oil": 6, "motion": 18}
    detail = {
        "id": "prod-1", "brand_name": "Brunswick", "name": "Crown Victory",
        "core_type": "asymmetric", "coverstock_type": "solid",
        "coverstock_material": "reactive_resin", "has_particle": False,
        "skus": [{"weight_lbs": 15, "differential": 0.045}],
    }
    row = script.build_training_row(entry, detail)
    assert row == {
        "product_id": "prod-1", "brand_name": "Brunswick", "name": "Crown Victory",
        "actual_oil": 6, "actual_motion": 18,
        "core_type": "asymmetric", "coverstock_type": "solid",
        "coverstock_material": "reactive_resin", "has_particle": False,
        "differential": 0.045,
    }


def test_build_training_row_tolerates_missing_skus():
    entry = {"id": "prod-2", "oil": 9, "motion": 17}
    detail = {
        "id": "prod-2", "brand_name": "Hammer", "name": "Black Widow Mania",
        "core_type": "asymmetric", "coverstock_type": "pearl",
        "coverstock_material": "reactive_resin", "has_particle": False,
        "skus": [],
    }
    row = script.build_training_row(entry, detail)
    assert row["differential"] is None


# --- run: orchestration, no network (injected fakes) ---

def test_run_pulls_detail_only_for_chart_matched_positions():
    positions = [
        {"id": "prod-1", "name": "Crown Victory", "oil": 6, "motion": 18, "oil_motion_source": "chart"},
    ]
    detail_calls = []

    def fake_get_detail(pid):
        detail_calls.append(pid)
        return {
            "id": pid, "brand_name": "Brunswick", "name": "Crown Victory",
            "core_type": "asymmetric", "coverstock_type": "solid",
            "coverstock_material": "reactive_resin", "has_particle": False,
            "skus": [{"weight_lbs": 15, "differential": 0.045}],
        }

    rows = script.run(
        "https://admin.example", "tok", "https://public.example",
        list_positions_fn=lambda: positions, get_detail_fn=fake_get_detail,
    )

    assert detail_calls == ["prod-1"]
    assert len(rows) == 1
    assert rows[0]["actual_oil"] == 6


def test_run_skips_product_with_no_detail():
    positions = [{"id": "prod-1", "name": "Ghost", "oil": 6, "motion": 18, "oil_motion_source": "chart"}]
    rows = script.run(
        "https://admin.example", "tok", "https://public.example",
        list_positions_fn=lambda: positions, get_detail_fn=lambda pid: None,
    )
    assert rows == []


def test_run_tolerates_per_product_errors():
    positions = [
        {"id": "prod-1", "name": "Boom", "oil": 6, "motion": 18, "oil_motion_source": "chart"},
        {"id": "prod-2", "name": "Fine", "oil": 9, "motion": 17, "oil_motion_source": "chart"},
    ]

    def flaky_get_detail(pid):
        if pid == "prod-1":
            raise RuntimeError("simulated network failure")
        return {
            "id": pid, "brand_name": "Hammer", "name": "Fine",
            "core_type": "symmetric", "coverstock_type": "pearl",
            "coverstock_material": "urethane", "has_particle": False,
            "skus": [],
        }

    rows = script.run(
        "https://admin.example", "tok", "https://public.example",
        list_positions_fn=lambda: positions, get_detail_fn=flaky_get_detail,
    )

    assert len(rows) == 1
    assert rows[0]["product_id"] == "prod-2"


# --- list_chart_matched_positions filtering happens at the network layer
# in this script (unlike run's injected fakes above), so this only checks
# get_requests_session's retry config sanity, same as every other script.

def test_get_requests_session_retries_on_throttle_and_5xx_status_codes():
    session = script.get_requests_session()
    adapter = session.get_adapter("https://admin.example")
    retry = adapter.max_retries

    assert retry.total == script.RETRY_TOTAL
    assert set(retry.status_forcelist) == set(script.RETRY_STATUS_FORCELIST)
    assert 503 in retry.status_forcelist
    assert "GET" in retry.allowed_methods
    assert retry.backoff_factor == script.RETRY_BACKOFF_FACTOR


if __name__ == "__main__":
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_")]
    passed = 0
    for name, t in tests:
        t()
        print(f"PASS: {name}")
        passed += 1
    print(f"\n{passed}/{len(tests)} tests passed")
