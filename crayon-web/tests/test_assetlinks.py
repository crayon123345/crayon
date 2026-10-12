"""assetlinks.json: Digital Asset Links statement for the TWA package."""
import json
import pytest
from fastapi.testclient import TestClient
import app as appmod

V3 = "FE:00:73:3A:71:01:AC:67:7C:ED:E9:D2:93:CD:90:8F:5C:58:F7:1D:B3:EC:87:4E:1A:30:FF:C2:22:F5:01:71"
V1 = "33:A5:EA:5A:82:3E:F7:1C:EE:08:45:7D:89:93:93:52:1B:F7:FC:12:91:56:98:7F:9F:D3:A3:31:42:98:66:75"


@pytest.fixture()
def client():
    return TestClient(appmod.app)


def test_served_as_json(client):
    r = client.get("/.well-known/assetlinks.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    json.loads(r.text)


def test_statement_shape(client):
    data = client.get("/.well-known/assetlinks.json").json()
    assert isinstance(data, list) and len(data) == 1
    stmt = data[0]
    assert stmt["relation"] == ["delegate_permission/common.handle_all_urls"]
    t = stmt["target"]
    assert t["namespace"] == "android_app"
    assert t["package_name"] == "app.crayonai.twa"


def test_both_signing_fingerprints_listed(client):
    fps = client.get("/.well-known/assetlinks.json").json()[0]["target"]["sha256_cert_fingerprints"]
    # v3 (current v1.1.0 build) and v1 (published GitHub v1.0.0 release APK)
    assert V3 in fps
    assert V1 in fps
