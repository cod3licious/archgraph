"""Language-specific tree-sitter configurations for unit extraction.

Each language provides a LanguageConfig that tells the generic extraction logic
how to find functions, classes, docstrings, imports, and references in the AST.
Only Python is fully implemented; others are extension points for the future.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tree_sitter import Node


def node_text(node: Node) -> str:
    """Decode node text, asserting it is not None (always true for parsed nodes)."""
    assert node.text is not None
    return node.text.decode()


def _walk(node: Node) -> Iterator[Node]:
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
    # Top-level node -> the definition it wraps (e.g. a decorated function), or the node itself
    unwrap_definition: Callable[[Node], Node | None]
    is_private: Callable[[str, Node], bool]  # (name, definition_node) -> is private?
    is_entry_point: Callable[[Node], bool]  # top-level node -> is it a script entry point block?
    docstring_extractor: Callable[[Node], str | None]
    import_extractor: Callable[[Node, str, bool], list[ImportInfo]]  # (module root, module_path, is_package)
    ref_extractor: Callable[[Node], list[str]]  # names / dotted chains referenced in a definition node


# Populated at runtime by register_languages()
LANGUAGE_CONFIGS: dict[str, tuple[Callable, LanguageConfig]] = {}


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def _python_unwrap_definition(node: Node) -> Node | None:
    """Return the definition wrapped by decorators; None for `@overload` stubs (only the implementation is a unit)."""
    if node.type != "decorated_definition":
        return node
    decorators = {node_text(d).removeprefix("@").strip() for d in node.children if d.type == "decorator"}
    return None if decorators & {"overload", "typing.overload"} else node.child_by_field_name("definition")


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
        raw = node_text(expr).lstrip("rRuUbBfF")
        for q in ('"""', "'''", '"', "'"):
            if raw.startswith(q) and raw.endswith(q):
                return inspect.cleandoc(raw[len(q) : -len(q)])
    return None


def _resolve_relative_import(node: Node, module_path: str, is_package: bool) -> str:
    """Resolve a relative import node to an absolute module path."""
    dots = 0
    suffix = ""
    for child in node.children:
        if child.type == "import_prefix":
            dots = len(node_text(child))
        elif child.type == "dotted_name":
            suffix = node_text(child)

    parts = module_path.split(".")
    # 1 dot = the containing package; a package file's (__init__.py) module path already is that package
    levels_up = dots - 1 if is_package else dots
    base_parts = parts[: max(len(parts) - levels_up, 0)]
    if suffix:
        base_parts.append(suffix)
    return ".".join(base_parts)


def _python_import_info(name_node: Node, source_module: str | None) -> ImportInfo:
    """Map an imported name (dotted_name or aliased_import) to its local and qualified name.

    source_module is None for plain `import` statements, where `import a.b.c` binds
    `a` (so `a.b.c.f()` resolves to `a.b.c.f`).
    """
    alias = name_node.child_by_field_name("alias")
    target = node_text(name_node.child_by_field_name("name") or name_node)
    if alias is not None:
        local_name = node_text(alias)
    elif source_module is None:
        local_name = target = target.split(".")[0]
    else:
        local_name = target
    qualified_name = f"{source_module}.{target}" if source_module else target
    return ImportInfo(local_name=local_name, qualified_name=qualified_name)


def _python_extract_imports(node: Node, module_path: str, is_package: bool) -> list[ImportInfo]:
    """Extract imports from anywhere in a module AST.

    Imports nested in functions, try/except or `if TYPE_CHECKING:` blocks are treated
    as module-wide, so different scopes importing different things under the same
    local name may resolve to the wrong one (the last import wins).
    """
    results: list[ImportInfo] = []
    for child in _walk(node):
        if child.type == "import_statement":
            results.extend(_python_import_info(name, None) for name in child.children_by_field_name("name"))
        elif child.type == "import_from_statement" and (module_node := child.child_by_field_name("module_name")):
            source = (
                _resolve_relative_import(module_node, module_path, is_package)
                if module_node.type == "relative_import"
                else node_text(module_node)
            )
            # wildcard imports have no "name" children and are skipped
            results.extend(_python_import_info(name, source) for name in child.children_by_field_name("name"))
    return results


# Identifiers directly inside these nodes bind a local name
_PY_PATTERN_TYPES = frozenset(
    {
        "parameters",
        "lambda_parameters",
        "typed_parameter",
        "list_splat_pattern",
        "dictionary_splat_pattern",
        "pattern_list",
        "tuple_pattern",
        "list_pattern",
        "as_pattern_target",
    }
)
# Node type -> field whose identifier binds a local name
_PY_BINDING_FIELDS = {
    "assignment": "left",
    "augmented_assignment": "left",
    "for_statement": "left",
    "for_in_clause": "left",
    "named_expression": "name",
    "default_parameter": "name",
    "typed_default_parameter": "name",
}
# Identifiers/attributes under these parents are not references on their own
# (attribute: part of a longer chain or the member name; the others: import statements)
_PY_NON_REF_PARENTS = frozenset({"attribute", "dotted_name", "aliased_import"})


def _python_is_binding(node: Node) -> bool:
    parent = node.parent
    if parent is None:
        return False
    field = _PY_BINDING_FIELDS.get(parent.type)
    return parent.type in _PY_PATTERN_TYPES or (field is not None and parent.child_by_field_name(field) == node)


def _is_dotted_chain(node: Node | None) -> bool:
    """True for an identifier or an attribute chain of identifiers (e.g. `a.b.c`, not `a().b`)."""
    while node is not None and node.type == "attribute":
        node = node.child_by_field_name("object")
    return node is not None and node.type == "identifier"


def _python_is_ref(node: Node) -> bool:
    parent = node.parent
    return (
        node.type in ("identifier", "attribute")
        and parent is not None
        and parent.type not in _PY_NON_REF_PARENTS
        and parent.child_by_field_name("name") != node  # def/class/keyword-argument names
        and _is_dotted_chain(node)
    )


def _python_extract_refs(node: Node) -> list[str]:
    """Extract referenced names and dotted chains from a definition node.

    Covers calls, callbacks passed as arguments, decorators, base classes and type
    annotations. Names bound locally anywhere in the definition (parameters,
    assignment/loop/`as` targets) are skipped, since they shadow module-level names.
    """
    nodes = list(_walk(node))
    bound = {node_text(n) for n in nodes if n.type == "identifier" and _python_is_binding(n)}
    refs = (node_text(n) for n in nodes if _python_is_ref(n))
    return list(dict.fromkeys(r for r in refs if r.split(".")[0] not in bound))


def _python_is_entry_point(node: Node) -> bool:
    """True for an `if __name__ == "__main__":` block."""
    condition = node.child_by_field_name("condition") if node.type == "if_statement" else None
    if condition is None:
        return False
    normalized = "".join(node_text(condition).split()).replace("'", '"')
    return normalized in ('__name__=="__main__"', '"__main__"==__name__')


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
        unwrap_definition=_python_unwrap_definition,
        is_private=_python_is_private,
        is_entry_point=_python_is_entry_point,
        docstring_extractor=_python_extract_docstring,
        import_extractor=_python_extract_imports,
        ref_extractor=_python_extract_refs,
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
