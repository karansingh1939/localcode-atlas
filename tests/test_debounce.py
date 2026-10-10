import threading
import time

from watchdog.events import FileDeletedEvent, FileModifiedEvent

from backend.config import Settings
from backend.indexer import Indexer, RepoEventHandler


def test_rapid_saves_coalesce_and_deletion_cancels_pending_update(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Settings, "VECTORAI_DB_PATH", str(tmp_path / "db"))
    repo = tmp_path / "repo"
    repo.mkdir()
    file = repo / "saved.py"
    file.write_text("def saved(): pass\n", encoding="utf-8")
    engine = Indexer(str(repo))
    seen = []
    ready = threading.Event()
    monkeypatch.setattr(
        engine, "reindex_file", lambda path: (seen.append(path), ready.set())
    )
    handler = RepoEventHandler(engine, debounce_seconds=0.05)
    try:
        for _ in range(10):
            handler.dispatch(FileModifiedEvent(str(file)))
        assert ready.wait(2)
        assert seen == [str(file)]
        ready.clear()
        handler.dispatch(FileModifiedEvent(str(file)))
        file.unlink()
        handler.dispatch(FileDeletedEvent(str(file)))
        assert not ready.wait(0.15)
        assert len(seen) == 1
    finally:
        handler.close()


def test_shutdown_cancels_pending_callbacks(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "VECTORAI_DB_PATH", str(tmp_path / "db"))
    repo = tmp_path / "repo"
    repo.mkdir()
    file = repo / "saved.py"
    file.write_text("def saved(): pass\n", encoding="utf-8")
    engine = Indexer(str(repo))
    seen = []
    monkeypatch.setattr(engine, "reindex_file", seen.append)
    handler = RepoEventHandler(engine, debounce_seconds=0.05)
    handler.dispatch(FileModifiedEvent(str(file)))
    handler.close()
    time.sleep(0.1)
    assert seen == []
