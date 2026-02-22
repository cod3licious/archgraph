import colorsys
import json
import logging
import re
from copy import deepcopy
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_unit_descriptions(unit_descriptions: str) -> tuple[dict, dict]:
    """Parse markdown → (units dict, unit_order dict).

    unit_order maps submodule → list of short unit *names* (not full paths),
    in the order they appear in the file.
    """
    units: dict[str, dict] = {}
    unit_order: dict[str, list[str]] = {}

    pattern = re.compile(r"^### ([^\n]+)\n(.*?)(?=^### |\Z)", re.MULTILINE | re.DOTALL)
    for header, body in pattern.findall(unit_descriptions):
        unit_path = header.strip()
        description = body.strip()

        if unit_path in units:
            raise ValueError(f"Duplicate unit path: {unit_path}")
        if "." not in unit_path:
            raise ValueError(f"Unit path has no dot separator: {unit_path!r}")

        dot = unit_path.rfind(".")
        submodule, name = unit_path[:dot], unit_path[dot + 1 :]
        dependencies = dict.fromkeys(re.findall(r"`@([\w.]+)`", description), True)

        units[unit_path] = {
            "submodule": submodule,
            "name": name,
            "description": description,
            "dependencies": dependencies,
        }
        unit_order.setdefault(submodule, []).append(name)  # short name, not full path

    return units, unit_order


def flatten_layers(layers: dict) -> list[str]:
    """Flatten root_layers/submodule_layers JSON into an ordered list of submodules.

    Iterates root_layers (list of lists). For each module:
    - If it has an entry in submodule_layers, expand its sub-rows in order.
    - Otherwise it is a leaf module and is added directly.

    The structure is exactly 2 levels deep (root → submodule), so no recursion
    is needed. O(total submodules).
    """
    root_layers: list[list[str]] = layers["root_layers"]
    submodule_layers: dict[str, list[list[str]]] = layers["submodule_layers"]

    all_submodules: list[str] = []
    seen: set[str] = set()

    def _add(sm: str) -> None:
        if sm in seen:
            raise ValueError(f"Duplicate submodule: '{sm}'")
        seen.add(sm)
        all_submodules.append(sm)

    for root_row in root_layers:
        for module in root_row:
            if module not in submodule_layers:
                _add(module)
            else:
                for sub_row in submodule_layers[module]:
                    for sm in sub_row:
                        if not sm.startswith(module + "."):
                            raise ValueError(f"Submodule '{sm}' does not start with parent module '{module}'")
                        _add(sm)

    return all_submodules


def validate_unit_paths(units: dict, all_submodules: list[str]) -> bool:
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


def create_submodules_dict(all_submodules: list[str], unit_order: dict) -> dict:
    """Build the submodules dict with default metadata.

    unit_order contains short unit names (not full paths); they are stored
    directly in 'units' for use by the frontend.
    """
    submodules: dict[str, dict] = {}
    for sm in all_submodules:
        units_list = unit_order.get(sm, [])
        if not units_list:
            logger.warning(f"Submodule {sm} has no units")
        submodules[sm] = {
            "module": sm.split(".")[0],
            "color": "#D3D3D3",
            "units": units_list,
            "dependencies": {},
        }
    return submodules


def assign_submodule_colors(submodules: dict, layers: dict) -> dict:
    """Assign pastel rainbow colors by root module.

    Colors are spread evenly across the hue wheel (HLS, high lightness, moderate
    saturation) based on the order modules appear in root_layers.
    Does not modify the input dict.
    """
    root_modules = [m for row in layers["root_layers"] for m in row]
    n = len(root_modules)
    module_colors: dict[str, str] = {}
    for i, module in enumerate(root_modules):
        h = i / n if n > 1 else 0.0
        r, g, b = colorsys.hls_to_rgb(h, 0.85, 0.55)
        module_colors[module] = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"

    # Shallow-copy each submodule dict — only 'color' is being replaced, and all
    # other values (units list, dependencies dict) are not mutated here.
    return {sm: {**sm_data, "color": module_colors.get(sm_data["module"], "#D3D3D3")} for sm, sm_data in submodules.items()}


def resolve_dependencies(units: dict) -> dict:
    """Resolve @-references to valid unit paths; remove/warn on bad ones.

    Edge cases (in priority order):
    1. Self-dependency → silently removed (not an error).
    2. Exact match in units → kept as True.
    3. One dot-strip resolves to an existing unit (sub-method ref) → matched
       with a WARNING; deduplicates if the parent was already listed.
    4. Unresolvable → ERROR logged, removed.

    Does not modify the input dict (works on a deepcopy).
    O(U * D) where U = units, D = max dependencies per unit.
    """
    result = deepcopy(units)
    unit_paths = set(units)
    error_count = 0

    for unit_path, unit in result.items():
        resolved: dict[str, bool] = {}
        for dep in unit["dependencies"]:
            if dep == unit_path:
                continue  # silent self-dep removal
            if dep in unit_paths:
                resolved[dep] = True
            else:
                # Try stripping the last segment (e.g. Model.predict → Model)
                parent = dep.rsplit(".", 1)[0] if "." in dep else None
                if parent and parent in unit_paths:
                    logger.warning(f"{unit_path} dependency {dep} was matched to {parent}")
                    resolved.setdefault(parent, True)  # deduplicates multiple sub-refs
                else:
                    logger.error(f"Referenced Unit Unknown: {unit_path} depends on {dep}, which could not be resolved")
                    error_count += 1
        unit["dependencies"] = resolved

    logger.info(f"Dependency resolution completed with {error_count} error(s)")
    return result


def _build_sm_info(layers: dict) -> dict[str, tuple[int, int, str]]:
    """Map each submodule → (root_row_idx, intra_row_idx, root_module).

    root_row_idx:  position of this submodule's root module in root_layers.
    intra_row_idx: position of this submodule's row within its root module's
                   submodule_layers (leaf modules get index 0).
    root_module:   the top-level module name.

    Built in O(S). Used by check_layer_violations for O(1) per-dep checks.
    """
    root_layers: list[list[str]] = layers["root_layers"]
    submodule_layers: dict[str, list[list[str]]] = layers["submodule_layers"]
    sm_info: dict[str, tuple[int, int, str]] = {}
    for root_row_idx, root_row in enumerate(root_layers):
        for module in root_row:
            if module not in submodule_layers:
                sm_info[module] = (root_row_idx, 0, module)
            else:
                for intra_row_idx, sub_row in enumerate(submodule_layers[module]):
                    for sm in sub_row:
                        sm_info[sm] = (root_row_idx, intra_row_idx, module)
    return sm_info


def check_layer_violations(units: dict, layers: dict) -> dict:
    """Flag dependencies that violate the layer hierarchy.

    A dependency from sm_a → sm_b is allowed iff:
    - sm_a == sm_b (intra-submodule call), or
    - sm_b is in a strictly lower root-layer row than sm_a, or
    - sm_a and sm_b share the same root module AND sm_b is in a strictly lower
      intra-module row than sm_a.

    Setup is O(S), each dependency check is O(1). Total: O(S + U*D).
    Does not modify the input dict.
    """
    sm_info = _build_sm_info(layers)
    result = deepcopy(units)

    for unit_path, unit in result.items():
        sm_a = unit["submodule"]
        info_a = sm_info.get(sm_a)
        if info_a is None:
            continue
        rr_a, ir_a, root_a = info_a
        for dep_path in unit["dependencies"]:
            dep_unit = units.get(dep_path)
            if dep_unit is None:
                continue
            sm_b = dep_unit["submodule"]
            if sm_a == sm_b:
                continue
            info_b = sm_info.get(sm_b)
            if info_b is None:
                continue
            rr_b, ir_b, root_b = info_b
            allowed = rr_b > rr_a or (rr_b == rr_a and root_a == root_b and ir_b > ir_a)
            if not allowed:
                logger.warning(f"Architecture Validation: {unit_path} must not depend on {dep_path}")
                unit["dependencies"][dep_path] = False

    return result


def assign_submodule_dependencies(submodules: dict, units: dict) -> dict:
    """Aggregate unit-level dependencies up to the submodule level.

    Keys in each submodule's dependencies dict are *target submodule paths*
    (not unit paths). The boolean is False if *any* unit-level dependency from
    this submodule to the target is a violation (False takes priority over True).
    Intra-submodule dependencies are skipped (no self-arrows in the graph).
    Does not modify either input dict.
    """
    result = deepcopy(submodules)
    for unit in units.values():
        sm_src = unit["submodule"]
        if sm_src not in result:
            continue
        sm_deps = result[sm_src]["dependencies"]
        for dep_unit_path, valid in unit["dependencies"].items():
            # derive the target submodule from the dep unit path
            dep_sm = dep_unit_path.rsplit(".", 1)[0]
            if dep_sm == sm_src:
                continue  # intra-submodule dep — no arrow
            # False (violation) takes priority: once False, never set back to True
            if dep_sm not in sm_deps or (not valid and sm_deps[dep_sm]):
                sm_deps[dep_sm] = valid
    return result


def process_files(unit_descriptions: str, layers: dict) -> dict:
    units, unit_order = parse_unit_descriptions(unit_descriptions)
    all_submodules = flatten_layers(layers)
    if not validate_unit_paths(units, all_submodules):
        raise ValueError("Unit path validation failed — see errors above")
    submodules = create_submodules_dict(all_submodules, unit_order)
    submodules = assign_submodule_colors(submodules, layers)
    units = resolve_dependencies(units)
    units = check_layer_violations(units, layers)
    submodules = assign_submodule_dependencies(submodules, units)
    return {"layers": layers, "submodules": submodules, "units": units}


if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Process architecture files into result.json")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", metavar="FOLDER", help="Folder containing layers.json and units.md")
    group.add_argument("--layers", metavar="FILE", help="Path to layers.json")
    parser.add_argument("--units", metavar="FILE", help="Path to units.md (required when --layers is used)")
    args = parser.parse_args()

    if args.input:
        base = Path(args.input)
        layers_path, units_path = base / "layers.json", base / "units.md"
    else:
        if not args.units:
            parser.error("--units is required when --layers is used")
        layers_path, units_path = Path(args.layers), Path(args.units)

    layers_data = json.loads(layers_path.read_text(encoding="utf-8"))
    unit_descriptions = units_path.read_text(encoding="utf-8")

    try:
        result = process_files(unit_descriptions, layers_data)
    except ValueError as e:
        logger.critical(str(e))
        sys.exit(1)

    output_path = Path(__file__).parent / "result.json"
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Saved result to {output_path}")
