"""Tests for generate.py and languages.py."""

import json
import textwrap
from pathlib import Path

import tree_sitter as ts
import tree_sitter_python as tspython

from generate import (
    UnitInfo,
    build_index,
    file_path_to_module,
    filter_to_valid_submodules,
    format_units_md,
    generate_layers_draft,
    parse_file,
    resolve_dependencies,
)
from languages import (
    ImportInfo,
    _make_python_config,
    _python_extract_calls,
    _python_extract_docstring,
    _python_extract_imports,
    _resolve_relative_import,
    register_languages,
)
from prepare import flatten_layers, parse_unit_descriptions

PY_CONFIG = _make_python_config()
PY_LANG = ts.Language(tspython.language())


def _parser() -> ts.Parser:
    return ts.Parser(PY_LANG)


def _parse_node(source: str) -> ts.Node:
    """Parse source and return the root node."""
    return _parser().parse(source.encode()).root_node


# =============================================================================
# file_path_to_module
# =============================================================================


def test_file_path_to_module_simple():
    assert file_path_to_module(Path("/root/foo/bar.py"), Path("/root"), PY_CONFIG) == "foo.bar"


def test_file_path_to_module_init():
    assert file_path_to_module(Path("/root/foo/__init__.py"), Path("/root"), PY_CONFIG) == "foo"


def test_file_path_to_module_nested():
    assert file_path_to_module(Path("/root/a/b/c.py"), Path("/root"), PY_CONFIG) == "a.b.c"


def test_file_path_to_module_top_level():
    assert file_path_to_module(Path("/root/main.py"), Path("/root"), PY_CONFIG) == "main"


def test_file_path_to_module_outside_root():
    assert file_path_to_module(Path("/other/foo.py"), Path("/root"), PY_CONFIG) is None


def test_file_path_to_module_root_init():
    """__init__.py at the root itself returns None (empty module path)."""
    assert file_path_to_module(Path("/root/__init__.py"), Path("/root"), PY_CONFIG) is None


# =============================================================================
# Python docstring extraction
# =============================================================================


def test_docstring_triple_quotes():
    root = _parse_node('def f():\n    """Hello."""\n    pass\n')
    assert _python_extract_docstring(root.children[0]) == "Hello."


def test_docstring_single_quotes():
    root = _parse_node("def f():\n    '''Hello.'''\n    pass\n")
    assert _python_extract_docstring(root.children[0]) == "Hello."


def test_docstring_none_when_missing():
    root = _parse_node("def f():\n    pass\n")
    assert _python_extract_docstring(root.children[0]) is None


def test_docstring_multiline():
    root = _parse_node('def f():\n    """Line 1.\n\n    Line 2.\n    """\n    pass\n')
    doc = _python_extract_docstring(root.children[0])
    assert doc is not None
    assert "Line 1." in doc
    assert "Line 2." in doc


def test_docstring_class():
    root = _parse_node('class C:\n    """Class doc."""\n    pass\n')
    assert _python_extract_docstring(root.children[0]) == "Class doc."


# =============================================================================
# Python import extraction
# =============================================================================


def test_import_simple():
    root = _parse_node("import os\n")
    imports = _python_extract_imports(root, "mymod")
    assert imports == [ImportInfo(local_name="os", qualified_name="os")]


def test_import_dotted():
    root = _parse_node("import os.path\n")
    imports = _python_extract_imports(root, "mymod")
    assert imports == [ImportInfo(local_name="os", qualified_name="os.path")]


def test_from_import():
    root = _parse_node("from pathlib import Path\n")
    imports = _python_extract_imports(root, "mymod")
    assert imports == [ImportInfo(local_name="Path", qualified_name="pathlib.Path")]


def test_from_import_multiple():
    root = _parse_node("from os.path import join, exists\n")
    imports = _python_extract_imports(root, "mymod")
    assert len(imports) == 2
    assert imports[0] == ImportInfo(local_name="join", qualified_name="os.path.join")
    assert imports[1] == ImportInfo(local_name="exists", qualified_name="os.path.exists")


def test_relative_import_dot():
    root = _parse_node("from . import sibling\n")
    imports = _python_extract_imports(root, "pkg.sub.mymod")
    assert imports == [ImportInfo(local_name="sibling", qualified_name="pkg.sub.sibling")]


def test_relative_import_dot_name():
    root = _parse_node("from .other import func\n")
    imports = _python_extract_imports(root, "pkg.sub.mymod")
    assert imports == [ImportInfo(local_name="func", qualified_name="pkg.sub.other.func")]


def test_relative_import_two_dots():
    root = _parse_node("from .. import util\n")
    imports = _python_extract_imports(root, "pkg.sub.mymod")
    assert imports == [ImportInfo(local_name="util", qualified_name="pkg.util")]


def test_star_import_skipped():
    root = _parse_node("from os import *\n")
    imports = _python_extract_imports(root, "mymod")
    assert imports == []


# =============================================================================
# Python call extraction
# =============================================================================


def _get_def(source: str) -> ts.Node:
    """Parse source and return the first definition node."""
    return _parse_node(source).children[0]


def test_call_simple():
    calls = _python_extract_calls(_get_def("def f():\n    foo()\n"))
    assert "foo" in calls


def test_call_attribute():
    calls = _python_extract_calls(_get_def("def f():\n    os.path.join('a', 'b')\n"))
    assert "os.path.join" in calls


def test_call_self_skipped():
    calls = _python_extract_calls(_get_def("def f(self):\n    self.method()\n"))
    assert not any(c.startswith("self.") for c in calls)


def test_call_cls_skipped():
    calls = _python_extract_calls(_get_def("def f(cls):\n    cls.create()\n"))
    assert not any(c.startswith("cls.") for c in calls)


# =============================================================================
# Relative import resolution
# =============================================================================


def test_resolve_relative_one_dot():
    node = _parse_node("from .sub import x\n").children[0]
    rel_node = next(c for c in node.children if c.type == "relative_import")
    assert _resolve_relative_import(rel_node, "pkg.mymod") == "pkg.sub"


def test_resolve_relative_two_dots():
    node = _parse_node("from ..other import x\n").children[0]
    rel_node = next(c for c in node.children if c.type == "relative_import")
    assert _resolve_relative_import(rel_node, "pkg.sub.mymod") == "pkg.other"


def test_resolve_relative_dot_only():
    node = _parse_node("from . import x\n").children[0]
    rel_node = next(c for c in node.children if c.type == "relative_import")
    assert _resolve_relative_import(rel_node, "pkg.sub.mymod") == "pkg.sub"


# =============================================================================
# parse_file
# =============================================================================


def test_parse_file_functions():
    source = b'def public():\n    """Doc."""\n    pass\n\ndef _private():\n    pass\n'
    units, _imports = parse_file(source, "mymod", PY_CONFIG, _parser())
    assert len(units) == 1
    assert units[0].name == "public"
    assert units[0].docstring == "Doc."


def test_parse_file_includes_private():
    source = b"def _private():\n    pass\n"
    units, _ = parse_file(source, "mymod", PY_CONFIG, _parser(), include_private=True)
    assert len(units) == 1
    assert units[0].name == "_private"


def test_parse_file_class_with_methods():
    source = b'class MyClass:\n    """Class doc."""\n    def method(self):\n        helper()\n'
    units, _ = parse_file(source, "mymod", PY_CONFIG, _parser())
    assert len(units) == 1
    unit = units[0]
    assert unit.name == "MyClass"
    assert unit.kind == "class"
    assert unit.docstring == "Class doc."
    assert "helper" in unit.raw_calls


def test_parse_file_class_collects_all_method_calls():
    source = b"class C:\n    def a(self):\n        foo()\n    def b(self):\n        bar()\n"
    units, _ = parse_file(source, "mymod", PY_CONFIG, _parser())
    assert "foo" in units[0].raw_calls
    assert "bar" in units[0].raw_calls


def test_parse_file_imports():
    source = b"from pathlib import Path\nimport os\ndef f():\n    pass\n"
    _, imports = parse_file(source, "mymod", PY_CONFIG, _parser())
    local_names = {i.local_name for i in imports}
    assert "Path" in local_names
    assert "os" in local_names


def test_parse_file_empty():
    source = b""
    units, imports = parse_file(source, "mymod", PY_CONFIG, _parser())
    assert units == []
    assert imports == []


# =============================================================================
# build_index
# =============================================================================


def test_build_index(tmp_path):
    register_languages()
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod_a.py").write_text('def func_a():\n    """Doc A."""\n    func_b()\n')
    (pkg / "mod_b.py").write_text("def func_b():\n    pass\n")

    si, _im = build_index(pkg)
    assert "mod_a.func_a" in si
    assert "mod_b.func_b" in si
    assert "func_b" in si["mod_a.func_a"].raw_calls


def test_build_index_excludes(tmp_path):
    register_languages()
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "main.py").write_text("def main():\n    pass\n")
    (pkg / "test_main.py").write_text("def test_it():\n    pass\n")

    si, _ = build_index(pkg, exclude_patterns=["test_*"])
    assert "main.main" in si
    assert "test_main.test_it" not in si


def test_build_index_private_excluded_by_default(tmp_path):
    register_languages()
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("def public():\n    pass\n\ndef _private():\n    pass\n")

    si, _ = build_index(pkg)
    assert "mod.public" in si
    assert "mod._private" not in si


# =============================================================================
# resolve_dependencies
# =============================================================================


def test_resolve_exact_match():
    si = {
        "a.func_a": UnitInfo("a.func_a", "a", "func_a", "function", raw_calls=["func_b"]),
        "a.func_b": UnitInfo("a.func_b", "a", "func_b", "function"),
    }
    im = {"a": {}}
    deps = resolve_dependencies(si, im)
    assert deps["a.func_a"] == ["a.func_b"]


def test_resolve_through_import():
    si = {
        "a.caller": UnitInfo("a.caller", "a", "caller", "function", raw_calls=["Target"]),
        "b.Target": UnitInfo("b.Target", "b", "Target", "class"),
    }
    im = {"a": {"Target": "b.Target"}, "b": {}}
    deps = resolve_dependencies(si, im)
    assert deps["a.caller"] == ["b.Target"]


def test_resolve_method_to_class():
    si = {
        "a.caller": UnitInfo("a.caller", "a", "caller", "function", raw_calls=["MyClass.method"]),
        "b.MyClass": UnitInfo("b.MyClass", "b", "MyClass", "class"),
    }
    im = {"a": {"MyClass": "b.MyClass"}, "b": {}}
    deps = resolve_dependencies(si, im)
    assert deps["a.caller"] == ["b.MyClass"]


def test_resolve_self_dep_skipped():
    si = {
        "a.func": UnitInfo("a.func", "a", "func", "function", raw_calls=["func"]),
    }
    im = {"a": {}}
    deps = resolve_dependencies(si, im)
    assert deps["a.func"] == []


def test_resolve_unresolvable_skipped():
    si = {
        "a.func": UnitInfo("a.func", "a", "func", "function", raw_calls=["unknown_thing"]),
    }
    im = {"a": {}}
    deps = resolve_dependencies(si, im)
    # "unknown_thing" resolves to "a.unknown_thing" via same-module, but that doesn't exist
    assert deps["a.func"] == []


def test_resolve_deduplicates():
    si = {
        "a.caller": UnitInfo("a.caller", "a", "caller", "function", raw_calls=["target", "target"]),
        "a.target": UnitInfo("a.target", "a", "target", "function"),
    }
    im = {"a": {}}
    deps = resolve_dependencies(si, im)
    assert deps["a.caller"] == ["a.target"]


# =============================================================================
# format_units_md
# =============================================================================


def test_format_units_md_basic():
    si = {
        "mod.func_a": UnitInfo("mod.func_a", "mod", "func_a", "function", docstring="Does A."),
        "mod.func_b": UnitInfo("mod.func_b", "mod", "func_b", "function"),
    }
    deps = {"mod.func_a": ["mod.func_b"], "mod.func_b": []}
    md = format_units_md(si, deps)
    assert "### mod.func_a" in md
    assert "`@mod.func_b`" in md
    assert "Does A." in md
    assert "### mod.func_b" in md


def test_format_units_md_no_docstring():
    si = {"mod.func": UnitInfo("mod.func", "mod", "func", "function")}
    deps = {"mod.func": []}
    md = format_units_md(si, deps)
    assert "Function in mod." in md


def test_format_units_md_sorted_by_submodule():
    si = {
        "b.func": UnitInfo("b.func", "b", "func", "function"),
        "a.func": UnitInfo("a.func", "a", "func", "function"),
    }
    deps = {"b.func": [], "a.func": []}
    md = format_units_md(si, deps)
    assert md.index("### a.func") < md.index("### b.func")


def test_format_units_md_parseable_by_prepare():
    """Output should be parseable by prepare.py's parse_unit_descriptions."""
    si = {
        "mod.func_a": UnitInfo("mod.func_a", "mod", "func_a", "function", docstring="Does A."),
        "mod.func_b": UnitInfo("mod.func_b", "mod", "func_b", "function"),
    }
    deps = {"mod.func_a": ["mod.func_b"], "mod.func_b": []}
    md = format_units_md(si, deps)

    units, unit_order = parse_unit_descriptions(md)
    assert "mod.func_a" in units
    assert "mod.func_b" in units
    assert "mod.func_b" in units["mod.func_a"]["dependencies"]
    assert unit_order["mod"] == ["func_a", "func_b"]


# =============================================================================
# generate_layers_draft
# =============================================================================


def test_layers_draft_basic():
    si = {
        "api.routes.get": UnitInfo("api.routes.get", "api.routes", "get", "function"),
        "api.auth.check": UnitInfo("api.auth.check", "api.auth", "check", "function"),
        "core.db.query": UnitInfo("core.db.query", "core.db", "query", "function"),
    }
    draft, valid = generate_layers_draft(si)
    assert draft["root_layers"] == [["api"], ["core"]]
    assert draft["submodule_layers"]["api"] == [["api.auth"], ["api.routes"]]
    assert draft["submodule_layers"]["core"] == [["core.db"]]
    assert valid == {"api.auth", "api.routes", "core.db"}


def test_layers_draft_leaf_module():
    """A root module with no submodules (single-level) should not appear in submodule_layers."""
    si = {
        "utils.helper": UnitInfo("utils.helper", "utils", "helper", "function"),
    }
    draft, valid = generate_layers_draft(si)
    assert draft["root_layers"] == [["utils"]]
    assert "utils" not in draft["submodule_layers"]
    assert valid == {"utils"}


def test_layers_draft_root_with_direct_units_stays_leaf():
    """When a root module has both direct units and submodules, it must stay a leaf.

    prepare.py doesn't support units at the root level when submodule_layers exist
    (e.g. units from __init__.py alongside deeper submodules).
    """
    si = {
        "pkg.init_func": UnitInfo("pkg.init_func", "pkg", "init_func", "function"),
        "pkg.sub.deep_func": UnitInfo("pkg.sub.deep_func", "pkg.sub", "deep_func", "function"),
    }
    draft, valid = generate_layers_draft(si)
    assert draft["root_layers"] == [["pkg"]]
    assert "pkg" not in draft["submodule_layers"]
    # Only the leaf root is valid; pkg.sub is dropped
    assert valid == {"pkg"}


def test_layers_draft_filter_keeps_consistency():
    """filter_to_valid_submodules drops units whose submodule isn't in layers."""
    si = {
        "pkg.init_func": UnitInfo("pkg.init_func", "pkg", "init_func", "function"),
        "pkg.sub.deep_func": UnitInfo("pkg.sub.deep_func", "pkg.sub", "deep_func", "function"),
        "other.sub.func": UnitInfo("other.sub.func", "other.sub", "func", "function"),
    }
    draft, valid = generate_layers_draft(si)
    filtered = filter_to_valid_submodules(si, valid)
    flat = flatten_layers(draft)
    # Every remaining unit's submodule must be in the flattened list
    for unit in filtered.values():
        assert unit.submodule in flat, f"{unit.submodule} not in flattened layers"
    # pkg.sub.deep_func should have been dropped (pkg is a leaf)
    assert "pkg.sub.deep_func" not in filtered


def test_layers_draft_valid_json():
    """Draft should be valid JSON and parseable."""
    si = {
        "a.b.func": UnitInfo("a.b.func", "a.b", "func", "function"),
        "c.func": UnitInfo("c.func", "c", "func", "function"),
    }
    draft, _valid = generate_layers_draft(si)
    assert json.loads(json.dumps(draft)) == draft


# =============================================================================
# Integration: build_index + resolve_dependencies + format_units_md
# =============================================================================


def test_end_to_end(tmp_path):
    """Full pipeline: multi-file package -> units.md -> parseable by prepare.py."""
    register_languages()

    pkg = tmp_path / "pkg"
    (pkg / "api").mkdir(parents=True)
    (pkg / "core").mkdir()

    (pkg / "api" / "routes.py").write_text(
        textwrap.dedent("""\
        from core.db import query

        def get_items():
            \"\"\"Fetch all items.\"\"\"
            return query("SELECT * FROM items")
    """)
    )
    (pkg / "core" / "db.py").write_text(
        textwrap.dedent("""\
        def query(sql):
            \"\"\"Execute a SQL query.\"\"\"
            pass
    """)
    )

    si, im = build_index(pkg)
    assert "api.routes.get_items" in si
    assert "core.db.query" in si

    deps = resolve_dependencies(si, im)
    assert "core.db.query" in deps["api.routes.get_items"]

    md = format_units_md(si, deps)
    assert "`@core.db.query`" in md

    # Verify prepare.py can parse the output
    units, _ = parse_unit_descriptions(md)
    assert "api.routes.get_items" in units
    assert "core.db.query" in units["api.routes.get_items"]["dependencies"]


def test_end_to_end_with_class(tmp_path):
    """Classes fold method calls; method references resolve to the class."""
    register_languages()

    pkg = tmp_path / "pkg"
    pkg.mkdir()

    (pkg / "service.py").write_text(
        textwrap.dedent("""\
        from models import User

        class UserService:
            \"\"\"Manages users.\"\"\"
            def get_user(self, uid):
                return User.find(uid)
    """)
    )
    (pkg / "models.py").write_text(
        textwrap.dedent("""\
        class User:
            \"\"\"User model.\"\"\"
            @classmethod
            def find(cls, uid):
                pass
    """)
    )

    si, im = build_index(pkg)
    deps = resolve_dependencies(si, im)
    assert "models.User" in deps["service.UserService"]
