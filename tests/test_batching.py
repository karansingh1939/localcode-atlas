import asyncio
from types import SimpleNamespace

import pytest

from backend.config import Settings
from backend import indexer


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_bounded_batches_reuse_unchanged_chunks_and_preserve_data_on_failure(
    tmp_path, monkeypatch, mode
):
    monkeypatch.setattr(Settings, "VECTORAI_DB_PATH", str(tmp_path / "db"))
    monkeypatch.setattr(Settings, "EMBED_BATCH_SIZE", 2)
    calls = []
    failing = False

    class SyncClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def embed(self, **kwargs):
            calls.append(kwargs["input"])
            if failing:
                raise RuntimeError("batch failed")
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
    engine = indexer.Indexer(str(tmp_path / "repo"))
    chunks = [
        {"file_path": "a.py", "content_hash": str(i), "chunk_text": f"chunk {i}"}
        for i in range(5)
    ]

    def store(chunks):
        return (
            engine.embed_and_store_sync(chunks)
            if mode == "sync"
            else asyncio.run(engine.embed_and_store(chunks))
        )

    assert store(chunks) == 5
    assert list(map(len, calls)) == [2, 2, 1]
    calls.clear()
    assert store(chunks) == 0
    assert calls == []
    changed = chunks[:-1] + [
        {**chunks[-1], "content_hash": "new", "chunk_text": "changed"}
    ]
    failing = True
    with pytest.raises(RuntimeError, match="batch failed"):
        store(changed)
    assert engine.db.get_file_hashes("a.py") == {str(i) for i in range(5)}
    failing = False
    calls.clear()
    assert store(changed) == 1
    assert len(calls) == 1 and len(calls[0]) == 1
    assert engine.db.get_file_hashes("a.py") == {"0", "1", "2", "3", "new"}
