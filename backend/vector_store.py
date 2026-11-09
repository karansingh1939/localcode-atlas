"""SQLite storage for code chunks and exact NumPy cosine search."""

import uuid
import os
import sqlite3
import hashlib
import json
import threading
import re
from pathlib import Path
from contextlib import closing, contextmanager
import numpy as np
from typing import List, Dict, Any, Optional

from backend.config import Settings


class LocalVectorDB:
    """
    Store code metadata and embedding vectors in a local SQLite database.
    Rank results with NumPy cosine similarity and optional keyword matching.
    """

    def __init__(self, path: str | None = None):
        self.path = path or Settings.VECTORAI_DB_PATH
        os.makedirs(self.path, exist_ok=True)
        self.db_file = os.path.join(self.path, "codelens.db")
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_file, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")  # Safe concurrent writes
        conn.execute("PRAGMA synchronous=NORMAL")  # Balance safety/speed
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                    id           TEXT PRIMARY KEY,
                    symbol_name  TEXT,
                    chunk_text   TEXT,
                    file_path    TEXT,
                    line_start   INTEGER,
                    line_end     INTEGER,
                    language     TEXT,
                    content_hash TEXT UNIQUE,
                    embedding    BLOB NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_file_path ON chunks(file_path)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hash     ON chunks(content_hash)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.commit()

    def get_state(self) -> dict:
        with self._conn() as conn:
            return {
                key: json.loads(value)
                for key, value in conn.execute("SELECT key, value FROM state")
            }

    def update_state(self, **values):
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO state VALUES (?, ?)",
                [(key, json.dumps(value)) for key, value in values.items()],
            )

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def batch_upsert(
        self,
        metadata_list: List[Dict[str, Any]],
        embeddings: List[List[float]],
        replace_file_path: str | None = None,
    ):
        """Insert or replace chunks + their embeddings atomically."""
        if len(metadata_list) != len(embeddings):
            raise ValueError("Every chunk must have an embedding.")
        rows = []
        dimension = None
        for meta, emb in zip(metadata_list, embeddings):
            vector = np.asarray(emb, dtype=np.float32)
            if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
                raise ValueError("Embeddings must be nonempty finite vectors.")
            if dimension is not None and vector.size != dimension:
                raise ValueError("Embedding dimensions must match.")
            dimension = vector.size
            emb_blob = vector.tobytes()
            rows.append(
                (
                    str(uuid.uuid4()),
                    meta.get("symbol_name", ""),
                    meta.get("chunk_text", ""),
                    meta.get("file_path", ""),
                    meta.get("line_start", 0),
                    meta.get("line_end", 0),
                    meta.get("language", ""),
                    meta.get("content_hash", str(uuid.uuid4())),
                    emb_blob,
                )
            )
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT length(embedding) FROM chunks LIMIT 1"
            ).fetchone()
            if existing and dimension is not None and existing[0] != dimension * 4:
                raise ValueError(
                    "Embedding model dimensions changed. Re-index the workspace."
                )
            if replace_file_path is not None:
                conn.execute(
                    "DELETE FROM chunks WHERE file_path = ?", (replace_file_path,)
                )
            conn.executemany(
                """
                INSERT OR REPLACE INTO chunks
                    (id, symbol_name, chunk_text, file_path,
                     line_start, line_end, language, content_hash, embedding)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                rows,
            )
            conn.commit()

    def get_existing_hashes(self, hashes: List[str]) -> List[str]:
        """Return the subset of hashes already stored (for incremental indexing)."""
        if not hashes:
            return []
        placeholders = ",".join("?" * len(hashes))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT content_hash FROM chunks WHERE content_hash IN ({placeholders})",
                hashes,
            ).fetchall()
        return [r[0] for r in rows]

    def get_filepaths(self) -> List[str]:
        with self._conn() as conn:
            return [
                row[0] for row in conn.execute("SELECT DISTINCT file_path FROM chunks")
            ]

    def get_embeddings_by_hash(self, hashes: list[str]) -> dict[str, list[float]]:
        found = {}
        with self._conn() as conn:
            for offset in range(0, len(hashes), 500):
                batch = hashes[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                for key, vector in conn.execute(
                    f"SELECT content_hash, embedding FROM chunks WHERE content_hash IN ({placeholders})",
                    batch,
                ):
                    found[key] = np.frombuffer(vector, dtype=np.float32).tolist()
        return found

    def get_file_hashes(self, file_path: str) -> set[str]:
        with self._conn() as conn:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT content_hash FROM chunks WHERE file_path = ?", (file_path,)
                )
            }

    def delete_by_filepath(self, file_path: str):
        """Remove all chunks for a file (used before re-indexing a modified file)."""
        with self._conn() as conn:
            conn.execute("DELETE FROM chunks WHERE file_path = ?", (file_path,))
            conn.commit()

    def clear(self):
        """Remove all chunks from the vector database (used for force reindex)."""
        with self._conn() as conn:
            conn.execute("DELETE FROM chunks")
            conn.commit()

    # ------------------------------------------------------------------
    # Read path — exact cosine scan
    # ------------------------------------------------------------------

    def search(
        self, embedding: List[float], top_k: int = 10, query: str = ""
    ) -> List[Dict[str, Any]]:
        """
        Vectorised cosine-similarity search.
        Load embeddings into a NumPy matrix and compute cosine similarities,
        then combine them with keyword relevance when a query is provided.
        """
        query_vec = np.array(embedding, dtype=np.float32)
        if (
            query_vec.ndim != 1
            or not query_vec.size
            or not np.isfinite(query_vec).all()
        ):
            raise ValueError("Query embedding must be a nonempty finite vector.")
        q_norm = np.linalg.norm(query_vec)
        if q_norm == 0:
            return []
        query_vec /= q_norm

        with self._conn() as conn:
            rows = conn.execute(
                "SELECT symbol_name, chunk_text, file_path, "
                "line_start, line_end, language, embedding FROM chunks"
            ).fetchall()

        if not rows:
            return []

        if any(len(row[6]) != query_vec.size * 4 for row in rows):
            raise ValueError(
                "Embedding model dimensions changed. Re-index the workspace."
            )

        # Stack all embedding blobs into one matrix
        emb_matrix = np.frombuffer(
            b"".join(r[6] for r in rows), dtype=np.float32
        ).reshape(len(rows), -1)

        norms = np.linalg.norm(emb_matrix, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        scores = (emb_matrix / norms) @ query_vec  # (N,) cosine scores
        if query.strip():
            scores = self._hybrid_scores(rows, scores, query)

        top_indices = np.argsort(scores)[::-1][:top_k]

        return [
            {
                "symbol_name": rows[i][0],
                "chunk_text": rows[i][1],
                "file_path": rows[i][2],
                "line_start": rows[i][3],
                "line_end": rows[i][4],
                "language": rows[i][5],
                "score": float(scores[i]),
            }
            for i in top_indices
        ]

    @staticmethod
    def _hybrid_scores(rows, semantic, query):
        stop_words = {
            "a",
            "an",
            "the",
            "is",
            "are",
            "where",
            "does",
            "do",
            "find",
            "show",
            "me",
            "all",
            "in",
            "of",
            "to",
            "and",
            "for",
            "how",
            "which",
        }

        def tokens(text):
            text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
            return set(re.findall(r"[a-z0-9]+", text.lower())) - stop_words

        wanted = tokens(query)
        if not wanted:
            return np.clip(semantic, 0, 1)
        scores = []
        lowered = query.lower()
        for row, cosine in zip(rows, semantic):
            symbol, text, file = row[:3]
            symbol, text, file = symbol or "", text or "", file or ""
            names = tokens(symbol + " " + file)
            lexical = (
                0.7 * len(wanted & names) + 0.3 * len(wanted & tokens(text))
            ) / len(wanted)
            cosine = max(0.0, min(1.0, float(cosine)))
            score = 0.75 * cosine + 0.25 * lexical
            if symbol and re.search(
                r"(?<!\w)" + re.escape(symbol.lower()) + r"(?!\w)", lowered
            ):
                score = max(score, 0.85 + 0.15 * cosine)
            filename = file.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if filename and re.search(
                r"(?<!\w)" + re.escape(filename) + r"(?!\w)", lowered
            ):
                score = max(score, 0.9 + 0.1 * cosine)
            scores.append(min(1.0, score))
        return np.asarray(scores)

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]


# Process-wide cache: one LocalVectorDB per database directory.
_db_instance: Optional[LocalVectorDB] = None
_workspace_dbs: dict[str, LocalVectorDB] = {}
_db_lock = threading.Lock()


def canonical_repo(repo_path: str) -> str:
    return os.path.normcase(os.path.realpath(repo_path))


def get_db(repo_path: str | None = None) -> LocalVectorDB:
    global _db_instance
    with _db_lock:
        if repo_path is None:
            if _db_instance is None:
                _db_instance = LocalVectorDB()
            return _db_instance
        repo_path = canonical_repo(repo_path)
        key = hashlib.sha256(repo_path.encode()).hexdigest()
        directory = Path(Settings.VECTORAI_DB_PATH).resolve() / "workspaces" / key
        cache_key = str(directory)
        if cache_key not in _workspace_dbs:
            db_file = directory / "codelens.db"
            # Preserve the legacy DB, copying it only for its recorded owner.
            if not db_file.exists():
                base = Path(Settings.VECTORAI_DB_PATH).resolve()
                sidecar = base.parent / "last_repo.json"
                try:
                    state = json.loads(sidecar.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    state = {}
                if (
                    state.get("repo_path")
                    and canonical_repo(state["repo_path"]) == repo_path
                    and (base / "codelens.db").exists()
                ):
                    directory.mkdir(parents=True, exist_ok=True)
                    # sqlite3's context manager only commits; close handles explicitly
                    # so Windows does not keep the database files locked.
                    with (
                        closing(sqlite3.connect(base / "codelens.db")) as source,
                        closing(sqlite3.connect(db_file)) as target,
                    ):
                        source.backup(target)
                    migrated = LocalVectorDB(str(directory))
                    migrated.update_state(**state)
            _workspace_dbs[cache_key] = LocalVectorDB(str(directory))
            _workspace_dbs[cache_key].update_state(repo_path=repo_path)
        return _workspace_dbs[cache_key]


def indexed_workspaces() -> list[str]:
    paths = []
    for db_file in (Path(Settings.VECTORAI_DB_PATH) / "workspaces").glob(
        "*/codelens.db"
    ):
        state = LocalVectorDB(str(db_file.parent)).get_state()
        if state.get("last_indexed") and state.get("repo_path"):
            paths.append(state["repo_path"])
    return paths
