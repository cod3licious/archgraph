"""Registry of the supported languages; add new languages to register_languages()."""

import importlib
from collections.abc import Callable

from languages import python, typescript
from languages.base import ImportInfo, LanguageConfig, module_segment, node_text

__all__ = ["LANGUAGE_CONFIGS", "ImportInfo", "LanguageConfig", "module_segment", "node_text", "register_languages"]

# Populated at runtime by register_languages()
LANGUAGE_CONFIGS: dict[str, tuple[Callable, LanguageConfig]] = {}


def register_languages() -> None:
    """Lazily import tree-sitter language modules and populate LANGUAGE_CONFIGS."""
    # (file extension, grammar module, grammar function, config factory)
    entries: list[tuple[str, str, str, Callable[[], LanguageConfig]]] = [
        ("py", "tree_sitter_python", "language", python.make_config),
        ("ts", "tree_sitter_typescript", "language_typescript", typescript.make_config),
        ("svelte", "tree_sitter_typescript", "language_typescript", typescript.make_svelte_config),
    ]
    for ext, module_name, function_name, config_factory in entries:
        try:
            LANGUAGE_CONFIGS[ext] = (getattr(importlib.import_module(module_name), function_name), config_factory())
        except ImportError:
            pass
