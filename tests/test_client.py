# tests/test_client.py
import pytest
import respx
import httpx
import hashlib

from kabooz import QobuzClient
from kabooz.exceptions import (
    InvalidCredentialsError,
    NoAuthError,
    TokenExpiredError,
    TokenPoolExhaustedError,
    AuthError,
)

def test_from_credentials_creates_unauthenticated_client():
    """
    After from_credentials(), the client exists but has no session.
    The caller must still call login() before making API calls.
    """
    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    assert client.is_authenticated is False
    assert client.session is None


def test_from_credentials_stores_app_id():
    """The App ID should be accessible on the credentials object."""
    client = QobuzClient.from_credentials(app_id="my_app_id", app_secret="secret")
    assert client._credentials.app_id == "my_app_id"


def test_from_credentials_rejects_empty_app_id():
    """
    AppCredentials validates its inputs. Passing an empty string should
    raise a ValueError — we want to catch configuration mistakes early,
    not get mysterious 401s from the API later.
    """
    with pytest.raises(ValueError):
        QobuzClient.from_credentials(app_id="", app_secret="abc")


def test_from_token_pool_string(tmp_path):
    """
    from_token_pool() with a local file should produce an authenticated
    client immediately — no login() call required.

    tmp_path is a pytest built-in fixture that gives you a temporary
    directory that's cleaned up after the test automatically.
    """
    pool_file = tmp_path / "pool.txt"
    pool_file.write_text("123456\nabcsecret\nUSER_TOKEN_ONE\n")

    client = QobuzClient.from_token_pool(pool_file)

    # Should be authenticated immediately
    assert client.is_authenticated is True
    assert client.session is not None
    assert client.session.user_auth_token == "USER_TOKEN_ONE"


def test_from_token_pool_missing_file(tmp_path):
    """Loading a pool from a path that doesn't exist should raise
    TokenPoolLoadError, not a raw FileNotFoundError."""
    from kabooz.exceptions import TokenPoolLoadError
    with pytest.raises(TokenPoolLoadError):
        QobuzClient.from_token_pool(tmp_path / "nonexistent.txt")


def test_client_context_manager():
    """The client should work as a context manager and close cleanly."""
    with QobuzClient.from_credentials(app_id="123", app_secret="abc") as client:
        assert client is not None
    # If close() raised an exception, the test would fail here.
    
    

# A fake API response that mimics what Qobuz returns on successful login.
# Defining it once here means all tests share the same fixture and you
# only need to update it in one place if the shape changes.
FAKE_LOGIN_RESPONSE = {
    "user_auth_token": "FAKE_TOKEN_ABC123",
    "user": {
        "id": 99999,
        "email": "test@example.com",
        "credential": {
            "description": "Studio Premier"
        }
    }
}


@respx.mock
def test_login_with_password_succeeds():
    """
    A successful username+password login should populate the session
    with the token, user_id, email, and subscription from the response.
    """
    respx.post("https://www.qobuz.com/api.json/0.2/user/login").mock(
        return_value=httpx.Response(200, json=FAKE_LOGIN_RESPONSE)
    )

    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    session = client.login(username="test@example.com", password="secret")

    assert session.user_auth_token == "FAKE_TOKEN_ABC123"
    assert session.user_id == "99999"
    assert session.user_email == "test@example.com"
    assert session.subscription == "Studio Premier"
    assert client.is_authenticated is True


@respx.mock
def test_login_sends_password_as_md5():
    """
    The API expects the password as an MD5 hash, not plaintext.
    We verify the outgoing request contains the hashed version.
    """
    request_capture = {}

    def capture(request):
        request_capture["params"] = str(request.url)
        return httpx.Response(200, json=FAKE_LOGIN_RESPONSE)

    respx.post("https://www.qobuz.com/api.json/0.2/user/login").mock(
        side_effect=capture
    )

    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    client.login(username="test@example.com", password="secret")

    expected_hash = hashlib.md5("secret".encode()).hexdigest()
    assert expected_hash in request_capture["params"]


@respx.mock
def test_login_with_wrong_password_raises():
    """A 401 from the login endpoint should raise InvalidCredentialsError."""
    respx.post("https://www.qobuz.com/api.json/0.2/user/login").mock(
        return_value=httpx.Response(401, json={"message": "Wrong password"})
    )

    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    with pytest.raises(InvalidCredentialsError):
        client.login(username="test@example.com", password="wrong")


def test_login_with_token_succeeds():
    """
    login(token=..., user_id=...) should populate the session without
    making any network call at all.
    """
    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    session = client.login(token="MY_TOKEN", user_id="12345")

    assert session.user_auth_token == "MY_TOKEN"
    assert session.user_id == "12345"
    assert client.is_authenticated is True


def test_login_with_token_missing_user_id_raises():
    """Passing a token without a user_id should raise ValueError immediately."""
    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    with pytest.raises(ValueError):
        client.login(token="MY_TOKEN")  # no user_id


def test_login_with_no_args_raises():
    """Calling login() with no arguments should raise ValueError."""
    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    with pytest.raises(ValueError):
        client.login()


def test_logout_clears_session():
    """After logout(), the client should be unauthenticated."""
    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    client.login(token="MY_TOKEN", user_id="12345")
    assert client.is_authenticated is True
    client.logout()
    assert client.is_authenticated is False
    assert client.session is None


def test_rotate_token_advances_to_next(tmp_path):
    """rotate_token() should move to the second token in the pool."""
    pool_file = tmp_path / "pool.txt"
    pool_file.write_text("123\nabc\nFIRST_TOKEN\nSECOND_TOKEN\n")

    client = QobuzClient.from_token_pool(pool_file)
    assert client.session.user_auth_token == "FIRST_TOKEN"

    client.rotate_token()
    assert client.session.user_auth_token == "SECOND_TOKEN"


def test_rotate_token_exhausted_raises(tmp_path):
    """When the pool has only one token and it fails, rotating should
    raise TokenPoolExhaustedError."""
    pool_file = tmp_path / "pool.txt"
    pool_file.write_text("123\nabc\nONLY_TOKEN\n")

    client = QobuzClient.from_token_pool(pool_file)
    with pytest.raises(TokenPoolExhaustedError):
        client.rotate_token()


def test_rotate_token_without_pool_raises():
    """rotate_token() on a client built with from_credentials() should
    raise AuthError since there's no pool to rotate."""
    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    client.login(token="MY_TOKEN", user_id="12345")
    with pytest.raises(AuthError):
        client.rotate_token()


def test_unauthenticated_request_raises_no_auth_error():
    """Calling a method that requires auth before login() should raise
    NoAuthError, not a cryptic httpx error."""
    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    with pytest.raises(NoAuthError):
        client._request("GET", "/track/get", params={"track_id": "123"})    
