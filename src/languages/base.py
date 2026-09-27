"""Language-agnostic building blocks for the tree-sitter configurations of individual languages.

Each language provides a LanguageConfig that tells the generic extraction logic
how to find functions, classes, docstrings, imports, and references in the AST.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tree_sitter import Node


def node_text(node: Node) -> str:
    """Decode node text, asserting it is not None (always true for parsed nodes)."""
    assert node.text is not None
    return node.text.decode()


def walk(node: Node) -> Iterator[Node]:
    """Yield node and all its descendants in source order (iterative, so deep trees can't hit the recursion limit)."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


@dataclass
class ImportInfo:
    """A single import found in a source file."""

    local_name: str  # name as used in this file
    qualified_name: str  # resolved dotted path (relative imports resolved)


def module_segment(name: str) -> str:
    """Map a file or directory name to its segment of a dotted module path.

    Everything from the first dot is dropped, so `quizFlow.svelte.ts` and an import of
    `./quizFlow.svelte` both map to `quizFlow`.
    """
    return name.split(".", maxsplit=1)[0]


@dataclass
class LanguageConfig:
    """Language-specific tree-sitter knowledge for unit extraction."""

    extensions: frozenset[str]
    # Filenames (without extension) that represent the directory itself,
    # e.g. "__init__" in Python, "index" in JS/TS, "mod" in Rust.
    package_filenames: frozenset[str]
    definition_kinds: dict[str, str]  # definition node type -> unit kind (e.g. "function", "class")
    name_field: str  # field of a definition node that holds its name
    # Top-level node -> the definition it wraps (e.g. a decorated function), or None if it isn't a unit
    unwrap_definition: Callable[[Node], Node | None]
    is_private: Callable[[str, Node], bool]  # (name, definition_node) -> is private?
    is_entry_point: Callable[[Node], bool]  # top-level node -> is it a script entry point block?
    docstring_extractor: Callable[[Node], str | None]  # definition node (or file root for file units) -> docstring
    import_extractor: Callable[[Node, str, bool], list[ImportInfo]]  # (module root, module_path, is_package)
    ref_extractor: Callable[[Node], list[str]]  # names / dotted chains referenced in a definition node
    # Raw file content -> the source to parse (e.g. only the <script> blocks of a Svelte component)
    preprocess: Callable[[bytes], bytes] | None = None
    # If set, each file is a single unit of this kind (e.g. a "component"), named after the file
    file_unit_kind: str | None = None
