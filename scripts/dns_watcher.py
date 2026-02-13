#!/usr/bin/env python3
"""
DNS Watcher - Monitors IP changes and updates DNS records.

Runs as a sidecar container, periodically checking for IP changes
and DNS record correctness. When a change is detected, updates DNS
records and signals the main container to restart (via kill marker file).

Monitors:
- Incoming IP (LoadBalancer/NodePort/NAT)
- Outbound IP (NAT for SPF records)
- DNS record correctness (A, MX, SPF, DKIM, DMARC)

Usage:
    dns_watcher.py [--interval SECONDS] [--shared-dir PATH]
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, Optional

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent))

from dns_providers.registry import get_provider_from_env
from dns_manager import DNSManager, MailConfig
from utils.ip import IPDetector, IPDetectorConfig
from utils.k8s import KubernetesClient, KubernetesConfig

# Event for graceful shutdown (can be set from signal handler to wake up sleeps)
shutdown_event = threading.Event()


@dataclass
class SavedState:
    """Saved state for tracking changes"""

    incoming_ip: str = ""
    outbound_ip: str = ""
    all_ips: Optional[list[str]] = None

    def __post_init__(self) -> None:
        if self.all_ips is None:
            self.all_ips = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "incoming_ip": self.incoming_ip,
            "outbound_ip": self.outbound_ip,
            "all_ips": self.all_ips,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SavedState":
        return cls(
            incoming_ip=str(data.get("incoming_ip", "")),
            outbound_ip=str(data.get("outbound_ip", "")),
            all_ips=list(data.get("all_ips", [])),
        )


def signal_handler(signum: int, frame: Optional[FrameType]) -> None:
    """Handle termination signals"""
    logging.info(f"Received signal {signum}, initiating shutdown...")
    shutdown_event.set()


def setup_logging(verbose: bool = False) -> None:
    """Configure logging"""
    level = logging.DEBUG if verbose else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def read_saved_state(shared_dir: Path) -> SavedState:
    """Read saved state from shared volume"""
    state_file = shared_dir / "dns-state.json"
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text())
            return SavedState.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            pass

    # Fallback: read legacy current-ip file
    ip_file = shared_dir / "current-ip"
    if ip_file.exists():
        return SavedState(incoming_ip=ip_file.read_text().strip())

    return SavedState()


def save_state(shared_dir: Path, state: SavedState) -> None:
    """Save state to shared volume"""
    state_file = shared_dir / "dns-state.json"
    state_file.write_text(json.dumps(state.to_dict()))

    # Also write legacy file for backward compatibility
    ip_file = shared_dir / "current-ip"
    if state.incoming_ip:
        ip_file.write_text(state.incoming_ip)


def create_kill_marker(shared_dir: Path) -> None:
    """Create marker file to signal pod restart"""
    marker_file = shared_dir / "kill-pod"
    marker_file.touch()


def main() -> None:
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

    # Wait for init container to save initial state
    logger.info("Waiting for initial state from init container...")
    saved_state = SavedState()  # Initialize with empty state
    while not shutdown_event.is_set():
        saved_state = read_saved_state(shared_dir)
        if saved_state.incoming_ip:
            break
        shutdown_event.wait(1)

    if shutdown_event.is_set():
        logger.info("Shutdown requested, exiting")
        return

    logger.info(f"Initial incoming IP: {saved_state.incoming_ip}")
    logger.info(f"Initial outbound IP: {saved_state.outbound_ip or 'not tracked'}")
    logger.info(f"Initial all IPs: {saved_state.all_ips or [saved_state.incoming_ip]}")
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

        # Detect current IPs
        current_incoming = ip_detector.get_incoming_ip(k8s, wait_timeout=0)
        current_outbound = ip_detector.detect_outbound_ip()
        current_all_ips = ip_detector.get_all_ips(k8s, wait_timeout=0)

        if not current_incoming:
            logger.warning("Could not detect current incoming IP")
            continue

        saved_state = read_saved_state(shared_dir)
        needs_update = False
        reasons: list[str] = []

        # Check for IP changes (both incoming and outbound)
        if current_incoming != saved_state.incoming_ip:
            reasons.append(
                f"incoming IP: {saved_state.incoming_ip} -> {current_incoming}"
            )
            needs_update = True

        if current_outbound and current_outbound != saved_state.outbound_ip:
            reasons.append(
                f"outbound IP: {saved_state.outbound_ip} -> {current_outbound}"
            )
            needs_update = True

        # Check if all IPs list changed (important for SPF)
        saved_all_ips = set(saved_state.all_ips or [saved_state.incoming_ip])
        if set(current_all_ips) != saved_all_ips:
            reasons.append(
                f"all IPs: {sorted(saved_all_ips)} -> {sorted(current_all_ips)}"
            )
            needs_update = True

        # Verify DNS records are correct
        if not needs_update:
            dns_correct, dns_issues = manager.check_records(
                current_incoming, current_all_ips
            )
            if not dns_correct:
                reasons.append(f"DNS records incorrect: {dns_issues[:3]}")  # Limit to 3
                needs_update = True

        if needs_update:
            logger.info("")
            logger.info("=" * 50)
            logger.info("CHANGE DETECTED - DNS UPDATE REQUIRED")
            logger.info("=" * 50)
            for reason in reasons:
                logger.info(f"  - {reason}")
            logger.info("")

            # Update DNS records
            logger.info("Updating DNS records...")
            success = manager.init_or_update(wait_for_lb=0)

            if success:
                # Save new state
                new_state = SavedState(
                    incoming_ip=current_incoming,
                    outbound_ip=current_outbound or "",
                    all_ips=current_all_ips,
                )
                save_state(shared_dir, new_state)

                # Only signal pod restart if incoming IP changed (affects service)
                if current_incoming != saved_state.incoming_ip:
                    logger.info("Creating kill marker to trigger pod restart...")
                    create_kill_marker(shared_dir)

                    # Wait for pod termination (interruptible)
                    logger.info("Waiting for liveness probe to detect kill marker...")
                    shutdown_event.wait(60)
                else:
                    logger.info("DNS records updated (no pod restart needed)")
            else:
                logger.error("DNS update failed, will retry on next check")
        else:
            # Periodic heartbeat at INFO level
            if check_count % heartbeat_interval == 0:
                logger.info(
                    f"Heartbeat: IPs unchanged (in:{current_incoming}, out:{current_outbound}), "
                    f"DNS verified OK, {check_count} checks completed"
                )
            else:
                logger.debug(
                    f"IPs unchanged: in={current_incoming}, out={current_outbound}"
                )

    logger.info("DNS Watcher stopped")


if __name__ == "__main__":
    main()
