"""Tests for generate.py and languages.py."""

import json
import random
import textwrap
from pathlib import Path

import tree_sitter as ts
import tree_sitter_python as tspython

from generate import (
    UnitInfo,
    build_index,
    file_path_to_module,
    format_units_md,
    generate_folder,
    generate_layers_draft,
    package_prefix,
    parse_file,
    resolve_dependencies,
)
from languages import (
    ImportInfo,
    _make_python_config,
    _python_extract_docstring,
    _python_extract_imports,
    _python_extract_refs,
    _resolve_relative_import,
    register_languages,
)
from prepare import flatten_layers, parse_unit_descriptions, prepare_folder

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


def test_file_path_to_module_with_prefix():
    """Package prefix is prepended so paths match absolute imports (e.g. root is a package)."""
    assert file_path_to_module(Path("/root/foo/bar.py"), Path("/root"), PY_CONFIG, ("pkg",)) == "pkg.foo.bar"
    assert file_path_to_module(Path("/root/__init__.py"), Path("/root"), PY_CONFIG, ("pkg",)) == "pkg"


def test_package_prefix(tmp_path):
    """When root itself is a package, its name (and package ancestors) prefix module paths."""
    (tmp_path / "verimo").mkdir()
    (tmp_path / "verimo" / "__init__.py").touch()
    assert package_prefix(tmp_path / "verimo", PY_CONFIG) == ("verimo",)
    # root without a package marker -> no prefix (backward compatible)
    assert package_prefix(tmp_path, PY_CONFIG) == ()


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


def test_docstring_string_prefixes():
    for prefix in ("r", "u", "R", "rb", "Br", "f"):
        root = _parse_node(f'def f():\n    {prefix}"""Hello."""\n')
        assert _python_extract_docstring(root.children[0]) == "Hello.", prefix


def test_docstring_dedented():
    root = _parse_node('def f():\n    """Summary.\n\n    Details\n        indented.\n    """\n')
    assert _python_extract_docstring(root.children[0]) == "Summary.\n\nDetails\n    indented."


def test_docstring_class():
    root = _parse_node('class C:\n    """Class doc."""\n    pass\n')
    assert _python_extract_docstring(root.children[0]) == "Class doc."


# =============================================================================
# Python import extraction
# =============================================================================


def test_import_simple():
    root = _parse_node("import os\n")
    imports = _python_extract_imports(root, "mymod", False)
    assert imports == [ImportInfo(local_name="os", qualified_name="os")]


def test_import_dotted_binds_top_level_package():
    """`import a.b.c` binds `a` to package `a`, so `a.b.c.f()` resolves to `a.b.c.f`."""
    root = _parse_node("import pkg.x\nimport pkg.y\n")
    imports = _python_extract_imports(root, "mymod", False)
    assert imports == [ImportInfo("pkg", "pkg"), ImportInfo("pkg", "pkg")]


def test_import_aliased():
    root = _parse_node("import a.b as c\nfrom x import y as z, w\n")
    imports = _python_extract_imports(root, "mymod", False)
    assert imports == [ImportInfo("c", "a.b"), ImportInfo("z", "x.y"), ImportInfo("w", "x.w")]


def test_relative_import_in_package_file():
    """In an __init__.py the module path is the package itself, so `.` refers to it."""
    root = _parse_node("from .impl import f\nfrom . import util\nfrom .. import top\n")
    imports = _python_extract_imports(root, "pkg.core", True)
    assert imports == [
        ImportInfo("f", "pkg.core.impl.f"),
        ImportInfo("util", "pkg.core.util"),
        ImportInfo("top", "pkg.top"),
    ]


def test_relative_import_beyond_root():
    """`from . import x` resolving to the empty package keeps `x` as the imported name."""
    root = _parse_node("from . import sibling\n")
    imports = _python_extract_imports(root, "main", False)
    assert imports == [ImportInfo("sibling", "sibling")]


def test_imports_nested_in_blocks_and_functions():
    source = textwrap.dedent("""\
        from typing import TYPE_CHECKING
        try:
            from fast import parse
        except ImportError:
            from slow import parse
        if TYPE_CHECKING:
            from models import User
        def f():
            from .lazy import load
    """)
    imports = _python_extract_imports(_parse_node(source), "pkg.mod", False)
    local = {i.local_name: i.qualified_name for i in imports}
    assert local == {
        "TYPE_CHECKING": "typing.TYPE_CHECKING",
        "parse": "slow.parse",  # last import wins
        "User": "models.User",
        "load": "pkg.lazy.load",
    }


def test_from_import():
    root = _parse_node("from pathlib import Path\n")
    imports = _python_extract_imports(root, "mymod", False)
    assert imports == [ImportInfo(local_name="Path", qualified_name="pathlib.Path")]


def test_from_import_multiple():
    root = _parse_node("from os.path import join, exists\n")
    imports = _python_extract_imports(root, "mymod", False)
    assert len(imports) == 2
    assert imports[0] == ImportInfo(local_name="join", qualified_name="os.path.join")
    assert imports[1] == ImportInfo(local_name="exists", qualified_name="os.path.exists")


def test_relative_import_dot():
    root = _parse_node("from . import sibling\n")
    imports = _python_extract_imports(root, "pkg.sub.mymod", False)
    assert imports == [ImportInfo(local_name="sibling", qualified_name="pkg.sub.sibling")]


def test_relative_import_dot_name():
    root = _parse_node("from .other import func\n")
    imports = _python_extract_imports(root, "pkg.sub.mymod", False)
    assert imports == [ImportInfo(local_name="func", qualified_name="pkg.sub.other.func")]


def test_relative_import_two_dots():
    root = _parse_node("from .. import util\n")
    imports = _python_extract_imports(root, "pkg.sub.mymod", False)
    assert imports == [ImportInfo(local_name="util", qualified_name="pkg.util")]


def test_star_import_skipped():
    root = _parse_node("from os import *\n")
    imports = _python_extract_imports(root, "mymod", False)
    assert imports == []


# =============================================================================
# Python call extraction
# =============================================================================


def _get_def(source: str) -> ts.Node:
    """Parse source and return the first definition node."""
    return _parse_node(source).children[0]


def test_call_simple():
    calls = _python_extract_refs(_get_def("def f():\n    foo()\n"))
    assert "foo" in calls


def test_call_attribute():
    calls = _python_extract_refs(_get_def("def f():\n    os.path.join('a', 'b')\n"))
    assert "os.path.join" in calls


def test_refs_callbacks_decorators_and_bases():
    source = textwrap.dedent("""\
        @register
        @app.route(path)
        class C(Base, mod.Mixin):
            def run(self, xs):
                return sorted(map(cb, xs), key=_keyfn)
    """)
    refs = _python_extract_refs(_get_def(source))
    assert {"register", "app.route", "path", "Base", "mod.Mixin", "cb", "_keyfn", "sorted", "map"} <= set(refs)
    # def/class names, keyword argument names and attribute members are not refs
    assert not {"C", "run", "key", "route", "Mixin"} & set(refs)


def test_refs_skip_locally_bound_names():
    source = textwrap.dedent("""\
        def f(a, b: T, c=default_cb, *args, **kw):
            for helper in items:
                helper()
            x, *rest = split()
            with open_it() as fh:
                fh.read()
            return [g(y) for y in ys] + a.b
    """)
    refs = _python_extract_refs(_get_def(source))
    assert set(refs) == {"T", "default_cb", "items", "split", "open_it", "g", "ys"}


def test_refs_deduplicated():
    assert _python_extract_refs(_get_def("def f():\n    g()\n    g()\n")) == ["g"]


def test_call_self_skipped():
    calls = _python_extract_refs(_get_def("def f(self):\n    self.method()\n"))
    assert not any(c.startswith("self.") for c in calls)


def test_call_cls_skipped():
    calls = _python_extract_refs(_get_def("def f(cls):\n    cls.create()\n"))
    assert not any(c.startswith("cls.") for c in calls)


# =============================================================================
# Relative import resolution
# =============================================================================


def test_resolve_relative_one_dot():
    node = _parse_node("from .sub import x\n").children[0]
    rel_node = next(c for c in node.children if c.type == "relative_import")
    assert _resolve_relative_import(rel_node, "pkg.mymod", False) == "pkg.sub"


def test_resolve_relative_two_dots():
    node = _parse_node("from ..other import x\n").children[0]
    rel_node = next(c for c in node.children if c.type == "relative_import")
    assert _resolve_relative_import(rel_node, "pkg.sub.mymod", False) == "pkg.other"


def test_resolve_relative_dot_only():
    node = _parse_node("from . import x\n").children[0]
    rel_node = next(c for c in node.children if c.type == "relative_import")
    assert _resolve_relative_import(rel_node, "pkg.sub.mymod", False) == "pkg.sub"


# =============================================================================
# parse_file
# =============================================================================


def test_parse_file_functions():
    source = b'def public():\n    """Doc."""\n    pass\n\ndef _private():\n    pass\n'
    units, _imports = parse_file(source, "mymod", PY_CONFIG, _parser())
    assert [(u.name, u.is_private) for u in units] == [("public", False), ("_private", True)]
    assert units[0].docstring == "Doc."


def test_parse_file_decorated_definitions():
    """Decorated defs are units; decorator refs count as deps, the docstring comes from the def."""
    source = textwrap.dedent("""\
        @app.route("/x")
        @depends(load_user)
        def view():
            \"\"\"Show the view.\"\"\"
            render()

        @dataclass
        class Config:
            \"\"\"Settings.\"\"\"
    """).encode()
    units, _ = parse_file(source, "mymod", PY_CONFIG, _parser())
    assert [(u.name, u.kind, u.docstring) for u in units] == [
        ("view", "function", "Show the view."),
        ("Config", "class", "Settings."),
    ]
    assert {"app.route", "depends", "load_user", "render"} <= set(units[0].raw_refs)
    assert "dataclass" in units[1].raw_refs


def test_parse_file_skips_overload_stubs():
    source = textwrap.dedent("""\
        @overload
        def f(x: int) -> int: ...
        @typing.overload
        def f(x: str) -> str: ...
        def f(x):
            \"\"\"Implementation.\"\"\"
    """).encode()
    units, _ = parse_file(source, "mymod", PY_CONFIG, _parser())
    assert [(u.name, u.docstring) for u in units] == [("f", "Implementation.")]


def test_parse_file_class_with_methods():
    source = b'class MyClass:\n    """Class doc."""\n    def method(self):\n        helper()\n'
    units, _ = parse_file(source, "mymod", PY_CONFIG, _parser())
    assert len(units) == 1
    unit = units[0]
    assert unit.name == "MyClass"
    assert unit.kind == "class"
    assert unit.docstring == "Class doc."
    assert "helper" in unit.raw_refs


def test_parse_file_class_collects_all_method_calls():
    source = b"class C:\n    def a(self):\n        foo()\n    def b(self):\n        bar()\n"
    units, _ = parse_file(source, "mymod", PY_CONFIG, _parser())
    assert "foo" in units[0].raw_refs
    assert "bar" in units[0].raw_refs


def test_parse_file_imports():
    source = b"from pathlib import Path\nimport os\ndef f():\n    pass\n"
    _, imports = parse_file(source, "mymod", PY_CONFIG, _parser())
    local_names = {i.local_name for i in imports}
    assert "Path" in local_names
    assert "os" in local_names


def test_parse_file_package_relative_imports():
    _, imports = parse_file(b"from .impl import f\n", "pkg.core", PY_CONFIG, _parser(), is_package=True)
    assert imports == [ImportInfo("f", "pkg.core.impl.f")]


def test_parse_file_empty():
    source = b""
    units, imports = parse_file(source, "mymod", PY_CONFIG, _parser())
    assert units == []
    assert imports == []


# =============================================================================
# build_index
# =============================================================================


def _write_files(root: Path, files: dict[str, str]) -> Path:
    """Write {relative path: source} below root (sources are dedented) and return root."""
    register_languages()
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source))
    return root


def _resolve_files(root: Path, files: dict[str, str], **kwargs) -> dict[str, list[str]]:
    """Write files, build the index and resolve dependencies."""
    si, im, helpers = build_index(_write_files(root, files), **kwargs)
    return resolve_dependencies(si, im, helpers)


def test_build_index(tmp_path):
    register_languages()
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod_a.py").write_text('def func_a():\n    """Doc A."""\n    func_b()\n')
    (pkg / "mod_b.py").write_text("def func_b():\n    pass\n")

    si, _im, _ = build_index(pkg)
    assert "mod_a.func_a" in si
    assert "mod_b.func_b" in si
    assert "func_b" in si["mod_a.func_a"].raw_refs


def test_build_index_excludes(tmp_path):
    register_languages()
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "main.py").write_text("def main():\n    pass\n")
    (pkg / "test_main.py").write_text("def test_it():\n    pass\n")

    si, _, _ = build_index(pkg, exclude_patterns=["test_*"])
    assert "main.main" in si
    assert "test_main.test_it" not in si


def test_build_index_private_excluded_by_default(tmp_path):
    root = _write_files(tmp_path, {"mod.py": "def public():\n    pass\n\ndef _private():\n    pass\n"})
    si, _, helpers = build_index(root)
    assert list(si) == ["mod.public"]
    assert list(helpers) == ["mod._private"]

    si, _, helpers = build_index(root, include_private=True)
    assert list(si) == ["mod.public", "mod._private"]
    assert helpers == {}


def test_build_index_skips_unit_colliding_with_submodule(tmp_path):
    """`main` in run/__init__.py would have the same path as the submodule run/main.py."""
    root = _write_files(
        tmp_path / "pkg",
        {
            "__init__.py": "",
            "run/__init__.py": "def main():\n    pass\n\ndef setup():\n    pass\n",
            "run/main.py": "def main():\n    pass\n",
        },
    )
    si, _, _ = build_index(root)
    assert set(si) == {"pkg.run.setup", "pkg.run.main.main"}


def test_build_index_namespace_package_root(tmp_path):
    """A root without __init__.py can still be imported by its own name."""
    deps = _resolve_files(
        tmp_path / "nspkg",
        {
            "api/routes.py": "from nspkg.core.db import query\n\ndef get():\n    query()\n",
            "core/db.py": "def query():\n    pass\n",
        },
    )
    assert deps["api.routes.get"] == ["core.db.query"]


# =============================================================================
# resolve_dependencies
# =============================================================================


def test_resolve_exact_match():
    si = {
        "a.func_a": UnitInfo("a.func_a", "a", "func_a", "function", raw_refs=["func_b"]),
        "a.func_b": UnitInfo("a.func_b", "a", "func_b", "function"),
    }
    im = {"a": {}}
    deps = resolve_dependencies(si, im)
    assert deps["a.func_a"] == ["a.func_b"]


def test_resolve_through_import():
    si = {
        "a.caller": UnitInfo("a.caller", "a", "caller", "function", raw_refs=["Target"]),
        "b.Target": UnitInfo("b.Target", "b", "Target", "class"),
    }
    im = {"a": {"Target": "b.Target"}, "b": {}}
    deps = resolve_dependencies(si, im)
    assert deps["a.caller"] == ["b.Target"]


def test_resolve_method_to_class():
    si = {
        "a.caller": UnitInfo("a.caller", "a", "caller", "function", raw_refs=["MyClass.method"]),
        "b.MyClass": UnitInfo("b.MyClass", "b", "MyClass", "class"),
    }
    im = {"a": {"MyClass": "b.MyClass"}, "b": {}}
    deps = resolve_dependencies(si, im)
    assert deps["a.caller"] == ["b.MyClass"]


def test_resolve_self_dep_skipped():
    si = {
        "a.func": UnitInfo("a.func", "a", "func", "function", raw_refs=["func"]),
    }
    im = {"a": {}}
    deps = resolve_dependencies(si, im)
    assert deps["a.func"] == []


def test_resolve_unresolvable_skipped():
    si = {
        "a.func": UnitInfo("a.func", "a", "func", "function", raw_refs=["unknown_thing"]),
    }
    im = {"a": {}}
    deps = resolve_dependencies(si, im)
    # "unknown_thing" resolves to "a.unknown_thing" via same-module, but that doesn't exist
    assert deps["a.func"] == []


def test_resolve_deduplicates():
    si = {
        "a.caller": UnitInfo("a.caller", "a", "caller", "function", raw_refs=["target", "target"]),
        "a.target": UnitInfo("a.target", "a", "target", "function"),
    }
    im = {"a": {}}
    deps = resolve_dependencies(si, im)
    assert deps["a.caller"] == ["a.target"]


def test_resolve_same_module_attribute_chain():
    """`Local.make()` and nested `Model.Manager.create()` resolve to the same-module class."""
    si = {
        "a.caller": UnitInfo("a.caller", "a", "caller", "function", raw_refs=["Local.make", "Model.Manager.create"]),
        "a.Local": UnitInfo("a.Local", "a", "Local", "class"),
        "a.Model": UnitInfo("a.Model", "a", "Model", "class"),
    }
    deps = resolve_dependencies(si, {"a": {}})
    assert deps["a.caller"] == ["a.Local", "a.Model"]


def test_resolve_never_strips_into_a_module_path():
    """A ref into module `pkg.sub` must not resolve to a unit `pkg.sub` defined in pkg/__init__.py."""
    si = {
        "pkg.sub": UnitInfo("pkg.sub", "pkg", "sub", "function"),
        "pkg.sub.mod.caller": UnitInfo("pkg.sub.mod.caller", "pkg.sub.mod", "caller", "function", raw_refs=["len"]),
    }
    im = {"pkg": {}, "pkg.sub": {}, "pkg.sub.mod": {}}
    assert resolve_dependencies(si, im)["pkg.sub.mod.caller"] == []


def test_resolve_aliased_and_dotted_imports(tmp_path):
    deps = _resolve_files(
        tmp_path,
        {
            "app.py": """\
                import lib.x
                import lib.y
                import lib.z as zz
                from lib.x import helper as h

                def run():
                    lib.x.fx()
                    lib.y.fy()
                    zz.fz()
                    h()
            """,
            "lib/__init__.py": "",
            "lib/x.py": "def fx():\n    pass\n\ndef helper():\n    pass\n",
            "lib/y.py": "def fy():\n    pass\n",
            "lib/z.py": "def fz():\n    pass\n",
        },
    )
    assert sorted(deps["app.run"]) == ["lib.x.fx", "lib.x.helper", "lib.y.fy", "lib.z.fz"]


def test_resolve_relative_imports_in_init(tmp_path):
    deps = _resolve_files(
        tmp_path / "pkg",
        {
            "__init__.py": "from . import util\nfrom .core import build\n\ndef main():\n    util.fmt()\n    build()\n",
            "util.py": "def fmt():\n    pass\n",
            "core.py": "def build():\n    pass\n",
        },
    )
    assert deps["pkg.main"] == ["pkg.util.fmt", "pkg.core.build"]


def test_resolve_follows_reexports(tmp_path):
    deps = _resolve_files(
        tmp_path / "pkg",
        {
            "__init__.py": "",
            "api.py": "from pkg.core import f, Model\n\ndef g():\n    f()\n    Model.create()\n",
            "core/__init__.py": "from .impl import f\nfrom .models import *\nfrom .models import Model\n",
            "core/impl.py": "def f():\n    pass\n",
            "core/models.py": "class Model:\n    pass\n",
        },
    )
    assert deps["pkg.api.g"] == ["pkg.core.impl.f", "pkg.core.models.Model"]


def test_resolve_reexport_cycle_terminates():
    si = {"a.caller": UnitInfo("a.caller", "a", "caller", "function", raw_refs=["x"])}
    im = {"a": {"x": "b.x"}, "b": {"x": "a.x"}}
    assert resolve_dependencies(si, im)["a.caller"] == []


def test_resolve_nested_imports(tmp_path):
    deps = _resolve_files(
        tmp_path,
        {
            "app.py": """\
                from typing import TYPE_CHECKING
                if TYPE_CHECKING:
                    from models import User

                def load(user: User):
                    from db import fetch
                    return fetch(user)
            """,
            "models.py": "class User:\n    pass\n",
            "db.py": "def fetch(x):\n    pass\n",
        },
    )
    assert sorted(deps["app.load"]) == ["db.fetch", "models.User"]


def test_resolve_non_call_refs(tmp_path):
    """Base classes, callbacks and bare decorators count as deps; locally bound names don't."""
    deps = _resolve_files(
        tmp_path,
        {
            "base.py": "class Base:\n    pass\n\ndef register(c):\n    return c\n",
            "app.py": """\
                import base
                from base import register

                def keyfn(x):
                    pass

                def helper():
                    pass

                @register
                class Child(base.Base):
                    def run(self, items):
                        for helper in items:
                            helper()
                        return sorted(items, key=keyfn)
            """,
        },
    )
    assert sorted(deps["app.Child"]) == ["app.keyfn", "base.Base", "base.register"]


def test_resolve_inlines_same_module_private_helpers(tmp_path):
    """A public unit inherits the outgoing deps of private helpers it uses, transitively."""
    deps = _resolve_files(
        tmp_path,
        {
            "lib.py": "def thing():\n    pass\n\ndef deep():\n    pass\n",
            "mod.py": """\
                from lib import thing, deep

                def public():
                    _a()

                def _a():
                    thing()
                    _b()

                def _b():
                    deep()
                    _a()
            """,
        },
    )
    assert deps["mod.public"] == ["lib.thing", "lib.deep"]
    assert "mod._a" not in deps


def test_resolve_inlines_private_helpers_across_modules(tmp_path):
    """Imported private helpers, private class methods and helpers passed as callbacks are inlined."""
    deps = _resolve_files(
        tmp_path,
        {
            "lib.py": "def x():\n    pass\n\ndef y():\n    pass\n\ndef z():\n    pass\n",
            "helpers.py": "from lib import x\n\ndef _imported():\n    x()\n",
            "mod.py": """\
                from helpers import _imported
                from lib import y, z

                class _Private:
                    def method(self):
                        y()

                def _callback(item):
                    z()

                def public(items):
                    _imported()
                    _Private.method()
                    return map(_callback, items)
            """,
        },
    )
    assert deps["mod.public"] == ["lib.x", "lib.y", "lib.z"]


def test_resolve_private_helpers_kept_when_included(tmp_path):
    """With include_private, helpers are their own units and deps are not inlined."""
    deps = _resolve_files(
        tmp_path,
        {"mod.py": "def public():\n    _helper()\n\ndef _helper():\n    other()\n\ndef other():\n    pass\n"},
        include_private=True,
    )
    assert deps["mod.public"] == ["mod._helper"]
    assert deps["mod._helper"] == ["mod.other"]


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

    units = parse_unit_descriptions(md)
    assert list(units) == ["mod.func_a", "mod.func_b"]
    assert "mod.func_b" in units["mod.func_a"]["dependencies"]


def test_format_units_md_truncates_docstring_by_default():
    si = {"mod.f": UnitInfo("mod.f", "mod", "f", "function", docstring="First paragraph.\n\nSecond paragraph.")}
    md = format_units_md(si, {"mod.f": []})
    assert "First paragraph." in md
    assert "Second paragraph." not in md


def test_format_units_md_full_docstrings():
    si = {"mod.f": UnitInfo("mod.f", "mod", "f", "function", docstring="First paragraph.\n\nSecond paragraph.")}
    md = format_units_md(si, {"mod.f": []}, full_docstrings=True)
    assert "First paragraph." in md
    assert "Second paragraph." in md


def test_format_units_md_first_paragraph_joined():
    doc = "Summary that\nspans two lines.\n   \nDetails."
    si = {"mod.f": UnitInfo("mod.f", "mod", "f", "function", docstring=doc)}
    md = format_units_md(si, {"mod.f": []})
    assert md == "### mod.f\nSummary that spans two lines.\n"


def test_format_units_md_escapes_docstring_markup():
    """Docstrings must not create fake units or dependencies when parsed by prepare.py."""
    doc = "```\nUses `@other.thing` internally.\n\n### Examples\n  ###\tMore\nSee `@x.y`."
    si = {
        "mod.f": UnitInfo("mod.f", "mod", "f", "function", docstring=doc),
        "mod.g": UnitInfo("mod.g", "mod", "g", "function"),
    }
    for full in (False, True):
        md = format_units_md(si, {"mod.f": ["mod.g"], "mod.g": []}, full_docstrings=full)
        units = parse_unit_descriptions(md)
        assert list(units) == ["mod.f", "mod.g"]
        assert list(units["mod.f"]["dependencies"]) == ["mod.g"]
    assert "\\### Examples" in md


# =============================================================================
# generate_layers_draft
# =============================================================================


def test_layers_draft_basic():
    si = {
        "api.routes.get": UnitInfo("api.routes.get", "api.routes", "get", "function"),
        "api.auth.check": UnitInfo("api.auth.check", "api.auth", "check", "function"),
        "core.db.query": UnitInfo("core.db.query", "core.db", "query", "function"),
    }
    draft = generate_layers_draft(si, {})
    assert draft["root_layers"] == [["api", "core"]]
    assert draft["submodule_layers"]["api"] == [["api.auth", "api.routes"]]
    assert draft["submodule_layers"]["core"] == [["core.db"]]


def test_layers_draft_strips_shared_root_package():
    """A common enclosing package is not a layer; group one level deeper."""
    si = {
        "pkg.core.analysis.f": UnitInfo("pkg.core.analysis.f", "pkg.core.analysis", "f", "function"),
        "pkg.core.interp.g": UnitInfo("pkg.core.interp.g", "pkg.core.interp", "g", "function"),
        "pkg.backend.server.h": UnitInfo("pkg.backend.server.h", "pkg.backend.server", "h", "function"),
    }
    draft = generate_layers_draft(si, {})
    assert draft["root_layers"] == [["pkg.backend", "pkg.core"]]
    assert draft["submodule_layers"]["pkg.core"] == [["pkg.core.analysis", "pkg.core.interp"]]
    assert draft["submodule_layers"]["pkg.backend"] == [["pkg.backend.server"]]


def test_layers_draft_leaf_module():
    """A root module with no submodules (single-level) should not appear in submodule_layers."""
    si = {
        "utils.helper": UnitInfo("utils.helper", "utils", "helper", "function"),
    }
    draft = generate_layers_draft(si, {})
    assert draft["root_layers"] == [["utils"]]
    assert "utils" not in draft["submodule_layers"]


def test_layers_draft_empty():
    assert generate_layers_draft({}, {}) == {"root_layers": [], "submodule_layers": {}}


def test_layers_draft_root_with_direct_units_lists_itself():
    """A root module with own units (e.g. from __init__.py) and submodules lists itself as a submodule."""
    si = {
        "pkg.init_func": UnitInfo("pkg.init_func", "pkg", "init_func", "function"),
        "pkg.sub.deep_func": UnitInfo("pkg.sub.deep_func", "pkg.sub", "deep_func", "function"),
        "pkg.other.g": UnitInfo("pkg.other.g", "pkg.other", "g", "function"),
    }
    deps = {"pkg.sub.deep_func": ["pkg.init_func"], "pkg.init_func": ["pkg.other.g"], "pkg.other.g": []}
    draft = generate_layers_draft(si, deps)
    assert draft == {"root_layers": [["pkg"]], "submodule_layers": {"pkg": [["pkg.sub"], ["pkg"], ["pkg.other"]]}}
    assert set(flatten_layers(draft)) == {u.submodule for u in si.values()}


def test_layers_draft_nested_package_with_own_units():
    """Units in pkg/core/__init__.py and pkg/core/impl.py both stay in the layers."""
    si = {
        "pkg.core.f": UnitInfo("pkg.core.f", "pkg.core", "f", "function"),
        "pkg.core.impl.g": UnitInfo("pkg.core.impl.g", "pkg.core.impl", "g", "function"),
        "pkg.api.h": UnitInfo("pkg.api.h", "pkg.api", "h", "function"),
    }
    draft = generate_layers_draft(si, {})
    assert draft["submodule_layers"] == {"pkg.core": [["pkg.core", "pkg.core.impl"]]}
    assert set(flatten_layers(draft)) == {u.submodule for u in si.values()}


def test_layers_draft_valid_json():
    """Draft should be valid JSON and parseable."""
    si = {
        "a.b.func": UnitInfo("a.b.func", "a.b", "func", "function"),
        "c.func": UnitInfo("c.func", "c", "func", "function"),
    }
    draft = generate_layers_draft(si, {})
    assert json.loads(json.dumps(draft)) == draft


def test_layers_draft_dep_ordering():
    """Consumers above providers, at the root level and within each module."""
    si = {
        "app.main.run": UnitInfo("app.main.run", "app.main", "run", "function"),
        "app.cli.parse": UnitInfo("app.cli.parse", "app.cli", "parse", "function"),
        "app.api.handle": UnitInfo("app.api.handle", "app.api", "handle", "function"),
        "lib.db.query": UnitInfo("lib.db.query", "lib.db", "query", "function"),
        "lib.utils.fmt": UnitInfo("lib.utils.fmt", "lib.utils", "fmt", "function"),
    }
    # app.main -> app.api, app.cli; app.api -> lib.db; lib.db and lib.utils are unrelated within lib
    deps = {
        "app.main.run": ["app.api.handle", "app.cli.parse", "lib.db.query"],
        "app.api.handle": ["lib.db.query"],
    }
    draft = generate_layers_draft(si, deps)
    assert draft == {
        "root_layers": [["app"], ["lib"]],
        "submodule_layers": {"app": [["app.main"], ["app.api", "app.cli"]], "lib": [["lib.db", "lib.utils"]]},
    }


def _leaf_modules(*names: str) -> dict[str, UnitInfo]:
    """Create a symbol_index with one unit per leaf root module."""
    return {f"{n}.f": UnitInfo(f"{n}.f", n, "f", "function") for n in names}


def _module_deps(edges: dict[str, list[str]]) -> dict[str, list[str]]:
    """Unit dependencies for leaf modules created by _leaf_modules, given as module -> modules."""
    return {f"{src}.f": [f"{dst}.f" for dst in targets] for src, targets in edges.items()}


def _root_layers(edges: dict[str, list[str]], *isolated: str, **kwargs) -> list[list[str]]:
    """Root layers of leaf modules connected by module-level edges, plus isolated modules."""
    names = {*edges, *(t for targets in edges.values() for t in targets), *isolated}
    return generate_layers_draft(_leaf_modules(*names), _module_deps(edges), **kwargs)["root_layers"]


def _reaches(edges: dict[str, list[str]], src: str, dst: str) -> bool:
    """True if there is a path from src to dst."""
    seen, stack = set(), [src]
    while stack:
        node = stack.pop()
        if node == dst:
            return True
        if node not in seen:
            seen.add(node)
            stack.extend(edges.get(node, []))
    return False


def _assert_valid_layers(edges: dict[str, list[str]], layers: list[list[str]], max_width: int) -> None:
    """Every edge outside a cycle points strictly downward, rows respect the width, nodes appear once."""
    row_of = {m: r for r, row in enumerate(layers) for m in row}
    assert len(row_of) == sum(len(row) for row in layers)
    assert all(len(row) <= max_width for row in layers)
    for src, targets in edges.items():
        for dst in targets:
            assert row_of[src] != row_of[dst], (src, dst)
            if not _reaches(edges, dst, src):
                assert row_of[src] < row_of[dst], (src, dst)


# archgraph -> generate -> languages, archgraph -> prepare
ARCHGRAPH_EDGES = {"archgraph": ["generate", "prepare"], "generate": ["languages"]}


def test_layers_draft_siblings_share_row():
    assert _root_layers(ARCHGRAPH_EDGES) == [["archgraph"], ["generate", "prepare"], ["languages"]]


def test_layers_draft_top_aligned():
    """Nodes sit directly below their lowest consumer, not directly above their highest provider."""
    edges = {"app": ["service", "config"], "service": ["repo"], "repo": ["db"]}
    assert _root_layers(edges) == [["app"], ["config", "service"], ["repo"], ["db"]]


def test_layers_draft_diamond():
    edges = {"a": ["b", "c"], "b": ["d"], "c": ["d"]}
    assert _root_layers(edges) == [["a"], ["b", "c"], ["d"]]


def test_layers_draft_cycle_chained_in_consecutive_rows():
    """Cycle members can't be siblings; they are chained in net-flow order (alphabetical on ties)."""
    assert _root_layers({"a": ["b"], "b": ["c"], "c": ["a"]}) == [["a"], ["b"], ["c"]]
    assert _root_layers({"x": ["a"], "a": ["b"], "b": ["a"]}) == [["x"], ["a"], ["b"]]


def test_layers_draft_cycle_members_by_net_flow():
    """Within a cycle, the node with the most outgoing (vs incoming) edges comes first."""
    assert _root_layers({"x": ["y", "w"], "y": ["w"], "w": ["x"]}) == [["x"], ["y"], ["w"]]


def test_layers_draft_cycles_keep_providers_below_consumers():
    """Acyclic nodes downstream of cycles stay below all their consumers: a<->b, c<->d, a,b,c -> q, q -> p."""
    edges = {"a": ["b", "q"], "b": ["a", "q"], "c": ["d", "q"], "d": ["c"], "q": ["p"]}
    assert _root_layers(edges, "z") == [["a", "c"], ["b", "d"], ["q"], ["p"], ["z"]]


def test_layers_draft_isolated_modules_share_bottom_rows():
    """Isolated modules (no edges at all) share the bottom row(s), alphabetically, split by the max width."""
    assert _root_layers({"consumer": ["provider"]}, "lone2", "lone1") == [["consumer"], ["provider"], ["lone1", "lone2"]]
    assert _root_layers({}, "z", "m", "a") == [["a", "m", "z"]]
    assert _root_layers({}, *"gfedcba", max_row_width=3) == [["a", "b", "c"], ["d", "e", "f"], ["g"]]


def test_layers_draft_max_row_width():
    """Overflow moves down, keeping the node with the longer path to the bottom high to avoid extra depth."""
    edges = {"r": ["a", "b", "z"], "z": ["y"]}
    assert _root_layers(edges, max_row_width=2) == [["r"], ["a", "z"], ["b", "y"]]
    fan_out = {"root": [f"leaf{i}" for i in range(7)]}
    assert [len(row) for row in _root_layers(fan_out)] == [1, 5, 2]  # default width
    assert [len(row) for row in _root_layers(fan_out, max_row_width=3)] == [1, 3, 3, 1]


def test_layers_draft_max_row_width_zero_is_unlimited():
    edges = {"root": [f"leaf{i}" for i in range(7)], "leaf0": ["base"], "leaf6": ["base"]}
    unlimited = _root_layers(edges, max_row_width=0)
    assert unlimited == _root_layers(edges, max_row_width=100)
    assert [len(row) for row in unlimited] == [1, 7, 1]


def test_layers_draft_keeps_edges_downward():
    """Property check: over various graphs and widths, edges outside cycles always point down."""
    rng = random.Random(0)
    names = [f"m{i:02d}" for i in range(20)]
    graphs = [
        ARCHGRAPH_EDGES,
        {"a": ["b", "q"], "b": ["a", "q"], "c": ["d", "q"], "d": ["c"], "q": ["p"]},
        *({n: rng.sample([m for m in names if m != n], rng.randint(0, 3)) for n in names} for _ in range(5)),
    ]
    for edges in graphs:
        for width in (1, 2, 3, 0):
            layers = _root_layers(edges, max_row_width=width)
            _assert_valid_layers(edges, layers, width or len(names))


def test_layers_draft_reduces_crossings():
    """Rows are ordered by the positions of their neighbors instead of alphabetically."""
    edges = {"a": ["y", "shared"], "b": ["x", "shared"], "y": ["base"], "x": ["base"]}
    assert _root_layers(edges) == [["a", "b"], ["y", "shared", "x"], ["base"]]


def test_layers_draft_deterministic():
    edges = {"x": ["y", "w", "q"], "y": ["w", "q"], "w": ["x"], "q": ["p", "r"], "s": ["p"]}
    reversed_edges = {src: list(reversed(targets)) for src, targets in reversed(edges.items())}
    si = _leaf_modules("x", "y", "w", "q", "p", "r", "s", "lone")
    first = generate_layers_draft(si, _module_deps(edges))
    assert generate_layers_draft(dict(reversed(si.items())), _module_deps(reversed_edges)) == first
    assert generate_layers_draft(si, _module_deps(edges)) == first


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

    si, im, _ = build_index(pkg)
    assert "api.routes.get_items" in si
    assert "core.db.query" in si

    deps = resolve_dependencies(si, im)
    assert "core.db.query" in deps["api.routes.get_items"]

    md = format_units_md(si, deps)
    assert "`@core.db.query`" in md

    # Verify prepare.py can parse the output
    units = parse_unit_descriptions(md)
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

    si, im, _ = build_index(pkg)
    deps = resolve_dependencies(si, im)
    assert "models.User" in deps["service.UserService"]


def test_generate_folder_output_can_be_prepared(tmp_path):
    root = _write_files(
        tmp_path / "pkg",
        {
            "api/__init__.py": "",
            "api/routes.py": "from pkg.core.db import query\n\ndef handle():\n    return query()\n",
            "core/__init__.py": "",
            "core/db.py": "def query():\n    pass\n",
        },
    )
    out = tmp_path / "out"
    generate_folder(root, out)
    assert "`@core.db.query`" in (out / "units.md").read_text(encoding="utf-8")
    result = json.loads(prepare_folder(out, out, strict=True).read_text(encoding="utf-8"))
    assert result["units"]["api.routes.handle"]["dependencies"] == {"core.db.query": True}


def test_entry_point_block_becomes_main_unit(tmp_path):
    root = _write_files(
        tmp_path / "pkg",
        {
            "cli.py": """\
                import sys
                from pkg.core import run, check

                def helper():
                    pass

                if __name__ == '__main__':
                    args = sys.argv
                    run(args)

                if "__main__" == __name__:
                    check()
            """,
            "core.py": "def run(args):\n    pass\n\ndef check():\n    pass\n",
            "lib.py": "if __name__ == 'other':\n    helper()\n",
        },
    )
    si, im, helpers = build_index(root)
    assert [q for q in si if q.startswith("cli.")] == ["cli.helper", "cli.__main__"]  # kept in source order
    assert not si["cli.__main__"].is_private
    assert "lib.__main__" not in si
    deps = resolve_dependencies(si, im, helpers)
    assert deps["cli.__main__"] == ["core.run", "core.check"]
    assert "### cli.__main__\nEntry point in cli." in format_units_md(si, deps)
