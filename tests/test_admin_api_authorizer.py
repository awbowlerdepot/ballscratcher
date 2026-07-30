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
    assert app.handler(event, None) == {"isAuthorized": True}


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
