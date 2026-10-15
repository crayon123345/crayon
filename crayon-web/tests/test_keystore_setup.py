"""POST /setup/key: attempt cap and no partial mutation on NEON repoint rejection."""
import os
import pytest
from fastapi.testclient import TestClient
import app as appmod
import keystore
import gmail_auth


class _FakeFernet:
    def encrypt(self, b):
        return b


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("SETUP_TOKEN", "setuptok")
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    keystore._tries["n"] = 0
    calls = []
    monkeypatch.setattr(gmail_auth, "db", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(gmail_auth, "_fernet", lambda: _FakeFernet())
    yield TestClient(appmod.app), calls
    keystore._tries["n"] = 0


def _post(client, **kw):
    body = {"t": "setuptok", "n": "OPENROUTER_API_KEY", "k": "sk-or-" + "a" * 40}
    body.update(kw)
    return client.post("/setup/key", json=body, headers={"x-requested-with": "crayon"})


def test_wrong_header_403(client):
    c, calls = client
    r = c.post("/setup/key", json={"t": "setuptok", "n": "OPENROUTER_API_KEY", "k": "sk-or-" + "a" * 40})
    assert r.status_code == 403
    assert calls == []


def test_attempt_cap_is_20_not_21(client):
    c, calls = client
    keystore._tries["n"] = 19
    assert _post(c).status_code == 200  # attempt #20 allowed
    keystore._tries["n"] = 20
    assert _post(c).status_code == 403  # attempt #21 refused


def test_success_stores_secret_and_pops_setup_token(client, monkeypatch):
    c, calls = client
    r = _post(c)
    assert r.status_code == 200
    assert any("INSERT INTO cw_secrets" in str(a[0]) for a in calls)
    assert os.environ.get("OPENROUTER_API_KEY") == "sk-or-" + "a" * 40
    assert "SETUP_TOKEN" not in os.environ


def test_neon_rejection_leaves_no_partial_mutation(client, monkeypatch):
    import sys, types
    c, calls = client
    monkeypatch.setenv("SETUP_TOKEN", "setuptok")  # restore after prior pop (fixture order)
    # Offline: psycopg.connect is mocked to fail, no network involved.
    def _boom(*a, **k):
        raise OSError("mocked: connection refused")
    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=_boom))
    bad_url = "postgresql://u:p@db.example.com:5432/db"  # matches ALLOWED regex
    r = _post(c, n="NEON_DATABASE_URL", k=bad_url)
    assert r.status_code == 400
    # no _init / upsert ran, env not mutated, setup token still active
    assert calls == []
    assert "NEON_DATABASE_URL" not in os.environ
    assert os.environ.get("SETUP_TOKEN") == "setuptok"


def test_store_is_single_atomic_upsert(client, monkeypatch):
    """An upsert failure must not lose the OLD secret: no separate DELETE."""
    c, calls = client

    def flaky_db(sql, args=(), fetch=False):
        calls.append((sql, args))
        if "ON CONFLICT" in sql:
            raise RuntimeError("mocked: write failed")

    monkeypatch.setattr(gmail_auth, "db", flaky_db)
    with pytest.raises(RuntimeError):
        _post(c)
    assert not any("DELETE FROM cw_secrets" in s for s, _ in calls)
    writes = [s for s, _ in calls if "INSERT INTO cw_secrets" in s]
    assert len(writes) == 1  # exactly one write statement, atomic by itself


def test_unicode_setup_token_fails_closed_not_500(client):
    c, calls = client
    r = _post(c, t="sétuptok-ü")
    assert r.status_code == 403
    assert calls == []
