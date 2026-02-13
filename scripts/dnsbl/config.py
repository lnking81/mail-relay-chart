"""Configuration for blacklist monitoring."""

import os
from dataclasses import dataclass, field


@dataclass
class BlacklistConfig:
    """Configuration for blacklist monitoring."""

    interval: int = 3600  # Check interval in seconds
    metrics_port: int = 8095

    # Custom DNS server (optional, plugins handle direct queries)
    dns_server: str = ""

    # Direct query mode - query authoritative NS servers directly
    # This bypasses public resolvers and works with premium DNSBLs
    direct_query: bool = True

    # IP-based DNSBL lists to check
    lists: list[str] = field(default_factory=list)

    # Domain-based blacklists (URIBL/DBL)
    domain_lists: list[str] = field(default_factory=list)

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
        """Create config from environment variables."""
        # IP blacklists - will be populated from registry if empty
        lists_env = os.environ.get("BLACKLIST_LISTS", "")
        lists = (
            [x.strip() for x in lists_env.split(",") if x.strip()] if lists_env else []
        )

        custom_lists = os.environ.get("BLACKLIST_CUSTOM_LISTS", "")
        if custom_lists:
            lists.extend([x.strip() for x in custom_lists.split(",") if x.strip()])

        # Domain blacklists - will be populated from registry if empty
        domain_lists_env = os.environ.get("BLACKLIST_DOMAIN_LISTS", "")
        domain_lists = (
            [x.strip() for x in domain_lists_env.split(",") if x.strip()]
            if domain_lists_env
            else []
        )

        custom_domain_lists = os.environ.get("BLACKLIST_CUSTOM_DOMAIN_LISTS", "")
        if custom_domain_lists:
            domain_lists.extend(
                [x.strip() for x in custom_domain_lists.split(",") if x.strip()]
            )

        # Domains to check
        domains_env = os.environ.get("BLACKLIST_DOMAINS", "")
        domains = (
            [d.strip() for d in domains_env.split(",") if d.strip()]
            if domains_env
            else []
        )

        recipients_env = os.environ.get("BLACKLIST_ALERT_RECIPIENTS", "")
        recipients = (
            [r.strip() for r in recipients_env.split(",") if r.strip()]
            if recipients_env
            else []
        )

        return cls(
            interval=int(os.environ.get("BLACKLIST_INTERVAL", "3600")),
            metrics_port=int(os.environ.get("BLACKLIST_METRICS_PORT", "8095")),
            dns_server=os.environ.get("BLACKLIST_DNS_SERVER", ""),
            direct_query=os.environ.get("BLACKLIST_DIRECT_QUERY", "true").lower()
            == "true",
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
