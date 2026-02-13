"""
DNSBL Service definitions.

Declarative configuration for all supported DNSBL services.
Each service is defined as a dataclass with:
- zone: DNSBL zone name
- type: "ip" or "domain"
- nameserver: authoritative NS for direct query (None = use system DNS)
- false_positives: response codes that indicate false positive
- valid_codes: only these codes mean "listed" (None = any 127.x.x.x)
- reason_map: human-readable reasons for each return code
"""

from dataclasses import dataclass, field


@dataclass
class DnsblService:
    """Configuration for a single DNSBL service."""

    zone: str
    type: str = "ip"  # "ip" or "domain"
    nameserver: str | None = None  # Direct query NS (None = system DNS)
    false_positives: set[str] = field(default_factory=set)
    valid_codes: set[str] | None = None  # None = any 127.x.x.x is valid
    reason_map: dict[str, str] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════
# IP BLACKLISTS
# ═══════════════════════════════════════════════════════════════════════════

IP_SERVICES: list[DnsblService] = [
    # ─────────────────────────────────────────────────────────────────────────
    # Spamhaus (requires direct query - blocks public resolvers)
    # ─────────────────────────────────────────────────────────────────────────
    DnsblService(
        zone="zen.spamhaus.org",
        nameserver="a.gns.spamhaus.org",
        false_positives={"127.255.255.254", "127.255.255.255"},
        reason_map={
            "127.0.0.2": "SBL (direct spam source)",
            "127.0.0.3": "CSS (spam operations)",
            "127.0.0.4": "XBL (exploits/proxies)",
            "127.0.0.5": "XBL (exploits/proxies)",
            "127.0.0.6": "XBL (exploits/proxies)",
            "127.0.0.7": "XBL (exploits/proxies)",
            "127.0.0.9": "SBL (drop list)",
            "127.0.0.10": "PBL (policy block)",
            "127.0.0.11": "PBL (ISP maintained)",
        },
    ),
    # ─────────────────────────────────────────────────────────────────────────
    # SpamCop
    # ─────────────────────────────────────────────────────────────────────────
    DnsblService(
        zone="bl.spamcop.net",
        valid_codes={"127.0.0.2"},
        reason_map={"127.0.0.2": "SpamCop reported"},
    ),
    # ─────────────────────────────────────────────────────────────────────────
    # Barracuda
    # ─────────────────────────────────────────────────────────────────────────
    DnsblService(
        zone="b.barracudacentral.org",
        valid_codes={"127.0.0.2"},
        reason_map={"127.0.0.2": "Barracuda RBL"},
    ),
    # ─────────────────────────────────────────────────────────────────────────
    # UCEProtect
    # ─────────────────────────────────────────────────────────────────────────
    DnsblService(
        zone="dnsbl-1.uceprotect.net",
        reason_map={"127.0.0.2": "Level 1 - IP listed"},
    ),
    DnsblService(
        zone="dnsbl-2.uceprotect.net",
        reason_map={"127.0.0.2": "Level 2 - Network listed"},
    ),
    DnsblService(
        zone="dnsbl-3.uceprotect.net",
        reason_map={"127.0.0.2": "Level 3 - ASN listed"},
    ),
    # ─────────────────────────────────────────────────────────────────────────
    # SpamRATS
    # ─────────────────────────────────────────────────────────────────────────
    DnsblService(
        zone="dyna.spamrats.com",
        reason_map={"127.0.0.36": "Dynamic IP"},
    ),
    DnsblService(
        zone="noptr.spamrats.com",
        reason_map={"127.0.0.37": "No reverse DNS"},
    ),
    DnsblService(
        zone="spam.spamrats.com",
        reason_map={"127.0.0.38": "Known spam source"},
    ),
    # ─────────────────────────────────────────────────────────────────────────
    # 0spam
    # ─────────────────────────────────────────────────────────────────────────
    DnsblService(zone="bl.0spam.org"),
    DnsblService(zone="rbl.0spam.org"),
    # ─────────────────────────────────────────────────────────────────────────
    # Abusix
    # ─────────────────────────────────────────────────────────────────────────
    DnsblService(
        zone="combined.mail.abusix.zone",
        reason_map={
            "127.0.0.2": "Spam source",
            "127.0.0.4": "Exploited system",
            "127.0.0.5": "Policy block",
        },
    ),
    DnsblService(
        zone="exploit.mail.abusix.zone",
        reason_map={"127.0.0.4": "Exploited system"},
    ),
    # ─────────────────────────────────────────────────────────────────────────
    # Other major lists
    # ─────────────────────────────────────────────────────────────────────────
    DnsblService(zone="bl.blocklist.de"),
    DnsblService(zone="psbl.surriel.com"),
    DnsblService(zone="all.s5h.net"),
    DnsblService(zone="truncate.gbudb.net"),
    DnsblService(zone="spam.dnsbl.anonmails.de"),
    DnsblService(zone="ips.backscatterer.org"),
    DnsblService(zone="bogons.cymru.com"),
    DnsblService(zone="torexit.dan.me.uk"),
    DnsblService(zone="tor.dan.me.uk"),
    DnsblService(zone="bl.drmx.org"),
    DnsblService(zone="dnsbl.dronebl.org"),
    DnsblService(zone="spamsources.fabel.dk"),
    DnsblService(
        zone="hostkarma.junkemailfilter.com",
        reason_map={
            "127.0.0.1": "Whitelisted",  # Not a listing!
            "127.0.0.2": "Blacklisted",
            "127.0.0.3": "Yellow (caution)",
            "127.0.0.4": "Brown (unknown)",
            "127.0.0.5": "No reverse DNS",
        },
        false_positives={"127.0.0.1"},  # White = not listed
    ),
    DnsblService(zone="dnsbl.inps.de"),
    DnsblService(zone="icm.your-freedom.de"),
    DnsblService(zone="spamrbl.imp.ch"),
    DnsblService(zone="wormrbl.imp.ch"),
    DnsblService(zone="rbl.interserver.net"),
    DnsblService(zone="mail-abuse.blacklist.jippg.org"),
    DnsblService(zone="ubl.lashback.com"),
    DnsblService(zone="bl.mailspike.net"),
    DnsblService(zone="z.mailspike.net"),
    DnsblService(zone="bl.nordspam.com"),
    DnsblService(zone="bl.nosolicitado.org"),
    DnsblService(zone="rbl.jp"),
    DnsblService(zone="rbl.schulte.org"),
    # NOTE: bl.score.senderscore.com removed - it's a reputation scoring service (0-100),
    # not a blacklist. Response 127.0.0.X means score=X, not "listed".
    DnsblService(zone="rbl.servicesnet.com"),
    DnsblService(zone="backscatter.spameatingmonkey.net"),
    DnsblService(zone="bl.spameatingmonkey.net"),
    DnsblService(zone="dnsbl.spfbl.net"),
    DnsblService(zone="bl.suomispam.net"),
    DnsblService(zone="dnsrbl.swinog.ch"),
    DnsblService(zone="dnsbl.zapbl.net"),
]


# ═══════════════════════════════════════════════════════════════════════════
# DOMAIN BLACKLISTS
# ═══════════════════════════════════════════════════════════════════════════

DOMAIN_SERVICES: list[DnsblService] = [
    # ─────────────────────────────────────────────────────────────────────────
    # Spamhaus DBL (requires direct query)
    # ─────────────────────────────────────────────────────────────────────────
    DnsblService(
        zone="dbl.spamhaus.org",
        type="domain",
        nameserver="a.gns.spamhaus.org",
        false_positives={"127.255.255.254", "127.255.255.255"},
        reason_map={
            "127.0.1.2": "Spam domain",
            "127.0.1.4": "Phishing domain",
            "127.0.1.5": "Malware domain",
            "127.0.1.6": "Botnet C&C",
            "127.0.1.102": "Abused legit spam",
            "127.0.1.103": "Abused spammed redirector",
            "127.0.1.104": "Abused legit phishing",
            "127.0.1.105": "Abused legit malware",
            "127.0.1.106": "Abused legit botnet",
        },
    ),
    # ─────────────────────────────────────────────────────────────────────────
    # URIBL (requires direct query - blocks public resolvers)
    # ─────────────────────────────────────────────────────────────────────────
    DnsblService(
        zone="multi.uribl.com",
        type="domain",
        nameserver="v.uribl.net",
        false_positives={"127.0.0.1"},  # Test/blocked response
        reason_map={
            "127.0.0.2": "URIBL black",
            "127.0.0.4": "URIBL grey",
            "127.0.0.8": "URIBL red",
        },
    ),
    # ─────────────────────────────────────────────────────────────────────────
    # SURBL (requires direct query - blocks public resolvers)
    # ─────────────────────────────────────────────────────────────────────────
    DnsblService(
        zone="multi.surbl.org",
        type="domain",
        nameserver="green.surbl.org",
        false_positives={"127.0.0.254"},  # Blocked response
        reason_map={
            "127.0.0.2": "SC (SpamCop)",
            "127.0.0.4": "WS (sa-blacklist)",
            "127.0.0.8": "PH (phishing)",
            "127.0.0.16": "MW (malware)",
            "127.0.0.32": "AB (abuse)",
            "127.0.0.64": "CR (cracked)",
        },
    ),
    # ─────────────────────────────────────────────────────────────────────────
    # Nordspam
    # ─────────────────────────────────────────────────────────────────────────
    DnsblService(zone="dbl.nordspam.com", type="domain"),
    # ─────────────────────────────────────────────────────────────────────────
    # SpamEatingMonkey
    # ─────────────────────────────────────────────────────────────────────────
    DnsblService(zone="fresh.spameatingmonkey.net", type="domain"),
    DnsblService(zone="uribl.spameatingmonkey.net", type="domain"),
    DnsblService(zone="urired.spameatingmonkey.net", type="domain"),
]


# ═══════════════════════════════════════════════════════════════════════════
# Service lookup helpers
# ═══════════════════════════════════════════════════════════════════════════

# Build lookup dict for fast access
_SERVICE_MAP: dict[str, DnsblService] = {
    **{s.zone: s for s in IP_SERVICES},
    **{s.zone: s for s in DOMAIN_SERVICES},
}


def get_service(zone: str) -> DnsblService | None:
    """Get service config by zone name."""
    return _SERVICE_MAP.get(zone)


def get_all_ip_zones() -> list[str]:
    """Get all IP blacklist zones."""
    return [s.zone for s in IP_SERVICES]


def get_all_domain_zones() -> list[str]:
    """Get all domain blacklist zones."""
    return [s.zone for s in DOMAIN_SERVICES]
