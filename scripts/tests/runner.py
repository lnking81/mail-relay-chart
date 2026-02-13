"""
SMTP test runner for mail relay test suite.

Executes test cases against SMTP servers and collects results.
"""

from __future__ import annotations

import smtplib
import socket
import ssl
from email.utils import formatdate, make_msgid
from typing import Callable, Optional

from .base import Tag, TestCase, TestResult
from .config import TestConfig


class SmtpTestRunner:
    """Executes SMTP tests against mail servers.

    Supports both internal (port-forward) and external (LoadBalancer) testing.

    Attributes:
        config: Test configuration.
        internal_server: Address for internal network tests (e.g., "localhost:2525").
        external_server: Address for external network tests (e.g., "mail.example.com:25").
    """

    def __init__(
        self,
        config: TestConfig,
        internal_server: str = "localhost:2525",
        external_server: Optional[str] = None,
    ):
        self.config = config
        self.internal_server = internal_server

        # External server defaults to mail.hostname:25
        if external_server:
            self.external_server = external_server
        else:
            self.external_server = f"{config.mail.hostname}:25"

        self.results: list[TestResult] = []

    def _parse_server(self, server: str) -> tuple[str, int]:
        """Parse server:port string."""
        if ":" in server:
            host, port = server.rsplit(":", 1)
            return host, int(port)
        return server, 25

    def _connect(self, host: str, port: int, timeout: int) -> smtplib.SMTP:
        """Establish SMTP connection and send EHLO."""
        smtp = smtplib.SMTP(host, port, timeout=timeout)
        code, msg = smtp.ehlo("test.local")
        if code != 250:
            raise smtplib.SMTPException(f"EHLO failed: {code} {msg.decode()}")
        return smtp

    def _do_starttls(self, smtp: smtplib.SMTP, host: str) -> None:
        """Perform STARTTLS negotiation."""
        code, msg = smtp.docmd("STARTTLS")
        if code != 220:
            raise smtplib.SMTPException(f"STARTTLS rejected: {code} {msg.decode()}")

        # Create SSL context and wrap socket
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        # Wrap socket with TLS
        if smtp.sock is None:
            raise smtplib.SMTPException("No socket available for STARTTLS")
        server_hostname = host if host and not host.startswith(".") else None
        smtp.sock = context.wrap_socket(smtp.sock, server_hostname=server_hostname)
        smtp.file = smtp.sock.makefile("rb")

        # Re-EHLO after STARTTLS
        code, msg = smtp.ehlo("test.local")
        if code != 250:
            raise smtplib.SMTPException(
                f"EHLO after STARTTLS failed: {code} {msg.decode()}"
            )

    def _build_message(self, test: TestCase) -> bytes:
        """Build email message with headers and body."""
        msg_id = make_msgid(
            domain=test.mail_from.split("@")[-1]
            if "@" in test.mail_from
            else "localhost"
        )
        date_str = formatdate(localtime=True)

        headers: dict[str, str] = {
            "From": test.from_header or test.mail_from,
            "To": test.rcpt_to,
            "Subject": test.subject,
            "Date": date_str,
            "Message-Id": msg_id,
        }
        headers.update(test.headers)

        header_lines = "\r\n".join(f"{k}: {v}" for k, v in headers.items())

        # Generate body
        if test.body_size:
            body_content = "X" * test.body_size
        else:
            body_content = test.body

        # Normalize body to CRLF
        body_crlf = body_content.replace("\r\n", "\n").replace("\n", "\r\n")
        message = f"{header_lines}\r\n\r\n{body_crlf}\r\n"
        return message.encode()

    def run_test(self, test: TestCase) -> TestResult:
        """Execute a single SMTP test.

        Args:
            test: Test case to execute.

        Returns:
            TestResult with pass/fail status and details.
        """
        server = (
            self.internal_server if test.network == "internal" else self.external_server
        )
        host, port = self._parse_server(server)

        # Calculate timeout based on body size
        base_timeout = 10
        if test.body_size and test.body_size > 1024 * 1024:
            base_timeout = max(30, test.body_size // (1024 * 1024) + 10)

        try:
            return self._execute_smtp_test(test, host, port, base_timeout)
        except socket.timeout:
            if test.body_size and not test.expect_accept:
                return TestResult(
                    name=test.name,
                    passed=True,
                    expected="Timeout/Rejection",
                    actual="Connection timed out",
                    details="Connection timed out (possibly due to size rejection)",
                )
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
        except (
            ConnectionResetError,
            BrokenPipeError,
            smtplib.SMTPServerDisconnected,
        ) as e:
            if test.body_size and not test.expect_accept:
                return TestResult(
                    name=test.name,
                    passed=True,
                    expected="Connection closed",
                    actual=str(e),
                    details="Server closed connection (size limit enforcement)",
                )
            return TestResult(
                name=test.name,
                passed=False,
                expected="Connection stable",
                actual=str(e),
                details="Server unexpectedly closed connection",
            )
        except Exception as e:
            return TestResult(
                name=test.name,
                passed=False,
                expected="Success",
                actual=str(e),
                details=f"Unexpected error: {type(e).__name__}",
            )

    def _execute_smtp_test(
        self, test: TestCase, host: str, port: int, timeout: int
    ) -> TestResult:
        """Core SMTP test execution logic."""
        smtp = self._connect(host, port, timeout)

        try:
            # Handle TLS
            has_starttls = smtp.has_extn("STARTTLS")

            if test.use_tls or test.require_tls:
                if not has_starttls:
                    if test.require_tls:
                        return TestResult(
                            name=test.name,
                            passed=False,
                            expected="STARTTLS supported",
                            actual="STARTTLS not advertised",
                            details="Server does not support STARTTLS",
                        )
                else:
                    try:
                        self._do_starttls(smtp, host)
                    except ssl.SSLError as e:
                        return TestResult(
                            name=test.name,
                            passed=False,
                            expected="STARTTLS success",
                            actual=str(e),
                            details="SSL/TLS negotiation failed",
                        )
                    except smtplib.SMTPException as e:
                        return TestResult(
                            name=test.name,
                            passed=False,
                            expected="STARTTLS success",
                            actual=str(e),
                            details="STARTTLS negotiation failed",
                        )

            # Handle AUTH
            if test.auth_user and test.auth_pass:
                result = self._handle_auth(smtp, test)
                if result:
                    return result

            # MAIL FROM
            mail_from = test.mail_from if test.mail_from else ""
            code, msg = smtp.mail(mail_from)

            if code not in (250, 251):
                return self._handle_rejection(test, "MAIL FROM", code, msg)

            # RCPT TO
            code, msg = smtp.rcpt(test.rcpt_to)

            if code not in (250, 251):
                return self._handle_rejection(test, "RCPT TO", code, msg)

            # DATA (if not skipping)
            if not test.skip_data:
                message = self._build_message(test)
                code, msg = smtp.data(message)

                if code not in (250, 251):
                    return self._handle_rejection(test, "DATA", code, msg)

            smtp.quit()

            # Message was accepted
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

        finally:
            try:
                smtp.close()
            except Exception:
                pass

    def _handle_auth(self, smtp: smtplib.SMTP, test: TestCase) -> Optional[TestResult]:
        """Handle SMTP AUTH. Returns TestResult if auth completes the test."""
        if not test.auth_user or not test.auth_pass:
            return None  # No auth requested

        try:
            smtp.login(test.auth_user, test.auth_pass)
            if test.expect_auth_fail:
                return TestResult(
                    name=test.name,
                    passed=False,
                    expected="AUTH rejected",
                    actual="AUTH succeeded",
                    details="AUTH should have failed but succeeded",
                )
        except smtplib.SMTPAuthenticationError as e:
            error_msg = (
                e.smtp_error.decode()
                if isinstance(e.smtp_error, bytes)
                else str(e.smtp_error)
            )
            if test.expect_auth_fail:
                return TestResult(
                    name=test.name,
                    passed=True,
                    expected="AUTH rejected",
                    actual=f"{e.smtp_code} {error_msg}",
                    details="AUTH rejected as expected",
                )
            return TestResult(
                name=test.name,
                passed=False,
                expected="AUTH accepted",
                actual=f"{e.smtp_code} {error_msg}",
                details="AUTH failed unexpectedly",
            )
        except smtplib.SMTPException as e:
            if test.expect_auth_fail:
                return TestResult(
                    name=test.name,
                    passed=True,
                    expected="AUTH rejected",
                    actual=str(e),
                    details="AUTH rejected as expected",
                )
            return TestResult(
                name=test.name,
                passed=False,
                expected="AUTH accepted",
                actual=str(e),
                details="AUTH failed unexpectedly",
            )
        return None

    def _handle_rejection(
        self, test: TestCase, stage: str, code: int, msg: bytes
    ) -> TestResult:
        """Handle SMTP rejection at any stage."""
        if not test.expect_accept:
            expected_code = test.expect_code or 550
            passed = code == expected_code or str(code).startswith("5")
            return TestResult(
                name=test.name,
                passed=passed,
                expected=f"{expected_code} reject",
                actual=f"{code} {msg.decode()}",
                details=f"{stage} rejected as expected"
                if passed
                else "Wrong rejection code",
            )
        return TestResult(
            name=test.name,
            passed=False,
            expected=f"250 {stage} accepted",
            actual=f"{code} {msg.decode()}",
            details=f"{stage} unexpectedly rejected",
        )

    def run_tests(
        self,
        tests: list[TestCase],
        only_network: Optional[str] = None,
        only_tags: Optional[set[Tag]] = None,
        callback: Optional[Callable[[TestCase, TestResult], None]] = None,
    ) -> list[TestResult]:
        """Run a list of tests.

        Args:
            tests: List of test cases to run.
            only_network: Filter by network type ("internal" or "external").
            only_tags: Filter by tags (tests must have at least one matching tag).
            callback: Optional callback function(test, result) after each test.

        Returns:
            List of TestResult objects.
        """
        results: list[TestResult] = []

        for test in tests:
            # Filter by network
            if only_network and test.network != only_network:
                continue

            # Filter by tags
            if only_tags and not (test.tags & only_tags):
                continue

            result = self.run_test(test)
            results.append(result)

            if callback:
                callback(test, result)

        self.results = results
        return results

    def get_summary(self) -> dict[str, int]:
        """Get test summary statistics.

        Returns:
            Dict with total, passed, failed counts.
        """
        total = len(self.results)
        passed = sum(1 for r in self.results if r.passed)
        failed = total - passed
        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "success": failed == 0,
        }

    def get_failed_tests(self) -> list[TestResult]:
        """Get list of failed test results."""
        return [r for r in self.results if not r.passed]
