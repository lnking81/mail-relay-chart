"""
Outbound mail test generators.

Tests for mail sent from internal/trusted networks through the relay.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..base import Tag, TestCase, TestGenerator
from ..registry import register

if TYPE_CHECKING:
    from ..config import TestConfig


@register
class OutboundAllowedDomainTests(TestGenerator):
    """Tests for sending from allowed sender addresses."""

    tags = {Tag.OUTBOUND}
    priority = 10

    def is_applicable(self, config: TestConfig) -> bool:
        return True  # Always applicable

    def generate(self, config: TestConfig) -> list[TestCase]:
        allowed_sender = config.get_allowed_sender()
        return [
            TestCase(
                name="outbound_allowed_domain",
                description=f"Outbound: Send from allowed sender {allowed_sender} (should ACCEPT)",
                network="internal",
                mail_from=allowed_sender,
                rcpt_to="recipient@gmail.com",
                expect_accept=True,
                tags={Tag.OUTBOUND},
            )
        ]


@register
class OutboundDisallowedDomainTests(TestGenerator):
    """Tests for sender validation blocking unauthorized senders."""

    tags = {Tag.OUTBOUND}
    priority = 11

    def is_applicable(self, config: TestConfig) -> bool:
        return config.mail.sender_validation.enabled

    def generate(self, config: TestConfig) -> list[TestCase]:
        tests: list[TestCase] = []
        sv = config.mail.sender_validation
        primary_domain = config.mail.primary_domain

        if sv.is_strict_mode:
            # Strict mode: even our domain is blocked if not in allowedFrom
            disallowed_sender = f"notallowed@{primary_domain}"
            desc = f"Outbound: Send from domain (not in allowedFrom) {disallowed_sender} (should REJECT)"
        else:
            # Default mode: external domains are blocked
            disallowed_sender = "hacker@gmail.com"
            desc = "Outbound: Send from non-whitelisted domain (should REJECT)"

        tests.append(
            TestCase(
                name="outbound_disallowed_domain",
                description=desc,
                network="internal",
                mail_from=disallowed_sender,
                rcpt_to="victim@example.org",
                expect_accept=False,
                expect_code=550,
                tags={Tag.OUTBOUND},
            )
        )

        return tests


@register
class OutboundForbiddenSenderTests(TestGenerator):
    """Tests for explicitly forbidden sender addresses."""

    tags = {Tag.OUTBOUND}
    priority = 12

    def is_applicable(self, config: TestConfig) -> bool:
        sv = config.mail.sender_validation
        return sv.enabled and len(sv.forbidden_from) > 0

    def generate(self, config: TestConfig) -> list[TestCase]:
        forbidden = config.mail.sender_validation.forbidden_from
        primary_domain = config.mail.primary_domain

        # Use first forbidden pattern or create test address
        forbidden_addr = forbidden[0]
        if forbidden_addr.startswith("/"):
            # Regex - create matching address
            forbidden_addr = f"ceo@{primary_domain}"

        return [
            TestCase(
                name="outbound_forbidden_sender",
                description=f"Outbound: Send from forbidden address {forbidden_addr} (should REJECT)",
                network="internal",
                mail_from=forbidden_addr,
                rcpt_to="recipient@gmail.com",
                expect_accept=False,
                expect_code=550,
                tags={Tag.OUTBOUND},
            )
        ]


@register
class OutboundFromHeaderMismatchTests(TestGenerator):
    """Tests for From header domain validation."""

    tags = {Tag.OUTBOUND}
    priority = 13

    def is_applicable(self, config: TestConfig) -> bool:
        sv = config.mail.sender_validation
        return sv.enabled and sv.check_from_header

    def generate(self, config: TestConfig) -> list[TestCase]:
        allowed_sender = config.get_allowed_sender()

        return [
            TestCase(
                name="outbound_from_header_mismatch",
                description="Outbound: From header domain != MAIL FROM domain (should REJECT)",
                network="internal",
                mail_from=allowed_sender,
                from_header="spoofed@other-domain.com",
                rcpt_to="recipient@gmail.com",
                expect_accept=False,
                tags={Tag.OUTBOUND},
            )
        ]
