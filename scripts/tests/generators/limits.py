"""
Limits and special case test generators.

Tests for message size limits, multi-domain, and regex patterns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..base import Tag, TestCase, TestGenerator
from ..registry import register

if TYPE_CHECKING:
    from ..config import TestConfig


@register
class MessageSizeTests(TestGenerator):
    """Tests for message size limit enforcement."""

    tags = {Tag.SIZE}
    priority = 80

    def is_applicable(self, config: TestConfig) -> bool:
        return config.haraka.max_message_size > 0

    def generate(self, config: TestConfig) -> list[TestCase]:
        tests = []
        allowed_sender = config.get_allowed_sender()
        max_size = config.haraka.max_message_size

        # Test: Message within size limit (should ACCEPT)
        tests.append(
            TestCase(
                name="size_within_limit",
                description=f"Size: Message within limit (1KB body, max={max_size}) (should ACCEPT)",
                network="internal",
                mail_from=allowed_sender,
                rcpt_to="recipient@gmail.com",
                body_size=1024,
                expect_accept=True,
                tags={Tag.SIZE},
            )
        )

        # Test: Message exceeding size limit (should REJECT)
        # Only test if max_size is less than 10MB (to avoid timeout issues)
        if max_size < 10 * 1024 * 1024:
            oversized = max_size + 1024  # 1KB over limit
            tests.append(
                TestCase(
                    name="size_exceeds_limit",
                    description=f"Size: Message exceeds limit ({oversized} bytes, max={max_size}) (should REJECT)",
                    network="internal",
                    mail_from=allowed_sender,
                    rcpt_to="recipient@gmail.com",
                    body_size=oversized,
                    expect_accept=False,
                    expect_code=552,  # 552 Message size exceeds limit
                    tags={Tag.SIZE},
                )
            )

        return tests


@register
class RegexPatternTests(TestGenerator):
    """Tests for regex patterns in senderValidation.allowedFrom."""

    tags = {Tag.REGEX, Tag.OUTBOUND}
    priority = 81

    def is_applicable(self, config: TestConfig) -> bool:
        sv = config.mail.sender_validation
        return sv.enabled and len(sv.get_regex_patterns()) > 0

    def generate(self, config: TestConfig) -> list[TestCase]:
        tests = []
        primary_domain = config.mail.primary_domain
        regex_patterns = config.mail.sender_validation.get_regex_patterns()

        # Test: Address matching regex pattern (should ACCEPT)
        for i, pattern in enumerate(regex_patterns[:2]):  # Test first 2 patterns
            # Remove leading / and trailing / from regex
            clean_pattern = pattern.strip("/")

            # Generate a test address that should match
            # Common patterns: /.*@notifications\./, /noreply@/, /^service-/
            if "notification" in clean_pattern.lower():
                test_match_addr = f"alert@notifications.{primary_domain}"
            elif "noreply" in clean_pattern.lower():
                test_match_addr = f"noreply@{primary_domain}"
            elif "service" in clean_pattern.lower():
                test_match_addr = f"service-alerts@{primary_domain}"
            else:
                # Generic: assume domain pattern
                test_match_addr = f"test@{primary_domain}"

            tests.append(
                TestCase(
                    name=f"regex_pattern_match_{i + 1}",
                    description=f"Regex: Address matching pattern {pattern} (should ACCEPT)",
                    network="internal",
                    mail_from=test_match_addr,
                    rcpt_to="recipient@gmail.com",
                    expect_accept=True,
                    tags={Tag.REGEX},
                )
            )

        return tests


@register
class MultiDomainTests(TestGenerator):
    """Tests for multiple configured domains."""

    tags = {Tag.MULTI_DOMAIN}
    priority = 82

    def is_applicable(self, config: TestConfig) -> bool:
        return config.mail.secondary_domain is not None

    def generate(self, config: TestConfig) -> list[TestCase]:
        tests = []
        secondary_domain = config.mail.secondary_domain
        sv = config.mail.sender_validation

        if not sv.is_strict_mode:
            # Test: Send from secondary domain (should ACCEPT in default mode)
            tests.append(
                TestCase(
                    name="multi_domain_secondary",
                    description=f"Multi-domain: Send from secondary domain {secondary_domain} (should ACCEPT)",
                    network="internal",
                    mail_from=f"sender@{secondary_domain}",
                    rcpt_to="recipient@gmail.com",
                    expect_accept=True,
                    tags={Tag.MULTI_DOMAIN},
                )
            )

            # Test: Inbound to secondary domain (if inbound enabled)
            if config.inbound.enabled:
                tests.append(
                    TestCase(
                        name="multi_domain_inbound_postmaster",
                        description=f"Multi-domain: Inbound to postmaster@{secondary_domain} (should ACCEPT)",
                        network="external",
                        mail_from="",
                        rcpt_to=f"postmaster@{secondary_domain}",
                        expect_accept=True,
                        tags={Tag.MULTI_DOMAIN, Tag.INBOUND},
                    )
                )
        else:
            # Strict mode: check if secondary domain is in allowedFrom
            secondary_allowed = any(
                addr.endswith(f"@{secondary_domain}") or addr == secondary_domain
                for addr in sv.allowed_from
                if not addr.startswith("/")
            )

            if not secondary_allowed:
                tests.append(
                    TestCase(
                        name="multi_domain_strict_secondary_rejected",
                        description=f"Multi-domain strict: Send from {secondary_domain} not in allowedFrom (should REJECT)",
                        network="internal",
                        mail_from=f"sender@{secondary_domain}",
                        rcpt_to="recipient@gmail.com",
                        expect_accept=False,
                        expect_code=550,
                        tags={Tag.MULTI_DOMAIN},
                    )
                )

        return tests
