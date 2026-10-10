"""Discover, parse, embed, and watch source files in a repository."""

import os
import hashlib
import asyncio
import logging
import threading
from typing import List, Generator, Dict, Any
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from rich.progress import (
    Progress,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeElapsedColumn,
)
from ollama import AsyncClient, Client

from backend.vector_store import get_db, canonical_repo
from backend.config import Settings
from backend.code_parser import LANGUAGES, extract_chunks, _sliding_window
from backend.exclusions import Exclusions, IGNORE_DIRS

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = set(LANGUAGES.keys()) | {".md", ".txt", ".toml", ".yaml", ".yml"}
MAX_CHUNK_CHARS = 1200


def md5_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def chunk_file(file_path: str, repo_root: str) -> List[Dict[str, Any]]:
    """Read one file, run AST chunking, attach content_hash to every chunk."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        logger.warning(f"Could not read {file_path}: {e}")
        raise

    if not content.strip():
        return []

    rel_path = os.path.relpath(file_path, repo_root)
    ext = os.path.splitext(file_path)[1].lower()

    if ext in LANGUAGES:
        raw_chunks = extract_chunks(rel_path, content)
    else:
        raw_chunks = _sliding_window(rel_path, content, "unknown")

    chunks = []
    for chunk in raw_chunks:
        text = chunk["chunk_text"]
        if len(text) <= MAX_CHUNK_CHARS:
            chunk["content_hash"] = md5_hash(f"{rel_path}:{chunk['line_start']}:{text}")
            chunks.append(chunk)
            continue

        line = chunk["line_start"]
        for offset in range(0, len(text), MAX_CHUNK_CHARS):
            part = text[offset : offset + MAX_CHUNK_CHARS]
            end_line = min(chunk["line_end"], line + part.count("\n"))
            chunks.append(
                {
                    **chunk,
                    "line_start": line,
                    "line_end": end_line,
                    "chunk_text": part,
                    "content_hash": md5_hash(f"{rel_path}:{line}:{offset}:{part}"),
                }
            )
            line += part.count("\n")

    return chunks


class Indexer:
    def __init__(self, repo_path: str, exclude_patterns: list[str] | None = None):
        self.repo_path = canonical_repo(repo_path)
        self.db = get_db(self.repo_path)
        patterns = (
            exclude_patterns
            if exclude_patterns is not None
            else self.db.get_state().get("exclude_patterns", [])
        )
        self.exclusions = Exclusions(
            self.repo_path, patterns, Settings.VECTORAI_DB_PATH
        )
        self.observer: Observer | None = None
        self._handler: RepoEventHandler | None = None

    def walk_repo(self) -> Generator[str, None, None]:
        for root, dirs, files in os.walk(self.repo_path):
            dirs[:] = [
                d
                for d in dirs
                if not self.exclusions.ignored(os.path.join(root, d), directory=True)
            ]
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in SUPPORTED_EXTENSIONS and not self.exclusions.ignored(
                    os.path.join(root, file)
                ):
                    yield os.path.join(root, file)

    def remove_deleted_files(self):
        eligible = {os.path.relpath(path, self.repo_path) for path in self.walk_repo()}
        for file_path in self.db.get_filepaths():
            if file_path not in eligible:
                self.db.delete_by_filepath(file_path)

    def _embedding_plan(self, chunks, force):
        hashes = [c["content_hash"] for c in chunks]
        cached = {} if force else self.db.get_embeddings_by_hash(hashes)
        pending = [c for c in chunks if c["content_hash"] not in cached]
        return cached, pending

    def _batches(self, chunks):
        for offset in range(0, len(chunks), Settings.EMBED_BATCH_SIZE):
            yield chunks[offset : offset + Settings.EMBED_BATCH_SIZE]

    @staticmethod
    def _collect_embeddings(cached, batch, response):
        vectors = (
            response.embeddings
            if hasattr(response, "embeddings")
            else response["embeddings"]
        )
        if len(vectors) != len(batch):
            raise ValueError("Ollama returned an incomplete embedding batch.")
        cached.update(
            (chunk["content_hash"], vector) for chunk, vector in zip(batch, vectors)
        )

    async def embed_and_store(self, chunks: List[Dict[str, Any]], force: bool = False):
        if not chunks:
            return 0

        file_path = chunks[0]["file_path"]
        if not force and self.db.get_file_hashes(file_path) == {
            c["content_hash"] for c in chunks
        }:
            return 0

        cached, pending = self._embedding_plan(chunks, force)

        async with AsyncClient(host=Settings.OLLAMA_HOST) as client:
            for batch in self._batches(pending):
                response = await client.embed(
                    model=Settings.EMBED_MODEL,
                    input=[f"search_document: {c['chunk_text']}" for c in batch],
                    truncate=False,
                )
                self._collect_embeddings(cached, batch, response)
        embeddings = [cached[c["content_hash"]] for c in chunks]
        self.db.batch_upsert(chunks, embeddings, replace_file_path=file_path)
        return len(pending)

    def embed_and_store_sync(self, chunks: List[Dict[str, Any]], force: bool = False):
        if not chunks:
            return 0

        file_path = chunks[0]["file_path"]
        if not force and self.db.get_file_hashes(file_path) == {
            c["content_hash"] for c in chunks
        }:
            return 0

        cached, pending = self._embedding_plan(chunks, force)

        with Client(host=Settings.OLLAMA_HOST) as client:
            for batch in self._batches(pending):
                response = client.embed(
                    model=Settings.EMBED_MODEL,
                    input=[f"search_document: {c['chunk_text']}" for c in batch],
                    truncate=False,
                )
                self._collect_embeddings(cached, batch, response)
        embeddings = [cached[c["content_hash"]] for c in chunks]
        self.db.batch_upsert(chunks, embeddings, replace_file_path=file_path)
        return len(pending)

    def run_full_index(self):
        self.remove_deleted_files()
        files = list(self.walk_repo())
        all_chunks = []

        with Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
        ) as progress:
            parse_task = progress.add_task("Parsing files", total=len(files))
            for fp in files:
                chunks = chunk_file(fp, self.repo_path)
                all_chunks.append((fp, chunks))
                progress.update(parse_task, description=f"[cyan]{os.path.basename(fp)}")
                progress.advance(parse_task)

            embed_task = progress.add_task(
                "Embedding chunks", total=sum(len(chunks) for _, chunks in all_chunks)
            )

            async def _run():
                for fp, chunks in all_chunks:
                    if chunks:
                        await self.embed_and_store(chunks)
                    else:
                        self.db.delete_by_filepath(os.path.relpath(fp, self.repo_path))
                    progress.advance(embed_task, advance=len(chunks))

            asyncio.run(_run())

        logger.info(f"Indexed {len(files)} files → {self.db.count()} chunks total.")

    async def reindex_file_async(self, file_path: str):
        rel_path = os.path.relpath(file_path, self.repo_path)
        chunks = chunk_file(file_path, self.repo_path)
        if chunks:
            await self.embed_and_store(chunks)
        else:
            self.db.delete_by_filepath(rel_path)
        logger.info(f"Re-indexed <{rel_path}> ({len(chunks)} chunks).")

    def reindex_file(self, file_path: str):
        asyncio.run(self.reindex_file_async(file_path))

    def start_watchdog(self):
        if self.observer is not None:
            return
        handler = RepoEventHandler(
            self, debounce_seconds=Settings.WATCH_DEBOUNCE_SECONDS
        )
        self._handler = handler
        self.observer = Observer()
        self.observer.schedule(handler, self.repo_path, recursive=True)
        self.observer.start()
        logger.info(f"Watchdog watching: {self.repo_path}")

    def stop_watchdog(self):
        if self.observer is not None:
            self.observer.stop()
            self.observer.join()
            self.observer = None
        if self._handler is not None:
            self._handler.close()
            self._handler = None


class RepoEventHandler(FileSystemEventHandler):
    def __init__(self, indexer: Indexer, debounce_seconds: float = 0):
        self.indexer = indexer
        self.debounce_seconds = debounce_seconds
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.RLock()
        self._closed = False

    def close(self):
        with self._lock:
            self._closed = True
            timers = list(self._timers.values())
            self._timers.clear()
            for timer in timers:
                timer.cancel()
        for timer in timers:
            timer.join()

    def _cancel(self, path):
        for pending in list(self._timers):
            if pending == path or Path(pending).is_relative_to(Path(path)):
                self._timers.pop(pending).cancel()

    def _refresh_exclusions(self, path):
        if Path(path).name not in {".gitignore", ".codelensignore"}:
            return False
        if self.indexer.exclusions.ignored(path):
            return False
        self.indexer.remove_deleted_files()
        for file in self.indexer.walk_repo():
            self.reindex_if_supported(file)
        return True

    def on_deleted(self, event):
        try:
            with self._lock:
                self._cancel(event.src_path)
                if self._refresh_exclusions(event.src_path):
                    return
                if self.indexer.exclusions.ignored(
                    event.src_path, directory=event.is_directory
                ):
                    return
                if event.is_directory:
                    self.indexer.remove_deleted_files()
                else:
                    rel_path = os.path.relpath(event.src_path, self.indexer.repo_path)
                    self.indexer.db.delete_by_filepath(rel_path)
        except Exception as e:
            logger.error(f"Index deletion failed for {event.src_path}: {e}")

    def on_modified(self, event):
        self.on_created(event)

    def on_created(self, event):
        if event.is_directory:
            return
        if self._refresh_exclusions(event.src_path):
            return
        self.reindex_if_supported(event.src_path)

    def on_moved(self, event):
        with self._lock:
            self._cancel(event.src_path)
        if self._refresh_exclusions(event.src_path) or self._refresh_exclusions(
            event.dest_path
        ):
            return
        if event.is_directory:
            self.indexer.remove_deleted_files()
            for root, dirs, files in os.walk(event.dest_path):
                dirs[:] = [
                    directory for directory in dirs if directory not in IGNORE_DIRS
                ]
                for file in files:
                    self.reindex_if_supported(os.path.join(root, file))
            return
        if not self.indexer.exclusions.ignored(event.src_path):
            self.indexer.db.delete_by_filepath(
                os.path.relpath(event.src_path, self.indexer.repo_path)
            )
        self.reindex_if_supported(event.dest_path)

    def reindex_if_supported(self, path: str):
        if os.path.splitext(path)[
            1
        ].lower() in SUPPORTED_EXTENSIONS and not self.indexer.exclusions.ignored(path):
            with self._lock:
                if self._closed:
                    return
                if not self.debounce_seconds:
                    self.reindex_safe(path)
                    return
                self._cancel(path)

                def flush():
                    with self._lock:
                        if self._closed or self._timers.get(path) is not timer:
                            return
                        self._timers.pop(path)
                        if os.path.isfile(path) and not self.indexer.exclusions.ignored(
                            path
                        ):
                            self.reindex_safe(path)

                timer = threading.Timer(self.debounce_seconds, flush)
                timer.daemon = True
                self._timers[path] = timer
                timer.start()

    def reindex_safe(self, path: str):
        try:
            self.indexer.reindex_file(path)
        except Exception as e:
            logger.error(f"Re-index failed for {path}: {e}")
