"""
Lambda authorizer for AdminApiFunction. v2 (task #465-473, Al: "can we
build a more sophisticated admin spa that is hosted on aws and has
users"): DUAL-MODE now, not shared-secret-only. Every request still has
to include `Authorization: Bearer <token>`, but `<token>` can now be
EITHER:
  1. A real Cognito ID token issued by AdminUserPool (a human, signed in
     through the new admin-spa/ SPA) -- verified via decode_cognito_jwt
     (signature/issuer/audience/expiry/token_use, against the pool's own
     public JWKS) then mapped to a role via resolve_role_from_groups/
     build_user_context (Cognito's "cognito:groups" claim -> "Admins"/
     "Editors" -> "admin"/"editor"); or
  2. The pre-existing shared-secret bearer token, unchanged from v1 --
     kept specifically because 17 scripts under scripts/ (backfills,
     rescrapes, etc.) already authenticate this way, and this feature
     request was about ADDING real user accounts for the new SPA, not
     breaking existing automation that has no "user" to sign in as.

handler() tries the Cognito path first (only when COGNITO_USER_POOL_ID is
actually configured AND the token has a JWT's three-dot-separated shape --
see _looks_like_jwt -- so a bare opaque automation token never even
attempts a JWKS fetch); ANY failure there (not a JWT, bad signature,
expired, wrong pool, or a real Cognito account with no group assigned
yet) falls through to the original shared-secret comparison, not straight
to a 401 -- this is what makes the two modes coexist rather than the
newer one silently taking over. A successful match either way now
returns a `context` object (`{"caller_type", "resolved_by", "role"}`) that
API Gateway forwards to admin_api under `requestContext.authorizer.lambda`
-- see admin_api/app.py's get_caller() for the FastAPI side that reads it
(task #468), letting `resolved_by` be derived automatically from a real
signed-in user instead of always requiring a client-supplied field.

The original shared-secret bearer token doc below is otherwise unchanged
-- that whole mechanism (Secrets Manager storage, comparison logic,
caching) is byte-for-byte the same as before this file had a Cognito
path at all.

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
_JWKS_CLIENT_CACHE = {}


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


def _looks_like_jwt(token):
    """Cheap shape check -- a JWT is always exactly three dot-separated,
    non-empty segments (header.payload.signature). This exists purely so
    the shared-secret automation path never pays for a JWKS-fetch attempt
    on a bare opaque token: an automation token obviously isn't a JWT and
    shouldn't even try, both for speed and so a coincidentally-JWT-shaped
    secret string (unlikely, but not impossible) doesn't get treated as
    an auth mode it was never meant to be."""
    if not token:
        return False
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


def resolve_role_from_groups(groups):
    """Maps a Cognito ID token's "cognito:groups" claim (a list, possibly
    absent/None for an account that exists but was never added to either
    group) to this project's two-role model. "Admins" wins if a user is
    (unusually) in both groups. Returns None -- deliberately not a
    default role like "editor" -- for a user in neither group: a real
    Cognito account with no group assigned must get NO access, the same
    fail-closed posture as everywhere else in this module, not silently
    downgraded to the least-privileged role."""
    groups = groups or []
    if "Admins" in groups:
        return "admin"
    if "Editors" in groups:
        return "editor"
    return None


def build_user_context(claims):
    """Builds the `context` object handler() returns for a successfully
    verified Cognito ID token. Returns None (not a context with some
    placeholder role) when resolve_role_from_groups finds no recognized
    group -- handler() treats that exactly like a failed JWT verification
    and falls through to the shared-secret check, rather than granting an
    ungrouped-but-otherwise-valid account any access at all. `resolved_by`
    prefers the token's own "email" claim (present because AdminUserPool's
    UsernameAttributes is ["email"] and AutoVerifiedAttributes includes
    it) over the opaque "cognito:username" sub-like value, since email is
    what's actually useful to show elsewhere in the admin UI (e.g. as the
    default resolved_by on approve/reject actions -- see admin_api/app.py's
    get_caller())."""
    role = resolve_role_from_groups(claims.get("cognito:groups"))
    if role is None:
        return None
    resolved_by = claims.get("email") or claims.get("cognito:username") or "unknown"
    return {"caller_type": "user", "resolved_by": resolved_by, "role": role}


def _get_jwks_client(user_pool_id, region):
    """Caches one PyJWKClient per (user_pool_id, region) pair for the life
    of the Lambda container -- PyJWKClient itself already caches the
    fetched JWKS in memory, but constructing a fresh client (and losing
    that cache) on every invocation would defeat the point. Same "hand-
    rolled per-container cache, not relying on unverified platform
    behavior" posture as _TOKEN_CACHE above."""
    cache_key = (user_pool_id, region)
    if cache_key not in _JWKS_CLIENT_CACHE:
        from jwt import PyJWKClient

        jwks_url = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/.well-known/jwks.json"
        _JWKS_CLIENT_CACHE[cache_key] = PyJWKClient(jwks_url)
    return _JWKS_CLIENT_CACHE[cache_key]


def decode_cognito_jwt(token, user_pool_id, client_id, region):
    """Verifies signature + issuer + audience + expiry against
    AdminUserPool's own public JWKS, then additionally requires
    token_use == "id" (rejecting a Cognito ACCESS token even though it's
    otherwise a validly-signed token from the same pool -- this project
    standardizes on the SPA sending the ID token specifically, since only
    the ID token carries the "email" claim build_user_context wants for
    resolved_by). Returns the decoded claims dict on success, or None on
    ANY failure: malformed token, bad signature, expired, wrong issuer/
    audience, wrong token_use, or a JWKS-fetch network error. Deliberately
    a single broad except -- this function's job is "is this specific
    token good", not "diagnose exactly why it's bad"; every failure mode
    means the same thing to handler() (try the shared-secret path next),
    so there's no reason to distinguish them here.

    NOT exercised against a real Cognito user pool, a real JWKS endpoint,
    or even the real PyJWT library in this session -- this sandbox has
    neither AWS access nor pip registry access to install PyJWT (see
    src/admin_api_authorizer/requirements.txt's own comment). Same
    disclosed-but-untested status as get_expected_token's boto3 call
    below. resolve_role_from_groups/build_user_context, which consume
    this function's OUTPUT, are fully unit-tested against a hand-built
    fake claims dict -- only the actual jwt.decode/JWKS-fetch call itself
    is unverified."""
    import jwt

    try:
        jwks_client = _get_jwks_client(user_pool_id, region)
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        claims = jwt.decode(
            token, signing_key.key, algorithms=["RS256"],
            audience=client_id, issuer=issuer,
        )
        if claims.get("token_use") != "id":
            return None
        return claims
    except Exception:
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
    """HTTP API v2 REQUEST authorizer, simple-response format. Tries the
    Cognito JWT path FIRST (only when COGNITO_USER_POOL_ID/COGNITO_
    CLIENT_ID/COGNITO_REGION are all configured AND the token has a JWT's
    shape -- see _looks_like_jwt), then falls through to the original
    shared-secret path on any failure there. See the module docstring for
    the full dual-mode reasoning and why both paths still fail closed.
    """
    headers = event.get("headers", {})
    provided_token = extract_bearer_token(_get_header(headers, "authorization"))

    user_pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    client_id = os.environ.get("COGNITO_CLIENT_ID", "")
    region = os.environ.get("COGNITO_REGION", "")
    if user_pool_id and client_id and region and _looks_like_jwt(provided_token):
        claims = decode_cognito_jwt(provided_token, user_pool_id, client_id, region)
        if claims is not None:
            user_context = build_user_context(claims)
            if user_context is not None:
                return {"isAuthorized": True, "context": user_context}
        # ANY failure above (not a real/valid JWT, wrong pool, or a real
        # account with no group assigned yet) falls through to the
        # shared-secret check below rather than denying outright -- this
        # is what lets both auth modes coexist on the same header.

    secret_arn = os.environ.get("ADMIN_API_TOKEN_SECRET_ARN", "")
    if not secret_arn:
        # Not configured yet (template.yaml's AdminApiTokenSecretArn
        # parameter defaults to blank) -- fail closed rather than
        # treating "no secret set up" as "no auth required".
        return {"isAuthorized": False}

    expected_token = get_expected_token(secret_arn)
    if is_valid_token(provided_token, expected_token):
        return {"isAuthorized": True,
                 "context": {"caller_type": "automation", "resolved_by": "automation", "role": "admin"}}
    return {"isAuthorized": False}
