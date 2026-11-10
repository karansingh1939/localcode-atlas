# LocalCode Atlas

Offline semantic code search for VS Code, using a local Python backend, SQLite,
NumPy and Ollama. Code and queries stay on your machine with the default settings.

## Windows setup

Install Python 3.12, Node.js 22 or newer and Ollama. From this project folder run:

```powershell
npm run setup
npm start
```

Setup installs Python and Node dependencies and downloads `nomic-embed-text`
if needed. For the optional AI explanation, also run `ollama pull llama3.2`.
Initial installation and model downloads need internet access; search works offline
afterward. Copy `.env.example` to `.env` to change the database directory, Ollama
host or models.

Open this folder in VS Code and press F5 with **Run LocalCode Atlas Extension** selected.
In the extension window, open the LocalCode Atlas sidebar and click **Re-index** before
searching. Results include a code preview, relevance score and **Jump to file**.
The file watcher updates the index when supported files are saved, moved or deleted.
API documentation is available at <http://127.0.0.1:8000/docs>.
The startup banner uses plain text for legacy Windows consoles.

VS Code windows share a backend that stays running when a window closes, including
the last window. To stop it explicitly, find its process ID with
`Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8000 -State Listen`
and run `Stop-Process -Id <OwningProcess>`. Restart it after backend code or
environment changes. A packaged VSIX already contains compiled JavaScript;
its setup installs Python dependencies and models without rebuilding the extension.

## Checks and packaging

```powershell
npm test
npm run lint
npm run format:check
npm run test:integration
npm run test:install
npm run package
npm run benchmark:search -- --relevance
```

`npm test` builds the extension and runs mocked startup/panel tests and Python
regression tests. Integration starts an isolated backend on a temporary port and
uses your running Ollama service to test indexing, search and live updates. It does
not replace your workspace index. Packaging creates `localcode-atlas-0.2.0.vsix`.
The VSIX includes the backend and setup scripts but requires Python dependencies
on the target machine; it does not bundle Python or Ollama.

`npm run test:install` unpacks the VSIX into a temporary folder, creates a new
Python environment, installs locked dependencies, and tests the packaged backend
and extension search bridge against a local fake Ollama server. This checks a
fresh package installation without downloading models. The real VS Code editor
UI is covered by the separate `tests/extension_host.cjs` smoke test.

Integration also checks AI explanations and missing-model fallback when using
`CODELENS_FAKE_OLLAMA=1`. To check explanations with the real local model, run
`$env:CODELENS_TEST_EXPLAIN='1'; npm run test:integration` in PowerShell.

The search benchmark measures local database latency at 1,000, 10,000 and 50,000
chunks using repeated source text and seeded synthetic vectors. `--relevance`
adds 20 fixed navigation queries over this project's actual backend and extension
source with real Ollama embeddings. It writes `benchmarks/search-latest.json` and
uses temporary databases. See [measurements and limits](benchmarks/README.md).
It does not overwrite your workspace index.

GitHub Actions runs lint, formatting, regression tests, dependency auditing,
fake-Ollama integration and the fresh package installation check on Windows.

## Workspace indexes and exclusions

Each repository has its own database under `.vectorai_db/workspaces/`. Opening
several VS Code windows preserves their separate results and live watchers. The
legacy index is copied to its recorded workspace on first startup; the original
database remains intact. Full index requests run one at a time; other indexed
workspaces can still be searched while one is being indexed.

Indexing respects root and nested `.gitignore` files, `.codelensignore` files, and
the VS Code setting `codelens.excludePatterns`. Patterns use
[PathSpec's Git ignore semantics](https://python-path-specification.readthedocs.io/en/latest/readme.html),
including negation. Built-in dependency/build directories and the index directory
are always excluded. Re-index after changing VS Code exclusion settings; changes
to ignore files refresh the watcher automatically.

For example, put `generated/` and `**/fixtures/**` on separate lines in
`.codelensignore`, or configure the same patterns in VS Code settings:

```json
{ "codelens.excludePatterns": ["generated/", "**/fixtures/**"] }
```

Re-indexing is incremental. Unchanged chunks retain their embeddings; new chunks
use [Ollama's batch embedding API](https://github.com/ollama/ollama/blob/main/docs/capabilities/embeddings.mdx).
`EMBED_BATCH_SIZE` defaults to 32. Saves are debounced by
`WATCH_DEBOUNCE_SECONDS`, which defaults to 0.35 seconds. File replacement remains
atomic if any embedding batch fails.

Search combines semantic similarity with symbol, filename and code keywords;
exact symbol and filename matches receive a ranking boost. Scores represent hybrid
relevance on a 0–1 scale.

## Current limits and useful improvements

- Large files are split into chunks of at most 1,200 characters. Supported AST
  languages are Python, JavaScript/JSX, TypeScript/TSX, Go, Rust and Java. Markdown,
  text, TOML and YAML use line windows. C++ requires its optional grammar.
- Each query loads and scans all vectors and tokenizes source text for hybrid
  ranking. The [search benchmark](benchmarks/README.md) records the resulting
  scaling cost; matrix/token caching or an approximate index remain future work.
- Windows setup and CI are provided; macOS/Linux need equivalent setup scripts
  and platform-specific installation checks. Ranking weights should be evaluated
  against a representative set of real code-navigation queries before tuning them.

## Identity and compatibility

LocalCode Atlas is a private project created and maintained by Karan Singh,
released under the MIT license in `LICENSE`. It includes workspace isolation,
ignore rules, batch embeddings, incremental updates, hybrid ranking, and
installation/CI checks.

The extension retains the `codelens.*` command and settings identifiers,
`.codelensignore`, `VECTORAI_DB_PATH`, `.vectorai_db`, `codelens.db`, the
`vectorai` field of `/health`, and the `CODELENS_*` test environment variables for
compatibility. These internal names do not indicate a Git remote connection.
The local extension packaging publisher ID remains `karansingh1939`; it is
separate from the GitHub owner `karansingh-1939`.

This checkout has fresh history. The original checkout and its indexes are
retained separately; this folder creates its own indexes after setup.
Do not commit databases, embeddings, saved workspace paths, real environment
files, credentials, or generated packages. A private GitHub repository is
configured separately from the npm package's `private` flag.
