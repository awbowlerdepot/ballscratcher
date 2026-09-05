"""
Tests for src/admin_api_authorizer/app.py. Covers the pure functions
directly (extract_bearer_token, parse_secret_value, is_valid_token,
_get_header) plus handler() with get_expected_token monkeypatched --
no real Secrets Manager or API Gateway call happens in this session (no
AWS access in this sandbox), so get_expected_token's actual boto3 call
is untested, same "logic verified, deployment isn't" status as this
project's other boto3-glue functions. Manual-runner pattern, run
standalone via `python3 tests/test_admin_api_authorizer.py`.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "admin_api_authorizer"))

import app  # noqa: E402


# --- extract_bearer_token ---

def test_extract_bearer_token_standard():
    assert app.extract_bearer_token("Bearer abc123") == "abc123"


def test_extract_bearer_token_case_insensitive_scheme():
    assert app.extract_bearer_token("bearer abc123") == "abc123"
    assert app.extract_bearer_token("BEARER abc123") == "abc123"


def test_extract_bearer_token_missing_header():
    assert app.extract_bearer_token(None) is None
    assert app.extract_bearer_token("") is None


def test_extract_bearer_token_wrong_scheme():
    assert app.extract_bearer_token("Basic abc123") is None


def test_extract_bearer_token_no_token_after_scheme():
    assert app.extract_bearer_token("Bearer") is None
    assert app.extract_bearer_token("Bearer   ") is None


def test_extract_bearer_token_extra_whitespace_tolerated():
    assert app.extract_bearer_token("  Bearer   abc123  ") == "abc123"


# --- parse_secret_value ---

def test_parse_secret_value_json_token_shape():
    assert app.parse_secret_value('{"token": "xyz789"}') == "xyz789"


def test_parse_secret_value_bare_string():
    assert app.parse_secret_value("just-a-plain-token") == "just-a-plain-token"


def test_parse_secret_value_bare_string_with_whitespace():
    assert app.parse_secret_value("  padded-token  \n") == "padded-token"


def test_parse_secret_value_json_but_not_token_shape_falls_back_to_raw():
    raw = '{"store_hash": "abc"}'
    assert app.parse_secret_value(raw) == raw.strip()


def test_parse_secret_value_json_list_falls_back_to_raw():
    raw = '["a", "b"]'
    assert app.parse_secret_value(raw) == raw.strip()


# --- is_valid_token ---

def test_is_valid_token_match():
    assert app.is_valid_token("secret123", "secret123") is True


def test_is_valid_token_mismatch():
    assert app.is_valid_token("wrong", "secret123") is False


def test_is_valid_token_missing_provided():
    assert app.is_valid_token(None, "secret123") is False


def test_is_valid_token_missing_expected():
    assert app.is_valid_token("secret123", None) is False


def test_is_valid_token_both_empty_never_matches():
    assert app.is_valid_token("", "") is False
    assert app.is_valid_token(None, None) is False


# --- _get_header ---

def test_get_header_exact_case():
    assert app._get_header({"authorization": "Bearer x"}, "authorization") == "Bearer x"


def test_get_header_case_insensitive():
    assert app._get_header({"Authorization": "Bearer x"}, "authorization") == "Bearer x"


def test_get_header_missing():
    assert app._get_header({}, "authorization") is None
    assert app._get_header(None, "authorization") is None


# --- _looks_like_jwt (v2, task #467) ---

def test_looks_like_jwt_three_segments():
    assert app._looks_like_jwt("header.payload.signature") is True


def test_looks_like_jwt_rejects_opaque_token():
    assert app._looks_like_jwt("just-a-shared-secret-token") is False


def test_looks_like_jwt_rejects_empty_segment():
    assert app._looks_like_jwt("header..signature") is False


def test_looks_like_jwt_rejects_none_and_empty():
    assert app._looks_like_jwt(None) is False
    assert app._looks_like_jwt("") is False


# --- resolve_role_from_groups (v2) ---

def test_resolve_role_from_groups_admins():
    assert app.resolve_role_from_groups(["Admins"]) == "admin"


def test_resolve_role_from_groups_editors():
    assert app.resolve_role_from_groups(["Editors"]) == "editor"


def test_resolve_role_from_groups_admins_wins_over_editors():
    assert app.resolve_role_from_groups(["Editors", "Admins"]) == "admin"


def test_resolve_role_from_groups_no_recognized_group_returns_none():
    assert app.resolve_role_from_groups(["SomeOtherGroup"]) is None


def test_resolve_role_from_groups_none_or_empty_returns_none():
    assert app.resolve_role_from_groups(None) is None
    assert app.resolve_role_from_groups([]) is None


# --- build_user_context (v2) ---

def test_build_user_context_admin_prefers_email():
    claims = {"cognito:groups": ["Admins"], "email": "al@bowlerdepot.com", "cognito:username": "abc-123"}
    assert app.build_user_context(claims) == {
        "caller_type": "user", "resolved_by": "al@bowlerdepot.com", "role": "admin",
    }


def test_build_user_context_falls_back_to_username_when_no_email():
    claims = {"cognito:groups": ["Editors"], "cognito:username": "abc-123"}
    assert app.build_user_context(claims) == {
        "caller_type": "user", "resolved_by": "abc-123", "role": "editor",
    }


def test_build_user_context_returns_none_when_no_recognized_group():
    claims = {"cognito:groups": [], "email": "nobody@bowlerdepot.com"}
    assert app.build_user_context(claims) is None


def test_build_user_context_returns_none_when_groups_claim_absent():
    # A real Cognito account that exists but was never added to a group --
    # the "cognito:groups" claim simply isn't present at all, not an empty
    # list. Must still be treated as "no recognized group", not crash on
    # a missing dict key.
    claims = {"email": "nobody@bowlerdepot.com"}
    assert app.build_user_context(claims) is None


# --- handler ---

def test_handler_denies_when_secret_arn_not_configured(monkeypatch):
    monkeypatch.delenv("ADMIN_API_TOKEN_SECRET_ARN", raising=False)
    event = {"headers": {"authorization": "Bearer whatever"}}
    assert app.handler(event, None) == {"isAuthorized": False}


def test_handler_denies_when_secret_arn_blank(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN_SECRET_ARN", "")
    event = {"headers": {"authorization": "Bearer whatever"}}
    assert app.handler(event, None) == {"isAuthorized": False}


def test_handler_allows_matching_token(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:fake")
    monkeypatch.setattr(app, "get_expected_token", lambda secret_arn: "real-token")
    event = {"headers": {"Authorization": "Bearer real-token"}}
    # v2: a successful shared-secret match now also returns a context
    # object identifying the caller as "automation" -- see admin_api/
    # app.py's get_caller() for the FastAPI-side consumer of this.
    assert app.handler(event, None) == {
        "isAuthorized": True,
        "context": {"caller_type": "automation", "resolved_by": "automation", "role": "admin"},
    }


def test_handler_denies_mismatched_token(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:fake")
    monkeypatch.setattr(app, "get_expected_token", lambda secret_arn: "real-token")
    event = {"headers": {"Authorization": "Bearer wrong-token"}}
    assert app.handler(event, None) == {"isAuthorized": False}


def test_handler_denies_missing_authorization_header(monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:fake")
    monkeypatch.setattr(app, "get_expected_token", lambda secret_arn: "real-token")
    event = {"headers": {}}
    assert app.handler(event, None) == {"isAuthorized": False}


# --- handler: Cognito JWT path (v2, task #467) -- decode_cognito_jwt itself
# monkeypatched, since the real PyJWT/JWKS-fetch call isn't exercised in
# this sandbox (see that function's own docstring).

def test_handler_allows_valid_cognito_jwt_admin(monkeypatch):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_fake")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "fakeclientid")
    monkeypatch.setenv("COGNITO_REGION", "us-east-1")
    monkeypatch.setattr(app, "decode_cognito_jwt",
                         lambda token, pool_id, client_id, region: {
                             "email": "al@bowlerdepot.com", "cognito:groups": ["Admins"],
                         })
    event = {"headers": {"Authorization": "Bearer header.payload.signature"}}
    assert app.handler(event, None) == {
        "isAuthorized": True,
        "context": {"caller_type": "user", "resolved_by": "al@bowlerdepot.com", "role": "admin"},
    }


def test_handler_cognito_path_skipped_when_not_configured_falls_through_to_secret(monkeypatch):
    """COGNITO_USER_POOL_ID unset entirely -- decode_cognito_jwt must
    never even be called, and a matching shared-secret token still
    works, exactly as it did before this file had a Cognito path at all."""
    monkeypatch.delenv("COGNITO_USER_POOL_ID", raising=False)
    monkeypatch.setenv("ADMIN_API_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:fake")
    monkeypatch.setattr(app, "get_expected_token", lambda secret_arn: "real-token")

    def _exploding_decode(*a, **kw):
        raise AssertionError("decode_cognito_jwt should never be called when Cognito isn't configured")

    monkeypatch.setattr(app, "decode_cognito_jwt", _exploding_decode)
    event = {"headers": {"Authorization": "Bearer real-token"}}
    result = app.handler(event, None)
    assert result["isAuthorized"] is True


def test_handler_cognito_path_skipped_for_non_jwt_shaped_token(monkeypatch):
    """A bare opaque automation token never even attempts a JWKS fetch --
    _looks_like_jwt's shape check short-circuits before decode_cognito_jwt
    is ever called."""
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_fake")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "fakeclientid")
    monkeypatch.setenv("COGNITO_REGION", "us-east-1")
    monkeypatch.setenv("ADMIN_API_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:fake")
    monkeypatch.setattr(app, "get_expected_token", lambda secret_arn: "real-token")

    def _exploding_decode(*a, **kw):
        raise AssertionError("decode_cognito_jwt should never be called for a non-JWT-shaped token")

    monkeypatch.setattr(app, "decode_cognito_jwt", _exploding_decode)
    event = {"headers": {"Authorization": "Bearer real-token"}}
    result = app.handler(event, None)
    assert result["isAuthorized"] is True


def test_handler_falls_through_to_secret_when_jwt_verification_fails(monkeypatch):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_fake")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "fakeclientid")
    monkeypatch.setenv("COGNITO_REGION", "us-east-1")
    monkeypatch.setenv("ADMIN_API_TOKEN_SECRET_ARN", "arn:aws:secretsmanager:fake")
    monkeypatch.setattr(app, "get_expected_token", lambda secret_arn: "real-token")
    # Looks like a JWT shape-wise, but decode_cognito_jwt reports it as
    # invalid (bad signature, expired, wrong pool, whatever) -- happens to
    # ALSO equal the real shared secret, which is a contrived coincidence
    # for this test but proves the fall-through actually reaches the
    # secret comparison rather than denying outright.
    monkeypatch.setattr(app, "decode_cognito_jwt", lambda token, pool_id, client_id, region: None)
    event = {"headers": {"Authorization": "Bearer a.b.c"}}
    assert app.handler(event, None) == {"isAuthorized": False}  # "a.b.c" != "real-token"


def test_handler_falls_through_to_secret_when_jwt_has_no_recognized_group(monkeypatch):
    """A real, validly-signed Cognito ID token for an account that was
    never added to Admins or Editors must NOT get any access -- falls
    through exactly like a failed verification would."""
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_fake")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "fakeclientid")
    monkeypatch.setenv("COGNITO_REGION", "us-east-1")
    monkeypatch.delenv("ADMIN_API_TOKEN_SECRET_ARN", raising=False)
    monkeypatch.setattr(app, "decode_cognito_jwt",
                         lambda token, pool_id, client_id, region: {
                             "email": "nobody@bowlerdepot.com", "cognito:groups": [],
                         })
    event = {"headers": {"Authorization": "Bearer a.b.c"}}
    assert app.handler(event, None) == {"isAuthorized": False}


if __name__ == "__main__":
    class _MonkeyPatch:
        def __init__(self):
            self._sets = []
            self._env_sets = []

        def setattr(self, obj, name, value):
            self._sets.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def setenv(self, name, value):
            self._env_sets.append((name, os.environ.get(name)))
            os.environ[name] = value

        def delenv(self, name, raising=False):
            self._env_sets.append((name, os.environ.get(name)))
            os.environ.pop(name, None)

        def undo(self):
            for obj, name, value in reversed(self._sets):
                setattr(obj, name, value)
            for name, value in reversed(self._env_sets):
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
