# tests/test_session_persistence.py
import json
import pytest
from kabooz import QobuzClient
from kabooz.exceptions import NoAuthError


def make_client() -> QobuzClient:
    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    client.login(token="MY_TOKEN", user_id="42")
    return client


# ── save_session ───────────────────────────────────────────────────────────

def test_save_session_writes_json(tmp_path):
    client = make_client()
    dest = tmp_path / "session.json"
    client.save_session(dest)
    assert dest.exists()
    data = json.loads(dest.read_text())
    assert data["user_auth_token"] == "MY_TOKEN"
    assert data["user_id"] == "42"


def test_save_session_creates_parent_dirs(tmp_path):
    client = make_client()
    dest = tmp_path / "a" / "b" / "session.json"
    client.save_session(dest)
    assert dest.exists()


def test_save_session_overwrites_existing(tmp_path):
    dest = tmp_path / "session.json"
    dest.write_text('{"stale": true}')

    client = make_client()
    client.save_session(dest)

    data = json.loads(dest.read_text())
    assert "user_auth_token" in data
    assert "stale" not in data


def test_save_session_without_auth_raises(tmp_path):
    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    with pytest.raises(NoAuthError):
        client.save_session(tmp_path / "session.json")


# ── load_session ───────────────────────────────────────────────────────────

def test_load_session_restores_token(tmp_path):
    dest = tmp_path / "session.json"
    dest.write_text(json.dumps({
        "user_auth_token": "RESTORED_TOKEN",
        "user_id": "99",
        "issued_at": 1700000000.0,
        "user_email": "test@example.com",
        "subscription": "Studio Premier",
    }))

    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    session = client.load_session(dest)

    assert session.user_auth_token == "RESTORED_TOKEN"
    assert session.user_id == "99"
    assert session.user_email == "test@example.com"
    assert client.session is session


def test_load_session_sets_active_session(tmp_path):
    dest = tmp_path / "session.json"
    dest.write_text(json.dumps({
        "user_auth_token": "TOKEN",
        "user_id": "1",
        "issued_at": 1700000000.0,
    }))
    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    assert not client.is_authenticated
    client.load_session(dest)
    assert client.is_authenticated


def test_load_session_missing_file_raises(tmp_path):
    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    with pytest.raises(FileNotFoundError):
        client.load_session(tmp_path / "nonexistent.json")


# ── round-trip ─────────────────────────────────────────────────────────────

def test_save_and_load_round_trip(tmp_path):
    dest = tmp_path / "session.json"

    original = make_client()
    original.session.user_email = "me@example.com"
    original.session.subscription = "Studio Premier"
    original.save_session(dest)

    restored = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    restored.load_session(dest)

    assert restored.session.user_auth_token == original.session.user_auth_token
    assert restored.session.user_id == original.session.user_id
    assert restored.session.user_email == original.session.user_email
    assert restored.session.subscription == original.session.subscription

