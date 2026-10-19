"""Умная цена: ₽-цена с зафиксированной наценкой, плавающей от живого закупа.

Режим ``price_mode="smart"`` у номинала:
  * админ задаёт цену в ₽ → из текущего закупа выводится и фиксируется наценка %;
  * дальше цена = ``живой_закуп × (1 + наценка%)`` (округление вверх до 1 ₽),
    хранится в ``price_rub`` как «липкая»;
  * порог 1%: мелкие изменения закупа цену не трогают;
  * при росте > 1% — цена поднимается и уведомление администратору;
  * при падении > 1% — цена снижается и уведомление администратору;
  * стоп-лосс: если цена оказалась ниже живого закупа — продажа блокируется.

Работает только для поставщиков с живым закупом по API (пока FazerCard).
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import List, Optional

from sqlalchemy import select

from ..db import Database
from ..db.models import Variant
from . import catalog as catalog_service
from . import rates as rates_service

log = logging.getLogger("vallshop.smartprice")

MODE_SMART = "smart"
MODE_FIXED = "fixed"
MODE_FLOAT = "float"

TOLERANCE = Decimal("0.01")   # 1% — ниже порога цену не меняем
ALERT_UP = Decimal("0.01")    # уведомляем при росте > 1%
ALERT_DOWN = Decimal("0.01")  # уведомляем при падении > 1%
_ONE = Decimal("1")

# Источники, умеющие отдавать живой закуп по API.
LIVE_COST_SOURCES = {"fazercard"}


def supports_smart(variant) -> bool:
    return getattr(variant, "source", "stock") in LIVE_COST_SOURCES


def derive_markup(price_rub, cost_usd, rate) -> Decimal:
    """Вывести наценку % из ₽-цены и закупа (USD) по курсу."""
    cost_rub = Decimal(cost_usd) * Decimal(rate)
    if cost_rub <= 0:
        return Decimal("0")
    return ((Decimal(price_rub) / cost_rub) - 1) * 100


def target_rub(cost_usd, markup_percent, rate) -> Decimal:
    """Целевая ₽-цена = закуп × (1 + наценка%) × курс (до 10 коп. <100 ₽, иначе до 1 ₽)."""
    from .pricing import round_price_rub
    val = Decimal(cost_usd) * Decimal(rate) * (Decimal(1) + Decimal(markup_percent) / 100)
    return round_price_rub(val)


async def live_cost(fzr, variant) -> Optional[Decimal]:
    m = await catalog_service.fazercard_cost(fzr, [variant])
    c = m.get(variant.id)
    return Decimal(c) if c is not None else None


async def recompute_all(db: Database, fzr, *, bot=None, admin_ids=None) -> list:
    """Пересчитать умные цены по живому закупу (порог 1%). Возвращает изменения.

    Обновляет ``price_rub`` (липкую цену) и ``cost_usd`` (для маржи/статистики),
    при росте > 1% собирает алерты и шлёт их администраторам.
    """
    rate = rates_service.get_rate()
    changes: list = []
    alerts: list = []
    drop_alerts: list = []
    async with db.session() as session:
        variants = list(
            await session.scalars(select(Variant).where(Variant.price_mode == MODE_SMART))
        )
        if not variants:
            return changes
        cost_map = await catalog_service.fazercard_cost(fzr, variants)
        for v in variants:
            cost = cost_map.get(v.id)
            if cost is None or Decimal(cost) <= 0:
                continue
            cost = Decimal(cost)
            old_cost = Decimal(v.cost_usd or 0)
            mk = v.markup_percent if v.markup_percent is not None else Decimal("0")
            target = target_rub(cost, mk, rate)
            v.cost_usd = cost  # всегда обновляем закуп (маржа/статистика)
            cur = Decimal(v.price_rub) if v.price_rub is not None else None
            if cur is None or cur <= 0:
                v.price_rub = target
                continue
            if abs(target - cur) / cur > TOLERANCE:
                v.price_rub = target
                changes.append((v.id, v.title, cur, target))
                if target > cur * (Decimal(1) + ALERT_UP):
                    alerts.append((v.title, cur, target, old_cost, cost))
                elif target < cur * (Decimal(1) - ALERT_DOWN):
                    drop_alerts.append((v.title, cur, target, old_cost, cost))
        await session.commit()

    if (alerts or drop_alerts) and bot is not None and admin_ids:
        from . import notify as notify_service
        if alerts:
            lines = ["📈 <b>Цены выросли (поднялся закуп):</b>", ""]
            for title, old, new, old_cost, new_cost in alerts:
                pct = (new - old) / old * 100
                lines.append(
                    f"• {title}: {int(old)} → {int(new)} ₽ (+{pct:.1f}%)\n"
                    f"   закуп: {old_cost:.2f} → {new_cost:.2f} $"
                )
            try:
                await notify_service.notify_admins(bot, admin_ids, "\n".join(lines))
            except Exception:  # noqa: BLE001
                log.warning("Не удалось отправить алерт о росте цен")
        if drop_alerts:
            lines = ["📉 <b>Цены упали (снизился закуп):</b>", ""]
            for title, old, new, old_cost, new_cost in drop_alerts:
                pct = (old - new) / old * 100
                lines.append(
                    f"• {title}: {int(old)} → {int(new)} ₽ (−{pct:.1f}%)\n"
                    f"   закуп: {old_cost:.2f} → {new_cost:.2f} $"
                )
            try:
                await notify_service.notify_admins(bot, admin_ids, "\n".join(lines))
            except Exception:  # noqa: BLE001
                log.warning("Не удалось отправить алерт о падении цен")
    return changes


async def recompute_socproof(db: Database, soc, *, bot=None, admin_ids=None) -> list:
    """SMM soc-proof: сверить закуп (₽/1000) каждой услуги с живым и обновить.

    Цена SMM = ``закуп × (1+наценка/100)``, поэтому достаточно обновить ``smm_rate``
    — цена пересчитается автоматически везде. Порог 1%: при изменении закупа > 1%
    обновляем закуп и шлём админу алерт (рост/падение). Работает для всех
    SMM-номиналов без ручного включения.
    """
    from . import smm as smm_service
    if soc is None or not getattr(soc, "configured", False):
        return []
    try:
        services = await asyncio.to_thread(soc.services, force=True)
    except Exception:  # noqa: BLE001
        log.warning("soc-proof: не удалось получить услуги для пересчёта цен")
        return []
    rate_map = {}
    for s in services:
        try:
            rate_map[int(s.get("id"))] = Decimal(str(s.get("rate") or "0"))
        except (InvalidOperation, TypeError, ValueError):
            continue

    changes: list = []
    ups: list = []
    downs: list = []
    async with db.session() as session:
        variants = list(
            await session.scalars(select(Variant).where(Variant.source == "socproof"))
        )
        if not variants:
            return []
        for v in variants:
            if not v.smm_service:
                continue
            live = rate_map.get(int(v.smm_service))
            if live is None or live <= 0:
                continue  # услуга пропала — это обрабатывает «наличие», цену не трогаем
            old = Decimal(v.smm_rate or 0)
            mk = v.markup_percent if v.markup_percent is not None else Decimal("0")
            if old <= 0:
                v.smm_rate = live
                continue
            if abs(live - old) / old > TOLERANCE:
                old_sell = smm_service.sell_per_1000_rub(old, mk)
                new_sell = smm_service.sell_per_1000_rub(live, mk)
                v.smm_rate = live
                changes.append((v.id, v.title, old_sell, new_sell))
                (ups if live > old else downs).append((v.title, old_sell, new_sell, old, live))
        await session.commit()

    if (ups or downs) and bot is not None and admin_ids:
        from . import notify as notify_service
        if ups:
            lines = ["📈 <b>SMM: цены выросли (поднялся закуп):</b>", ""]
            for title, old, new, oc, nc in ups:
                pct = (new - old) / old * 100 if old else Decimal(0)
                lines.append(
                    f"• {title}: {old:.2f} → {new:.2f} ₽/1000 (+{pct:.1f}%)\n"
                    f"   закуп: {oc:.2f} → {nc:.2f} ₽/1000"
                )
            try:
                await notify_service.notify_admins(bot, admin_ids, "\n".join(lines))
            except Exception:  # noqa: BLE001
                log.warning("Не удалось отправить SMM-алерт о росте цен")
        if downs:
            lines = ["📉 <b>SMM: цены упали (снизился закуп):</b>", ""]
            for title, old, new, oc, nc in downs:
                pct = (old - new) / old * 100 if old else Decimal(0)
                lines.append(
                    f"• {title}: {old:.2f} → {new:.2f} ₽/1000 (−{pct:.1f}%)\n"
                    f"   закуп: {oc:.2f} → {nc:.2f} ₽/1000"
                )
            try:
                await notify_service.notify_admins(bot, admin_ids, "\n".join(lines))
            except Exception:  # noqa: BLE001
                log.warning("Не удалось отправить SMM-алерт о падении цен")
    return changes


async def migrate_supplier_to_smart(db: Database, fzr, default_markup) -> tuple:
    """Массово перевести товары поставщика (живой закуп) на умную цену.

    Берёт текущую ₽-цену номинала, живой закуп и фиксирует наценку. Не трогает
    свой сток/LioGames/VPN и то, что уже на фикс-цене или уже умное.
    Возвращает ``(переведено, пропущено)``.
    """
    from .pricing import price_rub_value

    rate = rates_service.get_rate()
    converted = skipped = 0
    async with db.session() as session:
        variants = list(await session.scalars(select(Variant)))
        supplier = [v for v in variants if supports_smart(v)]
        todo = [v for v in supplier if v.price_mode not in (MODE_FIXED, MODE_SMART)]
        cost_map = await catalog_service.fazercard_cost(fzr, todo)
        for v in supplier:
            if v.price_mode in (MODE_FIXED, MODE_SMART):
                skipped += 1
                continue
            cost = cost_map.get(v.id)
            if cost is None or Decimal(cost) <= 0:
                skipped += 1
                continue
            cur = price_rub_value(v, default_markup)  # текущая ₽-цена
            v.markup_percent = derive_markup(cur, cost, rate)
            v.cost_usd = Decimal(cost)
            v.price_rub = Decimal(cur)
            v.price_usd = None
            v.price_mode = MODE_SMART
            converted += 1
        await session.commit()
    return converted, skipped


async def stop_loss_violation(fzr, variant, price_rub, rate=None) -> bool:
    """True, если ₽-цена ниже живого закупа (продавать нельзя)."""
    if not supports_smart(variant):
        return False
    r = rate if rate is not None else rates_service.get_rate()
    cost = await live_cost(fzr, variant)
    if cost is None:
        return False  # закуп неизвестен — не блокируем продажу
    cost_rub = (Decimal(cost) * Decimal(r)).quantize(_ONE, rounding=ROUND_CEILING)
    return Decimal(price_rub) < cost_rub
