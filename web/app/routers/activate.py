"""Активация кодов Roblox на сайте (аналог «Активировать Код» в боте).

Клиент вводит код → создаёт Game Pass → присылает ник → заявка уходит
админу в Telegram (как из бота) и появляется у клиента в «Мои активации».
Переиспользует bot.services.activation и тот же пакет roblox.
"""

from __future__ import annotations

import asyncio
import os
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import WebAccount
from bot.services import activation as act

from ..config import settings
from ..db import get_session
from ..deps import current_account

router = APIRouter(prefix="/api/activate", tags=["activate"])

_CODE_RE = re.compile(r"\d{3}-\d{3}-\d{3}")

_STATUS_LABEL = {
    "review": "На проверке",
    "approved": "Выполнено",
    "rejected": "Отклонено",
}


def _admin_ids() -> list[int]:
    raw = os.getenv("ADMIN_IDS", "") or ""
    out = []
    for part in raw.replace(",", " ").split():
        try:
            out.append(int(part))
        except ValueError:
            pass
    return out


def _roblox():
    """Ленивая загрузка пакета roblox (может отсутствовать сеть к Roblox)."""
    import roblox  # noqa: PLC0415
    return roblox


class CheckIn(BaseModel):
    code: str


class SubmitIn(BaseModel):
    code: str
    nickname: str


@router.post("/check")
async def check(body: CheckIn, session: AsyncSession = Depends(get_session)) -> dict:
    """Проверить код: not_found | used | ok (+ товар, инструкция, цена)."""
    code = (body.code or "").strip()
    if not _CODE_RE.fullmatch(code):
        raise HTTPException(status_code=400, detail="Код в формате XXX-XXX-XXX")
    c = await act.find_code(session, code)
    if not c:
        return {"status": "not_found"}
    if c.status == "used":
        return {"status": "used"}
    product = c.product or ""
    expected = None
    try:
        rb = _roblox()
        expected = rb.expected_gamepass_price(rb.parse_robux_amount(product))
    except Exception:  # noqa: BLE001
        expected = None
    return {
        "status": "ok",
        "product": product,
        "instruction": c.instruction or "",
        "expected_price": expected,
    }


@router.post("/submit")
async def submit(
    body: SubmitIn,
    acc: WebAccount = Depends(current_account),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Отправить заявку: пометить код used, создать заявку, уведомить админа."""
    code = (body.code or "").strip()
    nick = (body.nickname or "").strip().lstrip("@")
    if not _CODE_RE.fullmatch(code):
        raise HTTPException(status_code=400, detail="Код в формате XXX-XXX-XXX")
    if not nick:
        raise HTTPException(status_code=400, detail="Укажите ник Roblox")

    c = await act.find_code(session, code)
    if not c:
        raise HTTPException(status_code=400, detail="Код не найден")
    if c.status == "used":
        raise HTTPException(status_code=400, detail="Этот код уже активирован")

    product = c.product or ""
    expected = robux = None
    try:
        rb = _roblox()
        robux = rb.parse_robux_amount(product)
        expected = rb.expected_gamepass_price(robux)
    except Exception:  # noqa: BLE001
        pass

    await act.mark_used(session, code, acc.id)
    req = await act.create_request(
        session,
        code=code, product=product,
        user_id=acc.telegram_user_id or 0,
        username=acc.email or "сайт",
        nickname=nick, expected_price=expected, actual_price=None,
        web_account_id=acc.id,
    )
    await session.flush()
    rid = req.id

    # Авто-отчёт Roblox (best-effort) + фактическая цена.
    report, actual = "", None
    try:
        rb = _roblox()
        report, _uid, actual = await asyncio.to_thread(
            rb.build_gamepass_report, nick, expected, robux
        )
        if actual is not None:
            req.actual_price = actual
    except Exception:  # noqa: BLE001
        report = "⚠️ Автопроверку Roblox выполнить не удалось — проверьте вручную."

    await session.commit()

    await _notify_admins(code, product, nick, acc, report, rid)
    return {"ok": True, "request_id": rid, "product": product, "nickname": nick}


@router.get("/mine")
async def mine(
    acc: WebAccount = Depends(current_account),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Список заявок текущего аккаунта со статусами («Мои активации»)."""
    from ..textfmt import clean_plain
    rows = await act.list_for_account(session, acc.id)
    items = [{
        "id": r.id,
        "code": r.code,
        "product": r.product,
        "nickname": r.nickname,
        "status": r.status,
        "status_label": _STATUS_LABEL.get(r.status, r.status),
        "expected_price": r.expected_price,
        "actual_price": r.actual_price,
        "reason": clean_plain(r.reason) if r.reason else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
    } for r in rows]
    return {"items": items}


async def _notify_admins(code, product, nick, acc: WebAccount, report, rid) -> None:
    """Отправить заявку админам в Telegram теми же кнопками ✅/❌, что и из бота.

    Решение админ принимает в Telegram — обрабатывает существующий код бота
    (aok:/aerr:), т.к. база общая. Лучший-эффорт: сбой не ломает заявку.
    """
    token = (settings.telegram_bot_token or "").strip()
    admins = _admin_ids()
    if not token or not admins:
        return
    who = acc.email or (f"Telegram {acc.telegram_user_id}" if acc.telegram_user_id else "клиент сайта")
    text = (
        "📢 <b>Новая заявка на активацию (с сайта)!</b>\n\n"
        f"🌐 Клиент: {who}\n"
        f"🔑 Код: <code>{code}</code>\n"
        f"📦 Товар: {product}\n"
        f"🎮 Ник: {nick}\n\n"
        f"{report}"
    )
    if len(text) > 4000:
        text = text[:3950] + "\n\n… (обрезано)"
    markup = {"inline_keyboard": [[
        {"text": "✅ Успешно", "callback_data": f"aok:{rid}"},
        {"text": "❌ Ошибка", "callback_data": f"aerr:{rid}"},
    ]]}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for chat_id in admins:
                try:
                    await client.post(url, json={
                        "chat_id": chat_id, "text": text,
                        "parse_mode": "HTML", "reply_markup": markup,
                        "disable_web_page_preview": True,
                    })
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
