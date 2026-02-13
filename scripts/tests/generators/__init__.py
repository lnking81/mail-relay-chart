"""
Test generators package.

Each module in this package contains TestGenerator implementations
for a specific test category.
"""

# Import all generators to trigger registration
from . import (  # noqa: F401
    auth,
    delivery,
    inbound,
    limits,
    outbound,
    relay,
    security,
    tls,
)

__all__ = [
    "auth",
    "delivery",
    "inbound",
    "limits",
    "outbound",
    "relay",
    "security",
    "tls",
]
