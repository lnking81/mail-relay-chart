"""Main blacklist monitoring loop."""

import json
import logging
import os
import threading
from pathlib import Path

import requests

from .alerts import AlertManager
from .checker import BlacklistChecker
from .config import BlacklistConfig
from .metrics import MetricsServer
from .models import BlacklistResult


class BlacklistMonitor:
    """Main blacklist monitoring service."""

    def __init__(self, config: BlacklistConfig, shutdown_event: threading.Event):
        self.config = config
        self.shutdown_event = shutdown_event
        self.logger = logging.getLogger(__name__)

        self.checker = BlacklistChecker(config)
        self.alerts = AlertManager(config)
        self.metrics = MetricsServer(config.metrics_port, shutdown_event)

        self._check_count = 0
        self._current_ip: str | None = None

    def start(self) -> None:
        """Start the monitoring service."""
        self.logger.info("=" * 50)
        self.logger.info("Blacklist Monitor Starting")
        self.logger.info("=" * 50)
        self.logger.info(f"Check interval: {self.config.interval}s")
        self.logger.info(f"Metrics port: {self.config.metrics_port}")
        self.logger.info(f"Direct query: {self.config.direct_query}")
        self.logger.info(f"IP DNSBLs: {len(self.config.lists)}")
        self.logger.info(f"Domain DBLs: {len(self.config.domain_lists)}")
        if self.config.domains:
            self.logger.info(f"Domains to check: {', '.join(self.config.domains)}")
        self.logger.info("")

        # Start metrics server
        self.metrics.start()
        self.logger.info(f"Metrics server started on port {self.config.metrics_port}")

        # Wait for IP
        self._current_ip = self._wait_for_ip()
        if not self._current_ip:
            return

        self.logger.info(f"Monitoring IP: {self._current_ip}")
        if self.config.domains:
            self.logger.info(f"Monitoring domains: {', '.join(self.config.domains)}")
        self.logger.info("")

        # Main loop
        self._run_loop()

        self.logger.info("Blacklist Monitor stopped")

    def _run_loop(self) -> None:
        """Main monitoring loop."""
        while not self.shutdown_event.is_set():
            # Check for IP changes
            new_ip = self._detect_ip()
            if new_ip and new_ip != self._current_ip:
                self.logger.info(f"IP changed: {self._current_ip} -> {new_ip}")
                self._current_ip = new_ip

            if not self._current_ip:
                self.logger.warning("No IP available for checking")
                self.shutdown_event.wait(self.config.interval)
                continue

            # Run checks
            ip_results, domain_results = self._run_check()

            # Update metrics
            self.metrics.update_results(ip_results, domain_results, self._check_count)

            # Send alerts
            self.alerts.send_alerts(ip_results, domain_results)

            # Wait for next check
            self.shutdown_event.wait(self.config.interval)

    def _run_check(self) -> tuple[list[BlacklistResult], list[BlacklistResult]]:
        """Run a single check cycle."""
        self._check_count += 1

        # Check IP
        self.logger.info(
            f"Checking IP {self._current_ip} against {len(self.config.lists)} DNSBLs..."
        )
        ip_results = self.checker.check_all_ips(self._current_ip or "")

        ip_listed = [r for r in ip_results if r.listed]
        ip_clean = len(ip_results) - len(ip_listed)

        if ip_listed:
            self.logger.warning(
                f"IP LISTED on {len(ip_listed)} blacklist(s), clean on {ip_clean}"
            )
            for r in ip_listed:
                self.logger.warning(f"  - {r.dnsbl}: {r.return_code} {r.reason or ''}")
        else:
            self.logger.info(f"IP clean on all {len(ip_results)} blacklists")

        # Check domains
        domain_results: list[BlacklistResult] = []
        if self.config.domains and self.config.domain_lists:
            self.logger.info(
                f"Checking {len(self.config.domains)} domain(s) against {len(self.config.domain_lists)} DBLs..."
            )
            domain_results = self.checker.check_all_domains(self.config.domains)

            domain_listed = [r for r in domain_results if r.listed]
            domain_clean = len(domain_results) - len(domain_listed)

            if domain_listed:
                self.logger.warning(
                    f"DOMAIN(S) LISTED on {len(domain_listed)} blacklist(s), clean on {domain_clean}"
                )
                for r in domain_listed:
                    self.logger.warning(
                        f"  - {r.target} @ {r.dnsbl}: {r.return_code} {r.reason or ''}"
                    )
            else:
                self.logger.info(f"Domains clean on all {len(domain_results)} checks")

        return ip_results, domain_results

    def _wait_for_ip(self, max_attempts: int = 12) -> str | None:
        """Wait for IP to become available."""
        self.logger.info("Detecting IP address...")
        attempts = 0

        while not self.shutdown_event.is_set() and attempts < max_attempts:
            ip = self._detect_ip()
            if ip:
                return ip
            attempts += 1
            self.shutdown_event.wait(5)

        if self.shutdown_event.is_set():
            self.logger.info("Shutdown requested, exiting")
        else:
            self.logger.error("Could not detect IP address after 60 seconds")

        return None

    def _detect_ip(self) -> str | None:
        """Detect current IP from shared volume or external API."""
        shared_dir = Path(self.config.shared_dir)

        # Try dns-state.json first
        state_file = shared_dir / "dns-state.json"
        if state_file.exists():
            try:
                data: dict[str, str] = json.loads(state_file.read_text())
                ip = data.get("outbound_ip") or data.get("incoming_ip") or ""
                if ip:
                    return ip
            except (json.JSONDecodeError, KeyError):
                pass

        # Legacy format
        ip_file = shared_dir / "current-ip"
        if ip_file.exists():
            ip = ip_file.read_text().strip()
            if ip:
                return ip

        # Static IP from environment
        static_ip = os.environ.get("BLACKLIST_STATIC_IP", "")
        if static_ip:
            return static_ip

        # Auto-detect via external API
        for api in [
            "https://ifconfig.me/ip",
            "https://icanhazip.com",
            "https://api.ipify.org",
        ]:
            try:
                response = requests.get(api, timeout=5)
                if response.status_code == 200:
                    ip = response.text.strip()
                    if ip:
                        self.logger.info(f"Auto-detected IP via {api}: {ip}")
                        return ip
            except Exception:
                continue

        return None

    def check_once(self, ips: list[str], domains: list[str]) -> int:
        """Run a single check and return exit code (for testing)."""
        total_listed = 0

        for ip in ips:
            self.logger.info(f"\n{'=' * 60}")
            self.logger.info(f"Checking IP: {ip}")
            self.logger.info(f"{'=' * 60}")

            results = self.checker.check_all_ips(ip)
            listed = [r for r in results if r.listed]
            clean = len(results) - len(listed)

            if listed:
                self.logger.warning(
                    f"⚠️  LISTED on {len(listed)} blacklist(s), clean on {clean}"
                )
                for r in listed:
                    self.logger.warning(
                        f"  ❌ {r.dnsbl}: {r.return_code} {r.reason or ''}"
                    )
                total_listed += len(listed)
            else:
                self.logger.info(f"✅ Clean on all {len(results)} blacklists")

        for domain in domains:
            self.logger.info(f"\n{'=' * 60}")
            self.logger.info(f"Checking domain: {domain}")
            self.logger.info(f"{'=' * 60}")

            results = self.checker.check_all_domains([domain])
            listed = [r for r in results if r.listed]
            clean = len(results) - len(listed)

            if listed:
                self.logger.warning(
                    f"⚠️  LISTED on {len(listed)} blacklist(s), clean on {clean}"
                )
                for r in listed:
                    self.logger.warning(
                        f"  ❌ {r.dnsbl}: {r.return_code} {r.reason or ''}"
                    )
                total_listed += len(listed)
            else:
                self.logger.info(f"✅ Clean on all {len(results)} blacklists")

        self.logger.info(f"\n{'=' * 60}")
        if total_listed > 0:
            self.logger.warning(f"SUMMARY: Found {total_listed} listing(s)")
            return 1
        else:
            self.logger.info("SUMMARY: All targets clean!")
            return 0
