"""
Base classes for mail relay test suite.

Provides abstract TestGenerator class and core dataclasses.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, ClassVar, Optional

if TYPE_CHECKING:
    from .config import TestConfig


class Tag(Enum):
    """Test category tags for filtering."""

    OUTBOUND = auto()  # Outbound relay tests
    INBOUND = auto()  # Inbound mail handling tests
    SECURITY = auto()  # SPF/DKIM/DMARC verification tests
    RELAY = auto()  # Open relay protection tests
    DELIVERY = auto()  # Legitimate delivery flow tests
    AUTH = auto()  # SMTP AUTH tests
    TLS = auto()  # TLS/STARTTLS tests
    SIZE = auto()  # Message size limit tests
    MULTI_DOMAIN = auto()  # Multi-domain tests
    REGEX = auto()  # Regex pattern tests
    NO_INBOUND = auto()  # Tests for inbound disabled scenario


@dataclass
class TestResult:
    """Result of a single test case."""

    name: str
    passed: bool
    expected: str
    actual: str
    details: str = ""


@dataclass
class TestCase:
    """Definition of a test case.

    Attributes:
        name: Unique test identifier.
        description: Human-readable test description.
        network: Network type - "internal" or "external".
        mail_from: SMTP MAIL FROM address (empty for null sender).
        rcpt_to: SMTP RCPT TO address.
        from_header: From header value (defaults to mail_from).
        subject: Email subject line.
        body: Email body text.
        expect_accept: True if message should be accepted (250), False for rejection.
        expect_code: Specific SMTP response code to expect.
        skip_data: Skip DATA phase (test envelope only).
        headers: Additional headers to include.
        use_tls: Use STARTTLS.
        require_tls: Expect TLS to be available.
        auth_user: SMTP AUTH username.
        auth_pass: SMTP AUTH password.
        expect_auth_fail: Expect AUTH to fail.
        body_size: Generate body of exact size in bytes.
        tags: Test category tags for filtering.
    """

    name: str
    description: str
    network: str  # "internal" or "external"
    mail_from: str
    rcpt_to: str
    from_header: Optional[str] = None
    subject: str = "Test Email"
    body: str = "This is a test email."
    expect_accept: bool = True
    expect_code: Optional[int] = None
    skip_data: bool = False
    headers: dict[str, str] = field(default_factory=lambda: {})
    # TLS options
    use_tls: bool = False
    require_tls: bool = False
    # AUTH options
    auth_user: Optional[str] = None
    auth_pass: Optional[str] = None
    expect_auth_fail: bool = False
    # Size testing
    body_size: Optional[int] = None
    # Tags for filtering
    tags: set[Tag] = field(default_factory=lambda: set())

    def __post_init__(self) -> None:
        if self.from_header is None:
            self.from_header = self.mail_from


class TestGenerator(ABC):
    """Abstract base class for test generators.

    Each generator produces tests for a specific category/feature.
    Generators are auto-discovered and registered via the registry.

    Class Attributes:
        tags: Set of tags that categorize this generator's tests.
        priority: Order in which generators run (lower = earlier).
    """

    tags: ClassVar[set[Tag]] = set()
    priority: ClassVar[int] = 100

    @abstractmethod
    def is_applicable(self, config: TestConfig) -> bool:
        """Check if this generator should produce tests for the given config.

        Args:
            config: Parsed test configuration.

        Returns:
            True if this generator's tests are applicable.
        """
        ...

    @abstractmethod
    def generate(self, config: TestConfig) -> list[TestCase]:
        """Generate test cases for the given configuration.

        Args:
            config: Parsed test configuration.

        Returns:
            List of TestCase objects.
        """
        ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(tags={self.tags}, priority={self.priority})"
