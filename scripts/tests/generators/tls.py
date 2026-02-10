"""
TLS/STARTTLS test generators.

Tests for TLS functionality.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..base import Tag, TestCase, TestGenerator
from ..registry import register

if TYPE_CHECKING:
    from ..config import TestConfig


@register
class TlsStarttlsTests(TestGenerator):
    """Tests for STARTTLS negotiation."""

    tags = {Tag.TLS}
    priority = 70

    def is_applicable(self, config: TestConfig) -> bool:
        return config.tls.enabled

    def generate(self, config: TestConfig) -> list[TestCase]:
        allowed_sender = config.get_allowed_sender()

        return [
            TestCase(
                name="tls_starttls_works",
                description="TLS: STARTTLS negotiation succeeds",
                network="internal",
                mail_from=allowed_sender,
                rcpt_to="recipient@gmail.com",
                use_tls=True,
                require_tls=True,
                expect_accept=True,
                tags={Tag.TLS},
            )
        ]


@register
class TlsRequiredTests(TestGenerator):
    """Tests for requireTls enforcement."""

    tags = {Tag.TLS}
    priority = 71

    def is_applicable(self, config: TestConfig) -> bool:
        return config.tls.enabled and config.tls.require_tls

    def generate(self, config: TestConfig) -> list[TestCase]:
        allowed_sender = config.get_allowed_sender()

        return [
            TestCase(
                name="tls_required_no_tls_rejected",
                description="TLS: Send without TLS when requireTls=true (should REJECT)",
                network="internal",
                mail_from=allowed_sender,
                rcpt_to="recipient@gmail.com",
                use_tls=False,
                expect_accept=False,
                expect_code=530,  # 530 Must issue STARTTLS first
                tags={Tag.TLS},
            )
        ]


@register
class TlsOptionalTests(TestGenerator):
    """Tests for optional TLS (requireTls=false)."""

    tags = {Tag.TLS}
    priority = 72

    def is_applicable(self, config: TestConfig) -> bool:
        return config.tls.enabled and not config.tls.require_tls

    def generate(self, config: TestConfig) -> list[TestCase]:
        allowed_sender = config.get_allowed_sender()

        return [
            TestCase(
                name="tls_optional_no_tls_works",
                description="TLS: Send without TLS when requireTls=false (should ACCEPT)",
                network="internal",
                mail_from=allowed_sender,
                rcpt_to="recipient@gmail.com",
                use_tls=False,
                expect_accept=True,
                tags={Tag.TLS},
            )
        ]
