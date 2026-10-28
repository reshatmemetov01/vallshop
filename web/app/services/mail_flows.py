"""Готовые письма сайта: подтверждение email и сброс пароля."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import WebAccount

from ..config import settings
from . import email as email_service
from . import tokens as token_service


def _link(path: str, token: str) -> str:
    return f"{settings.base_url.rstrip('/')}{path}?token={token}"


def _wrap(heading: str, intro: str, button_label: str, url: str, note: str) -> str:
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#0e1116;
  padding:24px;color:#e8edf3">
  <div style="max-width:480px;margin:0 auto;background:#171b22;border:1px solid #2a303a;
    border-radius:14px;padding:28px">
    <div style="font-weight:800;font-size:22px;margin-bottom:18px">Vall<span style="color:#ff7a2f">Shop</span></div>
    <h2 style="margin:0 0 12px;font-size:19px">{heading}</h2>
    <p style="color:#c3ccd8;line-height:1.5;margin:0 0 20px">{intro}</p>
    <a href="{url}" style="display:inline-block;background:#ff7a2f;color:#1a0f07;
      text-decoration:none;font-weight:700;padding:12px 22px;border-radius:10px">{button_label}</a>
    <p style="color:#8b96a5;font-size:13px;margin:22px 0 0;line-height:1.5">{note}</p>
    <p style="color:#8b96a5;font-size:12px;margin:14px 0 0;word-break:break-all">
      Если кнопка не работает, откройте ссылку:<br>{url}</p>
  </div>
</div>"""


async def send_verification(session: AsyncSession, account: WebAccount) -> bool:
    if not account.email:
        return False
    token = await token_service.issue(
        session, account.id, token_service.VERIFY, token_service.VERIFY_TTL
    )
    html = _wrap(
        "Подтвердите email",
        "Вы зарегистрировались на VallShop. Подтвердите адрес, чтобы обезопасить аккаунт.",
        "Подтвердить email",
        _link("/verify", token),
        "Ссылка действует 2 дня. Если вы не регистрировались — просто игнорируйте письмо.",
    )
    return await email_service.send(account.email, "Подтверждение email — VallShop", html)


async def send_password_reset(session: AsyncSession, account: WebAccount) -> bool:
    if not account.email:
        return False
    token = await token_service.issue(
        session, account.id, token_service.RESET, token_service.RESET_TTL
    )
    html = _wrap(
        "Сброс пароля",
        "Вы запросили сброс пароля на VallShop. Нажмите кнопку, чтобы задать новый пароль.",
        "Сбросить пароль",
        _link("/reset", token),
        "Ссылка действует 2 часа. Если вы не запрашивали сброс — просто игнорируйте письмо.",
    )
    return await email_service.send(account.email, "Сброс пароля — VallShop", html)
