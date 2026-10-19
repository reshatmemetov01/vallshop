"""Обязательная подписка на канал (в разрезе магазина).

Настройки хранятся в merchant_settings: ссылка-кнопка (``channel_url``), цель для
проверки (``channel_chat`` — @username или числовой id канала) и тумблер
обязательности (``channel_required``). Значения кэшируются в памяти и
обновляются при изменении из админки.

Проверка подписки — через ``bot.get_chat_member``. Чтобы она работала, бот должен
быть администратором канала. Если проверить нельзя (не админ / ошибка) — считаем
результат неизвестным (вызывающий трактует как «пустить», fail-open).
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from ..db import Database
from ..db.models import HEAD_MERCHANT_ID
from . import msettings as msettings_service

log = logging.getLogger("vallshop.channel")

URL = "channel_url"          # ссылка-кнопка (https://t.me/... или invite-ссылка)
CHAT = "channel_chat"        # цель проверки: @username или числовой id (-100…)
REQUIRED = "channel_required"  # "1" — подписка обязательна

# Кэш настроек по мерчанту: merchant_id -> {"url","chat","required"}.
_cfg: Dict[int, dict] = {}

# Кэш статуса подписки: (bot_id, merchant_id, user_id) -> (ts, subscribed).
_sub: Dict[tuple, tuple] = {}
_SUB_TTL = 300.0             # сек — как долго доверяем «подписан» без повторной проверки


def _parse(vals: dict) -> dict:
    return {
        "url": vals.get(URL) or None,
        "chat": vals.get(CHAT) or None,
        "required": vals.get(REQUIRED) == "1",
    }


async def load(db: Database) -> None:
    """Прогреть кэш настроек каналов всех магазинов (на старте)."""
    global _cfg
    try:
        async with db.session() as s:
            grouped = await msettings_service.all_grouped(s)
        _cfg = {mid: _parse(vals) for mid, vals in grouped.items()}
    except Exception:  # noqa: BLE001 — кэш не должен ронять бота
        log.exception("Не удалось загрузить настройки каналов")


def config(merchant_id: int) -> dict:
    """Настройки канала магазина из кэша (url/chat/required)."""
    return _cfg.get(merchant_id) or {"url": None, "chat": None, "required": False}


async def refresh(db: Database, merchant_id: int) -> None:
    """Перечитать настройки одного магазина в кэш (после изменения)."""
    async with db.session() as s:
        vals = await msettings_service.all_for(s, merchant_id)
    _cfg[merchant_id] = _parse(vals)
    # Сбросить кэш подписок этого магазина — настройки изменились.
    for k in [k for k in _sub if k[1] == merchant_id]:
        _sub.pop(k, None)


async def set_url(db: Database, merchant_id: int, url: Optional[str]) -> None:
    async with db.session() as s:
        await msettings_service.set(s, merchant_id, URL, url)
        await s.commit()
    await refresh(db, merchant_id)


async def set_chat(db: Database, merchant_id: int, chat: Optional[str]) -> None:
    async with db.session() as s:
        await msettings_service.set(s, merchant_id, CHAT, chat)
        await s.commit()
    await refresh(db, merchant_id)


async def set_required(db: Database, merchant_id: int, required: bool) -> None:
    async with db.session() as s:
        await msettings_service.set(s, merchant_id, REQUIRED, "1" if required else "0")
        await s.commit()
    await refresh(db, merchant_id)


async def clear(db: Database, merchant_id: int) -> None:
    async with db.session() as s:
        for k in (URL, CHAT, REQUIRED):
            await msettings_service.set(s, merchant_id, k, None)
        await s.commit()
    await refresh(db, merchant_id)


def _invalidate_user(bot_id: int, merchant_id: int, user_id: int) -> None:
    _sub.pop((bot_id, merchant_id, user_id), None)


async def _query(bot, chat, user_id: int) -> Optional[bool]:
    """Один запрос в Telegram: подписан ли. None — проверить нельзя (fail-open)."""
    try:
        member = await bot.get_chat_member(chat, user_id)
        status = getattr(member, "status", None)
        status = getattr(status, "value", status)  # enum → str
        return status in ("creator", "administrator", "member")
    except Exception:  # noqa: BLE001 — бот не админ канала / канал не найден / сеть
        return None


async def is_subscribed(
    bot, merchant_id: int, user_id: int, *, force: bool = False
) -> Optional[bool]:
    """Подписан ли пользователь на канал магазина. None — неизвестно (fail-open).

    Кэшируем результат «подписан» на ``_SUB_TTL`` секунд. ``force`` — всегда
    свежая проверка (кнопка «Проверить подписку»)."""
    cfg = config(merchant_id)
    chat = cfg.get("chat")
    if not chat:
        return None  # нечего проверять
    key = (getattr(bot, "id", 0), merchant_id, user_id)
    now = time.time()
    if not force:
        cached = _sub.get(key)
        if cached and cached[1] and (now - cached[0]) < _SUB_TTL:
            return True  # недавно подтверждено, что подписан
    result = await _query(bot, chat, user_id)
    if result is not None:
        _sub[key] = (now, result)
    return result
