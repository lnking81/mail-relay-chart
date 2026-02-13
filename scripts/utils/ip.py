"""IP Detection Utilities.

Provides methods to detect external IP addresses from various sources.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import requests

if TYPE_CHECKING:
    from utils.k8s import KubernetesClient

logger = logging.getLogger(__name__)

# IPv4 regex pattern
IPV4_PATTERN = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")

# Default external IP detection APIs
DEFAULT_IP_APIS = [
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "https://api.ipify.org",
    "https://ipinfo.io/ip",
]


@dataclass
class IPDetectorConfig:
    """IP detection configuration"""

    # Static IPs (if set, disables auto-detection)
    static_ips: Optional[list[str]] = None

    # External APIs for IP detection
    external_apis: Optional[list[str]] = None

    # Detect outbound NAT IP
    detect_outbound: bool = True

    # Request timeout
    timeout: int = 15

    def __post_init__(self) -> None:
        if self.static_ips is None:
            self.static_ips = []
        if self.external_apis is None:
            self.external_apis = list(DEFAULT_IP_APIS)

    @classmethod
    def from_env(cls) -> "IPDetectorConfig":
        """Create config from environment variables"""
        static_ips = []
        static_env = os.environ.get("STATIC_IPS", "")
        if static_env:
            static_ips = [ip.strip() for ip in static_env.split(",") if ip.strip()]

        external_apis = list(DEFAULT_IP_APIS)
        apis_env = os.environ.get("IP_DETECTION_APIS", "")
        if apis_env:
            external_apis = [api.strip() for api in apis_env.split(",") if api.strip()]

        return cls(
            static_ips=static_ips,
            external_apis=external_apis,
            detect_outbound=os.environ.get("DETECT_OUTBOUND_IP", "true").lower()
            == "true",
            timeout=int(os.environ.get("IP_DETECTION_TIMEOUT", "15")),
        )


class IPDetector:
    """
    Detects external IP addresses from various sources.

    Priority:
    1. Static IPs (if configured)
    2. LoadBalancer service IP
    3. Node external IP (for NodePort)
    4. Outbound NAT IP (via external API)
    """

    def __init__(self, config: Optional[IPDetectorConfig] = None):
        self.config = config or IPDetectorConfig()
        self.logger = logging.getLogger(__name__)

    def detect_outbound_ip(self) -> Optional[str]:
        """Detect outbound IP via external API"""
        if not self.config.detect_outbound:
            return None

        for api_url in self.config.external_apis or []:
            try:
                response = requests.get(
                    api_url,
                    timeout=self.config.timeout,
                    headers={"User-Agent": "mail-relay-dns-manager"},
                )

                if response.status_code == 200:
                    ip = response.text.strip()
                    if self._is_valid_ipv4(ip):
                        self.logger.debug(f"Detected outbound IP via {api_url}: {ip}")
                        return ip

            except requests.RequestException as e:
                self.logger.debug(f"Failed to get IP from {api_url}: {e}")
                continue

        self.logger.warning("Could not detect outbound IP from any API")
        return None

    def get_static_ips(self) -> list[str]:
        """Get configured static IPs"""
        return list(self.config.static_ips or [])

    def get_incoming_ip(
        self, k8s_client: Optional[KubernetesClient] = None, wait_timeout: int = 0
    ) -> Optional[str]:
        """
        Get primary incoming IP address.

        Args:
            k8s_client: Optional KubernetesClient for service IP detection
            wait_timeout: Seconds to wait for LoadBalancer IP (0 = no wait)

        Returns:
            IP address string or None
        """
        # Static IP takes priority
        if self.config.static_ips:
            return self.config.static_ips[0]

        # Try LoadBalancer/NodePort IP
        if k8s_client:
            service_ip = k8s_client.get_service_ip(wait_timeout=wait_timeout)
            if service_ip:
                return service_ip

        # Fallback to outbound IP
        return self.detect_outbound_ip()

    def get_all_ips(
        self, k8s_client: Optional[KubernetesClient] = None, wait_timeout: int = 0
    ) -> list[str]:
        """
        Get all IPs for SPF record (incoming + outbound if different).

        Returns:
            List of unique IP addresses
        """
        ips: set[str] = set()

        # Static IPs
        if self.config.static_ips:
            ips.update(self.config.static_ips)
            return list(ips)

        # Incoming IP
        incoming = self.get_incoming_ip(k8s_client, wait_timeout)
        if incoming:
            ips.add(incoming)

        # Outbound IP (might be different due to NAT)
        if self.config.detect_outbound:
            outbound = self.detect_outbound_ip()
            if outbound:
                ips.add(outbound)

        return list(ips)

    def _is_valid_ipv4(self, ip: str) -> bool:
        """Validate IPv4 address format"""
        if not IPV4_PATTERN.match(ip):
            return False

        # Check octets are in valid range
        try:
            octets = [int(x) for x in ip.split(".")]
            return all(0 <= x <= 255 for x in octets)
        except ValueError:
            return False


def detect_ip(
    k8s_client: Optional[KubernetesClient] = None,
    wait_timeout: int = 0,
    config: Optional[IPDetectorConfig] = None,
) -> Optional[str]:
    """
    Convenience function to detect incoming IP.

    Args:
        k8s_client: Optional KubernetesClient
        wait_timeout: Seconds to wait for LoadBalancer
        config: Optional IPDetectorConfig

    Returns:
        IP address string or None
    """
    detector = IPDetector(config or IPDetectorConfig.from_env())
    return detector.get_incoming_ip(k8s_client, wait_timeout)
