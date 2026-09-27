"""Registry of the supported languages; add new languages to register_languages()."""

from collections.abc import Callable

from languages import python
from languages.base import ImportInfo, LanguageConfig, node_text

__all__ = ["LANGUAGE_CONFIGS", "ImportInfo", "LanguageConfig", "node_text", "register_languages"]

# Populated at runtime by register_languages()
LANGUAGE_CONFIGS: dict[str, tuple[Callable, LanguageConfig]] = {}


def register_languages() -> None:
    """Lazily import tree-sitter language modules and populate LANGUAGE_CONFIGS."""
    entries: list[tuple[str, str, Callable[[], LanguageConfig]]] = [
        ("py", "tree_sitter_python", python.make_config),
    ]
    for ext, module_name, config_factory in entries:
        try:
            mod = __import__(module_name)
            LANGUAGE_CONFIGS[ext] = (mod.language, config_factory())
        except ImportError:
            pass
