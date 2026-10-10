import pytest

from backend.code_parser import extract_chunks, _sliding_window
from backend.vector_store import LocalVectorDB
from backend.config import Settings


def test_tree_sitter_parser_python():
    sample_code = """
def calculate_sum(a: int, b: int) -> int:
    return a + b

class MathService:
    def multiply(self, x: int, y: int) -> int:
        return x * y
"""
    chunks = extract_chunks("sample.py", sample_code)
    assert len(chunks) >= 2
    symbols = {c["symbol_name"] for c in chunks}
    assert "calculate_sum" in symbols
    for c in chunks:
        assert c["line_start"] > 0
        assert c["line_end"] >= c["line_start"]
        assert len(c["chunk_text"]) > 0


def test_tree_sitter_parser_typescript():
    sample_ts = """
export function authenticateUser(token: string): boolean {
    return token.length > 0;
}

export class AuthService {
    login(): void {}
}
"""
    chunks = extract_chunks("auth.ts", sample_ts)
    assert len(chunks) >= 2
    symbols = {c["symbol_name"] for c in chunks}
    assert "authenticateUser" in symbols


def test_sliding_window_fallback():
    content = "\n".join([f"Line {i}: some descriptive config data" for i in range(100)])
    chunks = _sliding_window("config.txt", content, "unknown", window=40, overlap=10)
    assert len(chunks) > 1
    assert chunks[0]["line_start"] == 1


def test_local_vector_db_operations(tmp_path, monkeypatch):
    db_dir = str(tmp_path / "test_db")
    monkeypatch.setattr(Settings, "VECTORAI_DB_PATH", db_dir)

    db = LocalVectorDB()
    assert db.count() == 0

    meta = [
        {
            "symbol_name": "func_a",
            "chunk_text": "def func_a(): pass",
            "file_path": "a.py",
            "line_start": 1,
            "line_end": 2,
            "language": "python",
            "content_hash": "hash_a",
        },
        {
            "symbol_name": "func_b",
            "chunk_text": "def func_b(): pass",
            "file_path": "b.py",
            "line_start": 1,
            "line_end": 2,
            "language": "python",
            "content_hash": "hash_b",
        },
    ]
    vec_a = [1.0] + [0.0] * 767
    vec_b = [0.0, 1.0] + [0.0] * 766
    db.batch_upsert(meta, [vec_a, vec_b])

    assert db.count() == 2
    existing_hashes = db.get_existing_hashes(["hash_a", "hash_c"])
    assert existing_hashes == ["hash_a"]

    results = db.search(vec_a, top_k=1)
    assert len(results) == 1
    assert results[0]["symbol_name"] == "func_a"

    db.clear()
    assert db.count() == 0


@pytest.mark.parametrize("vectors", [[], [[1.0, float("nan")]], [[]]])
def test_invalid_embeddings_do_not_replace_existing_file(
    tmp_path, monkeypatch, vectors
):
    monkeypatch.setattr(Settings, "VECTORAI_DB_PATH", str(tmp_path / "db"))
    db = LocalVectorDB()
    meta = [{"file_path": "keep.py", "content_hash": "keep"}]
    db.batch_upsert(meta, [[1.0, 0.0]])
    with pytest.raises(ValueError):
        db.batch_upsert(meta, vectors, replace_file_path="keep.py")
    assert db.count() == 1
    assert db.search([1.0, 0.0])[0]["score"] == pytest.approx(1.0)
