"""
Mail Relay Test Suite

Modular test framework for SMTP mail relay configuration validation.

Example usage:
    from tests import TestConfig, generate_tests, SmtpTestRunner, Tag

    # Load configuration
    config = TestConfig.load("test-values.yaml")

    # Generate tests (optionally filter by tags)
    tests = generate_tests(config, tags={Tag.OUTBOUND, Tag.RELAY})

    # Run tests
    runner = SmtpTestRunner(config, internal_server="localhost:2525")
    results = runner.run_tests(tests)

    # Check results
    summary = runner.get_summary()
    print(f"Passed: {summary['passed']}/{summary['total']}")
"""

# Core classes
# Import generators to trigger registration
from . import generators  # noqa: F401  # pyright: ignore[reportUnusedImport]
from .base import Tag, TestCase, TestGenerator, TestResult
from .config import TestConfig

# Registry functions
from .registry import (
    clear_registry,
    discover_generators,
    generate_tests,
    get_generators,
    list_generators,
    register,
)

# Runner
from .runner import SmtpTestRunner

__all__ = [
    # Core classes
    "Tag",
    "TestCase",
    "TestResult",
    "TestGenerator",
    "TestConfig",
    # Registry
    "register",
    "get_generators",
    "generate_tests",
    "discover_generators",
    "list_generators",
    "clear_registry",
    # Runner
    "SmtpTestRunner",
]

__version__ = "2.0.0"
