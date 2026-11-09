from fastapi.testclient import TestClient
import pytest

from backend.api import app
from backend import api


@pytest.fixture(autouse=True)
def isolated_api(tmp_path, monkeypatch):
    from backend import vector_store
    from backend.config import Settings

    monkeypatch.setattr(Settings, "VECTORAI_DB_PATH", str(tmp_path / "db"))
    monkeypatch.setattr(vector_store, "_db_instance", None)
    monkeypatch.setattr(api, "SIDECAR_FILE", str(tmp_path / "last_repo.json"))
    monkeypatch.setattr(api, "indexers", {})
    monkeypatch.setattr(api, "indexing_repo", None)


def test_api_health():
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "ollama" in data
        assert "vectorai" in data


@pytest.mark.parametrize("online", [True, False])
def test_startup_in_legacy_console(monkeypatch, online):
    import io
    import sys
    import urllib.request

    def check_ollama(*args, **kwargs):
        if not online:
            raise OSError("Ollama is offline")

    monkeypatch.setattr(urllib.request, "urlopen", check_ollama)
    output = io.BytesIO()
    console = io.TextIOWrapper(output, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", console)
    with TestClient(app) as client:
        assert client.get("/status").status_code == 200
    console.flush()
    assert b"Backend Engine Starting" in output.getvalue()
    assert (b"ONLINE" if online else b"OFFLINE") in output.getvalue()


def test_api_status():
    with TestClient(app) as client:
        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "indexed_chunks" in data
        assert "embed_model" in data


def test_api_query_validation():
    with TestClient(app) as client:
        resp = client.post("/query", json={"query": "", "top_k": 5})
        assert resp.status_code == 400


def test_index_rejects_concurrent_request(tmp_path):
    with TestClient(app) as client:
        assert api.index_lock.acquire(blocking=False)
        try:
            response = client.post("/index", json={"repo_path": str(tmp_path)})
            assert response.status_code == 409
        finally:
            api.index_lock.release()


def test_query_requires_completed_workspace_index(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "get_last_repo", lambda: str(tmp_path / "other"))
    with TestClient(app) as client:
        response = client.post(
            "/query", json={"query": "find code", "repo_path": str(tmp_path)}
        )
        assert response.status_code == 409
        assert "Re-index" in response.json()["detail"]


def test_query_and_status_are_scoped_while_other_workspace_indexes(
    tmp_path, monkeypatch
):
    from backend.vector_store import get_db, canonical_repo

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for repo, name in [(first, "first.py"), (second, "second.py")]:
        db = get_db(str(repo))
        db.batch_upsert([{"file_path": name, "content_hash": name}], [[1.0, 0.0]])
        db.update_state(last_indexed="today", embed_model=api.Settings.EMBED_MODEL)

    async def query(text, top_k, explain, repo_path):
        return {"results": get_db(repo_path).search([1.0, 0.0])}

    monkeypatch.setattr(api, "run_query", query)
    with TestClient(app) as client:
        assert api.index_lock.acquire(blocking=False)
        monkeypatch.setattr(api, "indexing_repo", canonical_repo(str(second)))
        try:
            response = client.post(
                "/query", json={"query": "code", "repo_path": str(first)}
            )
            assert response.status_code == 200
            assert response.json()["results"][0]["file_path"] == "first.py"
            assert (
                client.post(
                    "/query", json={"query": "code", "repo_path": str(second)}
                ).status_code
                == 409
            )
            for repo in (first, second):
                status = client.get("/status", params={"repo_path": str(repo)}).json()
                assert status["repo_path"] == canonical_repo(str(repo))
                assert status["indexed_chunks"] == 1
                assert status["watching"] is True
        finally:
            api.index_lock.release()
        observers = [engine.observer for engine in api.indexers.values()]
    assert len(observers) == 2
    assert all(not observer.is_alive() for observer in observers)
