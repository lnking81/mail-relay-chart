#!/usr/bin/env python3
"""
Blacklist Monitor - Monitors IPs and domains against DNSBL/URIBL lists.

Runs as a sidecar container, periodically checking the mail relay's IP
against DNS-based blacklists (DNSBL/RBL) and domains against URI blacklists
(URIBL/DBL). When a listing is found, sends alerts via email, webhooks,
and exposes Prometheus metrics.

Features:
- Checks 60+ IP blacklists (full MXToolbox coverage)
- Checks 12+ domain blacklists (Spamhaus DBL, SURBL, URIBL)
- Parallel DNS queries for fast checks
- Custom DNS resolver support (required for premium DNSBLs)
- Prometheus metrics endpoint for Alertmanager integration
- Email alerts via local SMTP relay
- Webhook notifications (Slack, PagerDuty, etc.)
- Cooldown to prevent alert spam

Environment Variables:
    BLACKLIST_INTERVAL          Check interval in seconds (default: 3600)
    BLACKLIST_METRICS_PORT      Prometheus port (default: 8095)
    BLACKLIST_DNS_SERVER        Custom DNS server (e.g., your own resolver)
    BLACKLIST_LISTS             Comma-separated IP DNSBLs (or use default 60+)
    BLACKLIST_DOMAIN_LISTS      Comma-separated domain DBLs (or use default 12)
    BLACKLIST_DOMAINS           Comma-separated domains to check
    BLACKLIST_CUSTOM_LISTS      Additional IP blacklists to add
    BLACKLIST_CUSTOM_DOMAIN_LISTS  Additional domain blacklists to add
    BLACKLIST_ALERT_ENABLED     Enable email alerts (true/false)
    BLACKLIST_ALERT_RECIPIENTS  Comma-separated email addresses
    BLACKLIST_ALERT_FROM        Sender address for alerts
    BLACKLIST_WEBHOOK_ENABLED   Enable webhook alerts (true/false)
    BLACKLIST_WEBHOOK_URL       Webhook endpoint URL
    BLACKLIST_STATIC_IP         Static IP to check (disables auto-detection)
    SHARED_DIR                  Shared volume for IP detection (default: /shared)

Note on DNS resolvers:
    Many premium DNSBLs (Spamhaus, URIBL, SURBL, SenderScore) block queries
    from public DNS resolvers (Google 8.8.8.8, Cloudflare 1.1.1.1).

    To use these services, you need either:
    1. Your own recursive DNS resolver (unbound, bind, CoreDNS)
    2. A paid subscription (Spamhaus DQS, URIBL Data Feed)

    Set BLACKLIST_DNS_SERVER to your resolver's IP to enable premium checks.

Usage:
    blacklist_monitor.py [--interval SECONDS] [--metrics-port PORT] [--dns-server IP]
"""

import argparse
import json
import logging
import os
import signal
import smtplib
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import requests

# Event for graceful shutdown
shutdown_event = threading.Event()

# Try to import dnspython for custom resolver support
try:
    import dns.resolver

    HAS_DNSPYTHON = True
except ImportError:
    HAS_DNSPYTHON = False

# === DNSBLs that work with public resolvers (FREE) ===
FREE_IP_BLACKLISTS = [
    # Barracuda - works with public resolvers
    "b.barracudacentral.org",
    # SpamCop - works with public resolvers
    "bl.spamcop.net",
    # SORBS - works with public resolvers
    "dnsbl.sorbs.net",
    # UCEProtect - works with public resolvers
    "dnsbl-1.uceprotect.net",
    "dnsbl-2.uceprotect.net",
    "dnsbl-3.uceprotect.net",
    # PSBL - works with public resolvers
    "psbl.surriel.com",
    # SpamRATS - works with public resolvers
    "dyna.spamrats.com",
    "noptr.spamrats.com",
    "spam.spamrats.com",
    # Mailspike - works with public resolvers
    "bl.mailspike.net",
    "z.mailspike.net",
    # Blocklist.de - works with public resolvers
    "bl.blocklist.de",
    # Backscatterer - works with public resolvers
    "ips.backscatterer.org",
    # CYMRU Bogons - works with public resolvers
    "bogons.cymru.com",
    # SpamEatingMonkey - works with public resolvers
    "backscatter.spameatingmonkey.net",
    "bl.spameatingmonkey.net",
    # Other free lists
    "all.s5h.net",
    "rbl.interserver.net",
    "dnsbl.zapbl.net",
    "db.wpbl.info",
    "truncate.gbudb.net",
    "bl.nordspam.com",
    "bl.suomispam.net",
    "drone.abuse.ch",
]

# === DNSBLs that REQUIRE own resolver or subscription (PREMIUM) ===
PREMIUM_IP_BLACKLISTS = [
    # Spamhaus - blocks public resolvers, needs DQS subscription or own resolver
    "zen.spamhaus.org",
    "sbl.spamhaus.org",
    "xbl.spamhaus.org",
    "pbl.spamhaus.org",
    # SenderScore - requires subscription
    "score.senderscore.com",
    # Abusix - may require registration
    "combined.mail.abusix.zone",
    "dblack.mail.abusix.zone",
    "exploit.mail.abusix.zone",
    # Others that may block public resolvers
    "bl.0spam.org",
    "hostkarma.junkemailfilter.com",
    "dnsbl.inps.de",
    "dnsbl24.inps.de",
]

# === Domain blacklists that work with public resolvers (FREE) ===
FREE_DOMAIN_BLACKLISTS = [
    "rhsbl.sorbs.net",
    "dbl.nordspam.com",
    "dbl.suomispam.net",
    "rhsbl.zapbl.net",
    "fresh.spameatingmonkey.net",
    "fresh15.spameatingmonkey.net",
]

# === Domain blacklists that REQUIRE own resolver or subscription (PREMIUM) ===
PREMIUM_DOMAIN_BLACKLISTS = [
    # Spamhaus DBL - blocks public resolvers
    "dbl.spamhaus.org",
    # SURBL - blocks public resolvers
    "multi.surbl.org",
    # URIBL - blocks public resolvers
    "multi.uribl.com",
    "black.uribl.com",
    "grey.uribl.com",
    # SpamCop DBL
    "dbl.abuseat.org",
]

# Combined lists (for backward compatibility)
DEFAULT_IP_BLACKLISTS = FREE_IP_BLACKLISTS + PREMIUM_IP_BLACKLISTS
DEFAULT_DOMAIN_BLACKLISTS = FREE_DOMAIN_BLACKLISTS + PREMIUM_DOMAIN_BLACKLISTS

# Backward compatibility alias
DEFAULT_BLACKLISTS = DEFAULT_IP_BLACKLISTS


@dataclass
class BlacklistConfig:
    """Configuration for blacklist monitoring"""

    interval: int = 3600  # Check interval in seconds
    metrics_port: int = 8095

    # Custom DNS server (required for premium DNSBLs)
    dns_server: str = ""

    # Use only free lists (works with public resolvers)
    free_only: bool = False

    # IP-based DNSBL lists to check
    lists: list[str] = field(default_factory=lambda: DEFAULT_IP_BLACKLISTS.copy())

    # Domain-based blacklists (URIBL/DBL)
    domain_lists: list[str] = field(
        default_factory=lambda: DEFAULT_DOMAIN_BLACKLISTS.copy()
    )

    # Domains to check (from mail.domains)
    domains: list[str] = field(default_factory=list)

    # Alert configuration
    alert_enabled: bool = False
    alert_recipients: list[str] = field(default_factory=list)
    alert_from: str = ""
    alert_subject_prefix: str = "[BLACKLIST ALERT]"
    alert_cooldown_hours: int = 24
    alert_smtp_host: str = "localhost"
    alert_smtp_port: int = 25

    # Webhook configuration
    webhook_enabled: bool = False
    webhook_url: str = ""
    webhook_timeout: int = 5

    # Shared directory for IP detection
    shared_dir: str = "/shared"

    @classmethod
    def from_env(cls) -> "BlacklistConfig":
        """Create config from environment variables"""
        # IP blacklists
        lists = os.environ.get("BLACKLIST_LISTS", "")
        if lists:
            lists = [l.strip() for l in lists.split(",") if l.strip()]
        else:
            lists = DEFAULT_IP_BLACKLISTS.copy()

        custom_lists = os.environ.get("BLACKLIST_CUSTOM_LISTS", "")
        if custom_lists:
            lists.extend([l.strip() for l in custom_lists.split(",") if l.strip()])

        # Domain blacklists
        domain_lists = os.environ.get("BLACKLIST_DOMAIN_LISTS", "")
        if domain_lists:
            domain_lists = [l.strip() for l in domain_lists.split(",") if l.strip()]
        else:
            domain_lists = DEFAULT_DOMAIN_BLACKLISTS.copy()

        custom_domain_lists = os.environ.get("BLACKLIST_CUSTOM_DOMAIN_LISTS", "")
        if custom_domain_lists:
            domain_lists.extend(
                [l.strip() for l in custom_domain_lists.split(",") if l.strip()]
            )

        # Domains to check
        domains = os.environ.get("BLACKLIST_DOMAINS", "")
        if domains:
            domains = [d.strip() for d in domains.split(",") if d.strip()]
        else:
            domains = []

        recipients = os.environ.get("BLACKLIST_ALERT_RECIPIENTS", "")
        if recipients:
            recipients = [r.strip() for r in recipients.split(",") if r.strip()]
        else:
            recipients = []

        return cls(
            interval=int(os.environ.get("BLACKLIST_INTERVAL", "3600")),
            metrics_port=int(os.environ.get("BLACKLIST_METRICS_PORT", "8095")),
            dns_server=os.environ.get("BLACKLIST_DNS_SERVER", ""),
            free_only=os.environ.get("BLACKLIST_FREE_ONLY", "false").lower() == "true",
            lists=lists,
            domain_lists=domain_lists,
            domains=domains,
            alert_enabled=os.environ.get("BLACKLIST_ALERT_ENABLED", "false").lower()
            == "true",
            alert_recipients=recipients,
            alert_from=os.environ.get("BLACKLIST_ALERT_FROM", ""),
            alert_subject_prefix=os.environ.get(
                "BLACKLIST_ALERT_SUBJECT_PREFIX", "[BLACKLIST ALERT]"
            ),
            alert_cooldown_hours=int(
                os.environ.get("BLACKLIST_ALERT_COOLDOWN_HOURS", "24")
            ),
            alert_smtp_host=os.environ.get("BLACKLIST_ALERT_SMTP_HOST", "localhost"),
            alert_smtp_port=int(os.environ.get("BLACKLIST_ALERT_SMTP_PORT", "25")),
            webhook_enabled=os.environ.get("BLACKLIST_WEBHOOK_ENABLED", "false").lower()
            == "true",
            webhook_url=os.environ.get("BLACKLIST_WEBHOOK_URL", ""),
            webhook_timeout=int(os.environ.get("BLACKLIST_WEBHOOK_TIMEOUT", "5")),
            shared_dir=os.environ.get("SHARED_DIR", "/shared"),
        )


@dataclass
class BlacklistResult:
    """Result of a DNSBL check"""

    target: str  # IP address or domain being checked
    target_type: str  # "ip" or "domain"
    dnsbl: str
    listed: bool
    return_code: str = ""
    reason: str = ""
    check_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def ip(self) -> str:
        """Backward compatibility - return target for IP checks"""
        return self.target if self.target_type == "ip" else ""


class BlacklistChecker:
    """Checks IPs and domains against DNSBL/URIBL lists"""

    def __init__(self, config: BlacklistConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)

        # Setup custom DNS resolver if specified
        self.resolver = None
        if config.dns_server and HAS_DNSPYTHON:
            self.resolver = dns.resolver.Resolver()
            self.resolver.nameservers = [config.dns_server]
            self.resolver.timeout = 5
            self.resolver.lifetime = 10
            self.logger.info(f"Using custom DNS resolver: {config.dns_server}")
        elif config.dns_server and not HAS_DNSPYTHON:
            self.logger.warning(
                "Custom DNS server specified but dnspython not installed. "
                "Install with: pip install dnspython"
            )

        # Return codes that indicate "not authorized" or "public resolver blocked"
        # These are NOT real listings, just access denials
        self.false_positive_codes = {
            # Spamhaus: "You are using a public/open DNS resolver"
            "127.255.255.254",
            # Spamhaus: test record / SenderScore: not authorized
            "127.255.255.255",
            # Some services return 127.0.0.1 for public resolver block
            # But URIBL uses it for real listings, so we handle per-service
        }

        # Services that return 127.0.0.1 for "not authorized" (not real listing)
        self.public_resolver_block_services = {
            "multi.surbl.org",
            "multi.uribl.com",
            "black.uribl.com",
            "grey.uribl.com",
        }

    def _dns_lookup(self, query: str) -> Optional[str]:
        """Perform DNS A record lookup, using custom resolver if configured"""
        if self.resolver:
            try:
                answers = self.resolver.resolve(query, "A")
                return str(answers[0])
            except (
                dns.resolver.NXDOMAIN,
                dns.resolver.NoAnswer,
                dns.resolver.NoNameservers,
            ):
                return None
            except Exception as e:
                self.logger.debug(f"DNS lookup error for {query}: {e}")
                return None
        else:
            try:
                return socket.gethostbyname(query)
            except socket.gaierror:
                return None

    def _is_false_positive(self, return_code: str, dnsbl: str) -> bool:
        """Check if the return code indicates a false positive (not a real listing)"""
        if return_code in self.false_positive_codes:
            return True

        # URIBL/SURBL return 127.0.0.1 when queried via public resolver
        if return_code == "127.0.0.1" and dnsbl in self.public_resolver_block_services:
            return True

        return False

    def reverse_ip(self, ip: str) -> str:
        """Reverse IP address for DNSBL lookup"""
        parts = ip.split(".")
        return ".".join(reversed(parts))

    def check_ip(self, ip: str, dnsbl: str) -> BlacklistResult:
        """Check a single IP against a single DNSBL"""
        query = f"{self.reverse_ip(ip)}.{dnsbl}"

        result = self._dns_lookup(query)

        if result is None:
            # NXDOMAIN = not listed
            return BlacklistResult(
                target=ip,
                target_type="ip",
                dnsbl=dnsbl,
                listed=False,
            )

        # Filter out false positives (public resolver blocks)
        if self._is_false_positive(result, dnsbl):
            self.logger.debug(
                f"Ignoring false positive from {dnsbl}: {result} (public resolver block)"
            )
            return BlacklistResult(
                target=ip,
                target_type="ip",
                dnsbl=dnsbl,
                listed=False,
                return_code=result,
                reason="Public resolver blocked (not a real listing)",
            )

        # Real listing
        return BlacklistResult(
            target=ip,
            target_type="ip",
            dnsbl=dnsbl,
            listed=True,
            return_code=result,
            reason=self._get_reason(query),
        )

    def check_domain(self, domain: str, dnsbl: str) -> BlacklistResult:
        """Check a single domain against a URIBL/DBL"""
        # Domain blacklists use direct format: domain.dnsbl
        query = f"{domain}.{dnsbl}"

        result = self._dns_lookup(query)

        if result is None:
            # NXDOMAIN = not listed
            return BlacklistResult(
                target=domain,
                target_type="domain",
                dnsbl=dnsbl,
                listed=False,
            )

        # Filter out false positives (public resolver blocks)
        if self._is_false_positive(result, dnsbl):
            self.logger.debug(
                f"Ignoring false positive from {dnsbl}: {result} (public resolver block)"
            )
            return BlacklistResult(
                target=domain,
                target_type="domain",
                dnsbl=dnsbl,
                listed=False,
                return_code=result,
                reason="Public resolver blocked (not a real listing)",
            )

        # Real listing
        return BlacklistResult(
            target=domain,
            target_type="domain",
            dnsbl=dnsbl,
            listed=True,
            return_code=result,
            reason=self._get_reason(query),
        )

    # Backward compatibility
    def check_single(self, ip: str, dnsbl: str) -> BlacklistResult:
        """Check a single IP against a single DNSBL (backward compat)"""
        return self.check_ip(ip, dnsbl)

    def _get_reason(self, query: str) -> str:
        """Try to get listing reason from TXT record"""
        try:
            import dns.resolver

            answers = dns.resolver.resolve(query, "TXT")
            reasons = [str(r).strip('"') for r in answers]
            return "; ".join(reasons)
        except Exception:
            return ""

    def check_all_ips(self, ip: str) -> list[BlacklistResult]:
        """Check IP against all configured IP DNSBLs in parallel"""
        results = []

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {
                executor.submit(self.check_ip, ip, dnsbl): dnsbl
                for dnsbl in self.config.lists
            }

            for future in as_completed(futures):
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    dnsbl = futures[future]
                    self.logger.warning(f"Failed to check {dnsbl}: {e}")

        return results

    def check_all_domains(self, domains: list[str]) -> list[BlacklistResult]:
        """Check domains against all configured domain blacklists in parallel"""
        results = []

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {}
            for domain in domains:
                for dnsbl in self.config.domain_lists:
                    future = executor.submit(self.check_domain, domain, dnsbl)
                    futures[future] = (domain, dnsbl)

            for future in as_completed(futures):
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    domain, dnsbl = futures[future]
                    self.logger.warning(
                        f"Failed to check domain {domain} against {dnsbl}: {e}"
                    )

        return results

    def check_all(self, ip: str) -> list[BlacklistResult]:
        """Check IP against all configured DNSBLs in parallel (backward compat)"""
        return self.check_all_ips(ip)


class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP handler for Prometheus metrics endpoint"""

    # Class-level state shared across requests
    ip_results: list[BlacklistResult] = []
    domain_results: list[BlacklistResult] = []
    check_count: int = 0
    listing_events: dict[str, int] = {}  # "type:target:dnsbl" -> count

    # Backward compat
    results: list[BlacklistResult] = []

    def log_message(self, format, *args):
        """Suppress default HTTP logging"""
        pass

    def handle_one_request(self):
        """Handle a single HTTP request, suppressing connection errors.

        Kubernetes health probes and load balancers may send incomplete
        requests or disconnect early, causing noisy tracebacks. We catch
        these expected conditions to keep logs clean.
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            # Client disconnected - expected for health probes
            pass

    def do_GET(self):
        if self.path == "/metrics":
            self.send_metrics()
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def send_metrics(self):
        """Generate Prometheus metrics"""
        lines = []

        # IP blacklist status
        lines.append(
            "# HELP mail_relay_blacklist_status IP blacklist status (1=listed, 0=clean)"
        )
        lines.append("# TYPE mail_relay_blacklist_status gauge")

        for result in self.ip_results:
            status = 1 if result.listed else 0
            lines.append(
                f'mail_relay_blacklist_status{{ip="{result.target}",list="{result.dnsbl}"}} {status}'
            )

        # Domain blacklist status
        lines.append("")
        lines.append(
            "# HELP mail_relay_domain_blacklist_status Domain blacklist status (1=listed, 0=clean)"
        )
        lines.append("# TYPE mail_relay_domain_blacklist_status gauge")

        for result in self.domain_results:
            status = 1 if result.listed else 0
            lines.append(
                f'mail_relay_domain_blacklist_status{{domain="{result.target}",list="{result.dnsbl}"}} {status}'
            )

        # Help and type for checks total
        lines.append("")
        lines.append(
            "# HELP mail_relay_blacklist_checks_total Total number of blacklist checks performed"
        )
        lines.append("# TYPE mail_relay_blacklist_checks_total counter")
        lines.append(f"mail_relay_blacklist_checks_total {self.check_count}")

        # Help and type for listing events
        lines.append("")
        lines.append(
            "# HELP mail_relay_blacklist_listed_total Total times target was found on a blacklist"
        )
        lines.append("# TYPE mail_relay_blacklist_listed_total counter")

        for key, count in self.listing_events.items():
            parts = key.split(":", 2)
            if len(parts) == 3:
                target_type, target, dnsbl = parts
                if target_type == "ip":
                    lines.append(
                        f'mail_relay_blacklist_listed_total{{ip="{target}",list="{dnsbl}"}} {count}'
                    )
                else:
                    lines.append(
                        f'mail_relay_blacklist_listed_total{{domain="{target}",list="{dnsbl}"}} {count}'
                    )

        # Summary counts
        lines.append("")
        lines.append(
            "# HELP mail_relay_blacklist_ip_listed_count Number of IP blacklists where IP is listed"
        )
        lines.append("# TYPE mail_relay_blacklist_ip_listed_count gauge")
        ip_listed = sum(1 for r in self.ip_results if r.listed)
        if self.ip_results:
            lines.append(
                f'mail_relay_blacklist_ip_listed_count{{ip="{self.ip_results[0].target}"}} {ip_listed}'
            )

        lines.append("")
        lines.append(
            "# HELP mail_relay_blacklist_domain_listed_count Number of domain blacklists where domain is listed"
        )
        lines.append("# TYPE mail_relay_blacklist_domain_listed_count gauge")
        domains_seen = set()
        for result in self.domain_results:
            if result.target not in domains_seen:
                domain_listed = sum(
                    1
                    for r in self.domain_results
                    if r.target == result.target and r.listed
                )
                lines.append(
                    f'mail_relay_blacklist_domain_listed_count{{domain="{result.target}"}} {domain_listed}'
                )
                domains_seen.add(result.target)

        # Last check timestamp
        lines.append("")
        lines.append(
            "# HELP mail_relay_blacklist_last_check_timestamp Unix timestamp of last check"
        )
        lines.append("# TYPE mail_relay_blacklist_last_check_timestamp gauge")
        all_results = self.ip_results + self.domain_results
        if all_results:
            ts = int(all_results[0].check_time.timestamp())
            lines.append(f"mail_relay_blacklist_last_check_timestamp {ts}")

        content = "\n".join(lines) + "\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode())


class AlertManager:
    """Manages email and webhook alerts"""

    def __init__(self, config: BlacklistConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.cooldown_cache: dict[
            str, datetime
        ] = {}  # "type:target:dnsbl" -> last alert time

    def should_alert(self, result: BlacklistResult) -> bool:
        """Check if we should send an alert (respecting cooldown)"""
        key = f"{result.target_type}:{result.target}:{result.dnsbl}"
        last_alert = self.cooldown_cache.get(key)

        if last_alert is None:
            return True

        cooldown = timedelta(hours=self.config.alert_cooldown_hours)
        return datetime.now(timezone.utc) - last_alert > cooldown

    def mark_alerted(self, result: BlacklistResult):
        """Mark that we've sent an alert for this target/DNSBL combo"""
        key = f"{result.target_type}:{result.target}:{result.dnsbl}"
        self.cooldown_cache[key] = datetime.now(timezone.utc)

    def send_email_alert(
        self,
        ip_results: list[BlacklistResult],
        domain_results: list[BlacklistResult] = None,
    ):
        """Send email alert for listed IPs and domains"""
        if not self.config.alert_enabled or not self.config.alert_recipients:
            return

        domain_results = domain_results or []

        # Filter by cooldown
        ip_listed = [r for r in ip_results if r.listed and self.should_alert(r)]
        domain_listed = [r for r in domain_results if r.listed and self.should_alert(r)]

        if not ip_listed and not domain_listed:
            return

        total = len(ip_listed) + len(domain_listed)
        subject = f"{self.config.alert_subject_prefix} Found on {total} blacklist(s)"

        body_lines = []

        if ip_listed:
            body_lines.extend(
                [
                    "=== IP BLACKLIST ALERTS ===",
                    "",
                ]
            )
            for r in ip_listed:
                body_lines.append(f"  IP: {r.target}")
                body_lines.append(f"  Blacklist: {r.dnsbl}")
                body_lines.append(f"  Return Code: {r.return_code}")
                if r.reason:
                    body_lines.append(f"  Reason: {r.reason}")
                body_lines.append("")

        if domain_listed:
            body_lines.extend(
                [
                    "=== DOMAIN BLACKLIST ALERTS ===",
                    "",
                ]
            )
            for r in domain_listed:
                body_lines.append(f"  Domain: {r.target}")
                body_lines.append(f"  Blacklist: {r.dnsbl}")
                body_lines.append(f"  Return Code: {r.return_code}")
                if r.reason:
                    body_lines.append(f"  Reason: {r.reason}")
                body_lines.append("")

        body_lines.extend(
            [
                "=== ACTION REQUIRED ===",
                "",
                "For IP delisting:",
                "  1. Check https://mxtoolbox.com/blacklists.aspx",
                "  2. Review mail server logs for potential abuse",
                "  3. Submit delisting requests to RBL operators",
                "",
                "For domain delisting:",
                "  1. Check https://mxtoolbox.com/domain/",
                "  2. Review sending practices and content",
                "  3. Contact domain blacklist operators",
                "",
                f"Generated at {datetime.now(timezone.utc).isoformat()}Z by mail-relay blacklist monitor",
            ]
        )

        body = "\n".join(body_lines)

        try:
            msg = MIMEMultipart()
            msg["Subject"] = subject
            msg["From"] = self.config.alert_from
            msg["To"] = ", ".join(self.config.alert_recipients)
            msg.attach(MIMEText(body, "plain"))

            with smtplib.SMTP(
                self.config.alert_smtp_host, self.config.alert_smtp_port
            ) as server:
                server.send_message(msg)

            for r in ip_listed + domain_listed:
                self.mark_alerted(r)

            self.logger.info(f"Sent email alert to {self.config.alert_recipients}")

        except Exception as e:
            self.logger.error(f"Failed to send email alert: {e}")

    def send_webhook_alert(
        self,
        ip_results: list[BlacklistResult],
        domain_results: list[BlacklistResult] = None,
    ):
        """Send webhook notification for listed IPs and domains"""
        if not self.config.webhook_enabled or not self.config.webhook_url:
            return

        domain_results = domain_results or []

        ip_listed = [r for r in ip_results if r.listed]
        domain_listed = [r for r in domain_results if r.listed]

        if not ip_listed and not domain_listed:
            return

        payload = {
            "event": "blacklist_alert",
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "ip_listings": [
                {
                    "ip": r.target,
                    "blacklist": r.dnsbl,
                    "return_code": r.return_code,
                    "reason": r.reason,
                }
                for r in ip_listed
            ],
            "domain_listings": [
                {
                    "domain": r.target,
                    "blacklist": r.dnsbl,
                    "return_code": r.return_code,
                    "reason": r.reason,
                }
                for r in domain_listed
            ],
        }

        try:
            response = requests.post(
                self.config.webhook_url,
                json=payload,
                timeout=self.config.webhook_timeout,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            self.logger.info(f"Sent webhook alert to {self.config.webhook_url}")
        except Exception as e:
            self.logger.error(f"Failed to send webhook alert: {e}")


def signal_handler(signum, frame):
    """Handle termination signals"""
    logging.info(f"Received signal {signum}, initiating shutdown...")
    shutdown_event.set()


def setup_logging(verbose: bool = False):
    """Configure logging"""
    level = logging.DEBUG if verbose else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def get_current_ip(shared_dir: Path, config: BlacklistConfig) -> Optional[str]:
    """
    Get current IP from shared volume or detect via external API.

    Priority:
    1. dns-state.json (from dns-watcher)
    2. current-ip file (legacy)
    3. Static IP from env (BLACKLIST_STATIC_IP)
    4. Auto-detect via external API
    """
    # Try new format first
    state_file = shared_dir / "dns-state.json"
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text())
            ip = data.get("incoming_ip") or data.get("outbound_ip")
            if ip:
                return ip
        except (json.JSONDecodeError, KeyError):
            pass

    # Fallback to legacy format
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
    external_apis = [
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
        "https://api.ipify.org",
    ]

    for api in external_apis:
        try:
            response = requests.get(api, timeout=5)
            if response.status_code == 200:
                ip = response.text.strip()
                if ip:
                    logging.getLogger(__name__).info(
                        f"Auto-detected IP via {api}: {ip}"
                    )
                    return ip
        except Exception:
            continue

    return None


def start_metrics_server(port: int) -> HTTPServer:
    """Start Prometheus metrics HTTP server"""
    server = HTTPServer(("0.0.0.0", port), MetricsHandler)
    server.timeout = 1  # Allow checking shutdown_event

    thread = threading.Thread(target=lambda: run_server(server), daemon=True)
    thread.start()

    return server


def run_server(server: HTTPServer):
    """Run HTTP server until shutdown"""
    while not shutdown_event.is_set():
        server.handle_request()


def main():
    parser = argparse.ArgumentParser(description="Blacklist Monitor for Mail Relay")

    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("BLACKLIST_INTERVAL", "3600")),
        help="Check interval in seconds (default: 3600)",
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=int(os.environ.get("BLACKLIST_METRICS_PORT", "8095")),
        help="Prometheus metrics port (default: 8095)",
    )
    parser.add_argument(
        "--shared-dir",
        type=str,
        default=os.environ.get("SHARED_DIR", "/shared"),
        help="Shared volume directory (default: /shared)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--check-once",
        action="store_true",
        help="Run a single check and exit (for testing)",
    )
    parser.add_argument(
        "--ips",
        type=str,
        default="",
        help="Comma-separated IPs to check (for testing)",
    )
    parser.add_argument(
        "--domains",
        type=str,
        default="",
        help="Comma-separated domains to check (for testing)",
    )
    parser.add_argument(
        "--dns-server",
        type=str,
        default=os.environ.get("BLACKLIST_DNS_SERVER", ""),
        help="Custom DNS server IP (required for premium DNSBLs like Spamhaus)",
    )
    parser.add_argument(
        "--free-only",
        action="store_true",
        help="Only check against free DNSBLs that work with public resolvers",
    )

    args = parser.parse_args()

    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    config = BlacklistConfig.from_env()
    config.interval = args.interval
    config.metrics_port = args.metrics_port
    config.shared_dir = args.shared_dir

    # Apply command-line overrides
    if args.dns_server:
        config.dns_server = args.dns_server

    if args.free_only or config.free_only:
        config.lists = FREE_IP_BLACKLISTS.copy()
        config.domain_lists = FREE_DOMAIN_BLACKLISTS.copy()
        config.free_only = True

    shared_dir = Path(config.shared_dir)

    logger.info("=" * 50)
    logger.info("Blacklist Monitor Starting")
    logger.info("=" * 50)
    logger.info(f"Check interval: {config.interval}s")
    logger.info(f"Metrics port: {config.metrics_port}")
    logger.info(f"DNS server: {config.dns_server or 'system default (public)'}")
    logger.info(f"Free-only mode: {config.free_only}")
    logger.info(f"IP DNSBLs: {len(config.lists)}")
    logger.info(f"Domain DBLs: {len(config.domain_lists)}")
    logger.info(f"Domains to check: {config.domains or '(from mail.domains)'}")
    logger.info(f"Email alerts: {config.alert_enabled}")
    logger.info(f"Webhook alerts: {config.webhook_enabled}")
    if not config.dns_server and not config.free_only:
        logger.warning(
            "Using public DNS resolver - premium DNSBLs (Spamhaus, URIBL) will be skipped. "
            "Use --dns-server or --free-only to suppress this warning."
        )
    logger.info("")

    # === CHECK-ONCE MODE (for testing) ===
    if args.check_once:
        checker = BlacklistChecker(config)

        # Override IPs and domains from args
        ips_to_check = (
            [ip.strip() for ip in args.ips.split(",") if ip.strip()] if args.ips else []
        )
        domains_to_check = (
            [d.strip() for d in args.domains.split(",") if d.strip()]
            if args.domains
            else config.domains
        )

        if not ips_to_check and not domains_to_check:
            logger.error("No IPs or domains to check. Use --ips and/or --domains")
            sys.exit(1)

        total_listed = 0

        # Check IPs
        for ip in ips_to_check:
            logger.info(f"\n{'=' * 60}")
            logger.info(f"Checking IP: {ip}")
            logger.info(f"{'=' * 60}")

            results = checker.check_all_ips(ip)
            listed = [r for r in results if r.listed]
            clean = len(results) - len(listed)

            if listed:
                logger.warning(
                    f"⚠️  LISTED on {len(listed)} blacklist(s), clean on {clean}"
                )
                for r in listed:
                    logger.warning(f"  ❌ {r.dnsbl}: {r.return_code} {r.reason or ''}")
                total_listed += len(listed)
            else:
                logger.info(f"✅ Clean on all {len(results)} blacklists")

        # Check domains
        for domain in domains_to_check:
            logger.info(f"\n{'=' * 60}")
            logger.info(f"Checking domain: {domain}")
            logger.info(f"{'=' * 60}")

            results = checker.check_all_domains([domain])
            listed = [r for r in results if r.listed]
            clean = len(results) - len(listed)

            if listed:
                logger.warning(
                    f"⚠️  LISTED on {len(listed)} blacklist(s), clean on {clean}"
                )
                for r in listed:
                    logger.warning(f"  ❌ {r.dnsbl}: {r.return_code} {r.reason or ''}")
                total_listed += len(listed)
            else:
                logger.info(f"✅ Clean on all {len(results)} blacklists")

        logger.info(f"\n{'=' * 60}")
        if total_listed > 0:
            logger.warning(f"SUMMARY: Found {total_listed} listing(s)")
            sys.exit(1)
        else:
            logger.info("SUMMARY: All targets clean!")
            sys.exit(0)

    # Start metrics server
    start_metrics_server(config.metrics_port)
    logger.info(f"Metrics server started on port {config.metrics_port}")

    # Initialize checker and alert manager
    checker = BlacklistChecker(config)
    alerts = AlertManager(config)

    # Wait for IP to be available
    logger.info("Detecting IP address...")
    current_ip = None
    attempts = 0
    max_attempts = 12  # 1 minute max wait
    while not shutdown_event.is_set() and attempts < max_attempts:
        current_ip = get_current_ip(shared_dir, config)
        if current_ip:
            break
        attempts += 1
        shutdown_event.wait(5)

    if shutdown_event.is_set():
        logger.info("Shutdown requested, exiting")
        return

    if not current_ip:
        logger.error("Could not detect IP address after 60 seconds")
        sys.exit(1)

    logger.info(f"Monitoring IP: {current_ip}")
    if config.domains:
        logger.info(f"Monitoring domains: {', '.join(config.domains)}")
    logger.info("")

    # Main monitoring loop
    check_count = 0
    while not shutdown_event.is_set():
        # Check for IP changes
        new_ip = get_current_ip(shared_dir, config)
        if new_ip and new_ip != current_ip:
            logger.info(f"IP changed: {current_ip} -> {new_ip}")
            current_ip = new_ip

        if not current_ip:
            logger.warning("No IP available for checking")
            shutdown_event.wait(config.interval)
            continue

        # === IP Blacklist Check ===
        logger.info(f"Checking IP {current_ip} against {len(config.lists)} DNSBLs...")
        ip_results = checker.check_all_ips(current_ip)
        check_count += 1

        ip_listed = [r for r in ip_results if r.listed]
        ip_clean = len(ip_results) - len(ip_listed)

        # === Domain Blacklist Check ===
        domain_results = []
        if config.domains and config.domain_lists:
            logger.info(
                f"Checking {len(config.domains)} domain(s) against {len(config.domain_lists)} DBLs..."
            )
            domain_results = checker.check_all_domains(config.domains)

        domain_listed = [r for r in domain_results if r.listed]
        domain_clean = len(domain_results) - len(domain_listed)

        # Update metrics
        MetricsHandler.ip_results = ip_results
        MetricsHandler.domain_results = domain_results
        MetricsHandler.results = ip_results  # Backward compat
        MetricsHandler.check_count = check_count

        for r in ip_listed:
            key = f"ip:{r.target}:{r.dnsbl}"
            MetricsHandler.listing_events[key] = (
                MetricsHandler.listing_events.get(key, 0) + 1
            )

        for r in domain_listed:
            key = f"domain:{r.target}:{r.dnsbl}"
            MetricsHandler.listing_events[key] = (
                MetricsHandler.listing_events.get(key, 0) + 1
            )

        # Log results
        total_listed = len(ip_listed) + len(domain_listed)

        if ip_listed:
            logger.warning(
                f"IP LISTED on {len(ip_listed)} blacklist(s), clean on {ip_clean}"
            )
            for r in ip_listed:
                logger.warning(f"  - {r.dnsbl}: {r.return_code} {r.reason or ''}")
        else:
            logger.info(f"IP clean on all {len(ip_results)} blacklists")

        if domain_results:
            if domain_listed:
                logger.warning(
                    f"DOMAIN(S) LISTED on {len(domain_listed)} blacklist(s), clean on {domain_clean}"
                )
                for r in domain_listed:
                    logger.warning(
                        f"  - {r.target} @ {r.dnsbl}: {r.return_code} {r.reason or ''}"
                    )
            else:
                logger.info(f"Domains clean on all {len(domain_results)} checks")

        # Send alerts if anything is listed
        if total_listed > 0:
            alerts.send_email_alert(ip_results, domain_results)
            alerts.send_webhook_alert(ip_results, domain_results)

        # Wait for next check
        shutdown_event.wait(config.interval)

    logger.info("Blacklist Monitor stopped")


if __name__ == "__main__":
    main()
