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


def file_path_to_module(path: Path, root: Path, config: LanguageConfig) -> str | None:
    """Convert a file path to a dotted module path relative to root.

    Returns None for paths that can't be converted (e.g. outside root).
    Package filenames (e.g. __init__.py, index.ts) map to the parent directory.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] in config.package_filenames:
        parts.pop()
    return ".".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Generic parsing (delegates to LanguageConfig)
# ---------------------------------------------------------------------------


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
    """
    tree = parser.parse(source)
    root = tree.root_node

    units: list[UnitInfo] = []
    imports = config.import_extractor(root, module_path)

    for child in root.children:
        if child.type in config.function_node_types:
            name_node = child.child_by_field_name(config.function_name_field)
            if name_node is None:
                continue
            name = _text(name_node)
            if not include_private and config.is_private(name, child):
                continue
            kind = "function"

        elif child.type in config.class_node_types:
            name_node = child.child_by_field_name(config.class_name_field)
            if name_node is None:
                continue
            name = _text(name_node)
            if not include_private and config.is_private(name, child):
                continue
            kind = "class"

        else:
            continue

        units.append(
            UnitInfo(
                qualified_name=f"{module_path}.{name}",
                submodule=module_path,
                name=name,
                kind=kind,
                docstring=config.docstring_extractor(child),
                raw_calls=config.call_extractor(child),
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

        for path in sorted(root.rglob(f"*.{ext}")):
            if any(fnmatch(path.name, pat) for pat in exclude):
                continue
            module_path = file_path_to_module(path, root, config)
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
            desc = unit.docstring or f"{unit.kind.capitalize()} in {unit.submodule}."
            deps = dependencies.get(unit.qualified_name, [])
            if deps:
                dep_refs = ", ".join(f"`@{d}`" for d in sorted(deps))
                desc = f"{desc} Depends on {dep_refs}."
            lines.append(desc)
            lines.append("")

    return "\n".join(lines)


def generate_layers_draft(symbol_index: dict[str, UnitInfo]) -> tuple[dict, set[str]]:
    """Generate a draft layers.json with modules/submodules in alphabetical order.

    Returns (layers_dict, valid_submodules). The valid_submodules set contains
    exactly the submodules that appear in the flattened layers — use it to filter
    units.md so both files stay consistent.

    Each module and submodule gets its own row. The user is expected to reorder
    rows and merge siblings to express the intended dependency hierarchy.
    """
    # Collect submodules and root modules
    submodules: set[str] = set()
    root_modules: set[str] = set()
    for unit in symbol_index.values():
        submodules.add(unit.submodule)
        root_modules.add(unit.submodule.split(".")[0])

    root_layers = [[m] for m in sorted(root_modules)]

    # Only add submodule_layers for root modules that have actual submodules.
    # If units exist directly at the root level (e.g. from __init__.py), the module
    # must stay a leaf — prepare.py doesn't support mixing root-level units with
    # submodule_layers.
    submodule_layers: dict[str, list[list[str]]] = {}
    valid_submodules: set[str] = set()
    for root in sorted(root_modules):
        has_root_units = root in submodules
        nested = sorted(sm for sm in submodules if sm.startswith(root + "."))
        if nested and not has_root_units:
            submodule_layers[root] = [[sm] for sm in nested]
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
    args = ap.parse_args()

    exclude_patterns = [p.strip() for p in args.exclude.split(",") if p.strip()]

    symbol_index, import_map = build_index(
        args.root.resolve(),
        exclude_patterns=exclude_patterns,
        include_private=args.include_private,
    )
    logger.info(f"Found {len(symbol_index)} units across {len(import_map)} modules")

    # Generate layers first, then filter units to match
    draft, valid_submodules = generate_layers_draft(symbol_index)
    symbol_index = filter_to_valid_submodules(symbol_index, valid_submodules)

    deps = resolve_dependencies(symbol_index, import_map)

    args.output.mkdir(parents=True, exist_ok=True)

    units_path = args.output / "units.md"
    units_path.write_text(format_units_md(symbol_index, deps), encoding="utf-8")
    logger.info(f"Wrote {units_path}")

    layers_path = args.output / "layers.json"
    layers_path.write_text(json.dumps(draft, indent=2) + "\n", encoding="utf-8")
    logger.info(f"Wrote {layers_path}")
