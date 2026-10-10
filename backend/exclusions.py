"""Shared Git-style exclusions for directory walks and live updates."""

from pathlib import Path

from pathspec import GitIgnoreSpec


IGNORE_DIRS = {
    "node_modules",
    ".git",
    "vendor",
    "dist",
    "__pycache__",
    "build",
    ".venv",
    "venv",
    ".vectorai_db",
    ".pytest_cache",
    ".ruff_cache",
    "out",
}


class Exclusions:
    def __init__(self, root: str, patterns: list[str], db_path: str):
        self.root = Path(root).resolve()
        self.database = Path(db_path).resolve()
        self.extra = GitIgnoreSpec.from_lines(patterns)
        self._cache = {}

    def _spec(self, file: Path):
        try:
            stat = file.stat()
        except FileNotFoundError:
            return None
        version = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(file)
        if cached is None or cached[0] != version:
            self._cache[file] = (
                version,
                GitIgnoreSpec.from_lines(
                    file.read_text(encoding="utf-8", errors="replace").splitlines()
                ),
            )
        return self._cache[file][1]

    def _matches(self, path: Path, directory: bool) -> bool:
        ignored = False
        parents = [self.root]
        relative_parent = path.parent.relative_to(self.root)
        for part in relative_parent.parts:
            parents.append(parents[-1] / part)
        for scope in parents:
            relative = path.relative_to(scope).as_posix() + ("/" if directory else "")
            for name in (".gitignore", ".codelensignore"):
                spec = self._spec(scope / name)
                if spec is not None:
                    match = spec.check_file(relative)
                    if match.include is not None:
                        ignored = match.include
        relative = path.relative_to(self.root).as_posix() + ("/" if directory else "")
        match = self.extra.check_file(relative)
        return match.include if match.include is not None else ignored

    def ignored(self, path: str, directory: bool = False) -> bool:
        target = Path(path).resolve()
        if not target.is_relative_to(self.root) or target.is_relative_to(self.database):
            return True
        relative = target.relative_to(self.root)
        if any(
            part in IGNORE_DIRS or part.endswith(".egg-info") for part in relative.parts
        ):
            return True
        ancestor = self.root
        for part in relative.parts[:-1]:
            ancestor /= part
            if self._matches(ancestor, True):
                return True
        return self._matches(target, directory) if relative.parts else False
