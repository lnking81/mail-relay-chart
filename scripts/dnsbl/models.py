"""Data models for blacklist monitoring."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class BlacklistResult:
    """Result of a DNSBL check."""

    target: str  # IP address or domain being checked
    target_type: str  # "ip" or "domain"
    dnsbl: str
    listed: bool
    return_code: str = ""
    reason: str = ""
    check_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def ip(self) -> str:
        """Backward compatibility - return target for IP checks."""
        return self.target if self.target_type == "ip" else ""
