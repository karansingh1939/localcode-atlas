# Search and installation verification

Measured on 14 October 2026 on Windows 11, Intel Family 6 Model 154, 20 logical
CPUs, Python 3.12.6 and NumPy 2.5.3. Raw timings, result rankings, model details
and source SHA-256 hashes are in [search-2026-10-14.json](search-2026-10-14.json).

## Search latency

| Indexed chunks | Vector p50 | Vector p95 | Hybrid p50 | Hybrid p95 |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 12.70 ms | 19.72 ms | 59.67 ms | 70.18 ms |
| 10,000 | 235.36 ms | 243.81 ms | 1,134.89 ms | 1,181.83 ms |
| 50,000 | 1,157.48 ms | 1,251.08 ms | 5,638.82 ms | 5,832.28 ms |

Each mode uses 20 serial searches after one first query, returning five results.
Hybrid queries cycle through the fixed navigation query set. Source text comes
from this project's backend and extension, repeated under unique paths. Vectors
are seeded random 768-dimensional values. Each size has its own temporary SQLite
database, populated through the production storage method. The 50,000-chunk
database is about 223 MiB.

These measurements include SQLite loading, vector normalization, scoring,
sorting and, for hybrid queries, lexical ranking. They exclude embedding time,
HTTP, explanations, UI rendering, indexing and concurrent updates. OS caches were
not cleared; the first-query timings in the JSON are not cold-disk measurements.
The machine was otherwise left idle during latency collection. Twenty samples
give an exploratory p95, not a production service-level guarantee.

The current search path reloads every embedding and retokenizes every chunk on
each request. Hybrid ranking adds about 4.48 seconds to the median at 50,000
chunks in this run. Large indexes therefore have a material latency limitation.
No ranking weights or search algorithms were changed for this benchmark.

## Navigation relevance

The production parser produced 154 chunks from ten backend/extension source
files. All chunks and queries used the installed `nomic-embed-text` model
(137M parameters, F16), with the production `search_document:` and `search_query:`
prefixes. Embeddings were not mocked. Queries were authored for this check,
rather than collected from user search logs. The query set fixes expected files
and symbols before evaluation; split chunks can occupy separate ranking positions.
Filename queries accept any chunk in the named file.

| Query type | Queries | Intended target first | Intended target in top five |
| --- | ---: | ---: | ---: |
| Exact symbol | 4 | 4 | 4 |
| Filename/path | 2 | 2 | 2 |
| Natural-language task | 14 | 4 | 10 |
| Total | 20 | 10 (50%) | 16 (80%) |

Mean reciprocal rank at five is **0.5933**. For the real corpus, median query
embedding time was 44.78 ms and median local search time was 20.51 ms. Combined
embedding plus search had p50 67.10 ms and p95 90.29 ms, excluding HTTP/UI. The
embedding model was already warm from indexing.

Four queries missed their annotated targets in the top five:

- Nested gitignore rules and negated patterns.
- Rejecting searches after an embedding-model change.
- Streaming indexing progress through server-sent events.
- Removing indexed chunks after source-file deletion.

This is a small, project-specific baseline. The scaled corpus repeats source
text and uses synthetic vectors; it does not establish relevance or end-to-end
latency on a genuinely large external repository. Multiple languages, unrelated
repositories, negative queries, user-authored queries and concurrent load need
broader evaluation before making general quality claims.

## Reproduce

```powershell
npm run benchmark:search -- --relevance
```

Omit `--relevance` for a local storage-only run without Ollama. To change the
scale, pass `--sizes 1000 10000 50000 --repeats 20`. Results go to
`benchmarks/search-latest.json`; temporary databases are removed afterward.
Production workspace indexes are untouched. The benchmark smoke tests validate
navigation targets, exact seeded chunk counts and percentile calculations.

For a separate repository, supply annotated queries and use the real corpus:

```powershell
npm run benchmark:search -- --repo "C:\path\to\repo" --queries my_queries.json --skip-scale --relevance --output benchmarks/my-repo.json
```

The external repository is read without importing or running its code. Discovery
uses the same supported extensions and Git-style exclusions as indexing. The
query JSON contains `kind`, `query`, `file` (relative path with forward slashes),
and `symbols` (accepted symbol names, or an empty list for a filename query).

## Startup and explanations

The legacy-console regression failed on the original emoji banner with
`UnicodeEncodeError`. Startup now uses plain messages, and both online/offline
Ollama startup paths pass with strict `cp1252` output. The startup script still
enables UTF-8 for normal use.

The real-Ollama integration passed indexing, search, watcher updates, workspace
isolation and a `llama3.2` explanation delivered through the compiled extension
search bridge. The fake-Ollama integration additionally verifies a missing
explanation model returns the unavailable message while preserving results.
These tests exercise the extension bridge with a VS Code stub; they do not open
the real editor UI.

At the time of this benchmark (14 October 2026), the full regression run passed
44 backend tests plus extension startup/panel checks; later releases add more tests. Lint and formatting checks passed. `npm run test:install` built the VSIX,
created a fresh Python environment, installed locked dependencies and the
packaged backend, passed `pip check`, and passed the fake-Ollama integration,
including explanations and missing-model fallback. Python and Ollama themselves
remain external installation prerequisites.
