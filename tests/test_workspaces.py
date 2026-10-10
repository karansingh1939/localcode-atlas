import json
import sqlite3

from backend.config import Settings
from backend.vector_store import get_db, LocalVectorDB, canonical_repo


def test_same_relative_files_in_separate_workspaces_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "VECTORAI_DB_PATH", str(tmp_path / "db"))
    first = get_db(str(tmp_path / "first"))
    second = get_db(str(tmp_path / "second"))
    meta = [{"file_path": "same.py", "content_hash": "same", "symbol_name": "first"}]
    first.batch_upsert(meta, [[1.0, 0.0]])
    second.batch_upsert([{**meta[0], "symbol_name": "second"}], [[0.0, 1.0]])
    assert first.db_file != second.db_file
    assert first.search([1.0, 0.0])[0]["symbol_name"] == "first"
    assert second.search([1.0, 0.0])[0]["symbol_name"] == "second"
    second.clear()
    assert first.count() == 1
    assert get_db(str(tmp_path / "first" / ".")) is first


def test_legacy_migration_preserves_original_and_only_copies_to_owner(
    tmp_path, monkeypatch
):
    base = tmp_path / "db"
    monkeypatch.setattr(Settings, "VECTORAI_DB_PATH", str(base))
    legacy = LocalVectorDB()
    legacy.batch_upsert(
        [{"file_path": "keep.py", "content_hash": "legacy"}], [[1.0, 0.0]]
    )
    owner = str(tmp_path / "owner")
    (tmp_path / "last_repo.json").write_text(
        json.dumps({"repo_path": owner, "last_indexed": "yesterday"}), encoding="utf-8"
    )
    migrated = get_db(owner)
    assert migrated.count() == 1
    assert migrated.get_state()["repo_path"] == canonical_repo(owner)
    assert migrated.get_state()["last_indexed"] == "yesterday"
    assert get_db(str(tmp_path / "other")).count() == 0
    migrated.clear()
    with sqlite3.connect(base / "codelens.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
