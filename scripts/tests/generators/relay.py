"""
Open relay protection test generators.

Tests to verify the server is not an open relay.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..base import Tag, TestCase, TestGenerator
from ..registry import register

if TYPE_CHECKING:
    from ..config import TestConfig


@register
class OpenRelayProtectionTests(TestGenerator):
    """Tests for open relay protection.

    These tests verify that external connections cannot relay
    mail through the server to arbitrary destinations.
    """

    tags = {Tag.RELAY}
    priority = 40

    def is_applicable(self, config: TestConfig) -> bool:
        return True  # Always applicable - critical security tests

    def generate(self, config: TestConfig) -> list[TestCase]:
        tests: list[TestCase] = []
        primary_domain = config.mail.primary_domain
        allowed_sender = config.get_allowed_sender()

        # Test: Classic open relay - external sender to external recipient
        tests.append(
            TestCase(
                name="relay_external_to_external",
                description="Open Relay: External sender to external recipient (should REJECT)",
                network="external",
                mail_from="spammer@evil.com",
                rcpt_to="victim@other-domain.com",
                expect_accept=False,
                expect_code=550,
                tags={Tag.RELAY},
            )
        )

        # Test: Spoofed MAIL FROM - external connection spoofing our domain
        tests.append(
            TestCase(
                name="relay_spoofed_our_domain",
                description=f"Open Relay: External spoofing our domain {allowed_sender} to relay (should REJECT)",
                network="external",
                mail_from=allowed_sender,
                rcpt_to="victim@other-domain.com",
                expect_accept=False,
                expect_code=550,
                tags={Tag.RELAY},
            )
        )

        # Test: NULL sender relay attempt - spammers often use <> to bypass checks
        tests.append(
            TestCase(
                name="relay_null_sender",
                description="Open Relay: NULL sender <> to external recipient (should REJECT)",
                network="external",
                mail_from="",  # NULL sender <>
                rcpt_to="victim@other-domain.com",
                expect_accept=False,
                expect_code=550,
                tags={Tag.RELAY},
            )
        )

        # Test: Spoofed postmaster - attacker spoofs postmaster@ to relay
        tests.append(
            TestCase(
                name="relay_spoofed_postmaster",
                description=f"Open Relay: Spoofed postmaster@{primary_domain} to relay (should REJECT)",
                network="external",
                mail_from=f"postmaster@{primary_domain}",
                rcpt_to="victim@other-domain.com",
                expect_accept=False,
                expect_code=550,
                tags={Tag.RELAY},
            )
        )

        # Test: Spoofed mailer-daemon - common bounce address spoofing
        tests.append(
            TestCase(
                name="relay_spoofed_mailer_daemon",
                description="Open Relay: Spoofed mailer-daemon to relay (should REJECT)",
                network="external",
                mail_from=f"mailer-daemon@{primary_domain}",
                rcpt_to="victim@other-domain.com",
                expect_accept=False,
                expect_code=550,
                tags={Tag.RELAY},
            )
        )

        # Test: External to Gmail specifically (common spam target)
        tests.append(
            TestCase(
                name="relay_to_gmail",
                description="Open Relay: External to gmail.com (should REJECT)",
                network="external",
                mail_from="random@external.com",
                rcpt_to="victim@gmail.com",
                expect_accept=False,
                expect_code=550,
                tags={Tag.RELAY},
            )
        )

        # Test: External with localhost MAIL FROM (sometimes used in attacks)
        tests.append(
            TestCase(
                name="relay_localhost_sender",
                description="Open Relay: MAIL FROM localhost to external (should REJECT)",
                network="external",
                mail_from="root@localhost",
                rcpt_to="victim@other-domain.com",
                expect_accept=False,
                expect_code=550,
                tags={Tag.RELAY},
            )
        )

        return tests
