#!/usr/bin/env python3
"""
Blacklist Monitor - Entry point.

Monitors IPs and domains against DNSBL/URIBL lists, exposes Prometheus metrics,
and sends alerts via email/webhooks.

Usage:
    blacklist_monitor.py [--interval SECONDS] [--metrics-port PORT]
    blacklist_monitor.py --check-once --ips 1.2.3.4 --domains example.com
"""

import argparse
import logging
import os
import signal
import sys
import threading
from types import FrameType

from dnsbl import BlacklistConfig, BlacklistMonitor

# Shutdown event
shutdown_event = threading.Event()


def signal_handler(signum: int, frame: FrameType | None) -> None:
    """Handle termination signals."""
    logging.info(f"Received signal {signum}, initiating shutdown...")
    shutdown_event.set()


def setup_logging(verbose: bool = False) -> None:
    """Configure logging."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Blacklist Monitor for Mail Relay")

    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("BLACKLIST_INTERVAL", "3600")),
        help="Check interval in seconds (default: 3600)",
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=int(os.environ.get("BLACKLIST_METRICS_PORT", "8095")),
        help="Prometheus metrics port (default: 8095)",
    )
    parser.add_argument(
        "--shared-dir",
        type=str,
        default=os.environ.get("SHARED_DIR", "/shared"),
        help="Shared volume directory (default: /shared)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--check-once",
        action="store_true",
        help="Run a single check and exit (for testing)",
    )
    parser.add_argument(
        "--ips",
        type=str,
        default="",
        help="Comma-separated IPs to check (for testing)",
    )
    parser.add_argument(
        "--domains",
        type=str,
        default="",
        help="Comma-separated domains to check (for testing)",
    )
    parser.add_argument(
        "--dns-server",
        type=str,
        default=os.environ.get("BLACKLIST_DNS_SERVER", ""),
        help="Custom DNS server IP (optional)",
    )

    return parser.parse_args()


def main() -> None:
    """Main entry point."""
    args = parse_args()

    setup_logging(args.verbose)

    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Load config
    config = BlacklistConfig.from_env()
    config.interval = args.interval
    config.metrics_port = args.metrics_port
    config.shared_dir = args.shared_dir
    if args.dns_server:
        config.dns_server = args.dns_server

    # Create monitor
    monitor = BlacklistMonitor(config, shutdown_event)

    # Check-once mode
    if args.check_once:
        ips = (
            [ip.strip() for ip in args.ips.split(",") if ip.strip()] if args.ips else []
        )
        domains = (
            [d.strip() for d in args.domains.split(",") if d.strip()]
            if args.domains
            else config.domains
        )

        if not ips and not domains:
            logging.error("No IPs or domains to check. Use --ips and/or --domains")
            sys.exit(1)

        exit_code = monitor.check_once(ips, domains)
        sys.exit(exit_code)

    # Normal mode - start monitoring
    monitor.start()


if __name__ == "__main__":
    main()
