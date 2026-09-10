"""Telegram delivery.

Duplicate suppression is a *database* guarantee, not an in-memory one: every
message carries a deterministic ``dedup_key`` and ``sent_alerts.dedup_key`` is
UNIQUE. A restart, a second worker, or a re-run of the same collection cycle
therefore cannot re-send an alert that already went out.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import AlertType
from app.core.logging_config import get_logger
from app.models import SentAlert

log = get_logger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
#: Telegram rejects messages longer than 4096 characters.
MAX_MESSAGE_LENGTH = 4096


@dataclass(slots=True)
class DeliveryResult:
    sent: bool
    skipped_reason: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.sent


class TelegramNotifier:
    """Sends messages, recording each one so it is never sent twice."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.bot_token = bot_token or settings.telegram_bot_token
        self.chat_id = chat_id or settings.telegram_chat_id
        self.timeout = timeout or settings.telegram_timeout_seconds

    @property
    def is_configured(self) -> bool:
        return bool(settings.telegram_enabled and self.bot_token and self.chat_id)

    async def send(
        self,
        session: Session,
        *,
        alert_type: AlertType,
        dedup_key: str,
        text: str,
        parse_mode: str | None = "MarkdownV2",
        flight_id: int | None = None,
    ) -> DeliveryResult:
        """Deliver one message, unless an identical one has already gone out.

        The ledger row is claimed *before* the network call so two concurrent
        workers cannot both decide they are the first to send.
        """
        if not self.is_configured:
            log.debug("telegram.skipped", reason="not configured", dedup_key=dedup_key)
            return DeliveryResult(sent=False, skipped_reason="telegram not configured")

        if not _claim(session, alert_type, dedup_key, chat_id=self.chat_id, flight_id=flight_id):
            log.debug("telegram.duplicate_suppressed", dedup_key=dedup_key)
            return DeliveryResult(sent=False, skipped_reason="duplicate")

        payload = {
            "chat_id": self.chat_id,
            "text": _truncate(text),
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        url = f"{TELEGRAM_API_BASE}/bot{self.bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            _mark_failed(session, dedup_key, str(exc))
            log.error("telegram.transport_error", dedup_key=dedup_key, error=str(exc))
            return DeliveryResult(sent=False, error=str(exc))

        if response.status_code != 200:
            detail = response.text[:400]
            _mark_failed(session, dedup_key, detail)
            log.error(
                "telegram.delivery_failed",
                dedup_key=dedup_key,
                status_code=response.status_code,
                detail=detail,
            )
            return DeliveryResult(sent=False, error=detail)

        log.info("telegram.delivered", alert_type=alert_type.value, dedup_key=dedup_key)
        return DeliveryResult(sent=True)

    async def verify(self) -> dict[str, object]:
        """Call ``getMe`` so setup problems surface at ``/ready``, not at 03:00."""
        if not self.bot_token:
            return {"configured": False, "error": "TELEGRAM_BOT_TOKEN not set"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(f"{TELEGRAM_API_BASE}/bot{self.bot_token}/getMe")
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"configured": True, "reachable": False, "error": str(exc)}
        return {
            "configured": True,
            "reachable": bool(data.get("ok")),
            "bot_username": (data.get("result") or {}).get("username"),
            "chat_id_set": bool(self.chat_id),
        }


# --------------------------------------------------------------------- ledger
def _claim(
    session: Session,
    alert_type: AlertType,
    dedup_key: str,
    *,
    chat_id: str | None,
    flight_id: int | None,
) -> bool:
    """Insert the ledger row. ``False`` means someone already claimed this key."""
    savepoint = session.begin_nested()
    try:
        session.add(
            SentAlert(
                alert_type=alert_type,
                dedup_key=dedup_key,
                chat_id=chat_id,
                flight_id=flight_id,
                delivered=True,
            )
        )
        savepoint.commit()
        return True
    except IntegrityError:
        savepoint.rollback()
        return False


def _mark_failed(session: Session, dedup_key: str, error: str) -> None:
    """Record the failure and release the key so a later cycle can retry."""
    alert = session.query(SentAlert).filter(SentAlert.dedup_key == dedup_key).one_or_none()
    if alert is None:
        return
    alert.delivered = False
    alert.error = error[:1000]
    # Releasing the key matters: a transport blip must not permanently silence an
    # alert. Suffixing keeps the audit row while freeing the original key.
    alert.dedup_key = f"{dedup_key}#failed:{alert.id}"
    session.flush()


def _truncate(text: str) -> str:
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text
    marker = "\n... (truncated)"
    return text[: MAX_MESSAGE_LENGTH - len(marker)] + marker
