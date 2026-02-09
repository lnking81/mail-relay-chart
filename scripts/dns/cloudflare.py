"""
Cloudflare DNS Provider Implementation

Uses Cloudflare API v4 for DNS record management with ownership tracking.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import requests

from .base import DNSProvider, DNSProviderConfig, DNSRecord, RecordType

logger = logging.getLogger(__name__)


@dataclass
class CloudflareConfig(DNSProviderConfig):
    """Cloudflare-specific configuration"""

    # API Token with Zone:DNS:Edit and Zone:Zone:Read permissions
    api_token: str = ""

    # Explicit zone ID mapping: domain -> zone_id
    # If not specified, zones are auto-discovered
    zone_ids: dict[str, str] = field(default_factory=dict)

    # Enable Cloudflare proxy (orange cloud) for A/CNAME records
    proxied: bool = False

    # API base URL
    api_base: str = "https://api.cloudflare.com/client/v4"

    # Request timeout in seconds
    timeout: int = 30

    @classmethod
    def from_env(cls, owner_id: str) -> "CloudflareConfig":
        """Create config from environment variables"""
        zone_ids = {}

        # Parse CLOUDFLARE_ZONE_IDS: "domain1:zone1,domain2:zone2"
        zone_ids_env = os.environ.get("CLOUDFLARE_ZONE_IDS", "")
        if zone_ids_env:
            for pair in zone_ids_env.split(","):
                if ":" in pair:
                    domain, zone_id = pair.split(":", 1)
                    zone_ids[domain.strip()] = zone_id.strip()

        return cls(
            owner_id=owner_id,
            api_token=os.environ.get("CF_API_TOKEN", ""),
            zone_ids=zone_ids,
            proxied=os.environ.get("CLOUDFLARE_PROXIED", "false").lower() == "true",
            default_ttl=int(os.environ.get("DNS_TTL", "300")),
            dry_run=os.environ.get("DNS_DRY_RUN", "false").lower() == "true",
        )


class CloudflareProvider(DNSProvider):
    """
    Cloudflare DNS provider implementation.

    Features:
    - Automatic zone discovery from domain name
    - Support for proxied records (A/CNAME)
    - Ownership tracking via TXT records
    - Rate limiting awareness
    """

    def __init__(self, config: CloudflareConfig):
        super().__init__(config)
        self.cf_config = config
        self._zone_cache: dict[str, str] = dict(config.zone_ids)
        self._session = self._create_session()

    def _create_session(self) -> requests.Session:
        """Create configured requests session"""
        session = requests.Session()
        session.headers.update(
            {
                "Authorization": f"Bearer {self.cf_config.api_token}",
                "Content-Type": "application/json",
            }
        )
        return session

    def _api_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        json_data: Optional[dict] = None,
    ) -> dict:
        """Make API request to Cloudflare"""
        url = f"{self.cf_config.api_base}{endpoint}"

        try:
            response = self._session.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                timeout=self.cf_config.timeout,
            )

            data = response.json()

            if not data.get("success", False):
                errors = data.get("errors", [])
                error_msg = "; ".join(e.get("message", str(e)) for e in errors)
                self.logger.error(f"Cloudflare API error: {error_msg}")
                raise CloudflareAPIError(error_msg, errors)

            return data

        except requests.RequestException as e:
            self.logger.error(f"Cloudflare API request failed: {e}")
            raise CloudflareAPIError(f"Request failed: {e}")

    def get_zone_id(self, domain: str) -> Optional[str]:
        """
        Get zone ID for a domain.

        Tries exact match first, then walks up the domain hierarchy.
        Results are cached for performance.
        """
        # Check cache first
        if domain in self._zone_cache:
            return self._zone_cache[domain]

        # Try to find zone by walking up domain hierarchy
        check_domain = domain
        while "." in check_domain:
            try:
                data = self._api_request(
                    "GET",
                    "/zones",
                    params={"name": check_domain, "status": "active"},
                )

                results = data.get("result", [])
                if results:
                    zone_id = results[0]["id"]
                    # Cache both the requested domain and found zone domain
                    self._zone_cache[domain] = zone_id
                    self._zone_cache[check_domain] = zone_id
                    self.logger.debug(
                        f"Found zone {check_domain} ({zone_id}) for {domain}"
                    )
                    return zone_id

            except CloudflareAPIError:
                pass

            # Try parent domain
            check_domain = check_domain.split(".", 1)[1] if "." in check_domain else ""

        self.logger.error(f"Could not find Cloudflare zone for domain: {domain}")
        return None

    def list_records(
        self,
        zone_id: str,
        record_type: Optional[RecordType] = None,
        name: Optional[str] = None,
    ) -> list[DNSRecord]:
        """List DNS records in a zone with optional filtering"""
        params = {"per_page": 100}

        if record_type:
            params["type"] = record_type.value
        if name:
            params["name"] = name

        records = []
        page = 1

        while True:
            params["page"] = page

            try:
                data = self._api_request(
                    "GET", f"/zones/{zone_id}/dns_records", params=params
                )
            except CloudflareAPIError:
                break

            for item in data.get("result", []):
                record = DNSRecord(
                    name=item["name"],
                    type=RecordType(item["type"]),
                    content=item["content"],
                    ttl=item.get("ttl", 1),
                    priority=item.get("priority"),
                    proxied=item.get("proxied", False),
                    record_id=item["id"],
                )
                records.append(record)

            # Check pagination
            result_info = data.get("result_info", {})
            total_pages = result_info.get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1

        return records

    def create_record(self, zone_id: str, record: DNSRecord) -> bool:
        """Create a new DNS record"""
        data = {
            "type": record.type.value,
            "name": record.name,
            "content": record.content,
            "ttl": record.ttl if record.ttl > 1 else 1,  # 1 = auto
        }

        # Add priority for MX records
        if record.type == RecordType.MX and record.priority is not None:
            data["priority"] = record.priority

        # Proxied only for A/CNAME, never for TXT
        if record.type in (RecordType.A, RecordType.CNAME):
            data["proxied"] = self.cf_config.proxied and record.proxied

        try:
            result = self._api_request(
                "POST", f"/zones/{zone_id}/dns_records", json_data=data
            )
            record.record_id = result["result"]["id"]
            self.logger.info(f"✓ Created {record.type.value} {record.name}")
            return True
        except CloudflareAPIError as e:
            self.logger.error(
                f"✗ Failed to create {record.type.value} {record.name}: {e}"
            )
            return False

    def update_record(self, zone_id: str, record: DNSRecord) -> bool:
        """Update an existing DNS record"""
        if not record.record_id:
            self.logger.error(f"Cannot update record without record_id: {record.name}")
            return False

        data = {
            "type": record.type.value,
            "name": record.name,
            "content": record.content,
            "ttl": record.ttl if record.ttl > 1 else 1,
        }

        if record.type == RecordType.MX and record.priority is not None:
            data["priority"] = record.priority

        if record.type in (RecordType.A, RecordType.CNAME):
            data["proxied"] = self.cf_config.proxied and record.proxied

        try:
            self._api_request(
                "PUT",
                f"/zones/{zone_id}/dns_records/{record.record_id}",
                json_data=data,
            )
            self.logger.info(f"✓ Updated {record.type.value} {record.name}")
            return True
        except CloudflareAPIError as e:
            self.logger.error(
                f"✗ Failed to update {record.type.value} {record.name}: {e}"
            )
            return False

    def delete_record(self, zone_id: str, record_id: str) -> bool:
        """Delete a DNS record"""
        try:
            self._api_request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
            self.logger.info(f"✓ Deleted record {record_id}")
            return True
        except CloudflareAPIError as e:
            self.logger.error(f"✗ Failed to delete record {record_id}: {e}")
            return False

    def verify_credentials(self) -> bool:
        """Verify API token is valid"""
        try:
            data = self._api_request("GET", "/user/tokens/verify")
            status = data.get("result", {}).get("status")
            if status == "active":
                self.logger.info("Cloudflare API token verified")
                return True
            self.logger.error(f"Token status: {status}")
            return False
        except CloudflareAPIError:
            return False


class CloudflareAPIError(Exception):
    """Cloudflare API error"""

    def __init__(self, message: str, errors: Optional[list] = None):
        super().__init__(message)
        self.errors = errors or []
