"""Email and Webhook delivery providers.

Implements standard provider interface with:
- Strict status categorization: accepted | retryable_not_accepted | permanent_failure | unknown.
- Idempotency support.
- Sensitive credential masking in logs and errors.
"""

from __future__ import annotations

import email.message
import email.utils
import json
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import cost_tracker

ProviderStatus = Literal["accepted", "retryable_not_accepted", "permanent_failure", "unknown"]


@dataclass
class DeliveryResult:
    status: ProviderStatus
    provider_message_id: str | None = None
    safe_error: str | None = None
    retry_after_seconds: int | None = None


def mask_sensitive(text: str) -> str:
    """Mask credentials, tokens, or passwords in error messages."""
    if not text:
        return ""
    # Truncate long error bodies and remove potential key sequences
    cleaned = str(text)
    for key_env in ("RESEND_API_KEY", "SMTP_PASSWORD", "RSS_NOTIFY_WEBHOOK", "DISCORD_WEBHOOK_URL"):
        val = os.getenv(key_env)
        if val and len(val) > 4:
            cleaned = cleaned.replace(val, f"***{key_env}***")
    return cleaned[:400]


class BaseProvider:
    """Abstract interface for notification and email delivery."""

    def send(
        self,
        recipient: str,
        subject: str,
        text_body: str,
        html_body: str | None,
        idempotency_key: str,
        sender: str | None = None,
    ) -> DeliveryResult:
        raise NotImplementedError


class FakeProvider(BaseProvider):
    """Configurable mock provider for test suite."""

    def __init__(
        self,
        mode: ProviderStatus = "accepted",
        message_id: str = "fake_msg_123",
        safe_error: str | None = None,
        retry_after: int | None = None,
        crash_before_return: bool = False,
    ):
        self.mode = mode
        self.message_id = message_id
        self.safe_error = safe_error
        self.retry_after = retry_after
        self.crash_before_return = crash_before_return
        self.history: list[dict[str, Any]] = []

    def send(
        self,
        recipient: str,
        subject: str,
        text_body: str,
        html_body: str | None,
        idempotency_key: str,
        sender: str | None = None,
    ) -> DeliveryResult:
        self.history.append(
            {
                "recipient": recipient,
                "subject": subject,
                "text_body": text_body,
                "html_body": html_body,
                "idempotency_key": idempotency_key,
                "sender": sender,
                "timestamp": time.time(),
            }
        )
        if self.crash_before_return:
            raise OSError("Simulated process crash during delivery")

        return DeliveryResult(
            status=self.mode,
            provider_message_id=self.message_id if self.mode == "accepted" else None,
            safe_error=self.safe_error,
            retry_after_seconds=self.retry_after,
        )


class ResendProvider(BaseProvider):
    """Resend REST API provider supporting 24h idempotency keys."""

    def __init__(self, api_key: str | None = None, base_url: str = "https://api.resend.com"):
        self.api_key = api_key or os.getenv("RESEND_API_KEY", "")
        self.base_url = base_url.rstrip("/")

    def send(
        self,
        recipient: str,
        subject: str,
        text_body: str,
        html_body: str | None,
        idempotency_key: str,
        sender: str | None = None,
    ) -> DeliveryResult:
        if not self.api_key:
            return DeliveryResult(
                status="permanent_failure",
                safe_error="RESEND_API_KEY is unset",
            )
        from_email = sender or os.getenv("EMAIL_FROM", "digest@example.com")
        if not recipient:
            return DeliveryResult(status="permanent_failure", safe_error="Recipient email is empty")

        payload = {
            "from": from_email,
            "to": [recipient],
            "subject": subject,
            "text": text_body,
        }
        if html_body:
            payload["html"] = html_body

        req = urllib.request.Request(
            f"{self.base_url}/emails",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key[:255],
                "User-Agent": "VideoTimelinePipeline/1.0",
            },
            method="POST",
        )

        try:
            def send_resend() -> dict[str, Any]:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return json.loads(resp.read().decode("utf-8"))

            resp_data = cost_tracker.guarded_call("resend", "resend:email", send_resend)
            return DeliveryResult(
                    status="accepted",
                    provider_message_id=resp_data.get("id"),
                )
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="ignore")
            # 429: Rate limited
            if exc.code == 429:
                retry_header = exc.headers.get("Retry-After")
                retry_after = int(retry_header) if retry_header and retry_header.isdigit() else 30
                return DeliveryResult(
                    status="retryable_not_accepted",
                    safe_error=mask_sensitive(f"Resend HTTP 429 rate limit: {err_body}"),
                    retry_after_seconds=retry_after,
                )
            # 5xx: Transient server error
            if exc.code >= 500:
                return DeliveryResult(
                    status="retryable_not_accepted",
                    safe_error=mask_sensitive(f"Resend HTTP {exc.code}: {err_body}"),
                    retry_after_seconds=60,
                )
            # 401/403/422/400: Permanent configuration or validation failure
            return DeliveryResult(
                status="permanent_failure",
                safe_error=mask_sensitive(f"Resend HTTP {exc.code}: {err_body}"),
            )
        except (TimeoutError, urllib.error.URLError) as exc:
            # Network timeout after connection established -> status unknown
            return DeliveryResult(
                status="unknown",
                safe_error=mask_sensitive(f"Network timeout/disruption during Resend request: {exc}"),
            )
        except Exception as exc:
            return DeliveryResult(
                status="unknown",
                safe_error=mask_sensitive(f"Unexpected error during Resend request: {exc}"),
            )


class SmtpProvider(BaseProvider):
    """Standard SMTP / STARTTLS provider."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = True,
    ):
        self.host = host or os.getenv("SMTP_HOST", "localhost")
        self.port = port or int(os.getenv("SMTP_PORT", "587"))
        self.username = username or os.getenv("SMTP_USER", "")
        self.password = password or os.getenv("SMTP_PASSWORD", "")
        self.use_tls = use_tls

    def send(
        self,
        recipient: str,
        subject: str,
        text_body: str,
        html_body: str | None,
        idempotency_key: str,
        sender: str | None = None,
    ) -> DeliveryResult:
        from_email = sender or os.getenv("EMAIL_FROM", "digest@example.com")
        msg = email.message.EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = recipient
        msg["Message-ID"] = email.utils.make_msgid(domain=urlparse(f"http://{self.host}").hostname or "local")
        msg["X-Idempotency-Key"] = idempotency_key

        msg.set_content(text_body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")

        try:
            context = ssl.create_default_context() if self.use_tls else None
            def send_smtp() -> DeliveryResult:
                with smtplib.SMTP(self.host, self.port, timeout=25) as server:
                    if self.use_tls:
                        server.starttls(context=context)
                    if self.username and self.password:
                        server.login(self.username, self.password)
                    server.send_message(msg)
                    return DeliveryResult(
                        status="accepted",
                        provider_message_id=msg["Message-ID"],
                    )

            return cost_tracker.guarded_call("smtp", "smtp:email", send_smtp)
        except smtplib.SMTPAuthenticationError as exc:
            return DeliveryResult(
                status="permanent_failure",
                safe_error=mask_sensitive(f"SMTP authentication failed: {exc}"),
            )
        except smtplib.SMTPRecipientsRefused as exc:
            return DeliveryResult(
                status="permanent_failure",
                safe_error=mask_sensitive(f"Recipient refused: {exc}"),
            )
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, TimeoutError, OSError) as exc:
            # Timeout during or after transmission might mean it arrived
            return DeliveryResult(
                status="unknown",
                safe_error=mask_sensitive(f"SMTP socket disruption: {exc}"),
            )
        except Exception as exc:
            return DeliveryResult(
                status="unknown",
                safe_error=mask_sensitive(f"SMTP send error: {exc}"),
            )


class WebhookProvider(BaseProvider):
    """Webhook and Discord notification provider."""

    def __init__(self, webhook_url: str | None = None):
        self.webhook_url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL") or os.getenv("NOTIFY_WEBHOOK_URL") or os.getenv("RSS_NOTIFY_WEBHOOK")

    def send(
        self,
        recipient: str,
        subject: str,
        text_body: str,
        html_body: str | None,
        idempotency_key: str,
        sender: str | None = None,
    ) -> DeliveryResult:
        target_url = recipient if recipient.startswith("http") else self.webhook_url
        if not target_url:
            return DeliveryResult(status="permanent_failure", safe_error="No webhook URL provided")

        is_discord = "discord.com" in target_url or "discordapp.com" in target_url
        if is_discord:
            # Discord webhook JSON format
            body = {
                "content": f"**{subject}**\n\n{text_body[:1800]}",
            }
        else:
            body = {
                "subject": subject,
                "text": text_body,
                "idempotency_key": idempotency_key,
            }

        req = urllib.request.Request(
            target_url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "VideoTimelinePipeline/1.0"},
            method="POST",
        )

        try:
            def send_webhook() -> DeliveryResult:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    resp.read(512)
                    return DeliveryResult(status="accepted", provider_message_id=f"hook_{int(time.time())}")

            return cost_tracker.guarded_call("webhook", "webhook:notify", send_webhook)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="ignore")
            if exc.code == 429:
                return DeliveryResult(
                    status="retryable_not_accepted",
                    safe_error=f"Webhook 429: {err_body[:200]}",
                    retry_after_seconds=15,
                )
            if exc.code >= 500:
                return DeliveryResult(
                    status="retryable_not_accepted",
                    safe_error=f"Webhook {exc.code}: {err_body[:200]}",
                    retry_after_seconds=30,
                )
            return DeliveryResult(
                status="permanent_failure",
                safe_error=f"Webhook {exc.code}: {err_body[:200]}",
            )
        except Exception as exc:
            return DeliveryResult(
                status="unknown",
                safe_error=f"Webhook error: {mask_sensitive(str(exc))}",
            )


def get_configured_provider(name: str | None = None) -> BaseProvider:
    """Return provider instance according to configuration."""
    provider_name = (name or os.getenv("EMAIL_PROVIDER", "")).strip().lower()
    if provider_name == "resend":
        return ResendProvider()
    if provider_name == "smtp":
        return SmtpProvider()
    if provider_name in ("webhook", "discord"):
        return WebhookProvider()
    if provider_name == "fake":
        return FakeProvider()
    # Default fallback: if Webhook URL exists, use WebhookProvider, else SmtpProvider
    if os.getenv("DISCORD_WEBHOOK_URL") or os.getenv("RSS_NOTIFY_WEBHOOK"):
        return WebhookProvider()
    return SmtpProvider()
