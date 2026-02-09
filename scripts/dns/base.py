"""
Abstract DNS Provider Interface

Provides base class for DNS providers with ownership tracking via TXT records.
Similar to external-dns approach for record management.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class RecordType(str, Enum):
    """Supported DNS record types"""

    A = "A"
    AAAA = "AAAA"
    MX = "MX"
    TXT = "TXT"
    CNAME = "CNAME"


@dataclass
class DNSRecord:
    """Represents a DNS record"""

    name: str
    type: RecordType
    content: str
    ttl: int = 300
    priority: Optional[int] = None  # For MX records
    proxied: bool = False  # Cloudflare-specific

    # Internal tracking
    record_id: Optional[str] = None

    def __post_init__(self):
        # Normalize record name (remove trailing dot)
        self.name = self.name.rstrip(".")

    @property
    def ownership_record_name(self) -> str:
        """Generate TXT record name for ownership tracking"""
        # Format: _mail-relay-owner.{original_name}
        return f"_mail-relay-owner.{self.name}"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DNSRecord):
            return False
        return (
            self.name == other.name
            and self.type == other.type
            and self.content == other.content
        )

    def __hash__(self) -> int:
        return hash((self.name, self.type, self.content))


@dataclass
class DNSProviderConfig:
    """Base configuration for DNS providers"""

    # Ownership identifier (namespace/release or custom)
    owner_id: str

    # TXT record prefix for ownership tracking
    txt_prefix: str = "mail-relay-owner"

    # Default TTL for records
    default_ttl: int = 300

    # Dry run mode - don't make actual changes
    dry_run: bool = False


class DNSProvider(ABC):
    """
    Abstract base class for DNS providers.

    Implements ownership tracking via TXT records to ensure we only
    modify records that were created by this instance.

    Ownership record format:
        Name: _mail-relay-owner.{record_name}
        Type: TXT
        Content: "heritage=mail-relay,owner={owner_id},record-type={type}"
    """

    def __init__(self, config: DNSProviderConfig):
        self.config = config
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    def owner_id(self) -> str:
        return self.config.owner_id

    def _ownership_content(self, record_type: RecordType) -> str:
        """Generate ownership TXT record content"""
        return (
            f"heritage=mail-relay,owner={self.owner_id},record-type={record_type.value}"
        )

    def _parse_ownership(self, content: str) -> Optional[dict]:
        """Parse ownership TXT record content"""
        try:
            parts = {}
            for part in content.split(","):
                if "=" in part:
                    key, value = part.split("=", 1)
                    parts[key] = value
            if parts.get("heritage") == "mail-relay":
                return parts
        except Exception:
            pass
        return None

    def _is_owned_by_us(self, ownership_content: str) -> bool:
        """Check if record is owned by this instance"""
        parsed = self._parse_ownership(ownership_content)
        if parsed and parsed.get("owner") == self.owner_id:
            return True
        return False

    # ==========================================================================
    # Abstract methods - must be implemented by providers
    # ==========================================================================

    @abstractmethod
    def get_zone_id(self, domain: str) -> Optional[str]:
        """
        Get zone ID for a domain.

        Args:
            domain: Domain name to find zone for

        Returns:
            Zone ID string or None if not found
        """
        pass

    @abstractmethod
    def list_records(
        self,
        zone_id: str,
        record_type: Optional[RecordType] = None,
        name: Optional[str] = None,
    ) -> list[DNSRecord]:
        """
        List DNS records in a zone.

        Args:
            zone_id: Zone identifier
            record_type: Filter by record type
            name: Filter by record name

        Returns:
            List of DNSRecord objects
        """
        pass

    @abstractmethod
    def create_record(self, zone_id: str, record: DNSRecord) -> bool:
        """
        Create a new DNS record.

        Args:
            zone_id: Zone identifier
            record: Record to create

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    def update_record(self, zone_id: str, record: DNSRecord) -> bool:
        """
        Update an existing DNS record.

        Args:
            zone_id: Zone identifier
            record: Record with record_id set

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    def delete_record(self, zone_id: str, record_id: str) -> bool:
        """
        Delete a DNS record.

        Args:
            zone_id: Zone identifier
            record_id: Record identifier

        Returns:
            True if successful
        """
        pass

    # ==========================================================================
    # High-level operations with ownership tracking
    # ==========================================================================

    def ensure_record(self, zone_id: str, record: DNSRecord) -> bool:
        """
        Ensure a DNS record exists with ownership tracking.

        Creates or updates record only if:
        - Record doesn't exist, or
        - Record exists and is owned by us

        Args:
            zone_id: Zone identifier
            record: Desired record state

        Returns:
            True if record is in desired state
        """
        existing = self.list_records(zone_id, record.type, record.name)

        if existing:
            existing_record = existing[0]

            # Check ownership
            if not self._check_ownership(zone_id, record):
                self.logger.warning(
                    f"Record {record.type.value} {record.name} exists but not owned by us, skipping"
                )
                return False

            # Check if update needed
            if existing_record.content == record.content:
                self.logger.debug(f"Record {record.type.value} {record.name} unchanged")
                return True

            # Update record
            record.record_id = existing_record.record_id
            self.logger.info(
                f"Updating {record.type.value} {record.name}: "
                f"{existing_record.content} -> {record.content}"
            )

            if self.config.dry_run:
                self.logger.info("[DRY RUN] Would update record")
                return True

            return self.update_record(zone_id, record)

        # Create new record with ownership
        self.logger.info(
            f"Creating {record.type.value} {record.name} = {record.content}"
        )

        if self.config.dry_run:
            self.logger.info("[DRY RUN] Would create record")
            return True

        if self.create_record(zone_id, record):
            return self._set_ownership(zone_id, record)
        return False

    def delete_owned_record(
        self, zone_id: str, name: str, record_type: RecordType
    ) -> bool:
        """
        Delete a record only if owned by us.

        Args:
            zone_id: Zone identifier
            name: Record name
            record_type: Record type

        Returns:
            True if deleted or didn't exist
        """
        existing = self.list_records(zone_id, record_type, name)

        if not existing:
            return True

        record = existing[0]

        if not self._check_ownership(
            zone_id, DNSRecord(name=name, type=record_type, content="")
        ):
            self.logger.warning(
                f"Record {record_type.value} {name} not owned by us, skipping delete"
            )
            return False

        self.logger.info(f"Deleting {record_type.value} {name}")

        if self.config.dry_run:
            self.logger.info("[DRY RUN] Would delete record")
            return True

        if self.delete_record(zone_id, record.record_id):
            return self._delete_ownership(zone_id, name, record_type)
        return False

    def _check_ownership(self, zone_id: str, record: DNSRecord) -> bool:
        """Check if we own a record via its ownership TXT record"""
        ownership_name = record.ownership_record_name
        ownership_records = self.list_records(zone_id, RecordType.TXT, ownership_name)

        for txt_record in ownership_records:
            if self._is_owned_by_us(txt_record.content):
                return True

        # No ownership record = not owned
        return False

    def _set_ownership(self, zone_id: str, record: DNSRecord) -> bool:
        """Create ownership TXT record for a managed record"""
        ownership_record = DNSRecord(
            name=record.ownership_record_name,
            type=RecordType.TXT,
            content=self._ownership_content(record.type),
            ttl=record.ttl,
        )

        # Check if ownership record already exists
        existing = self.list_records(zone_id, RecordType.TXT, ownership_record.name)
        if existing:
            # Update if different
            if existing[0].content != ownership_record.content:
                ownership_record.record_id = existing[0].record_id
                return self.update_record(zone_id, ownership_record)
            return True

        return self.create_record(zone_id, ownership_record)

    def _delete_ownership(
        self, zone_id: str, name: str, record_type: RecordType
    ) -> bool:
        """Delete ownership TXT record"""
        ownership_name = f"_mail-relay-owner.{name}"
        ownership_records = self.list_records(zone_id, RecordType.TXT, ownership_name)

        for txt_record in ownership_records:
            parsed = self._parse_ownership(txt_record.content)
            if parsed and parsed.get("record-type") == record_type.value:
                return self.delete_record(zone_id, txt_record.record_id)

        return True

    def list_owned_records(self, zone_id: str) -> list[DNSRecord]:
        """List all records owned by this instance in a zone"""
        owned = []

        # Find all ownership TXT records
        all_txt = self.list_records(zone_id, RecordType.TXT)

        for txt_record in all_txt:
            if not txt_record.name.startswith("_mail-relay-owner."):
                continue

            parsed = self._parse_ownership(txt_record.content)
            if not parsed or parsed.get("owner") != self.owner_id:
                continue

            # Extract original record name
            original_name = txt_record.name.replace("_mail-relay-owner.", "", 1)
            record_type = RecordType(parsed.get("record-type", "A"))

            # Find the actual record
            records = self.list_records(zone_id, record_type, original_name)
            owned.extend(records)

        return owned
