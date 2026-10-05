"""
Lambda authorizer for the Partner API (DEPLOY_RUNBOOK.md 6cj). Al wanted
"a lightweight version of security ontop of that just to keep it in house"
for a server-side, read-only partner platform.

Each partner gets its own key, sent as `Authorization: Bearer <key>`. The
keys live in ONE Secrets Manager secret (PARTNER_API_KEYS_SECRET_ID,
default "bowling-scraper-partner-api-keys") as SHA-256 hashes, never the
keys themselves:

    {"keys": {"<partner name>": "<sha256 hex of the key>", ...}}

so reading the secret doesn't hand anyone a working key, and revoking one
partner is deleting one entry. scripts/create_partner_api_key.py mints a
key, prints it once, and stores only its hash.

Same shape and posture as admin_api_authorizer: HTTP API v2 REQUEST
authorizer with simple responses ({"isAuthorized": bool}), fail-closed:
  * no Authorization header, not "Bearer <key>", or no matching hash -> deny
  * the secret doesn't exist yet, or has no keys -> deny (not "allow all")
  * any other Secrets Manager error propagates -> API Gateway returns 500
    (still closed, and loud enough to diagnose)
On success it returns context {"partner": "<name>"}, which shows up in the
partner API's logs, so every call is attributable to a partner.

The secret is cached for SECRET_CACHE_SECONDS so a revoked key stops
working within a few minutes without a deploy; API Gateway additionally
caches each key's decision for the authorizer TTL (template.yaml).
"""
import hashlib
import hmac
import json
import os
import time

SECRET_CACHE_SECONDS = 300

_cache = {"hashes": None, "loaded_at": 0.0}


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _get_header(headers, name):
    if not headers:
        return None
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return None


def extract_bearer(headers):
    value = _get_header(headers, "authorization")
    if not value or not isinstance(value, str):
        return None
    parts = value.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return None
    return parts[1].strip()


def parse_secret(secret_string: str) -> dict:
    """{"keys": {name: sha256hex}} -> {name: sha256hex}. Anything malformed
    yields {} (deny everyone) rather than raising -- a typo in the secret
    must not open the API."""
    try:
        data = json.loads(secret_string)
        keys = data.get("keys", {}) if isinstance(data, dict) else {}
        return {str(n): str(h).lower() for n, h in keys.items() if isinstance(h, str) and len(h) == 64}
    except (ValueError, AttributeError):
        return {}


def match_partner(provided_key, hashes: dict):
    """Returns the partner name whose stored hash matches, else None.
    Compares against EVERY entry with hmac.compare_digest (no early exit),
    so timing doesn't reveal which or how many partners exist."""
    if not provided_key or not hashes:
        return None
    provided = hash_key(provided_key)
    found = None
    for name, stored in hashes.items():
        if hmac.compare_digest(provided, stored):
            found = name
    return found


def _load_hashes():
    import boto3

    now = time.time()
    if _cache["hashes"] is not None and now - _cache["loaded_at"] < SECRET_CACHE_SECONDS:
        return _cache["hashes"]
    client = boto3.client("secretsmanager")
    secret_id = os.environ.get("PARTNER_API_KEYS_SECRET_ID", "bowling-scraper-partner-api-keys")
    try:
        secret_string = client.get_secret_value(SecretId=secret_id)["SecretString"]
    except client.exceptions.ResourceNotFoundException:
        secret_string = "{}"  # not set up yet: deny everyone
    hashes = parse_secret(secret_string)
    _cache.update(hashes=hashes, loaded_at=now)
    return hashes


def handler(event, context):
    partner = match_partner(extract_bearer(event.get("headers")), _load_hashes())
    if partner is None:
        return {"isAuthorized": False}
    return {"isAuthorized": True, "context": {"partner": partner}}
