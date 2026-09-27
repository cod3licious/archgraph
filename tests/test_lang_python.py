"""Tests for the Python language config."""

import textwrap

import tree_sitter as ts
import tree_sitter_python as tspython

from generate import parse_file
from languages.base import ImportInfo
from languages.python import extract_docstring, extract_imports, extract_refs, make_config, resolve_relative_import

PY_CONFIG = make_config()
PY_LANG = ts.Language(tspython.language())


def _parser() -> ts.Parser:
    return ts.Parser(PY_LANG)


def _parse_node(source: str) -> ts.Node:
    """Parse source and return the root node."""
    return _parser().parse(source.encode()).root_node


# =============================================================================
# Python docstring extraction
# =============================================================================


def test_docstring_triple_quotes():
    root = _parse_node('def f():\n    """Hello."""\n    pass\n')
    assert extract_docstring(root.children[0]) == "Hello."


def test_docstring_single_quotes():
    root = _parse_node("def f():\n    '''Hello.'''\n    pass\n")
    assert extract_docstring(root.children[0]) == "Hello."


def test_docstring_none_when_missing():
    root = _parse_node("def f():\n    pass\n")
    assert extract_docstring(root.children[0]) is None


def test_docstring_multiline():
    root = _parse_node('def f():\n    """Line 1.\n\n    Line 2.\n    """\n    pass\n')
    doc = extract_docstring(root.children[0])
    assert doc is not None
    assert "Line 1." in doc
    assert "Line 2." in doc


def test_docstring_string_prefixes():
    for prefix in ("r", "u", "R", "rb", "Br", "f"):
        root = _parse_node(f'def f():\n    {prefix}"""Hello."""\n')
        assert extract_docstring(root.children[0]) == "Hello.", prefix


def test_docstring_dedented():
    root = _parse_node('def f():\n    """Summary.\n\n    Details\n        indented.\n    """\n')
    assert extract_docstring(root.children[0]) == "Summary.\n\nDetails\n    indented."


def test_docstring_class():
    root = _parse_node('class C:\n    """Class doc."""\n    pass\n')
    assert extract_docstring(root.children[0]) == "Class doc."


# =============================================================================
# Python import extraction
# =============================================================================


def test_import_simple():
    root = _parse_node("import os\n")
    imports = extract_imports(root, "mymod", False)
    assert imports == [ImportInfo(local_name="os", qualified_name="os")]


def test_import_dotted_binds_top_level_package():
    """`import a.b.c` binds `a` to package `a`, so `a.b.c.f()` resolves to `a.b.c.f`."""
    root = _parse_node("import pkg.x\nimport pkg.y\n")
    imports = extract_imports(root, "mymod", False)
    assert imports == [ImportInfo("pkg", "pkg"), ImportInfo("pkg", "pkg")]


def test_import_aliased():
    root = _parse_node("import a.b as c\nfrom x import y as z, w\n")
    imports = extract_imports(root, "mymod", False)
    assert imports == [ImportInfo("c", "a.b"), ImportInfo("z", "x.y"), ImportInfo("w", "x.w")]


def test_relative_import_in_package_file():
    """In an __init__.py the module path is the package itself, so `.` refers to it."""
    root = _parse_node("from .impl import f\nfrom . import util\nfrom .. import top\n")
    imports = extract_imports(root, "pkg.core", True)
    assert imports == [
        ImportInfo("f", "pkg.core.impl.f"),
        ImportInfo("util", "pkg.core.util"),
        ImportInfo("top", "pkg.top"),
    ]


def test_relative_import_beyond_root():
    """`from . import x` resolving to the empty package keeps `x` as the imported name."""
    root = _parse_node("from . import sibling\n")
    imports = extract_imports(root, "main", False)
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
    imports = extract_imports(_parse_node(source), "pkg.mod", False)
    local = {i.local_name: i.qualified_name for i in imports}
    assert local == {
        "TYPE_CHECKING": "typing.TYPE_CHECKING",
        "parse": "slow.parse",  # last import wins
        "User": "models.User",
        "load": "pkg.lazy.load",
    }


def test_from_import():
    root = _parse_node("from pathlib import Path\n")
    imports = extract_imports(root, "mymod", False)
    assert imports == [ImportInfo(local_name="Path", qualified_name="pathlib.Path")]


def test_from_import_multiple():
    root = _parse_node("from os.path import join, exists\n")
    imports = extract_imports(root, "mymod", False)
    assert len(imports) == 2
    assert imports[0] == ImportInfo(local_name="join", qualified_name="os.path.join")
    assert imports[1] == ImportInfo(local_name="exists", qualified_name="os.path.exists")


def test_relative_import_dot():
    root = _parse_node("from . import sibling\n")
    imports = extract_imports(root, "pkg.sub.mymod", False)
    assert imports == [ImportInfo(local_name="sibling", qualified_name="pkg.sub.sibling")]


def test_relative_import_dot_name():
    root = _parse_node("from .other import func\n")
    imports = extract_imports(root, "pkg.sub.mymod", False)
    assert imports == [ImportInfo(local_name="func", qualified_name="pkg.sub.other.func")]


def test_relative_import_two_dots():
    root = _parse_node("from .. import util\n")
    imports = extract_imports(root, "pkg.sub.mymod", False)
    assert imports == [ImportInfo(local_name="util", qualified_name="pkg.util")]


def test_star_import_skipped():
    root = _parse_node("from os import *\n")
    imports = extract_imports(root, "mymod", False)
    assert imports == []


# =============================================================================
# Python call extraction
# =============================================================================


def _get_def(source: str) -> ts.Node:
    """Parse source and return the first definition node."""
    return _parse_node(source).children[0]


def test_call_simple():
    calls = extract_refs(_get_def("def f():\n    foo()\n"))
    assert "foo" in calls


def test_call_attribute():
    calls = extract_refs(_get_def("def f():\n    os.path.join('a', 'b')\n"))
    assert "os.path.join" in calls


def test_refs_callbacks_decorators_and_bases():
    source = textwrap.dedent("""\
        @register
        @app.route(path)
        class C(Base, mod.Mixin):
            def run(self, xs):
                return sorted(map(cb, xs), key=_keyfn)
    """)
    refs = extract_refs(_get_def(source))
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
    refs = extract_refs(_get_def(source))
    assert set(refs) == {"T", "default_cb", "items", "split", "open_it", "g", "ys"}


def test_refs_deduplicated():
    assert extract_refs(_get_def("def f():\n    g()\n    g()\n")) == ["g"]


def test_call_self_skipped():
    calls = extract_refs(_get_def("def f(self):\n    self.method()\n"))
    assert not any(c.startswith("self.") for c in calls)


def test_call_cls_skipped():
    calls = extract_refs(_get_def("def f(cls):\n    cls.create()\n"))
    assert not any(c.startswith("cls.") for c in calls)


# =============================================================================
# Relative import resolution
# =============================================================================


def test_resolve_relative_one_dot():
    node = _parse_node("from .sub import x\n").children[0]
    rel_node = next(c for c in node.children if c.type == "relative_import")
    assert resolve_relative_import(rel_node, "pkg.mymod", False) == "pkg.sub"


def test_resolve_relative_two_dots():
    node = _parse_node("from ..other import x\n").children[0]
    rel_node = next(c for c in node.children if c.type == "relative_import")
    assert resolve_relative_import(rel_node, "pkg.sub.mymod", False) == "pkg.other"


def test_resolve_relative_dot_only():
    node = _parse_node("from . import x\n").children[0]
    rel_node = next(c for c in node.children if c.type == "relative_import")
    assert resolve_relative_import(rel_node, "pkg.sub.mymod", False) == "pkg.sub"


# =============================================================================
# parse_file (Python-specific definitions)
# =============================================================================


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


def test_parse_file_package_relative_imports():
    _, imports = parse_file(b"from .impl import f\n", "pkg.core", PY_CONFIG, _parser(), is_package=True)
    assert imports == [ImportInfo("f", "pkg.core.impl.f")]
