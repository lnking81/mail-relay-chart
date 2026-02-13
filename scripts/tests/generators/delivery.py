"""
Delivery test generators.

Tests for legitimate mail delivery paths.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..base import Tag, TestCase, TestGenerator
from ..registry import register

if TYPE_CHECKING:
    from ..config import TestConfig


@register
class DeliveryTests(TestGenerator):
    """Tests for legitimate outbound mail delivery."""

    tags = {Tag.DELIVERY, Tag.OUTBOUND}
    priority = 50

    def is_applicable(self, config: TestConfig) -> bool:
        return True  # Always applicable

    def generate(self, config: TestConfig) -> list[TestCase]:
        tests: list[TestCase] = []
        allowed_sender = config.get_allowed_sender()

        # Test: Internal to multiple external providers
        tests.append(
            TestCase(
                name="delivery_internal_to_yahoo",
                description="Delivery: Internal sender to yahoo.com (should ACCEPT)",
                network="internal",
                mail_from=allowed_sender,
                rcpt_to="user@yahoo.com",
                expect_accept=True,
                tags={Tag.DELIVERY},
            )
        )

        tests.append(
            TestCase(
                name="delivery_internal_to_outlook",
                description="Delivery: Internal sender to outlook.com (should ACCEPT)",
                network="internal",
                mail_from=allowed_sender,
                rcpt_to="user@outlook.com",
                expect_accept=True,
                tags={Tag.DELIVERY},
            )
        )

        # Test: Internal to corporate domain
        tests.append(
            TestCase(
                name="delivery_internal_to_corporate",
                description="Delivery: Internal sender to corporate domain (should ACCEPT)",
                network="internal",
                mail_from=allowed_sender,
                rcpt_to="contact@example-corp.com",
                expect_accept=True,
                tags={Tag.DELIVERY},
            )
        )

        return tests


@register
class DeliveryNullSenderTests(TestGenerator):
    """Tests for null sender (bounce) delivery."""

    tags = {Tag.DELIVERY, Tag.OUTBOUND}
    priority = 51

    def is_applicable(self, config: TestConfig) -> bool:
        # Null sender from internal is for bounce generation
        return config.inbound.enabled

    def generate(self, config: TestConfig) -> list[TestCase]:
        return [
            TestCase(
                name="delivery_internal_null_sender",
                description="Delivery: Internal NULL sender (bounce) to external (should ACCEPT)",
                network="internal",
                mail_from="",  # NULL sender for bounces
                rcpt_to="original-sender@external.com",
                expect_accept=True,
                tags={Tag.DELIVERY},
            )
        ]
