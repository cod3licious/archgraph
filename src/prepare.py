import argparse
import colorsys
import json
import logging
import re
from collections.abc import Collection
from pathlib import Path

logger = logging.getLogger(__name__)

# CommonMark allows up to 3 spaces of indentation for headings and code fences
HEADING_RE = re.compile(r" {0,3}###(?=[ \t]|$)(.*)")
FENCE_RE = re.compile(r" {0,3}(`{3,}|~{3,})")
# Group 1 is everything inside the backticks after the @, group 2 the unit path (trailing "()", "," etc. ignored);
# path segments may contain hyphens, since file names (and thus module paths) do, e.g. in TypeScript
REF_RE = re.compile(r"`@(([\w-]+(?:\.[\w-]+)*)?[^`\n]*)`")


def parse_unit_descriptions(unit_descriptions: str) -> dict[str, dict]:
    """Parse markdown -> units dict {unit_path: {submodule, name, description, dependencies}}.

    Units keep the order in which they appear in the file. Text before the first
    heading is ignored and '###' lines inside fenced code blocks are part of the description.
    """
    sections: dict[str, list[str]] = {}
    body: list[str] = []
    fence = ""
    for line in unit_descriptions.splitlines():
        heading = None if fence else HEADING_RE.match(line)
        if heading:
            unit_path = heading.group(1).strip()
            if not unit_path:
                raise ValueError("Empty unit heading: '###' without a unit path")
            if unit_path in sections:
                raise ValueError(f"Duplicate unit path: {unit_path}")
            if "." not in unit_path:
                raise ValueError(f"Unit path has no dot separator: {unit_path!r}")
            body = sections[unit_path] = []
            continue
        body.append(line)
        # A fence is closed by a fence of the same character that is at least as long
        if (fence_match := FENCE_RE.match(line)) and (not fence or fence_match.group(1).startswith(fence)):
            fence = "" if fence else fence_match.group(1)
    if fence:
        logger.warning(f"Unclosed code fence ({fence}): all following '###' headings were treated as description text")

    units: dict[str, dict] = {}
    for unit_path, lines in sections.items():
        submodule, _, name = unit_path.rpartition(".")
        description = "\n".join(lines).strip()
        # Unparseable references are kept verbatim so resolve_dependencies reports them as unresolved
        refs = [ref_path or raw for raw, ref_path in REF_RE.findall(description)]
        units[unit_path] = {
            "submodule": submodule,
            "name": name,
            "description": description,
            "dependencies": dict.fromkeys(refs, True),
        }
    return units


def _is_rows(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(row, list) and all(isinstance(x, str) for x in row) for row in value)


def flatten_layers(layers: dict) -> dict[str, tuple[int, int, str]]:
    """Validate layers.json and flatten it into an ordered {submodule: (root_row_idx, intra_row_idx, module)} dict.

    root_row_idx:  position of the submodule's module in root_layers.
    intra_row_idx: position of the submodule's row within its module's submodule_layers.
    module:        the root module the submodule belongs to.

    A module without an entry in submodule_layers is a leaf and acts as its own
    (only) submodule. A module may also list itself in its submodule_layers to hold
    units defined directly at the module level next to its submodules.
    """
    if not isinstance(layers, dict) or not _is_rows(layers.get("root_layers")):
        raise ValueError("Invalid layers: 'root_layers' must be a list of lists of strings")
    submodule_layers = layers.get("submodule_layers", {})
    if not isinstance(submodule_layers, dict) or not all(_is_rows(rows) for rows in submodule_layers.values()):
        raise ValueError("Invalid layers: 'submodule_layers' must map module names to lists of lists of strings")

    sm_info: dict[str, tuple[int, int, str]] = {}
    for root_row_idx, root_row in enumerate(layers["root_layers"]):
        for module in root_row:
            for intra_row_idx, sub_row in enumerate(submodule_layers.get(module, [[module]])):
                for sm in sub_row:
                    if sm != module and not sm.startswith(module + "."):
                        raise ValueError(f"Submodule '{sm}' does not start with parent module '{module}'")
                    if sm in sm_info:
                        raise ValueError(f"Duplicate submodule: '{sm}'")
                    sm_info[sm] = (root_row_idx, intra_row_idx, module)
    return sm_info


def validate_unit_paths(units: dict, all_submodules: Collection[str]) -> bool:
    """Return True iff all unit paths are valid w.r.t. the submodule list."""
    submodule_set = set(all_submodules)
    valid = True
    for unit_path, unit in units.items():
        if unit_path in submodule_set:
            logger.error(
                f"Unit Is Submodule: {unit_path}: a unit is supposed to be contained in a submodule "
                "(like a function or class), not be the submodule itself"
            )
            valid = False
        elif unit["submodule"] not in submodule_set:
            logger.error(f"Unknown Submodule: {unit_path} is not part of any submodule in the provided architectural layers")
            valid = False
    return valid


def create_submodules_dict(sm_info: dict[str, tuple[int, int, str]], units: dict) -> dict:
    """Build the submodules dict with default metadata from flatten_layers' result.

    'units' holds the short unit names (not full paths) in the order they appear in units.
    """
    unit_names: dict[str, list[str]] = {}
    for unit in units.values():
        unit_names.setdefault(unit["submodule"], []).append(unit["name"])
    submodules: dict[str, dict] = {}
    for sm, (_, _, module) in sm_info.items():
        if sm not in unit_names:
            logger.warning(f"Submodule {sm} has no units")
        submodules[sm] = {
            "module": module,
            "color": "#D3D3D3",
            "units": unit_names.get(sm, []),
            "dependencies": {},
        }
    return submodules


def assign_submodule_colors(submodules: dict, layers: dict) -> dict:
    """Assign muted earthy colors by root module.

    Hues are spread evenly across 0.0-0.85 of the hue wheel (terracotta ->
    ochre -> sage -> dusty teal -> muted violet), skipping the 0.85-1.0 cyan/
    electric-blue range. Lower lightness (0.80) and saturation (0.40) give a
    clay-like quality instead of candy pastels.
    Does not modify the input dict.
    """
    root_modules = [m for row in layers["root_layers"] for m in row]
    n = len(root_modules)
    module_colors: dict[str, str] = {}
    for i, module in enumerate(root_modules):
        h = (i / n if n > 1 else 0.0) * 0.85  # stay in earthy hue range
        r, g, b = colorsys.hls_to_rgb(h, 0.80, 0.40)
        module_colors[module] = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"

    # Shallow-copy each submodule dict - only 'color' is being replaced, and all
    # other values (units list, dependencies dict) are not mutated here.
    return {sm: {**sm_data, "color": module_colors.get(sm_data["module"], "#D3D3D3")} for sm, sm_data in submodules.items()}


def resolve_dependencies(units: dict, *, strict: bool = False) -> dict:
    """Resolve @-references to valid unit paths; remove/warn on bad ones.

    Edge cases:
    1. Exact match in units -> kept as True.
    2. One dot-strip resolves to an existing unit (sub-method ref) -> matched
       with a WARNING; deduplicates if the parent was already listed.
    3. Resolves to the unit itself (recursion, own method) -> silently removed.
    4. Unresolvable -> ERROR logged, removed. With strict=True, a ValueError is
       raised after all of them were logged.

    Shallow-copies each unit dict, replacing only the 'dependencies' value.
    O(U * D) where U = units, D = max dependencies per unit.
    """
    error_count = 0
    result: dict[str, dict] = {}

    for unit_path, unit in units.items():
        resolved: dict[str, bool] = {}
        for dep in unit["dependencies"]:
            target = dep if dep in units else dep.rpartition(".")[0]
            if target not in units:
                logger.error(f"Referenced Unit Unknown: {unit_path} depends on {dep}, which could not be resolved")
                error_count += 1
            elif target != unit_path:
                if target != dep:
                    logger.warning(f"{unit_path} dependency {dep} was matched to {target}")
                resolved[target] = True
        result[unit_path] = {**unit, "dependencies": resolved}

    logger.info(f"Dependency resolution completed with {error_count} error(s)")
    if strict and error_count:
        raise ValueError(f"{error_count} referenced unit(s) could not be resolved (strict mode)")
    return result


def _dependency_order(paths: list[str], units: dict, *, high_level_units_first: bool) -> list[str]:
    """Order the units of one submodule so their dependencies on each other point in the expected direction.

    Units are taken in the given order, and each is placed right after its prerequisites
    that aren't placed yet (recursively, depth-first): its dependencies (low-level first)
    or the units depending on it (high-level first). An already valid order is therefore
    kept as is, and a unit only moves up if something above it needs it. In a dependency
    cycle, the prerequisite that closes the cycle is skipped, so that one dependency
    points in the wrong direction.
    """
    deps = {path: [d for d in paths if d in units[path]["dependencies"] and d != path] for path in paths}
    prereqs = {path: [p for p in paths if path in deps[p]] for path in paths} if high_level_units_first else deps
    order: list[str] = []
    seen: set[str] = set()
    for start in paths:
        if start in seen:
            continue
        seen.add(start)
        # Iterative depth-first search, so long dependency chains can't hit the recursion limit
        stack = [(start, iter(prereqs[start]))]
        while stack:
            path, pending = stack[-1]
            nxt = next((p for p in pending if p not in seen), None)
            if nxt is None:
                stack.pop()
                order.append(path)
            else:
                seen.add(nxt)
                stack.append((nxt, iter(prereqs[nxt])))
    return order


def sort_units_by_dependencies(units: dict, *, high_level_units_first: bool = False) -> dict:
    """Reorder the units within each submodule by their dependencies on each other (see _dependency_order).

    Each submodule's units take up the same positions in the dict as before, just in a
    different order, so units of different submodules are never mixed up.
    Does not modify the input dict.
    """
    by_submodule: dict[str, list[str]] = {}
    for path, unit in units.items():
        by_submodule.setdefault(unit["submodule"], []).append(path)
    ordered = {
        sm: iter(_dependency_order(paths, units, high_level_units_first=high_level_units_first))
        for sm, paths in by_submodule.items()
    }
    return {(path := next(ordered[unit["submodule"]])): units[path] for unit in units.values()}


def check_layer_violations(
    units: dict, sm_info: dict[str, tuple[int, int, str]], *, high_level_units_first: bool = False
) -> dict:
    """Flag dependencies that violate the layer hierarchy (sm_info from flatten_layers).

    A dependency from unit_a -> unit_b is allowed iff:
    - Cross-submodule: sm_b is in a strictly lower root-layer row than sm_a,
      or sm_a and sm_b share the same root module AND sm_b is in a strictly
      lower intra-module row.
    - Intra-submodule: depends on high_level_units_first. By default
      (Python convention), units listed first are leaf-level, so a unit may
      only depend on units *above* it (lower index). With
      high_level_units_first=True (e.g. Java/C#), a unit may only depend
      on units *below* it (higher index).

    Each dependency check is O(1). Total: O(U*D).
    Does not modify the input dict.
    """
    # Global position suffices, since only units within the same submodule are compared
    unit_pos = {unit_path: idx for idx, unit_path in enumerate(units)}
    result: dict[str, dict] = {}

    for unit_path, unit in units.items():
        rr_a, ir_a, root_a = sm_info[unit["submodule"]]
        resolved: dict[str, bool] = {}
        for dep_path, valid in unit["dependencies"].items():
            sm_b = units[dep_path]["submodule"]
            if unit["submodule"] == sm_b:
                # Intra-submodule: direction depends on convention
                pos_a, pos_b = unit_pos[unit_path], unit_pos[dep_path]
                allowed = pos_b > pos_a if high_level_units_first else pos_b < pos_a
                if not allowed:
                    logger.warning(f"Architecture Validation (intra-submodule): {unit_path} must not depend on {dep_path}")
            else:
                rr_b, ir_b, root_b = sm_info[sm_b]
                allowed = rr_b > rr_a or (rr_b == rr_a and root_a == root_b and ir_b > ir_a)
                if not allowed:
                    logger.warning(f"Architecture Validation: {unit_path} must not depend on {dep_path}")
            resolved[dep_path] = valid and allowed
        result[unit_path] = {**unit, "dependencies": resolved}

    return result


def assign_submodule_dependencies(submodules: dict, units: dict) -> dict:
    """Aggregate unit-level dependencies up to the submodule level.

    Keys in each submodule's dependencies dict are *target submodule paths*
    (not unit paths). The boolean is False if *any* unit-level dependency from
    this submodule to the target is a violation (False takes priority over True).
    Intra-submodule dependencies are skipped (no self-arrows in the graph).
    Shallow-copies each submodule dict, replacing only the 'dependencies' value.
    Does not modify either input dict.
    """
    sm_deps: dict[str, dict[str, bool]] = {sm: {} for sm in submodules}
    for unit in units.values():
        sm_src = unit["submodule"]
        deps = sm_deps[sm_src]
        for dep_unit_path, valid in unit["dependencies"].items():
            dep_sm = units[dep_unit_path]["submodule"]
            if dep_sm == sm_src:
                continue  # intra-submodule dep - no arrow
            deps[dep_sm] = deps.get(dep_sm, True) and valid
    return {sm: {**sm_data, "dependencies": sm_deps[sm]} for sm, sm_data in submodules.items()}


def process_files(
    unit_descriptions: str,
    layers: dict,
    *,
    high_level_units_first: bool = False,
    strict: bool = False,
    sort_units: bool = False,
) -> dict:
    units = parse_unit_descriptions(unit_descriptions)
    sm_info = flatten_layers(layers)
    if not validate_unit_paths(units, sm_info):
        raise ValueError("Unit path validation failed - see errors above")
    units = resolve_dependencies(units, strict=strict)
    if sort_units:
        units = sort_units_by_dependencies(units, high_level_units_first=high_level_units_first)
    submodules = create_submodules_dict(sm_info, units)
    submodules = assign_submodule_colors(submodules, layers)
    units = check_layer_violations(units, sm_info, high_level_units_first=high_level_units_first)
    submodules = assign_submodule_dependencies(submodules, units)
    return {
        "layers": {"submodule_layers": {}, **layers},
        "submodules": submodules,
        "units": units,
        "high_level_units_first": high_level_units_first,
    }


def prepare_folder(
    input_dir: Path,
    output_dir: Path,
    *,
    high_level_units_first: bool = False,
    strict: bool = False,
    sort_units: bool = False,
) -> Path:
    """Process input_dir/layers.json and input_dir/units.md into output_dir/result.json; returns its path."""
    # utf-8-sig strips a BOM, which would otherwise break the JSON parsing and hide the first unit heading
    layers = json.loads((input_dir / "layers.json").read_text(encoding="utf-8-sig"))
    unit_descriptions = (input_dir / "units.md").read_text(encoding="utf-8-sig")
    result = process_files(
        unit_descriptions, layers, high_level_units_first=high_level_units_first, strict=strict, sort_units=sort_units
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "result.json"
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Saved result to {output_path}")
    return output_path


def add_options(parser: argparse.ArgumentParser) -> None:
    """Add prepare's processing options (shared with archgraph.py)."""
    parser.add_argument(
        "--high-level-units-first",
        action="store_true",
        help="Assume high-level units are listed before their dependencies (e.g. Java/C#). "
        "Default assumes low-level units first (Python convention).",
    )
    parser.add_argument("--strict", action="store_true", help="Fail if any `@` reference cannot be resolved to a unit.")
    parser.add_argument(
        "--sort-units",
        action="store_true",
        help="Reorder the units within each submodule by their dependencies on each other (in the direction set by "
        "--high-level-units-first), e.g. when the order in units.md is arbitrary.",
    )


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Process layers.json and units.md into result.json")
    parser.add_argument(
        "--input", required=True, type=Path, metavar="FOLDER", help="Folder containing layers.json and units.md"
    )
    parser.add_argument("--output", type=Path, metavar="FOLDER", help="Folder for result.json (default: the input folder)")
    add_options(parser)
    args = parser.parse_args()

    try:
        prepare_folder(
            args.input,
            args.output or args.input,
            high_level_units_first=args.high_level_units_first,
            strict=args.strict,
            sort_units=args.sort_units,
        )
    except (OSError, ValueError) as e:
        logger.critical(str(e))
        sys.exit(1)
