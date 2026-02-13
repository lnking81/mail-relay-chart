"""
SMTP AUTH test generators.

Tests for SMTP authentication functionality.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..base import Tag, TestCase, TestGenerator
from ..registry import register

if TYPE_CHECKING:
    from ..config import TestConfig


@register
class AuthValidCredentialsTests(TestGenerator):
    """Tests for valid SMTP AUTH credentials."""

    tags = {Tag.AUTH}
    priority = 60

    def is_applicable(self, config: TestConfig) -> bool:
        return config.auth.enabled and config.auth.first_user is not None

    def generate(self, config: TestConfig) -> list[TestCase]:
        tests: list[TestCase] = []
        test_user, test_pass = config.auth.first_user  # type: ignore

        # Test with TLS if required
        if config.auth.require_tls and config.tls.enabled:
            tests.append(
                TestCase(
                    name="auth_valid_with_tls",
                    description=f"AUTH: Valid credentials with TLS as {test_user} (should ACCEPT)",
                    network="internal",
                    mail_from=test_user,
                    rcpt_to="recipient@gmail.com",
                    auth_user=test_user,
                    auth_pass=test_pass,
                    use_tls=True,
                    expect_accept=True,
                    tags={Tag.AUTH},
                )
            )
        elif not config.auth.require_tls:
            # AUTH without TLS allowed
            tests.append(
                TestCase(
                    name="auth_valid_no_tls",
                    description=f"AUTH: Valid credentials without TLS as {test_user} (should ACCEPT)",
                    network="internal",
                    mail_from=test_user,
                    rcpt_to="recipient@gmail.com",
                    auth_user=test_user,
                    auth_pass=test_pass,
                    use_tls=False,
                    expect_accept=True,
                    tags={Tag.AUTH},
                )
            )

        return tests


@register
class AuthWithoutTlsRejectedTests(TestGenerator):
    """Tests for AUTH without TLS when requireTls=true."""

    tags = {Tag.AUTH, Tag.TLS}
    priority = 61

    def is_applicable(self, config: TestConfig) -> bool:
        return (
            config.auth.enabled
            and config.auth.first_user is not None
            and config.auth.require_tls
            and config.tls.enabled
        )

    def generate(self, config: TestConfig) -> list[TestCase]:
        test_user, test_pass = config.auth.first_user  # type: ignore

        return [
            TestCase(
                name="auth_without_tls_rejected",
                description="AUTH: Attempt AUTH without TLS (requireTls=true, should REJECT)",
                network="internal",
                mail_from=test_user,
                rcpt_to="recipient@gmail.com",
                auth_user=test_user,
                auth_pass=test_pass,
                use_tls=False,
                expect_auth_fail=True,
                expect_accept=False,
                tags={Tag.AUTH},
            )
        ]


@register
class AuthInvalidCredentialsTests(TestGenerator):
    """Tests for invalid SMTP AUTH credentials."""

    tags = {Tag.AUTH}
    priority = 62

    def is_applicable(self, config: TestConfig) -> bool:
        return config.auth.enabled and config.auth.first_user is not None

    def generate(self, config: TestConfig) -> list[TestCase]:
        tests: list[TestCase] = []
        test_user, _ = config.auth.first_user  # type: ignore
        use_tls = config.tls.enabled and config.auth.require_tls

        # Test: Invalid password
        tests.append(
            TestCase(
                name="auth_invalid_credentials",
                description="AUTH: Invalid password (should REJECT)",
                network="internal",
                mail_from=test_user,
                rcpt_to="recipient@gmail.com",
                auth_user=test_user,
                auth_pass="wrongpassword",
                use_tls=use_tls,
                expect_auth_fail=True,
                expect_accept=False,
                tags={Tag.AUTH},
            )
        )

        # Test: Unknown user
        tests.append(
            TestCase(
                name="auth_unknown_user",
                description="AUTH: Unknown user (should REJECT)",
                network="internal",
                mail_from="fakeuser@example.com",
                rcpt_to="recipient@gmail.com",
                auth_user="fakeuser@example.com",
                auth_pass="somepassword",
                use_tls=use_tls,
                expect_auth_fail=True,
                expect_accept=False,
                tags={Tag.AUTH},
            )
        )

        return tests


@register
class AuthConstrainSenderTests(TestGenerator):
    """Tests for sender constraint with authenticated users."""

    tags = {Tag.AUTH}
    priority = 63

    def is_applicable(self, config: TestConfig) -> bool:
        return (
            config.auth.enabled
            and config.auth.first_user is not None
            and config.auth.constrain_sender
        )

    def generate(self, config: TestConfig) -> list[TestCase]:
        test_user, test_pass = config.auth.first_user  # type: ignore
        use_tls = config.tls.enabled and config.auth.require_tls

        return [
            TestCase(
                name="auth_constrain_sender_violation",
                description=f"AUTH: Authenticated as {test_user}, sending from different address (should REJECT)",
                network="internal",
                mail_from="spoofed@other-domain.com",
                rcpt_to="recipient@gmail.com",
                auth_user=test_user,
                auth_pass=test_pass,
                use_tls=use_tls,
                expect_accept=False,
                expect_code=550,
                tags={Tag.AUTH},
            )
        ]
