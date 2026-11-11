from fastapi.testclient import TestClient

from instalador_music import web

client = TestClient(web.app)
H = {"X-Token": web.TOKEN}


def test_index_injects_token():
    r = client.get("/")
    assert r.status_code == 200 and web.TOKEN in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_api_requires_token():
    assert client.get("/api/state").status_code == 403
    assert client.get("/api/state", headers=H).status_code == 200


def test_rejects_system_paths():
    r = client.get("/api/destination", params={"path": "/etc"}, headers=H)
    assert r.json()["ok"] is False
    r = client.post("/api/library/trash", json={"path": "/etc", "files": ["/etc/passwd"]}, headers=H)
    assert r.status_code == 400


def test_trash_outside_library_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    lib = tmp_path / "lib"
    lib.mkdir()
    r = client.post("/api/library/trash", json={"path": str(lib), "files": [str(tmp_path / "x.mp3")]},
                    headers=H)
    assert r.status_code == 400


def test_analyze_endpoint():
    r = client.post("/api/links/analyze", json={"text": "https://youtu.be/qVdPh2cBTN0?si=a"}, headers=H)
    assert r.json()[0]["url"] == "https://www.youtube.com/watch?v=qVdPh2cBTN0"
