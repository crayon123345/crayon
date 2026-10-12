"""d360 inbound webhook: fail-closed when the lane is enabled without a hook secret."""
import pytest
from fastapi.testclient import TestClient
import app as appmod
import d360_wa


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("D360_API_KEY", "d360key123")
    return TestClient(appmod.app)


def _msg_body():
    return {"entry": [{"changes": [{"value": {"messages": [{"id": "wamid.1", "type": "text", "from": "15551234567", "text": {"body": "hi"}}]}}]}]}


def test_disabled_lane_404(client, monkeypatch):
    monkeypatch.delenv("D360_API_KEY")
    assert client.post("/whatsapp/d360", json={}).status_code == 404


def test_enabled_lane_without_secret_fails_closed(client, monkeypatch):
    monkeypatch.delenv("D360_HOOK_SECRET", raising=False)
    called = []
    monkeypatch.setattr(d360_wa, "handle", lambda *a: called.append(a))
    r = client.post("/whatsapp/d360", json=_msg_body())
    assert r.status_code == 503
    assert called == []


def test_wrong_secret_403_no_handling(client, monkeypatch):
    monkeypatch.setenv("D360_HOOK_SECRET", "s3cret")
    called = []
    monkeypatch.setattr(d360_wa, "handle", lambda *a: called.append(a))
    assert client.post("/whatsapp/d360?s=wrong", json=_msg_body()).status_code == 403
    assert client.post("/whatsapp/d360", json=_msg_body()).status_code == 403
    assert called == []


def test_correct_secret_reaches_handler(client, monkeypatch):
    monkeypatch.setenv("D360_HOOK_SECRET", "s3cret")
    seen = []

    async def fake_handle(wa_id, text):
        seen.append((wa_id, text))

    monkeypatch.setattr(d360_wa, "handle", fake_handle)
    r = client.post("/whatsapp/d360?s=s3cret", json=_msg_body())
    assert r.status_code == 200
    assert seen == [("15551234567", "hi")]
