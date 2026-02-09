#!/usr/bin/env python3
"""
Mail Relay Test Suite

Generates and runs SMTP tests based on Helm values configuration.
Tests both internal (port-forward) and external (LoadBalancer) access.

Usage:
  python test_mail_relay.py -f test-values.yaml
  python test_mail_relay.py -f test-values.yaml --internal localhost:2525
  python test_mail_relay.py -f test-values.yaml --external mail.example.com:25
  python test_mail_relay.py -f test-values.yaml --only internal
  python test_mail_relay.py -f test-values.yaml --only external
"""

import argparse
import copy
import smtplib
import socket
import sys
from dataclasses import dataclass, field
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Optional

import yaml


def deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dictionaries. Override values take precedence."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


@dataclass
class TestResult:
    """Result of a single test case."""

    name: str
    passed: bool
    expected: str
    actual: str
    details: str = ""


@dataclass
class TestCase:
    """Definition of a test case."""

    name: str
    description: str
    network: str  # "internal" or "external"
    mail_from: str
    rcpt_to: str
    from_header: Optional[str] = None  # If different from mail_from
    subject: str = "Test Email"
    body: str = "This is a test email."
    expect_accept: bool = True  # True = expect 250, False = expect 5xx
    expect_code: Optional[int] = None  # Specific code to expect
    skip_data: bool = False  # Skip DATA phase (test MAIL FROM/RCPT TO only)
    headers: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.from_header is None:
            self.from_header = self.mail_from


class MailRelayTester:
    """Tests mail relay based on Helm values configuration."""

    # Path to default values.yaml relative to this script
    DEFAULT_VALUES_PATH = "chart/values.yaml"

    def __init__(
        self,
        values_file: str,
        internal_server: str = "localhost:2525",
        external_server: Optional[str] = None,
        chart_dir: Optional[str] = None,
    ):
        self.values = self._load_values(values_file, chart_dir)
        self.internal_server = internal_server

        # External server defaults to mail.hostname:25
        if external_server:
            self.external_server = external_server
        else:
            hostname = self.values.get("mail", {}).get("hostname", "mail.example.com")
            self.external_server = f"{hostname}:25"

        self.results: list[TestResult] = []
        self.test_cases: list[TestCase] = []

    def _load_values(self, values_file: str, chart_dir: Optional[str] = None) -> dict:
        """Load and merge default values.yaml with override file."""
        # Find default values.yaml
        if chart_dir:
            default_path = Path(chart_dir) / "values.yaml"
        else:
            # Try relative to script location
            script_dir = Path(__file__).parent
            default_path = script_dir / self.DEFAULT_VALUES_PATH

            # Try relative to current directory
            if not default_path.exists():
                default_path = Path(self.DEFAULT_VALUES_PATH)

        # Load default values
        default_values = {}
        if default_path.exists():
            with open(default_path) as f:
                default_values = yaml.safe_load(f) or {}
            print(f"Loaded default values from: {default_path}")
        else:
            print(f"Warning: Default values not found at {default_path}")

        # Load override values
        with open(values_file) as f:
            override_values = yaml.safe_load(f) or {}

        # Deep merge
        merged = deep_merge(default_values, override_values)
        return merged

    def _get_domains(self) -> list[str]:
        """Get list of configured domains."""
        domains = []
        for d in self.values.get("mail", {}).get("domains", []):
            if isinstance(d, dict):
                domains.append(d.get("name", ""))
            else:
                domains.append(str(d))
        return [d for d in domains if d]

    def _get_hostname(self) -> str:
        """Get mail hostname."""
        return self.values.get("mail", {}).get("hostname", "mail.example.com")

    def _get_trusted_networks(self) -> list[str]:
        """Get trusted networks."""
        return self.values.get("mail", {}).get("trustedNetworks", [])

    def _is_inbound_enabled(self) -> bool:
        """Check if inbound (bounce/FBL) handling is enabled."""
        return self.values.get("inbound", {}).get("enabled", False)

    def _is_sender_validation_enabled(self) -> bool:
        """Check if sender validation is enabled."""
        return (
            self.values.get("mail", {})
            .get("senderValidation", {})
            .get("enabled", False)
        )

    def _get_allowed_from(self) -> list[str]:
        """Get allowedFrom list."""
        return (
            self.values.get("mail", {})
            .get("senderValidation", {})
            .get("allowedFrom", [])
        )

    def _get_forbidden_from(self) -> list[str]:
        """Get forbiddenFrom list."""
        return (
            self.values.get("mail", {})
            .get("senderValidation", {})
            .get("forbiddenFrom", [])
        )

    def _get_bounce_prefix(self) -> str:
        """Get VERP bounce prefix."""
        prefix = (
            self.values.get("inbound", {})
            .get("bounce", {})
            .get("verpPrefix", "bounce+")
        )
        return prefix.rstrip("+")

    def _get_allowed_sender(self) -> str:
        """Get an allowed sender address for testing."""
        domains = self._get_domains()
        hostname = self._get_hostname()
        primary_domain = domains[0] if domains else hostname.split(".", 1)[-1]
        allowed_from = self._get_allowed_from()

        if allowed_from:
            # Strict mode: use first allowed address (skip regex patterns)
            for addr in allowed_from:
                if not addr.startswith("/"):
                    return addr
        return f"sender@{primary_domain}"

    def _is_spf_enabled(self) -> bool:
        """Check if SPF verification is enabled."""
        return (
            self.values.get("inbound", {})
            .get("security", {})
            .get("spf", {})
            .get("enabled", False)
        )

    def _is_dmarc_enabled(self) -> bool:
        """Check if DMARC verification is enabled."""
        return (
            self.values.get("inbound", {})
            .get("security", {})
            .get("dmarc", {})
            .get("enabled", False)
        )

    def _is_auth_enabled(self) -> bool:
        """Check if SMTP AUTH is enabled."""
        return self.values.get("auth", {}).get("enabled", False)

    def _is_dkim_verify_enabled(self) -> bool:
        """Check if DKIM verification for inbound is enabled."""
        return (
            self.values.get("inbound", {})
            .get("security", {})
            .get("dkim", {})
            .get("enabled", False)
        )

    def _is_spf_reject_fail(self) -> bool:
        """Check if SPF rejects on fail."""
        return (
            self.values.get("inbound", {})
            .get("security", {})
            .get("spf", {})
            .get("rejectFail", False)
        )

    def _is_spf_reject_softfail(self) -> bool:
        """Check if SPF rejects on softfail."""
        return (
            self.values.get("inbound", {})
            .get("security", {})
            .get("spf", {})
            .get("rejectSoftfail", False)
        )

    def _is_dmarc_reject_on_fail(self) -> bool:
        """Check if DMARC rejects on fail."""
        return (
            self.values.get("inbound", {})
            .get("security", {})
            .get("dmarc", {})
            .get("rejectOnFail", False)
        )

    def generate_tests(self) -> list[TestCase]:
        """Generate test cases based on configuration.

        Test logic:
        - INTERNAL (trusted network, relaying=true): Can send to external recipients
          - Sender validation applies: only allowed domains as MAIL FROM
        - EXTERNAL (untrusted, relaying=false): Cannot relay
          - Can only send to our special addresses (bounce+, postmaster, abuse, fbl)
          - Any MAIL FROM is accepted (bounces come from anywhere)
        """
        tests = []
        domains = self._get_domains()
        hostname = self._get_hostname()
        primary_domain = domains[0] if domains else hostname.split(".", 1)[-1]
        allowed_from = self._get_allowed_from()

        # Determine allowed sender based on strict mode
        # Strict mode: if allowedFrom is specified, ONLY those are allowed
        # Default mode: all addresses in mail.domains are allowed
        is_strict_mode = len(allowed_from) > 0
        if is_strict_mode:
            # Use first allowed address (filter out regex patterns)
            allowed_sender = None
            for addr in allowed_from:
                if not addr.startswith("/"):
                    allowed_sender = addr
                    break
            if not allowed_sender:
                allowed_sender = f"sender@{primary_domain}"  # Fallback
        else:
            allowed_sender = f"sender@{primary_domain}"

        # =====================================================================
        # OUTBOUND TESTS (from internal/trusted network, relaying to external)
        # These simulate apps/services sending mail through the relay
        # =====================================================================

        # Test 1: Basic relay from allowed sender
        tests.append(
            TestCase(
                name="outbound_allowed_domain",
                description=f"Outbound: Send from allowed sender {allowed_sender} (should ACCEPT)",
                network="internal",
                mail_from=allowed_sender,
                rcpt_to="recipient@gmail.com",
                expect_accept=True,
            )
        )

        # Test 2: Relay from disallowed sender (should REJECT if sender validation enabled)
        if self._is_sender_validation_enabled():
            if is_strict_mode:
                # Strict mode: even our domain is blocked if not in allowedFrom
                disallowed_sender = f"sender@{primary_domain}"
                disallowed_desc = f"Outbound: Send from domain (not in allowedFrom) {disallowed_sender} (should REJECT)"
            else:
                # Default mode: external domains are blocked
                disallowed_sender = "hacker@gmail.com"
                disallowed_desc = (
                    "Outbound: Send from non-whitelisted domain (should REJECT)"
                )

            tests.append(
                TestCase(
                    name="outbound_disallowed_domain",
                    description=disallowed_desc,
                    network="internal",
                    mail_from=disallowed_sender,
                    rcpt_to="victim@example.org",
                    expect_accept=False,
                    expect_code=550,
                )
            )

            # Test 3: Relay from forbidden address
            forbidden = self._get_forbidden_from()
            if forbidden:
                # Use first forbidden pattern or create test address
                forbidden_addr = forbidden[0]
                if forbidden_addr.startswith("/"):
                    # Regex - create matching address
                    forbidden_addr = f"ceo@{primary_domain}"
                tests.append(
                    TestCase(
                        name="outbound_forbidden_sender",
                        description=f"Outbound: Send from forbidden address {forbidden_addr} (should REJECT)",
                        network="internal",
                        mail_from=forbidden_addr,
                        rcpt_to="recipient@gmail.com",
                        expect_accept=False,
                        expect_code=550,
                    )
                )

            # Test 4: From header domain mismatch (if checkFromHeader enabled)
            if (
                self.values.get("mail", {})
                .get("senderValidation", {})
                .get("checkFromHeader", False)
            ):
                tests.append(
                    TestCase(
                        name="outbound_from_header_mismatch",
                        description="Outbound: From header domain != MAIL FROM domain (should REJECT)",
                        network="internal",
                        mail_from=allowed_sender,
                        from_header="spoofed@other-domain.com",
                        rcpt_to="recipient@gmail.com",
                        expect_accept=False,
                    )
                )

        # =====================================================================
        # INBOUND TESTS (from external/untrusted network)
        # These simulate bounces, FBL, postmaster mail coming from the internet
        # Sender validation does NOT apply - anyone can send bounces to us
        # =====================================================================

        if self._is_inbound_enabled():
            bounce_prefix = self._get_bounce_prefix()
            require_hmac = (
                self.values.get("inbound", {})
                .get("bounce", {})
                .get("requireHmac", True)
            )

            if require_hmac:
                # Test 5: Fake bounce should be REJECTED (invalid HMAC)
                tests.append(
                    TestCase(
                        name="inbound_fake_bounce_rejected",
                        description="Inbound: Fake bounce with invalid HMAC (should REJECT)",
                        network="external",
                        mail_from="mailer-daemon@gmail.com",
                        rcpt_to=f"{bounce_prefix}+12345-fakehash-msgid@{primary_domain}",
                        expect_accept=False,
                        expect_code=550,
                    )
                )
            else:
                # Test 5: Bounce accepted without HMAC validation
                tests.append(
                    TestCase(
                        name="inbound_bounce_no_hmac",
                        description="Inbound: Bounce without HMAC validation (should ACCEPT)",
                        network="external",
                        mail_from="mailer-daemon@gmail.com",
                        rcpt_to=f"{bounce_prefix}+12345-abc-msgid@{primary_domain}",
                        expect_accept=True,
                    )
                )

            # Test 6: Inbound to postmaster (use null sender - RFC 5321 for system mail)
            tests.append(
                TestCase(
                    name="inbound_postmaster",
                    description="Inbound: Mail to postmaster@ from null sender (should ACCEPT)",
                    network="external",
                    mail_from="",  # Null sender <> - bypasses SPF
                    rcpt_to=f"postmaster@{primary_domain}",
                    expect_accept=True,
                )
            )

            # Test 8: Inbound to abuse (use null sender)
            tests.append(
                TestCase(
                    name="inbound_abuse",
                    description="Inbound: Mail to abuse@ from null sender (should ACCEPT)",
                    network="external",
                    mail_from="",  # Null sender <>
                    rcpt_to=f"abuse@{primary_domain}",
                    expect_accept=True,
                )
            )

            # Test 9: Inbound to FBL address (FBL reports come from null sender)
            tests.append(
                TestCase(
                    name="inbound_fbl",
                    description="Inbound: FBL complaint to fbl@ from null sender (should ACCEPT)",
                    network="external",
                    mail_from="",  # Null sender <>
                    rcpt_to=f"fbl@{primary_domain}",
                    expect_accept=True,
                )
            )

            # Test 10: Inbound to non-existent address (should REJECT)
            # Use null sender to test recipient validation, not SPF
            tests.append(
                TestCase(
                    name="inbound_unknown_recipient",
                    description="Inbound: Mail to unknown recipient (should REJECT)",
                    network="external",
                    mail_from="",  # Null sender to bypass SPF
                    rcpt_to=f"nonexistent@{primary_domain}",
                    expect_accept=False,
                    expect_code=550,
                )
            )

        else:
            # =====================================================================
            # INBOUND DISABLED TESTS
            # When inbound is disabled, ALL mail to our domain should be rejected
            # (we are relay-only, not accepting any incoming mail)
            # =====================================================================

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
                )
            )

        # =====================================================================
        # INBOUND SECURITY TESTS (SPF/DKIM/DMARC)
        # Test email authentication verification on inbound mail
        #
        # SPF checks: Is the connecting IP authorized to send for MAIL FROM domain?
        # - gmail.com has strict SPF (-all) → our test IP is NOT authorized → SPF FAIL
        # - Null sender <> → SPF not applicable
        # - Random subdomain of .invalid → SPF NONE (no record, RFC 2606)
        # =====================================================================

        if self._is_inbound_enabled():
            # SPF Tests
            if self._is_spf_enabled():
                # Test: SPF HARDFAIL - send from domain with "v=spf1 -all" policy
                # RFC 7208: -all means hardfail (no hosts authorized)
                # example.org has "v=spf1 -all" - IANA reserved, no mail allowed
                if self._is_spf_reject_fail():
                    tests.append(
                        TestCase(
                            name="inbound_spf_fail_rejected",
                            description="Inbound: SPF HARDFAIL (example.org has -all, should REJECT)",
                            network="external",
                            mail_from="test@example.org",  # example.org: v=spf1 -all
                            rcpt_to=f"postmaster@{primary_domain}",
                            expect_accept=False,
                            expect_code=550,
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
                    )
                )

            # DKIM Tests
            if self._is_dkim_verify_enabled():
                # Test: Mail without DKIM signature
                # Should be accepted (DKIM verification only checks IF signature present)
                # Use null sender to bypass SPF
                tests.append(
                    TestCase(
                        name="inbound_dkim_no_signature",
                        description="Inbound: No DKIM signature (should ACCEPT, DKIM optional)",
                        network="external",
                        mail_from="",  # Null sender bypasses SPF
                        rcpt_to=f"postmaster@{primary_domain}",
                        expect_accept=True,
                    )
                )

            # DMARC Tests
            if self._is_dmarc_enabled():
                # DMARC checks alignment between From header and MAIL FROM/DKIM domain
                # For DMARC test, use null sender (bypasses SPF) with spoofed From header

                if self._is_dmarc_reject_on_fail():
                    # Test: DMARC FAIL with p=reject policy domain
                    # From header spoofs a domain with strict DMARC, but no SPF/DKIM alignment
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
                        )
                    )

        # =====================================================================
        # RELAY PROTECTION TESTS
        # External connections should NOT be able to relay through us
        # =====================================================================

        # Test 11: External trying to relay through us (should REJECT)
        tests.append(
            TestCase(
                name="external_relay_attempt",
                description="External: Attempt to relay to third-party domain (should REJECT)",
                network="external",
                mail_from="spammer@evil.com",
                rcpt_to="victim@other-domain.com",
                expect_accept=False,
                expect_code=550,
            )
        )

        # Test 12: External with allowed sender in MAIL FROM trying to relay (should REJECT)
        # Even if they spoof an allowed address, they can't relay from external network
        tests.append(
            TestCase(
                name="external_spoofed_relay_attempt",
                description=f"External: Spoofed allowed sender {allowed_sender} + relay attempt (should REJECT)",
                network="external",
                mail_from=allowed_sender,
                rcpt_to="victim@other-domain.com",
                expect_accept=False,
                expect_code=550,
            )
        )

        self.test_cases = tests
        return tests

    def _parse_server(self, server: str) -> tuple[str, int]:
        """Parse server:port string."""
        if ":" in server:
            host, port = server.rsplit(":", 1)
            return host, int(port)
        return server, 25

    def _run_smtp_test(self, test: TestCase, server: str) -> TestResult:
        """Execute a single SMTP test."""
        host, port = self._parse_server(server)

        try:
            # Connect
            smtp = smtplib.SMTP(timeout=10)
            smtp.connect(host, port)

            # EHLO
            code, msg = smtp.ehlo("test.local")
            if code != 250:
                return TestResult(
                    name=test.name,
                    passed=False,
                    expected="250 EHLO",
                    actual=f"{code} {msg.decode()}",
                    details="EHLO failed",
                )

            # MAIL FROM
            mail_from = test.mail_from if test.mail_from else ""
            code, msg = smtp.mail(mail_from)

            if code not in (250, 251):
                # MAIL FROM rejected
                if not test.expect_accept:
                    expected_code = test.expect_code or 550
                    passed = code == expected_code or str(code).startswith("5")
                    return TestResult(
                        name=test.name,
                        passed=passed,
                        expected=f"{expected_code} reject",
                        actual=f"{code} {msg.decode()}",
                        details="MAIL FROM rejected as expected"
                        if passed
                        else "Wrong rejection code",
                    )
                return TestResult(
                    name=test.name,
                    passed=False,
                    expected="250 MAIL FROM accepted",
                    actual=f"{code} {msg.decode()}",
                    details="MAIL FROM unexpectedly rejected",
                )

            # RCPT TO
            code, msg = smtp.rcpt(test.rcpt_to)

            if code not in (250, 251):
                # RCPT TO rejected
                if not test.expect_accept:
                    expected_code = test.expect_code or 550
                    passed = code == expected_code or str(code).startswith("5")
                    return TestResult(
                        name=test.name,
                        passed=passed,
                        expected=f"{expected_code} reject",
                        actual=f"{code} {msg.decode()}",
                        details="RCPT TO rejected as expected"
                        if passed
                        else "Wrong rejection code",
                    )
                return TestResult(
                    name=test.name,
                    passed=False,
                    expected="250 RCPT TO accepted",
                    actual=f"{code} {msg.decode()}",
                    details="RCPT TO unexpectedly rejected",
                )

            # DATA (if not skipping)
            if not test.skip_data:
                # Generate proper email headers for DKIM signing
                msg_id = make_msgid(
                    domain=test.mail_from.split("@")[-1]
                    if "@" in test.mail_from
                    else "localhost"
                )
                date_str = formatdate(localtime=True)

                # Build headers dict
                headers = {
                    "From": test.from_header,
                    "To": test.rcpt_to,
                    "Subject": test.subject,
                    "Date": date_str,
                    "Message-Id": msg_id,
                }
                # Add custom headers
                headers.update(test.headers)

                # Build message with CRLF line endings (RFC 5321 requires CRLF)
                header_lines = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
                # Normalize body to CRLF - important for DKIM body hash
                body_crlf = test.body.replace("\r\n", "\n").replace("\n", "\r\n")
                message = f"{header_lines}\r\n\r\n{body_crlf}\r\n"

                code, msg = smtp.data(message.encode())

                if code not in (250, 251):
                    if not test.expect_accept:
                        expected_code = test.expect_code or 550
                        passed = code == expected_code or str(code).startswith("5")
                        return TestResult(
                            name=test.name,
                            passed=passed,
                            expected=f"{expected_code} reject",
                            actual=f"{code} {msg.decode()}",
                            details="DATA rejected as expected"
                            if passed
                            else "Wrong rejection code",
                        )
                    return TestResult(
                        name=test.name,
                        passed=False,
                        expected="250 DATA accepted",
                        actual=f"{code} {msg.decode()}",
                        details="DATA unexpectedly rejected",
                    )

            smtp.quit()

            # If we got here, message was accepted
            if test.expect_accept:
                return TestResult(
                    name=test.name,
                    passed=True,
                    expected="Message accepted",
                    actual="250 OK",
                    details="Message queued successfully",
                )
            else:
                return TestResult(
                    name=test.name,
                    passed=False,
                    expected="Message rejected",
                    actual="250 OK - Message accepted",
                    details="Message should have been rejected but was accepted",
                )

        except smtplib.SMTPRecipientsRefused as e:
            if not test.expect_accept:
                return TestResult(
                    name=test.name,
                    passed=True,
                    expected="Recipient rejected",
                    actual=str(e),
                    details="Rejected as expected",
                )
            return TestResult(
                name=test.name,
                passed=False,
                expected="Recipient accepted",
                actual=str(e),
                details="Recipient unexpectedly rejected",
            )

        except smtplib.SMTPSenderRefused as e:
            if not test.expect_accept:
                return TestResult(
                    name=test.name,
                    passed=True,
                    expected="Sender rejected",
                    actual=f"{e.smtp_code} {e.smtp_error.decode()}",
                    details="Rejected as expected",
                )
            return TestResult(
                name=test.name,
                passed=False,
                expected="Sender accepted",
                actual=f"{e.smtp_code} {e.smtp_error.decode()}",
                details="Sender unexpectedly rejected",
            )

        except socket.timeout:
            return TestResult(
                name=test.name,
                passed=False,
                expected="Connection",
                actual="Timeout",
                details=f"Connection to {host}:{port} timed out",
            )

        except ConnectionRefusedError:
            return TestResult(
                name=test.name,
                passed=False,
                expected="Connection",
                actual="Refused",
                details=f"Connection to {host}:{port} refused",
            )

        except Exception as e:
            return TestResult(
                name=test.name,
                passed=False,
                expected="Success",
                actual=str(e),
                details=f"Unexpected error: {type(e).__name__}",
            )

    def run_tests(
        self, only: Optional[str] = None, short: bool = False
    ) -> list[TestResult]:
        """Run all generated tests."""
        if not self.test_cases:
            self.generate_tests()

        results = []
        for test in self.test_cases:
            # Skip if filtering by network
            if only and test.network != only:
                continue

            # Select server based on network type
            server = (
                self.internal_server
                if test.network == "internal"
                else self.external_server
            )

            result = self._run_smtp_test(test, server)
            results.append(result)

            if short:
                # Compact single-line output
                status = "✅" if result.passed else "❌"
                expect = "ACCEPT" if test.expect_accept else "REJECT"
                print(
                    f"[{test.network}] {test.name}: {status} <{test.mail_from}> → <{test.rcpt_to}> (expect {expect}, got {result.actual})"
                )
            else:
                print(f"\n{'=' * 60}")
                print(f"TEST: {test.name}")
                print(f"  {test.description}")
                print(f"  Server: {server}")
                print(f"  MAIL FROM: <{test.mail_from}>")
                print(f"  RCPT TO: <{test.rcpt_to}>")
                print(f"  Expected: {'ACCEPT' if test.expect_accept else 'REJECT'}")

                status = "✅ PASS" if result.passed else "❌ FAIL"
                print(f"  Result: {status}")
                print(f"  Actual: {result.actual}")
                if result.details:
                    print(f"  Details: {result.details}")

        self.results = results
        return results

    def print_summary(self, short: bool = False):
        """Print test summary."""
        total = len(self.results)
        passed = sum(1 for r in self.results if r.passed)
        failed = total - passed

        if short:
            # Compact single-line summary
            status = "✅ ALL PASS" if failed == 0 else f"❌ {failed} FAILED"
            print(f"\nSummary: {passed}/{total} passed {status}")
        else:
            print(f"\n{'=' * 60}")
            print("TEST SUMMARY")
            print(f"{'=' * 60}")
            print(f"Total: {total}")
            print(f"Passed: {passed} ✅")
            print(f"Failed: {failed} ❌")

            if failed > 0:
                print("\nFailed tests:")
                for r in self.results:
                    if not r.passed:
                        print(f"  - {r.name}: {r.actual}")

            print(f"\nResult: {'ALL PASS ✅' if failed == 0 else 'SOME FAILED ❌'}")
        return failed == 0


def main():
    parser = argparse.ArgumentParser(description="Mail Relay Test Suite")
    parser.add_argument(
        "-f",
        "--values",
        required=True,
        help="Path to Helm values.yaml override file",
    )
    parser.add_argument(
        "-c",
        "--chart-dir",
        default=None,
        help="Path to chart directory containing default values.yaml",
    )
    parser.add_argument(
        "--internal",
        default="localhost:2525",
        help="Internal server address (default: localhost:2525)",
    )
    parser.add_argument(
        "--external",
        help="External server address (default: mail.hostname:25 from values)",
    )
    parser.add_argument(
        "--only",
        choices=["internal", "external"],
        help="Run only internal or external tests",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List tests without running them",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print merged values for debugging",
    )
    parser.add_argument(
        "--short",
        action="store_true",
        help="Compact single-line output per test",
    )
    parser.add_argument(
        "--real-recipient",
        metavar="EMAIL",
        help="Run real delivery test to this email address",
    )
    parser.add_argument(
        "--real-subject",
        default="[TEST] Mail Relay Delivery Test",
        help="Subject for real delivery test (default: '[TEST] Mail Relay Delivery Test')",
    )

    args = parser.parse_args()

    tester = MailRelayTester(
        values_file=args.values,
        internal_server=args.internal,
        external_server=args.external,
        chart_dir=args.chart_dir,
    )

    if args.debug:
        print("\n=== Merged Values ===")
        print(yaml.dump(tester.values, default_flow_style=False))
        print("=" * 40)

    tests = tester.generate_tests()

    # Add real delivery test if --real-recipient specified
    if args.real_recipient:
        tests.append(
            TestCase(
                name="real_delivery",
                description=f"Real delivery test to {args.real_recipient}",
                network="internal",
                mail_from=tester._get_allowed_sender(),
                rcpt_to=args.real_recipient,
                subject=args.real_subject,
                body=f"This is a test email from mail-relay.\n\nTimestamp: {__import__('datetime').datetime.now().isoformat()}\nServer: {tester.internal_server}\n",
                expect_accept=True,
            )
        )
        tester.test_cases = tests

    if args.list:
        print("Generated tests:")
        for t in tests:
            expect = "ACCEPT" if t.expect_accept else "REJECT"
            print(
                f"  [{t.network}] {t.name}: <{t.mail_from}> → <{t.rcpt_to}> (expect {expect})"
            )
        return 0

    if not args.short:
        print("Mail Relay Test Suite")
        print(f"Values: {args.values}")
        print(f"Internal: {tester.internal_server}")
        print(f"External: {tester.external_server}")
        print(f"Tests: {len(tests)}")

    tester.run_tests(only=args.only, short=args.short)
    success = tester.print_summary(short=args.short)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
