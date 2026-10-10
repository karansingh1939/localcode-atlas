from backend.config import Settings
from backend.vector_store import LocalVectorDB


def test_exact_symbol_and_file_path_beat_unrelated_semantic_match(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Settings, "VECTORAI_DB_PATH", str(tmp_path / "db"))
    db = LocalVectorDB()
    db.batch_upsert(
        [
            {
                "symbol_name": "authenticate_user",
                "file_path": "auth.py",
                "chunk_text": "validate token",
                "content_hash": "auth",
            },
            {
                "symbol_name": "unrelated",
                "file_path": "other.py",
                "chunk_text": "render page",
                "content_hash": "other",
            },
        ],
        [[0.0, 1.0], [1.0, 0.0]],
    )
    assert db.search([1.0, 0.0], top_k=1)[0]["symbol_name"] == "unrelated"
    result = db.search([1.0, 0.0], top_k=1, query="find authenticate_user")[0]
    assert result["symbol_name"] == "authenticate_user"
    assert 0 <= result["score"] <= 1
    assert db.search([1.0, 0.0], top_k=1, query="auth.py")[0]["file_path"] == "auth.py"


def test_keywords_camel_case_and_empty_query(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "VECTORAI_DB_PATH", str(tmp_path / "db"))
    db = LocalVectorDB()
    db.batch_upsert(
        [
            {
                "symbol_name": "validateToken",
                "chunk_text": "validate credentials",
                "content_hash": "auth",
            },
            {
                "symbol_name": "render",
                "chunk_text": "draw shapes",
                "content_hash": "draw",
            },
        ],
        [[1.0, 0.0], [1.0, 0.0]],
    )
    assert (
        db.search([1.0, 0.0], top_k=1, query="validate token")[0]["symbol_name"]
        == "validateToken"
    )
    assert db.search([1.0, 0.0], query="") == db.search([1.0, 0.0])
