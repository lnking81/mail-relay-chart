"""
Configuration dataclasses for mail relay test suite.

Provides typed configuration objects parsed from Helm values.yaml.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


def deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dictionaries. Override values take precedence."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


@dataclass
class DomainConfig:
    """Configuration for a single domain."""

    name: str
    dkim_selector: str = "mail"


@dataclass
class RelayConfig:
    """Upstream relay configuration."""

    enabled: bool = False
    host: str = ""
    port: int = 587
    tls: bool = True


@dataclass
class SenderValidationConfig:
    """Sender validation settings."""

    enabled: bool = False
    check_from_header: bool = False
    allowed_from: list[str] = field(default_factory=list)
    forbidden_from: list[str] = field(default_factory=list)

    @property
    def is_strict_mode(self) -> bool:
        """Check if strict mode (explicit allowedFrom list) is enabled."""
        return len(self.allowed_from) > 0

    def get_regex_patterns(self) -> list[str]:
        """Get regex patterns from allowedFrom list."""
        return [p for p in self.allowed_from if p.startswith("/")]

    def get_first_allowed_sender(self, fallback_domain: str) -> str:
        """Get first non-regex allowed sender address."""
        for addr in self.allowed_from:
            if not addr.startswith("/"):
                return addr
        return f"sender@{fallback_domain}"


@dataclass
class MailConfig:
    """Mail server settings."""

    hostname: str = "mail.example.com"
    domains: list[DomainConfig] = field(default_factory=list)
    trusted_networks: list[str] = field(default_factory=list)
    sender_validation: SenderValidationConfig = field(
        default_factory=SenderValidationConfig
    )
    relay: RelayConfig = field(default_factory=RelayConfig)

    @property
    def primary_domain(self) -> str:
        """Get primary domain name."""
        if self.domains:
            return self.domains[0].name
        return self.hostname.split(".", 1)[-1]

    @property
    def secondary_domain(self) -> Optional[str]:
        """Get secondary domain if multiple are configured."""
        return self.domains[1].name if len(self.domains) > 1 else None

    @property
    def domain_names(self) -> list[str]:
        """Get list of all domain names."""
        return [d.name for d in self.domains]


@dataclass
class SpfConfig:
    """SPF verification settings."""

    enabled: bool = False
    reject_fail: bool = False
    reject_softfail: bool = False


@dataclass
class DkimVerifyConfig:
    """DKIM verification settings."""

    enabled: bool = False


@dataclass
class DmarcVerifyConfig:
    """DMARC verification settings."""

    enabled: bool = False
    reject_on_fail: bool = False
    quarantine_on_fail: bool = False


@dataclass
class SecurityConfig:
    """Inbound security settings."""

    spf: SpfConfig = field(default_factory=SpfConfig)
    dkim: DkimVerifyConfig = field(default_factory=DkimVerifyConfig)
    dmarc: DmarcVerifyConfig = field(default_factory=DmarcVerifyConfig)


@dataclass
class BounceConfig:
    """Bounce processing settings."""

    enabled: bool = True
    verp_prefix: str = "bounce+"
    use_sender_domain: bool = True
    hmac_secret: str = ""
    require_hmac: bool = True
    max_age_days: int = 7

    @property
    def prefix_without_plus(self) -> str:
        """Get VERP prefix without trailing +."""
        return self.verp_prefix.rstrip("+")


@dataclass
class InboundConfig:
    """Inbound mail handling settings."""

    enabled: bool = False
    client_id_header: str = "X-Message-ID"
    recipients: list[str] = field(default_factory=list)
    bounce: BounceConfig = field(default_factory=BounceConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)


@dataclass
class AuthConfig:
    """SMTP AUTH settings."""

    enabled: bool = False
    methods: str = "PLAIN,LOGIN"
    require_tls: bool = True
    constrain_sender: bool = True
    users: dict[str, str] = field(default_factory=dict)

    @property
    def first_user(self) -> Optional[tuple[str, str]]:
        """Get first user credentials for testing."""
        if self.users:
            username = list(self.users.keys())[0]
            return username, self.users[username]
        return None


@dataclass
class TlsConfig:
    """TLS/SSL settings."""

    enabled: bool = False
    require_tls: bool = False
    min_version: str = "TLSv1.2"
    advertise_to_all: bool = True


@dataclass
class HarakaConfig:
    """Haraka server settings."""

    smtp_banner: str = "ready"
    max_message_size: int = 26214400
    concurrency: int = 50


@dataclass
class TestConfig:
    """Root configuration for mail relay tests.

    Provides typed access to all configuration values needed for test generation.
    """

    mail: MailConfig = field(default_factory=MailConfig)
    inbound: InboundConfig = field(default_factory=InboundConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    tls: TlsConfig = field(default_factory=TlsConfig)
    haraka: HarakaConfig = field(default_factory=HarakaConfig)

    # Raw values dict for edge cases
    _raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_values(cls, values: dict) -> TestConfig:
        """Create TestConfig from parsed Helm values dict."""
        mail_dict = values.get("mail", {})
        inbound_dict = values.get("inbound", {})
        auth_dict = values.get("auth", {})
        tls_dict = values.get("tls", {})
        haraka_dict = values.get("haraka", {})

        # Parse domains
        domains = []
        for d in mail_dict.get("domains", []):
            if isinstance(d, dict):
                domains.append(
                    DomainConfig(
                        name=d.get("name", ""),
                        dkim_selector=d.get("dkimSelector", "mail"),
                    )
                )
            elif d:
                domains.append(DomainConfig(name=str(d)))

        # Parse sender validation
        sv_dict = mail_dict.get("senderValidation", {})
        sender_validation = SenderValidationConfig(
            enabled=sv_dict.get("enabled", False),
            check_from_header=sv_dict.get("checkFromHeader", False),
            allowed_from=sv_dict.get("allowedFrom", []),
            forbidden_from=sv_dict.get("forbiddenFrom", []),
        )

        # Parse relay
        relay_dict = mail_dict.get("relay", {})
        relay = RelayConfig(
            enabled=relay_dict.get("enabled", False),
            host=relay_dict.get("host", ""),
            port=relay_dict.get("port", 587),
            tls=relay_dict.get("tls", True),
        )

        mail = MailConfig(
            hostname=mail_dict.get("hostname", "mail.example.com"),
            domains=domains,
            trusted_networks=mail_dict.get("trustedNetworks", []),
            sender_validation=sender_validation,
            relay=relay,
        )

        # Parse inbound security
        security_dict = inbound_dict.get("security", {})
        spf_dict = security_dict.get("spf", {})
        dkim_dict = security_dict.get("dkim", {})
        dmarc_dict = security_dict.get("dmarc", {})

        security = SecurityConfig(
            spf=SpfConfig(
                enabled=spf_dict.get("enabled", False),
                reject_fail=spf_dict.get("rejectFail", False),
                reject_softfail=spf_dict.get("rejectSoftfail", False),
            ),
            dkim=DkimVerifyConfig(enabled=dkim_dict.get("enabled", False)),
            dmarc=DmarcVerifyConfig(
                enabled=dmarc_dict.get("enabled", False),
                reject_on_fail=dmarc_dict.get("rejectOnFail", False),
                quarantine_on_fail=dmarc_dict.get("quarantineOnFail", False),
            ),
        )

        # Parse bounce
        bounce_dict = inbound_dict.get("bounce", {})
        bounce = BounceConfig(
            enabled=bounce_dict.get("enabled", True),
            verp_prefix=bounce_dict.get("verpPrefix", "bounce+"),
            use_sender_domain=bounce_dict.get("useSenderDomain", True),
            hmac_secret=bounce_dict.get("hmacSecret", ""),
            require_hmac=bounce_dict.get("requireHmac", True),
            max_age_days=bounce_dict.get("maxAgeDays", 7),
        )

        inbound = InboundConfig(
            enabled=inbound_dict.get("enabled", False),
            client_id_header=inbound_dict.get("clientIdHeader", "X-Message-ID"),
            recipients=inbound_dict.get("recipients", []),
            bounce=bounce,
            security=security,
        )

        auth = AuthConfig(
            enabled=auth_dict.get("enabled", False),
            methods=auth_dict.get("methods", "PLAIN,LOGIN"),
            require_tls=auth_dict.get("requireTls", True),
            constrain_sender=auth_dict.get("constrainSender", True),
            users=auth_dict.get("users", {}),
        )

        tls = TlsConfig(
            enabled=tls_dict.get("enabled", False),
            require_tls=tls_dict.get("requireTls", False),
            min_version=tls_dict.get("minVersion", "TLSv1.2"),
            advertise_to_all=tls_dict.get("advertiseToAll", True),
        )

        haraka = HarakaConfig(
            smtp_banner=haraka_dict.get("smtpBanner", "ready"),
            max_message_size=haraka_dict.get("maxMessageSize", 26214400),
            concurrency=haraka_dict.get("concurrency", 50),
        )

        return cls(
            mail=mail,
            inbound=inbound,
            auth=auth,
            tls=tls,
            haraka=haraka,
            _raw=values,
        )

    @classmethod
    def load(
        cls,
        values_file: str,
        chart_dir: Optional[str] = None,
        default_values_path: str = "chart/values.yaml",
    ) -> TestConfig:
        """Load and merge values files, returning TestConfig.

        Args:
            values_file: Path to override values file.
            chart_dir: Path to chart directory (for default values).
            default_values_path: Relative path to default values.yaml.
        """
        # Find default values.yaml
        if chart_dir:
            default_path = Path(chart_dir) / "values.yaml"
        else:
            # Try relative to script location
            script_dir = Path(__file__).parent.parent
            default_path = script_dir / default_values_path

            # Try relative to current directory
            if not default_path.exists():
                default_path = Path(default_values_path)

        # Load default values
        default_values: dict[str, Any] = {}
        if default_path.exists():
            with open(default_path) as f:
                default_values = yaml.safe_load(f) or {}

        # Load override values
        with open(values_file) as f:
            override_values = yaml.safe_load(f) or {}

        # Deep merge
        merged = deep_merge(default_values, override_values)
        return cls.from_values(merged)

    def get_allowed_sender(self) -> str:
        """Get an allowed sender address for testing."""
        if self.mail.sender_validation.is_strict_mode:
            return self.mail.sender_validation.get_first_allowed_sender(
                self.mail.primary_domain
            )
        return f"sender@{self.mail.primary_domain}"
