import json

import pytest

from tests.benchmark_search import ROOT, corpus, latency, scale_search


def test_benchmark_targets_exist_and_scale_counts_are_exact(tmp_path):
    _, chunks = corpus()
    cases = json.loads((ROOT / "tests/search_queries.json").read_text(encoding="utf-8"))
    assert len(cases) == 20
    for case in cases:
        assert any(
            c["file_path"] == case["file"]
            and (not case["symbols"] or c["symbol_name"] in case["symbols"])
            for c in chunks
        ), case
    results = scale_search(tmp_path, chunks, cases, [3, 7], repeats=2)
    assert [result["chunks"] for result in results] == [3, 7]
    assert all(len(r["hybrid"]["samples_ms"]) == 2 for r in results)


def test_benchmark_percentiles():
    result = latency([1, 2, 3, 4, 5])
    assert result["p50_ms"] == 3
    assert result["p95_ms"] == pytest.approx(4.8)


def test_external_corpus_respects_repository_exclusions(tmp_path):
    (tmp_path / "app.py").write_text("def target(): return 1\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("def ignored(): pass\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    dependencies = tmp_path / "node_modules"
    dependencies.mkdir()
    (dependencies / "dep.py").write_text("def dependency(): pass\n", encoding="utf-8")
    files, chunks = corpus(tmp_path)
    assert files == [tmp_path / "app.py"]
    assert [c["symbol_name"] for c in chunks] == ["target"]
    assert (tmp_path / "app.py").read_text(
        encoding="utf-8"
    ) == "def target(): return 1\n"
