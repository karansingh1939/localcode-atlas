import asyncio
import threading
import time

import pytest
from watchdog.events import FileModifiedEvent

from backend import api
from backend.code_parser import extract_chunks
from backend.config import Settings
from backend.indexer import Indexer, RepoEventHandler, chunk_file


@pytest.mark.parametrize(
    "name,content,missing",
    [
        (
            "config.py",
            "LIMIT = 17\ndef connect(): return True\nTAIL = 23\n",
            ["LIMIT = 17", "TAIL = 23"],
        ),
        (
            "config.ts",
            "const label = 'café';\nexport function connect() {}\nstart();\n",
            ["café", "start();"],
        ),
    ],
)
def test_parser_keeps_code_outside_symbols(name, content, missing):
    chunks = extract_chunks(name, content)
    for text in missing:
        assert any(text in chunk["chunk_text"] for chunk in chunks)
    assert any(chunk["symbol_name"] == "connect" for chunk in chunks)
    for chunk in chunks:
        assert chunk["chunk_text"].strip() in content
        assert (
            1 <= chunk["line_start"] <= chunk["line_end"] <= len(content.splitlines())
        )
        assert (
            chunk["chunk_text"].splitlines()[0].strip()
            in content.splitlines()[chunk["line_start"] - 1]
        )


def test_self_ignored_ignore_file_still_refreshes_index(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "VECTORAI_DB_PATH", str(tmp_path / "db"))
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "visible.py"
    source.write_text("def visible(): return 1\n", encoding="utf-8")
    engine = Indexer(str(repo))
    chunks = chunk_file(str(source), str(repo))
    engine.db.batch_upsert(chunks, [[1.0, 0.0]] * len(chunks))
    ignore = repo / ".gitignore"
    ignore.write_text(".*\nvisible.py\n", encoding="utf-8")
    RepoEventHandler(engine).dispatch(FileModifiedEvent(str(ignore)))
    assert engine.db.count() == 0


@pytest.mark.parametrize("operation", ["save", "delete", "create"])
def test_changes_during_full_index_are_eventually_indexed(
    tmp_path, monkeypatch, operation
):
    monkeypatch.setattr(Settings, "VECTORAI_DB_PATH", str(tmp_path / "db"))
    monkeypatch.setattr(Settings, "WATCH_DEBOUNCE_SECONDS", 0)
    monkeypatch.setattr(api, "SIDECAR_FILE", str(tmp_path / "last_repo.json"))
    monkeypatch.setattr(api, "indexers", {})
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "example.py"
    source.write_text('def value(): return "old"\n', encoding="utf-8")
    engine = Indexer(str(repo))
    monkeypatch.setattr(api, "Indexer", lambda _: engine)
    updated = threading.Event()

    def embed(chunks, force=False):
        if operation == "save":
            source.write_text('def value(): return "new"\n', encoding="utf-8")
        elif operation == "delete":
            source.unlink()
        else:
            (repo / "created.py").write_text(
                'def created(): return "new"\n', encoding="utf-8"
            )
        engine.db.batch_upsert(chunks, [[1.0, 0.0]] * len(chunks))
        return len(chunks)

    def reindex(path):
        chunks = chunk_file(path, str(repo))
        engine.db.batch_upsert(
            chunks, [[1.0, 0.0]] * len(chunks), replace_file_path=chunks[0]["file_path"]
        )
        updated.set()

    monkeypatch.setattr(engine, "embed_and_store_sync", embed)
    monkeypatch.setattr(engine, "reindex_file", reindex)

    async def run():
        queue = asyncio.Queue()
        await asyncio.to_thread(
            api.indexer_worker, str(repo), False, queue, asyncio.get_running_loop()
        )
        events = []
        while (event := await queue.get()) is not None:
            events.append(event)
        assert not any(event["type"] == "error" for event in events), events

    try:
        asyncio.run(run())
        if operation != "delete":
            assert updated.wait(3), "Watcher missed change during full indexing"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            results = engine.db.search([1.0, 0.0])
            if operation == "delete" and not results:
                break
            if operation != "delete" and any(
                '"new"' in result["chunk_text"] for result in results
            ):
                break
            time.sleep(0.02)
        if operation == "delete":
            assert engine.db.count() == 0
        else:
            assert any(
                '"new"' in result["chunk_text"]
                for result in engine.db.search([1.0, 0.0])
            )
    finally:
        engine.stop_watchdog()
