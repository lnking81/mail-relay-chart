"""
Test generator registry for mail relay test suite.

Provides automatic discovery and registration of test generators.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING, Any, Optional

from .base import Tag, TestCase, TestGenerator

if TYPE_CHECKING:
    from .config import TestConfig

# Global registry of test generator classes
_generators: list[type[TestGenerator]] = []


def register(cls: type[TestGenerator]) -> type[TestGenerator]:
    """Decorator to register a test generator class.

    Example:
        @register
        class MyTestGenerator(TestGenerator):
            tags = {Tag.OUTBOUND}
            ...
    """
    if cls not in _generators:
        _generators.append(cls)
    return cls


def get_generators(
    tags: Optional[set[Tag]] = None,
) -> list[type[TestGenerator]]:
    """Get registered generator classes, optionally filtered by tags.

    Args:
        tags: If provided, only return generators with matching tags.
              Uses intersection (generator must have at least one matching tag).

    Returns:
        List of TestGenerator classes sorted by priority.
    """
    result = _generators.copy()

    if tags:
        result = [g for g in result if g.tags & tags]

    # Sort by priority (lower = earlier)
    result.sort(key=lambda g: g.priority)
    return result


def generate_tests(
    config: TestConfig,
    tags: Optional[set[Tag]] = None,
) -> list[TestCase]:
    """Generate all applicable tests for the given configuration.

    Args:
        config: Parsed test configuration.
        tags: If provided, only run generators with matching tags.

    Returns:
        List of TestCase objects from all applicable generators.
    """
    tests: list[TestCase] = []
    generators = get_generators(tags)

    for generator_cls in generators:
        generator = generator_cls()
        if generator.is_applicable(config):
            new_tests = generator.generate(config)
            # Add generator tags to tests if not already set
            for test in new_tests:
                if not test.tags:
                    test.tags = generator_cls.tags.copy()
            tests.extend(new_tests)

    return tests


def discover_generators(package_name: str = "tests.generators") -> None:
    """Discover and import all generator modules from a package.

    This triggers the @register decorators in each module.

    Args:
        package_name: Dotted package name to scan for generator modules.
    """
    try:
        package = importlib.import_module(package_name)
    except ImportError:
        # Try with full path from current module
        full_name = f"scripts.{package_name}"
        try:
            package = importlib.import_module(full_name)
        except ImportError:
            # Fallback: try relative import
            from . import generators as package

    if hasattr(package, "__path__"):
        for _importer, modname, _ispkg in pkgutil.iter_modules(package.__path__):
            full_modname = f"{package.__name__}.{modname}"
            importlib.import_module(full_modname)


def list_generators() -> list[dict[str, Any]]:
    """List all registered generators with their metadata.

    Returns:
        List of dicts with generator info for display.
    """
    return [
        {
            "name": g.__name__,
            "tags": [t.name for t in g.tags],
            "priority": g.priority,
            "doc": g.__doc__.strip().split("\n")[0] if g.__doc__ else "",
        }
        for g in sorted(_generators, key=lambda x: x.priority)
    ]


def clear_registry() -> None:
    """Clear all registered generators. Useful for testing."""
    _generators.clear()
