"""Prometheus metrics server for blacklist monitoring."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from .models import BlacklistResult


class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP handler for Prometheus metrics endpoint."""

    # Class-level state shared across requests
    ip_results: list[BlacklistResult] = []
    domain_results: list[BlacklistResult] = []
    check_count: int = 0
    listing_events: dict[str, int] = {}  # "type:target:dnsbl" -> count

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default HTTP logging."""

    def handle_one_request(self) -> None:
        """Handle request, suppressing connection errors from health probes."""
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def do_GET(self) -> None:
        if self.path == "/metrics":
            self._send_metrics()
        elif self.path == "/health":
            self._send_health()
        else:
            self.send_response(404)
            self.end_headers()

    def _send_health(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    @staticmethod
    def _escape_label_value(value: str) -> str:
        """Escape special characters in Prometheus label values."""
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    def _send_metrics(self) -> None:
        """Generate Prometheus metrics."""
        lines: list[str] = []

        # IP blacklist status
        lines.append(
            "# HELP mail_relay_blacklist_status IP blacklist status (1=listed, 0=clean)"
        )
        lines.append("# TYPE mail_relay_blacklist_status gauge")
        for result in self.ip_results:
            status = 1 if result.listed else 0
            reason = self._escape_label_value(result.reason) if result.reason else ""
            lines.append(
                f'mail_relay_blacklist_status{{ip="{result.target}",list="{result.dnsbl}",reason="{reason}"}} {status}'
            )

        # Domain blacklist status
        lines.append("")
        lines.append(
            "# HELP mail_relay_domain_blacklist_status Domain blacklist status (1=listed, 0=clean)"
        )
        lines.append("# TYPE mail_relay_domain_blacklist_status gauge")
        for result in self.domain_results:
            status = 1 if result.listed else 0
            reason = self._escape_label_value(result.reason) if result.reason else ""
            lines.append(
                f'mail_relay_domain_blacklist_status{{domain="{result.target}",list="{result.dnsbl}",reason="{reason}"}} {status}'
            )

        # Total checks
        lines.append("")
        lines.append(
            "# HELP mail_relay_blacklist_checks_total Total number of blacklist checks performed"
        )
        lines.append("# TYPE mail_relay_blacklist_checks_total counter")
        lines.append(f"mail_relay_blacklist_checks_total {self.check_count}")

        # Listing events
        lines.append("")
        lines.append(
            "# HELP mail_relay_blacklist_listed_total Total times target was found on a blacklist"
        )
        lines.append("# TYPE mail_relay_blacklist_listed_total counter")
        for key, count in self.listing_events.items():
            parts = key.split(":", 2)
            if len(parts) == 3:
                target_type, target, dnsbl = parts
                if target_type == "ip":
                    lines.append(
                        f'mail_relay_blacklist_listed_total{{ip="{target}",list="{dnsbl}"}} {count}'
                    )
                else:
                    lines.append(
                        f'mail_relay_blacklist_listed_total{{domain="{target}",list="{dnsbl}"}} {count}'
                    )

        # Summary counts
        lines.append("")
        lines.append(
            "# HELP mail_relay_blacklist_ip_listed_count Number of IP blacklists where IP is listed"
        )
        lines.append("# TYPE mail_relay_blacklist_ip_listed_count gauge")
        ip_listed = sum(1 for r in self.ip_results if r.listed)
        if self.ip_results:
            lines.append(
                f'mail_relay_blacklist_ip_listed_count{{ip="{self.ip_results[0].target}"}} {ip_listed}'
            )

        lines.append("")
        lines.append(
            "# HELP mail_relay_blacklist_domain_listed_count Number of domain blacklists where domain is listed"
        )
        lines.append("# TYPE mail_relay_blacklist_domain_listed_count gauge")
        domains_seen: set[str] = set()
        for result in self.domain_results:
            if result.target not in domains_seen:
                domain_listed = sum(
                    1
                    for r in self.domain_results
                    if r.target == result.target and r.listed
                )
                lines.append(
                    f'mail_relay_blacklist_domain_listed_count{{domain="{result.target}"}} {domain_listed}'
                )
                domains_seen.add(result.target)

        # Last check timestamp
        lines.append("")
        lines.append(
            "# HELP mail_relay_blacklist_last_check_timestamp Unix timestamp of last check"
        )
        lines.append("# TYPE mail_relay_blacklist_last_check_timestamp gauge")
        all_results = self.ip_results + self.domain_results
        if all_results:
            ts = int(all_results[0].check_time.timestamp())
            lines.append(f"mail_relay_blacklist_last_check_timestamp {ts}")

        content = "\n".join(lines) + "\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode())


class MetricsServer:
    """Prometheus metrics server."""

    def __init__(self, port: int, shutdown_event: threading.Event):
        self.port = port
        self.shutdown_event = shutdown_event
        self.server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the metrics server in a background thread."""
        self.server = HTTPServer(("0.0.0.0", self.port), MetricsHandler)
        self.server.timeout = 1

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """Run the server until shutdown."""
        while not self.shutdown_event.is_set():
            if self.server:
                self.server.handle_request()

    def update_results(
        self,
        ip_results: list[BlacklistResult],
        domain_results: list[BlacklistResult],
        check_count: int,
    ) -> None:
        """Update metrics with new results."""
        MetricsHandler.ip_results = ip_results
        MetricsHandler.domain_results = domain_results
        MetricsHandler.check_count = check_count

        # Track listing events
        for r in ip_results:
            if r.listed:
                key = f"ip:{r.target}:{r.dnsbl}"
                MetricsHandler.listing_events[key] = (
                    MetricsHandler.listing_events.get(key, 0) + 1
                )

        for r in domain_results:
            if r.listed:
                key = f"domain:{r.target}:{r.dnsbl}"
                MetricsHandler.listing_events[key] = (
                    MetricsHandler.listing_events.get(key, 0) + 1
                )
