"""Alert management for blacklist monitoring (email, webhooks)."""

import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import requests

from .config import BlacklistConfig
from .models import BlacklistResult


class AlertManager:
    """Manages email and webhook alerts."""

    def __init__(self, config: BlacklistConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self._cooldown_cache: dict[
            str, datetime
        ] = {}  # "type:target:dnsbl" -> last alert time

    def _should_alert(self, result: BlacklistResult) -> bool:
        """Check if we should send an alert (respecting cooldown)."""
        key = f"{result.target_type}:{result.target}:{result.dnsbl}"
        last_alert = self._cooldown_cache.get(key)

        if last_alert is None:
            return True

        cooldown = timedelta(hours=self.config.alert_cooldown_hours)
        return datetime.now(timezone.utc) - last_alert > cooldown

    def _mark_alerted(self, result: BlacklistResult) -> None:
        """Mark that we've sent an alert for this target/DNSBL combo."""
        key = f"{result.target_type}:{result.target}:{result.dnsbl}"
        self._cooldown_cache[key] = datetime.now(timezone.utc)

    def send_alerts(
        self,
        ip_results: list[BlacklistResult],
        domain_results: list[BlacklistResult],
    ) -> None:
        """Send all configured alerts for listed IPs/domains."""
        ip_listed = [r for r in ip_results if r.listed]
        domain_listed = [r for r in domain_results if r.listed]

        if not ip_listed and not domain_listed:
            return

        self._send_email_alert(ip_listed, domain_listed)
        self._send_webhook_alert(ip_listed, domain_listed)

    def _send_email_alert(
        self,
        ip_listed: list[BlacklistResult],
        domain_listed: list[BlacklistResult],
    ) -> None:
        """Send email alert for listed IPs and domains."""
        if not self.config.alert_enabled or not self.config.alert_recipients:
            return

        # Filter by cooldown
        ip_to_alert = [r for r in ip_listed if self._should_alert(r)]
        domain_to_alert = [r for r in domain_listed if self._should_alert(r)]

        if not ip_to_alert and not domain_to_alert:
            return

        total = len(ip_to_alert) + len(domain_to_alert)
        subject = f"{self.config.alert_subject_prefix} Found on {total} blacklist(s)"

        body_lines: list[str] = []

        if ip_to_alert:
            body_lines.extend(["=== IP BLACKLIST ALERTS ===", ""])
            for r in ip_to_alert:
                body_lines.append(f"  IP: {r.target}")
                body_lines.append(f"  Blacklist: {r.dnsbl}")
                body_lines.append(f"  Return Code: {r.return_code}")
                if r.reason:
                    body_lines.append(f"  Reason: {r.reason}")
                body_lines.append("")

        if domain_to_alert:
            body_lines.extend(["=== DOMAIN BLACKLIST ALERTS ===", ""])
            for r in domain_to_alert:
                body_lines.append(f"  Domain: {r.target}")
                body_lines.append(f"  Blacklist: {r.dnsbl}")
                body_lines.append(f"  Return Code: {r.return_code}")
                if r.reason:
                    body_lines.append(f"  Reason: {r.reason}")
                body_lines.append("")

        body_lines.extend(
            [
                "=== ACTION REQUIRED ===",
                "",
                "For IP delisting:",
                "  1. Check https://mxtoolbox.com/blacklists.aspx",
                "  2. Review mail server logs for potential abuse",
                "  3. Submit delisting requests to RBL operators",
                "",
                "For domain delisting:",
                "  1. Check https://mxtoolbox.com/domain/",
                "  2. Review sending practices and content",
                "  3. Contact domain blacklist operators",
                "",
                f"Generated at {datetime.now(timezone.utc).isoformat()}Z by mail-relay blacklist monitor",
            ]
        )

        try:
            msg = MIMEMultipart()
            msg["Subject"] = subject
            msg["From"] = self.config.alert_from
            msg["To"] = ", ".join(self.config.alert_recipients)
            msg.attach(MIMEText("\n".join(body_lines), "plain"))

            with smtplib.SMTP(
                self.config.alert_smtp_host, self.config.alert_smtp_port
            ) as server:
                server.send_message(msg)

            for r in ip_to_alert + domain_to_alert:
                self._mark_alerted(r)

            self.logger.info(f"Sent email alert to {self.config.alert_recipients}")

        except Exception as e:
            self.logger.error(f"Failed to send email alert: {e}")

    def _send_webhook_alert(
        self,
        ip_listed: list[BlacklistResult],
        domain_listed: list[BlacklistResult],
    ) -> None:
        """Send webhook notification for listed IPs and domains."""
        if not self.config.webhook_enabled or not self.config.webhook_url:
            return

        if not ip_listed and not domain_listed:
            return

        payload: dict[str, Any] = {
            "event": "blacklist_alert",
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "ip_listings": [
                {
                    "ip": r.target,
                    "blacklist": r.dnsbl,
                    "return_code": r.return_code,
                    "reason": r.reason,
                }
                for r in ip_listed
            ],
            "domain_listings": [
                {
                    "domain": r.target,
                    "blacklist": r.dnsbl,
                    "return_code": r.return_code,
                    "reason": r.reason,
                }
                for r in domain_listed
            ],
        }

        try:
            response = requests.post(
                self.config.webhook_url,
                json=payload,
                timeout=self.config.webhook_timeout,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            self.logger.info(f"Sent webhook alert to {self.config.webhook_url}")
        except Exception as e:
            self.logger.error(f"Failed to send webhook alert: {e}")
