"""Partner API v1 CONTRACT LOCK (runbook 6cj). Al: "locking the shape down
so that if we move the learn and consumer sites forward it doesn't break
the integration to other platform".

The OpenAPI schema FastAPI generates from models.py + app.py must match
the committed snapshot exactly. If this fails:
  * you removed/renamed/retyped a field or endpoint -> don't; that's /v2.
  * you ADDED an optional field or a new endpoint (allowed in v1) ->
    regenerate the snapshot on purpose:
        PARTNER_API_UPDATE_SNAPSHOT=1 .venv/bin/python -m pytest -q tests/test_partner_api_contract.py
    and review the fixture diff in the same commit.
A second test checks the v1 rules directly against the snapshot, so even
a regenerated snapshot can't quietly drop a field or make one required."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "partner_api"))

import app  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "partner_api_v1_openapi.json")

# The v1 surface as first published. Each may GAIN fields but must keep
# every field listed here, with these required-ness flags.
V1_BASELINE = {
    "Ball": {
        "required": {"id", "name", "brand", "status", "coverstock", "core", "has_particle", "weights", "manufacturer_url", "content_changed_at"},
        "optional": {"release_date", "color", "factory_finish", "finish_category", "plotter", "image_url", "learn_article_url", "bowlerdepot_url"},
    },
    "Brand": {"required": {"id", "name"}, "optional": set()},
    "Weight": {"required": {"weight_lbs"}, "optional": {"rg", "differential", "mass_bias"}},
    "PlotterPosition": {"required": {"oil", "motion", "source"}, "optional": set()},
    "Change": {"required": {"id", "change", "changed_at"}, "optional": {"ball"}},
    "ChangePage": {"required": {"items", "next_cursor", "has_more"}, "optional": {"api_version"}},
    "BallPage": {"required": {"items"}, "optional": {"api_version", "next_cursor"}},
}
V1_PATHS = {"/v1/brands", "/v1/balls", "/v1/balls/{ball_id}", "/v1/changes", "/v1/plotter"}


def _schema():
    return json.loads(json.dumps(app.app.openapi(), sort_keys=True))


def test_openapi_matches_committed_snapshot():
    current = _schema()
    if os.environ.get("PARTNER_API_UPDATE_SNAPSHOT") == "1":
        os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
        with open(FIXTURE, "w") as f:
            json.dump(current, f, indent=2, sort_keys=True)
            f.write("\n")
    with open(FIXTURE) as f:
        committed = json.load(f)
    assert current == committed, "Partner API v1 shape changed -- see this file's docstring"


def test_v1_rules_hold_against_the_snapshot():
    with open(FIXTURE) as f:
        committed = json.load(f)
    assert V1_PATHS <= set(committed["paths"]), "a v1 endpoint was removed"
    schemas = committed["components"]["schemas"]
    for name, rule in V1_BASELINE.items():
        props = set(schemas[name]["properties"])
        required = set(schemas[name].get("required", []))
        missing = (rule["required"] | rule["optional"]) - props
        assert not missing, f"{name}: v1 fields removed: {missing}"
        assert rule["required"] <= required, f"{name}: a required v1 field became optional"
        assert not (required & rule["optional"]), f"{name}: an optional v1 field became required"
        assert schemas[name].get("additionalProperties") is False, f"{name}: must forbid extra fields"


def test_api_version_is_1():
    assert app.app.version == "1"
