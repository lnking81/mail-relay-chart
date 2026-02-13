"""
Security test generators.

Tests for SPF, DKIM, and DMARC verification on inbound mail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..base import Tag, TestCase, TestGenerator
from ..registry import register

if TYPE_CHECKING:
    from ..config import TestConfig


@register
class SpfTests(TestGenerator):
    """Tests for SPF verification."""

    tags = {Tag.SECURITY, Tag.INBOUND}
    priority = 30

    def is_applicable(self, config: TestConfig) -> bool:
        return config.inbound.enabled and config.inbound.security.spf.enabled

    def generate(self, config: TestConfig) -> list[TestCase]:
        tests: list[TestCase] = []
        primary_domain = config.mail.primary_domain
        spf = config.inbound.security.spf

        # Test: SPF HARDFAIL - send from domain with "v=spf1 -all" policy
        # example.org has "v=spf1 -all" - IANA reserved, no mail allowed
        if spf.reject_fail:
            tests.append(
                TestCase(
                    name="inbound_spf_fail_rejected",
                    description="Inbound: SPF HARDFAIL (example.org has -all, should REJECT)",
                    network="external",
                    mail_from="test@example.org",  # example.org: v=spf1 -all
                    rcpt_to=f"postmaster@{primary_domain}",
                    expect_accept=False,
                    expect_code=550,
                    tags={Tag.SECURITY},
                )
            )
        else:
            tests.append(
                TestCase(
                    name="inbound_spf_fail_accepted",
                    description="Inbound: SPF HARDFAIL (rejectFail=false, should ACCEPT)",
                    network="external",
                    mail_from="test@example.org",  # example.org: v=spf1 -all
                    rcpt_to=f"postmaster@{primary_domain}",
                    expect_accept=True,
                    tags={Tag.SECURITY},
                )
            )

        # Test: SPF NONE - domain without SPF record
        # Use .invalid TLD (RFC 2606) - guaranteed no DNS records
        tests.append(
            TestCase(
                name="inbound_spf_none",
                description="Inbound: SPF NONE (no SPF record, should ACCEPT)",
                network="external",
                mail_from="sender@no-spf-test.invalid",  # .invalid has no DNS
                rcpt_to=f"postmaster@{primary_domain}",
                expect_accept=True,
                tags={Tag.SECURITY},
            )
        )

        return tests


@register
class DkimVerifyTests(TestGenerator):
    """Tests for DKIM signature verification."""

    tags = {Tag.SECURITY, Tag.INBOUND}
    priority = 31

    def is_applicable(self, config: TestConfig) -> bool:
        return config.inbound.enabled and config.inbound.security.dkim.enabled

    def generate(self, config: TestConfig) -> list[TestCase]:
        primary_domain = config.mail.primary_domain

        # Test: Mail without DKIM signature
        # Should be accepted (DKIM verification only checks IF signature present)
        return [
            TestCase(
                name="inbound_dkim_no_signature",
                description="Inbound: No DKIM signature (should ACCEPT, DKIM optional)",
                network="external",
                mail_from="",  # Null sender bypasses SPF
                rcpt_to=f"postmaster@{primary_domain}",
                expect_accept=True,
                tags={Tag.SECURITY},
            )
        ]


@register
class DmarcTests(TestGenerator):
    """Tests for DMARC verification."""

    tags = {Tag.SECURITY, Tag.INBOUND}
    priority = 32

    def is_applicable(self, config: TestConfig) -> bool:
        return config.inbound.enabled and config.inbound.security.dmarc.enabled

    def generate(self, config: TestConfig) -> list[TestCase]:
        tests: list[TestCase] = []
        primary_domain = config.mail.primary_domain
        dmarc = config.inbound.security.dmarc

        # DMARC checks alignment between From header and MAIL FROM/DKIM domain
        # For DMARC test, use null sender (bypasses SPF) with spoofed From header

        if dmarc.reject_on_fail:
            # Test: DMARC FAIL with p=reject policy domain
            tests.append(
                TestCase(
                    name="inbound_dmarc_fail_rejected",
                    description="Inbound: DMARC FAIL (spoofed From header, p=reject, should REJECT)",
                    network="external",
                    mail_from="",  # Null sender - no SPF alignment
                    from_header="security@paypal.com",  # paypal.com has p=reject
                    rcpt_to=f"postmaster@{primary_domain}",
                    expect_accept=False,
                    expect_code=550,
                    tags={Tag.SECURITY},
                )
            )
        else:
            # Test: DMARC FAIL but rejectOnFail=false
            tests.append(
                TestCase(
                    name="inbound_dmarc_fail_accepted",
                    description="Inbound: DMARC FAIL (rejectOnFail=false, should ACCEPT)",
                    network="external",
                    mail_from="",  # Null sender
                    from_header="ceo@bigbank.com",  # Spoofed From header
                    rcpt_to=f"postmaster@{primary_domain}",
                    expect_accept=True,
                    tags={Tag.SECURITY},
                )
            )

        return tests
