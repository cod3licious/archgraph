"""Tests for the TypeScript and Svelte language configs."""

import textwrap
from pathlib import Path

import tree_sitter as ts
import tree_sitter_typescript as tstypescript

from generate import build_index, parse_file, resolve_dependencies
from languages import register_languages
from languages.base import ImportInfo
from languages.typescript import extract_imports, extract_refs, extract_scripts, make_config, make_svelte_config

TS_CONFIG = make_config()
SVELTE_CONFIG = make_svelte_config()
TS_LANG = ts.Language(tstypescript.language_typescript())


def _parser() -> ts.Parser:
    return ts.Parser(TS_LANG)


def _parse(source: str, module_path: str = "lib.mod", config=TS_CONFIG, **kwargs):
    return parse_file(textwrap.dedent(source).encode(), module_path, config, _parser(), **kwargs)


def _imports(source: str, module_path: str = "lib.mod", *, is_package: bool = False) -> dict[str, str]:
    root = _parser().parse(textwrap.dedent(source).encode()).root_node
    return {i.local_name: i.qualified_name for i in extract_imports(root, module_path, is_package)}


def _refs(source: str) -> list[str]:
    """Refs of the first top-level node."""
    return extract_refs(_parser().parse(textwrap.dedent(source).encode()).root_node.children[0])


def _resolve_files(root: Path, files: dict[str, str]) -> dict[str, list[str]]:
    register_languages()
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source))
    return resolve_dependencies(*build_index(root))


# =============================================================================
# Units
# =============================================================================


def test_units_and_kinds():
    units, _ = _parse("""\
        export function f() {}
        export async function g() {}
        export const h = () => 1;
        export const k = function () {};
        export class C {}
        export abstract class A {}
        export interface I {}
        export type T = string;
        export enum E { X }
        export default function d() {}
    """)
    assert [(u.name, u.kind) for u in units] == [
        ("f", "function"),
        ("g", "function"),
        ("h", "function"),
        ("k", "function"),
        ("C", "class"),
        ("A", "class"),
        ("I", "type"),
        ("T", "type"),
        ("E", "enum"),
        ("d", "function"),
    ]
    assert not any(u.is_private for u in units)


def test_non_exported_definitions_are_private():
    units, _ = _parse("function helper() {}\ninterface Props {}\nexport function f() {}\n")
    assert [(u.name, u.is_private) for u in units] == [("helper", True), ("Props", True), ("f", False)]


def test_skips_constants_signatures_and_multi_declarations():
    units, _ = _parse("""\
        export const LIMIT = 5;
        export const { a, b } = obj;
        export const x = () => 1, y = () => 2;
        export function over(a: string): string;
        export function over(a: number): number;
        export function over(a: any) { return a; }
        declare function ambient(): void;
    """)
    assert [u.name for u in units] == ["over"]


def test_entry_point_collects_top_level_code():
    units, _ = _parse("""\
        import App from './App.svelte';
        watch(1);
        const target = find();
        if (!target) throw new Error('missing');
        export default mount(App, { target });
    """)
    assert [(u.name, u.kind) for u in units] == [("__main__", "entry point")]
    assert set(units[0].raw_refs) == {"watch", "target", "Error", "mount", "App"}


# =============================================================================
# Docstrings
# =============================================================================


def test_docstring_jsdoc():
    units, _ = _parse("""\
        /**
         * Summary line.
         *
         * @param x - the input
         */
        export function f(x: number) {}
    """)
    assert units[0].docstring == "Summary line.\n\n@param x - the input"


def test_docstring_line_comments_directly_above():
    units, _ = _parse("""\
        // Unrelated header.

        // First line
        // second line.
        export const f = () => 1;
        export function g() {}
    """)
    assert units[0].docstring == "First line\nsecond line."
    assert units[1].docstring is None


# =============================================================================
# Imports
# =============================================================================


def test_imports_named_default_namespace_and_type():
    imports = _imports("""\
        import Chip, { type Trip as T, api } from './Chip.svelte';
        import * as fmt from '../format';
        import type { Out } from './api';
        import { onMount } from 'svelte';
        import './app.css';
    """)
    assert imports == {
        "Chip": "lib.Chip.default",
        "T": "lib.Chip.Trip",
        "api": "lib.Chip.api",
        "fmt": "format",
        "Out": "lib.api.Out",
        "onMount": "svelte.onMount",
    }


def test_imports_resolve_relative_to_package_file():
    """In an index.ts the module path is the directory itself."""
    assert _imports("import { f } from './impl';\n", "lib", is_package=True) == {"f": "lib.impl.f"}


def test_imports_normalize_specifiers_like_file_names():
    imports = _imports("import { a } from './flow.svelte';\nimport { b } from './my-util.js';\n")
    assert imports == {"a": "lib.flow.a", "b": "lib.my-util.b"}


def test_reexports_and_default_exports():
    imports = _imports("""\
        export { a, b as c } from './x';
        export * from './y';
        export { local as renamed, plain };
        export default function main() {}
    """)
    assert imports == {"a": "lib.x.a", "c": "lib.x.b", "renamed": "lib.mod.local", "default": "lib.mod.main"}
    assert _imports("const f = 1;\nexport default f;\n") == {"default": "lib.mod.f"}
    assert _imports("export default mount(App);\n") == {}


def test_dynamic_imports():
    source = "const routes = { home: () => import('./pages/Home.svelte'), ext: () => import('mapbox-gl') };\n"
    imports = _imports(source)
    refs = _refs(source)
    assert sorted(imports.values()) == ["lib.pages.Home.default", "mapbox-gl.default"]
    assert set(refs) == set(imports)


# =============================================================================
# Refs
# =============================================================================


def test_refs_calls_chains_and_types():
    refs = _refs("""\
        export class C extends Base implements I {
            run(x: ns.Trip, y: Map<K, V>): Out | typeof v {
                this.helper();
                return api.get(x)?.b.c + fmt.price({ short });
            }
        }
    """)
    assert set(refs) == {"Base", "I", "ns.Trip", "Map", "K", "V", "Out", "v", "api.get", "fmt.price", "short"}


def test_refs_skip_locally_bound_names():
    refs = _refs("""\
        export function f<G>(a: G, { b, c: d, e = dflt }: P, [g, ...h]: Q, k = defaultK) {
            const local = 1;
            for (const item of items) item();
            try { run(); } catch (err) { err.log(); }
            const cb = (z) => z + local + a + b + d + e + g + h + k;
            function inner() {}
            return inner(cb);
        }
    """)
    assert set(refs) == {"P", "Q", "dflt", "defaultK", "items", "run"}


# =============================================================================
# Svelte
# =============================================================================


def test_extract_scripts_joins_all_script_blocks():
    source = b'<script module lang="ts">A</script>\n<div>{x}</div>\n<script lang="ts" generics="T">B</script>\n'
    assert extract_scripts(source) == b"A\nB"


def test_svelte_component_is_a_single_unit():
    units, imports = _parse(
        """\
        <script lang="ts">
          // A small chip.
          import Icon from './Icon.svelte';
          import { format } from '../format';
          function handle() { track(); }
        </script>

        <button onclick={handle}><Icon />{format(1)}</button>
        """,
        "lib.components.Chip",
        SVELTE_CONFIG,
    )
    assert len(units) == 1
    unit = units[0]
    assert (unit.qualified_name, unit.submodule, unit.name, unit.kind) == (
        "lib.components.Chip",
        "lib.components",
        "Chip",
        "component",
    )
    assert (unit.module, unit.docstring) == ("lib.components.Chip", "A small chip.")
    # Markup-only usages (Icon, format) count through the imports
    assert set(unit.raw_refs) == {"track", "Icon", "format"}
    assert ImportInfo("default", "lib.components.Chip") in imports


def test_svelte_component_at_the_root_is_its_own_submodule():
    units, imports = _parse("<div></div>\n", "App", SVELTE_CONFIG)
    assert (units[0].qualified_name, units[0].submodule) == ("App.App", "App")
    assert imports == [ImportInfo("default", "App.App")]


# =============================================================================
# End-to-end: build_index + resolve_dependencies
# =============================================================================


def test_resolve_svelte_app(tmp_path):
    deps = _resolve_files(
        tmp_path,
        {
            "main.ts": "import App from './App.svelte';\nmount(App, {});\n",
            "App.svelte": """\
                <script lang="ts">
                  import { navigate } from './lib';
                  const pages = { home: () => import('./pages/Home.svelte') };
                </script>
            """,
            "pages/Home.svelte": """\
                <script lang="ts">
                  import Chip, { type ChipKind } from '../lib/components/Chip.svelte';
                  import { quiz } from '../lib/quiz.svelte';
                </script>
                <Chip />
            """,
            "lib/components/Chip.svelte": "<script module lang=\"ts\">export type ChipKind = 'a';</script>\n",
            "lib/index.ts": "export { navigate } from './router';\n",
            "lib/router.ts": "export function navigate(to: string) { log(to); }\nfunction log(x: string) { format(x); }\n"
            "import { format } from './quiz';\n",
            "lib/quiz.ts": "export function format(x: string) {}\n",
            "lib/quiz.svelte.ts": "import { format } from './quiz';\nexport const quiz = () => format('q');\n",
        },
    )
    # quiz.ts and quiz.svelte.ts are merged into one module; the private `log` is inlined into `navigate`
    assert {unit: sorted(targets) for unit, targets in deps.items()} == {
        "main.__main__": ["App.App"],
        "App.App": ["lib.router.navigate", "pages.Home"],
        "pages.Home": ["lib.components.Chip", "lib.quiz.quiz"],
        "lib.components.Chip": [],
        "lib.router.navigate": ["lib.quiz.format"],
        "lib.quiz.format": [],
        "lib.quiz.quiz": ["lib.quiz.format"],
    }
