from pathlib import Path

from backend.indexer import Indexer, RepoEventHandler
from backend.config import Settings
from watchdog.events import FileDeletedEvent


def test_root_nested_negations_and_custom_excludes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(Settings, "VECTORAI_DB_PATH", str(tmp_path / "db"))
    for name in [
        "keep.py",
        "drop.py",
        "secret.py",
        "nested/drop.py",
        "nested/keep.py",
        "nested/skip.py",
        "ignored/child.py",
        "generated/a.py",
    ]:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def code(): pass\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        "drop.py\nignored/\nnested/*.py\n!nested/keep.py\n", encoding="utf-8"
    )
    (repo / "nested/.gitignore").write_text("!drop.py\nskip.py\n", encoding="utf-8")
    (repo / ".codelensignore").write_text("secret.py\n", encoding="utf-8")
    engine = Indexer(str(repo), exclude_patterns=["generated/**"])
    names = {Path(path).relative_to(repo).as_posix() for path in engine.walk_repo()}
    assert names == {"keep.py", "nested/drop.py", "nested/keep.py"}
    seen = []
    monkeypatch.setattr(engine, "reindex_file", seen.append)
    handler = RepoEventHandler(engine, debounce_seconds=0)
    for name in [
        "drop.py",
        "secret.py",
        "nested/skip.py",
        "ignored/child.py",
        "generated/a.py",
    ]:
        handler.reindex_if_supported(str(repo / name))
    assert seen == []
    handler.reindex_if_supported(str(repo / "nested/drop.py"))
    assert seen == [str(repo / "nested/drop.py")]


def test_ignore_change_removes_previously_indexed_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "VECTORAI_DB_PATH", str(tmp_path / "db"))
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "drop.py"
    source.write_text("def code(): pass\n", encoding="utf-8")
    engine = Indexer(str(repo))
    engine.db.batch_upsert(
        [{"file_path": "drop.py", "content_hash": "old"}], [[1.0, 0.0]]
    )
    assert len(list(engine.walk_repo())) == 1
    (repo / ".gitignore").write_text("drop.py\n", encoding="utf-8")
    engine.remove_deleted_files()
    assert engine.db.count() == 0


def test_watcher_ignores_database_and_dependency_deletions(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(Settings, "VECTORAI_DB_PATH", str(repo / "custom-index"))
    engine = Indexer(str(repo))
    deleted = []
    monkeypatch.setattr(engine.db, "delete_by_filepath", deleted.append)
    handler = RepoEventHandler(engine)
    handler.dispatch(FileDeletedEvent(str(repo / "custom-index/db-wal")))
    handler.dispatch(FileDeletedEvent(str(repo / "node_modules/package/a.py")))
    assert deleted == []
