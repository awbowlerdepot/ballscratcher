"""
Lambda authorizer for AdminApiFunction, per the decision to put a
shared-secret bearer token in front of the admin API rather than standing
up Cognito or IAM/SigV4 auth. Every real request to the admin API now
has to include `Authorization: Bearer <token>`, where `<token>` matches
the value stored in the Secrets Manager secret referenced by
`ADMIN_API_TOKEN_SECRET_ARN`.

**Shape**: this is an API Gateway HTTP API (v2) REQUEST authorizer using
the "simple responses" format (`AuthorizerPayloadFormatVersion: "2.0"`,
`EnableSimpleResponses: true` in template.yaml) -- the handler returns
`{"isAuthorized": bool}`, not a full IAM policy document. See
template.yaml's `AdminHttpApi` resource for the wiring.

**Fail-closed by design, at two separate points**:
1. If `ADMIN_API_TOKEN_SECRET_ARN` is unset or blank (the parameter's
   default in template.yaml), `handler()` returns `isAuthorized: False`
   immediately rather than allowing every request through -- a missing
   secret ARN means "not configured yet", not "no auth required".
2. If the `Authorization` header is missing, malformed (not a `Bearer
   <token>` string), or simply doesn't match the stored token,
   `is_valid_token()` returns `False` the same way a real mismatch would
   -- there's no code path that defaults to allow.

If Secrets Manager itself errors (bad ARN, missing IAM permission, etc.),
`get_expected_token()` lets that exception propagate rather than catching
it and quietly returning a "no token configured" state -- API Gateway
turns an unhandled authorizer exception into a 500, which is still
fail-closed, just a louder failure than a clean 401/403. That's a
deliberate choice: masking a real permissions/config bug behind a generic
"unauthorized" response would make it harder to diagnose why the admin
API stopped working after a deploy.

**What's real vs. what's a disclosed guess, since this sandbox has no AWS
access to actually invoke a real HTTP API authorizer this session**: the
HTTP API v2 authorizer event/response shape (`event["headers"]`, a dict
of lowercased header names; `{"isAuthorized": bool}` as the accepted
simple-response format) is documented, current, real AWS behavior --
verified against AWS's own API Gateway developer docs this session, not
recalled from training data alone. What's NOT verified: whether every
client this API will actually see sends the header as exactly
`authorization` (HTTP header names are case-insensitive on the wire, and
API Gateway is documented to normalize them to lowercase in the v2
payload, but that's taken on faith from the docs, not observed directly)
-- `_get_header()` below matches case-insensitively as a defensive
measure regardless, so this shouldn't matter in practice even if the
lowercasing assumption were somehow wrong for a given client.

**Token caching**: `get_expected_token()` caches the fetched-and-parsed
token in a module-level dict keyed by secret ARN, so a warm Lambda
container doesn't call Secrets Manager on every single request. This is
deliberately a hand-rolled cache rather than relying on API Gateway's own
authorizer result caching (`AuthorizerResultTtlInSeconds`) -- that
feature's exact interaction with per-request identity sources for HTTP
API Lambda authorizers wasn't verified this session, so a cache this
module directly controls is more predictable than trusting unverified
platform behavior. Tradeoff: rotating the secret won't take effect for
already-warm containers until they're recycled -- acceptable for a
shared-secret token, not something you'd want for anything requiring
instant revocation.
"""
import hmac
import json
import os

_TOKEN_CACHE = {}


def extract_bearer_token(header_value):
    """`"Bearer abc123"` -> `"abc123"`. Case-insensitive on the scheme
    ("bearer", "BEARER", "Bearer" all accepted -- HTTP header *values*
    aren't case-normalized by API Gateway the way header *names* are, so
    this handles it explicitly rather than assuming a client always sends
    the canonical casing). Returns None for anything that isn't a
    well-formed "Bearer <token>" string: missing header, wrong scheme, no
    token after the scheme, or extra whitespace-only token.
    """
    if not header_value or not isinstance(header_value, str):
        return None
    parts = header_value.strip().split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def parse_secret_value(raw_secret_string):
    """Accepts either a bare token string or JSON `{"token": "..."}` as
    the Secrets Manager secret's contents, since either is a reasonable
    way for a human to have created the secret by hand in the console.
    Tries JSON first; if that parses to a dict with a "token" key, uses
    that value. Otherwise falls back to treating the whole raw string
    (stripped) as the token itself -- covers both a bare-string secret
    and a JSON parse failure on genuinely non-JSON input.
    """
    stripped = raw_secret_string.strip()
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return stripped or None
    if isinstance(parsed, dict) and "token" in parsed:
        value = parsed["token"]
        return value.strip() if isinstance(value, str) else None
    # Valid JSON, but not the {"token": ...} shape -- fall back to the
    # raw string rather than silently returning something unexpected.
    return stripped or None


def is_valid_token(provided_token, expected_token):
    """Constant-time comparison via hmac.compare_digest, specifically to
    avoid a timing side-channel on token comparison (an admin API worth
    protecting is worth protecting against timing attacks too, and
    compare_digest costs nothing here). Returns False for any missing/
    empty value on either side -- an empty expected token (e.g. a secret
    that was created blank) must never compare equal to an empty/missing
    provided token.
    """
    if not provided_token or not expected_token:
        return False
    return hmac.compare_digest(provided_token, expected_token)


def _get_header(headers, name):
    """Case-insensitive header lookup. API Gateway HTTP API v2 documents
    that it lowercases header names in the payload, but this doesn't rely
    on that alone -- see the module docstring."""
    if not headers:
        return None
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def get_expected_token(secret_arn):
    """Fetches and parses the expected token from Secrets Manager,
    caching the result per secret_arn for the life of the Lambda
    container. boto3 import is deferred to keep this module importable
    without boto3 installed (matches the rest of this project's
    convention -- see bowlerdepot_reconciliation/app.py and
    bowwwl_cross_check/app.py's get_db_connection()-style functions for
    the same pattern), and this specific function is correspondingly
    untested in this session: no AWS access here to verify it against a
    real secret. The pure parsing it delegates to (parse_secret_value)
    is what's actually tested.
    """
    if secret_arn in _TOKEN_CACHE:
        return _TOKEN_CACHE[secret_arn]
    import boto3

    client = boto3.client("secretsmanager")
    raw = client.get_secret_value(SecretId=secret_arn)["SecretString"]
    token = parse_secret_value(raw)
    _TOKEN_CACHE[secret_arn] = token
    return token


def handler(event, context):
    """HTTP API v2 REQUEST authorizer, simple-response format. See the
    module docstring for the two fail-closed checkpoints this goes
    through before ever returning `isAuthorized: True`.
    """
    secret_arn = os.environ.get("ADMIN_API_TOKEN_SECRET_ARN", "")
    if not secret_arn:
        # Not configured yet (template.yaml's AdminApiTokenSecretArn
        # parameter defaults to blank) -- fail closed rather than
        # treating "no secret set up" as "no auth required".
        return {"isAuthorized": False}

    headers = event.get("headers", {})
    provided_token = extract_bearer_token(_get_header(headers, "authorization"))
    expected_token = get_expected_token(secret_arn)
    return {"isAuthorized": is_valid_token(provided_token, expected_token)}
