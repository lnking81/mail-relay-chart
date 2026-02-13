#!/usr/bin/env python3
"""
Mail Relay Test Suite - CLI

Generates and runs SMTP tests based on Helm values configuration.
Tests both internal (port-forward) and external (LoadBalancer) access.

Usage:
    python test_mail_relay.py -f test-values.yaml
    python test_mail_relay.py -f test-values.yaml --internal localhost:2525
    python test_mail_relay.py -f test-values.yaml --external mail.example.com:25
    python test_mail_relay.py -f test-values.yaml --only internal
    python test_mail_relay.py -f test-values.yaml --only external
    python test_mail_relay.py -f test-values.yaml --tags outbound,relay
    python test_mail_relay.py -f test-values.yaml --tags security --only external

Available tags:
    outbound    - Outbound relay tests
    inbound     - Inbound mail handling tests
    security    - SPF/DKIM/DMARC verification tests
    relay       - Open relay protection tests (critical!)
    delivery    - Legitimate delivery flow tests
    auth        - SMTP AUTH tests
    tls         - TLS/STARTTLS tests
    size        - Message size limit tests
    multi_domain - Multi-domain tests
    regex       - Regex pattern tests
    no_inbound  - Tests for inbound disabled scenario
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from typing import Optional

import yaml
from tests import (
    SmtpTestRunner,
    Tag,
    TestCase,
    TestConfig,
    TestResult,
    generate_tests,
    list_generators,
)


def parse_tags(tags_str: str) -> set[Tag]:
    """Parse comma-separated tag names into Tag set."""
    tags: set[Tag] = set()
    for name in tags_str.split(","):
        name = name.strip().upper()
        try:
            tags.add(Tag[name])
        except KeyError:
            print(f"Warning: Unknown tag '{name}', skipping")
    return tags


def print_test_short(test: TestCase, result: TestResult) -> None:
    """Print single-line test result."""
    status = "✅" if result.passed else "❌"
    expect = "ACCEPT" if test.expect_accept else "REJECT"
    print(
        f"[{test.network}] {test.name}: {status} "
        f"<{test.mail_from}> → <{test.rcpt_to}> "
        f"(expect {expect}, got {result.actual})"
    )


def print_test_verbose(test: TestCase, result: TestResult, server: str) -> None:
    """Print detailed test result."""
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


def print_summary(runner: SmtpTestRunner, short: bool = False) -> bool:
    """Print test summary. Returns True if all passed."""
    summary = runner.get_summary()
    total = summary["total"]
    passed = summary["passed"]
    failed = summary["failed"]

    if short:
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
            for r in runner.get_failed_tests():
                print(f"  - {r.name}: {r.actual}")

        print(f"\nResult: {'ALL PASS ✅' if failed == 0 else 'SOME FAILED ❌'}")

    return failed == 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mail Relay Test Suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
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
        "--tags",
        type=str,
        help="Comma-separated list of tags to filter tests (e.g., outbound,relay)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List tests without running them",
    )
    parser.add_argument(
        "--list-tags",
        action="store_true",
        help="List available tags and generators",
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

    # List tags/generators
    if args.list_tags:
        print("Available tags:")
        for tag in Tag:
            print(f"  {tag.name.lower()}")
        print("\nRegistered generators:")
        for gen in list_generators():
            tags_str = ", ".join(gen["tags"])
            print(f"  {gen['name']} [{tags_str}] - {gen['doc']}")
        return 0

    # Load configuration
    config = TestConfig.load(
        values_file=args.values,
        chart_dir=args.chart_dir,
    )

    if args.debug:
        print("\n=== Merged Values ===")
        print(yaml.dump(config._raw, default_flow_style=False))  # pyright: ignore[reportPrivateUsage]
        print("=" * 40)

    # Parse tags filter
    tags_filter: Optional[set[Tag]] = None
    if args.tags:
        tags_filter = parse_tags(args.tags)
        if not tags_filter:
            print("Error: No valid tags specified")
            return 1

    # Generate tests
    tests = generate_tests(config, tags=tags_filter)

    # Add real delivery test if requested
    if args.real_recipient:
        tests.append(
            TestCase(
                name="real_delivery",
                description=f"Real delivery test to {args.real_recipient}",
                network="internal",
                mail_from=config.get_allowed_sender(),
                rcpt_to=args.real_recipient,
                subject=args.real_subject,
                body=f"This is a test email from mail-relay.\n\n"
                f"Timestamp: {datetime.now().isoformat()}\n"
                f"Server: {args.internal}\n",
                expect_accept=True,
                tags={Tag.DELIVERY},
            )
        )

    # Filter by network if specified
    if args.only:
        tests = [t for t in tests if t.network == args.only]

    # List tests
    if args.list:
        print("Generated tests:")
        for t in tests:
            expect = "ACCEPT" if t.expect_accept else "REJECT"
            tags_str = ",".join(tag.name.lower() for tag in t.tags) if t.tags else ""
            print(
                f"  [{t.network}] {t.name}: "
                f"<{t.mail_from}> → <{t.rcpt_to}> "
                f"(expect {expect}) [{tags_str}]"
            )
        print(f"\nTotal: {len(tests)} tests")
        return 0

    # Create runner
    runner = SmtpTestRunner(
        config=config,
        internal_server=args.internal,
        external_server=args.external,
    )

    # Print header
    if not args.short:
        print("Mail Relay Test Suite")
        print(f"Values: {args.values}")
        print(f"Internal: {runner.internal_server}")
        print(f"External: {runner.external_server}")
        if tags_filter:
            print(f"Tags filter: {', '.join(t.name.lower() for t in tags_filter)}")
        print(f"Tests: {len(tests)}")

    # Run tests with callback for output
    def on_test_complete(test: TestCase, result: TestResult) -> None:
        if args.short:
            print_test_short(test, result)
        else:
            server = (
                runner.internal_server
                if test.network == "internal"
                else runner.external_server
            )
            print_test_verbose(test, result, server)

    runner.run_tests(tests, callback=on_test_complete)

    # Print summary
    success = print_summary(runner, short=args.short)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
