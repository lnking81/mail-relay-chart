"""
Inbound mail test generators.

Tests for mail received from external networks (bounces, FBL, postmaster).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..base import Tag, TestCase, TestGenerator
from ..registry import register

if TYPE_CHECKING:
    from ..config import TestConfig


@register
class InboundBounceTests(TestGenerator):
    """Tests for bounce handling when inbound is enabled."""

    tags = {Tag.INBOUND}
    priority = 20

    def is_applicable(self, config: TestConfig) -> bool:
        return config.inbound.enabled

    def generate(self, config: TestConfig) -> list[TestCase]:
        tests: list[TestCase] = []
        primary_domain = config.mail.primary_domain
        bounce_prefix = config.inbound.bounce.prefix_without_plus
        require_hmac = config.inbound.bounce.require_hmac

        if require_hmac:
            # Test: Fake bounce should be REJECTED (invalid HMAC)
            tests.append(
                TestCase(
                    name="inbound_fake_bounce_rejected",
                    description="Inbound: Fake bounce with invalid HMAC (should REJECT)",
                    network="external",
                    mail_from="mailer-daemon@gmail.com",
                    rcpt_to=f"{bounce_prefix}+12345-fakehash-msgid@{primary_domain}",
                    expect_accept=False,
                    expect_code=550,
                    tags={Tag.INBOUND},
                )
            )
        else:
            # Test: Bounce accepted without HMAC validation
            tests.append(
                TestCase(
                    name="inbound_bounce_no_hmac",
                    description="Inbound: Bounce without HMAC validation (should ACCEPT)",
                    network="external",
                    mail_from="mailer-daemon@gmail.com",
                    rcpt_to=f"{bounce_prefix}+12345-abc-msgid@{primary_domain}",
                    expect_accept=True,
                    tags={Tag.INBOUND},
                )
            )

        return tests


@register
class InboundSpecialAddressTests(TestGenerator):
    """Tests for special addresses: postmaster, abuse, fbl."""

    tags = {Tag.INBOUND}
    priority = 21

    def is_applicable(self, config: TestConfig) -> bool:
        return config.inbound.enabled

    def generate(self, config: TestConfig) -> list[TestCase]:
        primary_domain = config.mail.primary_domain

        return [
            TestCase(
                name="inbound_postmaster",
                description="Inbound: Mail to postmaster@ from null sender (should ACCEPT)",
                network="external",
                mail_from="",  # Null sender <> - bypasses SPF
                rcpt_to=f"postmaster@{primary_domain}",
                expect_accept=True,
                tags={Tag.INBOUND},
            ),
            TestCase(
                name="inbound_abuse",
                description="Inbound: Mail to abuse@ from null sender (should ACCEPT)",
                network="external",
                mail_from="",  # Null sender <>
                rcpt_to=f"abuse@{primary_domain}",
                expect_accept=True,
                tags={Tag.INBOUND},
            ),
            TestCase(
                name="inbound_fbl",
                description="Inbound: FBL complaint to fbl@ from null sender (should ACCEPT)",
                network="external",
                mail_from="",  # Null sender <>
                rcpt_to=f"fbl@{primary_domain}",
                expect_accept=True,
                tags={Tag.INBOUND},
            ),
        ]


@register
class InboundUnknownRecipientTests(TestGenerator):
    """Tests for rejection of unknown recipients."""

    tags = {Tag.INBOUND}
    priority = 22

    def is_applicable(self, config: TestConfig) -> bool:
        return config.inbound.enabled

    def generate(self, config: TestConfig) -> list[TestCase]:
        primary_domain = config.mail.primary_domain

        return [
            TestCase(
                name="inbound_unknown_recipient",
                description="Inbound: Mail to unknown recipient (should REJECT)",
                network="external",
                mail_from="",  # Null sender to bypass SPF
                rcpt_to=f"nonexistent@{primary_domain}",
                expect_accept=False,
                expect_code=550,
                tags={Tag.INBOUND},
            )
        ]


@register
class NoInboundTests(TestGenerator):
    """Tests for when inbound is disabled (relay-only mode)."""

    tags = {Tag.NO_INBOUND, Tag.INBOUND}
    priority = 23

    def is_applicable(self, config: TestConfig) -> bool:
        return not config.inbound.enabled

    def generate(self, config: TestConfig) -> list[TestCase]:
        tests: list[TestCase] = []
        primary_domain = config.mail.primary_domain
        allowed_sender = config.get_allowed_sender()

        # Test: Internal network trying to send to our domain (should REJECT)
        tests.append(
            TestCase(
                name="no_inbound_internal_to_own_domain",
                description="No inbound: Internal sender to own domain (should REJECT)",
                network="internal",
                mail_from=allowed_sender,
                rcpt_to=f"anyone@{primary_domain}",
                expect_accept=False,
                expect_code=550,
                tags={Tag.NO_INBOUND},
            )
        )

        # Test: Internal to postmaster (should REJECT - no inbound)
        tests.append(
            TestCase(
                name="no_inbound_internal_postmaster",
                description="No inbound: Internal sender to postmaster@ (should REJECT)",
                network="internal",
                mail_from=allowed_sender,
                rcpt_to=f"postmaster@{primary_domain}",
                expect_accept=False,
                expect_code=550,
                tags={Tag.NO_INBOUND},
            )
        )

        # Test: External to postmaster (should REJECT - no inbound)
        tests.append(
            TestCase(
                name="no_inbound_external_postmaster",
                description="No inbound: External sender to postmaster@ (should REJECT)",
                network="external",
                mail_from="",  # Null sender
                rcpt_to=f"postmaster@{primary_domain}",
                expect_accept=False,
                expect_code=550,
                tags={Tag.NO_INBOUND},
            )
        )

        # Test: External to abuse (should REJECT - no inbound)
        tests.append(
            TestCase(
                name="no_inbound_external_abuse",
                description="No inbound: External sender to abuse@ (should REJECT)",
                network="external",
                mail_from="",
                rcpt_to=f"abuse@{primary_domain}",
                expect_accept=False,
                expect_code=550,
                tags={Tag.NO_INBOUND},
            )
        )

        # Test: External bounce address (should REJECT - no inbound)
        tests.append(
            TestCase(
                name="no_inbound_external_bounce",
                description="No inbound: External bounce to bounce+@ (should REJECT)",
                network="external",
                mail_from="",
                rcpt_to=f"bounce+test@{primary_domain}",
                expect_accept=False,
                expect_code=550,
                tags={Tag.NO_INBOUND},
            )
        )

        return tests
