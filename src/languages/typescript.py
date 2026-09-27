"""TypeScript and Svelte configurations for unit extraction (grammar: tree-sitter-typescript).

Units are exported functions (incl. `const f = () => ...`), classes, interfaces, type
aliases and enums. A Svelte component is a single unit made of its <script> blocks;
since its markup can only use what the scripts import, all imports count as its refs.
Only relative imports (`./x`, `../x`, incl. dynamic `import('./x')`) are resolved; path aliases (e.g. `$lib`) are not.
"""

from __future__ import annotations

import inspect
import re
from typing import TYPE_CHECKING

from languages.base import ImportInfo, LanguageConfig, module_segment, node_text, walk

if TYPE_CHECKING:
    from tree_sitter import Node

DEFINITION_KINDS = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "variable_declarator": "function",  # only those with a function value, see unwrap_definition
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "interface_declaration": "type",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
}
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "generator_function"})


def _top_level(node: Node) -> Node:
    """The ancestor of node (or node itself) that is a direct child of the program."""
    while node.parent is not None and node.parent.type != "program":
        node = node.parent
    return node


def unwrap_definition(node: Node) -> Node | None:
    """Return the definition wrapped by an export statement; for variable declarations the function-valued declarator."""
    definition = node.child_by_field_name("declaration") if node.type == "export_statement" else node
    if definition is None or definition.type not in ("lexical_declaration", "variable_declaration"):
        return definition
    declarators = [c for c in definition.named_children if c.type == "variable_declarator"]
    if len(declarators) != 1:
        return None
    name, value = declarators[0].child_by_field_name("name"), declarators[0].child_by_field_name("value")
    is_function = name is not None and name.type == "identifier" and value is not None and value.type in _FUNCTION_VALUES
    return declarators[0] if is_function else None


def is_private(_name: str, node: Node) -> bool:
    """Definitions that aren't exported are private."""
    return _top_level(node).type != "export_statement"


def is_entry_point(node: Node) -> bool:
    """True for top-level code that runs on import, e.g. `mount(App, ...)` in main.ts (incl. `export default <expr>`)."""
    if node.type == "export_statement":
        value = node.child_by_field_name("value")
        return value is not None and value.type != "identifier"
    return node.type in ("expression_statement", "if_statement")


def _comment_text(comment: str) -> str:
    """Strip the comment markers of a `//` or `/* */` (incl. JSDoc) comment."""
    if comment.startswith("//"):
        return comment[2:].removeprefix(" ")
    lines = comment.removeprefix("/*").removesuffix("*/").lstrip("*").splitlines()
    return inspect.cleandoc("\n".join(re.sub(r"^\s*\* ?", "", line) for line in lines))


def extract_docstring(node: Node) -> str | None:
    """Extract the comments directly above a definition, or at the top of a file (for file units)."""
    if node.type == "program":
        comments = []
        for child in node.children:
            if child.type != "comment" or (comments and child.start_point.row > comments[-1].end_point.row + 1):
                break
            comments.append(child)
    else:
        comments = []
        current = _top_level(node)
        while (prev := current.prev_sibling) is not None and prev.type == "comment":
            if prev.end_point.row + 1 < current.start_point.row:
                break
            comments.insert(0, prev)
            current = prev
    doc = "\n".join(_comment_text(node_text(c)) for c in comments).strip()
    return doc or None


def _resolve_specifier(specifier: str, module_path: str, is_package: bool) -> str:
    """Resolve an import specifier to a module path; non-relative ones (packages) are kept as they are."""
    if not specifier.startswith("."):
        return specifier
    # The importing file's directory; a package file's (index.ts) module path already is that directory
    parts = module_path.split(".") if is_package else module_path.split(".")[:-1]
    for segment in specifier.split("/"):
        if segment == "..":
            parts = parts[:-1]
        elif segment not in (".", ""):
            parts.append(module_segment(segment))
    return ".".join(parts)


def _dynamic_import_specifier(node: Node) -> str | None:
    """The specifier of a dynamic `import('...')` with a string literal, else None."""
    function, arguments = node.child_by_field_name("function"), node.child_by_field_name("arguments")
    if node.type != "call_expression" or function is None or function.type != "import" or arguments is None:
        return None
    specifier = arguments.named_children[0] if arguments.named_child_count == 1 else None
    return node_text(specifier)[1:-1] if specifier is not None and specifier.type == "string" else None


def _dynamic_import_name(node: Node) -> str:
    """Local name standing in for a dynamic import in refs and imports (unique, can't clash with identifiers)."""
    return f"import:{node.start_byte}"


def extract_imports(node: Node, module_path: str, is_package: bool) -> list[ImportInfo]:
    """Extract imports and re-exports from a module AST.

    Default imports refer to `<module>.default`, which export statements map to the
    exported definition (e.g. `export default function f` -> `<module>.f`). Dynamic
    imports (e.g. lazily loaded pages) also refer to the module's default export.
    Wildcard re-exports (`export * from`) are skipped.
    """
    results = [
        ImportInfo(_dynamic_import_name(n), f"{_resolve_specifier(specifier, module_path, is_package)}.default")
        for n in walk(node)
        if (specifier := _dynamic_import_specifier(n)) is not None
    ]
    for child in node.children:
        source_node = child.child_by_field_name("source")
        source = _resolve_specifier(node_text(source_node)[1:-1], module_path, is_package) if source_node else module_path
        if child.type == "import_statement":
            for n in walk(child):
                if n.type == "import_specifier" and (name := n.child_by_field_name("name")):
                    local = n.child_by_field_name("alias") or name
                    results.append(ImportInfo(node_text(local), f"{source}.{node_text(name)}"))
                elif n.type == "identifier" and n.parent is not None and n.parent.type == "import_clause":
                    results.append(ImportInfo(node_text(n), f"{source}.default"))
                elif n.type == "identifier" and n.parent is not None and n.parent.type == "namespace_import":
                    results.append(ImportInfo(node_text(n), source))
        elif child.type == "export_statement":
            for n in walk(child):
                if n.type == "export_specifier" and (name := n.child_by_field_name("name")):
                    alias = n.child_by_field_name("alias")
                    if source_node or alias:
                        results.append(ImportInfo(node_text(alias or name), f"{source}.{node_text(name)}"))
            if child.children and any(c.type == "default" for c in child.children):
                exported = child.child_by_field_name("declaration") or child.child_by_field_name("value")
                name = exported.child_by_field_name("name") if exported and exported.type != "identifier" else exported
                if name is not None:
                    results.append(ImportInfo("default", f"{module_path}.{node_text(name)}"))
    return results


# Declarations whose name field binds a local name (other nodes, e.g. generic_type, use "name" for references)
_DECLARATION_TYPES = frozenset({*DEFINITION_KINDS, "function_expression", "generator_function", "class", "type_parameter"})
# Node type -> field whose identifier binds a local name
_BINDING_FIELDS = {
    "required_parameter": "pattern",
    "optional_parameter": "pattern",
    "arrow_function": "parameter",
    "catch_clause": "parameter",
    "for_in_statement": "left",
    "pair_pattern": "value",
    "assignment_pattern": "left",
}
# Identifiers directly inside these nodes bind a local name
_PATTERN_TYPES = frozenset({"array_pattern", "rest_pattern"})
# Identifiers under these parents are not references (import/export names, parts of a qualified type)
_NON_REF_PARENTS = frozenset(
    {"import_specifier", "import_clause", "namespace_import", "export_specifier", "nested_identifier", "nested_type_identifier"}
)


def _is_binding(node: Node) -> bool:
    parent = node.parent
    if node.type == "shorthand_property_identifier_pattern":
        return True
    if parent is None or node.type not in ("identifier", "type_identifier"):
        return False
    field = "name" if parent.type in _DECLARATION_TYPES else _BINDING_FIELDS.get(parent.type)
    return parent.type in _PATTERN_TYPES or (field is not None and parent.child_by_field_name(field) == node)


def _dotted_chain(node: Node) -> str | None:
    """`a.b.c` for an identifier or a member expression chain of identifiers (optional chaining ignored), else None."""
    if node.type in ("identifier", "type_identifier", "shorthand_property_identifier", "nested_type_identifier"):
        return node_text(node).replace(" ", "")
    obj, prop = node.child_by_field_name("object"), node.child_by_field_name("property")
    if node.type != "member_expression" or obj is None or prop is None or prop.type != "property_identifier":
        return None
    base = _dotted_chain(obj)
    return f"{base}.{node_text(prop)}" if base else None


def _is_outermost(node: Node) -> bool:
    """False for the object of a member expression (only the longest chain is a ref)."""
    parent = node.parent
    return parent is None or not (parent.type == "member_expression" and parent.child_by_field_name("object") == node)


def extract_refs(node: Node) -> list[str]:
    """Extract referenced names and dotted chains (values and types) from a definition node.

    Names bound locally anywhere in the definition (parameters, variables, type
    parameters, nested declarations) are skipped, since they shadow module-level names.
    """
    nodes = list(walk(node))
    bound = {node_text(n) for n in nodes if _is_binding(n)}
    refs = (
        _dotted_chain(n)
        for n in nodes
        if not _is_binding(n) and _is_outermost(n) and (n.parent is None or n.parent.type not in _NON_REF_PARENTS)
    )
    dynamic_imports = (_dynamic_import_name(n) for n in nodes if _dynamic_import_specifier(n) is not None)
    return list(dict.fromkeys(r for r in [*refs, *dynamic_imports] if r and r.split(".")[0] not in bound))


def make_config() -> LanguageConfig:
    return LanguageConfig(
        extensions=frozenset(["ts"]),
        package_filenames=frozenset(["index"]),
        definition_kinds=DEFINITION_KINDS,
        name_field="name",
        unwrap_definition=unwrap_definition,
        is_private=is_private,
        is_entry_point=is_entry_point,
        docstring_extractor=extract_docstring,
        import_extractor=extract_imports,
        ref_extractor=extract_refs,
    )


_SCRIPT_RE = re.compile(rb"<script\b[^>]*>(.*?)</script>", re.DOTALL)


def extract_scripts(source: bytes) -> bytes:
    """The contents of all <script> blocks of a Svelte component (the instance and the module script)."""
    return b"\n".join(_SCRIPT_RE.findall(source))


def make_svelte_config() -> LanguageConfig:
    return LanguageConfig(
        extensions=frozenset(["svelte"]),
        package_filenames=frozenset(),
        definition_kinds=DEFINITION_KINDS,
        name_field="name",
        unwrap_definition=unwrap_definition,
        is_private=is_private,
        is_entry_point=is_entry_point,
        docstring_extractor=extract_docstring,
        import_extractor=extract_imports,
        ref_extractor=extract_refs,
        preprocess=extract_scripts,
        file_unit_kind="component",
    )
