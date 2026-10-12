"""d360 setup routes: GET status is read-only; sends live on POST only."""
import os
import pytest
from fastapi.testclient import TestClient
import app as appmod
import d360_wa


class _Resp:
    def __init__(self, status=200, text="{}"):
        self.status_code = status
        self.text = text


class _FakeHTTP:
    calls = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        _FakeHTTP.calls.append(("GET", url))
        return _Resp(200, '{"webhook":"ok"}')

    async def post(self, url, headers=None, json=None):
        _FakeHTTP.calls.append(("POST", url, json))
        return _Resp(200, '{"sent":true}')


@pytest.fixture()
def client(monkeypatch):
    _FakeHTTP.calls = []
    monkeypatch.setenv("ADMIN_TOKEN", "admintok")
    monkeypatch.setenv("D360_API_KEY", "d360key123")
    monkeypatch.setattr(d360_wa.httpx, "AsyncClient", _FakeHTTP)
    return TestClient(appmod.app)


def test_status_requires_admin_token(client):
    assert client.get("/setup/d360-status").status_code == 403


def test_status_get_never_sends(client):
    r = client.get("/setup/d360-status?to=%2B18775556666",
                   headers={"x-admin-token": "admintok"})
    assert r.status_code == 200
    assert not [c for c in _FakeHTTP.calls if c[0] == "POST"]


def test_test_send_requires_admin_token(client):
    r = client.post("/setup/d360-test-send", json={"to": "+18775556666"})
    assert r.status_code == 403
    assert not _FakeHTTP.calls


def test_test_send_validates_recipient(client):
    for bad in ("", "abc", "+1", "18775556666 x", "+1877555DROP"):
        r = client.post("/setup/d360-test-send", json={"to": bad},
                        headers={"x-admin-token": "admintok"})
        assert r.status_code == 400, bad
    assert not _FakeHTTP.calls


def test_test_send_posts_to_provider(client):
    r = client.post("/setup/d360-test-send", json={"to": "+18775556666"},
                    headers={"x-admin-token": "admintok"})
    assert r.status_code == 200
    posts = [c for c in _FakeHTTP.calls if c[0] == "POST"]
    assert len(posts) == 1 and posts[0][1].endswith("/v1/messages")
    assert posts[0][2]["to"] == "+18775556666"


def test_test_send_without_key_503(client, monkeypatch):
    monkeypatch.delenv("D360_API_KEY")
    r = client.post("/setup/d360-test-send", json={"to": "+18775556666"},
                    headers={"x-admin-token": "admintok"})
    assert r.status_code == 503
    assert not _FakeHTTP.calls


def test_test_send_rejects_non_object_body(client):
    for bad in (["x"], "x", 1, True):
        r = client.post("/setup/d360-test-send", json=bad,
                        headers={"x-admin-token": "admintok"})
        assert r.status_code == 400, repr(bad)
    assert not _FakeHTTP.calls


def test_test_send_rejects_non_e164_shapes(client):
    for bad in ("00000000", "+012345678", 12345678, "+1234567890123456", True):
        r = client.post("/setup/d360-test-send", json={"to": bad},
                        headers={"x-admin-token": "admintok"})
        assert r.status_code == 400, repr(bad)
    assert not _FakeHTTP.calls


def test_test_send_accepts_digits_only_without_plus(client):
    r = client.post("/setup/d360-test-send", json={"to": "18775556666"},
                    headers={"x-admin-token": "admintok"})
    assert r.status_code == 200
    posts = [c for c in _FakeHTTP.calls if c[0] == "POST"]
    assert len(posts) == 1 and posts[0][2]["to"] == "18775556666"
