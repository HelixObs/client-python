"""Tests for _TokenProvider — callable credential support, caching, and refresh."""

import json
import time
import unittest.mock as mock
from io import BytesIO

import pytest

from helixobs.instrument import _TokenProvider


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_response(token="test.jwt.token", expires_in=86400):
    """Return a mock object that behaves like urllib.request.urlopen()'s context manager."""
    body = json.dumps({"token": token, "expires_in": expires_in}).encode()
    cm = mock.MagicMock()
    cm.read.return_value = body
    cm.__enter__ = lambda s: s
    cm.__exit__ = mock.MagicMock(return_value=False)
    return cm


def _make_provider(credential, *, token="tok", expires_in=86400):
    """Build a _TokenProvider with a mocked urlopen. Returns (provider, urlopen_mock)."""
    response = _mock_response(token=token, expires_in=expires_in)
    with mock.patch("urllib.request.urlopen", return_value=response) as m:
        provider = _TokenProvider(
            auth_endpoint="https://herald.example.com/auth/token",
            instrument_id="TESTINST",
            credential=credential,
        )
    return provider, m


# ── Static credential ─────────────────────────────────────────────────────────

class TestStaticCredential:
    def test_token_returned_after_init(self):
        response = _mock_response(token="jwt-abc")
        with mock.patch("urllib.request.urlopen", return_value=response):
            p = _TokenProvider("https://gw/auth/token", "INST", "my-secret")
        assert p.token() == "jwt-abc"

    def test_static_credential_sent_in_post_body(self):
        response = _mock_response()
        with mock.patch("urllib.request.urlopen", return_value=response) as m:
            _TokenProvider("https://gw/auth/token", "INST", "my-secret")

        call_args = m.call_args
        req = call_args[0][0]  # urllib.request.Request object
        payload = json.loads(req.data.decode())
        assert payload["credential"] == "my-secret"
        assert payload["instrument_id"] == "INST"

    def test_static_credential_posts_to_correct_endpoint(self):
        response = _mock_response()
        with mock.patch("urllib.request.urlopen", return_value=response) as m:
            _TokenProvider("https://gw/auth/token", "INST", "secret")

        req = m.call_args[0][0]
        assert req.full_url == "https://gw/auth/token"
        assert req.method == "POST"
        assert req.get_header("Content-type") == "application/json"


# ── Callable credential ───────────────────────────────────────────────────────

class TestCallableCredential:
    def test_callable_invoked_during_init(self):
        getter = mock.MagicMock(return_value="dynamic-secret")
        response = _mock_response()
        with mock.patch("urllib.request.urlopen", return_value=response):
            _TokenProvider("https://gw/auth/token", "INST", getter)

        getter.assert_called_once()

    def test_callable_return_value_sent_in_post_body(self):
        getter = mock.MagicMock(return_value="dynamic-secret")
        response = _mock_response()
        with mock.patch("urllib.request.urlopen", return_value=response) as m:
            _TokenProvider("https://gw/auth/token", "INST", getter)

        payload = json.loads(m.call_args[0][0].data.decode())
        assert payload["credential"] == "dynamic-secret"

    def test_callable_called_fresh_on_each_refresh(self):
        """Callable must be invoked on every _refresh() so expiring credentials are renewed."""
        call_count = 0
        tokens = ["first-token", "second-token"]

        def getter():
            nonlocal call_count
            val = tokens[call_count % len(tokens)]
            call_count += 1
            return val

        responses = [_mock_response(token="jwt-1"), _mock_response(token="jwt-2")]
        with mock.patch("urllib.request.urlopen", side_effect=responses):
            p = _TokenProvider("https://gw/auth/token", "INST", getter)
            # Force expiry so next token() triggers refresh.
            p._expires_at = time.monotonic() - 1
            p.token()

        assert call_count == 2, "callable should be invoked once at init and once on refresh"

    def test_static_string_not_called(self):
        """Plain strings must not be called as functions."""
        response = _mock_response()
        with mock.patch("urllib.request.urlopen", return_value=response):
            p = _TokenProvider("https://gw/auth/token", "INST", "plain-string")
        # If _TokenProvider incorrectly called a string, a TypeError would be raised above.
        assert p.token() is not None


# ── Token caching and refresh ─────────────────────────────────────────────────

class TestTokenCaching:
    def test_token_not_refreshed_while_valid(self):
        """token() must not re-fetch while well within the validity window."""
        response = _mock_response(token="cached-jwt", expires_in=86400)
        with mock.patch("urllib.request.urlopen", return_value=response) as m:
            p = _TokenProvider("https://gw/auth/token", "INST", "secret")
            _ = p.token()
            _ = p.token()
            _ = p.token()

        # urlopen should have been called exactly once (at __init__).
        assert m.call_count == 1

    def test_token_refreshed_when_near_expiry(self):
        """token() must call _refresh() when within 1 hour of token expiry."""
        responses = [_mock_response(token="old-jwt"), _mock_response(token="new-jwt")]
        with mock.patch("urllib.request.urlopen", side_effect=responses):
            p = _TokenProvider("https://gw/auth/token", "INST", "secret")
            # Set expiry to 3500 s from now — inside the 1-hour refresh window.
            p._expires_at = time.monotonic() + 3500
            refreshed = p.token()

        assert refreshed == "new-jwt"

    def test_token_not_refreshed_outside_window(self):
        """token() must NOT refresh when expiry is more than 1 hour away."""
        responses = [_mock_response(token="valid-jwt"), _mock_response(token="should-not-use")]
        with mock.patch("urllib.request.urlopen", side_effect=responses):
            p = _TokenProvider("https://gw/auth/token", "INST", "secret")
            p._expires_at = time.monotonic() + 7200  # 2 hours away — no refresh
            result = p.token()

        assert result == "valid-jwt"

    def test_expires_at_set_from_response(self):
        """_expires_at must be derived from the expires_in field in the auth response."""
        before = time.monotonic()
        response = _mock_response(expires_in=3600)
        with mock.patch("urllib.request.urlopen", return_value=response):
            p = _TokenProvider("https://gw/auth/token", "INST", "secret")
        after = time.monotonic()

        # _expires_at should be approximately before + 3600.
        assert before + 3599 <= p._expires_at <= after + 3601


# ── Failure at init ───────────────────────────────────────────────────────────

class TestTokenProviderInitFailure:
    def test_raises_if_auth_endpoint_unreachable(self):
        """_TokenProvider must fail fast at init if the herald is unreachable."""
        import urllib.error
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with pytest.raises(urllib.error.URLError):
                _TokenProvider("https://unreachable/auth/token", "INST", "secret")
