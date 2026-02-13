"""
DNSBL Plugin Registry

Auto-discovers and manages DNSBL plugins. Routes queries to appropriate
plugin based on DNSBL zone.
"""

import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Optional

from .base import DnsblPlugin, DnsblResult


class DnsblRegistry:
    """
    Registry for DNSBL plugins.

    Auto-discovers plugins from the dnsbl package and routes queries
    to the appropriate plugin based on DNSBL zone.
    """

    def __init__(self):
        """Initialize registry and discover plugins."""
        self.logger = logging.getLogger(__name__)
        self._plugins: list[DnsblPlugin] = []
        self._plugin_cache: dict[str, DnsblPlugin] = {}

        self._discover_plugins()

    def _discover_plugins(self) -> None:
        """Auto-discover and load all plugins from dnsbl package."""
        # Get the dnsbl package directory
        package_dir = Path(__file__).parent

        # Find all Python modules in the package
        for _, module_name, _ in pkgutil.iter_modules([str(package_dir)]):
            # Skip non-plugin modules
            if module_name in ("base", "registry", "services", "__init__"):
                continue

            try:
                # Import the module using the actual package name
                # This works whether imported as 'dnsbl' or 'scripts.dnsbl'
                package_name = __package__ or "dnsbl"
                module = importlib.import_module(
                    f".{module_name}", package=package_name
                )

                # Look for plugin classes (subclasses of DnsblPlugin)
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, DnsblPlugin)
                        and attr is not DnsblPlugin
                    ):
                        # Instantiate plugin
                        plugin = attr()
                        self._plugins.append(plugin)
                        self.logger.debug(
                            f"Loaded plugin: {plugin.name} (priority={plugin.priority})"
                        )

            except Exception as e:
                self.logger.warning(f"Failed to load plugin module {module_name}: {e}")

        # Sort plugins by priority (highest first)
        self._plugins.sort(key=lambda p: p.priority, reverse=True)

        self.logger.info(
            f"Loaded {len(self._plugins)} DNSBL plugins: "
            f"{', '.join(p.name for p in self._plugins)}"
        )

    def get_plugin(self, dnsbl: str) -> Optional[DnsblPlugin]:
        """
        Get the plugin that handles a specific DNSBL zone.

        Args:
            dnsbl: DNSBL zone (e.g., zen.spamhaus.org)

        Returns:
            Plugin instance or None if no plugin handles this zone
        """
        # Check cache first
        if dnsbl in self._plugin_cache:
            return self._plugin_cache[dnsbl]

        # Find plugin that handles this zone
        for plugin in self._plugins:
            if plugin.handles(dnsbl):
                self._plugin_cache[dnsbl] = plugin
                return plugin

        return None

    def check_ip(self, ip: str, dnsbl: str, direct_query: bool = False) -> DnsblResult:
        """
        Check an IP against a DNSBL using the appropriate plugin.

        Args:
            ip: IP address to check
            dnsbl: DNSBL zone
            direct_query: Query authoritative NS directly (bypasses public DNS)

        Returns:
            DnsblResult with check outcome
        """
        plugin = self.get_plugin(dnsbl)

        if plugin is None:
            self.logger.warning(f"No plugin found for DNSBL: {dnsbl}")
            return DnsblResult(
                target=ip,
                target_type="ip",
                dnsbl=dnsbl,
                listed=False,
                error=f"No plugin handles {dnsbl}",
            )

        try:
            return plugin.check_ip(ip, dnsbl, direct_query=direct_query)
        except Exception as e:
            self.logger.error(
                f"Plugin {plugin.name} failed checking {ip} @ {dnsbl}: {e}"
            )
            return DnsblResult(
                target=ip,
                target_type="ip",
                dnsbl=dnsbl,
                listed=False,
                error=str(e),
            )

    def check_domain(
        self, domain: str, dnsbl: str, direct_query: bool = False
    ) -> DnsblResult:
        """
        Check a domain against a DNSBL using the appropriate plugin.

        Args:
            domain: Domain to check
            dnsbl: DNSBL zone
            direct_query: Query authoritative NS directly (bypasses public DNS)

        Returns:
            DnsblResult with check outcome
        """
        plugin = self.get_plugin(dnsbl)

        if plugin is None:
            self.logger.warning(f"No plugin found for DNSBL: {dnsbl}")
            return DnsblResult(
                target=domain,
                target_type="domain",
                dnsbl=dnsbl,
                listed=False,
                error=f"No plugin handles {dnsbl}",
            )

        try:
            return plugin.check_domain(domain, dnsbl, direct_query=direct_query)
        except Exception as e:
            self.logger.error(
                f"Plugin {plugin.name} failed checking {domain} @ {dnsbl}: {e}"
            )
            return DnsblResult(
                target=domain,
                target_type="domain",
                dnsbl=dnsbl,
                listed=False,
                error=str(e),
            )

    def list_plugins(self) -> list[str]:
        """Return list of loaded plugin names."""
        return [p.name for p in self._plugins]

    def get_default_ip_lists(self) -> list[str]:
        """Collect default IP blacklists from all plugins."""
        lists: list[str] = []
        for plugin in self._plugins:
            lists.extend(plugin.DEFAULT_IP_LISTS)
        return lists

    def get_default_domain_lists(self) -> list[str]:
        """Collect default domain blacklists from all plugins."""
        lists: list[str] = []
        for plugin in self._plugins:
            lists.extend(plugin.DEFAULT_DOMAIN_LISTS)
        return lists
