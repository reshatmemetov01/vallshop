"""Отправка писем через SMTP (подтверждение email, сброс пароля).

SMTP не настроен → send() тихо возвращает False (сайт работает и без писем).
Отправка синхронным smtplib в отдельном потоке, чтобы не блокировать event loop.
"""

from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage

from ..config import settings

log = logging.getLogger("vallshop.web.email")


def _text_from_html(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def _send_sync(to: str, subject: str, html: str, text: str) -> None:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    if settings.smtp_ssl:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=ctx) as s:
            s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)


async def send(to: str, subject: str, html: str) -> bool:
    """Отправить письмо. Возвращает True при успехе, False — если не настроено/ошибка."""
    if not settings.smtp_configured:
        log.info("SMTP не настроен — письмо %s не отправлено", subject)
        return False
    try:
        await asyncio.to_thread(_send_sync, to, subject, html, _text_from_html(html))
        return True
    except Exception:  # noqa: BLE001 — сбой почты не должен ронять запрос
        log.exception("Ошибка отправки письма на %s", to)
        return False
