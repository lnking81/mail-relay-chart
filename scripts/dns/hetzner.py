"""
Hetzner Cloud PTR Provider Implementation

Uses Hetzner Cloud API for PTR (reverse DNS) record management.
Note: This is NOT for Hetzner DNS Console (dns.hetzner.com), but for
PTR records on Hetzner Cloud resources (servers, IPs, etc.).
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Literal, Optional

import requests

from .base import DNSProvider, DNSProviderConfig, DNSRecord, RecordType

logger = logging.getLogger(__name__)

HETZNER_API_BASE = "https://api.hetzner.cloud/v1"


@dataclass
class HetznerIPInfo:
    """Information about an IP in Hetzner Cloud"""

    ip: str
    ip_type: Literal["server", "primary_ip", "floating_ip", "load_balancer", "unknown"]
    resource_id: int
    current_ptr: Optional[str] = None

    def __str__(self) -> str:
        return (
            f"{self.ip} ({self.ip_type}, id={self.resource_id}, ptr={self.current_ptr})"
        )


@dataclass
class HetznerConfig(DNSProviderConfig):
    """Hetzner Cloud-specific configuration"""

    # API Token with Read & Write permissions
    api_token: str = ""

    # API base URL
    api_base: str = HETZNER_API_BASE

    # Request timeout in seconds
    timeout: int = 30

    # Action wait timeout in seconds
    action_timeout: int = 60

    @classmethod
    def from_env(cls, owner_id: str) -> "HetznerConfig":
        """Create config from environment variables"""
        return cls(
            owner_id=owner_id,
            api_token=os.environ.get("HETZNER_API_TOKEN", ""),
            default_ttl=int(os.environ.get("DNS_TTL", "300")),
            dry_run=os.environ.get("DNS_DRY_RUN", "false").lower() == "true",
        )


class HetznerProvider(DNSProvider):
    """
    Hetzner Cloud PTR provider implementation.

    Manages PTR (reverse DNS) records for:
    - Server primary IPs
    - Primary IPs (standalone)
    - Floating IPs

    Note: Load Balancer PTR cannot be managed via API.
    Use annotation: load-balancer.hetzner.cloud/hostname
    """

    def __init__(self, config: HetznerConfig):
        super().__init__(config)
        self.hetzner_config = config
        self._session = self._create_session()
        self._ip_cache: dict[str, HetznerIPInfo] = {}

    def _create_session(self) -> requests.Session:
        """Create configured requests session"""
        session = requests.Session()
        session.headers.update(
            {
                "Authorization": f"Bearer {self.hetzner_config.api_token}",
                "Content-Type": "application/json",
            }
        )
        return session

    def _api_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Make API request to Hetzner Cloud"""
        url = f"{self.hetzner_config.api_base}{endpoint}"

        try:
            response = self._session.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                timeout=self.hetzner_config.timeout,
            )

            if response.status_code >= 400:
                error_body: dict[str, Any] = {}
                try:
                    error_body = response.json()
                except Exception:
                    pass

                error_msg: str = str(
                    error_body.get("error", {}).get("message", response.text)
                )
                self.logger.error(f"Hetzner API error: {error_msg}")
                raise HetznerAPIError(error_msg, response.status_code)

            data: dict[str, Any] = response.json() if response.content else {}
            return data

        except requests.RequestException as e:
            self.logger.error(f"Hetzner API request failed: {e}")
            raise HetznerAPIError(f"Request failed: {e}")

    def _wait_for_action(self, action_id: int) -> bool:
        """Wait for Hetzner action to complete"""
        if not action_id:
            return True

        start = time.time()
        timeout = self.hetzner_config.action_timeout

        while time.time() - start < timeout:
            result = self._api_request("GET", f"/actions/{action_id}")
            action = result.get("action", {})
            status = action.get("status")

            if status == "success":
                self.logger.debug("Action completed successfully")
                return True
            elif status == "error":
                error = action.get("error", {})
                self.logger.error(f"Action failed: {error.get('message')}")
                return False

            time.sleep(2)

        self.logger.error(f"Action timed out after {timeout}s")
        return False

    # ==========================================================================
    # IP Discovery Methods
    # ==========================================================================

    def find_ip(self, ip: str) -> Optional[HetznerIPInfo]:
        """
        Find IP in Hetzner Cloud and determine its type.

        Searches in order:
        1. Servers (primary IP)
        2. Primary IPs (standalone)
        3. Floating IPs
        4. Load Balancers
        """
        # Check cache first
        if ip in self._ip_cache:
            return self._ip_cache[ip]

        self.logger.debug(f"Searching for IP: {ip}")

        # Check servers
        result = self._check_servers(ip)
        if result:
            self._ip_cache[ip] = result
            return result

        # Check standalone Primary IPs
        result = self._check_primary_ips(ip)
        if result:
            self._ip_cache[ip] = result
            return result

        # Check Floating IPs
        result = self._check_floating_ips(ip)
        if result:
            self._ip_cache[ip] = result
            return result

        # Check Load Balancers
        result = self._check_load_balancers(ip)
        if result:
            self._ip_cache[ip] = result
            return result

        return None

    def _check_servers(self, ip: str) -> Optional[HetznerIPInfo]:
        """Check if IP belongs to a server"""
        try:
            data = self._api_request("GET", "/servers")
        except HetznerAPIError:
            return None

        for server in data.get("servers", []):
            public_net = server.get("public_net", {})
            ipv4 = public_net.get("ipv4", {})

            if ipv4.get("ip") == ip:
                return HetznerIPInfo(
                    ip=ip,
                    ip_type="server",
                    resource_id=server["id"],
                    current_ptr=ipv4.get("dns_ptr"),
                )

        return None

    def _check_primary_ips(self, ip: str) -> Optional[HetznerIPInfo]:
        """Check if IP is a standalone Primary IP"""
        try:
            data = self._api_request("GET", "/primary_ips")
        except HetznerAPIError:
            return None

        for pip in data.get("primary_ips", []):
            if pip.get("ip") == ip:
                ptr_records = pip.get("dns_ptr", [])
                current_ptr = ptr_records[0].get("dns_ptr") if ptr_records else None

                return HetznerIPInfo(
                    ip=ip,
                    ip_type="primary_ip",
                    resource_id=pip["id"],
                    current_ptr=current_ptr,
                )

        return None

    def _check_floating_ips(self, ip: str) -> Optional[HetznerIPInfo]:
        """Check if IP is a Floating IP"""
        try:
            data = self._api_request("GET", "/floating_ips")
        except HetznerAPIError:
            return None

        for fip in data.get("floating_ips", []):
            if fip.get("ip") == ip:
                ptr_records = fip.get("dns_ptr", [])
                current_ptr = ptr_records[0].get("dns_ptr") if ptr_records else None

                return HetznerIPInfo(
                    ip=ip,
                    ip_type="floating_ip",
                    resource_id=fip["id"],
                    current_ptr=current_ptr,
                )

        return None

    def _check_load_balancers(self, ip: str) -> Optional[HetznerIPInfo]:
        """Check if IP belongs to a Load Balancer"""
        try:
            data = self._api_request("GET", "/load_balancers")
        except HetznerAPIError:
            return None

        for lb in data.get("load_balancers", []):
            public_net = lb.get("public_net", {})
            ipv4 = public_net.get("ipv4", {})

            if ipv4.get("ip") == ip:
                return HetznerIPInfo(
                    ip=ip,
                    ip_type="load_balancer",
                    resource_id=lb["id"],
                    current_ptr=None,  # LB PTR managed via annotation
                )

        return None

    # ==========================================================================
    # PTR Record Management
    # ==========================================================================

    def set_ptr(self, ip: str, hostname: str) -> bool:
        """
        Set PTR record for the given IP.

        Args:
            ip: IP address
            hostname: PTR hostname (must have forward DNS pointing to this IP)

        Returns:
            True on success
        """
        ip_info = self.find_ip(ip)
        if not ip_info:
            self.logger.error(f"IP {ip} not found in Hetzner Cloud")
            return False

        self.logger.info(f"Found IP: {ip_info}")

        # Check if PTR already correct
        if ip_info.current_ptr == hostname:
            self.logger.info(f"PTR already set correctly: {ip} -> {hostname}")
            return True

        if self.config.dry_run:
            self.logger.info(f"[DRY RUN] Would set PTR: {ip} -> {hostname}")
            return True

        # Set PTR based on IP type
        if ip_info.ip_type == "server":
            return self._set_server_ptr(ip_info.resource_id, ip, hostname)
        elif ip_info.ip_type == "primary_ip":
            return self._set_primary_ip_ptr(ip_info.resource_id, ip, hostname)
        elif ip_info.ip_type == "floating_ip":
            return self._set_floating_ip_ptr(ip_info.resource_id, ip, hostname)
        elif ip_info.ip_type == "load_balancer":
            self.logger.error("Load Balancer PTR cannot be changed via API")
            self.logger.error("Use annotation: load-balancer.hetzner.cloud/hostname")
            return False
        else:
            self.logger.error(f"Unknown IP type: {ip_info.ip_type}")
            return False

    def _set_server_ptr(self, server_id: int, ip: str, hostname: str) -> bool:
        """Set PTR for server's primary IP"""
        endpoint = f"/servers/{server_id}/actions/change_dns_ptr"
        data = {"ip": ip, "dns_ptr": hostname}

        try:
            result = self._api_request("POST", endpoint, json_data=data)
            action = result.get("action", {})

            if self._wait_for_action(action.get("id")):
                self.logger.info(f"✓ PTR set: {ip} -> {hostname}")
                # Clear cache
                self._ip_cache.pop(ip, None)
                return True
            return False
        except HetznerAPIError as e:
            self.logger.error(f"✗ Failed to set server PTR: {e}")
            return False

    def _set_primary_ip_ptr(self, primary_ip_id: int, ip: str, hostname: str) -> bool:
        """Set PTR for Primary IP"""
        endpoint = f"/primary_ips/{primary_ip_id}/actions/change_dns_ptr"
        data = {"ip": ip, "dns_ptr": hostname}

        try:
            result = self._api_request("POST", endpoint, json_data=data)
            action = result.get("action", {})

            if self._wait_for_action(action.get("id")):
                self.logger.info(f"✓ PTR set: {ip} -> {hostname}")
                self._ip_cache.pop(ip, None)
                return True
            return False
        except HetznerAPIError as e:
            self.logger.error(f"✗ Failed to set primary IP PTR: {e}")
            return False

    def _set_floating_ip_ptr(self, floating_ip_id: int, ip: str, hostname: str) -> bool:
        """Set PTR for Floating IP"""
        endpoint = f"/floating_ips/{floating_ip_id}/actions/change_dns_ptr"
        data = {"ip": ip, "dns_ptr": hostname}

        try:
            result = self._api_request("POST", endpoint, json_data=data)
            action = result.get("action", {})

            if self._wait_for_action(action.get("id")):
                self.logger.info(f"✓ PTR set: {ip} -> {hostname}")
                self._ip_cache.pop(ip, None)
                return True
            return False
        except HetznerAPIError as e:
            self.logger.error(f"✗ Failed to set floating IP PTR: {e}")
            return False

    def get_ptr(self, ip: str) -> Optional[str]:
        """Get current PTR record for IP"""
        ip_info = self.find_ip(ip)
        return ip_info.current_ptr if ip_info else None

    def verify_credentials(self) -> bool:
        """Verify API token is valid"""
        try:
            # List servers to verify token works
            self._api_request("GET", "/servers", params={"per_page": 1})
            self.logger.info("Hetzner API token verified")
            return True
        except HetznerAPIError:
            return False

    # ==========================================================================
    # DNSProvider interface (partially implemented for PTR-only use)
    # ==========================================================================

    def get_zone_id(self, domain: str) -> Optional[str]:
        """
        Not applicable for Hetzner Cloud PTR.
        PTR records are managed per-IP, not per-zone.
        """
        return None

    def list_records(
        self,
        zone_id: str,
        record_type: Optional[RecordType] = None,
        name: Optional[str] = None,
    ) -> list[DNSRecord]:
        """Not applicable for Hetzner Cloud PTR."""
        return []

    def create_record(self, zone_id: str, record: DNSRecord) -> bool:
        """Not applicable for Hetzner Cloud PTR."""
        return False

    def update_record(self, zone_id: str, record: DNSRecord) -> bool:
        """Not applicable for Hetzner Cloud PTR."""
        return False

    def delete_record(self, zone_id: str, record_id: str) -> bool:
        """Not applicable for Hetzner Cloud PTR."""
        return False


class HetznerAPIError(Exception):
    """Hetzner API error"""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


# =============================================================================
# Hetzner Robot API (for Dedicated Servers)
# =============================================================================

HETZNER_ROBOT_API_BASE = "https://robot-ws.your-server.de"


@dataclass
class HetznerRobotConfig(DNSProviderConfig):
    """Hetzner Robot API configuration"""

    # Webservice username
    username: str = ""

    # Webservice password
    password: str = ""

    # API base URL
    api_base: str = HETZNER_ROBOT_API_BASE

    # Request timeout
    timeout: int = 30

    @classmethod
    def from_env(cls, owner_id: str) -> "HetznerRobotConfig":
        """Create config from environment variables"""
        return cls(
            owner_id=owner_id,
            username=os.environ.get("HETZNER_ROBOT_USERNAME", ""),
            password=os.environ.get("HETZNER_ROBOT_PASSWORD", ""),
            default_ttl=int(os.environ.get("DNS_TTL", "300")),
            dry_run=os.environ.get("DNS_DRY_RUN", "false").lower() == "true",
        )


class HetznerRobotProvider(DNSProvider):
    """
    Hetzner Robot PTR provider for dedicated servers.

    Uses Robot Webservice API: https://robot.your-server.de/doc/webservice/en.html
    Requires webservice credentials (not hcloud token).
    """

    def __init__(self, config: HetznerRobotConfig):
        super().__init__(config)
        self.robot_config = config
        self._session = self._create_session()

    def _create_session(self) -> requests.Session:
        """Create configured requests session"""
        session = requests.Session()
        session.auth = (self.robot_config.username, self.robot_config.password)
        return session

    def _api_request(
        self,
        method: str,
        endpoint: str,
        data: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """Make API request to Hetzner Robot"""
        url = f"{self.robot_config.api_base}{endpoint}"

        try:
            response = self._session.request(
                method=method,
                url=url,
                data=data,
                timeout=self.robot_config.timeout,
            )

            if response.status_code == 404:
                return None

            if response.status_code >= 400:
                error_msg = response.text
                try:
                    error_body: dict[str, Any] = response.json()
                    error_msg = str(
                        error_body.get("error", {}).get("message", response.text)
                    )
                except Exception:
                    pass
                self.logger.error(f"Hetzner Robot API error: {error_msg}")
                raise HetznerAPIError(error_msg, response.status_code)

            result: dict[str, Any] = response.json() if response.content else {}
            return result

        except requests.RequestException as e:
            self.logger.error(f"Hetzner Robot API request failed: {e}")
            raise HetznerAPIError(f"Request failed: {e}")

    def get_ptr(self, ip: str) -> Optional[str]:
        """Get current PTR record for IP"""
        try:
            result = self._api_request("GET", f"/rdns/{ip}")
            if result:
                rdns: dict[str, Any] = result.get("rdns", {})
                ptr_value = rdns.get("ptr")
                return str(ptr_value) if ptr_value else None
        except HetznerAPIError:
            pass
        return None

    def set_ptr(self, ip: str, hostname: str) -> bool:
        """
        Set PTR record for the given IP.

        Args:
            ip: IP address (must be assigned to your dedicated server)
            hostname: PTR hostname

        Returns:
            True on success
        """
        current_ptr = self.get_ptr(ip)

        if current_ptr == hostname:
            self.logger.info(f"PTR already set correctly: {ip} -> {hostname}")
            return True

        if self.config.dry_run:
            self.logger.info(f"[DRY RUN] Would set PTR: {ip} -> {hostname}")
            return True

        try:
            self._api_request("POST", f"/rdns/{ip}", data={"ptr": hostname})
            self.logger.info(f"✓ PTR set: {ip} -> {hostname}")
            return True
        except HetznerAPIError as e:
            self.logger.error(f"✗ Failed to set PTR: {e}")
            return False

    def verify_credentials(self) -> bool:
        """Verify Robot credentials are valid"""
        try:
            # Get server list to verify credentials
            self._api_request("GET", "/server")
            self.logger.info("Hetzner Robot credentials verified")
            return True
        except HetznerAPIError:
            return False

    # DNSProvider interface (not applicable for Robot PTR)
    def get_zone_id(self, domain: str) -> Optional[str]:
        return None

    def list_records(
        self,
        zone_id: str,
        record_type: Optional[RecordType] = None,
        name: Optional[str] = None,
    ) -> list[DNSRecord]:
        return []

    def create_record(self, zone_id: str, record: DNSRecord) -> bool:
        return False

    def update_record(self, zone_id: str, record: DNSRecord) -> bool:
        return False

    def delete_record(self, zone_id: str, record_id: str) -> bool:
        return False
