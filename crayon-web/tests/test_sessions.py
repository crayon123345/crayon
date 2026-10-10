"""Session-cache privacy tests: cache keys must be bound to the visitor identity,
and first-time visitors must not 500 (identity() must be called once per request)."""
import os
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("NEON_DATABASE_URL", None)
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest-only")

from fastapi.testclient import TestClient
import app as appmod
import memory
import gmail_auth


def _post(client, sid):
    return client.post("/api/chat", json={"message": "hi", "session_id": sid})


def test_first_time_visitor_does_not_500():
    """Regression: an unsigned first-time visitor used to get two different vids
    (identity() called twice) which tripped the key assertion -> 500."""
    c = TestClient(appmod.app, base_url="https://testserver")
    r = _post(c, "first-visit")
    assert r.status_code == 200, r.text
    assert "crayon_vid" in r.headers.get("set-cookie", "")


def test_same_sid_different_visitors_isolated():
    """Two different visitors sharing a session_id must get separate cache entries."""
    appmod.sessions.clear()
    c1, c2 = TestClient(appmod.app, base_url="https://testserver"), TestClient(appmod.app, base_url="https://testserver")
    r1 = _post(c1, "shared"); assert r1.status_code == 200
    r1b = _post(c1, "shared"); assert r1b.status_code == 200  # cookie preserved -> same key
    r2 = _post(c2, "shared"); assert r2.status_code == 200
    keys = [k for k in appmod.sessions if k.endswith(":shared")]
    assert len(keys) == 2, f"expected 2 visitor-bound keys, got {keys}"
    assert all(k.startswith("v_") for k in keys)


def test_signed_cookie_visitor_stable_key():
    """A returning visitor with a valid signed vid cookie keeps one stable cache key."""
    appmod.sessions.clear()
    vid = "v_" + "ab" * 16
    c = TestClient(appmod.app, base_url="https://testserver", cookies={memory.VID_COOKIE: gmail_auth._sign(vid)})
    assert _post(c, "ret").status_code == 200
    assert _post(c, "ret").status_code == 200
    keys = [k for k in appmod.sessions if k.endswith(":ret")]
    assert keys == [f"{vid}:ret"], keys


def test_forged_cookie_gets_fresh_vid_not_forged_one():
    """An unsigned/forged vid cookie must be replaced, never trusted as an identity."""
    appmod.sessions.clear()
    forged = "v_" + "ff" * 16
    c = TestClient(appmod.app, base_url="https://testserver", cookies={memory.VID_COOKIE: forged})
    r = _post(c, "forge"); assert r.status_code == 200
    keys = [k for k in appmod.sessions if k.endswith(":forge")]
    assert len(keys) == 1 and not keys[0].startswith(forged + ":"), keys
