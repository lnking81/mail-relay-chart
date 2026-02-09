#!/usr/bin/env python3
"""
DNS Watcher - Monitors IP changes and updates DNS records.

Runs as a sidecar container, periodically checking for IP changes.
When a change is detected, updates DNS records and signals the main
container to restart (via kill marker file).

Usage:
    dns_watcher.py [--interval SECONDS] [--shared-dir PATH]
"""

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent))

from dns import get_provider_from_env
from dns_manager import DNSManager, MailConfig
from utils.ip import IPDetector, IPDetectorConfig
from utils.k8s import KubernetesClient, KubernetesConfig

# Event for graceful shutdown (can be set from signal handler to wake up sleeps)
shutdown_event = threading.Event()


def signal_handler(signum, frame):
    """Handle termination signals"""
    logging.info(f"Received signal {signum}, initiating shutdown...")
    shutdown_event.set()


def setup_logging(verbose: bool = False):
    """Configure logging"""
    level = logging.DEBUG if verbose else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def read_saved_ip(shared_dir: Path) -> str:
    """Read saved IP from shared volume"""
    ip_file = shared_dir / "current-ip"
    if ip_file.exists():
        return ip_file.read_text().strip()
    return ""


def save_ip(shared_dir: Path, ip: str):
    """Save IP to shared volume"""
    ip_file = shared_dir / "current-ip"
    ip_file.write_text(ip)


def create_kill_marker(shared_dir: Path):
    """Create marker file to signal pod restart"""
    marker_file = shared_dir / "kill-pod"
    marker_file.touch()


def main():
    parser = argparse.ArgumentParser(description="DNS Watcher for Mail Relay")

    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("DNS_WATCHER_INTERVAL", "60")),
        help="Check interval in seconds (default: 60)",
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

    args = parser.parse_args()

    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    shared_dir = Path(args.shared_dir)

    logger.info("=" * 50)
    logger.info("DNS Watcher Starting")
    logger.info("=" * 50)
    logger.info(f"Check interval: {args.interval}s")
    logger.info(f"Shared dir: {shared_dir}")

    # Initialize components
    provider = get_provider_from_env()
    if not provider:
        logger.error("Failed to initialize DNS provider")
        sys.exit(1)

    mail_config = MailConfig.from_env()
    k8s = KubernetesClient(KubernetesConfig.from_env())
    ip_detector = IPDetector(IPDetectorConfig.from_env())

    manager = DNSManager(provider, mail_config, k8s, ip_detector)

    # Wait for init container to save initial IP
    logger.info("Waiting for initial IP from init container...")
    while not shutdown_event.is_set():
        saved_ip = read_saved_ip(shared_dir)
        if saved_ip:
            break
        shutdown_event.wait(1)

    if shutdown_event.is_set():
        logger.info("Shutdown requested, exiting")
        return

    logger.info(f"Initial IP: {saved_ip}")
    logger.info("")

    # Heartbeat counter
    check_count = 0
    heartbeat_interval = 10  # Log heartbeat every N checks

    # Main monitoring loop
    while not shutdown_event.is_set():
        # Wait for interval or shutdown signal (whichever comes first)
        if shutdown_event.wait(args.interval):
            break

        check_count += 1

        # Detect current IP
        current_ip = ip_detector.get_incoming_ip(k8s, wait_timeout=0)

        if not current_ip:
            logger.warning("Could not detect current IP")
            continue

        saved_ip = read_saved_ip(shared_dir)

        if current_ip != saved_ip:
            logger.info("")
            logger.info("=" * 50)
            logger.info("IP CHANGE DETECTED")
            logger.info("=" * 50)
            logger.info(f"Previous IP: {saved_ip}")
            logger.info(f"Current IP:  {current_ip}")
            logger.info("")

            # Update DNS records
            logger.info("Updating DNS records...")
            success = manager.init_or_update(wait_for_lb=0)

            if success:
                # Save new IP
                save_ip(shared_dir, current_ip)

                # Signal pod restart
                logger.info("Creating kill marker to trigger pod restart...")
                create_kill_marker(shared_dir)

                # Wait for pod termination (interruptible)
                logger.info("Waiting for liveness probe to detect kill marker...")
                shutdown_event.wait(60)
            else:
                logger.error("DNS update failed, will retry on next check")
        else:
            # Periodic heartbeat at INFO level
            if check_count % heartbeat_interval == 0:
                logger.info(
                    f"Heartbeat: IP unchanged ({current_ip}), {check_count} checks completed"
                )
            else:
                logger.debug(f"IP unchanged: {current_ip}")

    logger.info("DNS Watcher stopped")


if __name__ == "__main__":
    main()
