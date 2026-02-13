"""DNSBL monitoring package.

Public API:
    - BlacklistMonitor: Main monitoring service
    - BlacklistChecker: DNSBL checker using plugins
    - BlacklistConfig: Configuration dataclass
    - BlacklistResult: Check result model

Plugin API (for extending):
    - DnsblPlugin: Base class for DNSBL plugins
    - DnsblResult: Plugin result model
    - DnsblRegistry: Plugin registry
"""

from .alerts import AlertManager
from .checker import BlacklistChecker
from .config import BlacklistConfig
from .metrics import MetricsServer
from .models import BlacklistResult
from .monitor import BlacklistMonitor
from .plugins import DnsblPlugin, DnsblRegistry, DnsblResult

__all__ = [
    # Main API
    "BlacklistMonitor",
    "BlacklistChecker",
    "BlacklistConfig",
    "BlacklistResult",
    "AlertManager",
    "MetricsServer",
    # Plugin API
    "DnsblPlugin",
    "DnsblResult",
    "DnsblRegistry",
]
