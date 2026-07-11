"""Generate units.md (and optionally a draft layers.json) from a codebase using tree-sitter."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

import tree_sitter as ts

from languages import LANGUAGE_CONFIGS, ImportInfo, LanguageConfig, _text, register_languages

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class UnitInfo:
    qualified_name: str  # "payments.gateway.charge"
    submodule: str  # "payments.gateway"
    name: str  # "charge"
    kind: str  # "function" | "class"
    docstring: str | None = None
    raw_calls: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Path / module helpers
# ---------------------------------------------------------------------------


def file_path_to_module(path: Path, root: Path, config: LanguageConfig, prefix: tuple[str, ...] = ()) -> str | None:
    """Convert a file path to a dotted module path relative to root.

    Returns None for paths that can't be converted (e.g. outside root).
    Package filenames (e.g. __init__.py, index.ts) map to the parent directory.

    `prefix` is prepended to the module path. When root is itself a package
    directory, pass its package name(s) here so the resulting module paths match
    absolute imports in the code (e.g. `from verimo.core import analysis`).
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    parts = list(prefix) + list(rel.with_suffix("").parts)
    if parts and parts[-1] in config.package_filenames:
        parts.pop()
    return ".".join(parts) if parts else None


def _is_package_dir(directory: Path, config: LanguageConfig) -> bool:
    """True if directory is a package (contains a package marker like __init__.py)."""
    return any((directory / f"{name}.{ext}").exists() for name in config.package_filenames for ext in config.extensions)


def package_prefix(root: Path, config: LanguageConfig) -> tuple[str, ...]:
    """Package names to prepend to module paths when root sits inside a package.

    If root itself is a package (e.g. `.../verimo` with an __init__.py), its name
    is part of every module's import path. Walk up while each ancestor is also a
    package so absolute imports resolve correctly regardless of where root points.
    """
    prefix: list[str] = []
    directory = root
    while _is_package_dir(directory, config):
        prefix.append(directory.name)
        directory = directory.parent
    return tuple(reversed(prefix))


# ---------------------------------------------------------------------------
# Generic parsing (delegates to LanguageConfig)
# ---------------------------------------------------------------------------


def _def_name_kind(child: ts.Node, config: LanguageConfig) -> tuple[str, str] | None:
    """Return (name, kind) for a top-level function/class node, or None if it isn't one."""
    if child.type in config.function_node_types:
        name_node = child.child_by_field_name(config.function_name_field)
        kind = "function"
    elif child.type in config.class_node_types:
        name_node = child.child_by_field_name(config.class_name_field)
        kind = "class"
    else:
        return None
    if name_node is None:
        return None
    return _text(name_node), kind


def _expand_private_calls(calls: list[str], helper_calls: dict[str, list[str]]) -> list[str]:
    """Inline calls to same-module private helpers so their dependencies aren't lost.

    Private helpers aren't emitted as units, so a public unit that delegates to
    them would otherwise drop the helpers' outgoing dependencies. Recursively
    replace each call to a private helper with the helper's own calls (a `seen`
    set guards against recursion cycles).
    """
    expanded: list[str] = []
    seen: set[str] = set()

    def visit(cs: list[str]) -> None:
        for call in cs:
            if call in helper_calls:
                if call in seen:
                    continue
                seen.add(call)
                visit(helper_calls[call])
            else:
                expanded.append(call)

    visit(calls)
    return expanded


def parse_file(
    source: bytes,
    module_path: str,
    config: LanguageConfig,
    parser: ts.Parser,
    *,
    include_private: bool = False,
) -> tuple[list[UnitInfo], list[ImportInfo]]:
    """Parse a single source file into units and imports.

    Extracts top-level functions and classes. Class methods are folded into
    the class unit (their calls become the class's raw_calls).

    When private symbols are excluded (the default), their outgoing calls are
    still inlined into the public units that call them, so dependencies routed
    through private helpers are preserved as edges of the public callers.
    """
    tree = parser.parse(source)
    root = tree.root_node

    imports = config.import_extractor(root, module_path)
    defs = [(nk[0], nk[1], child) for child in root.children if (nk := _def_name_kind(child, config)) is not None]

    # Map of private helper name -> its raw calls, used to inline edges into public callers.
    helper_calls: dict[str, list[str]] = {}
    if not include_private:
        helper_calls = {name: config.call_extractor(child) for name, _kind, child in defs if config.is_private(name, child)}

    units: list[UnitInfo] = []
    for name, kind, child in defs:
        if not include_private and config.is_private(name, child):
            continue
        calls = config.call_extractor(child)
        if not include_private:
            calls = _expand_private_calls(calls, helper_calls)
        units.append(
            UnitInfo(
                qualified_name=f"{module_path}.{name}",
                submodule=module_path,
                name=name,
                kind=kind,
                docstring=config.docstring_extractor(child),
                raw_calls=calls,
            )
        )

    return units, imports


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------


def build_index(
    root: Path,
    *,
    exclude_patterns: list[str] | None = None,
    include_private: bool = False,
) -> tuple[dict[str, UnitInfo], dict[str, dict[str, str]]]:
    """Walk source files under root, parse each, return symbol index and import map.

    symbol_index: qualified_name -> UnitInfo
    import_map: module_path -> {local_name -> qualified_name}
    """
    exclude = exclude_patterns or []
    symbol_index: dict[str, UnitInfo] = {}
    import_map: dict[str, dict[str, str]] = {}

    # Build extension -> (language_fn, config) lookup
    ext_configs: dict[str, tuple] = {}
    for ext, (lang_fn, config) in LANGUAGE_CONFIGS.items():
        ext_configs[ext] = (lang_fn, config)

    for ext, (lang_fn, config) in ext_configs.items():
        lang = ts.Language(lang_fn())
        parser = ts.Parser(lang)
        prefix = package_prefix(root, config)

        for path in sorted(root.rglob(f"*.{ext}")):
            if any(fnmatch(path.name, pat) for pat in exclude):
                continue
            module_path = file_path_to_module(path, root, config, prefix)
            if module_path is None:
                continue

            source = path.read_bytes()
            units, imports = parse_file(source, module_path, config, parser, include_private=include_private)

            for unit in units:
                if unit.qualified_name in symbol_index:
                    logger.warning(f"Duplicate unit: {unit.qualified_name}")
                symbol_index[unit.qualified_name] = unit

            import_map[module_path] = {imp.local_name: imp.qualified_name for imp in imports}

    return symbol_index, import_map


# ---------------------------------------------------------------------------
# Dependency resolution
# ---------------------------------------------------------------------------


def resolve_dependencies(
    symbol_index: dict[str, UnitInfo],
    import_map: dict[str, dict[str, str]],
) -> dict[str, list[str]]:
    """Resolve raw calls to qualified unit paths that exist in symbol_index.

    Returns: {unit_qualified_name: [dependency_qualified_name, ...]}
    """
    result: dict[str, list[str]] = {}

    for qname, unit in symbol_index.items():
        local_imports = import_map.get(unit.submodule, {})
        deps: dict[str, bool] = {}  # use dict for dedup, preserving order

        for call in unit.raw_calls:
            resolved = _resolve_call(call, local_imports, unit.submodule)
            if resolved is None:
                continue
            # Try exact match, then strip last segment (method -> class)
            target = _find_in_index(resolved, symbol_index)
            if target is not None and target != qname:  # skip self-deps
                deps[target] = True

        result[qname] = list(deps)

    return result


def _resolve_call(call: str, local_imports: dict[str, str], module_path: str) -> str | None:
    """Resolve a raw call string through the import map or same-module lookup."""
    parts = call.split(".")
    first = parts[0]
    if first in local_imports:
        base = local_imports[first]
        if len(parts) > 1:
            return base + "." + ".".join(parts[1:])
        return base
    # Try as a same-module reference (e.g. calling another function in the same file)
    if len(parts) == 1:
        return f"{module_path}.{first}"
    return None


def _find_in_index(qualified: str, symbol_index: dict[str, UnitInfo]) -> str | None:
    """Find a unit in the index, trying exact match then stripping segments."""
    if qualified in symbol_index:
        return qualified
    # Try stripping last segment (e.g. Class.method -> Class)
    if "." in qualified:
        parent = qualified.rsplit(".", 1)[0]
        if parent in symbol_index:
            return parent
    return None


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_units_md(
    symbol_index: dict[str, UnitInfo],
    dependencies: dict[str, list[str]],
    *,
    full_docstrings: bool = False,
) -> str:
    """Format units and their dependencies as markdown compatible with prepare.py."""
    # Group by submodule, sorted
    by_submodule: dict[str, list[UnitInfo]] = {}
    for unit in symbol_index.values():
        by_submodule.setdefault(unit.submodule, []).append(unit)

    lines: list[str] = []
    for sm in sorted(by_submodule):
        units = by_submodule[sm]
        for unit in units:
            lines.append(f"### {unit.qualified_name}")
            docstring = unit.docstring if full_docstrings else unit.docstring.split("\n")[0] if unit.docstring else None
            desc = docstring or f"{unit.kind.capitalize()} in {unit.submodule}."
            deps = dependencies.get(unit.qualified_name, [])
            if deps:
                dep_refs = ", ".join(f"`@{d}`" for d in sorted(deps))
                desc = f"{desc}\n\nDepends on {dep_refs}."
            lines.append(desc)
            lines.append("")

    return "\n".join(lines)


def _aggregate_deps_by(
    symbol_index: dict[str, UnitInfo],
    dependencies: dict[str, list[str]],
    key_fn,
) -> dict[str, set[str]]:
    """Aggregate unit-level dependencies to a coarser grouping defined by key_fn.

    key_fn(UnitInfo) -> grouping key (e.g. submodule or root module).
    Returns {group: set of groups it depends on} (self-deps excluded).
    """
    graph: dict[str, set[str]] = {}
    for qname, dep_list in dependencies.items():
        src = key_fn(symbol_index[qname])
        graph.setdefault(src, set())
        for dep_qname in dep_list:
            if dep_qname in symbol_index:
                dst = key_fn(symbol_index[dep_qname])
                if dst != src:
                    graph[src].add(dst)
    return graph


def _dep_sorted(items: list[str], dep_graph: dict[str, set[str]]) -> list[str]:
    """Sort items by dependency flow: consumers at top, providers at bottom.

    Uses Kahn's topological sort (alphabetical tiebreak) for a perfect ordering
    when the graph is a DAG. Nodes involved in cycles fall back to a net-flow
    heuristic. Isolated nodes (no edges at all) are placed at the very bottom.
    """
    item_set = set(items)
    edges = {i: dep_graph.get(i, set()) & item_set for i in items}
    in_deg = dict.fromkeys(items, 0)
    for targets in edges.values():
        for t in targets:
            in_deg[t] += 1

    # Separate isolated nodes (no connections) — they go to the bottom
    connected = [i for i in items if edges[i] or in_deg[i] > 0]
    isolated = sorted(i for i in items if not edges[i] and in_deg[i] == 0)

    # Kahn's algorithm on connected nodes
    queue = sorted(i for i in connected if in_deg[i] == 0)
    result: list[str] = []
    while queue:
        node = queue.pop(0)
        result.append(node)
        for t in sorted(edges[node]):
            in_deg[t] -= 1
            if in_deg[t] == 0:
                queue.append(t)
                queue.sort()

    # Remaining connected nodes are in cycles — rank by net flow
    if len(result) < len(connected):
        placed = set(result)
        rest = [i for i in connected if i not in placed]
        out_deg = {i: len(edges[i] - placed) for i in rest}
        rest_in = dict.fromkeys(rest, 0)
        rest_set = set(rest)
        for i in rest:
            for t in edges[i] & rest_set:
                rest_in[t] += 1
        rest.sort(key=lambda i: (-(out_deg[i] - rest_in[i]), -rest_in[i], i))
        result.extend(rest)

    result.extend(isolated)
    return result


def _root_module_depth(submodules: set[str]) -> int:
    """Number of leading segments that make up a 'root module' key.

    Normally 1 (the top-level package). But when every module shares a common
    enclosing package (e.g. `verimo.core.*`, `verimo.backend.*`), that shared
    package is just the project container, not an architectural layer, so we group
    one level deeper (`verimo.core`, `verimo.backend`). Capped so a module that
    sits at the shared level itself still keeps a root of its own.
    """
    if not submodules:
        return 1
    segmented = [sm.split(".") for sm in submodules]
    min_len = min(len(s) for s in segmented)
    common = 0
    for i in range(min_len):
        if len({s[i] for s in segmented}) == 1:
            common += 1
        else:
            break
    return min(common, min_len - 1) + 1


def generate_layers_draft(
    symbol_index: dict[str, UnitInfo],
    dependencies: dict[str, list[str]],
) -> tuple[dict, set[str]]:
    """Generate a draft layers.json ordered by dependency flow.

    Returns (layers_dict, valid_submodules). The valid_submodules set contains
    exactly the submodules that appear in the flattened layers — use it to filter
    units.md so both files stay consistent.

    Each module and submodule gets its own row. The user is expected to reorder
    rows and merge siblings to express the intended dependency hierarchy.
    """
    submodules: set[str] = {unit.submodule for unit in symbol_index.values()}
    depth = _root_module_depth(submodules)

    def root_key(submodule: str) -> str:
        return ".".join(submodule.split(".")[:depth])

    root_modules = {root_key(sm) for sm in submodules}

    # Aggregate deps to submodule and root-module level
    sm_deps = _aggregate_deps_by(symbol_index, dependencies, lambda u: u.submodule)
    root_deps = _aggregate_deps_by(symbol_index, dependencies, lambda u: root_key(u.submodule))

    root_layers = [[m] for m in _dep_sorted(list(root_modules), root_deps)]

    # Only add submodule_layers for root modules that have actual submodules.
    # If units exist directly at the root level (e.g. from __init__.py), the module
    # must stay a leaf — prepare.py doesn't support mixing root-level units with
    # submodule_layers.
    submodule_layers: dict[str, list[list[str]]] = {}
    valid_submodules: set[str] = set()
    for root in sorted(root_modules):
        has_root_units = root in submodules
        nested = [sm for sm in submodules if sm.startswith(root + ".")]
        if nested and not has_root_units:
            submodule_layers[root] = [[sm] for sm in _dep_sorted(nested, sm_deps)]
            valid_submodules.update(nested)
        else:
            # Leaf module: only the root name is a valid submodule
            valid_submodules.add(root)

    layers = {"root_layers": root_layers, "submodule_layers": submodule_layers}
    return layers, valid_submodules


def filter_to_valid_submodules(
    symbol_index: dict[str, UnitInfo],
    valid_submodules: set[str],
) -> dict[str, UnitInfo]:
    """Remove units whose submodule is not in the valid set."""
    filtered: dict[str, UnitInfo] = {}
    for qname, unit in symbol_index.items():
        if unit.submodule in valid_submodules:
            filtered[qname] = unit
        else:
            logger.warning(f"Dropping {qname}: submodule {unit.submodule} not in layers")
    return filtered


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    register_languages()

    if not LANGUAGE_CONFIGS:
        logger.critical("No tree-sitter language grammars available. Install e.g. tree-sitter-python.")
        sys.exit(1)

    ap = argparse.ArgumentParser(description="Generate units.md and layers.json from a codebase using tree-sitter.")
    ap.add_argument("--root", required=True, type=Path, help="Root directory of the codebase to analyze")
    ap.add_argument("--output", required=True, type=Path, help="Output folder (created if it doesn't exist)")
    ap.add_argument("--include-private", action="store_true", help="Include private symbols (e.g., `_`-prefixed in Python)")
    ap.add_argument("--exclude", default="", help="Comma-separated glob patterns for filenames to skip")
    ap.add_argument(
        "--full-docstrings", action="store_true", help="Include full docstrings instead of just the first paragraph"
    )
    args = ap.parse_args()

    exclude_patterns = [p.strip() for p in args.exclude.split(",") if p.strip()]

    symbol_index, import_map = build_index(
        args.root.resolve(),
        exclude_patterns=exclude_patterns,
        include_private=args.include_private,
    )
    logger.info(f"Found {len(symbol_index)} units across {len(import_map)} modules")

    # Resolve deps on full index, then use them to order the draft
    deps = resolve_dependencies(symbol_index, import_map)
    draft, valid_submodules = generate_layers_draft(symbol_index, deps)
    symbol_index = filter_to_valid_submodules(symbol_index, valid_submodules)

    # Re-resolve after filtering so units.md only references valid submodules
    deps = resolve_dependencies(symbol_index, import_map)

    args.output.mkdir(parents=True, exist_ok=True)

    units_path = args.output / "units.md"
    units_path.write_text(format_units_md(symbol_index, deps, full_docstrings=args.full_docstrings), encoding="utf-8")
    logger.info(f"Wrote {units_path}")

    layers_path = args.output / "layers.json"
    layers_path.write_text(json.dumps(draft, indent=2) + "\n", encoding="utf-8")
    logger.info(f"Wrote {layers_path}")
    logger.info("Please adjust the (sub)module layer hierarchy in `layers.json` to reflect the target architecture.")
