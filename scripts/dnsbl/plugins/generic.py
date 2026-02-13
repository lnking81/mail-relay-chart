"""
Generic DNSBL plugin using declarative service definitions.

Single plugin that handles ALL DNSBLs using config from services.py.
"""

from .base import DnsblPlugin, DnsblResult
from .services import (
    DnsblService,
    get_all_domain_zones,
    get_all_ip_zones,
    get_service,
)


class GenericDnsblPlugin(DnsblPlugin):
    """Universal DNSBL plugin using declarative service configs.

    Handles all DNSBLs based on configuration in services.py.
    No service-specific code - everything is driven by DnsblService dataclass.
    """

    priority = 100  # Only plugin, always matches

    @property
    def name(self) -> str:
        return "generic"

    @property
    def DEFAULT_IP_LISTS(self) -> list[str]:  # type: ignore[override]
        return get_all_ip_zones()

    @property
    def DEFAULT_DOMAIN_LISTS(self) -> list[str]:  # type: ignore[override]
        return get_all_domain_zones()

    def handles(self, dnsbl: str) -> bool:
        """Handle all zones."""
        return True

    def _get_service(self, zone: str) -> DnsblService:
        """Get service config, or create default."""
        service = get_service(zone)
        if service:
            return service
        # Unknown zone - use defaults
        return DnsblService(zone=zone)

    def _is_false_positive(self, return_code: str, service: DnsblService) -> bool:
        """Check if return code is a false positive for this service."""
        # Explicitly configured false positives
        if return_code in service.false_positives:
            return True
        # Must be in 127.x.x.x range
        if not return_code.startswith("127."):
            return True
        return False

    def _is_valid_listing(self, return_code: str, service: DnsblService) -> bool:
        """Check if return code indicates a valid listing."""
        # If valid_codes specified, must match
        if service.valid_codes is not None:
            return return_code in service.valid_codes
        # Default: any 127.x.x.x except false positives
        return return_code.startswith("127.")

    def _get_reason(self, return_code: str, service: DnsblService, query: str) -> str:
        """Get human-readable reason for listing."""
        # Check reason_map first
        if return_code in service.reason_map:
            return service.reason_map[return_code]
        # Try TXT record
        txt_reason = self.get_txt_reason(query)
        if txt_reason:
            return txt_reason
        # Fallback
        return f"Listed ({return_code})"

    def check_ip(self, ip: str, dnsbl: str, direct_query: bool = True) -> DnsblResult:
        """Check IP against DNSBL using service config."""
        service = self._get_service(dnsbl)

        query = f"{self.reverse_ip(ip)}.{dnsbl}"
        result = self._lookup(query, dnsbl, direct_query, service.nameserver)

        if result is None:
            return DnsblResult(target=ip, target_type="ip", dnsbl=dnsbl, listed=False)

        # Check false positive
        if self._is_false_positive(result, service):
            return DnsblResult(
                target=ip,
                target_type="ip",
                dnsbl=dnsbl,
                listed=False,
                return_code=result,
                reason="False positive",
            )

        # Check valid listing
        if not self._is_valid_listing(result, service):
            return DnsblResult(
                target=ip,
                target_type="ip",
                dnsbl=dnsbl,
                listed=False,
                return_code=result,
                reason=f"Invalid code: {result}",
            )

        return DnsblResult(
            target=ip,
            target_type="ip",
            dnsbl=dnsbl,
            listed=True,
            return_code=result,
            reason=self._get_reason(result, service, query),
        )

    def check_domain(
        self, domain: str, dnsbl: str, direct_query: bool = True
    ) -> DnsblResult:
        """Check domain against DNSBL using service config."""
        service = self._get_service(dnsbl)

        domain = domain.lower().strip(".")
        query = f"{domain}.{dnsbl}"
        result = self._lookup(query, dnsbl, direct_query, service.nameserver)

        if result is None:
            return DnsblResult(
                target=domain, target_type="domain", dnsbl=dnsbl, listed=False
            )

        # Check false positive
        if self._is_false_positive(result, service):
            return DnsblResult(
                target=domain,
                target_type="domain",
                dnsbl=dnsbl,
                listed=False,
                return_code=result,
                reason="False positive",
            )

        # Check valid listing
        if not self._is_valid_listing(result, service):
            return DnsblResult(
                target=domain,
                target_type="domain",
                dnsbl=dnsbl,
                listed=False,
                return_code=result,
                reason=f"Invalid code: {result}",
            )

        return DnsblResult(
            target=domain,
            target_type="domain",
            dnsbl=dnsbl,
            listed=True,
            return_code=result,
            reason=self._get_reason(result, service, query),
        )
