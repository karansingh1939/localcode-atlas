"""Extract code symbols with tree-sitter and fall back to line windows."""

import os
from tree_sitter import Language, Parser

import tree_sitter_python
import tree_sitter_typescript
import tree_sitter_javascript
import tree_sitter_go
import tree_sitter_rust
import tree_sitter_java

# ---------------------------------------------------------------------------
# Language registry
# tree-sitter 0.22+ API: Language takes ONE argument (the capsule pointer)
# We store (Language object, explicit name string) because Language.name
# returns None for some grammars in 0.25.x (typescript, java, etc.)
# ---------------------------------------------------------------------------
_LANG_DEFS: dict[str, tuple[Language, str]] = {
    ".py": (Language(tree_sitter_python.language()), "python"),
    ".ts": (Language(tree_sitter_typescript.language_typescript()), "typescript"),
    ".tsx": (Language(tree_sitter_typescript.language_tsx()), "tsx"),
    ".js": (Language(tree_sitter_javascript.language()), "javascript"),
    ".jsx": (Language(tree_sitter_javascript.language()), "javascript"),
    ".go": (Language(tree_sitter_go.language()), "go"),
    ".rs": (Language(tree_sitter_rust.language()), "rust"),
    ".java": (Language(tree_sitter_java.language()), "java"),
}

# Optional C/C++
try:
    import tree_sitter_cpp

    _LANG_DEFS[".cpp"] = (Language(tree_sitter_cpp.language()), "cpp")
    _LANG_DEFS[".cc"] = (Language(tree_sitter_cpp.language()), "cpp")
    _LANG_DEFS[".h"] = (Language(tree_sitter_cpp.language()), "cpp")
except ImportError:
    pass

# Public: just the extension set (for the file walker)
LANGUAGES: dict[str, Language] = {ext: lang for ext, (lang, _) in _LANG_DEFS.items()}


# ---------------------------------------------------------------------------
# Node types we consider "semantic units" worth indexing per language
# ---------------------------------------------------------------------------
TARGET_NODE_TYPES: dict[str, set[str]] = {
    "python": {"function_definition", "class_definition", "decorated_definition"},
    "typescript": {
        "function_declaration",
        "method_definition",
        "class_declaration",
        "arrow_function",
        "interface_declaration",
        "type_alias_declaration",
    },
    "tsx": {
        "function_declaration",
        "method_definition",
        "class_declaration",
        "arrow_function",
        "interface_declaration",
    },
    "javascript": {
        "function_declaration",
        "method_definition",
        "class_declaration",
        "arrow_function",
    },
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "rust": {"function_item", "impl_item", "struct_item", "mod_item"},
    "java": {
        "method_declaration",
        "class_declaration",
        "constructor_declaration",
        "interface_declaration",
    },
    "cpp": {"function_definition", "class_specifier", "struct_specifier"},
}


def _get_symbol_name(node, source_bytes: bytes) -> str:
    """Extract identifier from an AST node."""
    name_node = node.child_by_field_name("name")
    if name_node:
        return source_bytes[name_node.start_byte : name_node.end_byte].decode(
            "utf-8", errors="replace"
        )

    if node.type == "decorated_definition":
        def_node = node.child_by_field_name("definition")
        if def_node:
            return _get_symbol_name(def_node, source_bytes)

    # Arrow function assigned to a variable_declarator
    if node.type == "arrow_function":
        parent = node.parent
        if parent and parent.type == "variable_declarator":
            name_n = parent.child_by_field_name("name")
            if name_n:
                return source_bytes[name_n.start_byte : name_n.end_byte].decode(
                    "utf-8", errors="replace"
                )

    for i in range(node.named_child_count):
        child = node.named_child(i)
        if child and child.type in {
            "identifier",
            "name",
            "type_identifier",
            "property_identifier",
        }:
            return source_bytes[child.start_byte : child.end_byte].decode(
                "utf-8", errors="replace"
            )

    return node.type


def extract_chunks(file_path: str, content: str) -> list[dict]:
    """
    Parse `content` with tree-sitter and return semantic chunk dicts.
    Falls back to sliding-window for unsupported or symbol-less files.
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in _LANG_DEFS:
        return _sliding_window(file_path, content, "unknown")

    language, lang_name = _LANG_DEFS[ext]  # explicit name — no more None
    source_bytes = content.encode("utf-8")
    parser = Parser(language)
    tree = parser.parse(source_bytes)

    target_types = TARGET_NODE_TYPES.get(lang_name, set())
    chunks: list[dict] = []

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        is_target = node.type in target_types

        # Arrow functions only count when assigned to a variable
        if is_target and node.type == "arrow_function":
            parent = node.parent
            is_target = bool(parent and parent.type == "variable_declarator")

        if is_target:
            symbol_name = _get_symbol_name(node, source_bytes)
            chunk_text = source_bytes[node.start_byte : node.end_byte].decode(
                "utf-8", errors="replace"
            )
            start_row = source_bytes[: node.start_byte].count(b"\n") + 1
            end_row = source_bytes[: node.end_byte].count(b"\n") + 1

            chunks.append(
                {
                    "file_path": file_path,
                    "line_start": start_row,
                    "line_end": end_row,
                    "symbol_name": symbol_name,
                    "language": lang_name,
                    "chunk_text": chunk_text,
                }
            )

        stack.extend(reversed(node.children))

    if not chunks and content.strip():
        return _sliding_window(file_path, content, lang_name)

    return chunks


def _sliding_window(
    file_path: str, content: str, lang_name: str, window: int = 40, overlap: int = 10
) -> list[dict]:
    """Line-based sliding-window fallback."""
    lines = content.splitlines()
    step = max(1, window - overlap)
    chunks = []
    for i in range(0, len(lines), step):
        chunk_lines = lines[i : i + window]
        text = "\n".join(chunk_lines).strip()
        if not text:
            continue
        chunks.append(
            {
                "file_path": file_path,
                "line_start": i + 1,
                "line_end": i + len(chunk_lines),
                "symbol_name": f"lines_{i + 1}_{i + len(chunk_lines)}",
                "language": lang_name,
                "chunk_text": text,
            }
        )
    return chunks
