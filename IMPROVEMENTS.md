# Improvement scope

1. Preserve independent workspace indexes and restore their watchers after restart.
   Migrate the legacy single-repository index without deleting its database.
2. Apply root/nested `.gitignore`, `.codelensignore`, and VS Code exclude patterns
   consistently to full indexing and watcher updates.
3. Embed bounded batches, retain atomic file replacement on failures, and debounce
   saves. Reuse unchanged chunks on incremental re-indexing.
4. Combine exact symbols, paths and keyword tokens with semantic ranking.
5. Run lint, formatting, backend and extension tests and VSIX packaging in CI.
   Verify a fresh installation with a local fake Ollama server; keep real Ollama
   integration as a separate explicit check.

Acceptance: regression tests for isolation/migration, nested/negated exclusions,
batch failures, debouncing and hybrid ranking; passing existing checks; successful
real Ollama integration and a rebuilt VSIX. Existing user changes remain intact.
