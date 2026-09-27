"""Generate units.md (and optionally a draft layers.json) from a codebase using tree-sitter."""

from __future__ import annotations

import heapq
import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

import tree_sitter as ts

from languages import LANGUAGE_CONFIGS, ImportInfo, LanguageConfig, node_text, register_languages

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
    raw_refs: list[str] = field(default_factory=list)  # names / dotted chains referenced in the unit
    is_private: bool = False


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


def _def_name_kind(definition: ts.Node | None, config: LanguageConfig) -> tuple[str, str] | None:
    """Return (name, kind) for a function/class definition node, or None if it isn't one."""
    if definition is None:
        return None
    if definition.type in config.function_node_types:
        name_node = definition.child_by_field_name(config.function_name_field)
        kind = "function"
    elif definition.type in config.class_node_types:
        name_node = definition.child_by_field_name(config.class_name_field)
        kind = "class"
    else:
        return None
    if name_node is None:
        return None
    return node_text(name_node), kind


def parse_file(
    source: bytes,
    module_path: str,
    config: LanguageConfig,
    parser: ts.Parser,
    *,
    is_package: bool = False,
) -> tuple[list[UnitInfo], list[ImportInfo]]:
    """Parse a single source file into units (public and private) and imports.

    Extracts top-level functions and classes (including decorated ones). Class
    methods are folded into the class unit (their refs become the class's raw_refs),
    and refs in decorators count as refs of the decorated unit.
    `is_package` marks package files (e.g. __init__.py), whose module path is the
    package itself, which matters for resolving relative imports.
    """
    root = parser.parse(source).root_node
    units: list[UnitInfo] = []
    for child in root.children:
        definition = config.unwrap_definition(child)
        name_kind = _def_name_kind(definition, config)
        if definition is None or name_kind is None:
            continue
        name, kind = name_kind
        units.append(
            UnitInfo(
                qualified_name=f"{module_path}.{name}",
                submodule=module_path,
                name=name,
                kind=kind,
                docstring=config.docstring_extractor(definition),
                raw_refs=config.ref_extractor(child),
                is_private=config.is_private(name, definition),
            )
        )
    return units, config.import_extractor(root, module_path, is_package)


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------


def build_index(
    root: Path,
    *,
    exclude_patterns: list[str] | None = None,
    include_private: bool = False,
) -> tuple[dict[str, UnitInfo], dict[str, dict[str, str]], dict[str, UnitInfo]]:
    """Walk source files under root, parse each, return symbol index, import map and private helpers.

    symbol_index: qualified_name -> UnitInfo (the units to emit)
    import_map: module_path -> {local_name -> qualified_name}
    helpers: qualified_name -> UnitInfo of private units that are not emitted
        (unless include_private); resolve_dependencies inlines their refs into callers.

    Units whose path equals a submodule path (e.g. `main` in `run/__init__.py` next to
    `run/main.py`) are skipped with a warning, since prepare.py rejects them.
    """
    exclude = exclude_patterns or []
    symbol_index: dict[str, UnitInfo] = {}
    import_map: dict[str, dict[str, str]] = {}
    helpers: dict[str, UnitInfo] = {}

    for ext, (lang_fn, config) in LANGUAGE_CONFIGS.items():
        parser = ts.Parser(ts.Language(lang_fn()))
        prefix = package_prefix(root, config)
        # A root without package marker may still be imported by its name (namespace package),
        # unless it contains a same-named module, in which case such imports refer to that one.
        namespace = "" if prefix or any(p.stem == root.name for p in root.iterdir()) else f"{root.name}."

        for path in sorted(root.rglob(f"*.{ext}")):
            if any(fnmatch(path.name, pat) for pat in exclude):
                continue
            module_path = file_path_to_module(path, root, config, prefix)
            if module_path is None:
                continue

            is_package = path.stem in config.package_filenames
            units, imports = parse_file(path.read_bytes(), module_path, config, parser, is_package=is_package)

            for unit in units:
                index = helpers if unit.is_private and not include_private else symbol_index
                if unit.qualified_name in index:
                    logger.warning(f"Duplicate unit: {unit.qualified_name}")
                index[unit.qualified_name] = unit

            import_map[module_path] = {imp.local_name: imp.qualified_name.removeprefix(namespace) for imp in imports}

    for qname in sorted({unit.submodule for unit in symbol_index.values()} & symbol_index.keys()):
        logger.warning(f"Skipping {qname}: unit path collides with the submodule of the same name")
        del symbol_index[qname]

    return symbol_index, import_map, helpers


# ---------------------------------------------------------------------------
# Dependency resolution
# ---------------------------------------------------------------------------


def _qualify_ref(ref: str, local_imports: dict[str, str], module_path: str) -> str:
    """Turn a raw ref into a fully qualified path via the import map, else as a same-module name."""
    first, _, rest = ref.partition(".")
    if first in local_imports:
        base = local_imports[first]
        return f"{base}.{rest}" if rest else base
    return f"{module_path}.{ref}"


def _find_in_index(qualified: str, units: dict[str, UnitInfo], modules: dict[str, dict[str, str]]) -> str | None:
    """Find the unit a qualified path points to.

    Strips trailing segments (e.g. `Model.Manager.create` -> `Model`) but never past a
    module path, so a ref into a module never resolves to a unit named like that module.
    """
    parts = qualified.split(".")
    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in units:
            return candidate
        if candidate in modules:
            return None
    return None


def _follow_reexport(qualified: str, import_map: dict[str, dict[str, str]]) -> str | None:
    """Rewrite `module.name.rest` to the path `name` was imported from in `module` (e.g. a package __init__)."""
    parts = qualified.split(".")
    for i in range(len(parts) - 1, 0, -1):
        module = ".".join(parts[:i])
        if module in import_map:
            target = import_map[module].get(parts[i])
            return ".".join([target, *parts[i + 1 :]]) if target else None
    return None


def _resolve_target(qualified: str, units: dict[str, UnitInfo], import_map: dict[str, dict[str, str]]) -> str | None:
    """Find the unit a qualified path refers to, following re-exports (a `seen` set guards against import cycles)."""
    seen: set[str] = set()
    current: str | None = qualified
    while current is not None and current not in seen:
        seen.add(current)
        if (found := _find_in_index(current, units, import_map)) is not None:
            return found
        current = _follow_reexport(current, import_map)
    return None


def resolve_dependencies(
    symbol_index: dict[str, UnitInfo],
    import_map: dict[str, dict[str, str]],
    helpers: dict[str, UnitInfo] | None = None,
) -> dict[str, list[str]]:
    """Resolve raw refs to qualified unit paths that exist in symbol_index.

    Private helpers aren't emitted as units, so a unit that delegates to them would
    otherwise drop the helpers' outgoing dependencies. A ref to a helper is therefore
    replaced by the helper's own resolved refs, recursively (a `seen` set guards
    against recursion cycles).

    Returns: {unit_qualified_name: [dependency_qualified_name, ...]}
    """
    helpers = helpers or {}
    units = symbol_index | helpers

    def resolve(unit: UnitInfo, seen: set[str]) -> Iterator[str]:
        local_imports = import_map.get(unit.submodule, {})
        for ref in unit.raw_refs:
            target = _resolve_target(_qualify_ref(ref, local_imports, unit.submodule), units, import_map)
            if target is None:
                continue
            if target not in helpers:
                yield target
            elif target not in seen:
                seen.add(target)
                yield from resolve(helpers[target], seen)

    return {
        qname: [dep for dep in dict.fromkeys(resolve(unit, set())) if dep != qname]  # skip self-deps
        for qname, unit in symbol_index.items()
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


_MARKUP_LINE_RE = re.compile(r"^( {0,3})(###(?=[ \t]|$)|```|~~~)", re.MULTILINE)


def _description(unit: UnitInfo, full_docstrings: bool) -> str:
    """Unit description from its docstring (first paragraph unless full_docstrings), escaped for units.md."""
    if not unit.docstring:
        return f"{unit.kind.capitalize()} in {unit.submodule}."
    doc = unit.docstring if full_docstrings else " ".join(re.split(r"\n\s*\n", unit.docstring)[0].split())
    # Backslash-escape what prepare.py would parse as a unit heading, code fence (which could
    # swallow the following headings if unclosed) or dependency ref
    return _MARKUP_LINE_RE.sub(r"\1\\\2", doc).replace("`@", "`\\@")


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
            desc = _description(unit, full_docstrings)
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
    key_fn: Callable[[UnitInfo], str],
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


def _strongly_connected_components(nodes: list[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's algorithm; returns the SCCs of the graph restricted to nodes."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    sccs: list[list[str]] = []

    def visit(v: str) -> None:
        index[v] = low[v] = len(index)
        stack.append(v)
        on_stack.add(v)
        for w in sorted(edges[v]):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            scc: list[str] = []
            while not scc or scc[-1] != v:
                scc.append(stack.pop())
                on_stack.discard(scc[-1])
            sccs.append(scc)

    for v in sorted(nodes):
        if v not in index:
            visit(v)
    return sccs


def _net_flow_sorted(scc: list[str], edges: dict[str, set[str]]) -> list[str]:
    """Order the nodes of a cycle: most outgoing minus incoming edges (within the cycle) first."""
    members = set(scc)
    out_deg = {i: len(edges[i] & members) for i in scc}
    in_deg = Counter(t for i in scc for t in edges[i] & members)
    return sorted(scc, key=lambda i: (-(out_deg[i] - in_deg[i]), -in_deg[i], i))


def _dep_sorted(items: list[str], dep_graph: dict[str, set[str]]) -> list[str]:
    """Sort items by dependency flow: consumers at top, providers at bottom.

    Cycles are condensed into strongly connected components, which are then
    topologically sorted with Kahn's algorithm (alphabetical tiebreak), so providers
    always end up below their consumers. Nodes within a cycle are ordered by a
    net-flow heuristic. Isolated nodes (no edges at all) are placed at the very bottom.
    """
    item_set = set(items)
    edges = {i: dep_graph.get(i, set()) & item_set for i in items}
    has_incoming = {t for targets in edges.values() for t in targets}
    isolated = sorted(i for i in items if not edges[i] and i not in has_incoming)
    connected = [i for i in items if edges[i] or i in has_incoming]

    sccs = _strongly_connected_components(connected, edges)
    component = {node: k for k, scc in enumerate(sccs) for node in scc}
    comp_edges = [{component[t] for node in scc for t in edges[node]} - {k} for k, scc in enumerate(sccs)]
    in_deg = Counter(t for targets in comp_edges for t in targets)

    heap = [(min(scc), k) for k, scc in enumerate(sccs) if in_deg[k] == 0]
    heapq.heapify(heap)
    result: list[str] = []
    while heap:
        _, k = heapq.heappop(heap)
        result.extend(_net_flow_sorted(sccs[k], edges))
        for t in comp_edges[k]:
            in_deg[t] -= 1
            if in_deg[t] == 0:
                heapq.heappush(heap, (min(sccs[t]), t))

    return result + isolated


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
) -> dict:
    """Generate a draft layers.json ordered by dependency flow, covering every unit's submodule.

    Each module and submodule gets its own row. The user is expected to reorder
    rows and merge siblings to express the intended dependency hierarchy.
    A root module with both its own units (e.g. from __init__.py) and nested
    submodules lists itself as one of its submodules.
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

    # Root modules without nested submodules stay leaves (not in submodule_layers)
    submodule_layers: dict[str, list[list[str]]] = {}
    for root in sorted(root_modules):
        nested = [sm for sm in submodules if sm.startswith(root + ".")]
        if nested:
            own = [root] if root in submodules else []
            submodule_layers[root] = [[sm] for sm in _dep_sorted(own + nested, sm_deps)]

    return {"root_layers": root_layers, "submodule_layers": submodule_layers}


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

    symbol_index, import_map, helpers = build_index(
        args.root.resolve(),
        exclude_patterns=exclude_patterns,
        include_private=args.include_private,
    )
    logger.info(f"Found {len(symbol_index)} units across {len(import_map)} modules")

    deps = resolve_dependencies(symbol_index, import_map, helpers)
    draft = generate_layers_draft(symbol_index, deps)

    args.output.mkdir(parents=True, exist_ok=True)

    units_path = args.output / "units.md"
    units_path.write_text(format_units_md(symbol_index, deps, full_docstrings=args.full_docstrings), encoding="utf-8")
    logger.info(f"Wrote {units_path}")

    layers_path = args.output / "layers.json"
    layers_path.write_text(json.dumps(draft, indent=2) + "\n", encoding="utf-8")
    logger.info(f"Wrote {layers_path}")
    logger.info("Please adjust the (sub)module layer hierarchy in `layers.json` to reflect the target architecture.")
