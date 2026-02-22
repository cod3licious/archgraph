import logging
import re
from copy import deepcopy

import pytest

from prepare import (
    assign_submodule_colors,
    assign_submodule_dependencies,
    check_layer_violations,
    create_submodules_dict,
    flatten_layers,
    parse_unit_descriptions,
    process_files,
    resolve_dependencies,
    validate_unit_paths,
)

LAYERS = {
    "root_layers": [["main", "api"], ["db"], ["core"]],
    "submodule_layers": {
        "api": [["api.routes"]],
        "db": [["db.commands"], ["db.queries.sample", "db.queries.config"]],
        "core": [["core.optimization", "core.prediction"], ["core.common"]],
    },
}

UNITS_MD = """\
### api.routes.get_samples

Calls `@db.queries.sample.get_samples` and returns results.

### api.routes.delete_config

Calls `@db.queries.config.get_config` to verify, then deletes.

### db.queries.sample.get_samples

Fetches samples from DB.

### db.queries.config.get_config

Fetches config from DB.

### db.commands.create_predictions

Calls `@db.queries.sample.get_samples` then `@core.prediction.Model`.

### core.prediction.Model

A model with fit and predict methods. Calls `@core.common.preprocess`.

### core.common.preprocess

Shared preprocessing utility.

### main.run

Calls `@db.queries.sample.get_samples` to kick off the pipeline.
"""


def _capture(fn, *args, caplog, **kwargs):
    with caplog.at_level(logging.DEBUG, logger="prepare"):
        result = fn(*args, **kwargs)
    return result, caplog


def _make_units(deps_map: dict) -> dict:
    return {
        path: {
            "submodule": path.rsplit(".", 1)[0],
            "name": path.rsplit(".", 1)[1],
            "description": "",
            "dependencies": dict.fromkeys(deps, True),
        }
        for path, deps in deps_map.items()
    }


# ---------------------------------------------------------------------------
# parse_unit_descriptions
# ---------------------------------------------------------------------------


def test_parse_basic_parsing():
    units, _ = parse_unit_descriptions(UNITS_MD)
    assert "api.routes.get_samples" in units
    u = units["api.routes.get_samples"]
    assert u["submodule"] == "api.routes"
    assert u["name"] == "get_samples"
    assert "db.queries.sample.get_samples" in u["dependencies"]


def test_parse_description_stripped():
    units, _ = parse_unit_descriptions("### a.b.c\n\n  hello world  \n\n")
    assert units["a.b.c"]["description"] == "hello world"


def test_parse_unit_order_contains_short_names():
    _, order = parse_unit_descriptions(UNITS_MD)
    # unit_order stores short names, not full paths
    assert order["api.routes"] == ["get_samples", "delete_config"]


def test_parse_unit_order_all_submodules_present():
    _, order = parse_unit_descriptions(UNITS_MD)
    assert set(order.keys()) == {
        "api.routes",
        "db.queries.sample",
        "db.queries.config",
        "db.commands",
        "core.prediction",
        "core.common",
        "main",
    }


def test_parse_multiple_deps():
    units, _ = parse_unit_descriptions("### a.b.f\n\nUses `@a.b.g` and `@c.d.h`.")
    assert units["a.b.f"]["dependencies"] == {"a.b.g": True, "c.d.h": True}


def test_parse_no_deps():
    units, _ = parse_unit_descriptions("### a.b.f\n\nNo references here.")
    assert units["a.b.f"]["dependencies"] == {}


def test_parse_duplicate_unit_path_raises():
    md = "### a.b.f\n\nhello\n\n### a.b.f\n\nworld"
    with pytest.raises(ValueError):
        parse_unit_descriptions(md)


def test_parse_empty_input():
    units, order = parse_unit_descriptions("")
    assert units == {}
    assert order == {}


def test_parse_no_dot_in_path_raises():
    with pytest.raises(ValueError):
        parse_unit_descriptions("### nodot\n\nhello")


def test_parse_dependencies_initially_all_true():
    units, _ = parse_unit_descriptions("### a.b.f\n\n`@a.b.g` and `@c.d.h`")
    assert all(v is True for v in units["a.b.f"]["dependencies"].values())


def test_parse_preamble_before_first_header_ignored():
    units, _ = parse_unit_descriptions("# Some title\n\nIntro.\n\n### a.b.f\n\nhello")
    assert "a.b.f" in units
    assert len(units) == 1


def test_parse_description_multiline():
    md = "### a.b.f\n\nLine 1.\n\nLine 2.\n\nLine 3."
    units, _ = parse_unit_descriptions(md)
    assert units["a.b.f"]["description"] == "Line 1.\n\nLine 2.\n\nLine 3."


def test_parse_dep_in_backticks_only():
    # bare @ref without backticks should NOT be picked up as a dependency
    units, _ = parse_unit_descriptions("### a.b.f\n\nSee @a.b.g for details (not a dep).")
    assert "a.b.g" not in units["a.b.f"]["dependencies"]


def test_parse_at_in_backticks_without_at_not_a_dep():
    # backtick without @ should not be picked up
    units, _ = parse_unit_descriptions("### a.b.f\n\nSee `a.b.g` (not a dep).")
    assert units["a.b.f"]["dependencies"] == {}


# ---------------------------------------------------------------------------
# flatten_layers
# ---------------------------------------------------------------------------


def _idx(result):
    return {sm: i for i, sm in enumerate(result)}


def test_flatten_basic_order():
    idx = _idx(flatten_layers(LAYERS))
    assert idx["main"] < idx["db.commands"]
    assert idx["db.commands"] < idx["db.queries.sample"]
    assert idx["db.queries.sample"] < idx["core.optimization"]
    assert idx["core.common"] > idx["core.optimization"]


def test_flatten_all_submodules_present():
    assert set(flatten_layers(LAYERS)) == {
        "main",
        "api.routes",
        "db.commands",
        "db.queries.sample",
        "db.queries.config",
        "core.optimization",
        "core.prediction",
        "core.common",
    }


def test_flatten_leaf_module_included():
    layers = {
        "root_layers": [["a", "b"]],
        "submodule_layers": {"a": [["a.x"]]},
    }
    result = flatten_layers(layers)
    assert "b" in result
    assert "a.x" in result


def test_flatten_leaf_comes_before_its_peer_submodule():
    # "b" is a leaf, "a.x" is a submodule; both in the same root layer row
    layers = {
        "root_layers": [["a", "b"]],
        "submodule_layers": {"a": [["a.x"]]},
    }
    result = flatten_layers(layers)
    # a.x should appear (from a's expansion), b should appear directly; both present
    assert set(result) == {"a.x", "b"}


def test_flatten_bad_prefix_raises():
    layers = {
        "root_layers": [["db"]],
        "submodule_layers": {"db": [["api.routes"]]},  # wrong prefix
    }
    with pytest.raises(ValueError):
        flatten_layers(layers)


def test_flatten_duplicate_submodule_raises():
    layers = {
        "root_layers": [["a"], ["b"]],
        "submodule_layers": {"a": [["a.x"]], "b": [["a.x"]]},  # a.x appears twice
    }
    with pytest.raises(ValueError):
        flatten_layers(layers)


def test_flatten_single_leaf_module():
    layers = {"root_layers": [["main"]], "submodule_layers": {}}
    assert flatten_layers(layers) == ["main"]


def test_flatten_multiple_root_rows_ordering():
    layers = {
        "root_layers": [["x"], ["y"]],
        "submodule_layers": {"x": [["x.a"]], "y": [["y.a"]]},
    }
    result = flatten_layers(layers)
    assert result.index("x.a") < result.index("y.a")


def test_flatten_sibling_order_within_row_preserved():
    idx = _idx(flatten_layers(LAYERS))
    assert idx["db.queries.sample"] < idx["db.queries.config"]


def test_flatten_submodule_layer_row_order_preserved():
    # db.commands is in a higher layer row than db.queries.*
    idx = _idx(flatten_layers(LAYERS))
    assert idx["db.commands"] < idx["db.queries.sample"]
    assert idx["db.commands"] < idx["db.queries.config"]


def test_flatten_empty_submodule_layers():
    layers = {
        "root_layers": [["main"], ["api"]],
        "submodule_layers": {},
    }
    result = flatten_layers(layers)
    assert result == ["main", "api"]


# ---------------------------------------------------------------------------
# validate_unit_paths
# ---------------------------------------------------------------------------

ALL_SM = ["api.routes", "db.commands"]


def test_validate_all_valid(caplog):
    units = {"api.routes.f": {"submodule": "api.routes", "name": "f", "description": "", "dependencies": {}}}
    result, caplog = _capture(validate_unit_paths, units, ALL_SM, caplog=caplog)
    assert result is True
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_validate_unit_is_submodule(caplog):
    units = {"api.routes": {"submodule": "api", "name": "routes", "description": "", "dependencies": {}}}
    result, caplog = _capture(validate_unit_paths, units, ALL_SM, caplog=caplog)
    assert result is False
    assert "Unit Is Submodule: api.routes" in caplog.text


def test_validate_unknown_submodule(caplog):
    units = {"unknown.mod.f": {"submodule": "unknown.mod", "name": "f", "description": "", "dependencies": {}}}
    result, caplog = _capture(validate_unit_paths, units, ALL_SM, caplog=caplog)
    assert result is False
    assert "Unknown Submodule: unknown.mod.f" in caplog.text


def test_validate_multiple_errors_returns_false(caplog):
    units = {
        "api.routes": {"submodule": "api", "name": "routes", "description": "", "dependencies": {}},
        "bad.mod.f": {"submodule": "bad.mod", "name": "f", "description": "", "dependencies": {}},
    }
    result, _ = _capture(validate_unit_paths, units, ALL_SM, caplog=caplog)
    assert result is False


def test_validate_empty_units_valid(caplog):
    result, _ = _capture(validate_unit_paths, {}, ALL_SM, caplog=caplog)
    assert result is True


def test_validate_both_errors_logged(caplog):
    units = {
        "api.routes": {"submodule": "api", "name": "routes", "description": "", "dependencies": {}},
        "bad.mod.f": {"submodule": "bad.mod", "name": "f", "description": "", "dependencies": {}},
    }
    _, caplog = _capture(validate_unit_paths, units, ALL_SM, caplog=caplog)
    assert "Unit Is Submodule: api.routes" in caplog.text
    assert "Unknown Submodule: bad.mod.f" in caplog.text


# ---------------------------------------------------------------------------
# create_submodules_dict
# ---------------------------------------------------------------------------


def test_create_submodules_basic_structure(caplog):
    result, _ = _capture(
        create_submodules_dict,
        ["api.routes", "db.commands"],
        {"api.routes": ["get_samples"], "db.commands": ["create_predictions"]},
        caplog=caplog,
    )
    sm = result["api.routes"]
    assert sm["module"] == "api"
    assert sm["color"] == "#D3D3D3"
    assert sm["units"] == ["get_samples"]
    assert sm["dependencies"] == {}
    # no "name" field
    assert "name" not in sm


def test_create_submodules_top_level_module_no_dot(caplog):
    result, _ = _capture(create_submodules_dict, ["main"], {"main": ["run"]}, caplog=caplog)
    assert result["main"]["module"] == "main"


def test_create_submodules_missing_units_warns(caplog):
    _, caplog = _capture(create_submodules_dict, ["api.routes"], {}, caplog=caplog)
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "api.routes" in caplog.text


def test_create_submodules_order_preserved(caplog):
    result, _ = _capture(create_submodules_dict, ["a.x", "b.y", "c.z"], {}, caplog=caplog)
    assert list(result.keys()) == ["a.x", "b.y", "c.z"]


def test_create_submodules_empty(caplog):
    result, _ = _capture(create_submodules_dict, [], {}, caplog=caplog)
    assert result == {}


def test_create_submodules_units_are_short_names(caplog):
    # unit_order contains short names; they should pass through unchanged
    result, _ = _capture(
        create_submodules_dict,
        ["core.common"],
        {"core.common": ["preprocess", "load_data"]},
        caplog=caplog,
    )
    assert result["core.common"]["units"] == ["preprocess", "load_data"]


def test_create_submodules_empty_unit_list_for_missing_submodule(caplog):
    result, _ = _capture(create_submodules_dict, ["api.routes"], {}, caplog=caplog)
    assert result["api.routes"]["units"] == []


# ---------------------------------------------------------------------------
# assign_submodule_colors
# ---------------------------------------------------------------------------


def _sm(module):
    return {"module": module, "color": "#D3D3D3", "units": [], "dependencies": {}}


def test_colors_differ_across_modules():
    submodules = {"a.x": _sm("a"), "b.y": _sm("b")}
    layers = {"root_layers": [["a"], ["b"]], "submodule_layers": {"a": [["a.x"]], "b": [["b.y"]]}}
    result = assign_submodule_colors(submodules, layers)
    assert result["a.x"]["color"] != result["b.y"]["color"]


def test_same_module_same_color():
    submodules = {"a.x": _sm("a"), "a.y": _sm("a")}
    layers = {"root_layers": [["a"]], "submodule_layers": {"a": [["a.x"], ["a.y"]]}}
    result = assign_submodule_colors(submodules, layers)
    assert result["a.x"]["color"] == result["a.y"]["color"]


def test_color_is_hex():
    submodules = {"a.x": _sm("a")}
    layers = {"root_layers": [["a"]], "submodule_layers": {"a": [["a.x"]]}}
    result = assign_submodule_colors(submodules, layers)
    color = result["a.x"]["color"]
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", color)


def test_colors_does_not_modify_original():
    submodules = {"a.x": _sm("a")}
    original = deepcopy(submodules)
    layers = {"root_layers": [["a"]], "submodule_layers": {"a": [["a.x"]]}}
    assign_submodule_colors(submodules, layers)
    assert submodules == original


def test_single_module_not_grey():
    submodules = {"main": _sm("main")}
    layers = {"root_layers": [["main"]], "submodule_layers": {}}
    result = assign_submodule_colors(submodules, layers)
    assert result["main"]["color"] != "#D3D3D3"


def test_colors_all_modules_in_rainbow_order_differ():
    # with 3+ modules, all should get distinct colors
    submodules = {"a.x": _sm("a"), "b.y": _sm("b"), "c.z": _sm("c")}
    layers = {
        "root_layers": [["a"], ["b"], ["c"]],
        "submodule_layers": {"a": [["a.x"]], "b": [["b.y"]], "c": [["c.z"]]},
    }
    result = assign_submodule_colors(submodules, layers)
    colors = [result[k]["color"] for k in ["a.x", "b.y", "c.z"]]
    assert len(set(colors)) == 3


# ---------------------------------------------------------------------------
# resolve_dependencies
# ---------------------------------------------------------------------------


def test_resolve_valid_dep_kept(caplog):
    units = _make_units({"a.b.f": ["a.b.g"], "a.b.g": []})
    result, _ = _capture(resolve_dependencies, units, caplog=caplog)
    assert "a.b.g" in result["a.b.f"]["dependencies"]


def test_resolve_self_dep_removed(caplog):
    units = _make_units({"a.b.f": ["a.b.f"]})
    result, _ = _capture(resolve_dependencies, units, caplog=caplog)
    assert "a.b.f" not in result["a.b.f"]["dependencies"]


def test_resolve_subunit_matched_to_parent(caplog):
    units = _make_units({"a.b.f": ["a.b.Model.predict"], "a.b.Model": []})
    result, caplog = _capture(resolve_dependencies, units, caplog=caplog)
    assert "a.b.Model" in result["a.b.f"]["dependencies"]
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_resolve_unknown_dep_removed(caplog):
    units = _make_units({"a.b.f": ["x.y.z"]})
    result, caplog = _capture(resolve_dependencies, units, caplog=caplog)
    assert "x.y.z" not in result["a.b.f"]["dependencies"]
    assert "Referenced Unit Unknown" in caplog.text


def test_resolve_error_count_in_summary(caplog):
    units = _make_units({"a.b.f": ["x.y.z", "p.q.r"]})
    _, caplog = _capture(resolve_dependencies, units, caplog=caplog)
    assert "2 error(s)" in caplog.text


def test_resolve_zero_errors_summary(caplog):
    units = _make_units({"a.b.f": []})
    _, caplog = _capture(resolve_dependencies, units, caplog=caplog)
    assert "0 error(s)" in caplog.text


def test_resolve_does_not_modify_original(caplog):
    units = _make_units({"a.b.f": ["x.y.z"]})
    original = deepcopy(units)
    _capture(resolve_dependencies, units, caplog=caplog)
    assert units == original


def test_resolve_subunit_match_deduplicates(caplog):
    units = _make_units({"a.b.f": ["a.b.Model.fit", "a.b.Model.predict"], "a.b.Model": []})
    result, _ = _capture(resolve_dependencies, units, caplog=caplog)
    keys = list(result["a.b.f"]["dependencies"].keys())
    assert keys.count("a.b.Model") == 1


def test_resolve_valid_dep_stays_true(caplog):
    units = _make_units({"a.b.f": ["a.b.g"], "a.b.g": []})
    result, _ = _capture(resolve_dependencies, units, caplog=caplog)
    assert result["a.b.f"]["dependencies"]["a.b.g"] is True


def test_resolve_self_dep_not_counted_as_error(caplog):
    units = _make_units({"a.b.f": ["a.b.f"]})
    _, caplog = _capture(resolve_dependencies, units, caplog=caplog)
    assert "0 error(s)" in caplog.text


# ---------------------------------------------------------------------------
# check_layer_violations
# ---------------------------------------------------------------------------


def _violation_units(unit_a, sm_a, dep_b, sm_b):
    return {
        unit_a: {"submodule": sm_a, "name": unit_a.split(".")[-1], "description": "", "dependencies": {dep_b: True}},
        dep_b: {"submodule": sm_b, "name": dep_b.split(".")[-1], "description": "", "dependencies": {}},
    }


def test_check_valid_cross_module_dep(caplog):
    units = _violation_units("api.routes.f", "api.routes", "db.commands.g", "db.commands")
    result, _ = _capture(check_layer_violations, units, LAYERS, caplog=caplog)
    assert result["api.routes.f"]["dependencies"]["db.commands.g"] is True


def test_check_invalid_upward_dep(caplog):
    units = _violation_units("db.commands.g", "db.commands", "api.routes.f", "api.routes")
    result, caplog = _capture(check_layer_violations, units, LAYERS, caplog=caplog)
    assert result["db.commands.g"]["dependencies"]["api.routes.f"] is False
    assert "Architecture Validation" in caplog.text


def test_check_invalid_same_root_layer_different_module(caplog):
    units = _violation_units("main.run", "main", "api.routes.f", "api.routes")
    result, _ = _capture(check_layer_violations, units, LAYERS, caplog=caplog)
    assert result["main.run"]["dependencies"]["api.routes.f"] is False


def test_check_valid_intra_module_downward(caplog):
    units = _violation_units("db.commands.g", "db.commands", "db.queries.sample.f", "db.queries.sample")
    result, _ = _capture(check_layer_violations, units, LAYERS, caplog=caplog)
    assert result["db.commands.g"]["dependencies"]["db.queries.sample.f"] is True


def test_check_invalid_intra_module_upward(caplog):
    units = _violation_units("db.queries.sample.f", "db.queries.sample", "db.commands.g", "db.commands")
    result, _ = _capture(check_layer_violations, units, LAYERS, caplog=caplog)
    assert result["db.queries.sample.f"]["dependencies"]["db.commands.g"] is False


def test_check_invalid_same_intra_layer_siblings(caplog):
    units = _violation_units(
        "db.queries.sample.f",
        "db.queries.sample",
        "db.queries.config.g",
        "db.queries.config",
    )
    result, _ = _capture(check_layer_violations, units, LAYERS, caplog=caplog)
    assert result["db.queries.sample.f"]["dependencies"]["db.queries.config.g"] is False


def test_check_same_submodule_always_allowed(caplog):
    units = _violation_units("db.commands.f", "db.commands", "db.commands.g", "db.commands")
    result, _ = _capture(check_layer_violations, units, LAYERS, caplog=caplog)
    assert result["db.commands.f"]["dependencies"]["db.commands.g"] is True


def test_check_does_not_modify_original(caplog):
    units = _violation_units("db.commands.g", "db.commands", "api.routes.f", "api.routes")
    original = deepcopy(units)
    _capture(check_layer_violations, units, LAYERS, caplog=caplog)
    assert units == original


def test_check_core_cannot_depend_on_higher(caplog):
    units = _violation_units("core.common.f", "core.common", "db.commands.g", "db.commands")
    result, _ = _capture(check_layer_violations, units, LAYERS, caplog=caplog)
    assert result["core.common.f"]["dependencies"]["db.commands.g"] is False


def test_check_lower_module_can_depend_on_same_lower_layer(caplog):
    # core.optimization and core.prediction are siblings — neither can depend on the other
    units = _violation_units("core.optimization.f", "core.optimization", "core.prediction.g", "core.prediction")
    result, _ = _capture(check_layer_violations, units, LAYERS, caplog=caplog)
    assert result["core.optimization.f"]["dependencies"]["core.prediction.g"] is False


def test_check_lower_submodule_can_depend_on_lower_submodule(caplog):
    # core.optimization and core.common: core.common is in a lower layer
    units = _violation_units("core.optimization.f", "core.optimization", "core.common.g", "core.common")
    result, _ = _capture(check_layer_violations, units, LAYERS, caplog=caplog)
    assert result["core.optimization.f"]["dependencies"]["core.common.g"] is True


def test_check_cross_module_lower_to_higher_invalid(caplog):
    # core (bottom) -> api (top): invalid
    units = _violation_units("core.common.f", "core.common", "api.routes.g", "api.routes")
    result, _ = _capture(check_layer_violations, units, LAYERS, caplog=caplog)
    assert result["core.common.f"]["dependencies"]["api.routes.g"] is False


# ---------------------------------------------------------------------------
# assign_submodule_dependencies
# ---------------------------------------------------------------------------


def _bare_sm(name, module):
    return {"module": module, "color": "#fff", "units": [], "dependencies": {}}


def test_assign_unit_deps_aggregated_to_submodule():
    submodules = {"a.x": _bare_sm("a.x", "a"), "b.y": _bare_sm("b.y", "b")}
    units = {
        "a.x.f": {"submodule": "a.x", "name": "f", "description": "", "dependencies": {"b.y.g": True}},
        "b.y.g": {"submodule": "b.y", "name": "g", "description": "", "dependencies": {}},
    }
    result = assign_submodule_dependencies(submodules, units)
    # key is the target submodule, not the unit path
    assert "b.y" in result["a.x"]["dependencies"]
    assert result["a.x"]["dependencies"]["b.y"] is True


def test_assign_violation_flag_preserved():
    submodules = {"a.x": _bare_sm("a.x", "a")}
    units = {"a.x.f": {"submodule": "a.x", "name": "f", "description": "", "dependencies": {"b.y.g": False}}}
    result = assign_submodule_dependencies(submodules, units)
    assert result["a.x"]["dependencies"]["b.y"] is False


def test_assign_any_violation_makes_arrow_red():
    # two units in a.x both depend on b.y; one valid, one violation => False
    submodules = {"a.x": _bare_sm("a.x", "a")}
    units = {
        "a.x.f": {"submodule": "a.x", "name": "f", "description": "", "dependencies": {"b.y.g": True}},
        "a.x.h": {"submodule": "a.x", "name": "h", "description": "", "dependencies": {"b.y.g": False}},
    }
    result = assign_submodule_dependencies(submodules, units)
    assert result["a.x"]["dependencies"]["b.y"] is False


def test_assign_all_valid_keeps_true():
    submodules = {"a.x": _bare_sm("a.x", "a")}
    units = {
        "a.x.f": {"submodule": "a.x", "name": "f", "description": "", "dependencies": {"b.y.g": True}},
        "a.x.h": {"submodule": "a.x", "name": "h", "description": "", "dependencies": {"b.y.i": True}},
    }
    result = assign_submodule_dependencies(submodules, units)
    assert result["a.x"]["dependencies"]["b.y"] is True


def test_assign_does_not_modify_originals():
    submodules = {"a.x": _bare_sm("a.x", "a")}
    orig_sm = deepcopy(submodules)
    assign_submodule_dependencies(submodules, {})
    assert submodules == orig_sm


def test_assign_multiple_target_submodules():
    submodules = {"a.x": _bare_sm("a.x", "a")}
    units = {
        "a.x.f": {"submodule": "a.x", "name": "f", "description": "", "dependencies": {"b.y.h": True}},
        "a.x.g": {"submodule": "a.x", "name": "g", "description": "", "dependencies": {"c.z.i": False}},
    }
    result = assign_submodule_dependencies(submodules, units)
    assert "b.y" in result["a.x"]["dependencies"]
    assert "c.z" in result["a.x"]["dependencies"]


def test_assign_intra_submodule_deps_skipped():
    submodules = {"a.x": _bare_sm("a.x", "a")}
    units = {
        "a.x.f": {"submodule": "a.x", "name": "f", "description": "", "dependencies": {"a.x.g": True}},
        "a.x.g": {"submodule": "a.x", "name": "g", "description": "", "dependencies": {}},
    }
    result = assign_submodule_dependencies(submodules, units)
    assert "a.x" not in result["a.x"]["dependencies"]


# ---------------------------------------------------------------------------
# process_files (integration)
# ---------------------------------------------------------------------------


def test_process_full_pipeline_structure(caplog):
    result, _ = _capture(process_files, UNITS_MD, LAYERS, caplog=caplog)
    assert "layers" in result
    assert "submodules" in result
    assert "units" in result
    assert "api.routes" in result["submodules"]
    assert "api.routes.get_samples" in result["units"]


def test_process_layers_preserved(caplog):
    result, _ = _capture(process_files, UNITS_MD, LAYERS, caplog=caplog)
    assert result["layers"] == LAYERS


def test_process_validation_failure_raises(caplog):
    md = "### nonexistent.module.f\n\nhello"
    with pytest.raises(ValueError):
        _capture(process_files, md, LAYERS, caplog=caplog)


def test_process_colors_assigned_not_grey(caplog):
    result, _ = _capture(process_files, UNITS_MD, LAYERS, caplog=caplog)
    for sm in result["submodules"].values():
        assert sm["color"] != "#D3D3D3"


def test_process_violation_detected(caplog):
    md = UNITS_MD + "\n### core.common.bad\n\nCalls `@api.routes.get_samples`."
    result, _ = _capture(process_files, md, LAYERS, caplog=caplog)
    assert result["units"]["core.common.bad"]["dependencies"].get("api.routes.get_samples") is False


def test_process_self_dep_removed(caplog):
    layers = {"root_layers": [["api"]], "submodule_layers": {"api": [["api.routes"]]}}
    md = "### api.routes.f\n\nCalls `@api.routes.f`."
    result, _ = _capture(process_files, md, layers, caplog=caplog)
    assert "api.routes.f" not in result["units"]["api.routes.f"]["dependencies"]


def test_process_submodule_colors_same_module(caplog):
    result, _ = _capture(process_files, UNITS_MD, LAYERS, caplog=caplog)
    db_sms = [sm for sm in result["submodules"].values() if sm["module"] == "db"]
    colors = {sm["color"] for sm in db_sms}
    assert len(colors) == 1


def test_process_submodule_units_are_short_names(caplog):
    result, _ = _capture(process_files, UNITS_MD, LAYERS, caplog=caplog)
    assert result["submodules"]["api.routes"]["units"] == ["get_samples", "delete_config"]


def test_process_submodule_deps_are_submodule_keys(caplog):
    result, _ = _capture(process_files, UNITS_MD, LAYERS, caplog=caplog)
    # all dependency keys in submodules must themselves be submodule paths
    all_sm_keys = set(result["submodules"].keys())
    for sm_path, sm in result["submodules"].items():
        for dep_key in sm["dependencies"]:
            assert dep_key in all_sm_keys, f"{sm_path} has dep key {dep_key!r} not in submodules"


def test_process_unit_deps_are_unit_keys(caplog):
    result, _ = _capture(process_files, UNITS_MD, LAYERS, caplog=caplog)
    all_unit_keys = set(result["units"].keys())
    for unit_path, unit in result["units"].items():
        for dep_key in unit["dependencies"]:
            assert dep_key in all_unit_keys, f"{unit_path} has dep key {dep_key!r} not in units"
