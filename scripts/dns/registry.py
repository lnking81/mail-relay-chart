"""
DNS Provider Registry

Factory for creating DNS provider instances based on configuration.
"""

import logging
import os
from typing import Any, Optional, Type

from .base import DNSProvider, DNSProviderConfig

logger = logging.getLogger(__name__)

# Provider registry
_providers: dict[str, Type[DNSProvider]] = {}


def register_provider(name: str, provider_class: Type[DNSProvider]) -> None:
    """Register a DNS provider class"""
    _providers[name.lower()] = provider_class
    logger.debug(f"Registered DNS provider: {name}")


def get_provider(
    provider_name: str,
    owner_id: str,
    **kwargs: Any,
) -> Optional[DNSProvider]:
    """
    Get a configured DNS provider instance.

    Args:
        provider_name: Provider type (e.g., "cloudflare")
        owner_id: Ownership identifier for record tracking
        **kwargs: Provider-specific configuration

    Returns:
        Configured DNSProvider instance or None
    """
    provider_name = provider_name.lower()

    if provider_name not in _providers:
        logger.error(f"Unknown DNS provider: {provider_name}")
        logger.info(f"Available providers: {list(_providers.keys())}")
        return None

    provider_class = _providers[provider_name]

    # Create provider-specific config
    if provider_name == "cloudflare":
        from .cloudflare import CloudflareConfig

        cf_config = CloudflareConfig.from_env(owner_id)

        # Override with kwargs
        for key, value in kwargs.items():
            if hasattr(cf_config, key):
                setattr(cf_config, key, value)

        return provider_class(cf_config)

    if provider_name == "hetzner":
        from .hetzner import HetznerConfig

        hz_config = HetznerConfig.from_env(owner_id)

        # Override with kwargs
        for key, value in kwargs.items():
            if hasattr(hz_config, key):
                setattr(hz_config, key, value)

        return provider_class(hz_config)

    # Generic provider
    generic_config = DNSProviderConfig(owner_id=owner_id, **kwargs)
    return provider_class(generic_config)


def get_provider_from_env() -> Optional[DNSProvider]:
    """
    Create DNS provider from environment variables.

    Environment variables:
        DNS_PROVIDER: Provider name (cloudflare, etc.)
        DNS_OWNER_ID: Ownership identifier
        NAMESPACE: Kubernetes namespace (fallback for owner_id)
        RELEASE_NAME: Helm release name (fallback for owner_id)

    Provider-specific variables are handled by each provider.
    """
    provider_name = os.environ.get("DNS_PROVIDER", "")

    if not provider_name:
        logger.error("DNS_PROVIDER environment variable not set")
        return None

    # Build owner_id from namespace/release or custom value
    owner_id = os.environ.get("DNS_OWNER_ID", "")
    if not owner_id:
        namespace = os.environ.get("NAMESPACE", "default")
        release_name = os.environ.get("RELEASE_NAME", "mail-relay")
        owner_id = f"{namespace}/{release_name}"

    logger.info(f"Creating DNS provider: {provider_name} (owner: {owner_id})")

    return get_provider(provider_name, owner_id)


# Auto-register built-in providers
def _register_builtin_providers() -> None:
    """Register all built-in providers"""
    try:
        from .cloudflare import CloudflareProvider

        register_provider("cloudflare", CloudflareProvider)
    except ImportError as e:
        logger.warning(f"Could not load Cloudflare provider: {e}")

    try:
        from .hetzner import HetznerProvider

        register_provider("hetzner", HetznerProvider)
    except ImportError as e:
        logger.warning(f"Could not load Hetzner provider: {e}")


_register_builtin_providers()
