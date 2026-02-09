#!/usr/bin/env python3
"""
DNS Manager - Main entry point for DNS record management.

This script handles DNS record creation and updates for mail relay.
Supports multiple DNS providers with ownership tracking.

Usage:
    dns_manager.py init     - Initialize DNS records
    dns_manager.py update   - Update DNS records
    dns_manager.py cleanup  - Remove owned DNS records
    dns_manager.py verify   - Verify DNS records exist
    dns_manager.py status   - Show current DNS status
"""

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent))

from dns import DNSRecord, RecordType, get_provider_from_env
from dns.base import DNSProvider
from utils.ip import IPDetector, IPDetectorConfig
from utils.k8s import KubernetesClient, KubernetesConfig


@dataclass
class MailConfig:
    """Mail relay configuration"""

    hostname: str  # FQDN for mail server
    domains: list[dict]  # List of domains with dkimSelector

    # DNS record options
    create_a: bool = True
    create_mx: bool = True
    create_spf: bool = True
    create_dkim: bool = True
    create_dmarc: bool = True

    # SPF/DMARC policies
    spf_policy: str = "~all"
    dmarc_policy: str = "none"
    dmarc_rua: str = ""

    # TTL
    ttl: int = 300

    @classmethod
    def from_env(cls) -> "MailConfig":
        """Create config from environment variables"""
        domains = []
        domains_json = os.environ.get("MAIL_DOMAINS", "")
        if domains_json:
            try:
                domains = json.loads(domains_json)
            except json.JSONDecodeError:
                # Fallback: comma-separated domain names
                for d in domains_json.split(","):
                    if d.strip():
                        domains.append({"name": d.strip(), "dkimSelector": "mail"})

        return cls(
            hostname=os.environ.get("MAIL_HOSTNAME", ""),
            domains=domains,
            create_a=os.environ.get("DNS_CREATE_A", "true").lower() == "true",
            create_mx=os.environ.get("DNS_CREATE_MX", "true").lower() == "true",
            create_spf=os.environ.get("DNS_CREATE_SPF", "true").lower() == "true",
            create_dkim=os.environ.get("DNS_CREATE_DKIM", "true").lower() == "true",
            create_dmarc=os.environ.get("DNS_CREATE_DMARC", "true").lower() == "true",
            spf_policy=os.environ.get("DNS_SPF_POLICY", "~all"),
            dmarc_policy=os.environ.get("DNS_DMARC_POLICY", "none"),
            dmarc_rua=os.environ.get("DNS_DMARC_RUA", ""),
            ttl=int(os.environ.get("DNS_TTL", "300")),
        )


class DNSManager:
    """
    Manages DNS records for mail relay.

    Creates and updates:
    - A record for mail hostname
    - MX record for each domain
    - SPF TXT record for each domain
    - DKIM TXT record for each domain
    - DMARC TXT record for each domain
    """

    def __init__(
        self,
        provider: DNSProvider,
        mail_config: MailConfig,
        k8s_client: KubernetesClient,
        ip_detector: IPDetector,
    ):
        self.provider = provider
        self.mail_config = mail_config
        self.k8s = k8s_client
        self.ip_detector = ip_detector
        self.logger = logging.getLogger(__name__)

        # Cache zone IDs
        self._zone_cache: dict[str, str] = {}

    def _get_zone_id(self, domain: str) -> Optional[str]:
        """Get zone ID with caching"""
        if domain not in self._zone_cache:
            zone_id = self.provider.get_zone_id(domain)
            if zone_id:
                self._zone_cache[domain] = zone_id
        return self._zone_cache.get(domain)

    def _extract_domain(self, fqdn: str) -> str:
        """Extract base domain from FQDN (e.g., mail.example.com -> example.com)"""
        parts = fqdn.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return fqdn

    def build_spf_record(self, ips: list[str]) -> str:
        """Build SPF record content"""
        # Sort IPs to ensure consistent record content regardless of detection order
        ip_parts = " ".join(f"ip4:{ip}" for ip in sorted(ips))
        return f"v=spf1 {ip_parts} {self.mail_config.spf_policy}"

    def build_dmarc_record(self, domain: str) -> str:
        """Build DMARC record content"""
        rua = self.mail_config.dmarc_rua or f"postmaster@{domain}"
        return f"v=DMARC1; p={self.mail_config.dmarc_policy}; rua=mailto:{rua}"

    def init_or_update(self, wait_for_lb: int = 300) -> bool:
        """
        Initialize or update all DNS records.

        Args:
            wait_for_lb: Seconds to wait for LoadBalancer IP

        Returns:
            True if all records were created/updated successfully
        """
        self.logger.info("=" * 60)
        self.logger.info("DNS Record Initialization")
        self.logger.info("=" * 60)

        # Detect IPs
        incoming_ip = self.ip_detector.get_incoming_ip(self.k8s, wait_for_lb)
        if not incoming_ip:
            self.logger.error("Could not detect incoming IP address")
            return False

        all_ips = self.ip_detector.get_all_ips(self.k8s, wait_for_lb)

        self.logger.info(f"Incoming IP:  {incoming_ip}")
        self.logger.info(f"All IPs:      {all_ips}")
        self.logger.info(f"Mail host:    {self.mail_config.hostname}")
        self.logger.info(
            f"Domains:      {[d['name'] for d in self.mail_config.domains]}"
        )
        self.logger.info(f"Owner ID:     {self.provider.owner_id}")
        self.logger.info("")

        success = True

        # A record for mail hostname
        if self.mail_config.create_a:
            success &= self._ensure_a_record(incoming_ip)

        # Per-domain records
        for domain_cfg in self.mail_config.domains:
            domain = domain_cfg["name"]
            selector = domain_cfg.get("dkimSelector", "mail")

            self.logger.info(f"\n--- Domain: {domain} (selector: {selector}) ---")

            zone_id = self._get_zone_id(domain)
            if not zone_id:
                self.logger.error(f"Could not find zone for {domain}")
                success = False
                continue

            if self.mail_config.create_mx:
                success &= self._ensure_mx_record(zone_id, domain)

            if self.mail_config.create_spf:
                success &= self._ensure_spf_record(zone_id, domain, all_ips)

            if self.mail_config.create_dkim:
                success &= self._ensure_dkim_record(zone_id, domain, selector)

            if self.mail_config.create_dmarc:
                success &= self._ensure_dmarc_record(zone_id, domain)

        self.logger.info("\n" + "=" * 60)
        if success:
            self.logger.info("DNS initialization completed successfully")
        else:
            self.logger.warning("DNS initialization completed with errors")
        self.logger.info("=" * 60)

        return success

    def _ensure_a_record(self, ip: str) -> bool:
        """Create/update A record for mail hostname"""
        hostname = self.mail_config.hostname
        domain = self._extract_domain(hostname)

        zone_id = self._get_zone_id(domain)
        if not zone_id:
            self.logger.error(f"Could not find zone for {domain}")
            return False

        record = DNSRecord(
            name=hostname,
            type=RecordType.A,
            content=ip,
            ttl=self.mail_config.ttl,
        )

        return self.provider.ensure_record(zone_id, record)

    def _ensure_mx_record(self, zone_id: str, domain: str) -> bool:
        """Create/update MX record"""
        record = DNSRecord(
            name=domain,
            type=RecordType.MX,
            content=self.mail_config.hostname,
            ttl=self.mail_config.ttl,
            priority=10,
        )

        return self.provider.ensure_record(zone_id, record)

    def _ensure_spf_record(self, zone_id: str, domain: str, ips: list[str]) -> bool:
        """Create/update SPF record"""
        record = DNSRecord(
            name=domain,
            type=RecordType.TXT,
            content=self.build_spf_record(ips),
            ttl=self.mail_config.ttl,
        )

        return self.provider.ensure_record(zone_id, record)

    def _ensure_dkim_record(self, zone_id: str, domain: str, selector: str) -> bool:
        """Create/update DKIM record"""
        # Get DKIM record from Kubernetes secret
        dkim_content = self.k8s.get_dkim_record(domain)
        if not dkim_content:
            self.logger.warning(f"DKIM secret not found for {domain}, skipping")
            return True  # Not a failure, just skip

        record = DNSRecord(
            name=f"{selector}._domainkey.{domain}",
            type=RecordType.TXT,
            content=dkim_content,
            ttl=self.mail_config.ttl,
        )

        return self.provider.ensure_record(zone_id, record)

    def _ensure_dmarc_record(self, zone_id: str, domain: str) -> bool:
        """Create/update DMARC record"""
        record = DNSRecord(
            name=f"_dmarc.{domain}",
            type=RecordType.TXT,
            content=self.build_dmarc_record(domain),
            ttl=self.mail_config.ttl,
        )

        return self.provider.ensure_record(zone_id, record)

    def cleanup(self) -> bool:
        """Remove all DNS records owned by this instance"""
        self.logger.info("Cleaning up owned DNS records...")

        success = True

        for domain_cfg in self.mail_config.domains:
            domain = domain_cfg["name"]
            zone_id = self._get_zone_id(domain)

            if not zone_id:
                continue

            owned = self.provider.list_owned_records(zone_id)
            self.logger.info(f"Found {len(owned)} owned records in {domain}")

            for record in owned:
                if not self.provider.delete_owned_record(
                    zone_id, record.name, record.type
                ):
                    success = False

        return success

    def verify(self, timeout: int = 600, interval: int = 10) -> bool:
        """
        Verify DNS records are propagated.

        Args:
            timeout: Maximum seconds to wait
            interval: Seconds between checks

        Returns:
            True if all required records are verified
        """
        import socket

        self.logger.info(f"Verifying DNS propagation (timeout: {timeout}s)...")

        start_time = time.time()

        while True:
            all_verified = True
            status = []

            # Check A record
            if self.mail_config.create_a:
                try:
                    socket.gethostbyname(self.mail_config.hostname)
                    status.append(f"A:{self.mail_config.hostname}:✓")
                except socket.gaierror:
                    status.append(f"A:{self.mail_config.hostname}:✗")
                    all_verified = False

            # Check DKIM records
            if self.mail_config.create_dkim:
                for domain_cfg in self.mail_config.domains:
                    domain = domain_cfg["name"]
                    selector = domain_cfg.get("dkimSelector", "mail")
                    dkim_name = f"{selector}._domainkey.{domain}"

                    try:
                        import dns.resolver

                        dns.resolver.resolve(dkim_name, "TXT")
                        status.append(f"DKIM:{domain}:✓")
                    except Exception:
                        status.append(f"DKIM:{domain}:✗")
                        all_verified = False

            elapsed = int(time.time() - start_time)

            if all_verified:
                self.logger.info(f"All DNS records verified: {' '.join(status)}")
                return True

            if elapsed >= timeout:
                self.logger.error(f"DNS verification timeout after {timeout}s")
                self.logger.error(f"Status: {' '.join(status)}")
                return False

            self.logger.info(f"[{elapsed}s/{timeout}s] {' '.join(status)}")
            time.sleep(interval)

    def status(self) -> dict:
        """Get current DNS status"""
        result = {
            "owner_id": self.provider.owner_id,
            "domains": {},
        }

        for domain_cfg in self.mail_config.domains:
            domain = domain_cfg["name"]
            zone_id = self._get_zone_id(domain)

            domain_status = {
                "zone_id": zone_id,
                "records": [],
            }

            if zone_id:
                owned = self.provider.list_owned_records(zone_id)
                for record in owned:
                    domain_status["records"].append(
                        {
                            "name": record.name,
                            "type": record.type.value,
                            "content": record.content[:50] + "..."
                            if len(record.content) > 50
                            else record.content,
                        }
                    )

            result["domains"][domain] = domain_status

        return result


def setup_logging(verbose: bool = False):
    """Configure logging"""
    level = logging.DEBUG if verbose else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    parser = argparse.ArgumentParser(
        description="DNS Manager for Mail Relay",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "command",
        choices=["init", "update", "cleanup", "verify", "status"],
        help="Command to execute",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--wait-for-lb",
        type=int,
        default=300,
        help="Seconds to wait for LoadBalancer IP (default: 300)",
    )
    parser.add_argument(
        "--verify-timeout",
        type=int,
        default=600,
        help="Seconds to wait for DNS propagation (default: 600)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't make actual changes",
    )

    args = parser.parse_args()

    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    # Set dry run mode
    if args.dry_run:
        os.environ["DNS_DRY_RUN"] = "true"

    # Initialize components
    provider = get_provider_from_env()
    if not provider:
        logger.error("Failed to initialize DNS provider")
        sys.exit(1)

    mail_config = MailConfig.from_env()
    if not mail_config.hostname:
        logger.error("MAIL_HOSTNAME not set")
        sys.exit(1)

    if not mail_config.domains:
        logger.error("MAIL_DOMAINS not set")
        sys.exit(1)

    k8s_config = KubernetesConfig.from_env()
    k8s = KubernetesClient(k8s_config)

    ip_config = IPDetectorConfig.from_env()
    ip_detector = IPDetector(ip_config)

    manager = DNSManager(provider, mail_config, k8s, ip_detector)

    # Shared directory for watcher communication
    shared_dir = Path(os.environ.get("SHARED_DIR", "/shared"))

    # Execute command
    if args.command in ("init", "update"):
        success = manager.init_or_update(wait_for_lb=args.wait_for_lb)

        # Save IP to shared volume for watcher
        if success and shared_dir.exists():
            incoming_ip = ip_detector.get_incoming_ip(k8s, wait_timeout=0)
            if incoming_ip:
                ip_file = shared_dir / "current-ip"
                ip_file.write_text(incoming_ip)
                logger.info(f"Saved IP to {ip_file}")

        sys.exit(0 if success else 1)

    elif args.command == "cleanup":
        success = manager.cleanup()
        sys.exit(0 if success else 1)

    elif args.command == "verify":
        success = manager.verify(timeout=args.verify_timeout)
        sys.exit(0 if success else 1)

    elif args.command == "status":
        status = manager.status()
        print(json.dumps(status, indent=2))
        sys.exit(0)


if __name__ == "__main__":
    main()
