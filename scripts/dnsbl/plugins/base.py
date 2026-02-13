"""
Base class for DNSBL plugins.

Each plugin handles querying specific DNSBL services with their own
query strategies (system DNS, direct authoritative NS, custom resolver, etc.)
"""

import logging
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import ClassVar, Optional

# Try to import dnspython
try:
    import dns.resolver

    HAS_DNSPYTHON = True
except ImportError:
    dns = None  # type: ignore[assignment]
    HAS_DNSPYTHON = False

logger = logging.getLogger(__name__)


@dataclass
class DnsblResult:
    """Result of a DNSBL check."""

    target: str  # IP address or domain being checked
    target_type: str  # "ip" or "domain"
    dnsbl: str  # DNSBL zone (e.g., zen.spamhaus.org)
    listed: bool  # Whether target is listed
    return_code: str = ""  # DNS response (e.g., 127.0.0.2)
    reason: str = ""  # Human-readable reason from TXT record
    check_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: str = ""  # Error message if check failed

    @property
    def ip(self) -> str:
        """Backward compatibility - return target for IP checks."""
        return self.target if self.target_type == "ip" else ""


class DnsblPlugin(ABC):
    """
    Abstract base class for DNSBL plugins.

    Each plugin:
    - Handles specific DNSBL zones (via `handles()` method)
    - Implements zone-specific query strategy
    - Returns standardized DnsblResult
    - Provides default zones for checking (DEFAULT_IP_LISTS, DEFAULT_DOMAIN_LISTS)

    DNS resolution is handled by the base class - plugins only need to
    implement handles(), _is_false_positive(), and optionally override
    check_ip/check_domain for custom behavior.
    """

    # Plugin priority (higher = checked first)
    priority: int = 0

    # Default lists to check - override in subclasses
    DEFAULT_IP_LISTS: ClassVar[list[str]] = []
    DEFAULT_DOMAIN_LISTS: ClassVar[list[str]] = []

    # Response codes that indicate false positive (e.g., public resolver block)
    # Override in subclasses
    FALSE_POSITIVE_CODES: ClassVar[set[str]] = set()

    # Shared NS cache across all plugin instances
    _ns_cache: ClassVar[dict[str, list[str]]] = {}

    def __init__(self) -> None:
        self.logger = logging.getLogger(f"{__name__}.{self.name}")

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable plugin name."""
        ...

    @abstractmethod
    def handles(self, dnsbl: str) -> bool:
        """Check if this plugin handles the given DNSBL zone."""
        ...

    # ─────────────────────────────────────────────────────────────────
    # DNS Resolution (shared by all plugins)
    # ─────────────────────────────────────────────────────────────────

    def _resolve_hostname(self, hostname: str) -> list[str]:
        """Resolve hostname to IP addresses."""
        if hostname in self._ns_cache:
            return self._ns_cache[hostname]

        try:
            # Use socket for simple A record lookup
            import socket

            ips = [socket.gethostbyname(hostname)]
            self._ns_cache[hostname] = ips
            return ips
        except Exception:
            return []

    def _get_authoritative_ns(self, zone: str) -> list[str]:
        """Get authoritative nameserver IPs for a DNSBL zone."""
        if zone in self._ns_cache:
            return self._ns_cache[zone]

        ns_ips: list[str] = []

        if not HAS_DNSPYTHON:
            return ns_ips

        try:
            ns_answers = dns.resolver.resolve(zone, "NS")  # pyright: ignore[reportOptionalMemberAccess]
            for ns in ns_answers:
                ns_name = str(ns).rstrip(".")
                try:
                    a_answers = dns.resolver.resolve(ns_name, "A")  # pyright: ignore[reportOptionalMemberAccess]
                    for a in a_answers:
                        ns_ips.append(str(a))
                except Exception:
                    continue

            if ns_ips:
                self._ns_cache[zone] = ns_ips
                self.logger.debug(f"Cached NS for {zone}: {ns_ips[:2]}...")

        except Exception as e:
            self.logger.debug(f"Failed to get NS for {zone}: {e}")

        return ns_ips

    def _direct_lookup(
        self, query: str, zone: str, nameserver: str | None = None
    ) -> Optional[str]:
        """Query authoritative NS directly.

        Args:
            query: DNS query (e.g., 67.231.199.138.zen.spamhaus.org)
            zone: DNSBL zone for NS lookup (e.g., zen.spamhaus.org)
            nameserver: Optional explicit nameserver hostname (e.g., a.gns.spamhaus.org)
        """
        # Get nameserver IPs
        if nameserver:
            # Explicit nameserver provided - resolve it
            ns_ips = self._resolve_hostname(nameserver)
            if not ns_ips:
                self.logger.debug(
                    f"Failed to resolve {nameserver}, falling back to zone NS"
                )
                ns_ips = self._get_authoritative_ns(zone)
        else:
            # Look up authoritative NS for zone
            ns_ips = self._get_authoritative_ns(zone)

        if not ns_ips:
            self.logger.debug(f"No NS for {zone}, falling back to system")
            return self._system_lookup(query)

        resolver = dns.resolver.Resolver()  # pyright: ignore[reportOptionalMemberAccess]
        resolver.nameservers = ns_ips[:3]
        resolver.timeout = 5
        resolver.lifetime = 10

        try:
            answers = resolver.resolve(query, "A")
            return str(answers[0])  # pyright: ignore[reportUnknownArgumentType]
        except (
            dns.resolver.NXDOMAIN,  # pyright: ignore[reportOptionalMemberAccess]
            dns.resolver.NoAnswer,  # pyright: ignore[reportOptionalMemberAccess]
            dns.resolver.NoNameservers,  # pyright: ignore[reportOptionalMemberAccess]
        ):
            return None
        except Exception as e:
            self.logger.debug(f"Direct lookup error for {query}: {e}")
            return None

    def _system_lookup(self, query: str) -> Optional[str]:
        """Query using system DNS resolver."""
        try:
            return socket.gethostbyname(query)
        except socket.gaierror:
            return None
        except socket.error as e:
            self.logger.debug(f"Socket error for {query}: {e}")
            return None

    def _lookup(
        self, query: str, zone: str, direct_query: bool, nameserver: str | None = None
    ) -> Optional[str]:
        """Perform DNS lookup using appropriate method.

        Args:
            query: Full DNS query (e.g., 67.231.199.138.zen.spamhaus.org)
            zone: DNSBL zone for NS lookup
            direct_query: Use authoritative NS instead of system DNS
            nameserver: Optional explicit nameserver hostname
        """
        if direct_query and HAS_DNSPYTHON:
            return self._direct_lookup(query, zone, nameserver)
        return self._system_lookup(query)

    def _is_false_positive(self, return_code: str) -> bool:
        """Check if return code indicates false positive (e.g., resolver block)."""
        return return_code in self.FALSE_POSITIVE_CODES

    # ─────────────────────────────────────────────────────────────────
    # Utility methods
    # ─────────────────────────────────────────────────────────────────

    def reverse_ip(self, ip: str) -> str:
        """Reverse IP address for DNSBL lookup."""
        parts = ip.split(".")
        return ".".join(reversed(parts))

    def get_txt_reason(self, query: str) -> str:
        """Try to get listing reason from TXT record."""
        if not HAS_DNSPYTHON:
            return ""
        try:
            answers = dns.resolver.resolve(query, "TXT")  # pyright: ignore[reportOptionalMemberAccess]
            reasons = [str(r).strip('"') for r in answers]
            return "; ".join(reasons)
        except Exception:
            return ""

    # ─────────────────────────────────────────────────────────────────
    # Check methods (can be overridden for custom behavior)
    # ─────────────────────────────────────────────────────────────────

    def check_ip(self, ip: str, dnsbl: str, direct_query: bool = False) -> DnsblResult:
        """Check an IP address against a DNSBL."""
        query = f"{self.reverse_ip(ip)}.{dnsbl}"
        result = self._lookup(query, dnsbl, direct_query)

        if result is None:
            return DnsblResult(target=ip, target_type="ip", dnsbl=dnsbl, listed=False)

        if self._is_false_positive(result):
            return DnsblResult(
                target=ip,
                target_type="ip",
                dnsbl=dnsbl,
                listed=False,
                return_code=result,
                reason="False positive (resolver block)",
            )

        return DnsblResult(
            target=ip,
            target_type="ip",
            dnsbl=dnsbl,
            listed=True,
            return_code=result,
            reason=self.get_txt_reason(query),
        )

    def check_domain(
        self, domain: str, dnsbl: str, direct_query: bool = False
    ) -> DnsblResult:
        """Check a domain against a DNSBL/URIBL."""
        domain = domain.lower().strip(".")
        query = f"{domain}.{dnsbl}"
        result = self._lookup(query, dnsbl, direct_query)

        if result is None:
            return DnsblResult(
                target=domain, target_type="domain", dnsbl=dnsbl, listed=False
            )

        if self._is_false_positive(result):
            return DnsblResult(
                target=domain,
                target_type="domain",
                dnsbl=dnsbl,
                listed=False,
                return_code=result,
                reason="False positive (resolver block)",
            )

        return DnsblResult(
            target=domain,
            target_type="domain",
            dnsbl=dnsbl,
            listed=True,
            return_code=result,
            reason=self.get_txt_reason(query),
        )
