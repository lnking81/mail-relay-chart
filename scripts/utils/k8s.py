"""
Kubernetes Client Utilities

Provides simplified Kubernetes API access for DNS management scripts.
"""

import base64
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class KubernetesConfig:
    """Kubernetes client configuration"""

    namespace: str = "default"
    service_name: str = ""
    release_name: str = ""

    # Use kubectl CLI (fallback if kubernetes library not available)
    use_kubectl: bool = True

    @classmethod
    def from_env(cls) -> "KubernetesConfig":
        """Create config from environment variables"""
        return cls(
            namespace=os.environ.get("NAMESPACE", "default"),
            service_name=os.environ.get("SERVICE_NAME", ""),
            release_name=os.environ.get("RELEASE_NAME", ""),
            use_kubectl=os.environ.get("USE_KUBECTL", "true").lower() == "true",
        )


class KubernetesClient:
    """
    Simplified Kubernetes client for DNS management.

    Uses kubectl CLI for maximum compatibility.
    """

    def __init__(self, config: Optional[KubernetesConfig] = None):
        self.config = config or KubernetesConfig.from_env()
        self.logger = logging.getLogger(__name__)

    def _kubectl(self, *args: str, timeout: int = 30) -> tuple[bool, str]:
        """Execute kubectl command"""
        cmd = ["kubectl", "-n", self.config.namespace, *args]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            if result.returncode == 0:
                return True, result.stdout.strip()
            else:
                self.logger.debug(f"kubectl failed: {result.stderr}")
                return False, result.stderr.strip()

        except subprocess.TimeoutExpired:
            self.logger.error(f"kubectl timeout: {' '.join(cmd)}")
            return False, "timeout"
        except Exception as e:
            self.logger.error(f"kubectl error: {e}")
            return False, str(e)

    def get_service_type(self) -> Optional[str]:
        """Get service type (LoadBalancer, NodePort, ClusterIP)"""
        success, output = self._kubectl(
            "get", "svc", self.config.service_name, "-o", "jsonpath={.spec.type}"
        )
        return output if success else None

    def get_loadbalancer_ip(self) -> Optional[str]:
        """Get LoadBalancer external IP (first one)"""
        ips = self.get_loadbalancer_ips()
        return ips[0] if ips else None

    def get_loadbalancer_ips(self) -> list[str]:
        """
        Get all LoadBalancer IPs from service status.

        Returns all IPs from status.loadBalancer.ingress[*].ip
        This includes both external IP and internal/node IPs that
        some cloud providers expose (e.g., Hetzner with ipMode: Proxy).
        """
        ips: list[str] = []

        # Get all IPs from ingress array
        success, output = self._kubectl(
            "get",
            "svc",
            self.config.service_name,
            "-o",
            'jsonpath={range .status.loadBalancer.ingress[*]}{.ip}{"\\n"}{end}',
        )

        if success and output:
            for line in output.strip().split("\n"):
                ip = line.strip()
                if ip and ip != "null" and ip not in ips:
                    ips.append(ip)

        # Also try hostnames and resolve them (AWS NLB)
        success, output = self._kubectl(
            "get",
            "svc",
            self.config.service_name,
            "-o",
            'jsonpath={range .status.loadBalancer.ingress[*]}{.hostname}{"\\n"}{end}',
        )

        if success and output:
            import socket

            for line in output.strip().split("\n"):
                hostname = line.strip()
                if hostname and hostname != "null":
                    try:
                        resolved_ip = socket.gethostbyname(hostname)
                        if resolved_ip not in ips:
                            ips.append(resolved_ip)
                    except Exception as e:
                        self.logger.debug(
                            f"Could not resolve LB hostname {hostname}: {e}"
                        )

        return ips

    def get_node_external_ip(self) -> Optional[str]:
        """Get external IP of the node running this pod"""
        # Get node name from pod
        pod_name = os.environ.get("HOSTNAME", "")
        if not pod_name:
            return None

        success, node_name = self._kubectl(
            "get", "pod", pod_name, "-o", "jsonpath={.spec.nodeName}"
        )

        if not success or not node_name:
            return None

        # Get node external IP
        success, external_ip = self._kubectl(
            "get",
            "node",
            node_name,
            "-o",
            'jsonpath={.status.addresses[?(@.type=="ExternalIP")].address}',
        )

        if success and external_ip:
            return external_ip

        # Fallback to internal IP
        success, internal_ip = self._kubectl(
            "get",
            "node",
            node_name,
            "-o",
            'jsonpath={.status.addresses[?(@.type=="InternalIP")].address}',
        )

        if success and internal_ip:
            self.logger.warning(f"Using node InternalIP: {internal_ip}")
            return internal_ip

        return None

    def get_service_ip(self, wait_timeout: int = 0) -> Optional[str]:
        """
        Get service external IP based on service type.

        Args:
            wait_timeout: Seconds to wait for LoadBalancer IP

        Returns:
            IP address or None
        """
        service_type = self.get_service_type()

        if service_type == "LoadBalancer":
            return self._wait_for_loadbalancer_ip(wait_timeout)
        elif service_type == "NodePort":
            return self.get_node_external_ip()

        return None

    def _wait_for_loadbalancer_ip(self, timeout: int) -> Optional[str]:
        """Wait for LoadBalancer IP with timeout"""
        start_time = time.time()

        while True:
            ip = self.get_loadbalancer_ip()
            if ip:
                return ip

            if timeout == 0:
                return None

            elapsed = time.time() - start_time
            if elapsed >= timeout:
                self.logger.error(
                    f"Timeout waiting for LoadBalancer IP after {timeout}s"
                )
                return None

            self.logger.info(
                f"Waiting for LoadBalancer IP... ({int(elapsed)}s/{timeout}s)"
            )
            time.sleep(5)

    def get_secret_data(self, secret_name: str, key: str) -> Optional[str]:
        """Get decoded data from a secret"""
        # Escape dots in key for jsonpath (e.g., "dns.record" -> "dns\.record")
        escaped_key = key.replace(".", r"\.")
        success, data = self._kubectl(
            "get", "secret", secret_name, "-o", f"jsonpath={{.data['{escaped_key}']}}"
        )

        if success and data:
            try:
                return base64.b64decode(data).decode("utf-8")
            except Exception as e:
                self.logger.error(f"Failed to decode secret {secret_name}/{key}: {e}")

        return None

    def get_dkim_record(self, domain: str) -> Optional[str]:
        """Get DKIM DNS record value from secret"""
        # Secret name format: {fullname}-dkim-{domain-with-dashes}
        # fullname = service_name (includes release name + chart name suffix)
        secret_name = f"{self.config.service_name}-dkim-{domain.replace('.', '-')}"
        return self.get_secret_data(secret_name, "dns.record")

    def get_dkim_selector(self, domain: str) -> Optional[str]:
        """Get DKIM selector from secret"""
        secret_name = f"{self.config.service_name}-dkim-{domain.replace('.', '-')}"
        return self.get_secret_data(secret_name, "selector")
