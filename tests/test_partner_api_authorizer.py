"""Partner API authorizer (runbook 6cj): per-partner hashed keys,
fail-closed on every malformed or missing input."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "partner_api_authorizer"))

import app  # noqa: E402

KEY = "biq_partner_test-key-123"
HASHES = {"acme": app.hash_key(KEY), "other": app.hash_key("biq_partner_other")}


def _event(auth):
    return {"headers": {"authorization": auth} if auth is not None else {}}


def _run(event, hashes=HASHES):
    original = app._load_hashes
    app._load_hashes = lambda: hashes
    try:
        return app.handler(event, None)
    finally:
        app._load_hashes = original


def test_valid_key_is_authorized_and_names_the_partner():
    assert _run(_event(f"Bearer {KEY}")) == {"isAuthorized": True, "context": {"partner": "acme"}}


def test_header_name_and_scheme_are_case_insensitive():
    assert _run({"headers": {"Authorization": f"bearer {KEY}"}})["isAuthorized"] is True


def test_wrong_missing_or_malformed_keys_are_denied():
    for auth in (None, "", "Bearer", "Bearer ", f"Basic {KEY}", KEY, "Bearer nope", f"Bearer {KEY}x"):
        assert _run(_event(auth)) == {"isAuthorized": False}, auth


def test_no_keys_configured_denies_everyone():
    assert _run(_event(f"Bearer {KEY}"), hashes={}) == {"isAuthorized": False}


def test_parse_secret_is_fail_closed_on_garbage():
    assert app.parse_secret("not json") == {}
    assert app.parse_secret(json.dumps(["a"])) == {}
    assert app.parse_secret(json.dumps({"keys": {"acme": "short"}})) == {}
    good = app.hash_key(KEY)
    assert app.parse_secret(json.dumps({"keys": {"acme": good.upper()}})) == {"acme": good}


def test_secret_never_needs_the_plaintext_key():
    """The stored value is a SHA-256 hex digest, never the key itself."""
    assert KEY not in json.dumps(HASHES)
    assert all(len(h) == 64 for h in HASHES.values())
