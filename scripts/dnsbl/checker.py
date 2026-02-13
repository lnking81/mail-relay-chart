"""Blacklist checker using DNSBL plugin system."""

import logging
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

from .config import BlacklistConfig
from .models import BlacklistResult
from .plugins import DnsblRegistry


class BlacklistChecker:
    """Checks IPs and domains against DNSBL/URIBL lists using plugin system."""

    def __init__(self, config: BlacklistConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.registry = DnsblRegistry()

        self.logger.info(
            f"DNSBL plugin system loaded: {', '.join(self.registry.list_plugins())}"
        )

        # Populate default lists from plugins if not specified
        if not self.config.lists:
            self.config.lists = self.registry.get_default_ip_lists()
            self.logger.info(
                f"Using {len(self.config.lists)} default IP blacklists from plugins"
            )

        if not self.config.domain_lists:
            self.config.domain_lists = self.registry.get_default_domain_lists()
            self.logger.info(
                f"Using {len(self.config.domain_lists)} default domain blacklists from plugins"
            )

    def check_ip(self, ip: str, dnsbl: str) -> BlacklistResult:
        """Check a single IP against a DNSBL."""
        result = self.registry.check_ip(
            ip, dnsbl, direct_query=self.config.direct_query
        )
        return BlacklistResult(
            target=ip,
            target_type="ip",
            dnsbl=dnsbl,
            listed=result.listed,
            return_code=result.return_code or "",
            reason=result.reason or "",
        )

    def check_domain(self, domain: str, dnsbl: str) -> BlacklistResult:
        """Check a single domain against a URIBL/DBL."""
        result = self.registry.check_domain(
            domain, dnsbl, direct_query=self.config.direct_query
        )
        return BlacklistResult(
            target=domain,
            target_type="domain",
            dnsbl=dnsbl,
            listed=result.listed,
            return_code=result.return_code or "",
            reason=result.reason or "",
        )

    def check_all_ips(self, ip: str) -> list[BlacklistResult]:
        """Check IP against all configured IP DNSBLs in parallel."""
        results: list[BlacklistResult] = []

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures: dict[Future[BlacklistResult], str] = {
                executor.submit(self.check_ip, ip, dnsbl): dnsbl
                for dnsbl in self.config.lists
            }

            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    dnsbl = futures[future]
                    self.logger.warning(f"Failed to check {dnsbl}: {e}")

        return results

    def check_all_domains(self, domains: list[str]) -> list[BlacklistResult]:
        """Check domains against all configured domain blacklists in parallel."""
        results: list[BlacklistResult] = []

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures: dict[Future[BlacklistResult], tuple[str, str]] = {}
            for domain in domains:
                for dnsbl in self.config.domain_lists:
                    future = executor.submit(self.check_domain, domain, dnsbl)
                    futures[future] = (domain, dnsbl)

            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    domain, dnsbl = futures[future]
                    self.logger.warning(f"Failed to check {domain} @ {dnsbl}: {e}")

        return results

    # Backward compatibility
    check_single = check_ip
    check_all = check_all_ips
