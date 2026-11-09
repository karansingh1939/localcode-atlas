import asyncio
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from backend.config import Settings
from backend.vector_store import get_db, canonical_repo, indexed_workspaces
from backend.indexer import Indexer, chunk_file
from backend.search import run_query

logger = logging.getLogger(__name__)

SIDECAR_FILE = os.path.join(
    os.path.dirname(Settings.VECTORAI_DB_PATH)
    if os.path.dirname(Settings.VECTORAI_DB_PATH)
    else ".",
    "last_repo.json",
)
indexers: dict[str, Indexer] = {}
indexing_repo: str | None = None
index_lock = threading.Lock()


def get_index_state(repo_path: str | None = None) -> dict:
    if repo_path:
        return get_db(repo_path).get_state()
    if os.path.exists(SIDECAR_FILE):
        try:
            with open(SIDECAR_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def get_last_repo() -> str | None:
    return get_index_state().get("repo_path")


def save_last_repo(path: str):
    get_db(path).update_state(
        repo_path=canonical_repo(path), embed_model=Settings.EMBED_MODEL
    )
    try:
        os.makedirs(os.path.dirname(os.path.abspath(SIDECAR_FILE)), exist_ok=True)
        with open(SIDECAR_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(
                {
                    "repo_path": path,
                    "last_indexed": datetime.now().isoformat(),
                    "embed_model": Settings.EMBED_MODEL,
                },
                f,
            )
        os.replace(SIDECAR_FILE + ".tmp", SIDECAR_FILE)
    except Exception as e:
        logger.error(f"Failed to save sidecar tracker: {e}")
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Init DB explicitly to load resources safely
    db = get_db()

    print("\n" + "=" * 60)
    print("LocalCode Atlas Backend Engine Starting...")
    print(f"Local index path: {Settings.VECTORAI_DB_PATH}")
    print(f"Total Chunks in DB: {db.count()}")

    import urllib.request

    try:
        urllib.request.urlopen(f"{Settings.OLLAMA_HOST}/api/tags", timeout=2)
        print("Ollama Status: ONLINE")
    except Exception as e:
        print(f"Ollama Status: OFFLINE ({e})")

    last_repo = get_last_repo()
    if last_repo:
        get_db(last_repo)  # Migrate the existing index before restoring watchers.
    for repo in indexed_workspaces():
        if os.path.isdir(repo):
            indexer = Indexer(repo)
            indexer.start_watchdog()
            indexers[canonical_repo(repo)] = indexer

    print("=" * 60 + "\n")

    yield

    # Teardown logic
    for indexer in list(indexers.values()):
        indexer.stop_watchdog()
    indexers.clear()


app = FastAPI(title="LocalCode Atlas Offline Core", lifespan=lifespan)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    status_code = 500
    if isinstance(exc, HTTPException):
        status_code = exc.status_code
    return JSONResponse(
        status_code=status_code,
        content={"error": type(exc).__name__, "detail": str(exc)},
    )


class IndexRequest(BaseModel):
    repo_path: str
    force_reindex: bool = False
    exclude_patterns: list[str] = Field(default_factory=list, max_length=100)


class QueryRequest(BaseModel):
    query: str
    repo_path: str | None = None
    top_k: int = Field(8, ge=1, le=20)
    explain: bool = False


def indexer_worker(
    repo_path: str,
    force: bool,
    q: asyncio.Queue,
    main_loop: asyncio.AbstractEventLoop,
    exclude_patterns: list[str] | None = None,
):
    def send(event: dict | None):
        asyncio.run_coroutine_threadsafe(q.put(event), main_loop)

    try:
        repo_path = canonical_repo(repo_path)
        if repo_path in indexers:
            indexers[repo_path].stop_watchdog()
        if exclude_patterns is not None:
            get_db(repo_path).update_state(exclude_patterns=exclude_patterns)
        indexer = Indexer(repo_path)
        with indexer.update_lock:
            indexers[repo_path] = indexer
            indexer.start_watchdog()
            previous_model = indexer.db.get_state().get("embed_model")
            if previous_model and previous_model != Settings.EMBED_MODEL:
                indexer.db.clear()
            # Record ownership before writing so partial indexes never belong to the old repo.
            save_last_repo(repo_path)
            indexer.remove_deleted_files()

            files = list(indexer.walk_repo())
            total_files = len(files)
            failed_files = 0
            skipped_files = 0
            start_time = time.time()

            for file_index, file_path in enumerate(files, 1):
                try:
                    rel = os.path.relpath(file_path, repo_path)
                    chunks = chunk_file(file_path, repo_path)
                    chunk_count = len(chunks)

                    send(
                        {
                            "type": "progress",
                            "file": rel,
                            "chunks": chunk_count,
                            "total_files": total_files,
                            "processed_files": file_index,
                        }
                    )

                    if chunks:
                        embedded = indexer.embed_and_store_sync(chunks, force=force)
                        if embedded == 0:
                            skipped_files += 1
                    else:
                        indexer.db.delete_by_filepath(rel)
                except Exception as err:
                    failed_files += 1
                    send({"type": "error", "message": str(err), "file": str(file_path)})

            duration = int((time.time() - start_time) * 1000)
            indexers[repo_path] = indexer
            indexer.db.update_state(
                last_indexed=datetime.now().astimezone().isoformat()
            )
            send(
                {
                    "type": "complete",
                    "total_chunks": indexer.db.count(),
                    "failed_files": failed_files,
                    "watching": indexer.observer is not None
                    and indexer.observer.is_alive(),
                    "duration_ms": duration,
                    "skipped": skipped_files,
                }
            )

            save_last_repo(repo_path)

    except Exception as e:
        send({"type": "error", "message": str(e), "file": "system"})
    finally:
        global indexing_repo
        indexing_repo = None
        if index_lock.locked():
            index_lock.release()
        send(None)


@app.post("/index")
async def api_index(req: IndexRequest):
    if not os.path.exists(req.repo_path) or not os.path.isdir(req.repo_path):
        raise HTTPException(
            status_code=400,
            detail="Target repo_path does not exist or is not a strictly defined directory.",
        )
    if not index_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Indexing is already in progress.")
    global indexing_repo
    indexing_repo = canonical_repo(req.repo_path)

    q: asyncio.Queue = asyncio.Queue()
    main_loop = asyncio.get_running_loop()

    t = threading.Thread(
        target=indexer_worker,
        args=(
            os.path.realpath(req.repo_path),
            req.force_reindex,
            q,
            main_loop,
            req.exclude_patterns,
        ),
        daemon=True,
    )
    try:
        t.start()
    except Exception:
        indexing_repo = None
        index_lock.release()
        raise

    async def sse_gen():
        while True:
            event = await q.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(sse_gen(), media_type="text/event-stream")


@app.post("/query")
async def api_query(req: QueryRequest):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query payload empty.")

    indexed_repo = canonical_repo(req.repo_path) if req.repo_path else get_last_repo()
    if index_lock.locked() and indexed_repo == indexing_repo:
        raise HTTPException(
            status_code=409,
            detail="Indexing is in progress. Try again when it finishes.",
        )
    state = get_index_state(indexed_repo)
    indexed_model = state.get("embed_model")
    if indexed_model and indexed_model != Settings.EMBED_MODEL:
        raise HTTPException(
            status_code=409,
            detail="Embedding model changed. Re-index the workspace before searching.",
        )
    if not indexed_repo or not state.get("last_indexed"):
        raise HTTPException(
            status_code=409,
            detail="Re-index this workspace before searching; it has no completed index.",
        )

    start_time = time.time()
    db = get_db(indexed_repo)

    data = await run_query(
        req.query, top_k=req.top_k, explain=req.explain, repo_path=indexed_repo
    )

    query_ts = int((time.time() - start_time) * 1000)
    total_indexed = db.count()

    logger.info(
        f"Query: '{req.query}' | Results: {len(data['results'])} | Execution: {query_ts}ms"
    )

    return {
        "results": data["results"],
        "explain_text": data.get("explain_text"),
        "query_ms": query_ts,
        "total_indexed": total_indexed,
        "repo_path": indexed_repo,
    }


@app.get("/status")
def api_status(repo_path: str | None = None):
    repo_path = canonical_repo(repo_path) if repo_path else get_last_repo()
    db = get_db(repo_path)
    state = db.get_state()
    indexer = indexers.get(repo_path)

    return {
        "indexed_chunks": db.count(),
        "last_indexed": state.get("last_indexed"),
        "db_path": db.path,
        "embed_model": Settings.EMBED_MODEL,
        "repo_path": repo_path,
        "indexing": index_lock.locked() and indexing_repo == repo_path,
        "watching": indexer is not None
        and indexer.observer is not None
        and indexer.observer.is_alive(),
    }


@app.get("/health")
def api_health():
    import urllib.request

    ollama_ok = False
    ollama_err = None

    try:
        urllib.request.urlopen(f"{Settings.OLLAMA_HOST}/api/tags", timeout=1)
        ollama_ok = True
    except Exception as e:
        ollama_err = str(e)

    db_ok = False
    db_dir = os.path.dirname(Settings.VECTORAI_DB_PATH)
    if os.path.exists(Settings.VECTORAI_DB_PATH) or (db_dir and os.path.exists(db_dir)):
        db_ok = True

    return {"ollama": ollama_ok, "vectorai": db_ok, "ollama_error": ollama_err}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.api:app", host="127.0.0.1", port=8000, reload=True)
