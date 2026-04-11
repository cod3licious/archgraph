"""Language-specific tree-sitter configurations for unit extraction.

Each language provides a LanguageConfig that tells the generic extraction logic
how to find functions, classes, docstrings, imports, and call sites in the AST.
Only Python is fully implemented; others are extension points for the future.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tree_sitter import Node


def _text(node: Node) -> str:
    """Decode node text, asserting it is not None (always true for parsed nodes)."""
    assert node.text is not None
    return node.text.decode()


@dataclass
class ImportInfo:
    """A single import found in a source file."""

    local_name: str  # name as used in this file
    qualified_name: str  # resolved dotted path (relative imports resolved)


@dataclass
class LanguageConfig:
    """Language-specific tree-sitter knowledge for unit extraction."""

    extensions: frozenset[str]
    # Filenames (without extension) that represent the directory itself,
    # e.g. "__init__" in Python, "index" in JS/TS, "mod" in Rust.
    package_filenames: frozenset[str]
    function_node_types: frozenset[str]
    function_name_field: str
    class_node_types: frozenset[str]
    class_name_field: str
    is_private: Callable[[str, Node], bool]  # (name, definition_node) -> is private?
    docstring_extractor: Callable[[Node], str | None]
    import_extractor: Callable[[Node, str], list[ImportInfo]]
    call_extractor: Callable[[Node], list[str]]  # extract raw call names from a definition node


# Populated at runtime by register_languages()
LANGUAGE_CONFIGS: dict[str, tuple[Callable, LanguageConfig]] = {}


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def _python_extract_docstring(node: Node) -> str | None:
    """Extract docstring from a function_definition or class_definition node."""
    body = node.child_by_field_name("body")
    if body is None or not body.children:
        return None
    first = body.children[0]
    if first.type != "expression_statement" or not first.children:
        return None
    expr = first.children[0]
    if expr.type == "string":
        raw = _text(expr)
        # Strip triple quotes or single quotes
        for q in ('"""', "'''", '"', "'"):
            if raw.startswith(q) and raw.endswith(q):
                return raw[len(q) : -len(q)].strip()
    return None


def _python_extract_imports(node: Node, module_path: str) -> list[ImportInfo]:
    """Extract imports from a module-level AST node."""
    results: list[ImportInfo] = []
    for child in node.children:
        if child.type == "import_statement":
            # `import foo.bar` -> local_name="foo", qualified="foo"
            # (also "foo.bar" -> "foo.bar")
            for name_node in child.children:
                if name_node.type == "dotted_name":
                    full = _text(name_node)
                    results.append(ImportInfo(local_name=full.split(".")[0], qualified_name=full))
        elif child.type == "import_from_statement":
            _python_parse_from_import(child, module_path, results)
    return results


def _python_parse_from_import(node: Node, module_path: str, results: list[ImportInfo]) -> None:
    source_module = ""
    imported_names: list[str] = []
    is_star = False

    for child in node.children:
        if child.type == "dotted_name" and not source_module:
            source_module = _text(child)
        elif child.type == "relative_import":
            source_module = _resolve_relative_import(child, module_path)
        elif child.type == "dotted_name" and source_module:
            imported_names.append(_text(child))
        elif child.type == "wildcard_import":
            is_star = True

    if is_star or not source_module:
        return

    for name in imported_names:
        results.append(ImportInfo(local_name=name, qualified_name=f"{source_module}.{name}"))


def _resolve_relative_import(node: Node, module_path: str) -> str:
    """Resolve a relative import node to an absolute module path."""
    dots = 0
    suffix = ""
    for child in node.children:
        if child.type == "import_prefix":
            dots = len(_text(child))
        elif child.type == "dotted_name":
            suffix = _text(child)

    parts = module_path.split(".")
    # Go up `dots` levels (1 dot = parent package, 2 dots = grandparent, etc.)
    base_parts = parts[:-dots] if dots <= len(parts) else []
    if suffix:
        base_parts.append(suffix)
    return ".".join(base_parts)


def _python_extract_calls(node: Node) -> list[str]:
    """Extract raw call target names from a definition node (function or class)."""
    calls: list[str] = []
    _walk_calls(node, calls)
    return calls


def _walk_calls(node: Node, calls: list[str]) -> None:
    if node.type == "call":
        fn = node.child_by_field_name("function")
        if fn is not None:
            text = _text(fn)
            # Skip self.x() and cls.x() calls (intra-unit)
            if not text.startswith(("self.", "cls.")):
                calls.append(text)
    for child in node.children:
        _walk_calls(child, calls)


def _python_is_private(name: str, _node: Node) -> bool:
    """In Python, names starting with _ are private by convention."""
    return name.startswith("_")


def _make_python_config() -> LanguageConfig:
    return LanguageConfig(
        extensions=frozenset(["py"]),
        package_filenames=frozenset(["__init__"]),
        function_node_types=frozenset(["function_definition"]),
        function_name_field="name",
        class_node_types=frozenset(["class_definition"]),
        class_name_field="name",
        is_private=_python_is_private,
        docstring_extractor=_python_extract_docstring,
        import_extractor=_python_extract_imports,
        call_extractor=_python_extract_calls,
    )


# ---------------------------------------------------------------------------
# Register Languages
# ---------------------------------------------------------------------------


def register_languages() -> None:
    """Lazily import tree-sitter language modules and populate LANGUAGE_CONFIGS."""
    entries: list[tuple[str, str, Callable[[], LanguageConfig]]] = [
        ("py", "tree_sitter_python", _make_python_config),
    ]
    for ext, module_name, config_factory in entries:
        try:
            mod = __import__(module_name)
            LANGUAGE_CONFIGS[ext] = (mod.language, config_factory())
        except ImportError:
            pass
