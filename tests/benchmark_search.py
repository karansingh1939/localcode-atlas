"""Run with python -m tests.benchmark_search; writes isolated search measurements."""

import argparse
import hashlib
import json
import os
import platform
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from ollama import Client

from backend.config import Settings
from backend.exclusions import Exclusions
from backend.indexer import SUPPORTED_EXTENSIONS, chunk_file
from backend.vector_store import LocalVectorDB


ROOT = Path(__file__).resolve().parents[1]


def latency(samples):
    return {
        "samples_ms": samples,
        "p50_ms": float(np.percentile(samples, 50)),
        "p95_ms": float(np.percentile(samples, 95)),
    }


def corpus(repo=None):
    root = repo.resolve() if repo else ROOT
    if repo is None:
        files = sorted(ROOT.glob("backend/*.py")) + sorted(
            ROOT.glob("extension/src/*.ts")
        )
    else:
        exclusions = Exclusions(str(root), [], Settings.VECTORAI_DB_PATH)
        files = []
        for current, dirs, names in os.walk(root):
            dirs[:] = [
                name
                for name in dirs
                if not exclusions.ignored(str(Path(current) / name), directory=True)
            ]
            files.extend(
                Path(current) / name
                for name in names
                if Path(name).suffix in SUPPORTED_EXTENSIONS
                and not exclusions.ignored(str(Path(current) / name))
            )
        files.sort()
    chunks = [chunk for file in files for chunk in chunk_file(str(file), str(root))]
    for chunk in chunks:
        chunk["file_path"] = chunk["file_path"].replace("\\", "/")
    return files, chunks


def scale_search(directory, chunks, cases, sizes, repeats):
    """Repeat real chunk text with seeded random vectors; measures speed only."""
    measurements = []
    for size in sizes:
        db = LocalVectorDB(str(directory / f"scale-{size}"))
        rng = np.random.default_rng(20261014)
        for start in range(0, size, 256):
            metadata = [
                {
                    **chunks[i % len(chunks)],
                    "file_path": f"copy-{i // len(chunks)}/"
                    + chunks[i % len(chunks)]["file_path"],
                    "content_hash": f"scale-{i}",
                }
                for i in range(start, min(start + 256, size))
            ]
            db.batch_upsert(
                metadata, rng.standard_normal((len(metadata), 768)).tolist()
            )
        vector = rng.standard_normal(768).tolist()
        entry = {"chunks": db.count(), "db_bytes": Path(db.db_file).stat().st_size}
        for mode in ("vector_only", "hybrid"):
            query = cases[0]["query"] if mode == "hybrid" else ""
            start = time.perf_counter()
            assert len(db.search(vector, top_k=5, query=query)) == min(5, size)
            first_ms = (time.perf_counter() - start) * 1000
            samples = []
            for i in range(repeats):
                query = cases[i % len(cases)]["query"] if mode == "hybrid" else ""
                start = time.perf_counter()
                db.search(vector, top_k=5, query=query)
                samples.append((time.perf_counter() - start) * 1000)
            entry[mode] = {"first_query_ms": first_ms, **latency(samples)}
        measurements.append(entry)
        print(json.dumps(entry), flush=True)
    return measurements


def relevance(directory, chunks, cases):
    """Evaluate fixed navigation targets with real Ollama embeddings."""
    for case in cases:
        assert any(
            chunk["file_path"] == case["file"]
            and (not case["symbols"] or chunk["symbol_name"] in case["symbols"])
            for chunk in chunks
        ), f"Missing navigation target: {case}"
    db = LocalVectorDB(str(directory / "relevance"))
    with Client(host=Settings.OLLAMA_HOST, timeout=120) as client:
        model = client.show(Settings.EMBED_MODEL)
        for start in range(0, len(chunks), Settings.EMBED_BATCH_SIZE):
            batch = chunks[start : start + Settings.EMBED_BATCH_SIZE]
            response = client.embed(
                model=Settings.EMBED_MODEL,
                input=[f"search_document: {c['chunk_text']}" for c in batch],
                truncate=False,
            )
            db.batch_upsert(batch, response.embeddings)
            print(
                f"Embedded {min(start + len(batch), len(chunks))}/{len(chunks)} chunks",
                flush=True,
            )
        queries = []
        for case in cases:
            start = time.perf_counter()
            vector = client.embed(
                model=Settings.EMBED_MODEL,
                input=f"search_query: {case['query']}",
                truncate=False,
            ).embeddings[0]
            embed_ms = (time.perf_counter() - start) * 1000
            start = time.perf_counter()
            results = db.search(vector, top_k=5, query=case["query"])
            search_ms = (time.perf_counter() - start) * 1000
            rank = next(
                (
                    i
                    for i, result in enumerate(results, 1)
                    if result["file_path"] == case["file"]
                    and (
                        not case["symbols"] or result["symbol_name"] in case["symbols"]
                    )
                ),
                None,
            )
            entry = {
                **case,
                "rank_at_5": rank,
                "embed_ms": embed_ms,
                "search_ms": search_ms,
                "top_5": [
                    {
                        key: result[key]
                        for key in ("file_path", "symbol_name", "line_start", "score")
                    }
                    for result in results
                ],
            }
            queries.append(entry)
            print(f"rank={rank}: {case['query']}", flush=True)
    return {
        "model": Settings.EMBED_MODEL,
        "model_details": model.details.model_dump(mode="json"),
        "chunks": db.count(),
        "hit_at_1": sum(q["rank_at_5"] == 1 for q in queries) / len(queries),
        "hit_at_5": sum(q["rank_at_5"] is not None for q in queries) / len(queries),
        "mrr_at_5": sum(1 / q["rank_at_5"] if q["rank_at_5"] else 0 for q in queries)
        / len(queries),
        "queries": queries,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000, 50000])
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--relevance", action="store_true")
    parser.add_argument(
        "--repo", type=Path, help="Read another repository, honoring indexer exclusions"
    )
    parser.add_argument(
        "--queries", type=Path, help="Annotated navigation queries for --repo"
    )
    parser.add_argument(
        "--skip-scale", action="store_true", help="Evaluate only the actual corpus"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeats < 2 or any(size < 1 for size in args.sizes):
        parser.error("Use positive sizes and at least two repeats.")
    if args.repo and (not args.repo.is_dir() or not args.queries):
        parser.error(
            "An existing --repo directory requires --queries for that repository."
        )
    root = args.repo.resolve() if args.repo else ROOT
    files, chunks = corpus(args.repo)
    if not chunks:
        parser.error("No supported source chunks found.")
    cases = json.loads(
        (args.queries or ROOT / "tests/search_queries.json").read_text(encoding="utf-8")
    )
    if not cases:
        parser.error("At least one annotated query is required.")
    report = {
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "cpu": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "python": platform.python_version(),
        "numpy": np.__version__,
        "corpus": root.name,
        "source_files": [file.relative_to(root).as_posix() for file in files],
        "source_sha256": {
            file.relative_to(root).as_posix(): hashlib.sha256(
                file.read_bytes()
            ).hexdigest()
            for file in files
        },
        "source_chunks": len(chunks),
        "scale_method": "Real source text repeated under unique paths, seeded random 768-dimensional vectors. Serial local DB searches; no Ollama or HTTP. OS cache not cleared; warm p50/p95 follow one first query. No relevance claims for scale data.",
    }
    with tempfile.TemporaryDirectory(prefix="localcode-benchmark-") as temp:
        directory = Path(temp)
        if not args.skip_scale:
            report["scale"] = scale_search(
                directory, chunks, cases, args.sizes, args.repeats
            )
        if args.relevance:
            report["relevance"] = relevance(directory, chunks, cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
