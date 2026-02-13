#!/usr/bin/env python3
"""
Proxy Protocol Configuration Script

Auto-detects trusted proxy IPs from LoadBalancer service status
and updates Haraka's connection.ini configuration.

Usage:
    python3 proxy_config.py --config-file /app/config/connection.ini

Environment variables:
    PROXY_PROTOCOL_AUTO_DETECT: Enable auto-detection (true/false)
    SERVICE_NAME: Kubernetes service name
    NAMESPACE: Kubernetes namespace
    STATIC_TRUSTED_PROXIES: Comma-separated list of additional IPs/CIDRs
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Optional

# Setup path for local imports
sys.path.insert(0, str(Path(__file__).parent))

from utils.k8s import KubernetesClient, KubernetesConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def get_static_proxies() -> list[str]:
    """Get static trusted proxies from environment"""
    static_env = os.environ.get("STATIC_TRUSTED_PROXIES", "")
    if not static_env:
        return []

    proxies: list[str] = []
    for item in static_env.split(","):
        item = item.strip()
        if item:
            proxies.append(item)

    return proxies


def detect_lb_ips(
    k8s_client: KubernetesClient, service_name: Optional[str] = None
) -> list[str]:
    """
    Detect all IPs from LoadBalancer service status.

    Args:
        k8s_client: Kubernetes client instance
        service_name: Service name to query (uses config default if not specified)

    Returns:
        List of IP addresses from LoadBalancer ingress
    """
    original_name: Optional[str] = None
    if service_name:
        # Temporarily override service name
        original_name = k8s_client.config.service_name
        k8s_client.config.service_name = service_name

    try:
        ips = k8s_client.get_loadbalancer_ips()
        logger.info(f"Detected LoadBalancer IPs: {ips}")
        return ips
    finally:
        if service_name and original_name is not None:
            k8s_client.config.service_name = original_name


def generate_hosts_config(ips: list[str]) -> str:
    """
    Generate hosts[] entries for Haraka connection.ini [haproxy] section.

    Args:
        ips: List of IP addresses or CIDRs

    Returns:
        Multi-line string with hosts[] entries
    """
    if not ips:
        return "; No trusted proxies configured\nhosts[] ="

    lines: list[str] = []
    for ip in ips:
        lines.append(f"hosts[]={ip}")

    return "\n".join(lines)


def update_connection_ini(config_file: Path, hosts_config: str) -> bool:
    """
    Update connection.ini with detected proxy hosts.

    Replaces __AUTO_DETECT_PROXIES__ placeholder with actual hosts[] entries.

    Args:
        config_file: Path to connection.ini
        hosts_config: Generated hosts[] configuration

    Returns:
        True if file was updated, False otherwise
    """
    if not config_file.exists():
        logger.error(f"Config file not found: {config_file}")
        return False

    content = config_file.read_text()

    # Check for placeholder
    placeholder = "__AUTO_DETECT_PROXIES__"
    if placeholder not in content:
        logger.info("No placeholder found in config, skipping update")
        return False

    # Replace placeholder with hosts config
    # Handle both single-line and multi-line replacement
    new_content = content.replace(placeholder, hosts_config)

    config_file.write_text(new_content)
    logger.info(f"Updated {config_file} with trusted proxies configuration")

    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Configure Haraka trusted proxies from LoadBalancer"
    )
    parser.add_argument(
        "--config-file",
        type=Path,
        default=Path("/app/config/connection.ini"),
        help="Path to connection.ini file",
    )
    parser.add_argument(
        "--service-name",
        type=str,
        default=os.environ.get("SERVICE_NAME", ""),
        help="Kubernetes service name to query",
    )
    parser.add_argument(
        "--namespace",
        type=str,
        default=os.environ.get("NAMESPACE", "default"),
        help="Kubernetes namespace",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print configuration without modifying files",
    )

    args = parser.parse_args()

    # Check if auto-detect is enabled
    auto_detect = (
        os.environ.get("PROXY_PROTOCOL_AUTO_DETECT", "false").lower() == "true"
    )

    if not auto_detect:
        logger.info("PROXY_PROTOCOL_AUTO_DETECT is not enabled, skipping")
        return 0

    logger.info("=== Proxy Protocol Auto-Configuration ===")

    # Collect all trusted proxies
    all_proxies: list[str] = []

    # 1. Get static proxies
    static_proxies = get_static_proxies()
    if static_proxies:
        logger.info(f"Static trusted proxies: {static_proxies}")
        all_proxies.extend(static_proxies)

    # 2. Auto-detect from LoadBalancer
    if args.service_name:
        try:
            k8s_config = KubernetesConfig(
                namespace=args.namespace,
                service_name=args.service_name,
            )
            k8s_client = KubernetesClient(k8s_config)

            lb_ips = detect_lb_ips(k8s_client)
            if lb_ips:
                all_proxies.extend(lb_ips)
        except Exception as e:
            logger.warning(f"Failed to detect LoadBalancer IPs: {e}")

    # Remove duplicates while preserving order
    seen: set[str] = set()
    unique_proxies: list[str] = []
    for proxy in all_proxies:
        if proxy not in seen:
            seen.add(proxy)
            unique_proxies.append(proxy)

    logger.info(f"All trusted proxies: {unique_proxies}")

    # Generate config
    hosts_config = generate_hosts_config(unique_proxies)

    if args.dry_run:
        print("\n=== Generated hosts configuration ===")
        print(hosts_config)
        print("=====================================\n")
        return 0

    # Update config file
    if update_connection_ini(args.config_file, hosts_config):
        logger.info("Configuration updated successfully")
    else:
        logger.warning("Configuration was not updated")

    return 0


if __name__ == "__main__":
    sys.exit(main())
