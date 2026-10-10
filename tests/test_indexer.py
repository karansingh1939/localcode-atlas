import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from watchdog.events import (
    DirDeletedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from backend import indexer
from backend.config import Settings


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "VECTORAI_DB_PATH", str(tmp_path / "db"))
    repo = tmp_path / "repo"
    repo.mkdir()
    from backend import api

    monkeypatch.setattr(api, "get_last_repo", lambda: str(repo))
    return indexer.Indexer(str(repo))


def seed(engine, name, text):
    path = Path(engine.repo_path) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    chunks = indexer.chunk_file(str(path), engine.repo_path)
    engine.db.batch_upsert(chunks, [[1.0, 0.0]] * len(chunks))
    return path


@pytest.mark.parametrize("extension", [".py", ".txt"])
def test_large_chunks_fit_embedding_context(tmp_path, extension):
    content = (
        'def huge():\n    value = "' + "x" * 5000 + '"\n'
        if extension == ".py"
        else "x" * 5000
    )
    path = tmp_path / f"large{extension}"
    path.write_text(content, encoding="utf-8")

    chunks = indexer.chunk_file(str(path), str(tmp_path))

    assert len(chunks) > 1
    assert all(0 < len(chunk["chunk_text"]) <= 1200 for chunk in chunks)
    assert len({chunk["content_hash"] for chunk in chunks}) == len(chunks)
    assert "".join(chunk["chunk_text"] for chunk in chunks).count("x") == 5000


@pytest.mark.parametrize("directory_event", [False, True])
def test_watcher_deletion_removes_all_file_chunks_only(engine, directory_event):
    name = "removed/deleted.py" if directory_event else "deleted.py"
    deleted = seed(engine, name, "def first(): pass\ndef second(): pass\n")
    seed(engine, "keep.py", "def keep(): return 42\n")
    deleted.unlink()
    if directory_event:
        deleted.parent.rmdir()
        event = DirDeletedEvent(str(deleted.parent))
    else:
        event = FileDeletedEvent(str(deleted))
    indexer.RepoEventHandler(engine).dispatch(event)
    assert engine.db.count() == 1
    assert engine.db.search([1.0, 0.0])[0]["file_path"] == "keep.py"


@pytest.mark.parametrize("entry", ["api", "full"])
def test_reindex_prunes_deleted_files_without_clearing(engine, monkeypatch, entry):
    deleted = seed(engine, "deleted.py", "def gone(): pass\n")
    seed(engine, "keep.py", "def keep(): return 42\n")
    deleted.unlink()
    if entry == "full":
        engine.run_full_index()
    else:
        from backend import api

        monkeypatch.setattr(api, "Indexer", lambda _: engine)
        monkeypatch.setattr(api, "save_last_repo", lambda _: None)
        monkeypatch.setattr(engine, "start_watchdog", lambda: None)
        monkeypatch.setattr(api, "indexers", {})

        async def run():
            queue = asyncio.Queue()
            await asyncio.to_thread(
                api.indexer_worker,
                engine.repo_path,
                False,
                queue,
                asyncio.get_running_loop(),
            )
            events = []
            while (event := await queue.get()) is not None:
                events.append(event)
            assert not any(e["type"] == "error" for e in events)

        asyncio.run(run())
    assert engine.db.count() == 1
    assert engine.db.search([1.0, 0.0])[0]["file_path"] == "keep.py"


def test_repeated_modifications_use_live_event_loop(engine, monkeypatch, caplog):
    clients = []

    class LoopBoundClient:
        loop = None

        def __init__(self, **kwargs):
            self.closed = False
            clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        async def embed(self, **kwargs):
            current = asyncio.get_running_loop()
            if self.loop is not None and self.loop is not current:
                raise RuntimeError("Event loop is closed")
            self.loop = current
            return SimpleNamespace(embeddings=[[1.0, 0.0]] * len(kwargs["input"]))

    monkeypatch.setattr(indexer, "AsyncClient", LoopBoundClient)
    # Reconstruct after patching so the old persistent-client implementation is tested too.
    engine = indexer.Indexer(engine.repo_path)
    source = seed(engine, "repeat.py", "def repeated(): return 0\n")
    handler = indexer.RepoEventHandler(engine)
    for value in range(1, 4):
        source.write_text(f"def repeated(): return {value}\n", encoding="utf-8")
        handler.dispatch(FileModifiedEvent(str(source)))
        result = engine.db.search([1.0, 0.0])[0]
        assert result["score"] == pytest.approx(1.0)
        assert f"return {value}" in result["chunk_text"]
    assert "Event loop is closed" not in caplog.text
    assert all(client.closed for client in clients)


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_failed_embedding_is_not_persisted_and_can_retry(engine, monkeypatch, mode):
    source = Path(engine.repo_path) / "retry.py"
    source.write_text("def retry_me(): return 7\n", encoding="utf-8")
    chunks = indexer.chunk_file(str(source), engine.repo_path)
    failing = True

    class SyncClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def embed(self, **kwargs):
            if failing:
                raise RuntimeError("embedding service unavailable")
            return SimpleNamespace(embeddings=[[1.0, 0.0]] * len(kwargs["input"]))

    class AsyncClient(SyncClient):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def embed(self, **kwargs):
            return super().embed(**kwargs)

    monkeypatch.setattr(indexer, "Client", SyncClient)
    monkeypatch.setattr(indexer, "AsyncClient", AsyncClient)
    engine = indexer.Indexer(engine.repo_path)

    def store():
        if mode == "sync":
            engine.embed_and_store_sync(chunks)
        else:
            asyncio.run(engine.embed_and_store(chunks))

    with pytest.raises(RuntimeError, match="embedding service unavailable"):
        store()
    assert engine.db.count() == 0
    assert engine.db.get_existing_hashes([c["content_hash"] for c in chunks]) == []
    failing = False
    store()
    assert engine.db.count() == 1
    assert engine.db.search([1.0, 0.0])[0]["score"] == pytest.approx(1.0)


def test_api_reports_embedding_failure_without_success_count(engine, monkeypatch):
    from backend import api

    source = seed(engine, "failure.py", "def unavailable(): return 9\n")
    engine.db.delete_by_filepath(source.name)
    monkeypatch.setattr(api, "Indexer", lambda _: engine)
    monkeypatch.setattr(api, "save_last_repo", lambda _: None)
    monkeypatch.setattr(api, "indexers", {})
    monkeypatch.setattr(engine, "start_watchdog", lambda: None)

    def fail(chunks, force=False):
        raise RuntimeError("embedding service unavailable")

    monkeypatch.setattr(engine, "embed_and_store_sync", fail)

    async def run():
        queue = asyncio.Queue()
        await asyncio.to_thread(
            api.indexer_worker,
            engine.repo_path,
            False,
            queue,
            asyncio.get_running_loop(),
        )
        events = []
        while (event := await queue.get()) is not None:
            events.append(event)
        assert any(
            e["type"] == "error" and "embedding service unavailable" in e["message"]
            for e in events
        )
        assert next(e for e in events if e["type"] == "complete")["total_chunks"] == 0

    asyncio.run(run())
    assert engine.db.count() == 0


def test_same_code_in_two_files_keeps_both_locations(engine, monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def embed(self, **kwargs):
            return SimpleNamespace(embeddings=[[1.0, 0.0]] * len(kwargs["input"]))

    monkeypatch.setattr(indexer, "Client", Client)
    for name in ("first.py", "second.py"):
        path = Path(engine.repo_path) / name
        path.write_text("def same(): return True\n", encoding="utf-8")
        engine.embed_and_store_sync(indexer.chunk_file(str(path), engine.repo_path))
    assert set(engine.db.get_filepaths()) == {"first.py", "second.py"}


def test_reindex_replaces_file_atomically(engine, monkeypatch):
    source = Path(engine.repo_path) / "source.py"
    source.write_text("def value(): return 1\n", encoding="utf-8")
    seed(engine, "source.py", source.read_text(encoding="utf-8"))

    class Client:
        fail = False

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def embed(self, **kwargs):
            if self.fail:
                raise RuntimeError("embedding failed")
            return SimpleNamespace(embeddings=[[1.0, 0.0]] * len(kwargs["input"]))

    monkeypatch.setattr(indexer, "AsyncClient", Client)
    source.write_text("\n\n\ndef value(): return 2\n", encoding="utf-8")
    Client.fail = True
    with pytest.raises(RuntimeError, match="embedding failed"):
        asyncio.run(engine.reindex_file_async(str(source)))
    assert engine.db.search([1.0, 0.0])[0]["chunk_text"] == "def value(): return 1"
    Client.fail = False
    asyncio.run(engine.reindex_file_async(str(source)))
    result = engine.db.search([1.0, 0.0])
    assert len(result) == 1
    assert result[0]["chunk_text"] == "def value(): return 2"
    assert result[0]["line_start"] == 4


def test_watcher_tracks_new_and_moved_files(engine, monkeypatch):
    original = seed(engine, "original.py", "def old(): return 1\n")
    seen = []
    monkeypatch.setattr(engine, "reindex_file", seen.append)
    handler = indexer.RepoEventHandler(engine)
    created = Path(engine.repo_path) / "created.md"
    created.write_text("new documentation", encoding="utf-8")
    handler.dispatch(FileCreatedEvent(str(created)))
    moved = Path(engine.repo_path) / "moved.py"
    original.rename(moved)
    handler.dispatch(FileMovedEvent(str(original), str(moved)))
    assert seen == [str(created), str(moved)]
    assert "original.py" not in engine.db.get_filepaths()


def test_full_index_removes_chunks_for_empty_and_ignored_files(engine):
    empty = seed(engine, "empty.py", "def removed(): pass\n")
    seed(engine, "node_modules/dependency.py", "def ignored(): pass\n")
    empty.write_text("", encoding="utf-8")
    engine.run_full_index()
    assert engine.db.count() == 0


def test_model_switch_rebuilds_instead_of_reusing_old_chunks(engine, monkeypatch):
    from backend import api

    seed(engine, "same.py", "def same(): pass\n")
    engine.db.update_state(embed_model="old-model")
    monkeypatch.setattr(api, "Indexer", lambda _: engine)
    monkeypatch.setattr(api, "save_last_repo", lambda _: None)
    monkeypatch.setattr(api, "indexers", {})
    monkeypatch.setattr(engine, "start_watchdog", lambda: None)
    calls = []

    def store(chunks, force=False):
        calls.append(chunks)
        assert engine.db.count() == 0
        engine.db.batch_upsert(chunks, [[0.0, 1.0]] * len(chunks))

    monkeypatch.setattr(engine, "embed_and_store_sync", store)

    async def run():
        queue = asyncio.Queue()
        await asyncio.to_thread(
            api.indexer_worker,
            engine.repo_path,
            False,
            queue,
            asyncio.get_running_loop(),
        )
        events = []
        while (event := await queue.get()) is not None:
            events.append(event)
        assert not any(event["type"] == "error" for event in events)

    asyncio.run(run())
    assert len(calls) == 1
    assert engine.db.search([0.0, 1.0])[0]["score"] == pytest.approx(1.0)
