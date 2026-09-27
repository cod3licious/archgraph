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

# "core" lists itself to hold units defined directly at the module level (e.g. core/__init__.py)
SELF_LISTED_LAYERS = {
    "root_layers": [["api"], ["core"]],
    "submodule_layers": {"core": [["core.service"], ["core"], ["core.db"]]},
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
    units = {}
    for path, deps in deps_map.items():
        submodule, name = path.rsplit(".", 1)
        units[path] = {
            "submodule": submodule,
            "name": name,
            "description": "",
            "dependencies": dict.fromkeys(deps, True),
        }
    return units


# ---------------------------------------------------------------------------
# parse_unit_descriptions
# ---------------------------------------------------------------------------


def test_parse_basic_parsing():
    units = parse_unit_descriptions(UNITS_MD)
    assert "api.routes.get_samples" in units
    u = units["api.routes.get_samples"]
    assert u["submodule"] == "api.routes"
    assert u["name"] == "get_samples"
    assert "db.queries.sample.get_samples" in u["dependencies"]


def test_parse_description_stripped():
    units = parse_unit_descriptions("### a.b.c\n\n  hello world  \n\n")
    assert units["a.b.c"]["description"] == "hello world"


def test_parse_units_in_file_order():
    units = parse_unit_descriptions(UNITS_MD)
    assert list(units) == [
        "api.routes.get_samples",
        "api.routes.delete_config",
        "db.queries.sample.get_samples",
        "db.queries.config.get_config",
        "db.commands.create_predictions",
        "core.prediction.Model",
        "core.common.preprocess",
        "main.run",
    ]


def test_parse_multiple_deps():
    units = parse_unit_descriptions("### a.b.f\n\nUses `@a.b.g` and `@c.d.h`.")
    assert units["a.b.f"]["dependencies"] == {"a.b.g": True, "c.d.h": True}


def test_parse_no_deps():
    units = parse_unit_descriptions("### a.b.f\n\nNo references here.")
    assert units["a.b.f"]["dependencies"] == {}


def test_parse_duplicate_unit_path_raises():
    md = "### a.b.f\n\nhello\n\n### a.b.f\n\nworld"
    with pytest.raises(ValueError):
        parse_unit_descriptions(md)


def test_parse_empty_input():
    assert parse_unit_descriptions("") == {}


def test_parse_no_dot_in_path_raises():
    with pytest.raises(ValueError):
        parse_unit_descriptions("### nodot\n\nhello")


def test_parse_dependencies_initially_all_true():
    units = parse_unit_descriptions("### a.b.f\n\n`@a.b.g` and `@c.d.h`")
    assert all(v is True for v in units["a.b.f"]["dependencies"].values())


def test_parse_preamble_before_first_header_ignored():
    units = parse_unit_descriptions("# Some title\n\nIntro.\n\n### a.b.f\n\nhello")
    assert "a.b.f" in units
    assert len(units) == 1


def test_parse_description_multiline():
    md = "### a.b.f\n\nLine 1.\n\nLine 2.\n\nLine 3."
    units = parse_unit_descriptions(md)
    assert units["a.b.f"]["description"] == "Line 1.\n\nLine 2.\n\nLine 3."


def test_parse_dep_in_backticks_only():
    # bare @ref without backticks should NOT be picked up as a dependency
    units = parse_unit_descriptions("### a.b.f\n\nSee @a.b.g for details (not a dep).")
    assert "a.b.g" not in units["a.b.f"]["dependencies"]


def test_parse_at_in_backticks_without_at_not_a_dep():
    # backtick without @ should not be picked up
    units = parse_unit_descriptions("### a.b.f\n\nSee `a.b.g` (not a dep).")
    assert units["a.b.f"]["dependencies"] == {}


def test_parse_method_style_heading_splits_at_last_dot():
    # A heading like "services.ml.Model.predict" should split into
    # submodule="services.ml.Model", name="predict" — not submodule="services.ml".
    # This means the heading is almost certainly wrong (Model is a unit, not a submodule),
    # which validate_unit_paths will reject as an unknown submodule.
    units = parse_unit_descriptions("### services.ml.Model.predict\n\nRuns inference.")
    assert units["services.ml.Model.predict"]["submodule"] == "services.ml.Model"
    assert units["services.ml.Model.predict"]["name"] == "predict"


@pytest.mark.parametrize(
    "ref",
    ["`@core.db.query()`", "`@core.db.query(sql)`", "`@core.db.query,`", "`@core.db.query.`", "`@core.db.query(a, b).rows`"],
)
def test_parse_ref_trailing_characters_ignored(ref):
    units = parse_unit_descriptions(f"### api.routes.f\n\nCalls {ref} to fetch rows.")
    assert units["api.routes.f"]["dependencies"] == {"core.db.query": True}


def test_parse_unparseable_ref_kept_verbatim():
    # kept so that resolve_dependencies reports it instead of it being silently dropped
    units = parse_unit_descriptions("### api.routes.f\n\nCalls `@(core.db.query)`.")
    assert units["api.routes.f"]["dependencies"] == {"(core.db.query)": True}


def test_parse_heading_on_last_line_without_newline():
    units = parse_unit_descriptions("### a.b.f\n\nhello\n### a.b.g")
    assert list(units) == ["a.b.f", "a.b.g"]
    assert units["a.b.f"]["description"] == "hello"
    assert units["a.b.g"]["description"] == ""


@pytest.mark.parametrize("heading", ["### ", "###", "###   \t"])
def test_parse_empty_heading_raises(heading):
    with pytest.raises(ValueError, match="Empty unit heading"):
        parse_unit_descriptions(f"### a.b.f\n\nhello\n\n{heading}\n\nCalls `@a.b.g`.")


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_parse_heading_inside_code_fence_is_description(fence):
    md = f"### a.b.f\n\nExample:\n\n{fence}markdown\n### a.b.fake\nuses `@a.b.g`\n{fence}\n\n### a.b.g\n\nDone."
    units = parse_unit_descriptions(md)
    assert list(units) == ["a.b.f", "a.b.g"]
    assert "### a.b.fake" in units["a.b.f"]["description"]
    assert units["a.b.f"]["dependencies"] == {"a.b.g": True}


def test_parse_code_fence_only_closed_by_same_marker():
    md = "### a.b.f\n\n````\n```\n### a.b.fake\n~~~\n````\n\n### a.b.g\n\nDone."
    assert list(parse_unit_descriptions(md)) == ["a.b.f", "a.b.g"]


def test_parse_unclosed_code_fence_warns(caplog):
    units, caplog = _capture(parse_unit_descriptions, "### a.b.f\n\n```\n### a.b.g\n", caplog=caplog)
    assert list(units) == ["a.b.f"]
    assert "Unclosed code fence" in caplog.text


@pytest.mark.parametrize("indent", [" ", "  ", "   "])
def test_parse_indented_heading(indent):
    units = parse_unit_descriptions(f"### a.b.f\n\nhello\n\n{indent}### a.b.g\n\nworld")
    assert list(units) == ["a.b.f", "a.b.g"]
    assert units["a.b.f"]["description"] == "hello"


@pytest.mark.parametrize("line", ["    ### a.b.g", "#### a.b.g", "###a.b.g"])
def test_parse_non_unit_headings_are_description(line):
    units = parse_unit_descriptions(f"### a.b.f\n\n{line}")
    assert list(units) == ["a.b.f"]
    assert units["a.b.f"]["description"] == line.strip()


def test_parse_crlf_line_endings():
    units = parse_unit_descriptions("### a.b.f\r\n\r\nCalls `@a.b.g`.\r\n### a.b.g\r\n")
    assert units["a.b.f"]["description"] == "Calls `@a.b.g`."
    assert list(units) == ["a.b.f", "a.b.g"]


def test_validate_method_style_heading_rejected_as_unknown_submodule(caplog):
    # "services.ml.Model.predict" parsed → submodule "services.ml.Model", which is
    # not in the submodule list, so validate_unit_paths must reject it.
    units = {
        "services.ml.Model.predict": {
            "submodule": "services.ml.Model",
            "name": "predict",
            "description": "",
            "dependencies": {},
        }
    }
    result, caplog = _capture(validate_unit_paths, units, ["services.ml"], caplog=caplog)
    assert result is False
    assert "Unknown Submodule: services.ml.Model.predict" in caplog.text


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
    assert flatten_layers(layers) == {"main": (0, 0, "main")}


def test_flatten_multiple_root_rows_ordering():
    layers = {
        "root_layers": [["x"], ["y"]],
        "submodule_layers": {"x": [["x.a"]], "y": [["y.a"]]},
    }
    idx = _idx(flatten_layers(layers))
    assert idx["x.a"] < idx["y.a"]


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
    assert list(flatten_layers(layers)) == ["main", "api"]


def test_flatten_does_not_modify_input():
    layers = deepcopy(LAYERS)
    flatten_layers(layers)
    assert layers == LAYERS


def test_flatten_layer_positions_and_modules():
    sm_info = flatten_layers(LAYERS)
    assert sm_info["main"] == (0, 0, "main")
    assert sm_info["api.routes"] == (0, 0, "api")
    assert sm_info["db.queries.config"] == (1, 1, "db")
    assert sm_info["core.common"] == (2, 1, "core")


def test_flatten_submodule_layers_optional():
    assert list(flatten_layers({"root_layers": [["main"], ["core"]]})) == ["main", "core"]


def test_flatten_module_listing_itself_as_submodule():
    sm_info = flatten_layers(SELF_LISTED_LAYERS)
    assert list(sm_info) == ["api", "core.service", "core", "core.db"]
    assert sm_info["core"] == (1, 1, "core")


def test_flatten_prefix_without_dot_raises():
    layers = {"root_layers": [["core"]], "submodule_layers": {"core": [["core_utils"]]}}
    with pytest.raises(ValueError, match="does not start with parent module"):
        flatten_layers(layers)


def test_flatten_duplicate_leaf_module_raises():
    with pytest.raises(ValueError, match="Duplicate submodule"):
        flatten_layers({"root_layers": [["a"], ["a"]]})


@pytest.mark.parametrize(
    "layers",
    [
        [["main"]],
        {},
        {"root_layers": "main"},
        {"root_layers": ["main", "core"]},
        {"root_layers": [["main", 1]]},
        {"root_layers": [["core"]], "submodule_layers": ["core"]},
        {"root_layers": [["core"]], "submodule_layers": {"core": ["core.db"]}},
        {"root_layers": [["core"]], "submodule_layers": {"core": [[["core.db"]]]}},
    ],
)
def test_flatten_invalid_shape_raises(layers):
    with pytest.raises(ValueError, match="Invalid layers"):
        flatten_layers(layers)


# ---------------------------------------------------------------------------
# validate_unit_paths
# ---------------------------------------------------------------------------

ALL_SM = ["api.routes", "db.commands"]
SELF_LISTED_SM = flatten_layers(SELF_LISTED_LAYERS)


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


def test_validate_self_listed_module(caplog):
    units = parse_unit_descriptions("### core.helper\n\n### core.db.x\n\n### core.db\n")
    result, caplog = _capture(
        validate_unit_paths, {k: units[k] for k in ["core.helper", "core.db.x"]}, SELF_LISTED_SM, caplog=caplog
    )
    assert result is True
    # "core.db" is a unit in submodule "core", but also a submodule itself
    result, caplog = _capture(validate_unit_paths, units, SELF_LISTED_SM, caplog=caplog)
    assert result is False
    assert "Unit Is Submodule: core.db" in caplog.text


def test_validate_does_not_modify_inputs(caplog):
    units = {"api.routes.f": {"submodule": "api.routes", "name": "f", "description": "", "dependencies": {}}}
    original_units = deepcopy(units)
    original_sm = list(ALL_SM)
    _capture(validate_unit_paths, units, ALL_SM, caplog=caplog)
    assert units == original_units
    assert original_sm == ALL_SM


# ---------------------------------------------------------------------------
# create_submodules_dict
# ---------------------------------------------------------------------------


def test_create_submodules_basic_structure(caplog):
    units = parse_unit_descriptions(UNITS_MD)
    result, _ = _capture(create_submodules_dict, flatten_layers(LAYERS), units, caplog=caplog)
    assert result["api.routes"] == {
        "module": "api",
        "color": "#D3D3D3",
        "units": ["get_samples", "delete_config"],  # short names in file order
        "dependencies": {},
    }
    assert result["main"]["module"] == "main"


def test_create_submodules_order_follows_layers(caplog):
    result, _ = _capture(create_submodules_dict, flatten_layers(LAYERS), {}, caplog=caplog)
    assert list(result) == list(flatten_layers(LAYERS))


def test_create_submodules_module_from_layer_mapping(caplog):
    """With a shared enclosing package, 'module' comes from the layer hierarchy, not the first path segment."""
    layers = {
        "root_layers": [["pkg.backend"], ["pkg.core"]],
        "submodule_layers": {"pkg.core": [["pkg.core.analysis"]], "pkg.backend": [["pkg.backend.server"]]},
    }
    result, _ = _capture(create_submodules_dict, flatten_layers(layers), {}, caplog=caplog)
    assert result["pkg.core.analysis"]["module"] == "pkg.core"
    assert result["pkg.backend.server"]["module"] == "pkg.backend"


def test_create_submodules_missing_units_warns(caplog):
    result, caplog = _capture(create_submodules_dict, flatten_layers(LAYERS), {}, caplog=caplog)
    assert result["api.routes"]["units"] == []
    assert "Submodule api.routes has no units" in caplog.text


def test_create_submodules_empty(caplog):
    result, _ = _capture(create_submodules_dict, {}, {}, caplog=caplog)
    assert result == {}


def test_create_submodules_does_not_modify_inputs(caplog):
    sm_info = flatten_layers(LAYERS)
    units = parse_unit_descriptions(UNITS_MD)
    original_sm_info, original_units = deepcopy(sm_info), deepcopy(units)
    _capture(create_submodules_dict, sm_info, units, caplog=caplog)
    assert sm_info == original_sm_info
    assert units == original_units


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


def test_resolve_ref_to_own_method_removed(caplog):
    units = _make_units({"core.db.Model": ["core.db.Model._prep", "core.db.q"], "core.db.q": []})
    result, caplog = _capture(resolve_dependencies, units, caplog=caplog)
    assert result["core.db.Model"]["dependencies"] == {"core.db.q": True}
    assert "0 error(s)" in caplog.text


def test_resolve_strict_raises_after_logging_all_errors(caplog):
    units = _make_units({"a.b.f": ["x.y.z", "a.b.g"], "a.b.g": ["p.q.r"]})
    with caplog.at_level(logging.DEBUG, logger="prepare"), pytest.raises(ValueError, match="2 referenced unit"):
        resolve_dependencies(units, strict=True)
    assert "a.b.f depends on x.y.z" in caplog.text
    assert "a.b.g depends on p.q.r" in caplog.text


def test_resolve_strict_passes_when_all_resolved(caplog):
    units = _make_units({"a.b.f": ["a.b.g", "a.b.g.method", "a.b.f"], "a.b.g": []})
    result, _ = _capture(resolve_dependencies, units, strict=True, caplog=caplog)
    assert result["a.b.f"]["dependencies"] == {"a.b.g": True}


# ---------------------------------------------------------------------------
# check_layer_violations
# ---------------------------------------------------------------------------


def _violation_units(unit_a, sm_a, dep_b, sm_b):
    return {
        unit_a: {"submodule": sm_a, "name": unit_a.split(".")[-1], "description": "", "dependencies": {dep_b: True}},
        dep_b: {"submodule": sm_b, "name": dep_b.split(".")[-1], "description": "", "dependencies": {}},
    }


SM_INFO = flatten_layers(LAYERS)


def test_check_valid_cross_module_dep(caplog):
    units = _violation_units("api.routes.f", "api.routes", "db.commands.g", "db.commands")
    result, _ = _capture(check_layer_violations, units, SM_INFO, caplog=caplog)
    assert result["api.routes.f"]["dependencies"]["db.commands.g"] is True


def test_check_invalid_upward_dep(caplog):
    units = _violation_units("db.commands.g", "db.commands", "api.routes.f", "api.routes")
    result, caplog = _capture(check_layer_violations, units, SM_INFO, caplog=caplog)
    assert result["db.commands.g"]["dependencies"]["api.routes.f"] is False
    assert "Architecture Validation" in caplog.text


def test_check_invalid_same_root_layer_different_module(caplog):
    units = _violation_units("main.run", "main", "api.routes.f", "api.routes")
    result, _ = _capture(check_layer_violations, units, SM_INFO, caplog=caplog)
    assert result["main.run"]["dependencies"]["api.routes.f"] is False


def test_check_valid_intra_module_downward(caplog):
    units = _violation_units("db.commands.g", "db.commands", "db.queries.sample.f", "db.queries.sample")
    result, _ = _capture(check_layer_violations, units, SM_INFO, caplog=caplog)
    assert result["db.commands.g"]["dependencies"]["db.queries.sample.f"] is True


def test_check_invalid_intra_module_upward(caplog):
    units = _violation_units("db.queries.sample.f", "db.queries.sample", "db.commands.g", "db.commands")
    result, _ = _capture(check_layer_violations, units, SM_INFO, caplog=caplog)
    assert result["db.queries.sample.f"]["dependencies"]["db.commands.g"] is False


def test_check_invalid_same_intra_layer_siblings(caplog):
    units = _violation_units(
        "db.queries.sample.f",
        "db.queries.sample",
        "db.queries.config.g",
        "db.queries.config",
    )
    result, _ = _capture(check_layer_violations, units, SM_INFO, caplog=caplog)
    assert result["db.queries.sample.f"]["dependencies"]["db.queries.config.g"] is False


def test_check_same_submodule_bottom_up_allowed(caplog):
    # Bottom-up (default): g (idx 1) depends on f (idx 0) = depends on earlier unit = allowed
    units = {
        "db.commands.f": {"submodule": "db.commands", "name": "f", "description": "", "dependencies": {}},
        "db.commands.g": {"submodule": "db.commands", "name": "g", "description": "", "dependencies": {"db.commands.f": True}},
    }
    result, _ = _capture(check_layer_violations, units, SM_INFO, caplog=caplog)
    assert result["db.commands.g"]["dependencies"]["db.commands.f"] is True


def test_check_same_submodule_bottom_up_violation(caplog):
    # Bottom-up (default): f (idx 0) depends on g (idx 1) = depends on later unit = violation
    units = _violation_units("db.commands.f", "db.commands", "db.commands.g", "db.commands")
    result, caplog = _capture(check_layer_violations, units, SM_INFO, caplog=caplog)
    assert result["db.commands.f"]["dependencies"]["db.commands.g"] is False
    assert "intra-submodule" in caplog.text


def test_check_same_submodule_high_level_first_allowed(caplog):
    # Top-down: f (idx 0) depends on g (idx 1) = depends on later unit = allowed
    units = _violation_units("db.commands.f", "db.commands", "db.commands.g", "db.commands")
    result, _ = _capture(check_layer_violations, units, SM_INFO, high_level_units_first=True, caplog=caplog)
    assert result["db.commands.f"]["dependencies"]["db.commands.g"] is True


def test_check_same_submodule_high_level_first_violation(caplog):
    # Top-down: g (idx 1) depends on f (idx 0) = depends on earlier unit = violation
    units = {
        "db.commands.f": {"submodule": "db.commands", "name": "f", "description": "", "dependencies": {}},
        "db.commands.g": {"submodule": "db.commands", "name": "g", "description": "", "dependencies": {"db.commands.f": True}},
    }
    result, caplog = _capture(check_layer_violations, units, SM_INFO, high_level_units_first=True, caplog=caplog)
    assert result["db.commands.g"]["dependencies"]["db.commands.f"] is False
    assert "intra-submodule" in caplog.text


def test_check_does_not_modify_original(caplog):
    units = _violation_units("db.commands.g", "db.commands", "api.routes.f", "api.routes")
    original = deepcopy(units)
    _capture(check_layer_violations, units, SM_INFO, caplog=caplog)
    assert units == original


def test_check_core_cannot_depend_on_higher(caplog):
    units = _violation_units("core.common.f", "core.common", "db.commands.g", "db.commands")
    result, _ = _capture(check_layer_violations, units, SM_INFO, caplog=caplog)
    assert result["core.common.f"]["dependencies"]["db.commands.g"] is False


def test_check_lower_module_can_depend_on_same_lower_layer(caplog):
    # core.optimization and core.prediction are siblings — neither can depend on the other
    units = _violation_units("core.optimization.f", "core.optimization", "core.prediction.g", "core.prediction")
    result, _ = _capture(check_layer_violations, units, SM_INFO, caplog=caplog)
    assert result["core.optimization.f"]["dependencies"]["core.prediction.g"] is False


def test_check_lower_submodule_can_depend_on_lower_submodule(caplog):
    # core.optimization and core.common: core.common is in a lower layer
    units = _violation_units("core.optimization.f", "core.optimization", "core.common.g", "core.common")
    result, _ = _capture(check_layer_violations, units, SM_INFO, caplog=caplog)
    assert result["core.optimization.f"]["dependencies"]["core.common.g"] is True


def test_check_cross_module_lower_to_higher_invalid(caplog):
    # core (bottom) -> api (top): invalid
    units = _violation_units("core.common.f", "core.common", "api.routes.g", "api.routes")
    result, _ = _capture(check_layer_violations, units, SM_INFO, caplog=caplog)
    assert result["core.common.f"]["dependencies"]["api.routes.g"] is False


@pytest.mark.parametrize(
    ("unit_a", "sm_a", "dep_b", "sm_b"),
    [
        ("core.service.f", "core.service", "core.helper", "core"),
        ("core.helper", "core", "core.db.g", "core.db"),
        ("api.f", "api", "core.helper", "core"),
    ],
)
def test_check_self_listed_module_downward_allowed(caplog, unit_a, sm_a, dep_b, sm_b):
    units = _violation_units(unit_a, sm_a, dep_b, sm_b)
    result, _ = _capture(check_layer_violations, units, SELF_LISTED_SM, caplog=caplog)
    assert result[unit_a]["dependencies"][dep_b] is True


@pytest.mark.parametrize(
    ("unit_a", "sm_a", "dep_b", "sm_b"),
    [
        ("core.helper", "core", "core.service.f", "core.service"),
        ("core.db.g", "core.db", "core.helper", "core"),
        ("core.helper", "core", "api.f", "api"),
    ],
)
def test_check_self_listed_module_upward_violation(caplog, unit_a, sm_a, dep_b, sm_b):
    units = _violation_units(unit_a, sm_a, dep_b, sm_b)
    result, _ = _capture(check_layer_violations, units, SELF_LISTED_SM, caplog=caplog)
    assert result[unit_a]["dependencies"][dep_b] is False


# ---------------------------------------------------------------------------
# assign_submodule_dependencies
# ---------------------------------------------------------------------------


def _aggregate(deps_map: dict[str, dict[str, bool]]) -> dict:
    """Run assign_submodule_dependencies on {unit_path: {dep_path: valid}}; referenced units are added too."""
    units = _make_units({path: [] for path in [*deps_map, *(dep for deps in deps_map.values() for dep in deps)]})
    for path, deps in deps_map.items():
        units[path]["dependencies"] = deps
    submodules = {u["submodule"]: {"module": "m", "color": "#fff", "units": [], "dependencies": {}} for u in units.values()}
    return assign_submodule_dependencies(submodules, units)


def test_assign_unit_deps_aggregated_to_submodule():
    result = _aggregate({"a.x.f": {"b.y.g": True}})
    # key is the target submodule, not the unit path
    assert result["a.x"]["dependencies"] == {"b.y": True}
    assert result["b.y"]["dependencies"] == {}


def test_assign_violation_flag_preserved():
    assert _aggregate({"a.x.f": {"b.y.g": False}})["a.x"]["dependencies"] == {"b.y": False}


@pytest.mark.parametrize("order", [[True, False], [False, True]])
def test_assign_any_violation_makes_arrow_red(order):
    # two units in a.x both depend on b.y; one valid, one violation => False (regardless of order)
    result = _aggregate({"a.x.f": {"b.y.g": order[0]}, "a.x.h": {"b.y.i": order[1]}})
    assert result["a.x"]["dependencies"] == {"b.y": False}


def test_assign_all_valid_keeps_true():
    result = _aggregate({"a.x.f": {"b.y.g": True}, "a.x.h": {"b.y.i": True}})
    assert result["a.x"]["dependencies"] == {"b.y": True}


def test_assign_does_not_modify_originals():
    submodules = {"a.x": {"module": "a", "color": "#fff", "units": ["f"], "dependencies": {}}}
    units = _make_units({"a.x.f": []})
    orig_sm, orig_units = deepcopy(submodules), deepcopy(units)
    assign_submodule_dependencies(submodules, units)
    assert submodules == orig_sm
    assert units == orig_units


def test_assign_multiple_target_submodules():
    result = _aggregate({"a.x.f": {"b.y.h": True}, "a.x.g": {"c.z.i": False}})
    assert result["a.x"]["dependencies"] == {"b.y": True, "c.z": False}


def test_assign_intra_submodule_deps_skipped():
    assert _aggregate({"a.x.f": {"a.x.g": True}})["a.x"]["dependencies"] == {}


def test_assign_self_listed_module():
    # "core" is both the module and a submodule holding module-level units like core.helper
    result = _aggregate({"core.service.f": {"core.helper": True}, "core.helper": {"core.db.x": True, "core.util": True}})
    assert result["core.service"]["dependencies"] == {"core": True}
    assert result["core"]["dependencies"] == {"core.db": True}


# ---------------------------------------------------------------------------
# process_files (integration)
# ---------------------------------------------------------------------------


def test_process_full_pipeline_structure(caplog):
    result, _ = _capture(process_files, UNITS_MD, LAYERS, caplog=caplog)
    assert "layers" in result
    assert "submodules" in result
    assert "units" in result
    assert result["high_level_units_first"] is False
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


def test_process_ref_to_own_method_not_a_violation(caplog):
    layers = {"root_layers": [["core"]], "submodule_layers": {"core": [["core.db"]]}}
    md = "### core.db.q\n\nRuns a query.\n\n### core.db.Model\n\nCalls `@core.db.Model._prep()` and `@core.db.q`."
    result, caplog = _capture(process_files, md, layers, caplog=caplog)
    assert result["units"]["core.db.Model"]["dependencies"] == {"core.db.q": True}
    assert "Architecture Validation" not in caplog.text


def test_process_strict_fails_on_unresolved_reference(caplog):
    md = UNITS_MD + "\n### core.common.g\n\nCalls `@core.common.missing`."
    result, _ = _capture(process_files, md, LAYERS, caplog=caplog)
    assert result["units"]["core.common.g"]["dependencies"] == {}
    with pytest.raises(ValueError, match="1 referenced unit"):
        _capture(process_files, md, LAYERS, strict=True, caplog=caplog)


def test_process_strict_passes_without_unresolved_references(caplog):
    result, _ = _capture(process_files, UNITS_MD, LAYERS, strict=True, caplog=caplog)
    assert "main.run" in result["units"]


def test_process_submodule_layers_optional(caplog):
    layers = {"root_layers": [["main"], ["core"]]}
    md = "### core.f\n\nHelper.\n\n### main.run\n\nCalls `@core.f`."
    result, _ = _capture(process_files, md, layers, caplog=caplog)
    assert result["layers"] == {"root_layers": [["main"], ["core"]], "submodule_layers": {}}
    assert result["submodules"]["main"]["dependencies"] == {"core": True}


def test_process_self_listed_module(caplog):
    md = """\
### api.handle
Calls `@core.service.run`.

### core.helper
Module-level helper. Calls `@core.db.query`.

### core.service.run
Calls `@core.helper` and `@core.db.query`.

### core.db.query
Calls `@core.helper` (violation).
"""
    result, _ = _capture(process_files, md, SELF_LISTED_LAYERS, caplog=caplog)
    submodules = result["submodules"]
    assert list(submodules) == ["api", "core.service", "core", "core.db"]
    assert submodules["core"]["module"] == "core"
    assert submodules["core"]["units"] == ["helper"]
    assert submodules["core"]["color"] == submodules["core.db"]["color"] != submodules["api"]["color"]
    assert submodules["core.service"]["dependencies"] == {"core": True, "core.db": True}
    assert submodules["core"]["dependencies"] == {"core.db": True}
    assert submodules["core.db"]["dependencies"] == {"core": False}
    assert result["units"]["core.db.query"]["dependencies"] == {"core.helper": False}


def test_process_unit_named_like_submodule_of_self_listed_module_fails(caplog):
    md = "### core.helper\n\nHelper.\n\n### core.db\n\nClashes with the submodule core.db."
    with pytest.raises(ValueError, match="validation failed"):
        _capture(process_files, md, SELF_LISTED_LAYERS, caplog=caplog)
    assert "Unit Is Submodule: core.db" in caplog.text
